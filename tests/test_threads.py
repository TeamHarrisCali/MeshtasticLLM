"""Thread safety of the pending-confirmation table and the duplicate-packet filter (no hardware, no Ollama).

Confirmations are created on the worker thread, confirmed on the radio's receive thread and swept by whichever thread asks for the status
(the dashboard polls it every few seconds), so the table is shared. These tests hammer those paths from several threads with the
interpreter forced to switch threads very often, which is what makes an unlocked read-modify-write show up.
"""
import itertools, os, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixture import make

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)

from meshllm.bridge import Job, Pending
from meshllm import actions

br, radio, tmp = make(confirm_seconds=0)          # a confirmation expires the moment it is created, so sweeps always have work to do
demo = {"function": {"name": "demo_confirm", "arguments": {}}}
META = lambda s: dict(node_id=s, node_name=None, auth="key verified", rx_snr=None, rx_rssi=None, hops=None)   # what on_receive passes along
SENDERS = ["!%08x" % (0x1000 + i) for i in range(40)] + ["!0000aaaa"]   # the last one is also the node that sends "/ai cancel" below

old_interval = sys.getswitchinterval()
sys.setswitchinterval(1e-6)                        # switch threads constantly so a race that needs bad luck happens within the test
errors, stop = [], threading.Event()

def guarded(fn):
    def run():
        while not stop.is_set():
            try:
                fn()
            except Exception as e:                 # any exception out of these paths is the bug being tested for
                errors.append(f"{fn.__name__}: {type(e).__name__}: {e}")
                return
    return run

def worker_parks():                                # what the worker thread does when the model asks for a confirmed action
    for s in SENDERS:
        rid = br.audit.new_request(s, None, "run the demo", status="queued")
        br.handle_tool_call(Job(rid, s, "run the demo", time.time(), 1), demo, 5)

def sweeper():                                     # what status() (dashboard thread) and each incoming command do
    br.sweep_pending()

pids = itertools.count(10_000)
def canceller():                                   # a real "/ai cancel" DM on the radio thread: it removes entries from the table too
    br.on_receive({"id": next(pids), "from": 0xaaaa, "fromId": "!0000aaaa", "to": 1, "decoded": {"text": "/ai cancel"}}, radio)

def confirmer():                                   # the radio thread answering a confirmation that may have just expired or been replaced
    for s in SENDERS:
        replies = []
        br.handle_confirm(s, "000000", 1, "key verified", lambda status, text: replies.append(text), META(s), "confirm ••••••")

threads = [threading.Thread(target=guarded(worker_parks)), threading.Thread(target=guarded(confirmer)), threading.Thread(target=guarded(canceller))] + \
          [threading.Thread(target=guarded(sweeper)) for _ in range(3)]
for t in threads: t.start()
time.sleep(4)
stop.set()
for t in threads: t.join(timeout=10)
sys.setswitchinterval(old_interval)
check("parking, confirming, cancelling and sweeping confirmations from several threads raises nothing", not errors, errors[:3])
check("every thread finished (none stuck)", not any(t.is_alive() for t in threads))

# ---- a confirmation is consumed exactly once, and the losers are told ------------------------------------------------------------
# (On the old code a second simultaneous confirm also never ran the action twice, but its `del` raised KeyError inside the radio callback;
# now each loser gets a clean "nothing is waiting" reply.)
ran = []
br.run_action = lambda action, params: (ran.append(1) or (True, "done"))
br.args.confirm_seconds = 60
s = SENDERS[0]
rid = br.audit.new_request(s, None, "run the demo", status="queued")
br.handle_tool_call(Job(rid, s, "run the demo", time.time(), 1), demo, 5)
code = br.pending[s].code
go = threading.Barrier(4)
replies_by_thread, crashed = [], []
def confirm_together():
    mine = []
    go.wait()
    try:
        br.handle_confirm(s, code, 1, "key verified", lambda status, text: mine.append(text), META(s), "confirm ••••••")
    except Exception as e:
        crashed.append(repr(e))
    replies_by_thread.append(mine)
