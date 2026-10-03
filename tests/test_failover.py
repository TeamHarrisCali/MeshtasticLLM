"""Automatic failover between radio connections (--fallback): flag parsing, the chain's choosing / parking / failback rules with an injected
clock, status data, and the bridge switching between a USB radio and a Wi-Fi one without treating them as different radios.

Everything is faked: the USB port list, the serial and TCP interfaces. No serial port, network, Bluetooth adapter or radio is touched, and
no check depends on wall-clock time (the chain's clock is a fake that the test moves by hand). The bridge-level section polls with `until`
only to wait for the connect thread, never to measure a duration."""
import argparse, contextlib, io, os, sys, tempfile, threading, time
from types import SimpleNamespace
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
HERE = tempfile.mkdtemp(prefix="meshtest_")     # scratch databases go in a temp folder, never in the project
DB = os.path.join(HERE, "failover.db")

import meshtastic.serial_interface, meshtastic.tcp_interface
from serial.tools import list_ports
from meshllm import bridge as b
from meshllm import connection as conn
from meshllm.diagnostics import Diagnostics

PORTS = []                  # the USB ports the fake OS lists right now
def plug(dev): PORTS.append(SimpleNamespace(device=dev, vid=0x10C4))
def unplug(dev): PORTS[:] = [p for p in PORTS if p.device != dev]
conn.SerialEndpoint.present = staticmethod(lambda: {p.device: p for p in PORTS})
list_ports.comports = lambda: list(PORTS)      # belt and braces: nothing may see a real port

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"), file=sys.__stdout__)   # the real stdout: it is swapped for a buffer below
    if not cond: fails.append(name)
def until(cond, t=8.0):
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

class Clock:
    """The chain's injected clock."""
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def advance(self, s): self.t += s

# ---- command line ----------------------------------------------------------------------------------------------------
MAC = "AA:BB:CC:DD:EE:FF"
code, err, ns = run_cli([])
check("no --fallback: no fallbacks and a plain endpoint, not a chain", code is None and not ns.fallback and not isinstance(conn.make_endpoint(ns), conn.FailoverChain), (code, err))
code, err, ns = run_cli(["--fallback", "ble:" + MAC])
ep = conn.make_endpoint(ns) if ns else None
check("--fallback ble:MAC splits on the first colon only (the address keeps its colons)", code is None and ns.fallback == [("ble", MAC)], (code, err))
check("the default USB primary plus one fallback is a chain [usb, ble]", isinstance(ep, conn.FailoverChain) and [e.kind for e in ep.endpoints] == ["usb", "ble"] and ep.endpoints[1].target == MAC)
code, err, ns = run_cli(["--tcp", "radio.test", "--fallback", "ble:" + MAC, "--fallback", "usb:auto", "--fallback", "usb:/dev/ttyUSB1"])
ep = conn.make_endpoint(ns) if ns else None
check("the order is the primary, then the fallbacks as given", code is None and [e.kind for e in ep.endpoints] == ["tcp", "ble", "usb", "usb"] and ep.endpoints[2].auto and ep.endpoints[3].port == "/dev/ttyUSB1", (code, err))
code, err, ns = run_cli(["--fallback", "tcp:[fe80::1]:4403", "--fallback", "tcp:192.0.2.5", "--fallback", "usb:COM3"])
ep = conn.make_endpoint(ns) if ns else None
check("tcp values keep their own syntax (bracketed IPv6 with a port, bare host) and usb takes a COM port",
      code is None and (ep.endpoints[1].host, ep.endpoints[1].port) == ("fe80::1", 4403) and (ep.endpoints[2].host, ep.endpoints[2].port) == ("192.0.2.5", 4403) and ep.endpoints[3].port == "COM3", (code, err))
