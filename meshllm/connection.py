"""How the bridge reaches the radio: USB serial, Wi-Fi (TCP) or Bluetooth (BLE).

The bridge's reconnect loop (`Bridge.connect_loop` in bridge.py) does not care how the radio is connected. It asks an `Endpoint` which
targets are worth trying, opens one, asks the endpoint whether the link is still healthy, and closes it when it is not. Each mode
supplies the parts that differ:

* `SerialEndpoint`: a USB port, auto-detected by chip vendor or pinned with `--port`. Healthy = the library's reader thread is alive and the
  port is still listed by the OS.
* `TcpEndpoint`: a radio on the LAN with Wi-Fi enabled (`--tcp HOST[:PORT]`, default port 4403). Healthy = the socket exists and the reader
  thread is alive.
* `BleEndpoint`: Bluetooth (`--ble ADDRESS_OR_NAME`). Healthy = the BLE client exists and the library's receive thread is alive. `bleak` and the
  library's BLE module are imported only when this mode is used, so a USB-only user never loads them.

A target's *label* is a plain string used everywhere the bridge names its connection: `/dev/ttyUSB0`, `tcp://host:4403`, `ble:ADDRESS`. It
is also the key of the retry back-off table (`Bridge.bad_until`), so a failing endpoint is tried once per back-off period, not in a loop.

Nothing here transmits anything; it only opens and watches the link."""
import socket
import os

import meshtastic.serial_interface
from serial.tools import list_ports

DEFAULT_TCP_PORT = 4403     # the meshtastic TCP API port

# USB vendor IDs of the serial chips Meshtastic boards use. Auto-detection only tries ports from these
# vendors (plus a real Meshtastic handshake), so it never pokes at headsets, dongles, modems, etc.
KNOWN_RADIO_VIDS = {
    0x10C4: "Silicon Labs CP210x",   # Heltec V3, T-Beam, many ESP32 boards
    0x1A86: "WCH CH340/CH9102",      # many clones and LilyGo boards
    0x303A: "Espressif native USB",  # ESP32-S3 boards
    0x239A: "Adafruit / RAK (nRF52)",
    0x0403: "FTDI",
    0x2886: "Seeed",
    0x1915: "Nordic",
    0x2E8A: "Raspberry Pi (RP2040)",
}


class BleUnavailable(Exception):
    """Bluetooth support cannot be loaded (the `bleak` package is missing or broken). The message is one friendly line."""


def open_failure_reason(port, error, windows=os.name == "nt"):
    """(seconds before retrying, message) for a serial port that could not be opened or did not answer like a radio.

    "Denied" means different things per OS. On Windows it is almost always another program holding the port. On Linux and macOS it is
    the device file's permissions (your user is not in the group that owns it), so say that and name the group, instead of blaming
    some other program."""
    text = str(error).lower()
    denied = isinstance(error, PermissionError) or "denied" in text
    if denied and not windows:
        group = ""
        try:
            import grp
            group = grp.getgrgid(os.stat(port).st_gid).gr_name
        except (ImportError, KeyError, OSError):
            pass
        hint = f" - the port belongs to the '{group}' group; add your user to it and log in again" if group else ""
        return 10, f"permission denied{hint} (python setup_env.py --check says exactly what to run)"
    if denied or "busy" in text or "exclusively lock" in text:
        return 10, "in use by another program"
    return 60, f"no Meshtastic radio answered ({str(error)[:70]})"  # a device that is not a radio, or one that did not handshake


def parse_tcp(text):
    """Split a `--tcp` value into (host, port). Accepts `host`, `host:4403`, `192.168.1.5`, `[fe80::1]:4403` and a bare IPv6 address.
    Raises ValueError with a readable message when it is not usable."""
    text = (text or "").strip()
    host, port = text, DEFAULT_TCP_PORT
    if text.startswith("["):                                 # bracketed IPv6, optionally with a port
        end = text.find("]")
        if end < 0:
            raise ValueError(f"unmatched '[' in {text!r}")
        host, rest = text[1:end], text[end + 1:]
        if rest:
            if not rest.startswith(":"):
                raise ValueError(f"unexpected text after ']' in {text!r}")
            port = rest[1:]
    elif text.count(":") == 1:                               # host:port (more than one colon = a bare IPv6 address, no port)
        host, port = text.split(":")
    if not host:
        raise ValueError("a host name or IP address is needed")
    try:
        port = int(port)
    except ValueError:
        raise ValueError(f"{port!r} is not a port number") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"port {port} is out of range (1-65535)")
    return host, port


