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

* `FailoverChain`: an ordered list of the above (`--fallback`). It behaves as ONE endpoint to the bridge: it offers the highest-priority
  entry that has something to try, hands link checks to whichever entry is live, parks entries that fail, and while a lower entry is live it
  watches for a higher one coming back (see its docstring). Only one transport to the radio is ever open at a time.

Nothing here transmits anything; it only opens and watches the link."""
import argparse
import socket
import logging
import os
import re
import sys
import threading
import time

import meshtastic.serial_interface
from serial.tools import list_ports

DEFAULT_TCP_PORT = 4403     # the meshtastic TCP API port
BLE_CONNECT_TIMEOUT = 90    # seconds for the whole Bluetooth connect (scan, connect, settings download); tests shorten it
BLE_SCAN_TIMEOUT = 25       # seconds for one Bluetooth scan (the library's own discovery runs 10)
BLE_CONFIG_TIMEOUT = 60     # seconds the library waits for the radio's settings (its own default is 300)
BLE_SILENCE_LIMIT = 900     # seconds without any message from a Bluetooth radio before the link is declared dead (3x the library's 300 s heartbeat)
TCP_SILENCE_LIMIT = 1800    # the same for Wi-Fi
SILENCE_BACKOFF = 60        # seconds to wait before reopening after repeated silence reconnects that brought no data
MAX_LIMIT_MULTIPLE = 4      # the silence limit may grow to this many times its base value while reconnects keep bringing no data
REQUIRED_LIBRARY = "meshtastic>=2.7.11,<2.8"
FAILBACK_STABLE = 10        # seconds a preferred (higher-priority) USB radio must stay listed before the chain switches back to it
PARK_CAP = 300              # seconds: the longest a failing, non-last entry of a failover chain is left alone
FAILBACK_OPEN_TIMEOUT = 45  # seconds a failover chain waits for a preferred USB radio's settings (the library's own default is 300)
FAILBACK_GIVE_UP = 3        # failed switch-backs to the same port after which the chain stops trying until the port is unplugged and replugged
FLAP_LIFETIME = 60          # a preferred link that ends sooner than this counts as a flap and doubles the next failback wait
FLAP_CAP = 300              # seconds: the longest failback wait that flapping can cause
PROBE_CACHE = 1.0           # seconds the dashboard reuses a port-list answer (many browsers poll the status at once)
clock = time.monotonic      # tests replace this with a fake clock
BLUETOOTH_SYSFS = "/sys/class/bluetooth"    # Linux lists the Bluetooth adapters (hci0, ...) here; tests point it elsewhere
MAC_RE = re.compile(r"[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}")     # used with fullmatch

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


class BleTimeout(Exception):
    """A Bluetooth step took longer than its time limit. The message says which step it was stuck in."""


class LinkUnavailable(Exception):
    """A connection mode cannot be used on this installation (missing package, incompatible library). The message is one friendly line."""


class TcpUnavailable(LinkUnavailable):
    """The installed meshtastic library lacks what the Wi-Fi mode needs. The message is one friendly line."""


class BleUnavailable(LinkUnavailable):
    """Bluetooth support cannot be loaded (the `bleak` package is missing or broken, or the meshtastic library is not the expected one)."""


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
        return 10, f"permission denied{hint} (python scripts/setup_env.py --check says exactly what to run)"
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


def _sets_attribute(cls, name):
    """True if `name` is assigned in the __init__ of `cls` or of one of its base classes (looked up in the compiled code, no instance needed)."""
    for klass in cls.__mro__:
        init = vars(klass).get("__init__")
        code = getattr(init, "__code__", None)
        if code is not None and name in code.co_names:
            return True
    return False


def library_problems(kind, interface_cls, client_cls=None):
    """What this bridge relies on in the installed meshtastic library but cannot find, as a list of names (empty = compatible).

    We reach into a few names the library does not document: the receive hook `_handleFromRadio`, the BLE connect steps, the `client`
    and `bleak_client` attributes, the reader thread and socket. If a future release renames one, a quiet failure (a frozen silence
    clock, a generic error) would be much worse than stopping with a clear message, so each open() checks first."""
    missing = [f"{interface_cls.__name__}.{n}" for n in ("_handleFromRadio",) if not callable(getattr(interface_cls, n, None))]
    if kind == "ble":
        missing += [f"BLEInterface.{n}" for n in ("find_device", "connect", "_startConfig", "close") if not callable(getattr(interface_cls, n, None))]
        missing += [f"BLEInterface.{n}" for n in ("client", "_want_receive", "_receiveThread") if not _sets_attribute(interface_cls, n)]
        if client_cls is None or not _sets_attribute(client_cls, "bleak_client"):
            missing.append("BLEClient.bleak_client")
        else:
            try:
                import inspect
                params = inspect.signature(client_cls.__init__).parameters
                if "address" not in params or not any(p.kind == p.VAR_KEYWORD for p in params.values()):
                    missing.append("BLEClient(address, **kwargs)")
            except (TypeError, ValueError):
                pass
    else:
        missing += [f"TCPInterface.{n}" for n in ("socket", "reconnectLock", "_rxThread") if not _sets_attribute(interface_cls, n)]
    return missing


def _incompatible(missing):
    """The one-line message for a library that lacks what we need."""
    return (f"the installed meshtastic library is not compatible with this version of the bridge: {', '.join(missing)} missing; "
            f"install {REQUIRED_LIBRARY}")


def load_ble():
    """The library's `BLEInterface` class, or raise `BleUnavailable` (see `_ble_module`, `library_problems`)."""
    module = _ble_module()
    missing = library_problems("ble", module.BLEInterface, getattr(module, "BLEClient", None))
    if missing:
        raise BleUnavailable(_incompatible(missing))
    return module.BLEInterface


