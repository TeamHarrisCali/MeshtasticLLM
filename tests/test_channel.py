"""Public channel: hearing, posting, limits, status, API, and -- above all -- that the AI stays out of it."""
import argparse, json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import tempfile; HERE = tempfile.mkdtemp(prefix="meshtest_")   # scratch databases and caches go in a temp folder, never in the project
DB = os.path.join(HERE, "channel_test.db")
for ext in ("", "-wal", "-shm"):
    try: os.remove(DB + ext)
    except OSError: pass

seen = []                       # every body the fake model receives
class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"]))); seen.append(body)
        out = json.dumps({"message": {"content": "plain answer"}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def do_GET(self):
        out = json.dumps({"models": [{"name": "fake"}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", 11497), Fake).serve_forever, daemon=True).start()

from meshllm import bridge as b, webui, channel as C, actions
import requests as rq

args = argparse.Namespace(db=DB, port="STUB", model="fake", command="/ai", ollama_url="http://127.0.0.1:11497", max_tokens=50, num_ctx=4096, max_chunks=4, chunk_delay=0,
    cooldown=0, timeout=10, memory_turns=6, memory_hours=24, memory_chars=3000, no_log_inbound=False, web_host="127.0.0.1", web_port=8097, no_web=False, max_queue=3,
    queue_ttl=600, no_queue_notice=False, access_mode=None, daily_cap=None, confirm_seconds=60, no_tool_gate=True, chunk_bytes=160, send_retries=2, retry_delay=0.05,
    traceroute_timeout=0.2, reconnect_hold=0.4, channel_gap=30, channel_per_hour=5)

class Ch:                       # the radio's channel list entry
    def __init__(self, index, name, psk): self.index = index; self.settings = type("S", (), {"name": name, "psk": psk})()
class Stub:
    stream = object(); _rxThread = threading.current_thread()
    class myInfo: my_node_num = 1
    def __init__(self):
        self.sent = []; self.handlers = []; self.boom = False
        self.nodes = {"!00000001": {"num": 1, "user": {"id": "!00000001", "longName": "Us"}},
                      "!0000aaaa": {"num": 0xaaaa, "user": {"id": "!0000aaaa", "longName": "Lodge"}}}
        self.nodesByNum = {n["num"]: n for n in self.nodes.values()}
        self.localNode = type("N", (), {"channels": [Ch(0, "", b"\x01"), Ch(1, "Private", b"x" * 32)]})()
    def getMyUser(self): return {"id": "!00000001", "longName": "Test", "shortName": "T", "hwModel": "STUB"}
    def sendText(self, text, destinationId="^all", wantAck=False, wantResponse=False, onResponse=None, channelIndex=0, **kw):
        if self.boom: raise OSError("radio vanished")
        self.sent.append(dict(text=text, dest=destinationId, ack=wantAck, ch=channelIndex)); self.handlers.append(onResponse)

br = b.Bridge(args); radio = Stub(); br.iface = radio; br.models.thinks = lambda name: False
threading.Thread(target=br.worker, daemon=True).start(); threading.Thread(target=br.sender_loop, daemon=True).start(); webui.start(br); time.sleep(0.3)
base = "http://127.0.0.1:8097"
post = lambda p, body: rq.post(base + p, json=body, timeout=5)
fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)
def rows(): return br.channel.list(300)
def pass_gap():
    """Let the minimum gap between posts 'elapse' by ageing the recorded post times (still well inside the hour, so the hourly limit is
    unaffected). A fixed sleep would be a race against a slow machine; this is exact."""
    br.channel._posts[:] = [t - 100 for t in br.channel._posts]
def wait(cond, t=3):
    end = time.time() + t
    while time.time() < end:
        if cond(): return True
        time.sleep(0.02)
    return cond()
def heard(text, frm=0xaaaa, to=0xFFFFFFFF, channel=0, pid=None, **extra):
    p = {"from": frm, "fromId": "!%08x" % frm, "to": to, "id": pid, "channel": channel, "decoded": {"text": text}, **extra}
    if channel == 0: p.pop("channel")                           # the library leaves out a zero
    br.on_receive(p, radio); br.channel.on_text(p, radio)     # exactly the two handlers the real radio feeds
    return p
n_requests = lambda: br.audit.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]

# ---- hearing the channel
heard("hello from the lodge", pid=11, rxSnr=6.5, rxRssi=-90, hopStart=3, hopLimit=2)
m = rows()
check("a broadcast on the primary channel is stored with who, signal and hops", len(m) == 1 and m[0]["text"] == "hello from the lodge" and m[0]["node_id"] == "!0000aaaa" and m[0]["node_name"] == "Lodge"
      and m[0]["direction"] == "in" and m[0]["rx_snr"] == 6.5 and m[0]["hops"] == 1, m)
heard("same packet again", pid=11)
check("the same packet heard twice is stored once", len(rows()) == 1, rows())
heard("a direct message to us", to=1, pid=12)
heard("on the private channel", channel=1, pid=13)
check("DMs and other channels are not stored on the channel page", len(rows()) == 1, rows())
check("...and the DM is still handled as a DM (kept for the conversation view)", n_requests() == 1, n_requests())
heard("my own echo", frm=1, pid=14)
check("our own echoed post is ignored", len(rows()) == 1)
heard("", pid=15); heard("x", frm=0xaaaa, pid=16, rxSnr="bad", rxRssi=[1], hopStart=2.5, hopLimit=[3])
check("empty and malformed packets don't crash, bad fields become blank", len(rows()) == 2 and rows()[1]["rx_snr"] is None and rows()[1]["hops"] is None, rows())
br.channel.on_text({"decoded": None, "fromId": "!0000aaaa"}, radio); br.channel.on_text({"decoded": {"text": 5}, "fromId": "!0000aaaa", "to": 0xFFFFFFFF}, radio); br.channel.on_text({}, None)
check("garbage packets are survived", len(rows()) == 2)

# ---- the AI stays out
before_req, before_q, before_seen = n_requests(), len(br.q), len(seen)
for i, t in enumerate(["/ai what is the uptime", "/AI help", "/ai ping", "/ai status", "/ai reset", "/ai confirm 123456"]):
    heard(t, pid=100 + i)
time.sleep(0.6)
check("'/ai ...' typed on the channel creates no request, no queue entry, no model call", n_requests() == before_req and len(br.q) == before_q and len(seen) == before_seen, (n_requests(), len(seen)))
check("...and nothing at all was transmitted in answer", radio.sent == [], radio.sent)
check("...the text is only kept as a channel message", len([r for r in rows() if r["text"].startswith("/")]) >= 5)
names = [a.name for a in actions.ACTIONS.values()] if hasattr(actions, "ACTIONS") else []
check("no AI tool can post to or read the channel", names and not any("channel" in n or "post" in n or "broadcast" in n or "send" in n for n in names), names)
r = post("/api/ai/ask", {"prompt": "what did people just say on the public channel? anything about the lodge?"}); rid = r.json()["id"]
wait(lambda: rq.get(base + "/api/conversation?node=web-console", timeout=5).json()["messages"][-1]["status"] != "queued", 6)
blob = json.dumps(seen[before_seen:])
check("a question to the AI never contains channel text", "hello from the lodge" not in blob and "what is the uptime" not in blob and len(seen) > before_seen, blob[:300])
check("the live context given to the AI has no channel text", "hello from the lodge" not in str(br.live_context()) and "hello from the lodge" not in b.SAMPLE_CONTEXT)
check("the web chat reply was not posted to the channel", radio.sent == [])
check("the AI log never lists channel messages", "hello from the lodge" not in json.dumps(br.audit.list(limit=100)))

# ---- text checks
for bad, why in [("", "empty"), ("   \n ", "blank"), (None, "missing"), (5, "number"), ("x" * 201, "201 bytes"), ("é" * 101, "101 two-byte chars = 202 bytes"), ("\x00\x07", "only control characters")]:
    try: C.clean(bad); ok = False
    except C.ChannelError: ok = True
    check(f"rejected: {why}", ok)
check("200 bytes exactly is accepted", C.clean("x" * 200) == "x" * 200 and C.clean("é" * 100) == "é" * 100)
check("control characters are stripped, newlines kept", C.clean("a\x00b\x07c\r\nd") == "abc\nd")
try: C.clean("x" * 250)
except C.ChannelError as e: check("...too-long error states the size and that nothing is split", "250 of 200" in str(e) and "never split" in str(e), str(e))

# ---- posting
r = post("/api/channel/post", {"text": "  Anyone on the ridge?  "})
check("posting works", r.status_code == 200 and "id" in r.json(), r.text)
check("it goes out as ONE broadcast on channel 0, to everyone", wait(lambda: len(radio.sent) == 1) and radio.sent[0] == dict(text="Anyone on the ridge?", dest="^all", ack=True, ch=0), radio.sent)
check("the stored row is 'you', and marked sent", wait(lambda: rows()[-1]["status"] == "sent") and rows()[-1]["direction"] == "out" and rows()[-1]["node_name"] == "You", rows()[-1])
rid = rows()[-1]["id"]
radio.handlers[0]({"decoded": {"routing": {"errorReason": "NONE"}}})
check("a neighbour repeating it marks it 'heard'", rows()[-1]["status"] == "heard", rows()[-1])
br.channel.set_status(rid, "sent")
check("...and it never steps back to 'sent'", rows()[-1]["status"] == "heard")
r = post("/api/channel/post", {"text": "too soon"})
check("a second post straight away is refused politely", r.status_code == 400 and "between posts" in r.json()["error"], r.text)
check("...and nothing more was sent", len(radio.sent) == 1)
pass_gap()
post("/api/channel/post", {"text": "second"}); wait(lambda: len(radio.sent) == 2)
radio.handlers[1]({"decoded": {"routing": {"errorReason": "MAX_RETRANSMIT"}}})
check("a failed broadcast is marked failed", rows()[-1]["status"] == "failed", rows()[-1])
pass_gap(); radio.boom = True
post("/api/channel/post", {"text": "third"})
check("a radio error marks the post failed", wait(lambda: rows()[-1]["text"] == "third" and rows()[-1]["status"] == "failed"), rows()[-1])
radio.boom = False
for i in range(3):
    pass_gap(); post("/api/channel/post", {"text": f"msg {i}"})
pass_gap()
r = post("/api/channel/post", {"text": "one too many"})
check("the hourly limit stops a flood", r.status_code == 400 and "in the last hour" in r.json()["error"], r.text)
check("bodies of the wrong shape are refused, not crashed on", post("/api/channel/post", {"text": ["a"]}).status_code == 400 and post("/api/channel/post", {}).status_code == 400 and post("/api/channel/post", {"text": "x" * 5000}).status_code == 400)
check("an invalid JSON list body is refused", rq.post(base + "/api/channel/post", data="[1]", headers={"Content-Type": "application/json"}, timeout=5).status_code == 400)

# ---- radio away
br.channel._posts.clear(); br.iface = None
r = post("/api/channel/post", {"text": "nobody home"})
check("no radio: refused up front", r.status_code == 400 and "No radio" in r.json()["error"], r.text)
br.iface = radio
br.channel._posts.clear()
br.iface = radio; rid = br.channel.post("queued then radio lost"); br.iface = None; br.down_since = time.time() - 60
check("radio lost while queued: the post is marked failed, not sent", wait(lambda: [r for r in rows() if r["id"] == rid][0]["status"] == "failed", 4))
br.iface = radio

# ---- the API and housekeeping
d = rq.get(base + "/api/channel", timeout=5).json()
check("GET /api/channel gives info and messages", d["info"]["connected"] and d["info"]["known"] and d["info"]["default_key"] is True and d["info"]["max_bytes"] == 200 and d["messages"], d["info"])
check("the default public key is recognised", d["info"]["name"] and "LongFast" in d["info"]["name"], d["info"])
radio.localNode.channels[0] = Ch(0, "Crew", b"k" * 32)
check("a private primary channel is flagged as not the default key", rq.get(base + "/api/channel", timeout=5).json()["info"]["default_key"] is False)
radio.localNode.channels[0] = Ch(0, "", b"\x01")
last = d["messages"][-1]["id"]
check("'after' returns only newer rows", rq.get(base + f"/api/channel?after={last}", timeout=5).json()["messages"] == [] and len(rq.get(base + f"/api/channel?after={last - 2}", timeout=5).json()["messages"]) == 2)
check("hostile query values are harmless", rq.get(base + "/api/channel?limit=abc&after=x", timeout=5).status_code == 200 and rq.get(base + "/api/channel?limit=-5", timeout=5).status_code == 200)
check("a foreign Host header is refused", rq.get(base + "/api/channel", headers={"Host": "evil.example"}, timeout=5).status_code == 403)
check("a foreign Origin on post is refused", rq.post(base + "/api/channel/post", json={"text": "x"}, headers={"Origin": "http://evil.example"}, timeout=5).status_code == 403)
ov = {x["name"]: x for x in br.mesh.data_overview()["datasets"]}
check("the Data page lists the channel table", "channel_messages" in ov and ov["channel_messages"]["rows"] >= 5 and "text" in ov["channel_messages"]["what"].lower(), ov.get("channel_messages"))
check("the channel table can't be exported as CSV by name", br.mesh.export_csv("channel_messages") is None)
br.audit.db.execute("INSERT INTO channel_messages (ts, direction, node_id, text) VALUES (?, 'in', '!0000aaaa', 'ancient')", (time.time() - 40 * 86400,)); br.audit.db.commit()
br.mesh.prune()
check("old channel messages are pruned with the other data (30 days)", "ancient" not in [r["text"] for r in rows()] and len(rows()) > 3)
n = post("/api/channel/clear", {}).json()["deleted"]
check("clearing deletes every saved channel message", n > 3 and rows() == [] and rq.get(base + "/api/channel", timeout=5).json()["messages"] == [])
check("the page exists in the dashboard", b"viewChannel" in open(os.path.join(ROOT, "meshllm", "static", "index.html"), "rb").read() and b"refreshChannel" in open(os.path.join(ROOT, "meshllm", "static", "js", "channel.js"), "rb").read())

print(f"\n{len(fails)} failed" if fails else "\nall passed")
sys.stdout.flush(); os._exit(1 if fails else 0)
