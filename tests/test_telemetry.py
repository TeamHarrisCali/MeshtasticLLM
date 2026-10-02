"""Telemetry tests: recording what the radio hears, retention/pruning, watch list, web API (no hardware)."""
import argparse, csv, io, json, os, sys, threading, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import tempfile; HERE = tempfile.mkdtemp(prefix="meshtest_")   # scratch databases and caches go in a temp folder, never in the project
DB = os.path.join(HERE, "tel_test.db")
if os.path.exists(DB): os.remove(DB)

from meshllm import bridge as b, webui, telemetry as T
import requests as rq
from meshtastic.protobuf import telemetry_pb2

W, OTHER, FLOOD = "!10000002", "!dddd0004", "!eeee0005"

class Radio:
    stream = object(); _rxThread = threading.current_thread()
    def __init__(self):
        self.nodes = {W: {"user": {"id": W, "longName": "House Base "}, "lastHeard": 500, "deviceMetrics": {"batteryLevel": 64, "voltage": 3.88, "uptimeSeconds": 7200}},
                      OTHER: {"user": {"id": OTHER, "longName": "=SUM(1)+1"}, "lastHeard": 900, "environmentMetrics": {"temperature": 19.5, "relativeHumidity": 61.0}},
                      FLOOD: {"user": {"id": FLOOD, "longName": "Quiet node"}, "lastHeard": 950},
                      "!00000001": {"user": {"id": "!00000001", "longName": "Us"}, "lastHeard": 999, "deviceMetrics": {"batteryLevel": 100}}}
        self.myInfo = type("M", (), {"my_node_num": 1})()
    def getMyUser(self): return {"id": "!00000001", "longName": "Us"}

def make(db=DB, **over):
    base = dict(db=db, ollama_url="http://127.0.0.1:9", model="m", access_mode=None, daily_cap=None, no_tool_gate=True,
                telemetry_passive_gap=0.4, web_host="127.0.0.1", web_port=8093, no_web=True, command="/ai", port="auto",
                memory_turns=6, memory_hours=24, memory_chars=3000, max_queue=5, max_chunks=4, cooldown=0)
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

def bc(frm, key, metrics, pid, **over):
    """A telemetry broadcast packet, shaped like the real library's (protobuf object + raw payload attached)."""
    p = {"from": int(frm[1:], 16), "fromId": frm, "id": pid, "to": 0xFFFFFFFF, "rxSnr": 4.25, "rxRssi": -77, "hopStart": 3, "hopLimit": 1,
         "decoded": {"portnum": "TELEMETRY_APP", "payload": b"\x0a\x00", "telemetry": {"time": 1_700_000_500, key: metrics, "raw": telemetry_pb2.Telemetry()}}}
    p.update(over); return p
DEV = {"batteryLevel": 64, "voltage": 3.88, "channelUtilization": 1.5, "airUtilTx": 0.2, "uptimeSeconds": 7200}

# ---- parsing ---------------------------------------------------------------------------------------------------------------
kind, cols, raw, nt = T.parse_reply(bc(W, "deviceMetrics", DEV, 1)["decoded"]["telemetry"])
check("device telemetry parsed into columns", kind == "device" and cols == {"battery_level": 64, "voltage": 3.88, "channel_utilization": 1.5, "air_util_tx": 0.2, "uptime_seconds": 7200} and nt == 1_700_000_500, (kind, cols))
kind, cols, raw, nt = T.parse_reply({"environmentMetrics": {"temperature": 21.5, "relativeHumidity": 44.0, "barometricPressure": 1013.2, "lux": 120.0}})
check("environment parsed; unmapped values stay in raw", kind == "environment" and cols["temperature"] == 21.5 and "lux" not in cols and raw["lux"] == 120.0, (cols, raw))
check("radio stats recognised", T.parse_reply({"localStats": {"uptimeSeconds": 5, "numOnlineNodes": 3}})[0] == "local_stats")
check("a block with no telemetry -> None", T.parse_reply({"time": 5}) is None and T.parse_reply({}) is None)
check("non-numeric / boolean values never reach numeric columns", T.parse_reply({"deviceMetrics": {"batteryLevel": "high", "voltage": True}})[1] == {})

