"""Traceroute tests: path building, positions, errors, guardrails, store, API (fake radio)."""
import argparse, json, os, sys, threading, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import tempfile; HERE = tempfile.mkdtemp(prefix="meshtest_")   # scratch databases and caches go in a temp folder, never in the project
DB = os.path.join(HERE, "trace_test.db")
if os.path.exists(DB): os.remove(DB)

from meshllm import bridge as b, webui, traceroute as TR
import requests as rq
from meshtastic.protobuf import mesh_pb2, portnums_pb2

US, R1, R2, TGT, NOPOS, ZERO = 1, 0xA1, 0xB2, 0xC3, 0xD4, 0xE5
NODES = {
    US: {"user": {"id": "!00000001", "longName": "Us"}, "position": {"latitude": 37.80, "longitude": -122.27}},
    R1: {"user": {"id": "!000000a1", "longName": "Relay One"}, "position": {"latitudeI": 378100000, "longitudeI": -1222600000}},
    R2: {"user": {"id": "!000000b2", "longName": "Ridge Repeater"}},
    TGT: {"user": {"id": "!000000c3", "longName": "House Base "}, "position": {"latitude": 37.83, "longitude": -122.22}},
    ZERO: {"user": {"id": "!000000e5", "longName": "Zero Pos"}, "position": {"latitude": 0.0, "longitude": 0.0, "latitudeI": 0, "longitudeI": 0}},
}
SENT, SCRIPT = [], {}
def reply(frm, route, snr_t, route_back=None, snr_b=None, hopstart=True, to=US):
    tr = {"route": route, "snrTowards": snr_t}
    if route_back is not None: tr.update(routeBack=route_back, snrBack=snr_b)
    # the real library attaches the protobuf object itself to the decoded dict, plus the raw payload bytes
    tr["raw"] = mesh_pb2.RouteDiscovery()
    p = {"from": frm, "to": to, "decoded": {"portnum": "TRACEROUTE_APP", "payload": b"\x0a\x00", "requestId": 7, "traceroute": tr}}
    if hopstart: p["hopStart"] = 3
    return p
def routing(reason): return {"from": TGT, "to": US, "decoded": {"portnum": "ROUTING_APP", "routing": {"errorReason": reason}}}

class Radio:
    stream = object(); _rxThread = threading.current_thread()
    def __init__(self):
        self.nodesByNum = NODES
        self.nodes = {n["user"]["id"]: n for n in NODES.values()}
        self.myInfo = type("M", (), {"my_node_num": US})()
    def getMyUser(self): return {"id": "!00000001"}
    def sendData(self, data, destinationId, portNum, wantResponse, onResponse, channelIndex, hopLimit):
        SENT.append({"data": data, "dest": destinationId, "port": portNum, "resp": wantResponse, "hop": hopLimit})
        actions = SCRIPT.get(destinationId, [("none",)])
        for i, a in enumerate(actions):
            if a[0] == "raise": raise a[1]
            if a[0] == "send": threading.Timer(0.03 + 0.03 * i, lambda p=a[1]: onResponse(p)).start()

def make(db=DB, **over):
    base = dict(db=db, ollama_url="http://127.0.0.1:9", model="m", access_mode=None, daily_cap=None, no_tool_gate=True,
                traceroute_timeout=0.3, traceroute_cooldown=1.0, telemetry_min_interval=60, web_host="127.0.0.1", web_port=8091,
                no_web=True, command="/ai", port="auto", memory_turns=6, memory_hours=24, memory_chars=3000, max_queue=5,
                max_chunks=4, cooldown=0)
    base.update(over)
    if os.path.exists(db):
        try: os.remove(db)
        except OSError: pass
    br = b.Bridge(argparse.Namespace(**base)); br.iface = Radio()
    return br
def until(cond, t=4.0):
    end = time.time() + t
    while time.time() < end:
        if cond(): return True
        time.sleep(0.03)
    return cond()

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)

