"""Queue + access-control tests: real Bridge/Audit/webui, stub radio, fake Ollama (no hardware)."""
import argparse, json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tempfile; HERE = tempfile.mkdtemp(prefix="meshtest_")   # scratch databases and caches go in a temp folder, never in the project
DB = os.path.join(HERE, "qa_test.db")
if os.path.exists(DB):
    os.remove(DB)

seen = []
class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _out(self, obj):
        out = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen.append(body)
        if "slow" in body["messages"][-1]["content"]:
            time.sleep(1.2)
        self._out({"message": {"content": f"reply#{len(seen)}"}})
    def do_GET(self): self._out({"models": [{"name": "fake"}]})
threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", 11498), Fake).serve_forever, daemon=True).start()

import mesh_llm_bridge as b
import requests as rq, webui

def make_args(**kw):
    base = dict(db=DB, port="STUB", model="fake", command="/ai", ollama_url="http://127.0.0.1:11498", max_tokens=50,
                num_ctx=4096, max_chunks=4, chunk_delay=0, cooldown=0, timeout=10, memory_turns=6, memory_hours=24,
                memory_chars=3000, no_log_inbound=False, web_host="127.0.0.1", web_port=8096, no_web=False,
                max_queue=3, queue_ttl=600, no_queue_notice=False, access_mode=None, daily_cap=None, confirm_seconds=60, no_tool_gate=True, chunk_bytes=160, send_retries=2, retry_delay=0.05)
    base.update(kw); return argparse.Namespace(**base)

class Stub:
    stream = object(); _rxThread = threading.current_thread()
    nodes = {"!0f0f0f0f": {"user": {"id": "!0f0f0f0f", "longName": "Truck 12", "shortName": "T12"}, "lastHeard": 50}}
    class myInfo: my_node_num = 1
    def __init__(self): self.sent = []
    def getMyUser(self): return {"id": "!00000001", "longName": "Test", "shortName": "T", "hwModel": "STUB"}
    def sendText(self, msg, destinationId, wantAck, onResponse):
        self.sent.append((destinationId, msg))
        if onResponse:
            threading.Timer(0.05, lambda: onResponse({"fromId": destinationId, "decoded": {"routing": {"errorReason": "NONE"}}})).start()

br = b.Bridge(make_args()); br.iface = Stub(); br.models.thinks = lambda name: False
threading.Thread(target=br.worker, daemon=True).start()
threading.Thread(target=br.sender_loop, daemon=True).start()
webui.start(br)

pid = [5000]
def dm(sender, text, to=1):
    pid[0] += 1
    br.on_receive({"id": pid[0], "from": int(sender[1:], 16), "fromId": sender, "to": to,
                   "decoded": {"text": text}}, br.iface)
def settle(t=6):
    end = time.time() + t
    while time.time() < end:
        time.sleep(0.05)
        if not br.q and br.current is None and br.outbox.empty(): break
    time.sleep(0.25)
def sent_to(n): return [m for d, m in br.iface.sent if d == n]
def status_rows(n, status): return [r for r in br.audit.conversation(n) if r["status"] == status]

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)

A, B, C, D, X, Y = "!aaaaaaaa", "!bbbbbbbb", "!cccccccc", "!dddddddd", "!eeeeee01", "!eeeeee02"
URL = "http://127.0.0.1:8096"; H = {"Content-Type": "application/json"}

# ---- blocklist -----------------------------------------------------------------------------
br.set_node_access(X, access="block")
n_calls = len(seen)
dm(X, "/ai let me in"); dm(X, "/ai please"); settle()
check("blocked node gets no model call", len(seen) == n_calls)
check("blocked node gets no reply at all", sent_to(X) == [], sent_to(X))
check("blocked attempt is logged once (throttled)", len(status_rows(X, "blocked")) == 1, status_rows(X, "blocked"))
dm(X, "hello there plain dm"); settle()
check("plain DM from blocked node is ignored entirely", not [r for r in br.audit.conversation(X) if r["kind"] == "inbound"])
br.set_node_access(X, access="default")
dm(X, "/ai now unblocked"); settle()
check("unblocking restores service", len(sent_to(X)) == 1 and sent_to(X)[0].startswith("reply#"), sent_to(X))