# ---- recording what the radio hears ----------------------------------------------------------------------------------------
br = make(); t = br.telemetry; cnt = lambda **kw: len(t.store.list(limit=500, **kw))
check("by default every node's broadcasts are recorded (no watch list needed)", t.passive_all() is True)
t.on_packet(bc(W, "deviceMetrics", DEV, 2), br.iface)
r = t.store.list()[0]
check("a heard broadcast is stored, tagged 'broadcast', with values, link quality, node clock and name",
      r["source"] == "broadcast" and r["status"] == "ok" and r["battery_level"] == 64 and r["voltage"] == 3.88 and r["uptime_seconds"] == 7200
      and r["rx_snr"] == 4.25 and r["rx_rssi"] == -77 and r["hops"] == 2 and r["node_time"] == 1_700_000_500 and r["node_name"] == "House Base " and abs(r["ts"] - time.time()) < 5, r)
check("the stored JSON is the plain metrics (no protobuf object from the library)", json.loads(r["raw"]) == DEV, r["raw"])
t.on_packet(bc(OTHER, "environmentMetrics", {"temperature": 19.5}, 3), br.iface)
check("another node that nobody put on a watch list is recorded too", cnt(node=OTHER) == 1)
t.on_packet(bc(W, "deviceMetrics", DEV, 2), br.iface)
check("the same packet heard twice (relayed) is stored once", cnt(node=W) == 1)
t.on_packet(bc(W, "deviceMetrics", DEV, 4), br.iface)
check("a second broadcast inside the minimum gap is dropped (a chatty node can't flood the database)", cnt(node=W) == 1)
t.on_packet(bc(W, "environmentMetrics", {"temperature": 20.0}, 5), br.iface)
check("a different kind from the same node isn't held back by that gap", cnt(node=W) == 2)
time.sleep(0.45)
t.on_packet(bc(W, "deviceMetrics", {**DEV, "batteryLevel": 63}, 6), br.iface)
check("after the gap the next broadcast is recorded", cnt(node=W, kind="device") == 2 and t.store.list(node=W, kind="device")[0]["battery_level"] == 63)

before = cnt(); time.sleep(0.45)
ignored = [
    ("a reply to somebody's request", bc(W, "deviceMetrics", DEV, 10, decoded={"portnum": "TELEMETRY_APP", "requestId": 123, "telemetry": {"deviceMetrics": DEV}})),
    ("our own radio's packet", bc("!00000001", "deviceMetrics", DEV, 11)),
    ("a packet with no telemetry block", bc(W, "deviceMetrics", DEV, 12, decoded={"portnum": "TELEMETRY_APP"})),
    ("a telemetry kind we don't store (health metrics)", bc(W, "healthMetrics", {"heartBpm": 70}, 13)),
    ("a packet with no sender", {"decoded": {"telemetry": {"deviceMetrics": DEV}}, "id": 14}),
]
for label, pkt in ignored:
    t.on_packet(pkt, br.iface)
check("ignored: " + "; ".join(l for l, _ in ignored), cnt() == before, cnt() - before)
try: t.on_packet("garbage", br.iface); ok = True
except Exception: ok = False
check("a malformed packet can't crash the radio's callback", ok)

# ---- watch-list mode (record everyone = off) ------------------------------------------------------------------------------------
t.set_passive_all(False)
time.sleep(0.45)
t.on_packet(bc(FLOOD, "deviceMetrics", DEV, 20), br.iface)
check("with 'record every node' off, unwatched nodes are ignored", cnt(node=FLOOD) == 0 and not t.passive_all())
t.watch_add("!eeee0005"); t.watch_add("!eeee0005")
t.on_packet(bc(FLOOD, "deviceMetrics", DEV, 21), br.iface)
check("...and watched ones are recorded (adding twice is harmless)", cnt(node=FLOOD) == 1 and [w["node_id"] for w in t.store.watched()] == [FLOOD])
w = t.store.watched()[0]
check("watch list reports readings and the last one's time", w["readings"] == 1 and w["node_name"] == "Quiet node" and abs(w["last_ts"] - time.time()) < 5, w)
for bad in ["bob", "!xyz", "", None, 5, ["!aaaa0001"]]:
    try: t.watch_add(bad); ok = False
    except T.TelemetryError: ok = True
    if not ok: check(f"watch rejects {bad!r}", False); break
