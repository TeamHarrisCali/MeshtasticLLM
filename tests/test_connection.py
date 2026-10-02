"""Wi-Fi (TCP) and Bluetooth (BLE) connections: flags, connect, drop and automatic reconnect, back-off, a swapped radio, --ble-scan.

The library's TCPInterface and BLEInterface classes are replaced by fakes (the BLE module is a stand-in in sys.modules, so bleak is never
imported); no network, Bluetooth adapter, serial port or radio is touched. USB serial behaviour is covered by test_connect.py."""
import argparse, contextlib, io, os, subprocess, sys, tempfile, threading, time, types
from types import SimpleNamespace
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
HERE = tempfile.mkdtemp(prefix="meshtest_")     # scratch databases go in a temp folder, never in the project
DB = os.path.join(HERE, "conn_tcp_ble.db")

import meshtastic.tcp_interface
from serial.tools import list_ports
from meshllm import bridge as b
from meshllm import connection as conn

# the serial layer must stay untouched in TCP/BLE mode: any look at the serial ports is a failure
SERIAL_LOOKS = []
list_ports.comports = lambda: SERIAL_LOOKS.append(1) or []

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)
def until(cond, t=6.0):
    end = time.time() + t
    while time.time() < end:
        if cond(): return True
        time.sleep(0.03)
    return cond()
def run_cli(argv):
    """Parse like the real start-up; returns (exit code or None, stderr text, args or None)."""
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            _, ns = b.parse_cli(argv)
        return None, err.getvalue(), ns
    except SystemExit as e:
        return e.code, err.getvalue(), None

class FakeThread:
    def __init__(self): self.alive = True
    def is_alive(self): return self.alive

# ---- fakes ----------------------------------------------------------------------------------------------------------
class World:
    """What the fake network / Bluetooth currently does. Reset per section."""
    def __init__(self):
        self.fail = None            # exception the next opens raise
        self.fail_late = None       # BLE only: raise after the object (and its threads) exist
        self.block = None           # BLE only: an Event the constructor waits on after "connecting" (a hung connect)
        self.block_scan = None      # BLE only: an Event the discovery waits on (a hung scan)
        self.node = "!00000a01"     # id of the radio answering at the endpoint
        self.made = []              # every fake interface created
        self.attempts = 0
W = World()

class FakeSocket:
    def __init__(self): self.opts = []
    def setsockopt(self, *a): self.opts.append(a)

class FakeTcp:
    def __init__(self, hostname, debugOut=None, noProto=False, connectNow=True, portNumber=4403, noNodes=False, timeout=300):
        W.attempts += 1
        if W.fail: raise W.fail
        self.hostname, self.portNumber, self.timeout = hostname, portNumber, timeout
        self.socket, self._rxThread, self.stream, self.closed, self.node = FakeSocket(), FakeThread(), None, False, W.node
        self.reconnectLock, self.polls = threading.Lock(), 0
        W.made.append(self)
    def getMyUser(self): self.polls += 1; return {"id": self.node, "longName": "Radio over Wi-Fi", "shortName": "W", "hwModel": "FAKE"}   # the bridge calls this on every poll
    def close(self): self.closed = True
    def drop(self): self._rxThread.alive = False; self.socket = None    # the reconnect failed: socket cleared and the reader ended
    def kill_reader(self): self._rxThread.alive = False                  # a reset / timeout: the real library ends the reader but leaves `socket` set
    def lose_socket(self): self.socket = None                            # socket gone with nobody reconnecting
meshtastic.tcp_interface.TCPInterface = FakeTcp

class FakeBleError(Exception):
    def __init__(self, msg, kind="unknown"): super().__init__(msg); self.kind = kind