# ---- reading a reply ---------------------------------------------------------------------------------------------------------
full = reply(TGT, [R1, R2], [24, -32, 48], [R2, R1], [20, 16, 40])
towards, back, raw = TR.parse_response(full, US, NODES)
check("path there: us -> relays -> target, in order", [h["id"] for h in towards] == ["!00000001", "!000000a1", "!000000b2", "!000000c3"], [h["id"] for h in towards])
check("signal strengths are converted from quarter-dB (start has none)", [h["snr"] for h in towards] == [None, 6.0, -8.0, 12.0], [h["snr"] for h in towards])
check("path back is read separately, target first", [h["id"] for h in back] == ["!000000c3", "!000000b2", "!000000a1", "!00000001"] and [h["snr"] for h in back] == [None, 5.0, 4.0, 10.0], back and [(h["id"], h["snr"]) for h in back])
check("names are trimmed and come from the radio's node list", towards[3]["name"] == "House Base" and towards[2]["name"] == "Ridge Repeater")
check("positions: decimal degrees, or latitudeI/longitudeI, used when present", (towards[0]["lat"], towards[0]["lon"]) == (37.80, -122.27) and abs(towards[1]["lat"] - 37.81) < 1e-9 and abs(towards[1]["lon"] + 122.26) < 1e-9)
check("a node with no position is kept in the path, just without coordinates", towards[2]["lat"] is None and towards[2]["lon"] is None)
check("a 0,0 'position' is treated as unknown", TR.node_entry(ZERO, None, NODES)["lat"] is None)
check("impossible coordinates are rejected", TR._position({"position": {"latitude": 123.0, "longitude": 10.0}}) == (None, None) and TR._position({"position": {"latitude": "x", "longitude": 1}}) == (None, None))

t2, b2, _ = TR.parse_response(reply(TGT, [], [40]), US, NODES)
check("direct route (no relays) gives just us and the target", [h["id"] for h in t2] == ["!00000001", "!000000c3"] and t2[1]["snr"] == 10.0 and b2 is None)
t3, b3, _ = TR.parse_response(reply(TGT, [R1], [-128, 8], [R1], [-128, -128]), US, NODES)
check("-128 means unknown signal, not -32 dB", [h["snr"] for h in t3] == [None, None, 2.0] and [h["snr"] for h in b3] == [None, None, None])
t4, b4, _ = TR.parse_response(reply(TGT, [R1, R2], [24, 8]), US, NODES)
check("a signal list of the wrong length is ignored instead of misaligning hops", [h["snr"] for h in t4] == [None, None, None, None])
t5, b5, _ = TR.parse_response(reply(TGT, [R1], [8, 8], [R1], [8, 8], hopstart=False), US, NODES)
check("no hop counter in the reply: the way back isn't trusted", b5 is None and t5[1]["id"] == "!000000a1")
t6, _, _ = TR.parse_response(reply(TGT, [0xFFFFFFFF, R1], [4, 8, 12]), US, NODES)
check("an unreported relay (0xFFFFFFFF) shows as 'Unknown node' with no position", t6[1]["name"] == "Unknown node" and t6[1]["id"] is None and t6[1]["lat"] is None and len(t6) == 4)
t7, _, _ = TR.parse_response(reply(TGT, [0x123456], [4, 8]), US, NODES)
check("a relay the radio has never heard of still appears, by id", t7[1]["id"] == "!00123456" and t7[1]["name"] is None)
rd = mesh_pb2.RouteDiscovery(); rd.route.extend([R1]); rd.snr_towards.extend([24, 48])
raw_pkt = {"from": TGT, "to": US, "decoded": {"portnum": "TRACEROUTE_APP", "payload": rd.SerializeToString()}}
t8, _, _ = TR.parse_response(raw_pkt, US, NODES)
check("a reply delivered as raw bytes is decoded too", [h["id"] for h in t8] == ["!00000001", "!000000a1", "!000000c3"] and t8[2]["snr"] == 12.0)
for bad, why in [({"from": TGT, "decoded": {"portnum": "TRACEROUTE_APP"}}, "no route"), ({"decoded": {"traceroute": {}}}, "no sender"), (routing("NO_ROUTE"), "error")]:
    try: TR.parse_response(bad, US, NODES); ok = False
    except TR.TracerouteError: ok = True
    if not ok: check(f"bad reply refused ({why})", False); break
else:
    check("replies with no route, no sender, or a routing error are refused with a clear error", True)

