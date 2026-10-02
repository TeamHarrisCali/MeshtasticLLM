"""PC-action tests: key verification, tiers, tool-call validation, confirmation codes (no hardware)."""
import argparse, json, os, re, sqlite3, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tempfile; HERE = tempfile.mkdtemp(prefix="meshtest_")   # scratch databases and caches go in a temp folder, never in the project
DB = os.path.join(HERE, "act_test.db")
for f in (DB,):
    if os.path.exists(f): os.remove(f)

seen = []
def call(name, args): return {"function": {"name": name, "arguments": args}}
class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _out(self, obj):
        out = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen.append(body)
        t = body["messages"][-1]["content"].lower()
        calls = None
        if "stringargs" in t: calls = [call("list_nodes", '{"sort": "recent"}')]
        elif "badsort" in t: calls = [call("list_nodes", {"sort": "sideways"})]
        elif "deleteall" in t: calls = [call("delete_files", {"path": "C:\\"})]
        elif "demo" in t: calls = [call("demo_confirm", {})]
        elif "sneaky" in t or "recent" in t: calls = [call("list_nodes", {"sort": "recent"})]
        elif "summary" in t: calls = [call("mesh_summary", {})]
        msg = {"content": "" if calls else "plain answer"}
        if calls: msg["tool_calls"] = calls
        self._out({"message": msg})
    def do_GET(self): self._out({"models": [{"name": "fake"}]})
threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", 11497), Fake).serve_forever, daemon=True).start()

from meshllm import bridge as b, actions, webui
import requests as rq
from meshllm.audit import Audit

args = argparse.Namespace(
    db=DB, port="STUB", model="fake", command="/ai", ollama_url="http://127.0.0.1:11497", max_tokens=50,
    num_ctx=4096, max_chunks=4, chunk_delay=0, cooldown=0, timeout=10, memory_turns=6, memory_hours=24,
    memory_chars=3000, no_log_inbound=False, web_host="127.0.0.1", web_port=8095, no_web=False,
    max_queue=5, queue_ttl=600, no_queue_notice=False, access_mode=None, daily_cap=None, confirm_seconds=60, no_tool_gate=True, chunk_bytes=160, send_retries=2, retry_delay=0.05)

P, Q, N0, R, NEW = "!aaaa0001", "!aaaa0002", "!aaaa0003", "!aaaa0004", "!aaaa0005"
KEYS = {P: "KEY-P-AAAA=", Q: "KEY-Q-BBBB=", N0: "KEY-N-CCCC=", R: "KEY-R-DDDD="}

class Stub:
    stream = object(); _rxThread = threading.current_thread()
    class myInfo: my_node_num = 1
    def __init__(self):
        self.sent = []
        self.nodes = {n: {"user": {"id": n, "longName": f"Node {n[-1]}", "publicKey": k}, "lastHeard": 10}
                      for n, k in KEYS.items()}
        self.nodes[NEW] = {"user": {"id": NEW, "longName": "No key yet"}, "lastHeard": 5}
    def getMyUser(self): return {"id": "!00000001", "longName": "Test", "shortName": "T", "hwModel": "STUB"}
    def sendText(self, msg, destinationId, wantAck, onResponse):
        self.sent.append((destinationId, msg))
        if onResponse:
            threading.Timer(0.03, lambda: onResponse({"fromId": destinationId, "decoded": {"routing": {"errorReason": "NONE"}}})).start()

br = b.Bridge(args); br.iface = Stub(); br.models.thinks = lambda name: False
threading.Thread(target=br.worker, daemon=True).start()
threading.Thread(target=br.sender_loop, daemon=True).start()
webui.start(br)

pid = [9000]
def dm(sender, text, pki=True, pubkey=None, to=1):
    pid[0] += 1
    pkt = {"id": pid[0], "from": int(sender[1:], 16), "fromId": sender, "to": to, "decoded": {"text": text}}
    if pki: pkt["pkiEncrypted"] = True
    if pubkey: pkt["publicKey"] = pubkey
    br.on_receive(pkt, br.iface)
def settle(t=6):
    end = time.time() + t
    while time.time() < end:
        time.sleep(0.05)
        if not br.q and br.current is None and br.outbox.empty(): break
    time.sleep(0.35)