def _call_with_timeout(fn, timeout, stuck_in, on_abandon=None, on_late_result=None):
    """Run `fn()` on a daemon helper thread and return its result, or raise BleTimeout after `timeout` seconds.

    The Bluetooth libraries have no timeouts of their own in several places (the connect, the discovery), and a hang there would block
    the bridge's reconnect loop silently. `stuck_in()` names the step that was running, for the message. After a timeout `on_abandon()`
    is called (to close what is half open), and if the helper ever finishes its result goes to `on_late_result` instead of being lost.
    The thread is a daemon, so a stuck call never holds up Ctrl+C or exit."""
    done, lock, box = threading.Event(), threading.Lock(), {}

    def work():
        try:
            result = fn()
        except BaseException as e:      # handed back to the caller's thread
            with lock:
                box["error"] = e
                done.set()
            return
        with lock:
            late = box.get("abandoned", False)
            if not late:
                box["result"] = result
            done.set()
        if late and on_late_result:
            on_late_result(result)

    threading.Thread(target=work, daemon=True, name="ble-helper").start()
    try:
        finished = done.wait(timeout)
    except BaseException:       # Ctrl+C while waiting: close what was half built before leaving
        with lock:
            box["abandoned"] = not done.is_set()
        if box["abandoned"] and on_abandon:
            on_abandon()
        raise
    if not finished:
        with lock:
            if not done.is_set():
                box["abandoned"] = True
        if box.get("abandoned"):
            if on_abandon:
                on_abandon()
            raise BleTimeout(f"Bluetooth connect timed out after {timeout:g} s while {stuck_in()}")
    if "error" in box:
        raise box["error"]
    return box["result"]


def safe_ble_scan(timeout=10):
    """Nearby Meshtastic Bluetooth devices as the library's BLEDevice objects, found with an UNFILTERED discovery.

    Why not the library's own `BLEInterface.scan()`: it asks BlueZ to filter by the Meshtastic service UUID, and on one real set-up
    (BlueZ 5.87, bleak 3.0.2) that filtered discovery made bluetoothd crash every time. A plain discovery works there, so we ask for
    everything and do the same filtering the library does afterwards: keep devices whose advertisement lists the Meshtastic service.
    Never pass `service_uuids` to a discover call here. Raises BleUnavailable if Bluetooth support cannot be loaded."""
    module = _ble_module()

    def discover():
        with module.BLEClient() as client:      # the library's wrapper: owns the event loop thread bleak needs, and closes it again
            return client.discover(timeout=timeout, return_adv=True)
    try:
        response = _call_with_timeout(discover, BLE_SCAN_TIMEOUT, lambda: "scanning")
    except BleTimeout as e:
        raise BleTimeout(str(e).replace("connect", "scan", 1)) from None
    wanted = module.SERVICE_UUID.lower()
    return [device for device, adv in response.values() if wanted in [u.lower() for u in (adv.service_uuids or [])]]


def stamp_rx(cls):
    """A subclass of a library interface class that remembers when the last message from the radio arrived (`_last_rx`, on `clock`).

    The link checks look at flags the library keeps, and those can stay true after the link is dead (a radio reboot with the OS
    reconnecting underneath, subscriptions gone). Data is the only proof that the link works, so the endpoints turn long silence into a
    lost link. `_handleFromRadio` is the library's single entry point for everything the radio sends."""
    class StampsReceive(cls):
        """Interface that records when it last heard from the radio."""
        def __init__(self, *args, **kwargs):
            self._last_rx = clock()         # set before the library starts its reader
            self._rx_count = self._rx_base = 0
            super().__init__(*args, **kwargs)

        def _handleFromRadio(self, fromRadioBytes):
            self._last_rx = clock()
            self._rx_count += 1
            return super()._handleFromRadio(fromRadioBytes)
    return StampsReceive


def _quiet(iface):
    """Close an interface, ignoring errors (it may be half built or already gone)."""
    try:
        iface.close()
    except Exception:
        pass


def _make_device(mac, details=None):
    """A minimal bleak BLEDevice for an address we did not scan for. `details` is bleak's OS-specific part (None = just the address)."""
    device_class = _ble_module().BLEDevice
    try:
        return device_class(mac, None, details)
    except TypeError:           # older bleak versions also wanted a signal strength
        return device_class(mac, None, details, 0)