def _ble_module():
    """Import and return the library's `meshtastic.ble_interface` module, or raise `BleUnavailable` with a one-line explanation.
    Imported here, not at start-up, so USB and Wi-Fi users never import bleak."""
    try:
        import meshtastic.ble_interface as module
    except Exception as e:      # ImportError normally, but a broken bleak/D-Bus set-up can fail in other ways
        raise BleUnavailable(f"Bluetooth is not available here ({type(e).__name__}: {str(e)[:80]}); "
                             "it needs the 'bleak' package (pip install -r requirements.txt) and, on Linux, BlueZ. "
                             "Use USB or --tcp instead.") from None
    return module


def load_ble():
    """The library's `BLEInterface` class, or raise `BleUnavailable` (see `_ble_module`)."""
    return _ble_module().BLEInterface


def safe_ble_scan(timeout=10):
    """Nearby Meshtastic Bluetooth devices as the library's BLEDevice objects, found with an UNFILTERED discovery.

    Why not the library's own `BLEInterface.scan()`: it asks BlueZ to filter by the Meshtastic service UUID, and on one real set-up
    (BlueZ 5.87, bleak 3.0.2) that filtered discovery made bluetoothd crash every time. A plain discovery works there, so we ask for
    everything and do the same filtering the library does afterwards: keep devices whose advertisement lists the Meshtastic service.
    Never pass `service_uuids` to a discover call here. Raises BleUnavailable if Bluetooth support cannot be loaded."""
    module = _ble_module()
    with module.BLEClient() as client:      # the library's wrapper: owns the event loop thread bleak needs, and closes it again
        response = client.discover(timeout=timeout, return_adv=True)
    wanted = module.SERVICE_UUID.lower()
    return [device for device, adv in response.values() if wanted in [u.lower() for u in (adv.service_uuids or [])]]


def _closing_on_error(cls):
    """A subclass of the library's BLEInterface that closes itself when its constructor fails for ANY reason.

    The library starts its receive thread first and only calls close() when the failure is its own BLEError. A bleak error or a timeout
    would leave that thread spinning and the caller without a reference to close it, and the bridge retries, so the leak would repeat
    every retry. (A BLE client created inside a failed connect() is still out of reach and is left to the library.)

    It also replaces the library's `find_device`, which scans with the service-UUID filter that crashed bluetoothd (see `safe_ble_scan`).
    The matching and the error messages are the library's own, so `BleEndpoint.failure_reason` keeps working."""
    class ClosingOnError(cls):
        """BLEInterface that cleans up after a failed constructor and finds its device with the unfiltered scan."""
        def find_device(self, address):
            """Same as the library's: the device whose name or address equals `address` (any device if None); exactly one must match."""
            devices = safe_ble_scan()
            if address:
                devices = [d for d in devices if address in (d.name, d.address)]
            if len(devices) == 0:
                raise cls.BLEError(
                    f"No Meshtastic BLE peripheral with identifier or address '{address}' found. Try --ble-scan to find it.",
                    cls.BLEError.DEVICE_NOT_FOUND)
            if len(devices) > 1:
                raise cls.BLEError(
                    f"More than one Meshtastic BLE peripheral with identifier or address '{address}' found.",
                    cls.BLEError.MULTIPLE_DEVICES)
            return devices[0]

        def __init__(self, *args, **kwargs):
            try:
                super().__init__(*args, **kwargs)
            except Exception:
                try:
                    self.close()
                except Exception:
                    pass
                raise
    return ClosingOnError


def scan_ble():
    """Scan for nearby Meshtastic Bluetooth devices (takes about 10 seconds). Returns a sorted list of (name, address).
    Raises BleUnavailable if Bluetooth cannot be used; other errors (adapter off, no BlueZ) propagate to the caller."""
    found = safe_ble_scan()
    return sorted(((getattr(d, "name", None) or "(no name)", d.address) for d in found), key=lambda t: (t[0].lower(), t[1]))


def check_args(parser, args):
    """Reject impossible connection flags with a clear argparse error: --tcp, --ble and a pinned --port are alternatives."""
    chosen = [flag for flag, given in (("--port", args.port.lower() != "auto"), ("--tcp", args.tcp is not None), ("--ble", args.ble is not None)) if given]
    if len(chosen) > 1:
        parser.error(f"{' and '.join(chosen)} cannot be used together: pick one way to reach the radio "
                     "(USB serial is the default and needs none of them)")
    if args.tcp is not None:      # "is not None": an empty value (say an unset shell variable) must be an error, not a silent USB fallback
        try:
            parse_tcp(args.tcp)
        except ValueError as e:
            parser.error(f"--tcp: {e}")
    if args.ble is not None and not args.ble.strip():
        parser.error("--ble: an address or device name is needed (--ble-scan lists them)")