code, err, ns = run_cli(["--fallback", " BLE: " + MAC])
check("kind is case-insensitive and stray spaces are trimmed", code is None and ns.fallback == [("ble", MAC)], (code, err))
for label, argv, words in (
        ("a value with no KIND:", ["--fallback", MAC], "KIND:VALUE"),
        ("no colon at all", ["--fallback", "ble"], "KIND:VALUE"),
        ("an unknown kind", ["--fallback", "wifi:192.0.2.5"], "one of usb, tcp, ble"),
        ("an empty value", ["--fallback", "ble:"], "needs a value"),
        ("a bad tcp port", ["--fallback", "tcp:host:99999"], "out of range"),
        ("a duplicate of the default USB auto primary", ["--fallback", "usb:auto"], "repeats"),
        ("a duplicate of the --ble primary (case ignored)", ["--ble", MAC, "--fallback", "ble:" + MAC.lower()], "repeats"),
        ("a duplicate of the --tcp primary", ["--tcp", "radio.test:4403", "--fallback", "tcp:RADIO.test"], "repeats"),
        ("a duplicate of a pinned --port primary", ["--port", "/dev/ttyUSB0", "--fallback", "usb:/dev/ttyUSB0"], "repeats"),
        ("two identical fallbacks", ["--fallback", "ble:x", "--fallback", "ble:x"], "repeats")):
    code, err, ns = run_cli(argv)
    check(f"{label} is an argparse error that says what is wrong", code == 2 and words in err, (code, err[-160:]))
code, err, ns = run_cli(["--port", "/dev/ttyUSB0", "--fallback", "usb:auto", "--fallback", "usb:/dev/ttyUSB1"])
check("usb:auto after a pinned port, and a different pinned port, are not duplicates", code is None, (code, err))
code, err, ns = run_cli(["--demo", "--fallback", "ble:" + MAC])
check("--demo parses with --fallback but builds no chain (the simulated radio ignores it)", code is None and type(conn.make_endpoint(ns)) is conn.SerialEndpoint, (code, err))

# ---- the chain: choosing, parking, failback (fake endpoints, fake clock) ---------------------------------------------
class Link:
    """A stand-in library interface."""
    def __init__(self, label, node): self.label, self.node, self.up, self.closed, self.stream = label, node, True, False, object()
    def getMyUser(self): return {"id": self.node, "longName": "Test Radio", "shortName": "T", "hwModel": "FAKE"}
    def close(self): self.closed = True; EVENTS.append(("close", self.label))
EVENTS = []             # ("open" | "close", label) in the order they happened
NODE = ["!00000a01"]    # the id every fake radio reports (one radio behind every transport)

class Usb(conn.SerialEndpoint):
    def open(self, label): EVENTS.append(("open", label)); return Link(label, NODE[0])
class Wifi(conn.TcpEndpoint):
    fail = None
    def open(self, label):
        if self.fail: raise self.fail
        EVENTS.append(("open", label)); return Link(label, NODE[0])
    def alive(self, iface): return iface.up
class Bt(conn.BleEndpoint):
    fail = None
    def open(self, label):
        if self.fail: raise self.fail
        EVENTS.append(("open", label)); return Link(label, NODE[0])
    def alive(self, iface): return iface.up

def make_chain(entries=("usb", "ble"), clock=None):
    """(chain, clock, the endpoint objects) for the given kinds in priority order."""
    clock = clock or Clock()
    eps = [{"usb": lambda: Usb("auto"), "tcp": lambda: Wifi("radio.test"), "ble": lambda: Bt(MAC)}[k]() for k in entries]
    return conn.FailoverChain(eps, now=clock), clock, eps

def connect(chain):
    """One pass of the bridge's connect loop: pick the first candidate and open it. ('opened', label, iface) | ('failed', label, text) | ('none',)."""
    targets = chain.candidates(None)
    if not targets: return ("none",)
    try:
        return ("opened", targets[0], chain.open(targets[0]))
    except Exception as e:
        return ("failed", targets[0], chain.failure_reason(targets[0], e)[1])
def lose(chain, link):
    """What connect_loop does when the link is gone (it asks loss_backoff for each loss)."""
    link.up = False; link.stream = None
    return chain.loss_backoff(link.label)