class FakeBle:
    SCAN = []
    def __init__(self, address=None, noProto=False, timeout=300, **kw):
        W.attempts += 1
        if W.fail: raise W.fail
        self.address, self.client, self._want_receive, self._receiveThread, self.closed, self.node = address, object(), True, FakeThread(), False, W.node
        W.made.append(self)
        if W.fail_late: raise W.fail_late       # fails after the receive thread exists, like a bleak error inside the library's constructor
        self.timeout = timeout
        self.device = self.find_device(address)  # the library's connect() does this first
        if W.block: W.block.wait()               # a connect that never returns
        self._startConfig()                      # ...then it downloads the radio's settings
    def _startConfig(self): pass
    @staticmethod
    def scan(): raise AssertionError("the library's filtered BLEInterface.scan() must never be called")
    class BLEError(Exception):
        DEVICE_NOT_FOUND, MULTIPLE_DEVICES = "device_not_found", "multiple_devices"
        def __init__(self, message, kind="unknown"): super().__init__(message); self.kind = kind
    def getMyUser(self): return {"id": self.node, "longName": "Radio over Bluetooth", "shortName": "B", "hwModel": "FAKE"}
    def close(self): self.closed = True
    def drop(self): self.client = None; self._want_receive = False; self._receiveThread.alive = False   # the library's disconnect callback closes the interface
FAKE_SERVICE = "6ba1b218-15a8-461f-9fa8-5dcae273eafd"
DISCOVER_CALLS = []          # kwargs of every discover() call the code made
class FakeBleClient:
    """Stands in for the library's BLEClient wrapper: an unfiltered discovery over FakeBle.SCAN (each device may carry .uuids)."""
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def discover(self, **kw):
        DISCOVER_CALLS.append(kw)
        if W.block_scan: W.block_scan.wait()
        return {d.address: (d, SimpleNamespace(service_uuids=getattr(d, "uuids", [FAKE_SERVICE.upper()]))) for d in FakeBle.SCAN}
FAKE_BLE_MODULE = types.ModuleType("meshtastic.ble_interface")
FAKE_BLE_MODULE.BLEInterface, FAKE_BLE_MODULE.BLEClient, FAKE_BLE_MODULE.SERVICE_UUID = FakeBle, FakeBleClient, FAKE_SERVICE
class FakeDevice:
    def __init__(self, address, name, details): self.address, self.name, self.details = address, name, details
FAKE_BLE_MODULE.BLEDevice = FakeDevice
sys.modules["meshtastic.ble_interface"] = FAKE_BLE_MODULE

def args(**over):
    base = dict(db=DB, port="auto", tcp=None, ble=None, probe_unknown=False, scan_interval=0.05, reconnect_hold=1.0, send_retries=2,
                retry_delay=0.05, chunk_bytes=160, max_chunks=4, chunk_delay=0, access_mode=None, daily_cap=None,
                confirm_seconds=60, model="m", command="/ai", ollama_url="http://127.0.0.1:9", cooldown=0, memory_turns=6,
                memory_hours=24, max_queue=5, no_web=True)
    base.update(over); return argparse.Namespace(**base)
BRIDGES = []
def start(**over):
    if os.path.exists(DB):
        try: os.remove(DB)
        except OSError: pass
    br = b.Bridge(args(**over))
    BRIDGES.append(br)
    threading.Thread(target=br.connect_loop, daemon=True).start()
    return br
def reset_world():
    for br in BRIDGES: br.stop()
    BRIDGES.clear(); time.sleep(0.3)
    W.fail, W.fail_late, W.block, W.block_scan, W.node, W.attempts = None, None, None, None, "!00000a01", 0
    W.made.clear()