def make_endpoint(args):
    """The Endpoint the command-line flags ask for. Tolerates an `args` without the newer flags (older tests and callers)."""
    if getattr(args, "demo", False):     # the simulated radio has no socket or Bluetooth client: --tcp/--ble are ignored there
        return SerialEndpoint()
    if getattr(args, "tcp", None) is not None:
        return TcpEndpoint(*parse_tcp(args.tcp))
    if getattr(args, "ble", None) is not None:
        return BleEndpoint(args.ble.strip())
    return SerialEndpoint(getattr(args, "port", "auto"), bool(getattr(args, "probe_unknown", False)))


class Endpoint:
    """One way of reaching the radio. Subclasses fill in the mode-specific parts; see the module docstring."""
    kind = ""

    def initial_label(self):
        """What to show as the connection before anything has connected."""
        return None

    def describe(self):
        """Phrase for the start-up line: 'Bridge starting: <this>.'"""
        return "using " + str(self.initial_label())

    def waiting_text(self):
        """What the bridge says (once) while it has nothing to try."""
        return str(self.initial_label())

    def search_hint(self):
        """Advice for the diagnostics page when no radio is connected."""
        return "The bridge keeps retrying by itself."

    def candidates(self, preferred):
        """Labels worth trying now, best first (`preferred` = the one used last). The bridge drops the ones in back-off."""
        raise NotImplementedError

    def open(self, label):
        """Open the library interface for `label`. Blocks while the handshake runs; raises on failure."""
        raise NotImplementedError

    def alive(self, iface):
        """True while the interface's own reader looks alive. Cheap and free of side effects (used by the dashboard status)."""
        raise NotImplementedError

    def link_problem(self, iface, label):
        """None if the link looks healthy, otherwise a short reason. Called every --scan-interval seconds on the connect thread."""
        return None if self.alive(iface) else "connection to the radio ended"

    def failure_reason(self, label, error):
        """(seconds before trying `label` again, message) for an open() that failed."""
        return 30, str(error)[:100]


class SerialEndpoint(Endpoint):
    """USB serial. With port 'auto' it finds radios by USB vendor and follows them across port renumbering; otherwise only that port."""
    kind = "usb"

    def __init__(self, port="auto", probe_unknown=False):
        self.port = port
        self.probe_unknown = probe_unknown

    @property
    def auto(self):
        """True when the port is not pinned."""
        return self.port.lower() == "auto"

    def initial_label(self):
        return self.port

    def describe(self):
        return "auto-detecting the radio" if self.auto else f"using {self.port}"

    def waiting_text(self):
        return "a Meshtastic radio" if self.auto else self.port

    def search_hint(self):
        return "Plug it in by USB, and close any other program that has its serial port open (only one can hold it)."

    @staticmethod
    def present():
        """Serial ports currently visible to the OS, as {device name: port info}."""
        return {p.device: p for p in list_ports.comports()}

    def candidates(self, preferred):
        present = self.present()
        if not self.auto:      # pinned by --port: only ever that one
            return [self.port] if self.port in present else []
        names = [d for d, p in present.items() if p.vid in KNOWN_RADIO_VIDS or (self.probe_unknown and p.vid is not None)]
        names.sort(key=lambda d: (d != preferred, d))   # prefer the port we were on before
        return names

    def open(self, label):
        # looked up on the module at call time so tests can replace SerialInterface
        return meshtastic.serial_interface.SerialInterface(devPath=label)

    def alive(self, iface):
        rx = getattr(iface, "_rxThread", None)
        return iface.stream is not None and (rx is None or rx.is_alive())

    def link_problem(self, iface, label):
        if not self.alive(iface):
            return "serial reader stopped"
        if label not in self.present():
            return f"{label} disappeared (unplugged?)"
        return None

    def failure_reason(self, label, error):
        return open_failure_reason(label, error)


