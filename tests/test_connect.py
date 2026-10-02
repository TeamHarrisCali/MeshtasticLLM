"""Radio auto-discovery / reconnect tests with a fake serial layer (no hardware)."""
import argparse, os, sys, threading, time
from types import SimpleNamespace
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import tempfile; HERE = tempfile.mkdtemp(prefix="meshtest_")   # scratch databases and caches go in a temp folder, never in the project
DB = os.path.join(HERE, "conn_test.db")

from meshllm import bridge as b
from meshllm import connection as conn
import meshtastic.serial_interface
from serial.tools import list_ports

PORTS = []                      # what "Windows" currently lists: SimpleNamespace(device, vid)
FAIL = {}                       # port -> exception to raise when opening it
ATTEMPTS = {}                   # port -> how many times we tried to open it
IFACES = []                     # every fake interface created
NODE_IDS = {}                   # port -> node id the radio on that port reports

class FakeThread:
    def __init__(self): self.alive = True
    def is_alive(self): return self.alive

class FakeIface:
    def __init__(self, devPath=None, **kw):
        ATTEMPTS[devPath] = ATTEMPTS.get(devPath, 0) + 1
        if devPath in FAIL: raise FAIL[devPath]
        self.devPath, self.stream, self._rxThread, self.closed, self.sent = devPath, object(), FakeThread(), False, []
        self.nodes = {}
        self.myInfo = SimpleNamespace(my_node_num=1)
        IFACES.append(self)
    def getMyUser(self): return {"id": NODE_IDS.get(self.devPath, "!00000001"), "longName": f"Radio on {self.devPath}", "shortName": "R", "hwModel": "FAKE"}
    def close(self): self.closed = True
    def sendText(self, msg, destinationId, wantAck, onResponse):
        self.sent.append(msg)
        if onResponse: threading.Timer(0.02, lambda: onResponse({"fromId": destinationId, "decoded": {"routing": {"errorReason": "NONE"}}})).start()
    def kill_reader(self): self._rxThread.alive = False; self.stream = None

meshtastic.serial_interface.SerialInterface = FakeIface
list_ports.comports = lambda: list(PORTS)
def plug(dev, vid=0x10C4, node=None):
    PORTS.append(SimpleNamespace(device=dev, vid=vid))
    if node: NODE_IDS[dev] = node
def unplug(dev):
    PORTS[:] = [p for p in PORTS if p.device != dev]

def args(**over):
    base = dict(db=DB, port="auto", probe_unknown=False, scan_interval=0.05, reconnect_hold=1.0, send_retries=2,
                retry_delay=0.05, chunk_bytes=160, max_chunks=4, chunk_delay=0, access_mode=None, daily_cap=None,
                confirm_seconds=60, model="m", command="/ai", ollama_url="http://127.0.0.1:9", cooldown=0, memory_turns=6,
                memory_hours=24, max_queue=5, no_web=True)
    base.update(over); return argparse.Namespace(**base)

def start(**over):
    if os.path.exists(DB):
        try: os.remove(DB)
        except OSError: pass
    br = b.Bridge(args(**over))
    threading.Thread(target=br.connect_loop, daemon=True).start()
    threading.Thread(target=br.sender_loop, daemon=True).start()
    return br
def until(cond, t=3.0):
    end = time.time() + t
    while time.time() < end:
        if cond(): return True
        time.sleep(0.03)
    return cond()

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)

# ---- nothing plugged in, then the wrong kind of device -------------------------------------------------
br = start()
time.sleep(0.4)
st = br.status()
check("no radio: bridge keeps running and reports 'searching'", st["searching"] and not st["connected"] and br.iface is None, st)
plug("COM3", vid=None)                 # Bluetooth serial port
plug("COM7", vid=0x28DE)               # a VR headset's serial interface (the real case on this PC)
time.sleep(0.5)
check("Bluetooth ports and unrelated USB devices are never touched", ATTEMPTS == {} and br.iface is None, ATTEMPTS)

# ---- plug in a radio ---------------------------------------------------------------------------------------
plug("COM9", node="!aaaa0009")
check("radio appears: connects to it automatically", until(lambda: br.iface is not None and br.port == "COM9"), (br.port, ATTEMPTS))
st = br.status()
check("status shows connected radio, its port and name", st["connected"] and st["port"] == "COM9" and st["node"]["id"] == "!aaaa0009" and not st["searching"], st)
check("only the radio was opened, not the headset", set(ATTEMPTS) == {"COM9"}, ATTEMPTS)
first = br.iface

# ---- unplug ------------------------------------------------------------------------------------------------------
unplug("COM9")
check("unplugged: noticed and the old connection released", until(lambda: br.iface is None and first.closed), (br.iface, first.closed))
st = br.status()
check("while away: status says searching but still names the last radio", st["searching"] and not st["connected"] and st["node"]["id"] == "!aaaa0009" and st["port"] == "COM9", st)

# ---- replugged under a different COM number (and a different radio) ----------------------------------------------------
plug("COM12", node="!bbbb0012")
check("new port number: follows the radio to COM12", until(lambda: br.iface is not None and br.port == "COM12"), br.port)
check("a different radio is recognised as such", br.radio_id == "!bbbb0012" and br.status()["node"]["id"] == "!bbbb0012")

# ---- reader thread dies but the port is still listed ---------------------------------------------------------------------
before = len(IFACES)
br.iface.kill_reader()
check("dead serial reader on a still-listed port: reconnects", until(lambda: len(IFACES) == before + 1 and br.iface is not None and br.iface.stream is not None), (len(IFACES), before))
check("...to the same port", br.port == "COM12")

