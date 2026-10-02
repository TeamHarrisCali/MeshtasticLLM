"""Model selection + download tests against a fake Ollama (no real downloads, no hardware)."""
import argparse, json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import tempfile; HERE = tempfile.mkdtemp(prefix="meshtest_")   # scratch databases and caches go in a temp folder, never in the project
DB = os.path.join(HERE, "models_test.db")
if os.path.exists(DB): os.remove(DB)

def mk(name, caps, size=2_000_000_000, params="3B"):
    return {"name": name, "digest": "sha:" + name, "size": size, "modified_at": "2026-10-01T00:00:00Z",
            "details": {"parameter_size": params, "quantization_level": "Q4_K_M", "family": "x"}}, caps
TAGS = {}
for n, caps in [mk("llama3.2:3b", ["completion", "tools"]), mk("gemma2:2b", ["completion"], params="2B"),
                mk("nomic-embed-text:latest", ["embedding"], params="137M"), mk("qwen2.5:latest", ["completion", "tools"], params="7B"),
                mk("qwen3.5:latest", ["completion", "tools", "thinking"], params="9B"),
                mk("oldthinker:1b", ["completion"], params="1B"), mk("mute:1b", ["completion"], params="1B")]:
    TAGS[n["name"]] = (n, caps)
LOADED, SEEN_CHAT, SEEN_GEN, STATE = [], [], [], {"no_caps": False}