# ---- sending one ----------------------------------------------------------------------------------------------------------------
br = make(); tr = br.traceroute
SCRIPT["!000000c3"] = [("send", full)]
row = tr.request("!000000c3", 5)
check("a successful traceroute is stored with both paths and relay counts", row["status"] == "ok" and row["relays_towards"] == 2 and row["relays_back"] == 2 and len(row["path_towards"]) == 4 and len(row["path_back"]) == 4, row)
check("...with node name, hop limit, latency and timestamp", row["node_name"] == "House Base " and row["hop_limit"] == 5 and 0 < row["latency_ms"] < 300 and abs(row["ts"] - time.time()) < 5)
check("the decoded reply is kept (without the library's protobuf object)", row["raw"]["route"] == [R1, R2] and "raw" not in row["raw"], row["raw"])
check("the stored record is plain JSON all the way down", json.dumps(row) and json.dumps(tr.store.list(limit=5)), "")
s = SENT[-1]
check("the radio is asked properly: traceroute port, response wanted, hop limit, empty route request", s["dest"] == "!000000c3" and s["port"] == portnums_pb2.PortNum.TRACEROUTE_APP and s["resp"] and s["hop"] == 5 and isinstance(s["data"], mesh_pb2.RouteDiscovery) and not s["data"].route, s)
SCRIPT["!000000c3"] = [("send", routing("NONE")), ("send", full)]
row = tr.request("!000000c3")
check("a bare acknowledgement is ignored and the real answer still arrives (default hop limit 7)", row["status"] == "ok" and SENT[-1]["hop"] == 7, row)
SCRIPT["!000000c3"] = [("send", routing("NO_ROUTE"))]
row = tr.request("!000000c3")
check("the radio reporting an error -> error row with the reason", row["status"] == "error" and "NO_ROUTE" in row["detail"], row)
SCRIPT["!000000c3"] = [("none",)]
row = tr.request("!000000c3", 3)
check("no answer -> a timeout row that mentions the hop limit", row["status"] == "timeout" and "3 hops" in row["detail"], row)
SCRIPT["!000000c3"] = [("raise", OSError("serial write failed"))]
row = tr.request("!000000c3")
check("send failure is recorded", row["status"] == "error" and "serial write failed" in row["detail"], row)
saved, br.iface = br.iface, None
row = tr.request("!000000c3")
check("radio away: error row, nothing sent", row["status"] == "error" and "not connected" in row["detail"])
br.iface = saved
SCRIPT["!000000c3"] = [("send", {"from": TGT, "to": US, "decoded": {"portnum": "TRACEROUTE_APP", "traceroute": "garbage"}})]
check("a malformed reply can't crash the request", tr.request("!000000c3")["status"] == "error")

for node, hop in [("bob", 7), (None, 7), (5, 7), ("!000000c3", 0), ("!000000c3", 8), ("!000000c3", "7"), ("!000000c3", True), ("!000000c3", None), ("!000000c3", 3.5)]:
    try: tr.request(node, hop); ok = False
    except TR.TracerouteError: ok = True
    if not ok: check(f"rejected before sending: {node!r}/{hop!r}", False); break
else:
    check("bad node ids and hop limits (0, 8, text, bool, None, float) are rejected before anything is sent", True)

# ---- guardrails ----------------------------------------------------------------------------------------------------------------------
SCRIPT["!000000c3"] = [("send", full)]
check("the traceroute uses the bridge-wide radio lock", br.radio_request_lock is tr._busy)
rid = tr.start_manual("!000000c3", 4)
check("manual traceroute returns an id and finishes with the trace", until(lambda: tr.manual_state(rid)["status"] == "ok") and tr.manual_state(rid)["trace"]["relays_towards"] == 2)
try: tr.start_manual("!000000c3"); ok = False
except TR.TracerouteError as e: ok = "moments ago" in str(e)
check("tracing the same node again straight away is refused (every trace loads the mesh)", ok)
SCRIPT["!000000b2"] = [("send", reply(R2, [], [40]))]
until(lambda: not tr._busy.locked())
rid2 = tr.start_manual("!000000b2")
check("a different node is fine", until(lambda: tr.manual_state(rid2)["status"] == "ok"))
until(lambda: not tr._busy.locked())
with br.radio_request_lock:
    try: tr.start_manual("!000000a1"); ok = False
    except TR.TracerouteError as e: ok = "in progress" in str(e)