# ---- command line ----------------------------------------------------------------------------------------------------
code, err, ns = run_cli([])
check("defaults: USB serial auto-detect, no TCP, no BLE", code is None and ns.port == "auto" and ns.tcp is None and ns.ble is None and not ns.ble_scan, (code, err))
check("the default endpoint is the serial one", isinstance(conn.make_endpoint(ns), conn.SerialEndpoint) and conn.make_endpoint(ns).auto)
code, err, ns = run_cli(["--port", "COM4"])
check("--port COMx still pins the serial port", code is None and isinstance(conn.make_endpoint(ns), conn.SerialEndpoint) and conn.make_endpoint(ns).port == "COM4", (code, err))
code, err, ns = run_cli(["--tcp", "radio.test"])
ep = conn.make_endpoint(ns) if ns else None
check("--tcp HOST uses the default port 4403 and labels it tcp://host:4403", code is None and isinstance(ep, conn.TcpEndpoint) and ep.initial_label() == "tcp://radio.test:4403", (code, err))
code, err, ns = run_cli(["--tcp", "192.0.2.7:5000"])
check("--tcp HOST:PORT takes the port", code is None and conn.make_endpoint(ns).initial_label() == "tcp://192.0.2.7:5000", (code, err))
code, err, ns = run_cli(["--ble", "AA:BB:CC:DD:EE:FF"])
check("--ble ADDRESS selects Bluetooth with the label ble:ADDRESS", code is None and isinstance(conn.make_endpoint(ns), conn.BleEndpoint) and conn.make_endpoint(ns).initial_label() == "ble:AA:BB:CC:DD:EE:FF", (code, err))
for flags, words in ((["--tcp", "h", "--ble", "x"], "--tcp and --ble"), (["--tcp", "h", "--port", "COM4"], "--port and --tcp"),
                     (["--ble", "x", "--port", "/dev/ttyUSB0"], "--port and --ble")):
    code, err, _ = run_cli(flags)
    check(f"{' '.join(flags)} is a clear argparse error", code == 2 and "cannot be used together" in err and words in err, (code, err))
code, err, _ = run_cli(["--tcp", "h", "--port", "auto"])
check("--port auto (the default spelled out) does not conflict with --tcp", code is None, (code, err))
for flags, words in ((["--tcp", ""], "--tcp:"), (["--ble", ""], "--ble:"), (["--ble", "  "], "--ble:")):
    code, err, _ = run_cli(flags)
    check(f"{flags} is an error, not a silent fall back to USB", code == 2 and words in err, (code, err))
for bad in ("host:0", "host:99999", "host:abc", ":4403", "[::1"):
    code, err, _ = run_cli(["--tcp", bad])
    check(f"--tcp {bad!r} is rejected with a message", code == 2 and "--tcp:" in err, (code, err))
check("parse_tcp: IPv6 forms", conn.parse_tcp("[fe80::1]:4500") == ("fe80::1", 4500) and conn.parse_tcp("fe80::1") == ("fe80::1", 4403) and conn.parse_tcp("[::1]") == ("::1", 4403))
check("an IPv6 target gets a bracketed label", conn.TcpEndpoint("fe80::1", 4403).initial_label() == "tcp://[fe80::1]:4403")
help_text = b.build_parser().format_help()
check("--help documents --tcp, --ble and --ble-scan", all(f in help_text for f in ("--tcp HOST[:PORT]", "--ble ADDRESS_OR_NAME", "--ble-scan")), help_text[:300])
r = subprocess.run([sys.executable, "-m", "meshllm", "--help"], cwd=ROOT, capture_output=True, text=True, timeout=120)
check("python -m meshllm --help works and lists the new flags", r.returncode == 0 and "--tcp" in r.stdout and "--ble-scan" in r.stdout, r.stderr[-300:])
r = subprocess.run([sys.executable, "-m", "meshllm", "--tcp", "h", "--ble", "x"], cwd=ROOT, capture_output=True, text=True, timeout=120)
check("python -m meshllm --tcp h --ble x exits with status 2 and the error", r.returncode == 2 and "cannot be used together" in r.stderr, (r.returncode, r.stderr[-300:]))

# ---- Wi-Fi (TCP) ----------------------------------------------------------------------------------------------------
br = start(tcp="radio.test:5555")
check("TCP: connects with the host and port it was given", until(lambda: br.iface is not None) and W.made[0].hostname == "radio.test" and W.made[0].portNumber == 5555,
      (W.attempts, [(m.hostname, m.portNumber) for m in W.made]))