PORTS.clear(); plug("/dev/ttyUSB7")
chain, clk, (usb, ble) = make_chain()
r = connect(chain)
check("primary present: the primary is used", r[:2] == ("opened", "/dev/ttyUSB7") and chain.active is usb and chain.alive(r[2]), r[:2])
check("the live link is checked by the endpoint that opened it (healthy -> no problem, nothing to fail back to)", chain.link_problem(r[2], r[1]) is None and not chain.switching)
unplug("/dev/ttyUSB7")
check("primary unplugged: the link check reports it, via the USB endpoint's own wording", "disappeared" in (chain.link_problem(r[2], r[1]) or ""))
lose(chain, r[2])
r = connect(chain)
check("primary lost: the first fallback is opened and becomes the active one", r[:2] == ("opened", f"ble:{MAC}") and chain.active is ble and chain.active_label == f"ble:{MAC}", r[:2])

# first fallback failing -> parked, second used
PORTS.clear()
chain, clk, (usb, wifi, ble) = make_chain(("usb", "tcp", "ble"))
wifi.fail = ConnectionRefusedError("refused")
r = connect(chain)
check("first fallback failing: the failure is reported with that endpoint's own wording", r[0] == "failed" and r[1] == "tcp://radio.test:4403" and "connection refused" in r[2], r)
check("the failed entry is parked, so the next pass moves on to the second fallback", connect(chain)[:2] == ("opened", f"ble:{MAC}") and chain.active is ble)
check("a parked entry is not offered while its back-off lasts", chain.candidates(None) == [f"ble:{MAC}"])
clk.advance(16)     # past the 15 s back-off for a refused connection
check("when its back-off ends the higher entry is offered again", chain.candidates(None) == ["tcp://radio.test:4403"], chain.candidates(None))
r = connect(chain)
check("a second failure in a row parks it for longer (15 s doubled), so it cannot keep dragging a working link down",
      r[0] == "failed" and chain._parked["tcp://radio.test:4403"] - clk.t == 30, chain._parked)
bt_wait = conn.FailoverChain([Usb("auto"), Bt(MAC)], now=clk)
bt_wait.endpoints[1].fail = TimeoutError("timed out")
connect(bt_wait); clk.advance(61); connect(bt_wait)
check("the LAST entry keeps the plain back-off (nothing below it to protect)", bt_wait._parked[f"ble:{MAC}"] - clk.t == 60, bt_wait._parked)

# failback with a stability window
PORTS.clear()
chain, clk, (usb, ble) = make_chain()
r = connect(chain); link = r[2]
check("running on Bluetooth, USB absent: no failback", r[1] == f"ble:{MAC}" and chain.link_problem(link, r[1]) is None)
plug("/dev/ttyUSB7")
check("USB reappears: not switched at once (the stability window has just started)", chain.link_problem(link, r[1]) is None and not chain.switching)
clk.advance(9)
check("still inside the 10 s window: no switch", chain.link_problem(link, r[1]) is None and not chain.switching)
clk.advance(1.5)
why = chain.link_problem(link, r[1])
check("after the window the live link is ended with the clear message", why == f"a preferred connection is available again: switching from ble:{MAC} to /dev/ttyUSB7" and chain.switching, why)
lose_wait = lose(chain, link)
r2 = connect(chain)
check("the normal connect path then opens USB, and the switching flag is cleared", r2[:2] == ("opened", "/dev/ttyUSB7") and chain.active is usb and not chain.switching and lose_wait == 0, r2[:2])
check("and with USB live there is nothing preferred left to switch to", chain.link_problem(r2[2], r2[1]) is None)

# flapping inside the window
PORTS.clear()
chain, clk, (usb, ble) = make_chain()
r = connect(chain)
plug("/dev/ttyUSB7"); chain.link_problem(r[2], r[1]); clk.advance(6)
unplug("/dev/ttyUSB7"); chain.link_problem(r[2], r[1]); clk.advance(1)       # gone for a moment: the window starts over
plug("/dev/ttyUSB7"); chain.link_problem(r[2], r[1]); clk.advance(9)
check("a flapping USB port does not switch (the window restarts each time it vanishes)", chain.link_problem(r[2], r[1]) is None and not chain.switching)
clk.advance(1.5)
check("... but once it has stayed for the whole window it does", (chain.link_problem(r[2], r[1]) or "").startswith("a preferred connection is available again"))

