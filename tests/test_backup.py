"""Backup and restore: consistent copies while running, the daily copy, safe restore at the next start, hostile uploads."""
import io, os, shutil, sqlite3, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixture import make, Checker
from meshllm import backup as B
import requests as rq

check = Checker()
br, radio, tmp = make(web_port=8113)
base = "http://127.0.0.1:8113"
get = lambda p, **kw: rq.get(base + p, timeout=10, **kw)
post = lambda p, body: rq.post(base + p, json=body, timeout=10)
for i in range(5):
    br.audit.new_request(prompt=f"question {i}", status="answered", response="an answer", node_id="!0000aaaa", node_name="Lodge", kind="ai")
br.audit.set_setting("model", "fake")

# ---- making backups
r = post("/api/backups/create", {})
check("a manual backup is made", r.status_code == 200 and r.json()["name"].startswith("manual-") and r.json()["name"].endswith(".db"), r.text)
items = get("/api/backups").json()["items"]
check("it is listed with size and kind", len(items) == 1 and items[0]["kind"] == "manual" and items[0]["size"] > 1000, items)
path = os.path.join(br.backups.dir, items[0]["name"])
con = sqlite3.connect(path)
check("the copy is a complete, healthy database with our data", con.execute("PRAGMA integrity_check").fetchone()[0] == "ok" and con.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 5
      and con.execute("SELECT value FROM settings WHERE key='model'").fetchone()[0] == "fake")
con.close()
check("no temporary file is left behind", not [f for f in os.listdir(br.backups.dir) if f.endswith(".tmp")])

# ---- download
r = get("/api/backups/download")
check("the live database can be downloaded", r.status_code == 200 and r.content.startswith(B.MAGIC) and "attachment" in r.headers["Content-Disposition"] and ".db" in r.headers["Content-Disposition"])
r = get("/api/backups/download", params={"name": items[0]["name"]})
check("a saved backup can be downloaded", r.status_code == 200 and r.content.startswith(B.MAGIC) and len(r.content) == items[0]["size"])
for bad in ["../t.db", "..\\t.db", "manual-1.db", "auto-20200101-000000.db", "x", "manual-20200101-000000.db/../../t.db", ""]:
    r = get("/api/backups/download", params={"name": bad}) if bad else None
    if r is not None:
        check(f"download refuses {bad!r}", r.status_code == 400, r.text[:80])

# ---- automatic daily backup
br.backups.set_auto(True)
check("automatic backups are on by default and can be switched", post("/api/backups/auto", {"enabled": False}).json() == {"auto": False} and br.backups.maybe_auto() is None and post("/api/backups/auto", {"enabled": True}).json() == {"auto": True})
check("a bad 'enabled' is refused", post("/api/backups/auto", {"enabled": "yes"}).status_code == 400)
name = br.backups.maybe_auto()
check("the daily backup is made when none exists", name and name.startswith("auto-"), name)
check("...and not again within a day", br.backups.maybe_auto() is None)
old = os.path.join(br.backups.dir, name)
os.utime(old, (time.time() - 2 * 86400,) * 2)
check("...but is made again when the last is over a day old", br.backups.maybe_auto() is not None)
# keeping only the newest
for i in range(10):
    f = os.path.join(br.backups.dir, f"auto-2020010{i % 10}-0000{i:02d}.db"); shutil.copyfile(old, f); os.utime(f, (1000 + i,) * 2)
br.backups._prune()
check("only the newest 7 automatic backups are kept", len([i for i in br.backups.list() if i["kind"] == "auto"]) == B.KEEP_AUTO)
check("manual backups are not pruned with them", len([i for i in br.backups.list() if i["kind"] == "manual"]) == 1)
check("a hand-placed file with any other name is ignored", (open(os.path.join(br.backups.dir, "notes.txt"), "w").write("x"), all(i["name"] != "notes.txt" for i in br.backups.list()))[1])
n = [i["name"] for i in br.backups.list() if i["kind"] == "auto"][-1]
check("a backup can be deleted", post("/api/backups/delete", {"name": n}).status_code == 200 and n not in [i["name"] for i in br.backups.list()])
check("deleting something that isn't a backup is refused", post("/api/backups/delete", {"name": "../t.db"}).status_code == 400 and post("/api/backups/delete", {"name": 5}).status_code == 400 and post("/api/backups/delete", {}).status_code == 400)
check("the live database is still there after all of that", os.path.isfile(br.args.db))