else:
    check("watch rejects invalid node ids (including non-strings)", True)
try: t.watch_remove("!aaaa9999"); ok = False
except T.TelemetryError: ok = True
check("removing a node that isn't watched is a clear error", ok)
t.watch_remove("!eeee0005"); t.set_passive_all(True)
check("record-everyone can be switched back on", t.passive_all())

heard = t.heard()
check("'heard on the mesh' lists nodes the radio holds telemetry for, newest first, not us, not silent nodes",
      [h["node_id"] for h in heard] == [OTHER, W] and heard[1]["device"]["batteryLevel"] == 64, heard)

# ---- reading it back ---------------------------------------------------------------------------------------------------------------
rows = t.store.list(node=W)
check("list filters by node, newest first", rows and all(r["node_id"] == W for r in rows) and rows[0]["id"] > rows[-1]["id"])
check("list filters by kind and source", all(r["kind"] == "environment" for r in t.store.list(kind="environment")) and {r["source"] for r in t.store.list(source="broadcast")} == {"broadcast"} and not t.store.list(source="job"))
latest = {(r["node_id"], r["kind"]): r for r in t.store.latest()}
check("latest gives the newest reading per node and kind", latest[(W, "device")]["battery_level"] == 63 and (OTHER, "environment") in latest, list(latest))
st = t.store.stats()
check("stats count rows and report the time span", st["total"] == st["ok"] and st["nodes"] >= 3 and st["oldest_ts"] <= st["newest_ts"], st)
cells = list(csv.DictReader(io.StringIO(t.store.export_csv())))
check("CSV has every row; text from the mesh can't become a spreadsheet formula", len(cells) == st["total"] and any(c["node_name"] == "'=SUM(1)+1" for c in cells), cells[0])

# ---- retention and pruning -------------------------------------------------------------------------------------------------------------
check("retention defaults to 30 days", t.retention_days() == 30)
old = time.time() - 40 * 86400
t.store.add(old, W, "House Base", "device", "broadcast", "ok", values={"battery_level": 90})
t.store.add(old - 86400, OTHER, "x", "environment", "job", "timeout", job_id=3)     # an older row from the removed request feature
n_before = t.store.stats()["total"]
deleted = t.prune_old()
check("pruning deletes readings older than the retention period (whatever their source) and keeps the rest", deleted == 2 and t.store.stats()["total"] == n_before - 2 and all(r["ts"] > time.time() - 31 * 86400 for r in t.store.list(limit=500)), (deleted, n_before))
t.set_retention_days(1)
t.store.add(time.time() - 2 * 86400, W, "h", "device", "broadcast", "ok", values={"battery_level": 80})
check("a shorter retention prunes more", t.prune_old() == 1)
t.set_retention_days(0)
t.store.add(time.time() - 900 * 86400, W, "h", "device", "broadcast", "ok", values={"battery_level": 70})
check("retention 0 keeps everything", t.prune_old() == 0 and any(r["ts"] < time.time() - 800 * 86400 for r in t.store.list(limit=500)))
for bad in [-1, 3651, "30", 7.5, True, None, [1]]:
    try: t.set_retention_days(bad); ok = False
    except T.TelemetryError: ok = True
    if not ok: check(f"retention rejects {bad!r}", False); break
else:
    check("retention rejects negatives, > 10 years, text, floats, booleans, null, lists", True)
t.set_retention_days(30)
br2 = make(db=os.path.join(HERE, "tel_test_flag.db"), telemetry_retention_days=7)
check("--telemetry-retention-days overrides and is remembered", br2.telemetry.retention_days() == 7)
br2.audit.db.close()
br3 = b.Bridge(argparse.Namespace(**{**vars(br2.args), "telemetry_retention_days": None}))
check("a later start without the flag keeps the saved value", br3.telemetry.retention_days() == 7)
br3.audit.db.close()

