"""Node labels, notes and stars; saved snippets. Local to this PC: never transmitted, never shown to the AI."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixture import make, Checker
import userdata as U
import requests as rq

check = Checker()
br, radio, tmp = make(web_port=8111)
base = "http://127.0.0.1:8111"
post = lambda p, body: rq.post(base + p, json=body, timeout=5)

# ---- labels, notes, stars
r = post("/api/notes/set", {"node": "!0000aaaa", "label": "Lodge repeater", "note": "On the roof, solar", "starred": True})
check("setting a label, note and star works", r.status_code == 200 and r.json()["note"] == {"label": "Lodge repeater", "note": "On the roof, solar", "starred": True}, r.text)
rows = {n["id"]: n for n in br.mesh.nodes()}
check("every node row carries label, note and starred", rows["!0000aaaa"]["label"] == "Lodge repeater" and rows["!0000aaaa"]["starred"] is True and rows["!0000bbbb"]["label"] == "" and rows["!0000bbbb"]["starred"] is False, rows["!0000aaaa"])
check("...also in the list that includes remembered nodes", {n["id"]: n for n in br.mesh.all_nodes()}["!0000aaaa"]["label"] == "Lodge repeater")
check("...and in a node's detail", br.mesh.node_detail("!0000aaaa")["node"]["note"] == "On the roof, solar")
post("/api/notes/set", {"node": "!0000aaaa", "note": "moved"})
n = br.userdata.notes()["!0000aaaa"]
check("changing one field leaves the others alone", n["label"] == "Lodge repeater" and n["note"] == "moved" and n["starred"] is True, n)
post("/api/notes/set", {"node": "!0000aaaa", "label": "  Lodge \n  repeater  "})
check("a label's spaces and line breaks are tidied", br.userdata.notes()["!0000aaaa"]["label"] == "Lodge repeater")
post("/api/notes/set", {"node": "!0000aaaa", "label": "", "note": "", "starred": False})
check("clearing everything removes the row", "!0000aaaa" not in br.userdata.notes())
for body, why in [({"node": "lodge", "label": "x"}, "bad node id"), ({"label": "x"}, "no node"), ({"node": "!0000aaaa", "label": "x" * 41}, "label too long"),
                  ({"node": "!0000aaaa", "note": "x" * 501}, "note too long"), ({"node": "!0000aaaa", "label": 5}, "label not text"), ({"node": "!0000aaaa", "starred": "yes"}, "starred not a bool"),
                  ({"node": ["!0000aaaa"], "label": "x"}, "node not text")]:
    r = post("/api/notes/set", body)
    check(f"rejected: {why}", r.status_code == 400 and "error" in r.json(), r.text)
check("nothing was saved by the bad requests", br.userdata.notes() == {})
post("/api/notes/set", {"node": "!0000AAAA", "label": "Upper"})
check("node ids are case-insensitive", br.userdata.notes().get("!0000aaaa", {}).get("label") == "Upper")
post("/api/notes/set", {"node": "!0000aaaa", "label": "a\x00b\x07c"})
check("control characters are dropped", br.userdata.notes()["!0000aaaa"]["label"] == "abc")
check("a label for a node the radio has never heard is allowed", post("/api/notes/set", {"node": "!12345678", "label": "Future node", "starred": True}).status_code == 200)
check("labels are not sent over the radio", radio.sent == [])
check("the AI's tools never see them", "label" not in json.dumps(br.audit.list(limit=50)) and all("note" not in a.name and "label" not in a.name for a in __import__("actions").ACTIONS.values()))

# ---- snippets
r = post("/api/snippets/add", {"target": "channel", "text": "Testing, one two three"})
check("a channel snippet is saved", r.status_code == 200 and isinstance(r.json()["id"], int), r.text)
sn = rq.get(base + "/api/snippets?target=channel", timeout=5).json()["snippets"]
check("it is listed with an automatic label", len(sn) == 1 and sn[0]["text"] == "Testing, one two three" and sn[0]["label"] == "Testing, one two three", sn)
check("the label is shortened for a long snippet", post("/api/snippets/add", {"target": "dm", "text": "x" * 100 + " tail"}).status_code == 200 and rq.get(base + "/api/snippets?target=dm", timeout=5).json()["snippets"][0]["label"].endswith("..."))
check("a snippet can have its own label", post("/api/snippets/add", {"target": "dm", "text": "On my way", "label": "OMW"}).status_code == 200 and any(s["label"] == "OMW" for s in br.userdata.snippets("dm")))
check("the same snippet twice is refused", post("/api/snippets/add", {"target": "channel", "text": "Testing, one two three"}).status_code == 400)
check("a channel snippet over the post limit is refused (it could never be posted)", post("/api/snippets/add", {"target": "channel", "text": "x" * 201}).status_code == 400)
check("...but a DM snippet may be longer", post("/api/snippets/add", {"target": "dm", "text": "y" * 600}).status_code == 200)
check("a DM snippet has a limit too", post("/api/snippets/add", {"target": "dm", "text": "z" * 701}).status_code == 400)
for body, why in [({"target": "everyone", "text": "hi"}, "unknown target"), ({"text": "hi"}, "no target"), ({"target": "dm"}, "no text"), ({"target": "dm", "text": "   "}, "blank text"),
                  ({"target": "dm", "text": 5}, "text not text"), ({"target": "dm", "text": "ok", "label": "l" * 31}, "label too long")]:
    check(f"snippet rejected: {why}", post("/api/snippets/add", body).status_code == 400)
ids = [s["id"] for s in br.userdata.snippets()]
check("deleting works and is reported", post("/api/snippets/delete", {"id": ids[0]}).json() == {"deleted": 1} and post("/api/snippets/delete", {"id": ids[0]}).json() == {"deleted": 0})
check("a bad delete request is refused", post("/api/snippets/delete", {"id": "1"}).status_code == 400 and post("/api/snippets/delete", {}).status_code == 400 and post("/api/snippets/delete", {"id": True}).status_code == 400)
for i in range(U.SNIPPET_MAX_PER_TARGET):
    br.userdata.add_snippet("channel", f"snippet number {i}")
check("there is a cap per list", post("/api/snippets/add", {"target": "channel", "text": "one too many"}).status_code == 400)
check("a snippet never transmits anything", radio.sent == [])
check("the Data page lists both tables", {"node_notes", "snippets"} <= {d["name"] for d in br.mesh.data_overview()["datasets"]})
check("neither table can be exported under another name", br.mesh.export_csv("node_notes") is None and br.mesh.export_csv("snippets") is None)
check("a foreign origin cannot add a snippet", rq.post(base + "/api/snippets/add", json={"target": "dm", "text": "x"}, headers={"Origin": "http://evil.example"}, timeout=5).status_code == 403)

check.done()