# a preferred USB whose open failed is parked, so it is not 'available'
PORTS.clear(); plug("/dev/ttyUSB7")
chain, clk, (usb, ble) = make_chain()
chain._park("/dev/ttyUSB7", 60)
r = connect(chain)
check("a parked USB port is skipped at connect time", r[:2] == ("opened", f"ble:{MAC}"), r[:2])
clk.advance(30); chain.link_problem(r[2], r[1]); clk.advance(30)
check("a parked USB port does not count as available for failback while it is parked", chain.link_problem(r[2], r[1]) is None and not chain.switching)
clk.advance(1)
chain.link_problem(r[2], r[1]); clk.advance(10.5)
check("... and starts counting once its back-off is over", (chain.link_problem(r[2], r[1]) or "").startswith("a preferred"))
# a Wi-Fi or Bluetooth entry above the live one is never probed (nothing to look at without opening it)
chain, clk, (wifi, ble) = make_chain(("tcp", "ble"))
wifi.fail = ConnectionRefusedError("x")
connect(chain); r = connect(chain); clk.advance(1000)
check("Wi-Fi above a live Bluetooth link is not probed or switched back to", chain.link_problem(r[2], r[1]) is None and not chain.switching)

# every endpoint unavailable
PORTS.clear()
chain, clk, (usb, wifi, ble) = make_chain(("usb", "tcp", "ble"))
wifi.fail, ble.fail = ConnectionRefusedError("x"), TimeoutError("timed out")
check("nothing opens at first: the failures are reported", connect(chain)[0] == "failed" and connect(chain)[0] == "failed")
check("then nothing is offered (everything unavailable or parked): the bridge keeps waiting", chain.candidates(None) == [] and connect(chain) == ("none",))
check("the waiting line names every connection being tried", all(w in chain.waiting_text() for w in ("Meshtastic radio", "tcp://radio.test:4403", f"ble:{MAC}")), chain.waiting_text())
check("the search hint covers USB, Wi-Fi and Bluetooth", all(w in chain.search_hint() for w in ("Plug it in", "Wi-Fi", "Bluetooth")))
clk.advance(61)
check("after the back-off the entries are tried again, highest first", chain.candidates(None) == ["tcp://radio.test:4403"])
wifi.fail = None
check("and a recovered one connects", connect(chain)[:2] == ("opened", "tcp://radio.test:4403"))
check("describe() lists the order", "falls back, in this order, to tcp://radio.test:4403, ble:" in chain.describe(), chain.describe())

# status data
PORTS.clear()
chain, clk, (usb, ble) = make_chain()
info = chain.connection_info(False)
check("status chain, nothing connected: USB unavailable, Bluetooth standby, no text",
      info["failover"] and [(e["kind"], e["state"]) for e in info["entries"]] == [("usb", "unavailable"), ("ble", "standby")] and info["text"] == "", info)
r = connect(chain); info = chain.connection_info(True)
check("status chain on the fallback: Bluetooth active with its label, and the 'via' text",
      [(e["kind"], e["state"]) for e in info["entries"]] == [("usb", "unavailable"), ("ble", "active")] and info["entries"][1]["label"] == f"ble:{MAC}"
      and info["text"] == "via Bluetooth (USB not connected)", info)
plug("/dev/ttyUSB7")
chain._probes.clear()      # the dashboard reuses a port-list answer for a second; the fake ports changed just now
check("status chain: USB shows as available while the fallback is live", chain.connection_info(True)["entries"][0]["state"] == "available")
chain._park("/dev/ttyUSB7", 20)
e0 = chain.connection_info(True)["entries"][0]
check("status chain: a parked entry says so and when it will be retried", e0["state"] == "parked" and e0["retry_in_s"] == 21, e0)
lose(chain, r[2]); plug("/dev/ttyUSB7"); chain._parked.clear()
r = connect(chain); chain._probes.clear(); info = chain.connection_info(True)
check("status chain on the primary: USB active and no 'via' text", info["entries"][0]["state"] == "active" and info["entries"][0]["label"] == "/dev/ttyUSB7" and info["text"] == "", info)
check("a plain endpoint reports no chain", conn.SerialEndpoint().connection_info(True) == {"failover": False, "entries": [], "text": ""})

