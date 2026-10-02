"""Diagnostics and logs, coverage and the walk test, the written report, and the direct-message / AI-conversation split."""
import json, os, sys, time, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixture import make, Checker
import reach as R, diagnostics as D, report
import mesh_llm_bridge as b
import requests as rq

check = Checker()
br, radio, tmp = make(web_port=8114, web_host="127.0.0.1")
base = "http://127.0.0.1:8114"
get = lambda p, **kw: rq.get(base + p, timeout=10, **kw)
post = lambda p, body: rq.post(base + p, json=body, timeout=10)
logs = os.path.join(tmp, "logs"); os.makedirs(logs)
br.diagnostics.log_dir = __import__("pathlib").Path(logs)

# ---- diagnostics
d = get("/api/diagnostics").json()
ids = {c["id"]: c for c in d["checks"]}
check("the report has the main checks", {"radio", "ollama", "queue", "disk", "db", "backup", "errors", "web", "system"} <= set(ids), list(ids))
check("every check has a status, detail and hint field", all(c["status"] in ("ok", "warn", "bad", "info") and isinstance(c["detail"], str) and "hint" in c for c in d["checks"]))
check("a connected radio is reported ok with its port", ids["radio"]["status"] == "ok" or ids["radio"]["status"] == "warn", ids["radio"])
check("Ollama not running is reported as a problem with a hint", ids["ollama"]["status"] == "bad" and "Ollama" in ids["ollama"]["hint"], ids["ollama"])
check("the overall verdict is the worst one", d["overall"] == "bad")
check("localhost-only web is reported as fine", ids["web"]["status"] == "ok" and "this PC only" in ids["web"]["detail"])
br.iface = None
ids = {c["id"]: c for c in get("/api/diagnostics").json()["checks"]}
check("no radio is reported as bad with advice", ids["radio"]["status"] == "bad" and "USB" in ids["radio"]["hint"], ids["radio"])
br.iface = radio
open(os.path.join(logs, "bridge.err.log"), "w").write("Traceback (most recent call last):\nValueError: boom\n")
open(os.path.join(logs, "bridge.log"), "w").write("[radio] connected\n[error] something broke\n" + "".join(f"[tx] line {i}\n" for i in range(700)))
ids = {c["id"]: c for c in get("/api/diagnostics").json()["checks"]}
check("errors in the log are flagged", ids["errors"]["status"] == "warn", ids["errors"])
r = get("/api/logs", params={"which": "out", "lines": 50}).json()
check("the log tail returns the last lines only", len(r["lines"]) == 50 and r["lines"][-1] == "[tx] line 699" and r["exists"], r["lines"][:2])
check("a line count is capped", len(get("/api/logs", params={"which": "out", "lines": 99999}).json()["lines"]) == 500)
check("the error log is readable", "ValueError: boom" in get("/api/logs", params={"which": "err"}).json()["lines"])
check("only the two known logs can be read", get("/api/logs", params={"which": "../../t.db"}).status_code == 400 and get("/api/logs", params={"which": "audit"}).status_code == 400)
check("a missing log is an empty result, not an error", D.read_log("out", 10, os.path.join(tmp, "nope"))["lines"] == [] and not D.read_log("out", 10, os.path.join(tmp, "nope"))["exists"])
big = os.path.join(logs, "bridge.log"); open(big, "w").write("x" * 10 + "\n" + ("y" * 99 + "\n") * 20000)
check("a huge log is read from its end only", len(D.tail_lines(big, 5)) == 5 and all(l == "y" * 99 for l in D.tail_lines(big, 5)))
check("a junk line count is handled", get("/api/logs", params={"which": "out", "lines": "abc"}).status_code == 200)

