"""The tool-choice safety net: a live-state question must not be answered from imagination (fake Ollama, fake radio)."""
import argparse, json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import tempfile; HERE = tempfile.mkdtemp(prefix="meshtest_")   # scratch databases and caches go in a temp folder, never in the project
DB = os.path.join(HERE, "toolfix_test.db")
for ext in ("", "-wal", "-shm"):
    try: os.remove(DB + ext)
    except OSError: pass
from meshllm import bridge as b, webui
import requests as rq

log = []      # (kind, forced, user text)
systems = []  # the system message of every chat request
def call(name, args): return {"function": {"name": name, "arguments": args}}
class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _out(self, obj, code=200):
        out = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        sysmsg, user = body["messages"][0]["content"], body["messages"][-1]["content"].lower()
        if sysmsg.startswith("You route messages"):                                   # the YES/NO gate
            log.append(("gate", False, user)); return self._out({"message": {"content": "NO" if ("hello" in user or "joke" in user) else "YES"}})
        forced = b.FORCE_PROMPT.strip()[:30] in sysmsg
        log.append(("chat", forced, user))
        systems.append(sysmsg)
        if "notools" in user and "tools" in body: return self._out({"error": "registry.ollama.ai/x does not support tools"}, 400)
        if "eager" in user: return self._out({"message": {"content": "", "tool_calls": [call("mesh_summary", {})]}})       # uses the tool straight away
        if "reluctant" in user and forced: return self._out({"message": {"content": "", "tool_calls": [call("mesh_summary", {})]}})   # only when told to
        if "echoyear" in user: return self._out({"message": {"content": f"The year is {time.strftime('%Y')}."}})               # words, but only numbers it was given
        if "echono" in user: return self._out({"message": {"content": "No."}})                                                  # words with nothing to check
        if "stubborn" in user: return self._out({"message": {"content": "The mesh has 731 nodes and 88 low batteries."}})        # never uses a tool
        return self._out({"message": {"content": "The mesh has 731 nodes, 88 low on battery." if "reluctant" in user else "A plain reply."}})
    def do_GET(self): self._out({"models": [{"name": "fake"}]})
threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", 11499), Fake).serve_forever, daemon=True).start()

args = argparse.Namespace(db=DB, port="STUB", model="fake", command="/ai", ollama_url="http://127.0.0.1:11499", max_tokens=50, num_ctx=4096, max_chunks=4, chunk_delay=0,
    cooldown=0, timeout=10, memory_turns=6, memory_hours=24, memory_chars=3000, no_log_inbound=False, web_host="127.0.0.1", web_port=8097, no_web=False, max_queue=3,
    queue_ttl=600, no_queue_notice=False, access_mode=None, daily_cap=None, confirm_seconds=60, no_tool_gate=False, chunk_bytes=160, send_retries=2, retry_delay=0.05, traceroute_timeout=0.2)
NOW = time.time()
class Stub:
    stream = object(); _rxThread = threading.current_thread()
    class myInfo: my_node_num = 1
    def __init__(self):
        self.nodes = {"!00000001": {"num": 1, "user": {"id": "!00000001", "longName": "Us"}},
                      "!0000aaaa": {"num": 0xaaaa, "user": {"id": "!0000aaaa", "longName": "Lodge"}, "lastHeard": int(NOW - 60), "deviceMetrics": {"batteryLevel": 12}, "hopsAway": 0}}
        self.nodesByNum = {n["num"]: n for n in self.nodes.values()}
    def getMyUser(self): return {"id": "!00000001", "longName": "Test", "shortName": "T", "hwModel": "STUB"}
    def sendText(self, *a, **k): pass
br = b.Bridge(args); br.iface = Stub(); br.models.thinks = lambda name: False
threading.Thread(target=br.worker, daemon=True).start(); webui.start(br); time.sleep(0.3)
base = "http://127.0.0.1:8097"
fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)
def ask(text, wait=8):
    log.clear()
    rid = rq.post(base + "/api/ai/ask", json={"prompt": text}, timeout=5).json()["id"]; end = time.time() + wait
    while time.time() < end:
        rows = {m["id"]: m for m in rq.get(base + "/api/conversation?node=web-console", timeout=5).json()["messages"]}
        if rows[rid]["status"] != "queued": return rows[rid], list(log)
        time.sleep(0.05)
    return rows[rid], list(log)

