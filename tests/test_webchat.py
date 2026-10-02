"""Browser AI chat: the same queue/gate/model/tools as a radio DM, nothing transmitted (fake Ollama, fake radio)."""
import argparse, json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import tempfile; HERE = tempfile.mkdtemp(prefix="meshtest_")   # scratch databases and caches go in a temp folder, never in the project
DB = os.path.join(HERE, "webchat_test.db")
for ext in ("", "-wal", "-shm"):
    try: os.remove(DB + ext)
    except OSError: pass

seen = []
def call(name, args): return {"function": {"name": name, "arguments": args}}
class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _out(self, obj, code=200):
        out = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"]))); seen.append(body)
        t = body["messages"][-1]["content"].lower()
        if "boom" in t: return self._out({"error": "model exploded"}, 500)
        if "slowpoke" in t: time.sleep(1.2)
        calls = None
        if "mesh" in t: calls = [call("mesh_summary", {})]
        elif "nearest" in t: calls = [call("list_nodes", {"sort": "nearest"})]
        elif "demo" in t: calls = [call("demo_confirm", {})]
        elif "lowbatt" in t: calls = [call("list_nodes", {"sort": "low_battery"})]
        if "empty" in t: return self._out({"message": {"content": ""}})
        msg = {"content": "" if calls else f"plain answer to: {t[:40]} (memory {len(body['messages']) - 2})"}
        if calls: msg["tool_calls"] = calls
        self._out({"message": msg})
    def do_GET(self): self._out({"models": [{"name": "fake"}]})
threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", 11498), Fake).serve_forever, daemon=True).start()

import mesh_llm_bridge as b, webui
import requests as rq

args = argparse.Namespace(db=DB, port="STUB", model="fake", command="/ai", ollama_url="http://127.0.0.1:11498", max_tokens=50, num_ctx=4096, max_chunks=4, chunk_delay=0,
    cooldown=0, timeout=10, memory_turns=6, memory_hours=24, memory_chars=3000, no_log_inbound=False, web_host="127.0.0.1", web_port=8096, no_web=False, max_queue=3,
    queue_ttl=600, no_queue_notice=False, access_mode=None, daily_cap=1, confirm_seconds=60, no_tool_gate=True, chunk_bytes=160, send_retries=2, retry_delay=0.05, traceroute_timeout=0.2)
NOW = time.time()
class Stub:
    stream = object(); _rxThread = threading.current_thread()
    class myInfo: my_node_num = 1
    def __init__(self):
        self.sent = []
        self.nodes = {"!00000001": {"num": 1, "user": {"id": "!00000001", "longName": "Us"}},
                      "!0000aaaa": {"num": 0xaaaa, "user": {"id": "!0000aaaa", "longName": "Lodge"}, "lastHeard": int(NOW - 60), "deviceMetrics": {"batteryLevel": 12}, "hopsAway": 0}}
        self.nodesByNum = {n["num"]: n for n in self.nodes.values()}
    def getMyUser(self): return {"id": "!00000001", "longName": "Test", "shortName": "T", "hwModel": "STUB"}
    def sendText(self, msg, destinationId, wantAck, onResponse): self.sent.append((destinationId, msg))
br = b.Bridge(args); br.iface = Stub(); br.models.thinks = lambda name: False
threading.Thread(target=br.worker, daemon=True).start(); threading.Thread(target=br.sender_loop, daemon=True).start(); webui.start(br); time.sleep(0.3)
base = "http://127.0.0.1:8096"
post = lambda p, body: rq.post(base + p, json=body, timeout=5)
fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)
def ask(text, wait=8):
    r = post("/api/ai/ask", {"prompt": text})
    if r.status_code != 200: return r, None
    rid = r.json()["id"]; end = time.time() + wait
    while time.time() < end:
        rows = {m["id"]: m for m in rq.get(base + "/api/conversation?node=web-console", timeout=5).json()["messages"]}
        if rows[rid]["status"] != "queued": return r, rows[rid]
        time.sleep(0.05)
    return r, rows[rid]