st = br.status()
check("TCP: status shows the endpoint label as a plain string and is connected", st["connected"] and st["port"] == "tcp://radio.test:5555" and isinstance(st["port"], str) and st["node"]["id"] == "!00000a01", st)
check("TCP: the handshake wait is bounded (not the library's 5 minutes)", W.made[0].timeout == conn.TcpEndpoint.OPEN_TIMEOUT, W.made[0].timeout)
check("TCP: keepalive is switched on so a silent drop is noticed", any(o[:3] == (conn.socket.SOL_SOCKET, conn.socket.SO_KEEPALIVE, 1) for o in W.made[0].socket.opts), W.made[0].socket.opts)
check("TCP: the serial ports are never looked at", SERIAL_LOOKS == [], SERIAL_LOOKS)
first = br.iface
first.drop()
check("TCP: a dropped link is noticed, closed, and reconnected by itself", until(lambda: br.iface is not None and br.iface is not first and first.closed), (W.attempts, first.closed))
check("TCP: ...to the same endpoint and status is connected again", br.port == "tcp://radio.test:5555" and br.status()["connected"] and br.connects == 2, (br.port, br.connects))
check("TCP: a reconnect to the same radio is not reported as a different radio", not br.mesh.radio_change_active())

# the realistic dead links: the library ends its reader on a reset/timeout but leaves the socket set
a_ = br.iface; a_.kill_reader()
check("TCP: a dead reader thread with the socket still set is a lost link", until(lambda: br.iface is not None and br.iface is not a_ and a_.closed))
a_ = br.iface; a_.lose_socket()
check("TCP: a socket that is gone with no reconnect in progress is a lost link", until(lambda: br.iface is not None and br.iface is not a_ and a_.closed))
# the library's own clean-close reconnect: socket None for about a second while it holds reconnectLock, then it recovers
a_ = br.iface; made, polls = len(W.made), a_.polls
a_.reconnectLock.acquire(); a_.socket = None
reached = until(lambda: a_.polls >= polls + 5)       # the bridge polled several times while the library was mid-reconnect
check("TCP: the bridge polled during the library's own reconnect without giving up on it", reached and br.iface is a_ and not a_.closed and len(W.made) == made, (a_.polls - polls, a_.closed, len(W.made) - made))
a_.socket = FakeSocket(); a_.reconnectLock.release()
polls = a_.polls
until(lambda: a_.polls >= polls + 3)
check("TCP: ...and once the library has reconnected it is still the same, connected interface", br.iface is a_ and br.status()["connected"] and len(W.made) == made, (len(W.made) - made))
check("TCP: ...with keepalive re-applied to the library's new socket", any(o[:3] == (conn.socket.SOL_SOCKET, conn.socket.SO_KEEPALIVE, 1) for o in a_.socket.opts), a_.socket.opts)
# a failed library reconnect (lock released, reader ended) is detected
a_.reconnectLock.acquire(); a_.socket = None; time.sleep(0.2)
a_._rxThread.alive = False; a_.reconnectLock.release()
check("TCP: when the library's reconnect fails (lock released, reader ended) it is a lost link", until(lambda: br.iface is not None and br.iface is not a_ and a_.closed))

# the radio is power-cycled and refuses connections for a while: tried once, then backs off
second = br.iface
W.fail = ConnectionRefusedError(111, "Connection refused"); W.attempts = 0
second.drop()
check("TCP: radio down: noticed", until(lambda: br.iface is None))
time.sleep(0.6)       # many scan intervals; a hammering loop would have retried many times
check("TCP: a refusing endpoint is tried once, then parked (not hammered)", W.attempts == 1 and br.bad_until["tcp://radio.test:5555"] > time.time() + 5, (W.attempts, br.bad_until))
check("TCP: while parked the status says searching and keeps the label", br.status()["searching"] and br.status()["port"] == "tcp://radio.test:5555")
W.fail = None; br.bad_until.clear()
check("TCP: radio back: connects again", until(lambda: br.iface is not None and br.iface is not second))