# ---- review fixes: give-up, flap hysteresis, open timeout, dashboard probing, key normalisation ------------------------
U = "/dev/ttyUSB7"
class FailUsb(Usb):
    """A USB entry whose open always fails (the port is listed but the radio never answers)."""
    def open(self, label): EVENTS.append(("open", label)); raise OSError("no radio answered")

PORTS.clear(); plug(U)
chain, clk, _ = make_chain(("usb", "ble"))
chain.endpoints[0] = FailUsb("auto")
chain._parked[U] = clk.t + 1; r = connect(chain); clk.advance(2)     # BT is live; USB is listed but broken
check("setup: the broken USB port is skipped and Bluetooth is live", r[:2] == ("opened", f"ble:{MAC}"), r[:2])
def failed_switch(chain, clk, link, label):
    """One switch-back attempt that fails to open USB, then the chain re-opens the fallback. Returns (link, label, why-it-switched or None)."""
    chain.link_problem(link, label); clk.advance(chain.stable_for + 1)
    why = chain.link_problem(link, label)
    if not why: return link, label, None
    lose(chain, link); connect(chain)                    # USB fails to open here and is parked
    r = connect(chain)                                   # so the next pass re-opens the fallback
    clk.advance(400)                                     # past any park time
    return r[2], r[1], why
link, label = r[2], r[1]
attempts = 0
for _ in range(6):
    link, label, why = failed_switch(chain, clk, link, label)
    if not why: break
    attempts += 1
check(f"a USB port that listed but never opens is tried {conn.FAILBACK_GIVE_UP} times, then left alone (it does not keep dragging the working link down)",
      attempts == conn.FAILBACK_GIVE_UP, attempts)
unplug(U); chain.link_problem(link, label); plug(U)
chain.link_problem(link, label); clk.advance(chain.stable_for + 1)
check("...until it is unplugged and plugged in again, which gives it a fresh start", (chain.link_problem(link, label) or "").startswith("a preferred connection"))

# flap hysteresis: a preferred link that dies quickly doubles the next failback wait; one that lasted resets it
PORTS.clear(); plug(U)
chain, clk, _ = make_chain(("usb", "ble"))
r = connect(chain); clk.advance(5); unplug(U); lose(chain, r[2]); r = connect(chain)
plug(U); chain.link_problem(r[2], r[1]); clk.advance(chain.stable_for + 1)
check("after USB died within a minute of opening, the normal window is not enough any more", chain.link_problem(r[2], r[1]) is None and chain._flaps == 1, chain._flaps)
clk.advance(chain.stable_for)
why = chain.link_problem(r[2], r[1])
check("...it takes twice as long (20 s instead of 10)", (why or "").startswith("a preferred connection"), why)
lose(chain, r[2]); r = connect(chain); clk.advance(conn.FLAP_LIFETIME + 1); unplug(U); lose(chain, r[2])
check("a USB link that lasted a minute resets the hysteresis", chain._flaps == 0, chain._flaps)
for _ in range(30):         # many quick deaths cannot make the wait unbounded
    chain._flaps = min(chain._flaps + 1, 10)
check("the doubling is capped (flap count and seconds)", chain._flaps == 10 and min(chain.stable_for * 2 ** chain._flaps, conn.FLAP_CAP) == conn.FLAP_CAP)

# silence_limit with nothing live, and a link that vanishes while the dashboard reads the chain
chain, clk, _ = make_chain(("usb", "ble"))
check("silence_limit is 0 with nothing open", chain.silence_limit == 0)
class Racy(conn.FailoverChain):
    """`active` reads as the live endpoint the first time and None afterwards, like a link ending on another thread mid-call."""
    _reads = 0
    @property
    def active(self):
        Racy._reads += 1
        return self._a if Racy._reads == 1 else None
    @active.setter
    def active(self, v): self._a = v
