"""End-to-end test: real Bridge/Audit/webui with a stub radio and a fake Ollama (no hardware)."""
import argparse, json, os, sqlite3, sys, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tempfile; HERE = tempfile.mkdtemp(prefix="meshtest_")   # scratch databases and caches go in a temp folder, never in the project
DB = os.path.join(HERE, "mem_test.db")
if os.path.exists(DB):
    os.remove(DB)

# 1. a database in the OLD schema (no `kind` column) holding one existing row
old = sqlite3.connect(DB)
old.executescript("""CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
 node_id TEXT NOT NULL, node_name TEXT, prompt TEXT, response TEXT, status TEXT NOT NULL, model TEXT,
 llm_ms INTEGER, chunks INTEGER DEFAULT 0, delivered INTEGER DEFAULT 0, relayed INTEGER DEFAULT 0,
 failed INTEGER DEFAULT 0, rx_snr REAL, rx_rssi INTEGER, hops INTEGER);""")
old.execute("INSERT INTO requests (ts,node_id,node_name,prompt,response,status) VALUES (?,?,?,?,?,?)",
            (time.time() - 100, "!11111111", "Legacy", "old question", "old answer", "answered"))
old.commit(); old.close()

# 2. fake Ollama that records every chat request
seen = []
class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen.append(body)
        out = json.dumps({"message": {"content": f"reply#{len(seen)}"}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def do_GET(self):
        out = json.dumps({"models": [{"name": "fake"}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
threading.Thread(target=HTTPServer(("127.0.0.1", 11499), Fake).serve_forever, daemon=True).start()

from meshllm import bridge as b
import requests as rq

args = argparse.Namespace(
    db=DB, port="STUB", model="fake", command="/ai", ollama_url="http://127.0.0.1:11499", max_tokens=50,
    num_ctx=4096, max_chunks=4, chunk_delay=0, cooldown=0, timeout=10, memory_turns=6, memory_hours=24,
    memory_chars=3000, no_log_inbound=False, web_host="127.0.0.1", web_port=8097, no_web=False,
    max_queue=5, queue_ttl=600, no_queue_notice=False, access_mode=None, daily_cap=None, confirm_seconds=60, no_tool_gate=True, chunk_bytes=160, send_retries=2, retry_delay=0.05)
br = b.Bridge(args); br.models.thinks = lambda name: False

class Stub:
    stream = object(); _rxThread = threading.current_thread(); nodes = {}
    class myInfo: my_node_num = 1
    sent = []
    def getMyUser(self): return {"id": "!00000001", "longName": "Test Node", "shortName": "TN", "hwModel": "STUB"}
    def sendText(self, msg, destinationId, wantAck, onResponse):
        self.sent.append((destinationId, msg, wantAck))
        threading.Timer(0.05, lambda: onResponse({"fromId": destinationId, "decoded": {"routing": {"errorReason": "NONE"}}})).start()
br.iface = Stub()
threading.Thread(target=br.worker, daemon=True).start()
threading.Thread(target=br.sender_loop, daemon=True).start()
from meshllm import webui; webui.start(br)

pid = [1000]
def dm(sender, text, to=1):
    pid[0] += 1
    br.on_receive({"id": pid[0], "from": int(sender[1:], 16), "fromId": sender, "to": to,
                   "decoded": {"text": text}, "rxSnr": 4.0, "rxRssi": -90}, br.iface)
def settle():
    for _ in range(100):
        time.sleep(0.05)
        if not br.q and br.current is None and br.outbox.empty(): break
    time.sleep(0.3)
def roles(body): return [(m["role"], m["content"]) for m in body["messages"]]

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)

A, B = "!aaaaaaaa", "!bbbbbbbb"
URL = "http://127.0.0.1:8097"

# migration
rows = br.audit.list()
check("old rows migrated with kind='ai'", len(rows) == 1 and rows[0]["kind"] == "ai")

# memory + isolation
dm(A, "/ai my name is Sam"); settle()
check("first request has no history", [r for r, _ in roles(seen[0])] == ["system", "user"], roles(seen[0]))
dm(A, "/ai what is my name"); settle()
r1 = roles(seen[1])
check("second request carries A's earlier exchange",
      [r for r, _ in r1] == ["system", "user", "assistant", "user"] and r1[1][1] == "my name is Sam" and r1[2][1] == "reply#1", r1)
dm(B, "/ai what is my name"); settle()
r2 = roles(seen[2])
check("user B does NOT see user A's history", [r for r, _ in r2] == ["system", "user"] and "Sam" not in json.dumps(seen[2]), r2)
check("options carry num_ctx", seen[1]["options"]["num_ctx"] == 4096)

# reset via mesh command
n_before = len(seen)
dm(A, "/ai reset"); settle()
check("reset did not call the model", len(seen) == n_before)
check("reset acknowledged to sender", any(d == A and "forgotten" in m for d, m, _ in br.iface.sent), br.iface.sent[-3:])
dm(A, "/ai hi again"); settle()
check("after reset, A has no history", [r for r, _ in roles(seen[-1])] == ["system", "user"], roles(seen[-1]))

# inbound plain DM: logged, never sent to the model, never remembered
n_before = len(seen)
dm(A, "hello operator, are you there?"); settle()
check("plain DM did not reach the model", len(seen) == n_before)
inb = [r for r in br.audit.conversation(A) if r["kind"] == "inbound"]
check("plain DM logged as inbound", len(inb) == 1 and inb[0]["prompt"].startswith("hello operator"))
dm(A, "/ai and now?"); settle()
check("inbound text excluded from model context", "hello operator" not in json.dumps(seen[-1]))

# channel (broadcast) message ignored entirely
n_rows = len(br.audit.list(limit=500))
dm(A, "/ai should be ignored", to=0xFFFFFFFF); settle()
check("broadcast message ignored", len(br.audit.list(limit=500)) == n_rows and len(seen) == n_before + 1)

# manual message through the web API
H = {"Content-Type": "application/json"}
r = rq.post(URL + "/api/send", headers=H, data=json.dumps({"node": A, "text": "Call me when you're free"}))
check("manual send accepted", r.status_code == 200 and "id" in r.json(), r.text)
settle()
check("manual message went out as a DM with ack requested", (A, "Call me when you're free", True) in br.iface.sent, br.iface.sent[-2:])
man = [x for x in br.audit.conversation(A) if x["kind"] == "manual"]
check("manual message audited + delivery tracked", len(man) == 1 and man[0]["delivered"] == 1, man)
dm(A, "/ai what did the operator say?"); settle()
last = roles(seen[-1])
check("operator message appears in the model's context, labelled",
      any(r == "assistant" and c.startswith("[message from the human operator] Call me") for r, c in last), last)
check("operator message not leaked to B's context",
      "operator" not in json.dumps(br.audit.history(B, 6, 86400, 3000)))

# input validation + CSRF-style checks
check("bad node id rejected", rq.post(URL + "/api/send", headers=H, data=json.dumps({"node": "bob", "text": "x"})).status_code == 400)
check("empty text rejected", rq.post(URL + "/api/send", headers=H, data=json.dumps({"node": A, "text": "  "})).status_code == 400)
check("over-long text rejected", rq.post(URL + "/api/send", headers=H, data=json.dumps({"node": A, "text": "word " * 400})).status_code == 400)
check("cross-origin send blocked", rq.post(URL + "/api/send", headers={**H, "Origin": "http://evil.example"}, data=json.dumps({"node": A, "text": "x"})).status_code == 403)
check("non-json send blocked", rq.post(URL + "/api/send", headers={"Content-Type": "text/plain"}, data="x").status_code == 415)

# read APIs
convs = rq.get(URL + "/api/conversations").json()
ids = {c["node_id"] for c in convs}
check("conversations list separates nodes", {A, B, "!11111111"} <= ids, ids)
ca = next(c for c in convs if c["node_id"] == A)
check("conversation summary reports memory size", ca["memory"] > 0, ca)
cv = rq.get(URL + "/api/conversation", params={"node": B}).json()
check("conversation view for B contains only B", all(m["node_id"] == B for m in cv["messages"]) and len(cv["messages"]) == 1, cv)
web_clear = rq.post(URL + "/api/memory/clear", headers=H, data=json.dumps({"node": A}))
check("web clear-memory works", web_clear.status_code == 200 and rq.get(URL + "/api/conversation", params={"node": A}).json()["memory"] == 0)
st = rq.get(URL + "/api/stats").json()
check("stats count only AI requests", st["total"] == len([r for r in br.audit.list(limit=500) if r["kind"] == "ai"]), st)
csv_txt = rq.get(URL + "/api/export.csv").text
check("csv has kind column", csv_txt.splitlines()[0].startswith("id,ts,kind,"))

# memory expiry + char budget (audit level)
check("expired memory is empty", br.audit.history("!11111111", 6, 0.001, 3000) == [] if (time.sleep(0.05) or True) else False)
check("legacy node memory available within TTL", len(br.audit.history("!11111111", 6, 86400, 3000)) == 2)
big = "x" * 1800
for i in range(3):
    rid = br.audit.new_request("!cccccccc", "C", f"q{i} {big}", status="answered", response=f"a{i} {big}")
h = br.audit.history("!cccccccc", 6, 86400, 3000)
check("char budget keeps newest turns and drops oldest", len(h) == 2 and h[0]["content"].startswith("q2"), [m["content"][:3] for m in h])
check("memory disabled with turns=0", br.audit.history("!cccccccc", 0, 86400, 3000) == [])

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)