# a different radio answers at the same address after a drop
W.node = "!00000b02"
br.iface.drop()
check("TCP: a different radio behind the same address is noticed after a drop", until(lambda: br.radio_id == "!00000b02" and br.iface is not None), br.radio_id)
check("TCP: ...and the radio-change banner is raised", br.mesh.radio_change_active())
# ... and one that swaps while the library kept the connection open (it reconnects TCP on its own)
live = br.iface
W.node = live.node = "!00000c03"     # the new radio keeps answering at this address
check("TCP: a different radio on a still-open connection is noticed too", until(lambda: br.radio_id == "!00000c03" and br.iface is not live and live.closed), (br.radio_id, br.iface is live))
# the library is still re-reading the config after its own reconnect: no identity yet, which is not a verdict
settling = br.iface
settling.getMyUser = lambda: None
time.sleep(0.4)
check("TCP: a radio that has no identity yet (config reloading) is left alone", br.iface is settling and br.status()["connected"])
reset_world()

# an unreachable host name backs off too, with its own message
W.fail = conn.socket.gaierror(-2, "Name or service not known")
br = start(tcp="nonexistent.test")
time.sleep(0.5)
check("TCP: an unknown host is tried once and parked", W.attempts == 1 and br.bad_until["tcp://nonexistent.test:4403"] > time.time() + 5, (W.attempts, br.bad_until))
check("TCP: failure wording per error", conn.TcpEndpoint("h").failure_reason("x", ConnectionRefusedError())[1].startswith("connection refused")
      and "not found" in conn.TcpEndpoint("h").failure_reason("x", conn.socket.gaierror())[1]
      and "no answer" in conn.TcpEndpoint("h").failure_reason("x", TimeoutError())[1])
reset_world()

# ---- Bluetooth (BLE) ------------------------------------------------------------------------------------------------
br = start(ble="AA:BB:CC:DD:EE:FF")
check("BLE: connects with the address it was given", until(lambda: br.iface is not None) and W.made[0].address == "AA:BB:CC:DD:EE:FF", W.attempts)
st = br.status()
check("BLE: status shows ble:ADDRESS and is connected", st["connected"] and st["port"] == "ble:AA:BB:CC:DD:EE:FF" and st["node"]["id"] == "!00000a01", st)
first = br.iface
first.drop()
check("BLE: a dropped link (the library closed its client) is noticed and reconnected by itself", until(lambda: br.iface is not None and br.iface is not first and first.closed), W.attempts)
check("BLE: ...connected again", br.status()["connected"] and br.connects == 2)
second = br.iface
second._receiveThread.alive = False      # the library's receive thread died but the client object is still there
check("BLE: a dead receive thread is a lost link too", until(lambda: br.iface is not None and br.iface is not second and second.closed))
# not found nearby: slow back-off, one attempt
third = br.iface
W.fail = FakeBleError("No Meshtastic BLE peripheral found", "device_not_found"); W.attempts = 0
third.drop()
until(lambda: br.iface is None); time.sleep(0.6)
check("BLE: radio not found: tried once, then parked for at least 30 s", W.attempts == 1 and br.bad_until["ble:AA:BB:CC:DD:EE:FF"] > time.time() + 25, (W.attempts, br.bad_until))
W.fail = None; W.node = "!00000d04"; br.bad_until.clear()
check("BLE: found again, and a different radio there is noticed", until(lambda: br.iface is not None and br.radio_id == "!00000d04") and br.mesh.radio_change_active(), br.radio_id)
check("BLE: the serial ports are never looked at", SERIAL_LOOKS == [], SERIAL_LOOKS)
W.fail_late = RuntimeError("bleak: device busy"); W.attempts = 0       # set first: the bridge reopens as soon as it sees the drop
br.iface.drop(); until(lambda: br.iface is None)
time.sleep(0.6)
check("BLE: a constructor that fails with a non-BLEError is closed (no leaked reader thread) and backed off", W.attempts == 1 and W.made[-1].closed and br.bad_until["ble:AA:BB:CC:DD:EE:FF"] > time.time() + 25, (W.attempts, W.made[-1].closed, br.bad_until))
check("BLE: ...and the bridge still got no interface from it", br.iface is None)
W.fail_late = None
check("BLE: failure wording", "not found" in conn.BleEndpoint("x").failure_reason("x", FakeBleError("m", "device_not_found"))[1]
      and conn.BleEndpoint("x").failure_reason("x", RuntimeError("boom"))[0] >= 30)