# ---- coverage
ov = get("/api/coverage").json()
check("coverage knows our position from the radio", ov["us"] and abs(ov["us"]["lat"] - 37.80) < 1e-6, ov.get("us"))
check("no links yet means an empty but valid overview", ov["nodes"] == [] and len(ov["sectors"]) == 8 and ov["trend"] is None and ov["points"] == [], ov)
hour = int(time.time() // 3600)
def link(node, h, n, snr, rssi):
    br.audit.db.execute("INSERT OR REPLACE INTO mesh_links (hour, node_id, n, snr_sum, snr_min, snr_max, rssi_sum, rssi_min, rssi_max) VALUES (?,?,?,?,?,?,?,?,?)", (h, node, n, snr * n, snr - 1, snr + 1, rssi * n, rssi - 2, rssi + 2))
for k in range(6):
    link("!0000aaaa", hour - k, 4, 8.0 - k * 0.2, -80)          # Lodge ~1.1 km north, strong
    link("!0000bbbb", hour - k, 3, -6.0 - k * 0.3, -112)        # Ridge ~8 km north-east, weak
br.audit.db.commit()
ov = get("/api/coverage", params={"days": 7}).json()
byid = {n["id"]: n for n in ov["nodes"]}
check("nodes heard directly appear with distance and bearing", set(byid) == {"!0000aaaa", "!0000bbbb"} and 1.0 < byid["!0000aaaa"]["distance_km"] < 1.3 and byid["!0000aaaa"]["bearing"] < 5 or byid["!0000aaaa"]["bearing"] > 355, byid)
check("...the far one is further and to the north-east", byid["!0000bbbb"]["distance_km"] > 7 and 30 < byid["!0000bbbb"]["bearing"] < 70, byid["!0000bbbb"])
check("nodes with no position are counted as such, not dropped silently", ov["summary"]["nodes"] == 2 and ov["summary"]["without_position"] == 0)
link("!0000cccc", hour, 5, 2.0, -100); br.audit.db.commit()
ov = get("/api/coverage", params={"days": 7}).json()
check("a node without a known position is counted as without position", ov["summary"]["without_position"] == 1 and len(ov["nodes"]) == 2, ov["summary"])
check("the furthest node is named", ov["summary"]["farthest"]["name"] == "Ridge Repeater", ov["summary"])
sec = {s["name"]: s for s in ov["sectors"]}
check("the compass sectors hold the furthest node in each direction", sec["N"]["max_km"] and 1.0 < sec["N"]["max_km"] < 1.3 and sec["NE"]["max_km"] > 7 and sec["S"]["max_km"] is None, sec)
check("hourly points pair signal with distance", len(ov["points"]) == 12 and all({"d", "snr", "rssi", "id", "ts"} <= set(p) for p in ov["points"]), ov["points"][:2])
check("a trend needs more than two nodes: none here", ov["trend"] is None)
check("days is limited and junk is tolerated", get("/api/coverage", params={"days": 9999}).json()["days"] == 30 and get("/api/coverage", params={"days": "x"}).status_code == 200)
check("bearing maths: due east, north, south-west", abs(R.bearing(0, 0, 0, 1) - 90) < 0.01 and R.bearing(0, 0, 1, 0) < 0.01 and abs(R.bearing(1, 1, 0, 0) - 225) < 0.6 and R.sector_of(359) == 0 and R.sector_of(23) == 1 and R.sector_of(22) == 0)
pts = [(d_, -2 * d_ + 10 + (0.1 if i % 2 else -0.1), "n%d" % (i % 4)) for i, d_ in enumerate([0.5, 1, 1.5, 2, 3, 4, 5, 6, 7, 8])]
t = R.trend(pts)
check("the trend fit recovers a known line", t and abs(t["slope_db_per_km"] + 2) < 0.05 and abs(t["at_0_km"] - 10) < 0.3 and t["r2"] > 0.99, t)
check("too few points, too few nodes, or no spread give no trend", R.trend(pts[:5]) is None and R.trend([(1, -3, "a")] * 12) is None and R.trend([(d_, s, "a") for d_, s, _ in pts]) is None and R.trend([(1.0, -3, "a"), (1.05, -3.1, "b"), (1.1, -3, "c")] * 4) is None)
check("non-numbers in the points are ignored", R.trend(pts + [(None, 1, "z"), (float("nan"), 1, "y")]) is not None)

# ---- walk test
check("a walk test needs a node this radio has heard", post("/api/coverage/walk/start", {"node": "!99999999"}).status_code == 400)
check("...and not this radio itself", post("/api/coverage/walk/start", {"node": "!00000001"}).status_code == 400 and post("/api/coverage/walk/start", {}).status_code == 400)
st = get("/api/coverage/walk").json()
check("nothing is running at first", st["active"] is None and st["sessions"] == [])
pkt = lambda pid, snr=5.5, rssi=-95, hs=3, hl=3, pos=None, frm=0xbbbb: {"from": frm, "fromId": "!%08x" % frm, "to": 0xFFFFFFFF, "id": pid, "rxSnr": snr, "rxRssi": rssi, "hopStart": hs, "hopLimit": hl,
                                                                          "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "x", **({"position": pos} if pos else {})}}
br.coverage.on_packet(pkt(1), radio)
check("packets are not recorded while no walk test is running", br.audit.db.execute("SELECT COUNT(*) FROM walk_samples").fetchone()[0] == 0)
r = post("/api/coverage/walk/start", {"node": "!0000bbbb"})
check("a walk test starts", r.status_code == 200 and get("/api/coverage/walk").json()["active"]["node_id"] == "!0000bbbb", r.text)
check("only one at a time", post("/api/coverage/walk/start", {"node": "!0000aaaa"}).status_code == 400)
br.coverage.on_packet(pkt(10), radio)
br.coverage.on_packet(pkt(10), radio)                                              # the same packet again
br.coverage.on_packet(pkt(11, frm=0xaaaa), radio)                                   # somebody else
br.coverage.on_packet(pkt(12, snr=99), radio); br.coverage.on_packet(pkt(13, rssi=0), radio); br.coverage.on_packet(pkt(14, snr="x"), radio)   # not believable receptions
br.coverage.on_packet(pkt(15, hs=3, hl=2, pos={"latitudeI": 378500000, "longitudeI": -1221900000}), radio)
br.coverage.on_packet(pkt(16, hs=3, hl=1), radio)
br.coverage.on_packet({"from": 0xbbbb, "fromId": "!0000bbbb", "decoded": None}, radio); br.coverage.on_packet({}, None)
n = br.audit.db.execute("SELECT COUNT(*) FROM walk_samples").fetchone()[0]
check("only real receptions from the chosen node are recorded, once each", n == 3, n)
s = get("/api/coverage/walk").json()
check("the running session shows its count", s["active"]["samples"] == 3 and s["sessions"][0]["samples"] == 3 and s["sessions"][0]["direct"] == 1, s)
sid = s["active"]["id"]
d = get("/api/coverage/walk/session", params={"id": sid}).json()
check("samples carry signal, hops and distance", [x["hops"] for x in d["samples"]] == [0, 1, 2] and d["samples"][0]["snr"] == 5.5 and d["samples"][0]["distance_km"] > 7, d["samples"])
check("a position in the packet itself is used", abs(d["samples"][1]["lat"] - 37.85) < 1e-4, d["samples"][1])
check("the summary counts direct samples only for range", d["summary"]["direct"] == 1 and d["summary"]["farthest_direct_km"] > 7, d["summary"])
check("deleting a running session is refused", post("/api/coverage/walk/delete", {"id": sid}).status_code == 400)
check("stopping works and ends the session", post("/api/coverage/walk/stop", {}).json() == {"stopped": True} and get("/api/coverage/walk").json()["active"] is None and get("/api/coverage/walk").json()["sessions"][0]["ended"])
check("stopping when nothing runs is fine", post("/api/coverage/walk/stop", {}).json() == {"stopped": False})
br.coverage.on_packet(pkt(20), radio)
check("nothing is recorded after stopping", br.audit.db.execute("SELECT COUNT(*) FROM walk_samples").fetchone()[0] == 3)
post("/api/coverage/walk/start", {"node": "!0000bbbb"}); sid2 = get("/api/coverage/walk").json()["active"]["id"]
for i in range(12):
    br.coverage.on_packet(pkt(100 + i, snr=8 - i * 0.7, rssi=-80 - i * 3, pos={"latitudeI": 378000000 + i * 4000000, "longitudeI": -1222700000 + i * 500000}), radio)
post("/api/coverage/walk/stop", {})
d = get("/api/coverage/walk/session", params={"id": sid2}).json()
check("a walk with enough spread gets a signal-versus-distance line", d["trend"] and d["trend"]["slope_db_per_km"] < 0 and d["trend"]["n"] >= 8, d["trend"])
check("deleting a finished session removes its samples", post("/api/coverage/walk/delete", {"id": sid2}).json() == {"deleted": 1} and br.audit.db.execute("SELECT COUNT(*) FROM walk_samples WHERE session_id=?", (sid2,)).fetchone()[0] == 0)
check("an unknown session is a clean 404", get("/api/coverage/walk/session", params={"id": 99999}).status_code == 404 and post("/api/coverage/walk/delete", {"id": "x"}).status_code == 400)
post("/api/coverage/walk/start", {"node": "!0000bbbb"})
br.coverage._active["started"] = time.time() - R.WALK_MAX_SECONDS - 5
br.coverage.on_packet(pkt(300), radio)
check("a walk test ends itself after four hours", br.coverage._active is None)
br.coverage.start_walk("!0000bbbb"); br.coverage._active = None              # simulate a restart with a session left open
import reach as R2
c2 = R2.Coverage(br)
check("a session left open by a restart is closed when the bridge starts again", br.audit.db.execute("SELECT COUNT(*) FROM walk_sessions WHERE ended IS NULL").fetchone()[0] == 0)
check("a walk test transmits nothing", radio.sent == [])

# ---- the report
rep = get("/api/report", params={"days": 7}).json()
md = rep["markdown"]
check("the report is Markdown with the main sections", md.startswith("# Mesh and AI report - last 7 days") and all(h in md for h in ("## The mesh", "## Traffic", "## Signal and range", "## The AI", "## Public channel", "## Worth a look")), md[:300])
check("it carries real numbers from the link data", "3 nodes heard directly" in md and "Furthest direct neighbour: Ridge Repeater" in md, md)
check("no message text is in the report", "question 0" not in md)
br.channel.on_text({"from": 0xaaaa, "fromId": "!0000aaaa", "to": 0xFFFFFFFF, "id": 1, "decoded": {"text": "secret words about the ladder"}}, radio)
br.audit.new_request(prompt="a private question about payroll", status="answered", response="an answer about payroll", node_id="!0000aaaa", node_name="Lodge", kind="ai")
md = get("/api/report", params={"days": 7}).json()["markdown"]
check("channel text and AI questions never appear in it, only counts", "secret words" not in md and "payroll" not in md and "Posts heard: 1" in md and "from 1 node" in md, md[-900:])
check("the days choice is honoured, and junk falls back to 7", "last 1 day" in get("/api/report", params={"days": 1}).json()["markdown"] and "last 30 days" in get("/api/report", params={"days": 30}).json()["markdown"] and get("/api/report", params={"days": 5}).json()["days"] == 7)
r = get("/api/report.md", params={"days": 30})
check("it can be downloaded as a .md file", r.status_code == 200 and r.headers["Content-Type"].startswith("text/markdown") and ".md" in r.headers["Content-Disposition"] and r.text.startswith("# Mesh"))
check("the report lists what is worth a look (the low battery)", "low on battery" in md, md[-400:])

# ---- direct messages and AI conversations are separate lists
br.audit.new_request(prompt="hello there", status="inbound", node_id="!0000cccc", node_name="Far Farm", kind="inbound")
br.audit.new_request(prompt="", status="manual", response="hi back", node_id="!0000cccc", node_name="Far Farm", kind="manual")
dm = get("/api/conversations", params={"scope": "dm"}).json(); ai = get("/api/conversations", params={"scope": "ai"}).json(); allc = get("/api/conversations").json()
check("direct messages list only people you talked with directly", [c["node_id"] for c in dm] == ["!0000cccc"] and dm[0]["last_text"] == "hi back", dm)
check("the AI list only has nodes that asked the AI", [c["node_id"] for c in ai] == ["!0000aaaa"] and ai[0]["last_kind"] == "ai", ai)
check("with no scope both are together, as before", {c["node_id"] for c in allc} == {"!0000aaaa", "!0000cccc"})
check("a thread shows only its own kind", [m["kind"] for m in get("/api/conversation", params={"node": "!0000cccc", "scope": "dm"}).json()["messages"]] == ["inbound", "manual"]
      and get("/api/conversation", params={"node": "!0000cccc", "scope": "ai"}).json()["messages"] == [])
check("...and the AI thread doesn't show direct messages", {m["kind"] for m in get("/api/conversation", params={"node": "!0000aaaa", "scope": "ai"}).json()["messages"]} == {"ai"})
check("an unknown scope is refused, not treated as everything", get("/api/conversations", params={"scope": "everything"}).status_code == 400 and get("/api/conversation", params={"node": "x", "scope": "zzz"}).status_code == 400)
check("the browser chat's own thread still works without a scope", get("/api/conversation", params={"node": "web-console"}).status_code == 200)

# ---- keep the model loaded
body = b.build_chat_body("m", "hi", [], 0, 50, 4096)
check("questions ask Ollama to keep the model loaded", body["keep_alive"] == b.KEEP_ALIVE == "30m" and b.gate_body("m", "hi", 4096)["keep_alive"] == "30m")
seen = []
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        seen.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
        self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")
srv = ThreadingHTTPServer(("127.0.0.1", 11496), Fake); threading.Thread(target=srv.serve_forever, daemon=True).start()
br.args.ollama_url = "http://127.0.0.1:11496"; br.warm_up(); time.sleep(0.6)
check("start-up warm-up loads the model without asking it anything", seen and seen[0][0] == "/api/generate" and seen[0][1] == {"model": "fake", "keep_alive": "30m"}, seen)

check.done()
