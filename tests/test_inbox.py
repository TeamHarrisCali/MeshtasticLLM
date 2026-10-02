"""What's new (sidebar badges and alerts) and search across everything."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixture import make, Checker
import requests as rq

check = Checker()
br, radio, tmp = make(web_port=8112)
base = "http://127.0.0.1:8112"
get = lambda p: rq.get(base + p, timeout=5).json()

def heard(text, frm=0xaaaa, to=0xFFFFFFFF, pid=None):
    p = {"from": frm, "fromId": "!%08x" % frm, "to": to, "id": pid, "decoded": {"text": text}}
    br.on_receive(p, radio); br.channel.on_text(p, radio)

# ---- unread
u = get("/api/unread")
check("a first visit (no ids) reports nothing new and says where to start", u["channel"]["count"] == 0 and u["dm"]["count"] == 0 and u["channel"]["latest"] == 0 and "alerts" in u, u)
heard("first channel post", pid=1); heard("second, hello Base Station!", pid=2); heard("third, nothing to do with you", pid=3)
heard("a direct hello", to=1, pid=4)
u = get("/api/unread?channel=0&dm=0")
check("three channel posts are new since id 0", u["channel"]["count"] == 3 and u["channel"]["latest"] == 3, u["channel"])
check("the newest one is named, for a notification", u["channel"]["newest"]["text"].startswith("third") and u["channel"]["newest"]["name"] == "Lodge", u["channel"]["newest"])
check("a mention of this radio's name is counted", u["channel"]["mentions"] == 1, u["channel"])
check("the direct message is counted separately", u["dm"]["count"] == 1 and u["dm"]["newest"]["text"] == "a direct hello", u["dm"])
u = get("/api/unread?channel=2&dm=%d" % u["dm"]["latest"])
check("only newer ones count once the page has seen some", u["channel"]["count"] == 1 and u["dm"]["count"] == 0, u)
heard("BASE is a short-name mention", pid=5); heard("the database is big", pid=6); heard("Base Stations everywhere", pid=7)
u = get("/api/unread?channel=3&dm=99")
check("mentions match whole words only, in any case", u["channel"]["mentions"] == 2, u["channel"])
check("our own posts are never 'unread'", (br.channel.post("my own post"), get("/api/unread?channel=7&dm=99")["channel"]["count"])[1] == 0)
check("junk ids don't break it", rq.get(base + "/api/unread?channel=abc&dm=-5", timeout=5).status_code == 200)
alerts = get("/api/unread")["alerts"]
check("alerts carry a kind (battery is low on the Lodge)", any(a["kind"] == "battery" for a in alerts), alerts)
check("the AI log does not include channel posts", "first channel post" not in json.dumps(br.audit.list(limit=100)))

# ---- search
br.userdata.set_note("!0000aaaa", label="Roof repeater", note="solar powered, check the panel in spring", starred=True)
br.audit.new_request(prompt="what is the battery on the lodge?", status="answered", response="Lodge is at 15%.", node_id="!0000aaaa", node_name="Lodge", kind="ai")
br.audit.new_request(prompt="hey, are you coming to the farm on friday?", status="inbound", node_id="!0000cccc", node_name="Far Farm", kind="inbound")
br.audit.new_request(prompt="", status="manual", response="yes, bringing the ladder", node_id="!0000cccc", node_name="Far Farm", kind="manual")
br.mesh.remember()
def groups(q):
    r = get("/api/search?q=" + rq.utils.quote(q))
    return {g["key"]: g for g in r.get("groups", [])}, r
g, r = groups("ladder")
check("a message you sent is found under direct messages, marked as yours", list(g) == ["dm"] and g["dm"]["items"][0]["sub"] == "you" and "ladder" in g["dm"]["items"][0]["text"], r)
check("...with where to go", g["dm"]["items"][0]["go"] == ["dm", "!0000cccc"], g["dm"]["items"][0])
g, _ = groups("friday")
check("a message they sent is found too", g["dm"]["items"][0]["sub"] == "them", g)
g, _ = groups("battery")
check("AI questions are found under AI conversations", g["ai"]["items"][0]["go"] == ["chat", "!0000aaaa"] and g["ai"]["items"][0]["sub"] == "asked", g)
g, _ = groups("15%")
check("...including the AI's answer, and a % is taken literally", g["ai"]["items"][0]["sub"] == "AI answered", g)
g, r = groups("solar")
check("your node notes are searchable", g["nodes"]["items"][0]["title"] == "Roof repeater" and "solar" in g["nodes"]["items"][0]["text"], r)
g, _ = groups("roof rep")
check("a label is found and shown as the title, even with spaces in the search", g["nodes"]["items"][0]["title"] == "Roof repeater")
g, _ = groups("!0000bbbb")
check("a node id finds the node", g["nodes"]["items"][0]["go"] == ["nodes", "!0000bbbb"], g)
g, _ = groups("Ridge")
check("a node's own name is found", g["nodes"]["items"][0]["title"] == "Ridge Repeater")
g, _ = groups("third")
check("public channel posts are found and link to the page", g["channel"]["items"][0]["go"] == ["channel", None] and g["channel"]["items"][0]["sub"] == "heard", g)
g, _ = groups("my own post")
check("...including your own posts", g["channel"]["items"][0]["sub"] == "you")
check("a search with no matches says so cleanly", groups("zzzzqqq")[1]["groups"] == [] and groups("zzzzqqq")[1]["total"] == 0)
check("one letter is too short", "at least" in get("/api/search?q=a")["error"] and get("/api/search?q=a")["groups"] == [])
check("no query at all is handled", get("/api/search")["groups"] == [])
for q in ["%", "_", "\\", "'; DROP TABLE requests; --", "\" OR 1=1", "a" * 500, "\x00\x00"]:
    r = rq.get(base + "/api/search", params={"q": q}, timeout=5)
    check(f"hostile search {q[:12]!r} is harmless", r.status_code == 200, r.text[:100])
check("the requests table is intact after all that", br.audit.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] >= 4)
g, _ = groups("LODGE")
check("search is case-insensitive", "nodes" in g)
check("searching and counting transmit nothing: the only thing queued for the radio is the one post made above", radio.sent == [] and br.outbox.qsize() == 1, (radio.sent, br.outbox.qsize()))

check.done()