reset_world()

# ---- Bluetooth unavailable -------------------------------------------------------------------------------------------
sys.modules["meshtastic.ble_interface"] = None     # makes `import` raise ImportError, like a missing bleak
try:
    conn.load_ble(); msg = None
except conn.BleUnavailable as e:
    msg = str(e)
check("BLE unavailable: a one-line friendly error", msg is not None and "\n" not in msg and "bleak" in msg and "--tcp" in msg, msg)
br = start(ble="AA:BB:CC:DD:EE:FF")
time.sleep(0.5)
check("BLE unavailable: the bridge keeps running, tries once and backs off for a long while", br.iface is None and "ble:AA:BB:CC:DD:EE:FF" in br.bad_until and br.bad_until["ble:AA:BB:CC:DD:EE:FF"] > time.time() + 100, br.bad_until)
reset_world()
old_argv = sys.argv
err = io.StringIO()
sys.argv = ["meshllm", "--ble", "AA:BB"]
try:
    with contextlib.redirect_stderr(err): b.main(); code = None
except SystemExit as e:
    code = e.code
finally:
    sys.argv = old_argv
check("BLE unavailable: main() exits with status 2 and the message instead of starting", code == 2 and "Bluetooth is not available" in err.getvalue(), (code, err.getvalue()))
err, out = io.StringIO(), io.StringIO()
with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
    code = b.ble_scan_main()
check("BLE unavailable: --ble-scan reports it in one line and exits 1", code == 1 and err.getvalue().count("\n") == 1 and "Bluetooth is not available" in err.getvalue(), (code, err.getvalue()))
sys.modules["meshtastic.ble_interface"] = FAKE_BLE_MODULE

# ---- the safe scan (never the library's filtered one) ----------------------------------------------------------------------
FakeBle.SCAN = [SimpleNamespace(name="Meshtastic_aa22", address="AA:BB:CC:00:00:01"), SimpleNamespace(name="Meshtastic_zz11", address="11:22:33:44:55:66"),
                SimpleNamespace(name="Headphones", address="99:99:99:99:99:99", uuids=["0000110b-0000-1000-8000-00805f9b34fb"]),
                SimpleNamespace(name="Silent", address="98:98:98:98:98:98", uuids=None)]
del DISCOVER_CALLS[:]
found = conn.safe_ble_scan()
check("safe scan: discover is called without a service filter, with advertisement data", len(DISCOVER_CALLS) == 1 and "service_uuids" not in DISCOVER_CALLS[0] and DISCOVER_CALLS[0].get("return_adv") is True, DISCOVER_CALLS)
check("safe scan: devices that do not advertise the Meshtastic service are dropped (even with no advertised services)", sorted(d.address for d in found) == ["11:22:33:44:55:66", "AA:BB:CC:00:00:01"], [d.address for d in found])
Closing = conn._closing_on_error(FakeBle)
finder = Closing.__new__(Closing)        # find_device does not need a constructed object
del DISCOVER_CALLS[:]
dev = finder.find_device("aa-bb-cc-00-00-01")
check("find_device: a MAC address (any case, ':' or '-') is used directly with no scan", dev.address == "AA:BB:CC:00:00:01" and DISCOVER_CALLS == [], (dev.address, DISCOVER_CALLS))
check("find_device: a name that is not a MAC goes through the scan", finder.find_device("Meshtastic_aa22").address == "AA:BB:CC:00:00:01" and len(DISCOVER_CALLS) == 1)
check("find_device: matches by name", finder.find_device("Meshtastic_zz11").address == "11:22:33:44:55:66")
try: finder.find_device("nobody"); err_ = None
except FakeBle.BLEError as e: err_ = e
check("find_device: none found raises the library's BLEError text and kind", err_ is not None and err_.kind == "device_not_found" and "No Meshtastic BLE peripheral with identifier or address 'nobody' found" in str(err_) and "--ble-scan" in str(err_), err_)
check("...which the endpoint turns into the 'not found' back-off", conn.BleEndpoint("nobody").failure_reason("x", err_)[0] == 30)
try: finder.find_device(None); err_ = None
except FakeBle.BLEError as e: err_ = e
check("find_device: no address with several radios nearby is the library's 'more than one' error", err_ is not None and err_.kind == "multiple_devices" and "More than one Meshtastic BLE peripheral" in str(err_), err_)
FakeBle.SCAN = FakeBle.SCAN[:1]
check("find_device: no address with exactly one radio nearby picks it (as the library does)", finder.find_device(None).address == "AA:BB:CC:00:00:01")