# ---- allowlist mode ------------------------------------------------------------------------
br.set_mode("allowlist"); br.set_node_access(Y, access="allow")
br.last_denied.clear()
n_calls = len(seen); before_x = len(sent_to(X))
dm(X, "/ai am I allowed?"); settle()
check("allowlist: unlisted node denied silently", len(seen) == n_calls and len(sent_to(X)) == before_x)
check("allowlist: denial audited as 'denied'", len(status_rows(X, "denied")) == 1)
dm(Y, "/ai I am on the list"); settle()
check("allowlist: allowed node is served", len(sent_to(Y)) == 1)
plain = len([r for r in br.audit.conversation(X) if r["kind"] == "inbound"])
dm(X, "just a plain dm from an unlisted node"); settle()
check("allowlist: plain DMs from unlisted nodes still show up for the operator",
      len([r for r in br.audit.conversation(X) if r["kind"] == "inbound"]) == plain + 1)
br.set_mode("open")

# ---- daily cap -----------------------------------------------------------------------------
br.set_default_cap(2)
for i in range(2): dm(C, f"/ai q{i}"); settle()
check("cap: first two questions answered", len(status_rows(C, "answered")) == 2)
dm(C, "/ai third"); settle()
check("cap: third gets a limit message, no model call",
      sent_to(C)[-1].startswith("Daily limit of 2") and len(status_rows(C, "cap")) == 1, sent_to(C))
n_sent = len(sent_to(C)); dm(C, "/ai fourth"); settle()
check("cap: repeat attempts are logged but quiet", len(sent_to(C)) == n_sent and len(status_rows(C, "cap")) == 2)
br.set_node_access(C, daily_cap=0)
dm(C, "/ai unlimited now"); settle()
check("cap: per-node override 0 = unlimited", len(status_rows(C, "answered")) == 3)
br.set_node_access(C, daily_cap=None, confirm_seconds=60, no_tool_gate=True, chunk_bytes=160, send_retries=2, retry_delay=0.05); br.set_default_cap(0)
check("cap: clearing override + default 0 leaves no access row", br.audit.get_access(C) == ("default", None))

# ---- queue: one per node, positions, full queue, cancel -------------------------------------
n_calls = len(seen)
dm(A, "/ai slow one"); time.sleep(0.35)
check("queue: A is being worked on", br.current is not None and br.current[1] == A)
dm(A, "/ai second from A"); time.sleep(0.2)
check("queue: second question from same node refused while one is pending",
      any("still working" in m for m in sent_to(A)), sent_to(A))
dm(B, "/ai from B"); dm(C, "/ai from C"); dm(D, "/ai from D (queue full)"); time.sleep(0.4)
check("queue: waiting nodes told their position", any(m.startswith("Queued (#2") for m in sent_to(B)) and any(m.startswith("Queued (#3") for m in sent_to(C)), (sent_to(B), sent_to(C)))
check("queue: first in line is not told to wait", not any(m.startswith("Queued") for m in sent_to(A)))
check("queue: full queue answered with 'busy'", any(m.startswith("Busy") for m in sent_to(D)) and len(status_rows(D, "busy")) == 1, sent_to(D))
snap = br.queue_snapshot()
check("queue: snapshot lists working + waiting in order",
      [(s["node_id"], s["working"]) for s in snap] == [(A, True), (B, False), (C, False)], snap)