r, row = ask("hello there")
check("a plain question is answered by the model", r.status_code == 200 and row["status"] == "answered" and row["response"].startswith("plain answer to: hello there") and row["kind"] == "web" and row["model"] == "fake" and row["llm_ms"] is not None, row)
check("the browser chat never transmits over the radio", br.iface.sent == [], br.iface.sent)
r, row = ask("how is the mesh doing?")
check("a mesh question uses the read-only mesh tool and gets the real numbers", row["status"] == "action_ok" and row["action"] == "mesh_summary" and row["response"].startswith("Mesh: "), row)
check("...and still nothing was transmitted", br.iface.sent == [])
r, row = ask("who is lowbatt?")
check("another tool with arguments works", row["action"] == "list_nodes" and "Lodge 12%" in row["response"], row)
r, row = ask("which node is nearest?")
check("every read-only tool works (same as a verified radio node at the read-only level)", row["action"] == "list_nodes" and row["status"] == "action_ok", row)
r, row = ask("run the demo action")
check("confirmed (tier 1) actions are not available from the browser chat", row["status"] == "action_denied" and "authorised" in row["response"] and br.pending == {}, row)
check("the chat remembers earlier turns (the model received them as context)", "memory" in ask("what did I say before?")[1]["response"] and "(memory 0)" not in ask("and before that?")[1]["response"])
check("the AI's memory for the chat is kept separately from any radio node's", all(m["content"] != "" for m in br.history("web-console")) and br.history("!0000aaaa") == [])
r, row = ask("boom")
check("a model error becomes an error row the chat can show, not a hang", row["status"] == "llm_error" and "LLM unavailable" in row["response"], row)
r, row = ask("empty please")
check("an empty model answer is reported clearly", row["status"] == "llm_error" and "no answer" in row["response"], row)
check("the radio daily cap and cooldown don't apply to the operator (the cap is 1 here, yet many questions were answered)", sum(1 for m in br.audit.conversation("web-console") if m["status"] in ("answered", "action_ok")) >= 5)

# input handling
for bad, word in [("", "Type a question"), ("   ", "Type a question"), (None, "Type a question"), (5, "Type a question"), (["x"], "Type a question"), ("x" * 601, "under 600")]:
    r = post("/api/ai/ask", {"prompt": bad}); check(f"input {str(bad)[:12]!r} is refused: {word}", r.status_code == 400 and word in r.json()["error"], r.text)
check("a missing prompt is refused", post("/api/ai/ask", {}).status_code == 400)
check("needs JSON and a same-site origin", rq.post(base + "/api/ai/ask", data="x", timeout=5).status_code == 415 and rq.post(base + "/api/ai/ask", json={"prompt": "hi"}, headers={"Origin": "http://evil.example"}, timeout=5).status_code == 403)
# one at a time, and the queue limit
r1 = post("/api/ai/ask", {"prompt": "slowpoke one"}); time.sleep(0.1)
r2 = post("/api/ai/ask", {"prompt": "second while busy"})
check("a second question while one is being answered is refused politely", r1.status_code == 200 and r2.status_code == 400 and "last question" in r2.json()["error"], r2.text)
end = time.time() + 6
while time.time() < end and br.pending_for("web-console"): time.sleep(0.05)
# fill the queue with radio-style jobs so web gets 'busy'
br.current = None
with br.qcv:
    for i in range(3): br.q.append(b.Job(-1, f"!0000f00{i}", "x", time.time(), -1))
r = post("/api/ai/ask", {"prompt": "queue is full"})
check("when the question queue is full the chat says so and records it", r.status_code == 400 and "queue is full" in r.json()["error"], r.text)
with br.qcv: br.q.clear()
rows = rq.get(base + "/api/conversation?node=web-console", timeout=5).json()["messages"]
check("...the refused question is marked busy in the log", rows[-1]["status"] == "busy")

# where it shows up (and where it must not)
names = [c["node_id"] for c in rq.get(base + "/api/conversations", timeout=5).json()]
check("the web console is NOT a conversation with a node", "web-console" not in names)
acc = [n["node_id"] for n in rq.get(base + "/api/access", timeout=5).json()["nodes"]]
check("...nor a node on the Access page", "web-console" not in acc)
log = rq.get(base + "/api/requests?limit=100", timeout=5).json()
check("it does appear in the AI log, named 'Web console'", any(r["node_id"] == "web-console" and r["node_name"] == "Web console" and r["kind"] == "web" for r in log))
st = rq.get(base + "/api/stats", timeout=5).json()
check("radio-question statistics don't count browser questions", st["total"] == 0 and st["unique_nodes"] == 0, st)
# clear memory
rq.post(base + "/api/memory/clear", json={"node": "web-console"}, timeout=5)
check("'New chat' clears what the AI remembers", br.history("web-console") == [])
r, row = ask("fresh start")
check("...and the next answer sees no earlier turns", "(memory 0)" in row["response"], row["response"])
# restart: a question left 'queued' is cancelled, not stuck forever
br.audit.new_request("web-console", "Web console", "left over", status="queued", kind="web")
br.restore_queue()
check("after a restart a leftover browser question is cancelled, not stuck 'in progress'", br.audit.conversation("web-console")[-1]["status"] == "cancelled")
# radio DMs are unaffected
pid = 500
pkt = {"id": pid, "from": 0xaaaa, "fromId": "!0000aaaa", "to": 1, "decoded": {"text": "/ai hello from radio"}}
br.on_receive(pkt, br.iface); time.sleep(2)
check("a radio DM still works and is answered over the radio", any(d == "!0000aaaa" and "plain answer" in m for d, m in br.iface.sent), br.iface.sent)

