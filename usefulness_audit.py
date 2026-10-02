"""Send a realistic set of questions through the live bridge (the browser-chat path: same queue, gate, model and read-only tools as a radio DM)
and record what comes back, so the gaps can be counted. Memory is cleared before every question."""
import json, sys, time
import requests as rq

BASE = "http://127.0.0.1:8080"
QUESTIONS = [  # (group, question)
    ("environment", "what's the temperature outside?"),
    ("environment", "is it humid out there?"),
    ("environment", "what's the weather like near us?"),
    ("environment", "which nodes report temperature?"),
    ("mesh", "which node has the best signal to us?"),
    ("mesh", "who has been quiet for a while?"),
    ("mesh", "how many nodes can we reach directly?"),
    ("mesh", "is Downtown Router online?"),
    ("mesh", "when was Tracker Node last heard?"),
    ("mesh", "which node is farthest away?"),
    ("mesh", "where is the nearest router?"),
    ("history", "has the battery on Trail Mobile been dropping today?"),
    ("history", "what's the busiest time on the mesh?"),
    ("history", "what happened on the mesh in the last hour?"),
    ("computer", "how's the PC doing?"),   # no tool for this any more: the right answer is an honest "I can't check that"
    ("computer", "is ollama running?"),
    ("self", "what time is it?"),
    ("self", "what's today's date?"),
    ("self", "what can you do?"),
    ("self", "help"),
    ("self", "what model are you?"),
    ("self", "are you there?"),
    ("jobsite", "how many gallons per foot in 2 inch pipe?"),
    ("jobsite", "convert 3/4 inch to millimeters"),
    ("jobsite", "what's the travel on a 45 degree offset with 12 inches of set?"),
]


def ask(q, wait=90):
    rq.post(f"{BASE}/api/memory/clear", json={"node": "web-console"}, timeout=10)
    r = rq.post(f"{BASE}/api/ai/ask", json={"prompt": q}, timeout=10)
    if r.status_code != 200:
        return {"status": "refused", "response": r.text, "action": None, "ms": None}
    rid, end = r.json()["id"], time.time() + wait
    while time.time() < end:
        rows = {m["id"]: m for m in rq.get(f"{BASE}/api/conversation?node=web-console", timeout=10).json()["messages"]}
        if rows[rid]["status"] != "queued":
            m = rows[rid]; return {"status": m["status"], "response": m["response"], "action": m["action"], "ms": m["llm_ms"]}
        time.sleep(1)
    return {"status": "timeout", "response": None, "action": None, "ms": None}


out = []
for grp, q in QUESTIONS:
    res = ask(q); res.update(group=grp, question=q); out.append(res)
    print(f"[{grp}] {q!r} -> {res['status']}/{res['action']}/{res['ms']}ms :: {(res['response'] or '')[:160]!r}", flush=True)
json.dump(out, open(sys.argv[1], "w", encoding="utf-8"), indent=1, ensure_ascii=False)
print("DONE")