rid_b = snap[1]["id"]
check("queue: cannot cancel a question already running", br.cancel(snap[0]["id"]) is False)
check("queue: cancel a waiting question", br.cancel(rid_b) is True and status_rows(B, "cancelled"))
settle(8)
check("queue: cancelled question never reached the model", not any("from B" in json.dumps(s) for s in seen))
check("queue: remaining questions answered in order", status_rows(A, "answered") and status_rows(C, "answered"))
check("queue: courtesy notices don't touch delivery accounting",
      all(r["chunks"] == 0 for r in br.audit.conversation(B) if r["status"] == "cancelled"))
check("queue drained", br.depth() == 0)

# ---- restart persistence -------------------------------------------------------------------
fresh = br.audit.new_request(B, None, "asked just before the restart", status="queued")
stale = br.audit.new_request(B, None, "asked long ago", status="queued")
with br.audit.lock:
    br.audit.db.execute("UPDATE requests SET ts=? WHERE id=?", (time.time() - 3600, stale)); br.audit.db.commit()
br2 = b.Bridge(make_args())   # a "restarted" bridge on the same database (worker not started)
br2.restore_queue()
check("restart: fresh queued question restored", [it[0] for it in br2.q] == [fresh], list(br2.q))
check("restart: stale queued question expired", [r["status"] for r in br.audit.conversation(B) if r["id"] == stale] == ["expired"])
br3 = b.Bridge(make_args(access_mode="allowlist", daily_cap=7))
check("flags override saved settings", br3.mode == "allowlist" and br3.default_cap == 7)
br3.set_mode("open"); br3.set_default_cap(0)

# ---- web API ---------------------------------------------------------------------------------
post = lambda p, obj, hdr=H: rq.post(URL + p, headers=hdr, data=json.dumps(obj))
check("api: bad node id rejected", post("/api/access/node", {"node": "zzz", "access": "block"}).status_code == 400)
check("api: bad access value rejected", post("/api/access/node", {"node": A, "access": "admin"}).status_code == 400)
check("api: negative cap rejected", post("/api/access/node", {"node": A, "daily_cap": -1}).status_code == 400)
check("api: string cap rejected", post("/api/access/node", {"node": A, "daily_cap": "5"}).status_code == 400)
check("api: boolean cap rejected", post("/api/access/node", {"node": A, "daily_cap": True}).status_code == 400)
check("api: bad mode rejected", post("/api/access/mode", {"mode": "everyone"}).status_code == 400)
check("api: string default cap rejected", post("/api/access/default_cap", {"cap": "5"}).status_code == 400)
check("api: cancel of unknown id -> 409", post("/api/queue/cancel", {"id": 99999}).status_code == 409)
check("api: cross-origin access change blocked", post("/api/access/node", {"node": A, "access": "block"}, {**H, "Origin": "http://evil.example"}).status_code == 403)
check("api: block a node", post("/api/access/node", {"node": A.upper(), "access": "block", "daily_cap": 3}).status_code == 200 and br.audit.get_access(A) == ("block", 3))
ov = rq.get(URL + "/api/access").json()
ids = {n["node_id"]: n for n in ov["nodes"]}
check("api: access overview merges asked + mesh-known nodes", {A, B, "!0f0f0f0f"} <= set(ids) and ids["!0f0f0f0f"]["asked"] is False and ids[A]["access"] == "block" and ids[A]["effective_cap"] == 3, ids.get(A))
conv = rq.get(URL + "/api/conversation", params={"node": A}).json()
check("api: conversation includes access info", conv["access"]["access"] == "block" and conv["access"]["effective_cap"] == 3, conv["access"])
check("api: queue endpoint empty when idle", rq.get(URL + "/api/queue").json() == [])
check("api: status reports queue + mode", {"queue_depth", "max_queue", "access_mode"} <= set(rq.get(URL + "/api/status").json()))
check("api: unblock node clears its row", post("/api/access/node", {"node": A, "access": "default", "daily_cap": None}).status_code == 200 and br.audit.get_access(A) == ("default", None))

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)