class Fake(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    def log_message(self, *a): pass
    def _json(self, obj, code=200):
        out = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def do_GET(self):
        if self.path == "/api/tags": return self._json({"models": [v[0] for v in TAGS.values()]})
        if self.path == "/api/ps": return self._json({"models": [{"name": n} for n in LOADED]})
        self._json({}, 404)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/show":
            name = body["model"]
            if name not in TAGS: return self._json({"error": "not found"}, 404)
            return self._json({} if STATE["no_caps"] else {"capabilities": TAGS[name][1]})
        if self.path == "/api/generate":
            SEEN_GEN.append(body); return self._json({"done": True})
        if self.path == "/api/chat":
            SEEN_CHAT.append(body)
            m, think = body["model"], body.get("think")
            last = body["messages"][-1]["content"].lower()
            gate_call = body.get("options", {}).get("num_predict") == 4
            if m in ("qwen3.5:latest", "oldthinker:1b") and think is not False:   # reasoning models burn the token budget thinking
                return self._json({"message": {"content": "", "thinking": "Let me think about that..."}, "done_reason": "length"})
            if m == "mute:1b":
                return self._json({"message": {"content": ""}, "done_reason": "stop"})
            if m in ("qwen3.5:latest", "oldthinker:1b") and gate_call:
                return self._json({"message": {"content": "YES" if "nodes" in last else "NO"}})
            if body["model"] == "gemma2:2b" and "tools" in body:
                return self._json({"error": f"registry.ollama.ai/library/{body['model']} does not support tools"}, 400)
            gate = body.get("options", {}).get("num_predict") == 4
            return self._json({"message": {"content": "NO" if gate else f"ok from {body['model']}"}})
        if self.path == "/api/pull": return self._pull(body)
        self._json({}, 404)
    def _line(self, obj): self.wfile.write((json.dumps(obj) + "\n").encode()); self.wfile.flush()
    def _pull(self, body):
        name = body.get("model")
        if not name: return self._json({"error": "model is required"}, 400)
        self.send_response(200); self.end_headers()
        self._line({"status": "pulling manifest"})
        if name == "bad/model":
            return self._line({"error": "pull model manifest: file does not exist"})
        steps = 60 if name.startswith("slow") else 4
        try:
            for i in range(steps + 1):
                time.sleep(0.15 if name.startswith("slow") else 0.12)
                self._line({"status": "pulling aaa", "digest": "sha256:aaa", "total": 1000, "completed": 1000 * i // steps})
                self._line({"status": "pulling bbb", "digest": "sha256:bbb", "total": 500, "completed": 500 * i // steps})
            self._line({"status": "verifying sha256 digest"})
            n, caps = mk(name, ["completion", "tools"], params="1B")
            TAGS[name] = (n, caps)
            self._line({"status": "success"})
        except (BrokenPipeError, ConnectionResetError):
            pass
threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", 11496), Fake).serve_forever, daemon=True).start()

from meshllm import bridge as b, webui
import requests as rq
from meshllm.ollama_models import ModelManager, OllamaError, MODEL_NAME_RE, same_model

def args(**over):
    base = dict(db=DB, ollama_url="http://127.0.0.1:11496", model=None, access_mode=None, daily_cap=None, no_tool_gate=True,
                num_ctx=4096, max_tokens=50, timeout=10, web_host="127.0.0.1", web_port=8094, no_web=True,
                cooldown=0, memory_turns=6, memory_hours=24, max_queue=5, max_chunks=4, command="/ai", port="auto", memory_chars=3000)
    base.update(over); return argparse.Namespace(**base)

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)
def until(cond, t=5.0):
    end = time.time() + t
    while time.time() < end:
        if cond(): return True
        time.sleep(0.03)
    return cond()

# ---- which model is used, and does the choice stick ------------------------------------------------------
br = b.Bridge(args())
check("no flags, nothing saved: falls back to the default model", br.model == b.DEFAULT_MODEL, br.model)
br_flag = b.Bridge(args(model="qwen2.5:latest"))
check("--model overrides and is saved", br_flag.model == "qwen2.5:latest")
br_again = b.Bridge(args())   # a later start with no arguments at all
check("a later start with no arguments remembers it", br_again.model == "qwen2.5:latest", br_again.model)
check("'qwen2.5' (no tag) counts as installed 'qwen2.5:latest'", same_model("qwen2.5", "qwen2.5:latest") and not same_model("qwen2.5:3b", "qwen2.5:latest"))
br_again.audit.set_setting("model", "qwen2.5")
br_again._ollama_cache = (0.0, False)
check("ollama_ok accepts the tagless name", br_again.ollama_ok())
br = b.Bridge(args(model="llama3.2:3b"))
webui.start(br)
URL = "http://127.0.0.1:8094"; H = {"Content-Type": "application/json"}
post = lambda p, o, h=H: rq.post(URL + p, headers=h, data=json.dumps(o))

# ---- listing ------------------------------------------------------------------------------------------------------------------
ov = rq.get(URL + "/api/models").json()
by = {m["name"]: m for m in ov["installed"]}
check("lists every installed model", set(by) == set(TAGS), list(by))
check("reports current model and that it is installed", ov["current"] == "llama3.2:3b" and ov["current_installed"])
check("flags tool support per model", by["llama3.2:3b"]["tools"] and not by["gemma2:2b"]["tools"])
check("flags embedding-only models as unable to chat", by["nomic-embed-text:latest"]["chat"] is False and by["gemma2:2b"]["chat"] is True)
check("size/params/quant are passed through", by["llama3.2:3b"]["size"] == 2_000_000_000 and by["llama3.2:3b"]["params"] == "3B" and by["llama3.2:3b"]["quant"] == "Q4_K_M")
LOADED.append("llama3.2:3b")
check("shows what is loaded in memory", rq.get(URL + "/api/models").json()["loaded"] == ["llama3.2:3b"])
STATE["no_caps"] = True
old = ModelManager("http://127.0.0.1:11496").installed()
check("older Ollama without capability info: assumed able to chat, no tools claimed", all(m["chat"] and not m["tools"] for m in old), old[:1])
STATE["no_caps"] = False

# ---- switching ---------------------------------------------------------------------------------------------------------------------
r = post("/api/model", {"model": "qwen2.5:latest"})
check("switching to an installed model works", r.status_code == 200 and r.json()["tools"] is True and br.model == "qwen2.5:latest", r.text)
check("status reports the new model", br.status()["model"] == "qwen2.5:latest")
check("the new model is preloaded", until(lambda: any(g["model"] == "qwen2.5:latest" and g.get("keep_alive") for g in SEEN_GEN)), SEEN_GEN)
SEEN_CHAT.clear()
text, calls = br.ask_llm("hi", [], -1)
check("questions now go to the new model", SEEN_CHAT and SEEN_CHAT[-1]["model"] == "qwen2.5:latest" and text == "ok from qwen2.5:latest", SEEN_CHAT[-1:])
check("a model that isn't installed is refused", post("/api/model", {"model": "nope:1b"}).status_code == 400)
check("an embedding-only model is refused", post("/api/model", {"model": "nomic-embed-text:latest"}).status_code == 400)
check("empty model name refused", post("/api/model", {"model": ""}).status_code == 400 and post("/api/model", {}).status_code == 400)
check("failed switches leave the model unchanged", br.model == "qwen2.5:latest")
check("switching to the model already in use is harmless", post("/api/model", {"model": "qwen2.5:latest"}).status_code == 200)

# a model without tool support must not break verified nodes
post("/api/model", {"model": "gemma2:2b"}); SEEN_CHAT.clear()
text, calls = br.ask_llm("how many nodes are around?", [], 0)
tried = [("tools" in c) for c in SEEN_CHAT]
check("model without tool support: retried as plain chat instead of failing", tried == [True, False] and text == "ok from gemma2:2b" and calls == [], (tried, text))
check("...and the log warns only once", br._no_tools_warned == {"gemma2:2b"})
post("/api/model", {"model": "llama3.2:3b"})

# ---- reasoning ("thinking") models: the empty-answer bug -------------------------------------------------------------------
post("/api/model", {"model": "qwen3.5:latest"}); SEEN_CHAT.clear()
text, calls = br.ask_llm("hello! How are you?", [], -1)
check("thinking model declared via capabilities: thinking is switched off and it answers", text == "ok from qwen3.5:latest" and SEEN_CHAT[-1].get("think") is False, (text, SEEN_CHAT[-1:]))
check("non-thinking models are not sent a 'think' parameter", (lambda: (post("/api/model", {"model": "llama3.2:3b"}), SEEN_CHAT.clear(), br.ask_llm("hi", [], -1), "think" not in SEEN_CHAT[-1])[-1])())
post("/api/model", {"model": "qwen3.5:latest"}); SEEN_CHAT.clear()
br.args.no_tool_gate = False
text, calls = br.ask_llm("how many nodes are around?", [], 0)
gate_bodies = [c for c in SEEN_CHAT if c.get("options", {}).get("num_predict") == 4]
main_bodies = [c for c in SEEN_CHAT if c.get("options", {}).get("num_predict") != 4]
check("the tool gate also turns thinking off (else it would silently say NO and disable AI tools)",
      gate_bodies and gate_bodies[0].get("think") is False and main_bodies and "tools" in main_bodies[0], (gate_bodies, [("tools" in m) for m in main_bodies]))
br.args.no_tool_gate = True
post("/api/model", {"model": "oldthinker:1b"}); SEEN_CHAT.clear()
text, calls = br.ask_llm("hi there", [], -1)
check("a thinking model that doesn't declare it: empty answer triggers one retry with thinking off",
      text == "ok from oldthinker:1b" and [c.get("think") for c in SEEN_CHAT] == [None, False], [c.get("think") for c in SEEN_CHAT])
post("/api/model", {"model": "mute:1b"}); SEEN_CHAT.clear()
try: br.ask_llm("hi there", [], -1); raised = None
except b.EmptyAnswer as e: raised = str(e)
check("a model that returns nothing raises a clear error instead of sending '(empty response)'", raised and "mute:1b" in raised and "done_reason=stop" in raised, raised)
check("...after exactly one retry", [c.get("think") for c in SEEN_CHAT] == [None, False], [c.get("think") for c in SEEN_CHAT])
# and through the real worker: the sender gets a sensible message, the audit records an error
rid = br.audit.new_request("!aaaa0001", None, "say something", status="queued")
br.enqueue(rid, "!aaaa0001", "say something")
class _Radio:
    sent = []
    stream = object(); _rxThread = threading.current_thread()
    def sendText(self, msg, destinationId, wantAck, onResponse): self.sent.append(msg)
br.iface = _Radio(); br.args.chunk_bytes = 160; br.args.send_retries = 0; br.args.retry_delay = 0; br.args.chunk_delay = 0; br.args.reconnect_hold = 1
threading.Thread(target=br.worker, daemon=True).start(); threading.Thread(target=br.sender_loop, daemon=True).start()
check("worker turns an empty answer into a user-facing message and an audit error",
      until(lambda: _Radio.sent and "no answer" in _Radio.sent[-1]) and next(x for x in br.audit.list(limit=20) if x["id"] == rid)["status"] == "llm_error", _Radio.sent)
post("/api/model", {"model": "llama3.2:3b"})
check("Model tab data exposes the thinking capability", "thinking" in next(m for m in rq.get(URL + "/api/models").json()["installed"] if m["name"] == "qwen3.5:latest")["capabilities"])

# ---- downloads -------------------------------------------------------------------------------------------------------------------------
for bad in ["", "a b", "../etc/passwd", "x;rm -rf /", "a" * 200, "model:", "-bad", "a\nb"]:
    if post("/api/models/pull", {"name": bad}).status_code != 400: check(f"invalid name rejected: {bad!r}", False); break
else:
    check("invalid model names are rejected (spaces, traversal, shell characters, too long)", True)
check("valid names pass the pattern", all(MODEL_NAME_RE.match(n) for n in ["qwen2.5:7b", "library/llama3.2:3b", "hf.co/user/repo:Q4_K_M", "phi3"]))

seen_mid = []
check("starting a download is accepted", post("/api/models/pull", {"name": "tiny:1b"}).status_code == 200)
def prog():
    p = rq.get(URL + "/api/models").json()["pulls"]
    if p and 0 < p[0]["completed"] < p[0]["total"]: seen_mid.append(p[0]["completed"] / p[0]["total"])
    return p and p[0]["done"]
check("progress is reported and ends in success", until(prog, 8) and seen_mid, seen_mid)
p = rq.get(URL + "/api/models").json()["pulls"][0]
check("finished download: 100%, not active, no error", p["completed"] == p["total"] == 1500 and not p["active"] and p["error"] is None and p["status"] == "success", p)
check("the new model is now installed and selectable", any(m["name"] == "tiny:1b" for m in rq.get(URL + "/api/models").json()["installed"]) and post("/api/model", {"model": "tiny:1b"}).status_code == 200)
post("/api/model", {"model": "llama3.2:3b"})

check("unknown model: Ollama's error is shown", post("/api/models/pull", {"name": "bad/model"}).status_code == 200 and until(lambda: rq.get(URL + "/api/models").json()["pulls"][0]["status"] == "failed"))
check("...with its message", "does not exist" in rq.get(URL + "/api/models").json()["pulls"][0]["error"])

check("slow download starts", post("/api/models/pull", {"name": "slow:1b"}).status_code == 200)
until(lambda: rq.get(URL + "/api/models").json()["pulls"][0]["completed"] > 0)
busy = post("/api/models/pull", {"name": "other:1b"})
check("a second download while one runs is refused", busy.status_code == 400 and "already running" in busy.json()["error"], busy.text)
check("cancel accepted", post("/api/models/pull/cancel", {"name": "slow:1b"}).status_code == 200)
check("cancelled download stops and is not reported as failed", until(lambda: rq.get(URL + "/api/models").json()["pulls"][0]["status"] == "cancelled") and not rq.get(URL + "/api/models").json()["pulls"][0]["active"])
check("cancelled model was not added", "slow:1b" not in {m["name"] for m in rq.get(URL + "/api/models").json()["installed"]})
check("cancelling something that isn't running is a clear error", post("/api/models/pull/cancel", {"name": "slow:1b"}).status_code == 400)
check("after a cancel, a new download can start", post("/api/models/pull", {"name": "tiny2:1b"}).status_code == 200 and until(lambda: rq.get(URL + "/api/models").json()["pulls"][0]["done"], 8))

# ---- Ollama not running ------------------------------------------------------------------------------------------------------------------
dead = b.Bridge(args(ollama_url="http://127.0.0.1:9", db=os.path.join(HERE, "models_dead.db")))
ovd = dead.models_overview()
check("Ollama down: overview explains instead of crashing", ovd["installed"] == [] and "Can't reach Ollama" in ovd["error"] and not ovd["current_installed"], ovd["error"])
try: dead.set_model("llama3.2:3b"); ok = False
except OllamaError: ok = True
check("Ollama down: switching model gives a clear error", ok)
dead.models.start_pull("whatever:1b")
check("Ollama down: a download fails cleanly", until(lambda: dead.models.pulls()[0]["status"] == "failed") and dead.models.pulls()[0]["error"], dead.models.pulls())
check("Ollama down: status says the model isn't available", dead.ollama_ok() is False)

# ---- web safety --------------------------------------------------------------------------------------------------------------------------------
check("cross-origin model change blocked", post("/api/model", {"model": "llama3.2:3b"}, {**H, "Origin": "http://evil.example"}).status_code == 403)
check("cross-origin download blocked", post("/api/models/pull", {"name": "x:1b"}, {**H, "Origin": "http://evil.example"}).status_code == 403)
check("non-JSON model change blocked", rq.post(URL + "/api/model", headers={"Content-Type": "text/plain"}, data="x").status_code == 415)

print("\n%d failure(s)" % len(fails))
for f in (DB, os.path.join(HERE, "models_dead.db")):
    try: os.remove(f)
    except OSError: pass
sys.exit(1 if fails else 0)