def bluez_device_path(mac):
    """BlueZ's D-Bus object path for a device address on this computer's first Bluetooth adapter, e.g. /org/bluez/hci0/dev_AA_BB_...

    BlueZ keeps an object for every device it has paired with or connected to, advertising or not. Handing bleak that path lets it connect to a
    device it cannot see in a scan, which is the case when the OS already holds the connection (the radio then stops advertising)."""
    try:
        adapters = sorted(n for n in os.listdir(BLUETOOTH_SYSFS) if re.match(r"^hci\d+$", n))
    except OSError:
        adapters = []
    return f"/org/bluez/{adapters[0] if adapters else 'hci0'}/dev_{mac.replace('-', ':').upper().replace(':', '_')}"


_closing_lost = threading.local()       # set while WE close an interface whose link is already gone (see _quiet_library_close_error)


class _LostLinkCloseFilter(logging.Filter):
    """Drops the library's 'Error closing mesh interface' log line, but only on a thread that is closing a link that is already lost."""
    def filter(self, record):
        return not (getattr(_closing_lost, "on", False) and record.getMessage().startswith("Error closing mesh interface"))


def _quiet_library_close_error():
    """Make sure the library's BLE logger carries the filter above (added once). Closing a dead link makes the library log a misleading
    'are you in the bluetooth group? did you enter the pairing PIN?' error from its failed goodbye write; when the link is known to be lost
    that is expected, not a problem. Real errors in any other situation are still logged."""
    logger = logging.getLogger("meshtastic.ble_interface")
    if not any(isinstance(f, _LostLinkCloseFilter) for f in logger.filters):
        logger.addFilter(_LostLinkCloseFilter())


def _device_unknown(error):
    """True if bleak/BlueZ is saying it has no such device (as opposed to a real connect failure such as a refused pairing)."""
    text = str(error).lower()
    return "was not found" in text or "unknownobject" in text or "does not exist" in text


def _closing_on_error(cls, note=None, state=None):
    """A subclass of the library's BLEInterface that closes itself when its constructor fails for ANY reason.

    The library starts its receive thread first and only calls close() when the failure is its own BLEError. A bleak error or a timeout
    would leave that thread spinning and the caller without a reference to close it, and the bridge retries, so the leak would repeat
    every retry. (A BLE client created inside a failed connect() is still out of reach and is left to the library.)

    It also replaces the library's `find_device`, which scans with the service-UUID filter that crashed bluetoothd (see `safe_ble_scan`).
    The matching and the error messages are the library's own, so `BleEndpoint.failure_reason` keeps working.

    A MAC address is connected to directly, with no scan: a radio that the computer's Bluetooth already holds stops advertising, so a scan
    cannot see it, yet connecting by address works because the OS knows the device. `note(phase, text)` (optional) is told about each phase
    so it can be logged; `state` (optional) receives this object as state["iface"] so a timed-out connect can close it."""
    say = note or (lambda phase, text: None)
    class ClosingOnError(stamp_rx(cls)):
        """BLEInterface that cleans up after a failed constructor and finds its device with the unfiltered scan."""
        def __init__(self, *args, **kwargs):
            self._close_lock, self._close_started = threading.Lock(), False
            if state is not None:
                state["iface"] = self
            try:
                super().__init__(*args, **kwargs)
            except Exception:
                try:
                    self.close()
                except Exception:
                    pass
                raise

        def close(self):
            """The library's close, except that closing a link that is already lost says so plainly instead of logging a misleading error."""
            lock = getattr(self, "_close_lock", None)
            if lock is not None:        # several closers can race (disconnect callback, the bridge's detach, a timed-out connect): only the first acts
                with lock:
                    if self._close_started:
                        return
                    self._close_started = True
            client = getattr(self, "client", None)
            bleak = getattr(client, "bleak_client", None)
            lost = client is not None and bleak is not None and not getattr(bleak, "is_connected", True)
            if not lost:
                return super().close()
            _quiet_library_close_error()
            _closing_lost.on = True
            try:
                super().close()
            finally:
                _closing_lost.on = False
            if not getattr(self, "_lost_noted", False):
                self._lost_noted = True
                print("[radio] Bluetooth: closed the lost connection", flush=True)

        def _startConfig(self):
            """The library calls this once connected, right before it downloads the radio's settings."""
            say("config", "connected, waiting for the radio to send its settings")
            return super()._startConfig()

        def connect(self, address=None):
            """The library's connect (find the device, open a client, connect, discover services), with one change for a MAC address.

            The library hands bleak only the address string, and bleak then SCANS for it, which cannot see a radio the OS already holds
            (it stopped advertising). On Linux we give bleak a device that carries BlueZ's own object path for that address instead, so it
            connects to (or reuses) the existing system connection with no scan. If BlueZ turns out not to know the object we fall back once
            to the plain address, as the library would. Elsewhere there is no BlueZ and the address string is used."""
            module = _ble_module()
            device = self.find_device(address)
            attempts = []
            direct = getattr(device, "details", "scanned") is None      # details None = a MAC address we did not scan for
            if direct and sys.platform.startswith("linux"):
                path = bluez_device_path(device.address)
                attempts.append((f"connecting to {device.address} (using the system's existing device entry)",
                                _make_device(device.address, {"path": path, "props": {}})))
            if direct:
                attempts.append((f"connecting to {device.address}" + (" (scanning first)" if attempts else ""), device.address))
            else:
                attempts.append((None, device.address))         # found by our scan: its phase was already logged
            for i, (phase, target) in enumerate(attempts):
                if phase:
                    say("connect", phase)
                # The library's callback calls self.close() directly. bleak runs the callback ON the Bluetooth event-loop thread, and close()
                # talks to the radio through that same loop and waits for the answer, so it can wait forever on itself: the interface then
                # never closes and looks connected. Closing from a thread of its own avoids that.
                client = module.BLEClient(target, disconnected_callback=lambda _: threading.Thread(
                    target=_quiet, args=(self,), daemon=True, name="ble-close").start())
                connected = False
                try:
                    client.connect()
                    connected = True
                    client.discover()
                except Exception as e:
                    if connected:       # the link came up but the service discovery failed: let go of it, or BlueZ keeps the radio
                        try:
                            client.disconnect()
                        except Exception:
                            pass
                    _quiet(client)      # a failed attempt must not leave the client's event-loop thread behind
                    if i + 1 < len(attempts) and _device_unknown(e):
                        continue
                    raise
                return client

        def find_device(self, address):
            """Same as the library's: the device whose name or address equals `address` (any device if None); exactly one must match.
            A MAC address skips the scan entirely (see above)."""
            if address and MAC_RE.fullmatch(address):
                return _make_device(address.replace("-", ":").upper())
            say("scan", f"scanning for {address or 'any Meshtastic radio'}")
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
            say("connect", f"found {devices[0].address}, connecting")
            return devices[0]

    return ClosingOnError