# ---- direct MAC connect, phase logging, time limits ------------------------------------------------------------------------
FakeBle.SCAN = [SimpleNamespace(name="Meshtastic_aa22", address="AA:BB:CC:00:00:01")]
ep = conn.BleEndpoint("AA:BB:CC:00:00:01")
del DISCOVER_CALLS[:]
out = io.StringIO()
with contextlib.redirect_stdout(out):
    iface_ = ep.open(ep.label)
log = out.getvalue()
check("BLE MAC: the interface is created with that address and no scan runs at all", iface_.address == "AA:BB:CC:00:00:01" and DISCOVER_CALLS == [] and iface_.timeout == conn.BLE_CONFIG_TIMEOUT, (iface_.address, DISCOVER_CALLS))
pos = [log.find(x) for x in ("connecting to AA:BB:CC:00:00:01", "connected, waiting for the radio to send its settings", "radio ready")]
check("BLE: each phase is logged, in order", all(p >= 0 for p in pos) and pos == sorted(pos) and "scanning" not in log, log)
ep = conn.BleEndpoint("Meshtastic_aa22")
out = io.StringIO()
with contextlib.redirect_stdout(out):
    iface_ = ep.open(ep.label)
log = out.getvalue()
pos = [log.find(x) for x in ("scanning for Meshtastic_aa22", "found AA:BB:CC:00:00:01, connecting", "waiting for the radio to send its settings", "radio ready")]
check("BLE name: scanned (unfiltered) then connected, phases in order", len(DISCOVER_CALLS) == 1 and all(p >= 0 for p in pos) and pos == sorted(pos), (log, DISCOVER_CALLS))
# the OS does not know a MAC: bleak's own error becomes advice, with the usual back-off
err_ = RuntimeError("Device with address AA:BB:CC:00:00:99 was not found.")
wait_, msg_ = conn.BleEndpoint("AA:BB:CC:00:00:99").failure_reason("x", err_)
check("BLE MAC unknown to the OS: clear 'pair it' advice and the 30 s back-off", wait_ == 30 and "not paired" in msg_ and "--ble-scan" in msg_, (wait_, msg_))
W.fail = err_; W.attempts = 0
br = start(ble="AA:BB:CC:00:00:99")
time.sleep(0.5)
check("BLE MAC unknown: tried once and parked", W.attempts == 1 and br.bad_until["ble:AA:BB:CC:00:00:99"] > time.time() + 25, (W.attempts, br.bad_until))
reset_world()
# a hung connect: the endpoint gives up, names the phase, closes what it built, and the helper thread is a daemon
saved = conn.BLE_CONNECT_TIMEOUT
conn.BLE_CONNECT_TIMEOUT = 0.5
W.block = threading.Event()
ep = conn.BleEndpoint("AA:BB:CC:00:00:01")
out = io.StringIO()
try:
    with contextlib.redirect_stdout(out):
        ep.open(ep.label)
    stuck = None
except conn.BleTimeout as e:
    stuck = str(e)