# ---- /ai help, /ai ping, /ai status: answered without the model -------------------------------------------------------------------------------------
sent = []
br.iface.sendText = lambda msg, destinationId, wantAck, onResponse: sent.append((destinationId, msg))
pk = [1000]
def dm(text, sender=0xaaaa, **fields):
    pk[0] += 1
    p = {"id": pk[0], "from": sender, "fromId": f"!{sender:08x}", "to": 1, "decoded": {"text": text}}; p.update(fields)
    n0 = len(sent); br.on_receive(p, br.iface); time.sleep(0.6); return [m for d, m in sent[n0:] if d == p["fromId"]]
calls0 = len(seen)
r = dm("/ai help")
check("/ai help answers in one radio message and names the commands", len(r) == 1 and "ping" in r[0] and "status" in r[0] and "/ai" in r[0] and len(r[0].encode()) <= 160, r)
check("'?' and upper case work too", dm("/ai ?") == r and dm("/ai HELP") == r)
r = dm("/ai ping", rxSnr=5.5, rxRssi=-42, hopStart=3, hopLimit=3)
check("ping on a direct message reports the signal it arrived with", r == ["Pong from Test: heard you directly: SNR 5.5 dB, RSSI -42 dBm."], r)
r = dm("/ai ping", rxSnr=-3.25, rxRssi=-101, hopStart=3, hopLimit=1)
check("ping through relays says how many and that the signal is the last hop's", r == ["Pong from Test: your message passed through 2 relays; the last hop was SNR -3.2 dB, RSSI -101 dBm."], r)
r = dm("/ai ping", rxSnr=5.5, rxRssi=-42, hopStart=3, hopLimit=2)
check("one relay is singular", "1 relay;" in r[0], r)
r = dm("/ai ping", rxSnr=5.5, rxRssi=-42)
check("ping with no hop information says so", r == ["Pong from Test: heard you (SNR 5.5 dB, RSSI -42 dBm); hop count unknown."], r)
r = dm("/ai ping", hopStart=3, hopLimit=3)
check("ping with no signal fields still answers", r == ["Pong from Test: heard you directly."], r)
r = dm("/ai ping", rxSnr="bad", rxRssi=[1], hopStart="x", hopLimit=None)
check("malformed signal fields never break ping", len(r) == 1 and r[0].startswith("Pong from Test"), r)
r = dm("/ai status")
check("status reports model, queue, radio and recent nodes without asking the model", len(r) == 1 and r[0].startswith("AI online. Model fake, queue 0/3, radio connected, ") and r[0].endswith("nodes heard in the last hour."), r)
check("none of them used the model", len(seen) == calls0, (len(seen), calls0))
for _ in range(3): dm("/ai help", sender=0xbbbb); dm("/ai ping", sender=0xbbbb, rxSnr=1, rxRssi=-50, hopStart=3, hopLimit=3)
r = dm("/ai hello there", sender=0xbbbb)
check("they don't count towards the daily limit (the cap is 1, yet a real question is still answered after 6 of them)", r and "plain answer" in r[0] and "Daily limit" not in r[0], r)
rows = [m for m in br.audit.conversation("!0000bbbb") + br.audit.conversation("!0000aaaa") if m["status"] in ("help", "ping", "status")]
check("they are logged with their own statuses", {"help", "ping", "status"} <= {m["status"] for m in rows} and all(m["llm_ms"] is None for m in rows))
br.paused = True
r = dm("/ai ping", rxSnr=5, rxRssi=-50, hopStart=3, hopLimit=3)
check("when the bot is paused these are silenced too", r == ["The AI assistant is paused right now."], r)
br.paused = False
br.audit.set_access("!0000aaaa", access="block")
check("a blocked node gets no reply, not even to ping", dm("/ai ping", rxSnr=5, rxRssi=-50, hopStart=3, hopLimit=3) == [] and dm("/ai help") == [])
br.audit.set_access("!0000aaaa", access="default")
check("ordinary direct messages are still not treated as commands", dm("help") == [] and dm("ping") == [])
# the browser chat answers the same commands directly
for text, want in [("help", "I'm an AI on this mesh"), ("/ai help", "I'm an AI on this mesh"), ("status", "AI online. Model fake"), ("/ai status", "AI online. Model fake"), ("ping", "Pong from the web console")]:
    n0 = len(seen); r_, row = ask(text)
    check(f"browser chat: {text!r} is answered without the model", r_.status_code == 200 and row["response"].startswith(want) and row["status"] in ("help", "status", "ping") and len(seen) == n0 and row["llm_ms"] is None, row)
check("...and isn't remembered as a conversation turn", all("Pong" not in m["content"] and "AI online" not in m["content"] for m in br.history("web-console")))
check("nothing was transmitted for the browser chat", not any(d == "web-console" for d, _ in sent))
print(f"\n{len(fails)} failed" if fails else "\nall passed")
sys.exit(1 if fails else 0)