# ---- restoring: nothing is replaced while running
check("nothing is set aside to begin with", get("/api/backups").json()["staged"] is None)
r = post("/api/backups/restore/existing", {"name": items[0]["name"]})
check("a saved backup can be set aside for restore", r.status_code == 200 and r.json()["requests"] == 5, r.text)
st = get("/api/backups").json()["staged"]
check("the page can see what is waiting (and that it is real)", st and st["requests"] == 5 and st["size"] > 1000, st)
before = br.audit.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
br.audit.new_request(prompt="newer question", status="answered", response="x", node_id="!0000aaaa", node_name=None, kind="ai")
check("the running database is untouched by setting aside", br.audit.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == before + 1)
check("it can be cancelled", post("/api/backups/restore/cancel", {}).json() == {"cancelled": True} and get("/api/backups").json()["staged"] is None and post("/api/backups/restore/cancel", {}).json() == {"cancelled": False})

# ---- uploads
data = open(path, "rb").read()
r = rq.post(base + "/api/backups/restore", data=data, headers={"Content-Type": "application/octet-stream"}, timeout=20)
check("an uploaded backup is checked and set aside", r.status_code == 200 and r.json()["requests"] == 5, r.text)
check("...and no stray upload file remains", not os.path.exists(br.backups.staged_path.with_suffix(".upload")))
br.backups.cancel_staged()
for label, payload in [("text", b"this is not a database at all" * 10), ("empty", b""), ("truncated", data[:5000]), ("wrong magic", b"SQLite format 4\x00" + data[16:]), ("zeros", b"\x00" * 4096)]:
    r = rq.post(base + "/api/backups/restore", data=payload, headers={"Content-Type": "application/octet-stream"}, timeout=20)
    check(f"upload refused: {label}", r.status_code == 400 and "error" in r.json() and get("/api/backups").json()["staged"] is None, r.text[:120])
other = os.path.join(tmp, "other.db"); c = sqlite3.connect(other); c.execute("CREATE TABLE unrelated (a)"); c.commit(); c.close()
r = rq.post(base + "/api/backups/restore", data=open(other, "rb").read(), headers={"Content-Type": "application/octet-stream"}, timeout=20)
check("a real SQLite file from something else is refused", r.status_code == 400 and "isn't from this bridge" in r.json()["error"], r.text)
check("a foreign origin cannot upload", rq.post(base + "/api/backups/restore", data=data, headers={"Content-Type": "application/octet-stream", "Origin": "http://evil.example"}, timeout=20).status_code == 403)
check("the live database was not touched by any of that", br.audit.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == before + 1)
check("files are created where expected", str(br.backups.dir).startswith(tmp))

# ---- the swap at start-up
live = os.path.join(tmp, "swap.db")
c = sqlite3.connect(live); c.execute("CREATE TABLE requests (id)"); c.execute("CREATE TABLE settings (key, value)"); c.execute("INSERT INTO requests VALUES (1)"); c.commit(); c.close()
open(live + "-wal", "wb").write(b"stale"); open(live + "-shm", "wb").write(b"stale")
shutil.copyfile(path, live + ".restore")
msgs = []
check("apply_staged_restore swaps the file in", B.apply_staged_restore(live, log=msgs.append) is True, msgs)
c = sqlite3.connect(live)
check("...the restored data is now the database", c.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 5); c.close()
kept = [f for f in os.listdir(os.path.join(tmp, "backups")) if f.startswith("before-restore-")]
check("...the old database is kept, not deleted", len(kept) == 1, kept)
check("...stale journal files are gone and the staged file is consumed", not os.path.exists(live + "-wal") and not os.path.exists(live + "-shm") and not os.path.exists(live + ".restore"))
check("...and nothing happens when nothing is waiting", B.apply_staged_restore(live, log=msgs.append) is False)
open(live + ".restore", "wb").write(b"garbage that slipped in")
check("a bad file set aside is rejected at start-up, the database stays", B.apply_staged_restore(live, log=msgs.append) is False and sqlite3.connect(live).execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 5 and os.path.exists(live + ".restore.rejected"))
check("start-up names the reason", any("not used" in m for m in msgs), msgs)
check("the old database kept by a restore appears in the list as before-restore", any(i["kind"] == "before-restore" for i in br.backups.list()), br.backups.list())

check.done()