def sent_to(n): return [m for d, m in br.iface.sent if d == n]
def rows(n, **kw): return [r for r in br.audit.conversation(n) if all(r[k] == v for k, v in kw.items())]
def last_row(n): return br.audit.conversation(n)[-1]
def tools_in(i): return [t["function"]["name"] for t in seen[i].get("tools", [])] if "tools" in seen[i] else None

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)

def pin(n, tier):
    br.set_node_access(n, pin_key=KEYS[n], max_tier=tier)

# ---- node with nothing enabled: no tools offered, hostile tool call ignored ----------------------
dm(N0, "/ai sneaky recent please"); settle()
check("no actions enabled: tools not offered to the model", tools_in(-1) is None, tools_in(-1))
check("no actions enabled: a tool call the model makes anyway is ignored",
      last_row(N0)["status"] == "answered" and last_row(N0)["action"] is None
      and sent_to(N0) == [b.BLANK_REPLY_ANSWER] and last_row(N0)["response"] == b.BLANK_REPLY_ANSWER, (sent_to(N0), last_row(N0)))   # nothing ran; the asker is told rather than left waiting
check("no actions enabled: system prompt denies tool access", "cannot run commands" in seen[-1]["messages"][0]["content"])

# ---- verified node, read-only tier -------------------------------------------------------------
pin(P, 0)
dm(P, "/ai who was heard recently?"); settle()
check("verified node is offered only read-only tools", tools_in(-1) == ["mesh_summary", "list_nodes", "mesh_report", "node_history", "node_info"], tools_in(-1))
r = last_row(P)
check("read-only action runs and the result is what the radio sends", r["status"] == "action_ok" and r["action"] == "list_nodes" and sent_to(P)[-1] != "plain answer", (r["status"], sent_to(P)[-1:]))
check("audit records how the sender was verified", "key verified" in r["auth"] and "PKI-encrypted" in r["auth"], r["auth"])
check("action result not counted as remembered conversation", br.audit.history(P, 6, 86400, 3000) == [])

dm(P, "/ai who was heard recently?", pki=False); settle()
check("same node without PKI: no tools", tools_in(-1) is None and last_row(P)["status"] == "answered", last_row(P))
check("...and the reason is recorded", "actions off: message was not PKI-encrypted" in last_row(P)["auth"], last_row(P)["auth"])

dm(P, "/ai recent", pubkey="SOMEONE-ELSE="); settle()
check("packet key differing from pinned key: no tools", tools_in(-1) is None and "packet key differs" in last_row(P)["auth"], last_row(P)["auth"])

br.iface.nodes[P]["user"]["publicKey"] = "ROTATED-KEY="
dm(P, "/ai recent"); settle()
check("radio's stored key changed since pinning: no tools", tools_in(-1) is None and "radio's key" in last_row(P)["auth"], last_row(P)["auth"])
br.iface.nodes[P]["user"]["publicKey"] = KEYS[P]

br.set_node_access(R, max_tier=0)   # enabled but never pinned
dm(R, "/ai recent"); settle()
check("actions enabled but no pinned key: no tools", tools_in(-1) is None and "no pinned key" in last_row(R)["auth"], last_row(R)["auth"])

# ---- the code, not the model, decides ---------------------------------------------------------------
dm(P, "/ai badsort"); settle()
check("invalid parameter value refused", last_row(P)["status"] == "action_denied" and "valid sort" in sent_to(P)[-1], sent_to(P)[-1])
dm(P, "/ai deleteall"); settle()
check("hallucinated tool name refused", last_row(P)["status"] == "action_denied" and "isn't something I can do" in sent_to(P)[-1], sent_to(P)[-1])
dm(P, "/ai stringargs"); settle()
check("arguments given as a JSON string are accepted (after validation)", last_row(P)["status"] == "action_ok" and last_row(P)["action"] == "list_nodes", sent_to(P)[-1])
dm(P, "/ai demo"); settle()
check("action above the node's tier refused", last_row(P)["status"] == "action_denied" and "aren't authorised" in sent_to(P)[-1], sent_to(P)[-1])
check("undeclared parameters are dropped, not passed on", actions.validate("list_nodes", {"sort": "RECENT", "evil": "x"}, 0)[1] == {"sort": "recent"})
for bad in [("mesh_summary_x", {}), (None, {}), (["x"], {})]:
    try: actions.validate(bad[0], bad[1], 1); ok = False
    except actions.ActionError: ok = True
    check(f"validate rejects {bad[0]!r}", ok)