check("while one traceroute is on the air another is refused", ok)
br.iface, saved = None, br.iface
try: tr.start_manual("!000000a1"); ok = False
except TR.TracerouteError as e: ok = "radio" in str(e)
br.iface = saved
check("a manual traceroute with the radio away is refused up front", ok)
# the lock check and the lock grab are not atomic: if another request slips in between, the refused trace must not leave the node in its cooldown
class _SlippedIn:
    """Looks free to the up-front check, but is taken by the time the worker thread tries it."""
    def locked(self): return False
    def acquire(self, blocking=True): return False
    def release(self): pass
real_busy, tr._busy = tr._busy, _SlippedIn()
tr._last.pop("!000000a3", None)
rid3 = tr.start_manual("!000000a3")
until(lambda: tr.manual_state(rid3)["status"] == "error")
tr._busy = real_busy
check("a trace refused at the lock does not leave its node in the cooldown", "!000000a3" not in tr._last and tr.manual_state(rid3)["status"] == "error", (tr._last.get("!000000a3"), tr.manual_state(rid3)))
time.sleep(1.1)

# ---- store ----------------------------------------------------------------------------------------------------------------------------
rows = tr.store.list(limit=50)
check("history is newest first, with the paths as real lists", rows[0]["id"] > rows[-1]["id"] and isinstance(rows[0]["raw"], (dict, type(None))) and any(isinstance(r["path_towards"], list) for r in rows))
check("pagination by id", [r["id"] for r in tr.store.list(limit=2, before=rows[2]["id"])] == [rows[3]["id"], rows[4]["id"]])
check("get by id; unknown id is None", tr.store.get(rows[0]["id"])["id"] == rows[0]["id"] and tr.store.get(99999) is None)

# ---- web API ---------------------------------------------------------------------------------------------------------------------------
DB2 = os.path.join(HERE, "trace_test2.db")
if os.path.exists(DB2): os.remove(DB2)
br2 = make(db=DB2, traceroute_cooldown=0.0); webui.start(br2)
URL = "http://127.0.0.1:8091"; H = {"Content-Type": "application/json"}
post = lambda p, o, h=H: rq.post(URL + p, headers=h, data=json.dumps(o))
r = post("/api/traceroute/request", {"node": "!000000c3", "hop_limit": 5}); rid = r.json().get("id")
check("POST starts a traceroute", r.status_code == 200 and rid, r.text)
check("polling shows it finishing, with the trace", until(lambda: rq.get(URL + "/api/traceroute/request", params={"id": rid}).json()["status"] == "ok") and len(rq.get(URL + "/api/traceroute/request", params={"id": rid}).json()["trace"]["path_towards"]) == 4)
tid = rq.get(URL + "/api/traceroute/request", params={"id": rid}).json()["trace"]["id"]
check("default hop limit applies when none is given", (lambda: (until(lambda: not br2.traceroute._busy.locked()), post("/api/traceroute/request", {"node": "!000000c3"}).status_code == 200, until(lambda: not br2.traceroute._busy.locked()), SENT[-1]["hop"] == 7)[-2:])() == (True, True))
lst = rq.get(URL + "/api/traceroutes").json()
check("history list", len(lst) >= 1 and lst[0]["node_id"] == "!000000c3")
one = rq.get(URL + "/api/traceroute", params={"id": tid}).json()
check("a single stored traceroute can be fetched for the map", one["id"] == tid and one["path_towards"][0]["lat"] == 37.80 and one["path_back"][0]["id"] == "!000000c3", one.get("id"))
check("unknown ids are clean 404s", rq.get(URL + "/api/traceroute", params={"id": 9999}).status_code == 404 and rq.get(URL + "/api/traceroute/request", params={"id": "x"}).status_code == 404 and rq.get(URL + "/api/traceroute", params={"id": "abc"}).status_code == 404)
check("bad input -> 400", all(post("/api/traceroute/request", o).status_code == 400 for o in [{"node": "bob"}, {}, {"node": "!000000c3", "hop_limit": 99}, {"node": "!000000c3", "hop_limit": "5"}, {"node": 5}]))
check("cross-origin traceroute blocked", post("/api/traceroute/request", {"node": "!000000c3"}, {**H, "Origin": "http://evil.example"}).status_code == 403)
check("non-JSON blocked", rq.post(URL + "/api/traceroute/request", headers={"Content-Type": "text/plain"}, data="x").status_code == 415)

print("\n%d failure(s)" % len(fails))
for f in (DB, DB2):
    try: os.remove(f)
    except OSError: pass
sys.exit(1 if fails else 0)
