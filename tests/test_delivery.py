"""Delivery tests: balanced chunks, resend of failed parts, failure notes, migration (no hardware)."""
import argparse, os, sqlite3, sys, threading, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import tempfile; HERE = tempfile.mkdtemp(prefix="meshtest_")   # scratch databases and caches go in a temp folder, never in the project
DB = os.path.join(HERE, "deliv_test.db")
if os.path.exists(DB): os.remove(DB)

from meshllm import bridge as b
from meshllm.audit import Audit

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)

# ---- chunking ---------------------------------------------------------------------------------
REPLY = ('I couldn\'t find any information on "meshtastic". It\'s possible that it\'s a local project or a '
         'new technology that hasn\'t gained much attention yet. Can you please provide more context or '
         'information about what meshtastic is?')                      # the reply that lost a part on the radio
ch = b.chunk_text(REPLY, 160)
sizes = [len(c.encode()) for c in ch]
check("two parts for that reply", len(ch) == 2, sizes)
check("parts are balanced, no tiny tail", max(sizes) - min(sizes) <= 25 and min(sizes) > 60, sizes)
check("no part over the limit", all(s <= 160 for s in sizes), sizes)
check("no words lost or split", " ".join(ch).split() == REPLY.split())
check("short text is one untouched part", b.chunk_text("hello there", 160) == ["hello there"])
long_word = "x" * 500
check("a giant unbroken word is hard-split within the limit", all(len(c.encode()) <= 160 for c in b.chunk_text(long_word, 160)))
uni = "héllo wörld " * 40
check("multi-byte text respects the byte limit", all(len(c.encode()) <= 160 for c in b.chunk_text(uni, 160)))
big = " ".join(f"w{i}" for i in range(400))
check("balanced output never needs more parts than greedy", len(b.chunk_text(big, 160)) == len(b._greedy_chunks(big, 160)))

# ---- a bridge with a scriptable radio --------------------------------------------------------------
args = argparse.Namespace(db=DB, chunk_bytes=160, max_chunks=4, chunk_delay=0, send_retries=2, retry_delay=0.05,
                          access_mode=None, daily_cap=None, confirm_seconds=60)
class Radio:
    stream = object(); _rxThread = threading.current_thread()
    def __init__(self, script): self.script, self.sent, self.count = script, [], {}
    def sendText(self, msg, destinationId, wantAck, onResponse):
        self.sent.append(msg)
        self.count[msg] = self.count.get(msg, 0) + 1
        verdict = self.script(msg, self.count[msg], destinationId)   # "ack" | "relay" | error reason string
        if onResponse is None: return
        def reply():
            if verdict == "ack": onResponse({"fromId": destinationId, "decoded": {"routing": {"errorReason": "NONE"}}})
            elif verdict == "relay": onResponse({"fromId": "!someoneelse", "decoded": {"routing": {"errorReason": "NONE"}}})
            else: onResponse({"fromId": "!00000001", "decoded": {"routing": {"errorReason": verdict}}})
        threading.Timer(0.02, reply).start()

def make(script, **over):
    a = argparse.Namespace(**{**vars(args), **over})
    if os.path.exists(DB):
        try: os.remove(DB)
        except OSError: pass
    br = b.Bridge(a); br.iface = Radio(script)
    threading.Thread(target=br.sender_loop, daemon=True).start()
    return br
def row(br, rid): return next(r for r in br.audit.list(limit=50) if r["id"] == rid)
def wait(br, rid, want=None, t=3.0):
    end = time.time() + t
    while time.time() < end:
        time.sleep(0.05)
        r = row(br, rid)
        if want(r): break
    time.sleep(0.2)
    return row(br, rid)

# 1. first attempt of part 1 is NAKed, the resend works
br = make(lambda msg, n, dest: "MAX_RETRANSMIT" if (msg.startswith("(1/2)") and n == 1) else "ack")
rid = br.audit.new_request("!aaaaaaaa", None, "q", status="answered", response=REPLY)
br.queue_reply(rid, "!aaaaaaaa", REPLY)
r = wait(br, rid, lambda r: r["delivered"] + r["failed"] >= 2)
p1 = next(m for m in br.iface.sent if m.startswith("(1/2)"))
check("failed part is resent exactly once with the same text", br.iface.count[p1] == 2, br.iface.count)
check("parts went out as (1/2), (2/2), then the resend", [m[:5] for m in br.iface.sent] == ["(1/2)", "(2/2)", "(1/2)"], [m[:5] for m in br.iface.sent])
check("after the resend both parts count as delivered, none failed", (r["delivered"], r["failed"]) == (2, 0), r)
check("the radio's failure reason is kept in the audit", "(1/2)" in (r["delivery_note"] or "") and "MAX_RETRANSMIT" in r["delivery_note"] and "resent" in r["delivery_note"], r["delivery_note"])

# 2. part never gets through: gives up after 2 resends and says so
br = make(lambda msg, n, dest: "MAX_RETRANSMIT" if msg.startswith("(1/2)") else "ack")
rid = br.audit.new_request("!aaaaaaaa", None, "q", status="answered", response=REPLY)
br.queue_reply(rid, "!aaaaaaaa", REPLY)
r = wait(br, rid, lambda r: r["failed"] >= 1, t=4)
p1 = next(m for m in br.iface.sent if m.startswith("(1/2)"))
check("gives up after the retry limit (1 try + 2 resends)", br.iface.count[p1] == 3, br.iface.count)
check("permanent loss is recorded as failed, the other part as delivered", (r["delivered"], r["failed"]) == (1, 1), r)
check("note says it gave up", "gave up" in r["delivery_note"], r["delivery_note"])