check("BLE hung connect: raises a timeout that names the phase it was stuck in", stuck is not None and "timed out after 0.5 s while connecting to AA:BB:CC:00:00:01" in stuck, stuck)
check("...and the failure wording/back-off is the existing path", conn.BleEndpoint("x").failure_reason("x", conn.BleTimeout(stuck or "t"))[0] == 60)
helpers = [t_ for t_ in threading.enumerate() if t_.name == "ble-helper"]
check("...the helper thread is a daemon (it can never hold up Ctrl+C or exit)", helpers and all(t_.daemon for t_ in helpers), helpers)
hung = W.made[-1]
check("...the half-built interface is closed best-effort", until(lambda: hung.closed), hung.closed)
W.block.set()
check("...and once the connect finally returns, the helper thread ends (its late interface is closed too)", until(lambda: not [t_ for t_ in threading.enumerate() if t_.name == "ble-helper"]) and hung.closed)
# the same hang through a running bridge: the connect thread is released after the limit and backs off
W.block = threading.Event()
br = start(ble="AA:BB:CC:00:00:01")
check("BLE hung connect in a bridge: the connect loop is released and the endpoint is parked", until(lambda: br.bad_until.get("ble:AA:BB:CC:00:00:01", 0) > time.time() + 25) and br.iface is None, br.bad_until)
W.block.set()
reset_world()
conn.BLE_CONNECT_TIMEOUT = saved
# a hung discovery
saved = conn.BLE_SCAN_TIMEOUT
conn.BLE_SCAN_TIMEOUT = 0.4
W.block_scan = threading.Event()
try:
    conn.safe_ble_scan(); stuck = None
except conn.BleTimeout as e:
    stuck = str(e)
check("BLE hung scan: bounded by its own timeout", stuck is not None and "scan timed out after 0.4 s" in stuck, stuck)
out, err = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
    code = b.ble_scan_main()
check("--ble-scan with a hung discovery reports the timeout and exits 1", code == 1 and "timed out" in err.getvalue(), (code, err.getvalue()))
W.block_scan.set(); conn.BLE_SCAN_TIMEOUT = saved
W.block_scan = None

# ---- --ble-scan ----------------------------------------------------------------------------------------------------------
FakeBle.SCAN = [SimpleNamespace(name="Meshtastic_zz11", address="11:22:33:44:55:66"), SimpleNamespace(name="Meshtastic_aa22", address="AA:BB:CC:00:00:01")]
out = io.StringIO()
with contextlib.redirect_stdout(out):
    code = b.ble_scan_main()
text = out.getvalue()
check("--ble-scan prints each radio's name and address, sorted, and succeeds",
      code == 0 and "Meshtastic_aa22  AA:BB:CC:00:00:01" in text and "Meshtastic_zz11  11:22:33:44:55:66" in text and text.index("aa22") < text.index("zz11"), text)
FakeBle.SCAN = []
out = io.StringIO()
with contextlib.redirect_stdout(out):
    code = b.ble_scan_main()
check("--ble-scan with nothing nearby says so and still exits 0", code == 0 and "No Meshtastic Bluetooth radios found" in out.getvalue(), out.getvalue())
FakeBle.SCAN = [SimpleNamespace(name=None, address="DE:AD:BE:EF:00:01")]
sys.argv = ["meshllm", "--ble-scan"]
real_bridge = b.Bridge
b.Bridge = lambda *a, **k: (_ for _ in ()).throw(AssertionError("--ble-scan must not start a bridge"))
out = io.StringIO()
try:
    with contextlib.redirect_stdout(out): b.main(); code = None
except SystemExit as e:
    code = e.code
except AssertionError as e:
    code = str(e)
finally:
    b.Bridge = real_bridge; sys.argv = old_argv
check("--ble-scan through main() exits 0 without starting the bridge (an unnamed device shows a placeholder)", code == 0 and "(no name)  DE:AD:BE:EF:00:01" in out.getvalue(), (code, out.getvalue()))

# ---- demo mode is unaffected ---------------------------------------------------------------------------------------------
code, err, ns = run_cli(["--demo", "--tcp", "radio.test"])
check("--demo still parses (and demo mode ignores the connection flags with a note)", code is None and ns.demo and ns.tcp == "radio.test", (code, err))

check("bleak was never imported by these tests (everything was faked)", "bleak" not in sys.modules)
for br in BRIDGES: br.stop()
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