def scan_ble():
    """Scan for nearby Meshtastic Bluetooth devices (takes about 10 seconds). Returns a sorted list of (name, address).
    Raises BleUnavailable if Bluetooth cannot be used; other errors (adapter off, no BlueZ) propagate to the caller."""
    found = safe_ble_scan()
    return sorted(((getattr(d, "name", None) or "(no name)", d.address) for d in found), key=lambda t: (t[0].lower(), t[1]))


FALLBACK_KINDS = ("usb", "tcp", "ble")
KIND_NAMES = {"usb": "USB", "tcp": "Wi-Fi", "ble": "Bluetooth"}     # what the dashboard calls each kind


def parse_fallback(text):
    """Split one `--fallback KIND:VALUE` into (kind, value). Only the FIRST colon separates them, because the value may contain colons
    (a Bluetooth address, a bracketed IPv6 address, host:port). Raises ValueError with a readable message when it is not usable."""
    kind, sep, value = (text or "").strip().partition(":")
    kind, value = kind.strip().lower(), value.strip()
    if not sep or kind not in FALLBACK_KINDS:
        raise ValueError(f"{text!r} is not KIND:VALUE with KIND one of {', '.join(FALLBACK_KINDS)} "
                         "(for example ble:AA:BB:CC:DD:EE:FF, tcp:192.168.1.50:4403 or usb:/dev/ttyUSB1)")
    if not value:
        raise ValueError(f"{kind}: needs a value (usb:auto or usb:PORT, tcp:HOST[:PORT], ble:ADDRESS_OR_NAME)")
    if kind == "tcp":
        parse_tcp(value)        # same checks as --tcp; its ValueError passes through
    return kind, value