# ---- confirmation flow (tier 1) ----------------------------------------------------------------------
pin(Q, 1)
dm(Q, "/ai run the confirmation demo"); settle()
reply = sent_to(Q)[-1]
m = re.search(r"confirm (\d{6})", reply)
check("tier-1 action waits for a code instead of running", m and last_row(Q)["status"] == "action_pending" and "demo_confirm" in reply, reply)
code = m.group(1) if m else "000000"
check("tier-1 node is offered the confirmed action too", "demo_confirm" in (tools_in(-1) or []))
check("the live code is not stored in the audit log", code not in json.dumps(br.audit.conversation(Q)), br.audit.conversation(Q)[-1])
pending_rid = last_row(Q)["id"]

dm(Q, "/ai confirm 000000"); settle()
check("wrong code rejected and counted", "Wrong code (1/3)" in sent_to(Q)[-1] and Q in br.pending)
dm(Q, f"/ai confirm {code}", pki=False); settle()
check("right code without PKI refused, confirmation stays pending", "Can't confirm" in sent_to(Q)[-1] and Q in br.pending, sent_to(Q)[-1])
dm(P, f"/ai confirm {code}"); settle()
check("another node cannot use someone else's code", "Nothing is waiting" in sent_to(P)[-1] and Q in br.pending, sent_to(P)[-1])
dm(Q, f"/ai confirm {code}"); settle()
check("correct code from the verified sender runs the action", sent_to(Q)[-1] == "Demo action confirmed. Nothing was changed.", sent_to(Q)[-1])
check("original request marked done; confirm row logged with redacted code",
      next(r for r in br.audit.conversation(Q) if r["id"] == pending_rid)["status"] == "action_ok" and last_row(Q)["status"] == "action_ok" and code not in last_row(Q)["prompt"] and "••••••" in last_row(Q)["prompt"], (next(r for r in br.audit.conversation(Q) if r["id"] == pending_rid)["status"], last_row(Q)["prompt"], last_row(Q)["status"], last_row(Q)["response"], last_row(Q)["id"]))
dm(Q, f"/ai confirm {code}"); settle()
check("a used code cannot be replayed", "Nothing is waiting" in sent_to(Q)[-1])

dm(Q, "/ai demo again"); settle()
code2 = re.search(r"confirm (\d{6})", sent_to(Q)[-1]).group(1)
br.pending[Q].expires = time.time() - 1
dm(Q, f"/ai confirm {code2}"); settle()
check("expired code is refused and the request marked expired", "Nothing is waiting" in sent_to(Q)[-1] and rows(Q, status="expired"), sent_to(Q)[-1])

dm(Q, "/ai demo third"); settle()
for i in range(3): dm(Q, "/ai confirm 111111"); settle()
check("three wrong codes cancel the request", "three times" in sent_to(Q)[-1] and Q not in br.pending and rows(Q, status="cancelled"), sent_to(Q)[-1])

dm(Q, "/ai demo fourth"); settle()
dm(Q, "/ai cancel"); settle()
check("/ai cancel drops a pending confirmation", sent_to(Q)[-1] == "Cancelled." and Q not in br.pending)
check("no typed code ever reached the audit prompts", not any(("111111" in (r["prompt"] or "")) or ("000000" in (r["prompt"] or "")) for r in br.audit.conversation(Q)))

# ---- /ai actions, inbound labelling ---------------------------------------------------------------------------
dm(Q, "/ai actions"); settle()
check("/ai actions lists what the node may run (no demo entry)", "list_nodes" in sent_to(Q)[-1] and "demo_confirm" not in sent_to(Q)[-1], sent_to(Q)[-1])
dm(N0, "/ai actions"); settle()
check("/ai actions for an unauthorised node says so", "aren't enabled" in sent_to(N0)[-1], sent_to(N0)[-1])
dm(N0, "hello plain dm", pki=False); settle()
check("inbound DMs record whether they were PKI-encrypted", last_row(N0)["kind"] == "inbound" and last_row(N0)["auth"] == "not PKI-encrypted", last_row(N0))

# ---- built-in handlers really work -----------------------------------------------------------------------------------
for name in actions.ACTIONS:
    try:
        out = actions.run(br, actions.ACTIONS[name], {})
        check(f"handler {name} returns short text", isinstance(out, str) and 0 < len(out) <= actions.MAX_RESULT_CHARS, out)
    except Exception as e:
        check(f"handler {name} returns short text", False, repr(e))