PORTS.clear()
racy = Racy([Usb("auto"), Bt(MAC)], now=Clock()); racy.active = racy.endpoints[1]; Racy._reads = 0
try:
    lim = racy.silence_limit
    check("silence_limit reads `active` once (no AttributeError when the link ends mid-call)", lim == racy.endpoints[1].silence_limit and Racy._reads == 1, (lim, Racy._reads))
except Exception as e:
    check("silence_limit reads `active` once (no AttributeError when the link ends mid-call)", False, repr(e))

# dashboard probing: cached for a moment, and a failing port listing cannot break the status page
PORTS.clear(); plug(U)
chain, clk, (usb, ble) = make_chain(("usb", "ble"))
calls = []
real_candidates = usb.candidates
usb.candidates = lambda preferred: (calls.append(1), real_candidates(preferred))[1]
chain.connection_info(False); chain.connection_info(False); chain.connection_info(False)
check("several dashboard polls in a row list the ports once", len(calls) == 1, len(calls))
usb.candidates = lambda preferred: (_ for _ in ()).throw(OSError("port list failed"))
chain._probes.clear()
try:
    info = chain.connection_info(False)
    check("a failing port listing shows the entry as unavailable instead of an error", info["entries"][0]["state"] == "unavailable", info)
except Exception as e:
    check("a failing port listing shows the entry as unavailable instead of an error", False, repr(e))

# a USB open that gets no answer is cut short while a lower entry is live; the last/only entry keeps the library's default
seen = []
class SpySerial:
    def __init__(self, devPath=None, **kw): seen.append(kw)
real_serial = meshtastic.serial_interface.SerialInterface
meshtastic.serial_interface.SerialInterface = SpySerial
try:
    conn.SerialEndpoint("auto").open(U)
    chained = conn.FailoverChain([conn.SerialEndpoint("auto"), conn.BleEndpoint(MAC)]); chained.endpoints[0].open(U)
    only = conn.FailoverChain([conn.BleEndpoint(MAC), conn.SerialEndpoint("auto")]); only.endpoints[1].open(U)
finally:
    meshtastic.serial_interface.SerialInterface = real_serial
check("a plain USB open passes no timeout (library default); a chain's preferred USB gets the short one; the last entry does not",
      seen == [{}, {"timeout": conn.FAILBACK_OPEN_TIMEOUT}, {}], seen)

# duplicate detection survives the other spelling of an address or port
same = lambda a, b_: conn.endpoint_key(a) == conn.endpoint_key(b_)
check("a Bluetooth address with dashes or lower case is the same entry as the colon form", same(conn.BleEndpoint("aa-bb-cc-dd-ee-ff"), conn.BleEndpoint(MAC)))
check("COM ports compare case-insensitively, device paths do not", same(conn.SerialEndpoint("com3"), conn.SerialEndpoint("COM3")) and not same(conn.SerialEndpoint("/dev/ttyUSB1"), conn.SerialEndpoint("/dev/ttyusb1")))
check("a device NAME is still compared by name", same(conn.BleEndpoint("Radio_1234"), conn.BleEndpoint("radio_1234")) and not same(conn.BleEndpoint("Radio_1234"), conn.BleEndpoint(MAC)))

# ---- the bridge: USB radio and Wi-Fi radio are the same radio ---------------------------------------------------------
class OutBuf(io.StringIO):
    """sys.stdout while a bridge runs, so the log lines can be searched (the connect thread prints into it)."""
    lock = threading.Lock()
    def write(self, s):
        with self.lock: return super().write(s)
    def text(self):
        with self.lock: return self.getvalue()

class FakeSerial:
    node = "!00000a01"
    def __init__(self, devPath=None, **kw):
        self.devPath, self.stream, self._rxThread, self.closed = devPath, object(), None, False
        EVENTS.append(("open", devPath))
    def getMyUser(self): return {"id": FakeSerial.node, "longName": "Radio over USB", "shortName": "R", "hwModel": "FAKE"}
    close_gate = None       # an Event: while set up and not triggered, close() blocks (a hung close)
    def close(self):
        if FakeSerial.close_gate: FakeSerial.close_gate.wait(20)
        self.closed = True; EVENTS.append(("close", self.devPath))
