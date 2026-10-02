"""Home-dashboard data: node snapshot, packet counters, history, alerts, feed, API (fake radio)."""
import argparse, json, os, sys, threading, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import tempfile; HERE = tempfile.mkdtemp(prefix="meshtest_")   # scratch databases and caches go in a temp folder, never in the project
DB = os.path.join(HERE, "mesh_test.db")
if os.path.exists(DB): os.remove(DB)

import mesh_llm_bridge as b, webui, mesh as M
import requests as rq

NOW = time.time()
def node(num, long, last_ago=None, hops=None, role="CLIENT", hw="HELTEC_V3", snr=5.5, dm=None, em=None, pos=None, mqtt=False, key=True):
    n = {"num": num, "user": {"id": f"!{num:08x}", "longName": long, "shortName": long[:4], "role": role, "hwModel": hw}, "snr": snr}
    if key: n["user"]["publicKey"] = "KEY="
    if last_ago is not None: n["lastHeard"] = int(NOW - last_ago)
    if hops is not None: n["hopsAway"] = hops
    if dm: n["deviceMetrics"] = dm
    if em: n["environmentMetrics"] = em
    if pos: n["position"] = pos
    if mqtt: n["viaMqtt"] = True
    return n
US, N1, N2, N3, N4, N5, N6, N7 = 1, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77
NODES = {n["user"]["id"]: n for n in [
    node(US, "Us", 5, None, dm={"batteryLevel": 100, "channelUtilization": 4.0, "airUtilTx": 0.5, "voltage": 4.2, "uptimeSeconds": 999}, pos={"latitude": 37.8, "longitude": -122.3}),
    node(N1, "Low Battery Lodge", 300, 0, dm={"batteryLevel": 15, "channelUtilization": 10.0, "voltage": 3.5}, pos={"latitude": 37.9, "longitude": -122.4}),
    node(N2, "Ridge Repeater", 2400, 1, role="ROUTER", hw="RAK4631", dm={"batteryLevel": 90, "channelUtilization": 20.0}, pos={"latitudeI": 380000000, "longitudeI": -1225000000}),
    node(N3, "Far Node", 36000, 3),
    node(N4, "Stale Node", 3 * 86400, 2, dm={"batteryLevel": 5}),
    node(N5, "Internet Friend", None, None, mqtt=True),
    node(N6, "Weather Mast", 600, 1, em={"temperature": 18.5, "relativeHumidity": 60.0, "barometricPressure": 1010.0}),
    node(N7, "Null Island", 900, 2, pos={"latitude": 0.0, "longitude": 0.0, "latitudeI": 0, "longitudeI": 0}),
]}

class Radio:
    stream = object(); _rxThread = threading.current_thread()
    nodes = NODES; nodesByNum = {n["num"]: n for n in NODES.values()}
    myInfo = type("M", (), {"my_node_num": US})()
    def getMyUser(self): return {"id": "!00000001", "longName": "Us"}

def make(db=DB, **over):
    base = dict(db=db, ollama_url="http://127.0.0.1:9", model="m", access_mode=None, daily_cap=None, no_tool_gate=True,
                web_host="127.0.0.1", web_port=8090, no_web=True, command="/ai", port="auto", memory_turns=6, memory_hours=24,
                memory_chars=3000, max_queue=5, max_chunks=4, cooldown=0, traceroute_timeout=0.2)
    base.update(over)
    if os.path.exists(db):
        try: os.remove(db)
        except OSError: pass
    br = b.Bridge(argparse.Namespace(**base)); br.iface = Radio()
    return br

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)

# ---- one node, flattened ----------------------------------------------------------------------------------------------------
r = M.node_row(NODES["!00000011"], US, NOW)
check("a node is flattened with name, role, hardware, hops, signal and metrics", (r["id"], r["name"], r["role"], r["hw"], r["hops"], r["snr"], r["battery"], r["voltage"], r["channel_util"]) == ("!00000011", "Low Battery Lodge", "CLIENT", "HELTEC_V3", 0, 5.5, 15, 3.5, 10.0), r)
check("age is computed from last-heard; our own node is flagged", abs(r["age_s"] - 300) <= 2 and not r["us"] and M.node_row(NODES["!00000001"], US, NOW)["us"])
check("positions are read from decimal degrees or latitudeI/longitudeI, and 0,0 means none", (r["lat"], r["lon"]) == (37.9, -122.4) and abs(M.node_row(NODES["!00000022"], US, NOW)["lat"] - 38.0) < 1e-9 and M.node_row(NODES["!00000077"], US, NOW)["lat"] is None)
r5 = M.node_row(NODES["!00000055"], US, NOW)
check("a node with no last-heard time or metrics still flattens (fields are None)", r5["age_s"] is None and r5["battery"] is None and r5["mqtt"] is True and r5["lat"] is None)
check("an entry with no user block falls back to an id made from the node number", M.node_row({"num": 0xABCDEF}, US, NOW)["id"] == "!00abcdef")
check("environment metrics are exposed", M.node_row(NODES["!00000066"], US, NOW)["temperature"] == 18.5 and M.node_row(NODES["!00000066"], US, NOW)["humidity"] == 60.0)