print("   sample:", actions.run(br, actions.ACTIONS["mesh_summary"], {}))

# ---- restart safety ----------------------------------------------------------------------------------------------------------
rid = br.audit.new_request(Q, None, "confirm ••••••", status="action_running")
check("an interrupted action row is not re-asked after a restart", rid not in [r["id"] for r in br.audit.pending_queue(600)])

# ---- web API: pinning ----------------------------------------------------------------------------------------------------------
URL = "http://127.0.0.1:8095"; H = {"Content-Type": "application/json"}
post = lambda p, obj: rq.post(URL + p, headers=H, data=json.dumps(obj))
ov = {n["node_id"]: n for n in rq.get(URL + "/api/access").json()["nodes"]}
check("overview reports key state + tier", ov[P]["key_state"] == "pinned" and ov[P]["max_tier"] == 0 and ov[N0]["key_state"] == "unpinned" and ov[NEW]["key_state"] == "none", {k: v["key_state"] for k, v in ov.items()})
check("pin with a stale/wrong key refused", post("/api/access/node", {"node": N0, "pin_key": "NOT-THE-KEY="}).status_code == 400)
check("pin a node the radio has no key for refused", post("/api/access/node", {"node": NEW, "pin_key": "anything"}).status_code == 400)
check("pin with the current key accepted", post("/api/access/node", {"node": N0, "pin_key": KEYS[N0]}).status_code == 200 and br.audit.get_node(N0)["pinned_key"] == KEYS[N0])
check("bad action levels rejected", all(post("/api/access/node", {"node": N0, "max_tier": v}).status_code == 400 for v in (5, -1, True, "1", 1.5)))
check("good action level accepted", post("/api/access/node", {"node": N0, "max_tier": 1}).status_code == 200 and br.audit.get_node(N0)["max_tier"] == 1)
br.iface.nodes[N0]["user"]["publicKey"] = "ROTATED="
check("key rotation after pinning is flagged", {n["node_id"]: n for n in rq.get(URL + "/api/access").json()["nodes"]}[N0]["key_state"] == "changed")
br.iface.nodes[N0]["user"]["publicKey"] = KEYS[N0]
check("unpin works", post("/api/access/node", {"node": N0, "pin_key": None}).status_code == 200 and br.audit.get_node(N0)["pinned_key"] is None)
check("clearing every setting removes the row", post("/api/access/node", {"node": N0, "max_tier": None}).status_code == 200 and N0 not in br.audit.access_rows())
check("cross-origin pin blocked", rq.post(URL + "/api/access/node", headers={**H, "Origin": "http://evil.example"}, data=json.dumps({"node": N0, "pin_key": KEYS[N0]})).status_code == 403)
check("/api/actions lists the menu", {a["name"] for a in rq.get(URL + "/api/actions").json()} == set(actions.ACTIONS))

# ---- migration of an old database ---------------------------------------------------------------------------------------------------
old = os.path.join(HERE, "act_old.db")
if os.path.exists(old): os.remove(old)
c = sqlite3.connect(old)
c.executescript("""CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, node_id TEXT NOT NULL,
 node_name TEXT, prompt TEXT, response TEXT, status TEXT NOT NULL, model TEXT, llm_ms INTEGER, chunks INTEGER DEFAULT 0,
 delivered INTEGER DEFAULT 0, relayed INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, rx_snr REAL, rx_rssi INTEGER, hops INTEGER);
 CREATE TABLE node_access (node_id TEXT PRIMARY KEY, access TEXT NOT NULL DEFAULT 'default', daily_cap INTEGER);
 INSERT INTO node_access VALUES ('!bbbb0001', 'block', 5);
 INSERT INTO requests (ts,node_id,prompt,status) VALUES (1,'!bbbb0001','hi','answered');""")
c.commit(); c.close()
a2 = Audit(old)
n = a2.get_node("!bbbb0001")
check("old node_access row survives and gains new columns", n["access"] == "block" and n["daily_cap"] == 5 and n["pinned_key"] is None and n["max_tier"] is None, n)
check("old requests table gains action/auth/kind", a2.list()[0]["kind"] == "ai" and "action" in a2.list()[0] and "auth" in a2.list()[0])
a2.db.close(); os.remove(old)

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)