class TcpEndpoint(Endpoint):
    """A radio on the LAN with Wi-Fi enabled, reached over the meshtastic TCP API.

    The library's TCPInterface already reconnects by itself when the radio closes the connection cleanly (it sleeps a second, reconnects and
    re-downloads the config). It does NOT recover when the reconnect fails (radio still rebooting) or when the connection is reset: the reader
    thread then ends. That, a vanished socket or a changed radio identity is what the bridge watches for, then it starts over with back-off."""
    kind = "tcp"
    OPEN_TIMEOUT = 60       # seconds to wait for the radio's config after connecting (the library's own default is 300)

    def __init__(self, host, port=DEFAULT_TCP_PORT):
        self.host, self.port = host, port
        self.label = f"tcp://[{host}]:{port}" if ":" in host else f"tcp://{host}:{port}"

    def initial_label(self):
        return self.label

    def waiting_text(self):
        return f"the radio at {self.label} (retrying)"

    def search_hint(self):
        return f"Check that the radio is powered, on the same network, and has Wi-Fi enabled ({self.label}). The bridge retries by itself."

    def candidates(self, preferred):
        return [self.label]

    def open(self, label):
        from meshtastic.tcp_interface import TCPInterface
        iface = TCPInterface(hostname=self.host, portNumber=self.port, timeout=self.OPEN_TIMEOUT)
        self._keepalive(iface)
        return iface

    @staticmethod
    def _keepalive(iface):
        """Ask the OS to probe an idle connection. Without this, a radio that drops off Wi-Fi without closing the socket would leave the
        library's reader blocked in recv() for hours and the bridge would believe it is connected. Best effort: not every OS has every option, and
        keepalive only helps while the connection is idle; if the radio vanishes with unsent data queued, the OS's retransmission timeout
        (about 15 minutes on Linux) decides instead."""
        sock = getattr(iface, "socket", None)
        if sock is None or getattr(iface, "_meshllm_keepalive_for", None) is sock:
            return
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for name, value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):   # Linux names; skipped elsewhere
                if hasattr(socket, name):
                    sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
        except (OSError, AttributeError):
            pass
        iface._meshllm_keepalive_for = sock     # the library swaps the socket when it reconnects itself, so remember which one was set up

    def alive(self, iface):
        rx = getattr(iface, "_rxThread", None)
        return getattr(iface, "socket", None) is not None and (rx is None or rx.is_alive())

    @staticmethod
    def reconnecting(iface):
        """True while the library is re-opening the connection itself. Its reconnect holds `reconnectLock` for the whole time and
        deliberately leaves `socket` as None (about a second), so that state is not a dead link and must not be torn down: doing so
        would open a second connection to the radio while the library's own one completes. If the reconnect fails the lock is released
        and the reader thread ends, which is then reported as a lost link."""
        lock = getattr(iface, "reconnectLock", None)
        return getattr(iface, "socket", None) is None and lock is not None and lock.locked()

    def link_problem(self, iface, label):
        if self.reconnecting(iface):
            return None
        if not self.alive(iface):
            return "connection to the radio ended"
        self._keepalive(iface)      # re-applies after the library reconnected on a new socket
        return None

    def failure_reason(self, label, error):
        text = str(error).lower()
        if isinstance(error, (ConnectionRefusedError, ConnectionResetError)):
            return 15, "connection refused - is Wi-Fi enabled on the radio, and is it port " + str(self.port) + "?"
        if isinstance(error, socket.gaierror):
            return 30, f"host name {self.host!r} not found"
        if isinstance(error, (TimeoutError, socket.timeout)) or "timed out" in text:
            return 30, "no answer (radio off, out of Wi-Fi range, or not a Meshtastic radio)"
        return 30, f"could not connect ({str(error)[:80]})"


class BleEndpoint(Endpoint):
    """Bluetooth LE, by address or device name. Host-only: it needs the computer's Bluetooth adapter (on Linux BlueZ over D-Bus), so it
    cannot work inside a container.

    The library does not reconnect a dropped BLE link: its disconnect callback closes the interface. The bridge notices (the client is gone) and
    opens a fresh one. Each open scans for about 10 seconds first, which is one more reason the retry back-off is 30 seconds."""
    kind = "ble"

    def __init__(self, target):
        self.target = target
        self.label = f"ble:{target}"

    def initial_label(self):
        return self.label

    def waiting_text(self):
        return f"the radio at {self.label} (retrying)"

    def search_hint(self):
        return ("Check that the radio is powered, in Bluetooth range and not already connected to a phone (a radio accepts one "
                "Bluetooth connection at a time). Run with --ble-scan to list nearby radios. The bridge retries by itself.")

    def candidates(self, preferred):
        return [self.label]

    def open(self, label):
        return _closing_on_error(load_ble())(self.target)

    def alive(self, iface):
        thread = getattr(iface, "_receiveThread", None)
        return (getattr(iface, "client", None) is not None and getattr(iface, "_want_receive", True)
                and (thread is None or thread.is_alive()))

    def failure_reason(self, label, error):
        if isinstance(error, BleUnavailable):
            return 600, str(error)
        if getattr(error, "kind", None) == "device_not_found":
            return 30, "radio not found nearby (Bluetooth on? in range? not connected to a phone? --ble-scan lists what is visible)"
        return 60, f"could not connect over Bluetooth ({str(error)[:90]})"