# ---- headline numbers ----------------------------------------------------------------------------------------------------------
rows = [M.node_row(n, US, NOW) for n in NODES.values()]
s = M.summarize(rows)
check("counts of nodes heard in the last 15 min / hour / day (not counting us)", (s["nodes_total"], s["heard_15m"], s["heard_1h"], s["heard_24h"]) == (8, 3, 4, 5), s)
check("direct neighbours, furthest hop count, nodes with a position, with telemetry, via MQTT", (s["direct"], s["max_hops"], s["with_position"], s["with_telemetry"], s["via_mqtt"]) == (1, 3, 2, 4, 1), s)
check("mean channel use counts only nodes heard in the last hour that report it", s["avg_channel_util"] == 15.0, s["avg_channel_util"])
check("role and hardware breakdowns", s["roles"].get("CLIENT") == 6 and s["roles"].get("ROUTER") == 1 and s["hardware"].get("HELTEC_V3") == 6, (s["roles"], s["hardware"]))
check("an empty mesh summarises without dividing by zero", M.summarize([])["heard_24h"] == 0 and M.summarize([])["avg_channel_util"] is None and M.summarize([])["max_hops"] is None)

# ---- counting what the radio hears ------------------------------------------------------------------------------------------------
br = make(); mesh = br.mesh
def pkt(frm, port="TEXT_MESSAGE_APP", **kw):
    p = {"from": frm, "fromId": f"!{frm:08x}"}
    if port: p["decoded"] = {"portnum": port}
    p.update(kw); return p
for f, p in [(N1, "TELEMETRY_APP"), (N1, "TELEMETRY_APP"), (N2, "TELEMETRY_APP"), (N2, "NODEINFO_APP"), (N6, "POSITION_APP"), (N6, None), (N6, None)]:
    mesh.on_packet(pkt(f, p), br.iface)