group = [threading.Thread(target=confirm_together) for _ in range(4)]
for t in group: t.start()
for t in group: t.join(timeout=10)
time.sleep(0.5)                                    # the action runs on its own thread
losers = [m for m in replies_by_thread if m]
check("four simultaneous confirms with the right code run the action once", len(ran) == 1 and s not in br.pending, (len(ran), s in br.pending))
check("...and the three that lose each get a clean 'nothing is waiting' reply instead of an error", not crashed and len(losers) == 3 and all("Nothing is waiting" in m[0] for m in losers), (crashed, replies_by_thread))

# ---- an entry that expired after the last sweep cannot be claimed ------------------------------------------------------------------
rid = br.audit.new_request(s, None, "run the demo", status="queued")
br.handle_tool_call(Job(rid, s, "run the demo", time.time(), 1), demo, 5)
parked = br.pending[s]
parked.expires = time.time() - 1                   # ran out between the sweep at the top of on_receive and the confirm
check("a confirmation that has expired cannot be claimed, even before a sweep removed it", br.take_pending(s, parked) is False and s in br.pending)
br.sweep_pending()
check("...and the next sweep removes it and marks it expired in the log", s not in br.pending and br.audit.conversation(s)[-1]["status"] == "expired", br.audit.conversation(s)[-1]["status"])

# ---- every change to the table goes through the lock (structural guard) ---------------------------------------------------------------
# A lock only excludes other lockers: one unlocked pop() while a locked sweep iterates still raises "dictionary changed size during
# iteration". (That is how the /ai cancel path was missed the first time.) So no code may touch self.pending outside `with self.pending_lock`,
# except the one-time creation in __init__.
import ast
src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "meshllm", "bridge.py")
tree = ast.parse(open(src_path, encoding="utf-8").read())
parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
is_pending = lambda n: isinstance(n, ast.Attribute) and n.attr == "pending" and isinstance(n.value, ast.Name) and n.value.id == "self"
def under_lock(node):
    while node in parent:
        node = parent[node]
        if isinstance(node, ast.With) and any(isinstance(i.context_expr, ast.Attribute) and i.context_expr.attr == "pending_lock" for i in node.items):
            return True
    return False
touches = []
for node in ast.walk(tree):
    if isinstance(node, ast.Subscript) and is_pending(node.value) and isinstance(node.ctx, (ast.Store, ast.Del)):
        touches.append(node)
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and is_pending(node.func.value) \
            and node.func.attr in ("pop", "popitem", "clear", "update", "setdefault", "items", "values", "keys"):
        touches.append(node)
unlocked = sorted(n.lineno for n in touches if not under_lock(n))
check("every mutation or iteration of self.pending in bridge.py is inside `with self.pending_lock`", len(touches) >= 6 and not unlocked, unlocked)

# ---- duplicate-packet filter: a rolling window, not a cliff --------------------------------------------------------------------
br2, radio2, _ = make()
def deliver(pid):
    """A text DM from a known node, as the radio library would hand it to on_receive."""
    br2.on_receive({"id": pid, "from": 0xaaaa, "fromId": "!0000aaaa", "to": 1, "decoded": {"text": "hello"}}, radio2)
n0 = len(br2.audit.conversation("!0000aaaa"))
deliver(1); deliver(1)
check("the same packet delivered twice is handled once", len(br2.audit.conversation("!0000aaaa")) - n0 == 1)
for pid in range(2, 2600):                         # well past the old 2000-entry reset
    br2.remember_packet(pid)
before = len(br2.audit.conversation("!0000aaaa"))
deliver(2599)                                      # a very recent packet arriving again
check("a recent duplicate is still caught after the filter has rolled over many ids", len(br2.audit.conversation("!0000aaaa")) == before)
check("memory stays bounded", len(br2.seen_ids) <= 2000, len(br2.seen_ids))
check("a packet with no id is never treated as a duplicate (it used to be dropped after the first)", br2.remember_packet(None) is True and br2.remember_packet(None) is True)

print(f"\n{len(fails)} failed" if fails else "\nall passed")
sys.exit(1 if fails else 0)