def fallback_type(text):
    """argparse `type=` for --fallback: turns a bad value into argparse's own 'argument --fallback: ...' error."""
    try:
        return parse_fallback(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def endpoint_key(endpoint):
    """What makes two endpoints the same way of reaching a radio, for refusing duplicates: kind plus a normalised target."""
    if isinstance(endpoint, TcpEndpoint):
        return ("tcp", endpoint.host.lower(), endpoint.port)
    if isinstance(endpoint, BleEndpoint):
        target = endpoint.target.strip().lower().replace("-", ":")      # Windows and macOS write a Bluetooth address with dashes
        return ("ble", target if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", target) else endpoint.target.strip().lower())
    port = endpoint.port.strip()
    return ("usb", "auto" if endpoint.auto else port.lower() if port.upper().startswith("COM") else port)


def _fallback_endpoint(kind, value, silence=None):
    """The Endpoint for one parsed --fallback entry (`silence` as in make_endpoint)."""
    if kind == "tcp":
        return TcpEndpoint(*parse_tcp(value), silence_limit=TCP_SILENCE_LIMIT if silence is None else silence)
    if kind == "ble":
        return BleEndpoint(value, silence_limit=BLE_SILENCE_LIMIT if silence is None else silence)
    return SerialEndpoint(value)


def check_args(parser, args):
    """Reject impossible connection flags with a clear argparse error: --tcp, --ble and a pinned --port are alternatives,
    and a --fallback may not repeat another entry of the chain."""
    chosen = [flag for flag, given in (("--port", args.port.lower() != "auto"), ("--tcp", args.tcp is not None), ("--ble", args.ble is not None)) if given]
    if len(chosen) > 1:
        parser.error(f"{' and '.join(chosen)} cannot be used together: pick one way to reach the radio "
                     "(USB serial is the default and needs none of them)")
    if args.tcp is not None:      # "is not None": an empty value (say an unset shell variable) must be an error, not a silent USB fallback
        try:
            parse_tcp(args.tcp)
        except ValueError as e:
            parser.error(f"--tcp: {e}")
    if (getattr(args, "link_silence", None) or 0) < 0:
        parser.error("--link-silence: seconds must be 0 (off) or more")
    if args.ble is not None and not args.ble.strip():
        parser.error("--ble: an address or device name is needed (--ble-scan lists them)")
    if getattr(args, "fallback", None):
        seen = {endpoint_key(make_endpoint(args, with_fallbacks=False))}
        for kind, value in args.fallback:
            key = endpoint_key(_fallback_endpoint(kind, value))
            if key in seen:
                parser.error(f"--fallback {kind}:{value} repeats a connection that is already in the list (the primary or an earlier --fallback)")
            seen.add(key)


def make_endpoint(args, with_fallbacks=True):
    """The Endpoint the command-line flags ask for: the primary one, wrapped in a FailoverChain when --fallback entries were given.
    Tolerates an `args` without the newer flags (older tests and callers)."""
    if getattr(args, "demo", False):     # the simulated radio has no socket or Bluetooth client: --tcp/--ble/--fallback are ignored there
        return SerialEndpoint()
    silence = getattr(args, "link_silence", None)     # hidden --link-silence SECONDS: overrides the per-mode limit, 0 = off
    if getattr(args, "tcp", None) is not None:
        primary = TcpEndpoint(*parse_tcp(args.tcp), silence_limit=TCP_SILENCE_LIMIT if silence is None else silence)
    elif getattr(args, "ble", None) is not None:
        primary = BleEndpoint(args.ble.strip(), silence_limit=BLE_SILENCE_LIMIT if silence is None else silence)
    else:
        primary = SerialEndpoint(getattr(args, "port", "auto"), bool(getattr(args, "probe_unknown", False)))
    fallbacks = getattr(args, "fallback", None) or []
    if not (with_fallbacks and fallbacks):
        return primary
    return FailoverChain([primary] + [_fallback_endpoint(k, v, silence) for k, v in fallbacks])


class Endpoint:
    """One way of reaching the radio. Subclasses fill in the mode-specific parts; see the module docstring."""
    kind = ""
    silence_limit = 0       # seconds without data before the link counts as dead; 0 = no watchdog (USB serial has its own checks)
    _streak = 0             # silence reconnects in a row that brought no data
    _multiple = 1           # how many times silence_limit is currently allowed (grows while reconnects keep bringing no data)
    _backoff = 0            # seconds the bridge should wait before reopening after the last loss (see loss_backoff)
    probeable = False       # True when "is it there again?" can be answered without opening it (USB: the port list); used for failback
    switching = False       # True while the link is being ended on purpose to move to a preferred connection (FailoverChain only)

    def display(self):
        """A name for this endpoint in the connection chain on the dashboard."""
        return str(self.initial_label())

    def connection_info(self, connected):
        """Plain data about the configured connections for /api/status: {"failover", "entries", "text"}. A single endpoint has no chain."""
        return {"failover": False, "entries": [], "text": ""}

    def initial_label(self):
        """What to show as the connection before anything has connected."""
        return None

    @staticmethod
    def silence(iface):
        """Seconds since the radio last sent anything, or None when the interface does not record it (USB serial, the demo radio)."""
        last = getattr(iface, "_last_rx", None)
        return None if last is None else max(0.0, clock() - last)

    def silent_too_long(self, iface):
        """A reason if the radio has said nothing for longer than the silence limit, else None.

        Guards against a quiet mesh turning into an endless reconnect loop (each reconnect restarts the silence clock): data received on
        a connection resets everything; after a second silence loss in a row with no data in between, it warns, doubles the limit
        (up to MAX_LIMIT_MULTIPLE times) and asks the bridge to wait SILENCE_BACKOFF seconds before reopening."""
        if getattr(iface, "_rx_count", 0) > getattr(iface, "_rx_base", 0):      # something arrived since the link was ready
            self._streak, self._multiple, self._backoff = 0, 1, 0
        age = self.silence(iface)
        limit = self.silence_limit * self._multiple
        if not (self.silence_limit and age is not None and age > limit):
            return None
        if not getattr(iface, "_silence_counted", False):       # count each lost link once, however often it is asked
            iface._silence_counted = True
            self._streak += 1
            if self._streak >= 2:
                self._multiple = min(self._multiple * 2, MAX_LIMIT_MULTIPLE)
                self._backoff = SILENCE_BACKOFF
                print(f"[radio] no data from the radio again after reconnecting; the mesh may just be quiet. Waiting {SILENCE_BACKOFF} s and "
                      f"allowing {self.silence_limit * self._multiple:.0f} s of silence next time (--link-silence changes the limit, 0 turns it off)",
                      flush=True)
        return f"no data from the radio for {age:.0f} s"

    def loss_backoff(self, label):
        """Seconds the bridge should leave `label` alone after the loss it just handled (0 = reconnect at once). Read once per loss."""
        wait, self._backoff = self._backoff, 0
        return wait

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
    probeable = True        # the OS port list shows whether a radio is there, with no need to open anything

    def __init__(self, port="auto", probe_unknown=False):
        self.port = port
        self.probe_unknown = probe_unknown
        self.open_timeout = None    # seconds to wait for the radio's settings; None keeps the library's default (a failover chain sets it)

    @property
    def auto(self):
        """True when the port is not pinned."""
        return self.port.lower() == "auto"

    def initial_label(self):
        return self.port

    def display(self):
        return "USB (auto-detect)" if self.auto else self.port

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
        extra = {"timeout": self.open_timeout} if self.open_timeout else {}
        return meshtastic.serial_interface.SerialInterface(devPath=label, **extra)

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

    def __init__(self, host, port=DEFAULT_TCP_PORT, silence_limit=TCP_SILENCE_LIMIT):
        self.host, self.port, self.silence_limit = host, port, silence_limit
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
        missing = library_problems("tcp", TCPInterface)
        if missing:
            raise TcpUnavailable(_incompatible(missing))
        iface = stamp_rx(TCPInterface)(hostname=self.host, portNumber=self.port, timeout=self.OPEN_TIMEOUT)
        iface._last_rx = clock()        # the silence clock starts when the radio is ready
        iface._rx_base = getattr(iface, "_rx_count", 0)
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
        return self.silent_too_long(iface)

    def failure_reason(self, label, error):
        text = str(error).lower()
        if isinstance(error, LinkUnavailable):
            return 600, str(error)
        if isinstance(error, (ConnectionRefusedError, ConnectionResetError)):
            return 15, "connection refused - is Wi-Fi enabled on the radio, and is it port " + str(self.port) + "?"
        if isinstance(error, socket.gaierror):
            return 30, f"host name {self.host!r} not found"
        if isinstance(error, (TimeoutError, socket.timeout)) or "timed out" in text:
            return 30, "no answer (radio off, out of Wi-Fi range, or not a Meshtastic radio)"
        return 30, f"could not connect ({str(error)[:80]})"


def _ble_pairing_hint(error):
    """One actionable line for the host-side Bluetooth pairing failures seen in practice, with the raw error in parentheses; None otherwise.
    All three come down to the computer's stored pairing being stale, classic (BR/EDR) or unauthenticated."""
    text = str(error)
    low = text.lower()
    fix = "remove the pairing and re-pair as Low Energy with a PIN (docs/setup.md, Bluetooth)"
    if "br-connection-canceled" in low:
        why = f"Bluetooth refused the connection: the stored pairing is stale or recorded as classic (BR/EDR); {fix}"
    elif "le-connection-abort-by-local" in low:
        why = (f"the computer aborted the connection: the stored pairing is stale (a radio in 'No PIN' mode loses its bond when it "
               f"reboots); {fix}")
    elif "timed out waiting for" in low or "waiting for the radio to send its settings" in low:
        why = f"connected, but the radio never sent its settings: usually a stale or unauthenticated pairing; {fix}"
    else:
        return None
    return f"{why} ({text[:90]})"


class BleEndpoint(Endpoint):
    """Bluetooth LE, by address or device name. Host-only: it needs the computer's Bluetooth adapter (on Linux BlueZ over D-Bus), so it
    cannot work inside a container.

    The library does not reconnect a dropped BLE link: its disconnect callback closes the interface. The bridge notices (the client is gone) and
    opens a fresh one. A name is looked up with a scan of about 10 seconds; a MAC address is connected to directly. Every phase is logged and the whole
    connect has a time limit (BLE_CONNECT_TIMEOUT), because the Bluetooth stack can hang without an error."""
    kind = "ble"

    def __init__(self, target, silence_limit=BLE_SILENCE_LIMIT):
        self.target, self.silence_limit = target, silence_limit
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
        """Connect over Bluetooth, logging each phase and giving up after BLE_CONNECT_TIMEOUT seconds instead of hanging silently."""
        cls = load_ble()
        state = {"phase": "starting", "iface": None, "ready": False}

        def note(phase, text):
            if not state["ready"] and not state.get("abandoned"):   # not after a reboot's config re-run, nor from a connect we gave up on
                state["phase"] = text
                print(f"[radio] Bluetooth: {text}", flush=True)

        def abandon():
            """Timed out: close whatever the constructor had built so far (best effort, on its own thread in case close hangs)."""
            state["abandoned"] = True
            if state["iface"] is not None:
                threading.Thread(target=_quiet, args=(state["iface"],), daemon=True, name="ble-cleanup").start()

        iface = _call_with_timeout(lambda: _closing_on_error(cls, note, state)(self.target, timeout=BLE_CONFIG_TIMEOUT),
                                   BLE_CONNECT_TIMEOUT, lambda: state["phase"], on_abandon=abandon, on_late_result=_quiet)
        state["ready"] = True
        iface._last_rx = clock()        # the silence clock starts when the radio is ready
        iface._rx_base = getattr(iface, "_rx_count", 0)
        print("[radio] Bluetooth: radio ready", flush=True)
        return iface

    def alive(self, iface):
        thread = getattr(iface, "_receiveThread", None)
        client = getattr(iface, "client", None)
        return (client is not None and getattr(iface, "_want_receive", True) and (thread is None or thread.is_alive())
                and self._os_connected(client))

    @staticmethod
    def _os_connected(client):
        """The Bluetooth stack's own view: is the link up? (bleak reads BlueZ's Connected state.) The library's flags alone can stay true
        after the radio has gone, so this is checked too. Unknown counts as connected; the silence watchdog covers that case."""
        try:
            return bool(getattr(getattr(client, "bleak_client", None), "is_connected", True))
        except Exception:
            return True

    def link_problem(self, iface, label):
        if not self.alive(iface):
            return "Bluetooth link lost"
        return self.silent_too_long(iface)

    def failure_reason(self, label, error):
        if isinstance(error, LinkUnavailable):
            return 600, str(error)
        hint = _ble_pairing_hint(error)
        if hint:
            return 60, hint
        if isinstance(error, BleTimeout):
            return 60, str(error)
        if getattr(error, "kind", None) == "device_not_found":
            return 30, "radio not found nearby (Bluetooth on? in range? not connected to a phone? --ble-scan lists what is visible)"
        text = str(error)
        if "was not found" in text and "address" in text.lower():     # bleak: the OS does not know this address (never paired / not seen)
            return 30, ("that address is not paired or not known to Bluetooth yet; pair the radio once in the system Bluetooth settings, "
                        "or run --ble-scan to find it")
        return 60, f"could not connect over Bluetooth ({str(error)[:90]})"


class FailoverChain(Endpoint):
    """An ordered list of endpoints that the bridge treats as one (`--fallback`; the primary first, then each fallback in the order given).

    * Choosing: `candidates()` returns what the highest-priority entry that has something to try offers. USB offers its radio ports; Wi-Fi and
      Bluetooth always offer their one label. An entry that failed to open (or whose link was lost with a back-off) is *parked* for a while, so
      the chain moves on to the next entry instead of retrying a dead one. Park times are on this chain's own clock (`now`), not the bridge's.
    * Watching: link checks go to the entry that is live. When it reports a loss, the next connect attempt starts again from the top.
    * Failback: while a lower entry is live, a higher entry that can be checked without opening it (`probeable`: in practice USB, whose port
      list shows the radio again) must stay available for `stable_for` seconds. Then the live link is ended cleanly (`link_problem` says why)
      and the normal connect path opens the higher entry. Wi-Fi and Bluetooth entries are not probed (that would mean opening them), so they are only tried when the
      entries above them are unavailable, and the chain does not switch *back* to one of them while a lower entry works.
      A preferred link that keeps dying soon after it opens makes the chain wait longer each time (flap hysteresis), and a port that keeps
      failing to open is given up on until it is unplugged and replugged.
    * One transport at a time: the bridge waits (up to its SWITCH_CLOSE_WAIT) for the old link to finish closing before it opens the next one,
      because what the firmware does with USB and Bluetooth open together is not known. If a close hangs past that wait, the bridge moves on."""
    kind = "failover"

    def __init__(self, endpoints, stable_for=FAILBACK_STABLE, now=None):
        self.endpoints = list(endpoints)
        self.stable_for = stable_for
        self._now = now or (lambda: clock())    # looked up at call time so tests can replace `clock`
        for ep in self.endpoints[:-1]:
            if isinstance(ep, SerialEndpoint) and ep.open_timeout is None:
                ep.open_timeout = FAILBACK_OPEN_TIMEOUT     # a preferred USB radio that opens but never answers must not hold the working link for 300 s
        self.active = None          # the endpoint whose link is open, else None
        self.active_label = None
        self._owner = {}            # label -> endpoint that offered it (open and failure_reason need to find it again)
        self._parked = {}           # label -> chain-clock time before which it is left alone
        self._fails = {}            # label -> failed opens in a row (grows the park time of non-last entries)
        self._since = {}            # index of a preferred entry -> chain-clock time it was first seen available
        self._opened_at = None      # chain-clock time the live link was opened
        self._flaps = 0             # preferred links in a row that ended within FLAP_LIFETIME of opening (lengthens the failback wait)
        self._probes = {}           # id(endpoint) -> (time, labels): the dashboard's short-lived cache of port-list answers
        self.switching = False

    # ---- what the bridge asks of an endpoint ----------------------------------------------------------
    @property
    def silence_limit(self):
        """The live entry's silence limit (0 when nothing is open)."""
        ep = self.active        # read once: the watchdog thread and the connect thread both touch it
        return ep.silence_limit if ep else 0

    def initial_label(self):
        return self.endpoints[0].initial_label()

    def describe(self):
        rest = ", ".join(ep.display() for ep in self.endpoints[1:])
        return f"{self.endpoints[0].describe()}; if that is not available it falls back, in this order, to {rest}"

    def waiting_text(self):
        return " or ".join(ep.waiting_text() for ep in self.endpoints)

    def search_hint(self):
        return " ".join(dict.fromkeys(ep.search_hint() for ep in self.endpoints))

    def candidates(self, preferred):
        for ep in self.endpoints:
            labels = [t for t in ep.candidates(preferred) if not self._is_parked(t)]
            if labels:
                for t in labels:
                    self._owner[t] = ep
                return labels
        return []

    def open(self, label):
        ep = self._owner.get(label, self.endpoints[0])
        iface = ep.open(label)      # a failure propagates; the bridge then asks failure_reason, which parks the label
        self.active, self.active_label, self.switching = ep, label, False
        self._fails.pop(label, None)
        self._since.clear()
        self._opened_at = self._now()
        return iface

    def alive(self, iface):
        ep = self.active
        return bool(ep and ep.alive(iface))

    def link_problem(self, iface, label):
        ep = self.active
        if ep is None:
            ep = self._owner.get(label, self.endpoints[0])
        return ep.link_problem(iface, label) or self._better_available(ep, label)

    def loss_backoff(self, label):
        ep = self._owner.get(label, self.active or self.endpoints[0])
        wait = ep.loss_backoff(label)
        if wait:
            self._park(label, wait)
        if self._opened_at is not None and ep is not self.endpoints[-1] and not self.switching:
            # a preferred link that dies soon after it opened is a flap: wait longer before the next switch-back to it
            self._flaps = 0 if self._now() - self._opened_at >= FLAP_LIFETIME else min(self._flaps + 1, 10)
        self._opened_at = None
        self.active = self.active_label = None
        self._since.clear()
        return wait

    def failure_reason(self, label, error):
        ep = self._owner.get(label, self.endpoints[0])
        wait, why = ep.failure_reason(label, error)
        n = self._fails[label] = self._fails.get(label, 0) + 1
        if ep is not self.endpoints[-1]:        # a failing preferred entry backs off further each time, so it cannot keep dragging a
            wait = min(wait * 2 ** (n - 1), max(wait, PARK_CAP))    # working lower link down with failed switch attempts
        self._park(label, wait)
        return wait, why

    # ---- parking and failback -------------------------------------------------------------------------
    def _park(self, label, seconds):
        """Leave `label` alone for `seconds` of the chain's clock."""
        self._parked[label] = self._now() + seconds

    def _is_parked(self, label):
        """True while `label` is still in its back-off."""
        return self._parked.get(label, 0) > self._now()

    def _better_available(self, active, label):
        """A reason to end the live link when a higher-priority entry has stayed available long enough, else None."""
        now = self._now()
        for i, ep in enumerate(self.endpoints):
            if ep is active:
                break
            seen = list(ep.candidates(self.active_label)) if ep.probeable else []
            if not seen:        # it went away again: a flapping port starts its window over, and a port that is replugged gets a fresh start
                self._since.pop(i, None)
                for t in [t for t, owner in self._owner.items() if owner is ep]:
                    self._fails.pop(t, None)
                continue
            offer = [t for t in seen if not self._is_parked(t) and self._fails.get(t, 0) < FAILBACK_GIVE_UP]
            if not offer:
                self._since.pop(i, None)
            elif now - self._since.setdefault(i, now) >= min(self.stable_for * 2 ** self._flaps, max(self.stable_for, FLAP_CAP)):
                self.switching = True
                return f"a preferred connection is available again: switching from {label} to {offer[0]}"
        return None

    # ---- dashboard ------------------------------------------------------------------------------------
    def _probe(self, ep, preferred):
        """What a probeable entry offers, for the dashboard: the answer is reused for PROBE_CACHE seconds and a failing port listing counts as
        'nothing', so polling browsers neither hammer the OS nor break the status page. (The connect thread asks `candidates` directly.)"""
        now = time.monotonic()
        hit = self._probes.get(id(ep))
        if hit and now - hit[0] < PROBE_CACHE:
            return hit[1]
        try:
            labels = list(ep.candidates(preferred))
        except Exception:
            labels = []
        self._probes[id(ep)] = (now, labels)
        return labels

    def connection_info(self, connected):
        """The chain as plain data: one entry per endpoint with its kind, label and state (active, available, parked, unavailable or
        standby), plus a short text such as 'via Bluetooth (USB not connected)' when a lower entry is live."""
        entries, active_index = [], None
        active, active_label = self.active, self.active_label       # read once: another thread may end the link while this runs
        for i, ep in enumerate(list(self.endpoints)):
            entry = {"kind": ep.kind, "label": ep.display()}
            if connected and ep is active:
                active_index = i
                entry.update(label=active_label or ep.display(), state="active")
            else:
                labels = self._probe(ep, active_label) if ep.probeable else [ep.initial_label()]
                wait = max([self._parked.get(t, 0) - self._now() for t in labels] or [0])
                if not labels:
                    entry["state"] = "unavailable"
                elif wait > 0 and all(self._is_parked(t) for t in labels):
                    entry.update(state="parked", retry_in_s=int(wait) + 1)
                else:
                    entry["state"] = "available" if ep.probeable else "standby"
            entries.append(entry)
        text = ""
        if active_index:
            lower = list(dict.fromkeys(KIND_NAMES[e["kind"]] for e in entries[:active_index]))
            text = f"via {KIND_NAMES[entries[active_index]['kind']]} ({' and '.join(lower)} not connected)"
        return {"failover": True, "entries": entries, "text": text}