meshtastic.serial_interface.SerialInterface = FakeSerial

class FakeThread:
    alive = True
    def is_alive(self): return self.alive
class FakeTcp:
    node = "!00000a01"
    def _handleFromRadio(self, b): pass
    def __init__(self, hostname, debugOut=None, noProto=False, connectNow=True, portNumber=4403, noNodes=False, timeout=300):
        self.hostname, self.socket, self._rxThread, self.closed = hostname, SimpleNamespace(setsockopt=lambda *a: None), FakeThread(), False
        self.reconnectLock, self.stream = threading.Lock(), None
        EVENTS.append(("open", f"tcp://{hostname}:{portNumber}"))
        self.label = f"tcp://{hostname}:{portNumber}"
    def getMyUser(self): return {"id": FakeTcp.node, "longName": "Radio over Wi-Fi", "shortName": "W", "hwModel": "FAKE"}
    close_gate = None
    def close(self):
        if FakeTcp.close_gate: FakeTcp.close_gate.wait(20)
        self.closed = True; EVENTS.append(("close", self.label))
meshtastic.tcp_interface.TCPInterface = FakeTcp

def bridge_args(**over):
    base = dict(db=DB, port="auto", tcp=None, ble=None, fallback=[("tcp", "radio.test")], probe_unknown=False, scan_interval=0.05, reconnect_hold=1.0,
                send_retries=2, retry_delay=0.05, chunk_bytes=160, max_chunks=4, chunk_delay=0, access_mode=None, daily_cap=None,
                confirm_seconds=60, model="m", command="/ai", ollama_url="http://127.0.0.1:9", cooldown=0, memory_turns=6,
                memory_hours=24, max_queue=5, no_web=True, web_host="127.0.0.1", web_port=8090)
    base.update(over); return argparse.Namespace(**base)

def run_bridge(node_usb="!00000a01", node_tcp="!00000a01", usb_present=False):
    """A bridge on [USB auto, tcp://radio.test:4403] with a fake chain clock, its connect loop running. Returns (bridge, clock, stdout buffer)."""
    for f in (DB, DB + "-wal", DB + "-shm"):
        if os.path.exists(f): os.remove(f)
    FakeSerial.node, FakeTcp.node = node_usb, node_tcp
    del EVENTS[:]; PORTS.clear()
    if usb_present: plug("/dev/ttyUSB7")      # before the loop starts, so there is no race with the first connect
    br = b.Bridge(bridge_args())
    clk = Clock()
    br.endpoint._now = clk
    threading.Thread(target=br.connect_loop, daemon=True).start()
    return br, clk