# ---- busy port and failed handshake ---------------------------------------------------------------------------------------
unplug("COM12"); until(lambda: br.iface is None)
plug("COM13", node="!cccc0013"); FAIL["COM13"] = PermissionError(13, "Access is denied")
time.sleep(0.6)
check("port in use by another program: tried once, then backs off (not hammered)", ATTEMPTS.get("COM13") == 1 and br.bad_until["COM13"] - time.time() > 5, (ATTEMPTS.get("COM13"), br.bad_until.get("COM13")))
# the wording is honest per OS: permission problems on Linux/macOS are not "another program"
w_, m_ = conn.open_failure_reason("COM13", PermissionError(13, "Access is denied"), windows=True)
check("Windows 'access denied' is reported as another program holding the port", (w_, m_) == (10, "in use by another program"), (w_, m_))
w_, m_ = conn.open_failure_reason("/dev/null", PermissionError(13, "Permission denied"), windows=False)
check("Linux/macOS 'permission denied' names the permission problem, not another program", w_ == 10 and m_.startswith("permission denied") and "another program" not in m_ and "group" in m_, m_)
check("a busy port on Linux is retried soon and called busy", conn.open_failure_reason("/dev/ttyUSB9", OSError("Could not exclusively lock port /dev/ttyUSB9"), windows=False) == (10, "in use by another program"))
w_, m_ = conn.open_failure_reason("COM14", RuntimeError("Timed out waiting for connection completion"), windows=False)
check("a device that never answers is retried slowly and described as not a radio", w_ == 60 and m_.startswith("no Meshtastic radio answered"), (w_, m_))
del FAIL["COM13"]; br.bad_until.clear()
check("once the other program lets go it connects", until(lambda: br.iface is not None and br.port == "COM13"))
unplug("COM13"); until(lambda: br.iface is None)
plug("COM14"); FAIL["COM14"] = RuntimeError("Timed out waiting for connection completion")
time.sleep(0.6)
check("device that never answers the handshake: backs off ~60 s", ATTEMPTS.get("COM14") == 1 and br.bad_until["COM14"] - time.time() > 30, (ATTEMPTS.get("COM14"), br.bad_until.get("COM14")))
unplug("COM14")

# ---- several radios: prefer the one we had ---------------------------------------------------------------------------------------
del FAIL["COM14"]
plug("COM20", node="!dddd0020"); until(lambda: br.port == "COM20"); unplug("COM20"); until(lambda: br.iface is None)
plug("COM19", node="!dddd0019"); plug("COM20", node="!dddd0020")
check("with two radios plugged in, reconnects to the one it used last", until(lambda: br.iface is not None) and br.port == "COM20", br.port)
unplug("COM19"); unplug("COM20"); until(lambda: br.iface is None)

# ---- vendor filter ------------------------------------------------------------------------------------------------------------------
plug("COM40", vid=0x1234)
time.sleep(0.4)
check("unknown USB vendors are skipped by default", "COM40" not in ATTEMPTS and br.iface is None, ATTEMPTS)
unplug("COM40")
br.stop(); time.sleep(0.3)                     # retire the first bridge so later sections are isolated
unplug("COM7"); unplug("COM3")                 # and take the unrelated devices out of the fake world
brp = start(probe_unknown=True); plug("COM41", vid=0x1234)
check("--probe-unknown will try them", until(lambda: "COM41" in ATTEMPTS), ATTEMPTS)
brp.stop(); unplug("COM41"); time.sleep(0.3)

# ---- pinned port -------------------------------------------------------------------------------------------
IFACES.clear()
brx = start(port="COM30")
plug("COM31", node="!eeee0031"); time.sleep(0.4)
check("--port COMx: ignores other radios", "COM31" not in ATTEMPTS and brx.iface is None, ATTEMPTS)
plug("COM30", node="!eeee0030")
check("--port COMx: connects when that port appears", until(lambda: brx.iface is not None and brx.port == "COM30"))
unplug("COM30"); unplug("COM31")
check("--port COMx: still follows unplug", until(lambda: brx.iface is None))
brx.stop(); time.sleep(0.3)

# ---- outgoing messages across an outage ------------------------------------------------------------------------------------------------------
plug("COM50", node="!ffff0050")
bo = start(); until(lambda: bo.iface is not None)
unplug("COM50"); until(lambda: bo.iface is None)
rid = bo.audit.new_request("!aaaa0001", None, "q", status="answered", response="hello there")
bo.queue_reply(rid, "!aaaa0001", "hello there")
time.sleep(0.3)
plug("COM51", node="!ffff0050")
r = None
def delivered():
    global r
    r = next(x for x in bo.audit.list(limit=20) if x["id"] == rid); return r["delivered"] == 1
check("a reply queued while the radio was away is sent once it is back", until(delivered, 3) and "hello there" in bo.iface.sent, (r, bo.iface and bo.iface.sent))
unplug("COM51"); until(lambda: bo.iface is None)
rid2 = bo.audit.new_request("!aaaa0001", None, "q", status="answered", response="too late")
bo.queue_reply(rid2, "!aaaa0001", "too late")
def failed():
    global r
    r = next(x for x in bo.audit.list(limit=20) if x["id"] == rid2); return r["failed"] == 1
check("if the radio stays away past --reconnect-hold the message is marked failed", until(failed, 3.5) and "offline" in (r["delivery_note"] or ""), r)

# ---- stale events -----------------------------------------------------------------------------------------------------------------------------------
try: bo.on_lost(first); ok = True
except Exception as e: ok = False
check("connection-lost events from an old interface are ignored", ok)

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