row, lg = ask("eager: how is the mesh")
check("when the model uses the tool straight away there is no retry", row["action"] == "mesh_summary" and row["response"].startswith("Mesh: ") and [k for k, _, _ in lg] == ["gate", "chat"], lg)
row, lg = ask("reluctant: how is the mesh")
check("gate YES + an answer in words -> one insisting retry, and the tool result replaces the guess", row["action"] == "mesh_summary" and row["response"].startswith("Mesh: ") and "731" not in row["response"] and [(k, f) for k, f, _ in lg] == [("gate", False), ("chat", False), ("chat", True)], lg)
row, lg = ask("stubborn: how is the mesh")
check("if it still won't use a tool, the guess is NOT sent: the reply says what can be checked", row["response"] == b.NO_TOOL_ANSWER and "731" not in row["response"] and row["action"] is None and row["status"] == "answered" and [(k, f) for k, f, _ in lg] == [("gate", False), ("chat", False), ("chat", True)], (row["response"], lg))
check("the fallback fits in one radio message", len(b.NO_TOOL_ANSWER.encode()) <= args.chunk_bytes and len(br.make_messages(b.NO_TOOL_ANSWER, 4)) == 1)
row, lg = ask("echoyear: how is the mesh")
check("gate YES + words that only use numbers from the live facts: the words are kept (no tool was needed to be right)", row["response"] == f"The year is {time.strftime('%Y')}." and row["response"] != b.NO_TOOL_ANSWER and [(k, f) for k, f, _ in lg] == [("gate", False), ("chat", False), ("chat", True)], (row["response"], lg))
row, lg = ask("echono: how is the mesh")
check("...but words with no number to check are replaced by the list of checks", row["response"] == b.NO_TOOL_ANSWER, row["response"])
row, lg = ask("hello there")
check("gate NO -> ordinary chat, no tools offered and no retry", row["response"] == "A plain reply." and [k for k, _, _ in lg] == ["gate", "chat"] and not lg[1][1], lg)
row, lg = ask("tell me a joke")
check("general questions are never forced into a tool", row["response"] == "A plain reply." and row["action"] is None and [k for k, _, _ in lg].count("chat") == 1)
row, lg = ask("notools: how is the mesh")
check("a model without tool support chats normally and is not retried or given the fallback", row["response"] == "A plain reply." and row["action"] is None and [k for k, _, _ in lg].count("chat") == 2 and not any(f for _, f, _ in lg), lg)
# the retry is not used when the gate is off (older behaviour preserved)
br.args.no_tool_gate = True
row, lg = ask("stubborn: how is the mesh")
check("with the gate switched off a plain answer is left alone", row["response"] == "The mesh has 731 nodes and 88 low batteries." and [k for k, _, _ in lg] == ["chat"], lg)
br.args.no_tool_gate = False
# radio nodes get the same behaviour
br.audit.set_access("!0000aaaa", max_tier=0, pinned_key="K="); br.iface.nodes["!0000aaaa"]["user"]["publicKey"] = "K="
sent = []; br.iface.sendText = lambda msg, destinationId, wantAck, onResponse: sent.append((destinationId, msg))
threading.Thread(target=br.sender_loop, daemon=True).start()
log.clear()
br.on_receive({"id": 77, "from": 0xaaaa, "fromId": "!0000aaaa", "to": 1, "pkiEncrypted": True, "publicKey": "K=", "decoded": {"text": "/ai stubborn how is the mesh"}}, br.iface)
time.sleep(3)
check("a verified radio node gets the honest fallback over the radio too", any(d == "!0000aaaa" and b.NO_TOOL_ANSWER in m for d, m in sent), sent)
row, lg = ask("eager: how is the mesh")
sysm = systems[-1]
check("every chat request to the model carries the live facts", "It is " in sysm and "You are the AI assistant behind the radio node 'Test' (!00000001)" in sysm and "running the model fake" in sysm and "heard" in sysm, sysm[-520:])
check("...including the rules about what to do when it doesn't know", "say you can't tell instead of guessing" in sysm)
check("...and never a stranger's node name", "Lodge" not in sysm)
row, lg = ask("hello there")
check("ordinary chat (gate NO) gets the facts too, so 'are you there?' can be answered truthfully", "You are the AI assistant behind the radio node" in systems[-1] and "read-only tools" not in systems[-1], systems[-1][-300:])
ctx = "It is Wednesday September 30 2026, 02:05 PM. The radio has heard 12 nodes in the last 15 minutes and 65 in the last day (6 directly). Sensor nodes report 88 F, 35% humidity."
g = b.grounded
check("grounded(): numbers that are in the facts pass (also time written differently and rounding)", g("6 nodes are heard directly", ctx) and g("35% humidity", ctx) and g("about 88.4 F", ctx) and g("It is 2:05 PM", ctx) and g("12 in 15 minutes and 65 today", ctx))
check("grounded(): an invented number fails, even next to real ones", not g("Humidity is 82%", ctx) and not g("6 nodes and 731 packets", ctx) and not g("88 F and 91% humidity", ctx))
check("grounded(): words with no number, empty and None are not grounded", not g("No.", ctx) and not g("", ctx) and not g(None, ctx) and not g("6 nodes", None) and not g("6 nodes", ""))
check("grounded(): the question's own numbers count as given, but a computed result does not (it cannot be checked against the facts)", g("You asked about 3/4 inch", "what is 3/4 inch in decimal") and not g("It is 0.75 inch", "what is 3/4 inch in decimal"))
from meshllm.tools import eval_tools as E
tuned = (b.GATE_PROMPT + b.TOOL_PROMPT + b.SYSTEM_PROMPT + b.FORCE_PROMPT + " ".join(a.description for a in __import__("meshllm.actions", fromlist=["x"]).ACTIONS.values())).lower()
leaks = [pr for _, pr, _ in E.HELDOUT_CASES if pr.lower().rstrip("?.!") in tuned]
check("evaluation integrity: no held-out prompt appears verbatim in the gate, tool prompt, system prompt or any tool description", leaks == [], leaks)
leaks2 = [pr for _, pr, _ in E.HELDOUT_CASES if any(pr.lower().rstrip("?.!") == q.lower().rstrip("?.!") for _, q, _ in E.CASES)]
check("...and the held-out set shares no prompt with the dev set", leaks2 == [], leaks2)
check("the gate carries examples, and they are YES and NO cases", "Examples of YES" in b.GATE_PROMPT and "Examples of NO" in b.GATE_PROMPT)
print(f"\n{len(fails)} failed" if fails else "\nall passed")
sys.exit(1 if fails else 0)