# ---- web API ---------------------------------------------------------------------------------------------------------------------------
DB2 = os.path.join(HERE, "tel_test2.db")
if os.path.exists(DB2): os.remove(DB2)
brw = make(db=DB2); webui.start(brw)
URL = "http://127.0.0.1:8093"; H = {"Content-Type": "application/json"}
post = lambda p, o, h=H: rq.post(URL + p, headers=h, data=json.dumps(o))
brw.telemetry.on_packet(bc(W, "deviceMetrics", DEV, 40), brw.iface)
d = rq.get(URL + "/api/telemetry").json()
check("GET /api/telemetry: readings, latest, stats, kinds, metrics, retention and the record-all switch",
      len(d["readings"]) == 1 and d["latest"] and d["stats"]["total"] == 1 and "device" in d["kinds"] and "battery_level" in d["metrics"] and d["retention_days"] == 30 and d["record_all"] is True, list(d))
check("filters work through the API", rq.get(URL + "/api/telemetry", params={"node": OTHER}).json()["readings"] == [] and len(rq.get(URL + "/api/telemetry", params={"source": "broadcast"}).json()["readings"]) == 1)
csv_r = rq.get(URL + "/api/telemetry/export.csv")
check("CSV export downloads", csv_r.status_code == 200 and "attachment" in csv_r.headers["Content-Disposition"] and csv_r.text.splitlines()[0].startswith("id,ts,node_id"))
check("set retention through the API", post("/api/telemetry/retention", {"days": 14}).json()["retention_days"] == 14 and rq.get(URL + "/api/telemetry").json()["retention_days"] == 14)
check("retention input validated", all(post("/api/telemetry/retention", o).status_code == 400 for o in [{"days": -1}, {"days": "7"}, {"days": 99999}, {"days": True}, {}, {"days": None}]))
brw.telemetry.store.add(time.time() - 20 * 86400, W, "h", "device", "broadcast", "ok", values={"battery_level": 1})
check("'prune now' deletes what's past retention and says how many", post("/api/telemetry/prune", {}).json() == {"deleted": 1})
check("watch list through the API", rq.get(URL + "/api/telemetry/watch").json()["watched"] == [] and [h["node_id"] for h in rq.get(URL + "/api/telemetry/watch").json()["heard"]] == [OTHER, W])
check("add/remove a watched node; removing twice is a clean 400", post("/api/telemetry/watch/add", {"node": "!10000002"}).status_code == 200 and [w["node_id"] for w in rq.get(URL + "/api/telemetry/watch").json()["watched"]] == ["!10000002"]
      and post("/api/telemetry/watch/remove", {"node": "!10000002"}).status_code == 200 and post("/api/telemetry/watch/remove", {"node": "!10000002"}).status_code == 400)
check("watch input validated", all(post("/api/telemetry/watch/add", o).status_code == 400 for o in [{"node": "bob"}, {}, {"node": 5}]))
check("toggle 'record every node'", post("/api/telemetry/watch/all", {"enabled": False}).status_code == 200 and rq.get(URL + "/api/telemetry/watch").json()["all"] is False and post("/api/telemetry/watch/all", {"enabled": True}).status_code == 200)
check("'enabled' must be a real boolean", all(post("/api/telemetry/watch/all", o).status_code == 400 for o in [{"enabled": "yes"}, {"enabled": 1}, {}]))
check("the removed request/job endpoints are gone", all(post(p, {"node": W, "kind": "device", "interval_min": 10, "id": 1}).status_code == 404 for p in ["/api/telemetry/request", "/api/telemetry/jobs", "/api/telemetry/jobs/run"]) and rq.get(URL + "/api/telemetry/jobs").status_code == 404)
check("cross-origin changes blocked", all(post(p, o, {**H, "Origin": "http://evil.example"}).status_code == 403 for p, o in [("/api/telemetry/retention", {"days": 1}), ("/api/telemetry/prune", {}), ("/api/telemetry/watch/all", {"enabled": True})]))
check("non-JSON bodies blocked", rq.post(URL + "/api/telemetry/prune", headers={"Content-Type": "text/plain"}, data="x").status_code == 415)

print("\n%d failure(s)" % len(fails))
for f in (DB, DB2, os.path.join(HERE, "tel_test_flag.db")):
    try: os.remove(f)
    except OSError: pass
sys.exit(1 if fails else 0)