mesh.on_packet(pkt(US, "TEXT_MESSAGE_APP"), br.iface)
mesh.on_packet({"decoded": {"portnum": "X"}}, br.iface); mesh.on_packet("garbage", br.iface)
tr = mesh.traffic(24)
check("packets are counted by type, including ones the radio couldn't decrypt", dict(zip(tr["types"], [sum(h["counts"].get(t, 0) for h in tr["hours"]) for t in tr["types"]])) == {"TELEMETRY_APP": 3, "NODEINFO_APP": 1, "POSITION_APP": 1, "ENCRYPTED": 2}, tr["types"])
check("our own packets and malformed ones aren't counted", tr["total"] == 7)
check("traffic covers every hour of the window, oldest first, empty hours included", len(tr["hours"]) == 24 and [h["hour"] for h in tr["hours"]] == sorted(h["hour"] for h in tr["hours"]) and sum(1 for h in tr["hours"] if not h["counts"]) == 23)
check("types come back most common first", tr["types"][0] == "TELEMETRY_APP")
check("top senders in the last hour are named from the node list", [(t["node_id"], t["n"]) for t in tr["talkers"]][:2] == [("!00000066", 3), ("!00000011", 2)] and tr["talkers"][0]["name"] == "Weather Mast", tr["talkers"])
mesh.on_packet(pkt(N1, "TELEMETRY_APP"), br.iface)
check("counts accumulate across flushes", mesh.traffic(24)["total"] == 8 and [t for t in mesh.traffic(24)["talkers"] if t["node_id"] == "!00000011"][0]["n"] == 3)
h_now = int(time.time() // 3600)
with br.audit.lock:
    br.audit.db.execute("INSERT INTO mesh_packets VALUES (?, 'TEXT_MESSAGE_APP', 5)", (h_now - 5,)); br.audit.db.commit()
t6 = mesh.traffic(6)
check("an older hour appears in its own slot, and a shorter window drops the rest", len(t6["hours"]) == 6 and t6["hours"][0]["counts"].get("TEXT_MESSAGE_APP") == 5 and mesh.traffic(3)["total"] == 8)
check("absurd windows are clamped", len(mesh.traffic(0)["hours"]) == 1 and len(mesh.traffic(10 ** 9)["hours"]) == M.PACKET_RETENTION_H)
brd = make(db=os.path.join(HERE, "mesh_test_off.db"), no_mesh_stats=True)
brd.mesh.on_packet(pkt(N1, "TELEMETRY_APP"), brd.iface)
check("--no-mesh-stats switches the counting off", brd.mesh.traffic(3)["total"] == 0)
brd.audit.db.close()

# ---- history snapshots ------------------------------------------------------------------------------------------------------------
row = mesh.sample()
smp = mesh.samples(1)
check("a snapshot stores mesh health and our own radio's load", len(smp) == 1 and smp[0]["nodes_total"] == 8 and smp[0]["heard_1h"] == 4 and smp[0]["avg_channel_util"] == 15.0 and smp[0]["us_battery"] == 100 and smp[0]["us_channel_util"] == 4.0 and smp[0]["us_air_util_tx"] == 0.5, smp)
check("the snapshot records how many packets arrived since the previous one (and resets)", smp[0]["packets"] == 8 and (mesh.sample(), mesh.samples(1)[-1]["packets"])[1] == 0)
br.iface, saved = None, br.iface
check("no radio -> no snapshot, no crash", mesh.sample() is None and mesh.nodes() == [])
br.iface = saved
with br.audit.lock:
    br.audit.db.execute("INSERT INTO mesh_samples (ts, nodes_total) VALUES (?, 3)", (time.time() - 40 * 86400,)); br.audit.db.commit()
check("history window is respected", all(x["ts"] > time.time() - 3700 for x in mesh.samples(1)) and len(mesh.samples(24 * 30)) == 2)

# ---- pruning -------------------------------------------------------------------------------------------------------------------------
with br.audit.lock:
    br.audit.db.execute("INSERT INTO mesh_packets VALUES (?, 'OLD', 1)", (h_now - M.PACKET_RETENTION_H - 5,))
    br.audit.db.execute("INSERT INTO mesh_talkers VALUES (?, '!old', 1)", (h_now - M.PACKET_RETENTION_H - 5,)); br.audit.db.commit()
br.telemetry.store.add(time.time() - 45 * 86400, "!00000011", "x", "device", "broadcast", "ok", values={"battery_level": 1})
br.telemetry.store.add(time.time() - 3600, "!00000011", "x", "device", "broadcast", "ok", values={"battery_level": 2})
mesh.prune()
with br.audit.lock:
    left = br.audit.db.execute("SELECT (SELECT COUNT(*) FROM mesh_packets WHERE portnum='OLD'), (SELECT COUNT(*) FROM mesh_talkers WHERE node_id='!old'), (SELECT COUNT(*) FROM mesh_samples WHERE ts < ?)", (time.time() - 31 * 86400,)).fetchone()
check("the prune job deletes old packet counts, talkers and snapshots", tuple(left) == (0, 0, 0), tuple(left))
check("...and telemetry older than its retention setting, keeping recent readings", [r["battery_level"] for r in br.telemetry.store.list(limit=50)] == [2])

# ---- alerts --------------------------------------------------------------------------------------------------------------------------
al = mesh.alerts(rows)
texts = [a["text"] for a in al]
check("low-battery nodes heard in the last day are flagged in one line (and only those)", sum("low on battery" in t for t in texts) == 1 and any("1 node low on battery (20% or less): Low Battery Lodge 15%" in t for t in texts) and not any("Stale Node" in t for t in texts) and not any("Ridge Repeater 90" in t for t in texts), texts)
check("an unavailable AI model is flagged", any("isn't available" in t for t in texts), texts)
br.telemetry.watch_add("!00000022")
br.telemetry.store.add(time.time() - 4 * 3600, "!00000022", "Ridge Repeater", "device", "broadcast", "ok", values={"battery_level": 90})
check("a watched node that has gone quiet for hours is flagged", any("Ridge Repeater (watched) hasn't broadcast telemetry for 4 h" in a["text"] for a in mesh.alerts(rows)))
br.iface, saved = None, br.iface
check("a missing radio is the first thing flagged", mesh.alerts([])[0]["level"] == "bad" and "isn't connected" in mesh.alerts([])[0]["text"])
br.iface = saved

# ---- activity feed -------------------------------------------------------------------------------------------------------------------
br.audit.new_request("!00000011", "Low Battery Lodge", "how many nodes are around?", status="answered", response="x")
br.audit.new_request("!00000022", "Ridge Repeater", "hello there", status="inbound", kind="inbound")
br.audit.new_request("!00000022", "Ridge Repeater", "", status="manual", kind="manual", response="on my way")
br.telemetry.store.add(time.time(), "!00000066", "Weather Mast", "environment", "broadcast", "ok", values={"temperature": 18.5})
br.traceroute.store.add(ts=time.time(), node_id="!00000033", node_name="Far Node", hop_limit=7, status="ok", relays_towards=2)
feed = mesh.feed(30)
kinds = {f["type"] for f in feed}
check("the feed merges AI questions, direct messages, your messages, telemetry and traceroutes", {"ai", "dm", "you", "broadcast", "traceroute"} <= kinds, kinds)
check("newest first, and capped", [f["ts"] for f in feed] == sorted((f["ts"] for f in feed), reverse=True) and len(mesh.feed(3)) == 3)
check("entries name the node and stay short", any("Weather Mast: 18.5 °C" in f["text"] for f in feed) and all(len(f["text"]) < 160 for f in feed), [f["text"] for f in feed][:4])

# ---- overview / detail ------------------------------------------------------------------------------------------------------------------
ov = mesh.overview()
check("the overview bundles radio, our node, summary, alerts, feed and activity counts", {"radio", "us", "summary", "alerts", "feed", "activity"} <= set(ov) and ov["us"]["battery"] == 100 and ov["summary"]["nodes_total"] == 8 and ov["radio"]["connected"] and ov["activity"]["traceroutes"] == 1 and ov["activity"]["telemetry_24h"] >= 2, {k: ov[k] for k in ("radio", "activity")})
d = mesh.node_detail("!00000022")
check("node detail: the node, its recent readings, latest traceroute, access rules and watch state", d["node"]["name"] == "Ridge Repeater" and d["readings"] and d["watched"] is True and d["traceroute"] is None and d["access"]["access"] == "default", d and list(d))
check("...and the latest traceroute to that node when there is one", mesh.node_detail("!00000033")["traceroute"]["relays_towards"] == 2)
check("an unknown node has no detail", mesh.node_detail("!deadbeef") is None and mesh.node_detail("") is None)
br.iface, saved = None, br.iface
ov2 = mesh.overview()
check("the overview still works with the radio away", ov2["radio"]["connected"] is False and ov2["us"] is None and ov2["summary"]["nodes_total"] == 0 and ov2["alerts"][0]["level"] == "bad")
br.iface = saved

# ---- web API ------------------------------------------------------------------------------------------------------------------------------
webui.start(br); URL = "http://127.0.0.1:8090"
home = rq.get(URL + "/api/home").json()
check("GET /api/home", home["summary"]["nodes_total"] == 8 and home["feed"] and home["us"]["name"] == "Us")
nodes = rq.get(URL + "/api/mesh/nodes").json()
check("GET /api/mesh/nodes lists every node with positions for the map", len(nodes) == 8 and sum(1 for n in nodes if n["lat"] is not None) == 3 and {n["id"] for n in nodes} == set(NODES))
check("GET /api/mesh/traffic honours the hours parameter and survives junk", len(rq.get(URL + "/api/mesh/traffic", params={"hours": 6}).json()["hours"]) == 6 and len(rq.get(URL + "/api/mesh/traffic", params={"hours": "abc"}).json()["hours"]) == 24)
check("GET /api/mesh/samples", isinstance(rq.get(URL + "/api/mesh/samples", params={"hours": 24}).json(), list))
check("GET /api/mesh/node: detail, or a clean 404", rq.get(URL + "/api/mesh/node", params={"id": "!00000022"}).json()["node"]["name"] == "Ridge Repeater" and rq.get(URL + "/api/mesh/node", params={"id": "!nope"}).status_code == 404 and rq.get(URL + "/api/mesh/node").status_code == 404)

print("\n%d failure(s)" % len(fails))
for f in (DB, os.path.join(HERE, "mesh_test_off.db")):
    try: os.remove(f)
    except OSError: pass
sys.exit(1 if fails else 0)