TCP = "tcp://radio.test:4403"
out = OutBuf(); real_stdout = sys.stdout; sys.stdout = out
try:
    br, clk = run_bridge()
    ok = until(lambda: br.port == TCP and br.status()["connected"])
    st = br.status()
    check("bridge: USB absent -> the Wi-Fi fallback connects", ok)
    check("status()['port'] stays a plain string: the active label", st["port"] == TCP and isinstance(st["port"], str), st["port"])
    check("status()['connection'] carries the chain and the 'via' text",
          st["connection"]["failover"] and st["connection"]["entries"][1]["state"] == "active" and st["connection"]["text"] == "via Wi-Fi (USB not connected)", st["connection"])
    sentinel = b.Pending("abc123", "set_channel", {}, 1, time.time() + 3600); br.pending["!0000aaaa"] = sentinel     # a confirmation waiting for its code while the radio switches
    plug("/dev/ttyUSB7")
    def tick():
        clk.advance(1); return br.port == "/dev/ttyUSB7" and br.status()["connected"]
    check("bridge: USB comes back and, after the stability window, the bridge switches to it", until(tick, 15), (br.port, out.text()[-300:]))
    ev = [e for e in EVENTS if e[1] in (TCP, "/dev/ttyUSB7")]
    check("only one transport is held at a time: the Wi-Fi link was closed BEFORE USB was opened", ev == [("open", TCP), ("close", TCP), ("open", "/dev/ttyUSB7")], ev)
    log = out.text()
    check("the switch is logged as a switch, not as a lost link",
          f"a preferred connection is available again: switching from {TCP} to /dev/ttyUSB7" in log and f"lost {TCP}" not in log, log[-400:])
    check("same radio behind both transports: no 'different radio' log line and no radio-change banner",
          "different radio" not in log and not br.mesh.radio_change_active(), log[-400:])
    check("the pending confirmation is untouched by the switch", br.pending.get("!0000aaaa") is sentinel)
    check("status on the primary: no 'via' text", br.status()["connection"]["text"] == "" and br.status()["connection"]["entries"][0]["state"] == "active")
    unplug("/dev/ttyUSB7")
    check("bridge: USB unplugged -> the Wi-Fi fallback takes over again", until(lambda: br.port == TCP and br.status()["connected"]), (br.port, out.text()[-300:]))
    check("... still with no 'different radio' line", "different radio" not in out.text() and not br.mesh.radio_change_active())
    br.stop()

    br, clk = run_bridge(node_tcp="!00000b02")
    check("control: with a different radio behind the fallback the bridge DOES say so (the test above is meaningful)",
          until(lambda: br.port == TCP and br.status()["connected"]) and (plug("/dev/ttyUSB7") or True) and until(lambda: (clk.advance(1), br.port == "/dev/ttyUSB7")[1], 15)
          and "this is a different radio than before" in out.text(), out.text()[-300:])
    br.stop()

    br, clk = run_bridge(usb_present=True)
    check("bridge: with USB present at start the primary is used and nothing is switched", until(lambda: br.port == "/dev/ttyUSB7" and br.status()["connected"]) and br.status()["connection"]["text"] == "")
    check("diagnostics gets one line about the chain",
          any(c["id"] == "failover" and "/dev/ttyUSB7 (active)" in c["detail"] and f"{TCP} (not checked until needed)" in c["detail"] for c in Diagnostics(br).report()["checks"]))
    br.stop()

    # the old link must have finished closing before the next one opens, on a plain loss as well as on a deliberate switch
    br, clk = run_bridge(usb_present=True)
    until(lambda: br.port == "/dev/ttyUSB7" and br.status()["connected"])
    FakeSerial.close_gate = threading.Event()
    unplug("/dev/ttyUSB7")
    until(lambda: br.iface is None)
    time.sleep(1.0)
    check("a lost USB link whose close is still running: the next connection is not opened yet", ("open", TCP) not in EVENTS, EVENTS)
    FakeSerial.close_gate.set()
    check("...and it is opened as soon as the close has finished", until(lambda: br.port == TCP and br.status()["connected"]), EVENTS)
    check("...with the close recorded first", [e for e in EVENTS if e[1] in (TCP, "/dev/ttyUSB7")][-2:] == [("close", "/dev/ttyUSB7"), ("open", TCP)], EVENTS)
    FakeSerial.close_gate = None
    br.stop()

    br, clk = run_bridge()
    until(lambda: br.port == TCP and br.status()["connected"])
    FakeTcp.close_gate = threading.Event()
    plug("/dev/ttyUSB7")
    until(lambda: (clk.advance(1), br.iface is None)[1], 15)
    time.sleep(1.0)
    check("a deliberate switch whose close is still running: USB is not opened yet", ("open", "/dev/ttyUSB7") not in EVENTS, EVENTS)
    FakeTcp.close_gate.set()
    check("...and it is opened once the Wi-Fi close has finished", until(lambda: br.port == "/dev/ttyUSB7" and br.status()["connected"], 15), EVENTS)
    FakeTcp.close_gate = None
    br.stop()
finally:
    sys.stdout = real_stdout
time.sleep(0.2)

print()
print(f"{'FAILED: ' + str(len(fails)) if fails else 'all passed'}")
sys.exit(1 if fails else 0)
