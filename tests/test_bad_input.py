"""Throw wrong-typed input at every web endpoint: nothing may answer 5xx or kill the server."""
import argparse, json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import tempfile; HERE = tempfile.mkdtemp(prefix="meshtest_")   # scratch databases and caches go in a temp folder, never in the project
DB = os.path.join(HERE, "fuzz_test.db")
if os.path.exists(DB): os.remove(DB)

class Fake(BaseHTTPRequestHandler):       # just enough Ollama for the model endpoints
    protocol_version = "HTTP/1.0"
    def log_message(self, *a): pass
    def _j(self, o, c=200):
        d = json.dumps(o).encode(); self.send_response(c); self.send_header("Content-Length", str(len(d))); self.end_headers(); self.wfile.write(d)
    def do_GET(self):
        if self.path == "/api/tags": return self._j({"models": [{"name": "m1:latest", "digest": "d", "size": 5, "details": {}}]})
        self._j({"models": []})
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        if self.path == "/api/show": return self._j({"capabilities": ["completion", "tools"]})
        if self.path == "/api/pull":
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"status":"success"}\n'); return
        self._j({"done": True, "message": {"content": "ok"}})
threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", 11493), Fake).serve_forever, daemon=True).start()

from meshllm import bridge as b, webui
import requests as rq

A = "!aaaa0001"
class Radio:
    stream = object(); _rxThread = threading.current_thread()
    nodes = {A: {"user": {"id": A, "longName": "A", "publicKey": "KEY="}, "lastHeard": 1}}
    myInfo = type("M", (), {"my_node_num": 1})()
    def getMyUser(self): return {"id": "!00000001", "longName": "Us"}
    def sendText(self, *a, **k): pass
    def sendData(self, *a, **k): pass
args = argparse.Namespace(db=DB, ollama_url="http://127.0.0.1:11493", model="m1:latest", access_mode=None, daily_cap=None, no_tool_gate=True,
    num_ctx=4096, max_tokens=50, timeout=5, web_host="127.0.0.1", web_port=8092, no_web=False, command="/ai", port="auto",
    memory_turns=6, memory_hours=24, memory_chars=3000, max_queue=5, max_chunks=4, cooldown=0, chunk_bytes=160, send_retries=0,
    retry_delay=0.1, chunk_delay=0, reconnect_hold=1, telemetry_min_interval=60, telemetry_timeout=0.2, telemetry_manual_cooldown=0)
br = b.Bridge(args); br.iface = Radio()
threading.Thread(target=br.sender_loop, daemon=True).start()
webui.start(br)
URL = "http://127.0.0.1:8092"; H = {"Content-Type": "application/json"}

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)

WEIRD = [5, -1, 1.5, True, None, [], {}, "x" * 5000, [A], {"a": 1}, "", "\u0000", "!zzzzzzzz", 10 ** 30]
POSTS = {
    "/api/pause": {"paused": True},
    "/api/send": {"node": A, "text": "hi"},
    "/api/memory/clear": {"node": A},
    "/api/access/mode": {"mode": "open"},
    "/api/access/default_cap": {"cap": 5},
    "/api/access/node": {"node": A, "access": "allow", "daily_cap": 3, "max_tier": 0, "pin_key": None},
    "/api/queue/cancel": {"id": 1},
    "/api/model": {"model": "m1:latest"},
    "/api/models/pull": {"name": "tiny:1b"},
    "/api/models/pull/cancel": {"name": "tiny:1b"},
    "/api/traceroute/request": {"node": A, "hop_limit": 7},
    "/api/telemetry/retention": {"days": 30},
    "/api/telemetry/prune": {},
    "/api/telemetry/watch/add": {"node": A},
    "/api/telemetry/watch/remove": {"node": A},
    "/api/telemetry/watch/all": {"enabled": True},
}
bad = []
def post(path, body):
    try:
        r = rq.post(URL + path, headers=H, data=json.dumps(body), timeout=10)
        return r.status_code
    except Exception as e:
        return f"EXC {type(e).__name__}"
n = 0
for path, good in POSTS.items():
    for key in good:
        for w in WEIRD:
            n += 1
            code = post(path, {**good, key: w})
            if not isinstance(code, int) or code >= 500: bad.append((path, key, str(w)[:20], code))
    for whole in ([], "text", 5, None, True, [A]):                # the body itself is not an object
        n += 1
        code = post(path, whole)
        if not isinstance(code, int) or code >= 500: bad.append((path, "<whole body>", str(whole)[:20], code))
    n += 1
    code = post(path, {})                                         # and empty
    if not isinstance(code, int) or code >= 500: bad.append((path, "<empty>", "", code))
check(f"{n} malformed POST bodies: none answered 5xx or dropped the connection", not bad, bad[:6])
alive = rq.get(URL + "/api/status", timeout=10)
check("server still answers normally afterwards", alive.status_code == 200)

# raw broken JSON / missing length
r = rq.post(URL + "/api/model", headers=H, data=b"{not json")
check("invalid JSON -> 400", r.status_code == 400, r.status_code)

GETS = ["/api/home", "/api/mesh/nodes", "/api/mesh/traffic", "/api/mesh/samples", "/api/mesh/node", "/api/traceroutes", "/api/traceroute", "/api/traceroute/request", "/api/telemetry/node", "/api/requests", "/api/conversation", "/api/telemetry", "/api/conversations", "/api/access", "/api/models",
        "/api/queue", "/api/stats", "/api/status", "/api/nodes", "/api/actions", "/api/telemetry/watch", "/api/export.csv", "/api/telemetry/export.csv"]
QVALS = ["abc", "-5", "0", "1e9", "", "%00", "' OR 1=1 --", "9" * 30, "!aaaa0001", "null", "[]", "../../etc/passwd"]
QKEYS = ["limit", "before", "since", "id", "node", "kind", "status", "source", "q"]
badg = []
for path in GETS:
    for k in QKEYS:
        for v in QVALS:
            try:
                r = rq.get(URL + path, params={k: v}, timeout=10)
                if r.status_code >= 500: badg.append((path, k, v[:12], r.status_code))
            except Exception as e:
                badg.append((path, k, v[:12], type(e).__name__))
check(f"{len(GETS) * len(QKEYS) * len(QVALS)} odd query strings: none answered 5xx", not badg, badg[:6])
check("server still alive after the GET barrage", rq.get(URL + "/api/status", timeout=10).status_code == 200)
# static/ lives at meshllm/static, so ../audit.py is a real file next to it and ../../README.md is a real file in the project folder:
# a refused request here proves the guard works, not just that the file is missing
check("path traversal on static files still refused", all(rq.get(URL + u, timeout=10).status_code in (403, 404) for u in ("/..%2faudit.py", "/..%2f..%2fREADME.md")))

print("\n%d failure(s)" % len(fails))
try: os.remove(DB)
except OSError: pass
sys.exit(1 if fails else 0)