# 3. retries disabled
br = make(lambda msg, n, dest: "NO_CHANNEL" if msg.startswith("(1/2)") else "ack", send_retries=0)
rid = br.audit.new_request("!aaaaaaaa", None, "q", status="answered", response=REPLY)
br.queue_reply(rid, "!aaaaaaaa", REPLY)
r = wait(br, rid, lambda r: r["failed"] >= 1)
p1 = next(m for m in br.iface.sent if m.startswith("(1/2)"))
check("--send-retries 0 never resends", br.iface.count[p1] == 1 and r["failed"] == 1, (br.iface.count, r["failed"]))

# 4. single short message also retried; untracked courtesy notice never tracked or retried
br = make(lambda msg, n, dest: "TIMEOUT" if (msg == "hello" and n == 1) else "ack")
rid = br.audit.new_request("!aaaaaaaa", None, "q", status="answered", response="hello")
br.queue_reply(rid, "!aaaaaaaa", "hello")
r = wait(br, rid, lambda r: r["delivered"] >= 1)
check("single-part reply is retried too and delivered", br.iface.count["hello"] == 2 and r["delivered"] == 1 and r["failed"] == 0, (br.iface.count, r))
br.outbox.put((None, "!aaaaaaaa", ["Queued (#2, about 40s)."], 0)); time.sleep(0.3)
check("courtesy notice (no request id) is sent once without tracking", br.iface.count["Queued (#2, about 40s)."] == 1)

# 4b. an empty or blank result still gets a reply (it used to send nothing while the log said "answered")
br = make(lambda msg, n, dest: "ack")
for blank in ("", "   \n", None):
    rid = br.audit.new_request("!aaaaaaaa", None, "q", status="answered", response="")
    n_before = len(br.iface.sent)
    br.queue_reply(rid, "!aaaaaaaa", blank)
    r = wait(br, rid, lambda r: r["delivered"] >= 1)
    check(f"a blank reply ({blank!r}) sends exactly the short notice", r["chunks"] == 1 and r["delivered"] == 1 and br.iface.sent[n_before:] == [b.BLANK_REPLY_ANSWER], (r["chunks"], br.iface.sent[n_before:]))
    check(f"...and the log row records what was sent ({blank!r})", r["response"] == b.BLANK_REPLY_ANSWER, r["response"])
rid = br.audit.new_request(b.WEB_SENDER, None, "q", status="answered", response="")
n_before = len(br.iface.sent)
br.queue_reply(rid, b.WEB_SENDER, "")
time.sleep(0.2)
web_row = row(br, rid)
check("a blank answer to the browser chat transmits nothing and its log row is left alone", br.iface.sent[n_before:] == [] and web_row["response"] == "" and web_row["chunks"] == 0, (br.iface.sent[n_before:], web_row["response"], web_row["chunks"]))

# 5. ack semantics unchanged: implicit ack from a neighbour counts as relayed
br = make(lambda msg, n, dest: "relay")
rid = br.audit.new_request("!aaaaaaaa", None, "q", status="answered", response="hi")
br.queue_reply(rid, "!aaaaaaaa", "hi")
r = wait(br, rid, lambda r: r["relayed"] >= 1)
check("implicit ack still counts as relayed", (r["delivered"], r["relayed"], r["failed"]) == (0, 1, 0), r)

# 6. message building / manual messages
br = make(lambda *a: "ack")
msgs = br.make_messages(REPLY, 4)
check("numbered messages carry (i/n) prefixes", [m[:6] for m in msgs] == ["(1/2) ", "(2/2) "], msgs)
check("every message fits chunk bytes + prefix", all(len(m.encode()) <= 160 + 6 for m in msgs))
over = br.make_messages(" ".join(["word"] * 400), 4)
check("over the cap: exactly max parts, filled fully", len(over) == 4 and all(len(m.encode()) > 140 for m in over[:3]), [len(m.encode()) for m in over])
try: br.send_manual("!aaaaaaaa", "word " * 400); ok = False
except ValueError: ok = True
check("manual message over 4 parts rejected", ok)
rid = br.send_manual("!aaaaaaaa", REPLY); time.sleep(0.3)
check("manual 2-part message goes out as two numbered DMs", sum(1 for m in br.iface.sent if m.startswith("(")) == 2)

# 7. migration: database from before delivery_note existed
old = os.path.join(HERE, "deliv_old.db")
if os.path.exists(old): os.remove(old)
c = sqlite3.connect(old)
c.executescript("""CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, node_id TEXT NOT NULL,
 node_name TEXT, prompt TEXT, response TEXT, status TEXT NOT NULL, model TEXT, llm_ms INTEGER, chunks INTEGER DEFAULT 0,
 delivered INTEGER DEFAULT 0, relayed INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, rx_snr REAL, rx_rssi INTEGER, hops INTEGER,
 kind TEXT NOT NULL DEFAULT 'ai', action TEXT, auth TEXT);
 INSERT INTO requests (ts,node_id,prompt,status) VALUES (1,'!bbbb0001','hi','answered');""")
c.commit(); c.close()
a2 = Audit(old); a2.add_note(1, "first"); a2.add_note(1, "second")
check("old database gains delivery_note; notes append", a2.list()[0]["delivery_note"] == "first; second", a2.list()[0])
a2.db.close(); os.remove(old)

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
