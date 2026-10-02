"""Map tile cache: on-demand fetch, disk cache, limits, and the /tiles route (no network: the fetcher is faked)."""
import argparse, os, shutil, sys, threading, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import tempfile; HERE = tempfile.mkdtemp(prefix="meshtest_")   # scratch databases and caches go in a temp folder, never in the project
from meshllm import tiles as T, webui, bridge as b
import requests as rq

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)

PNG = T.PNG_MAGIC + b"\x00" * 100
folder = os.path.join(HERE, "tile_cache_test"); shutil.rmtree(folder, ignore_errors=True)
calls = []
def fetch_ok(z, x, y): calls.append((z, x, y)); return PNG + bytes([z % 256])
tc = T.TileCache(folder, fetch=fetch_ok)

check("only real tiles are valid (zoom 0-19, x/y inside the world)", tc.valid(0, 0, 0) and tc.valid(19, 2 ** 19 - 1, 0) and not tc.valid(20, 0, 0) and not tc.valid(3, 8, 0) and not tc.valid(3, 0, 8) and not tc.valid(-1, 0, 0) and not tc.valid(3, -1, 0) and not tc.valid(True, 0, 0) and not tc.valid("3", 0, 0) and not tc.valid(3.0, 0, 0))
d = tc.get(5, 10, 12)
check("a tile is fetched the first time it's looked at and saved", d == PNG + b"\x05" and calls == [(5, 10, 12)] and (tc.dir / "5" / "10" / "12.png").read_bytes() == d)
d2 = tc.get(5, 10, 12)
check("...and the second time comes from disk without asking the tile server", d2 == d and len(calls) == 1 and tc.served_from_disk == 1)
check("an invalid tile is refused without any fetch", tc.get(3, 99, 0) is None and tc.get(25, 0, 0) is None and len(calls) == 1)
check("nothing but tiles is written (no stray temp files)", not list(tc.dir.rglob("*.tmp")) and [f.name for f in (tc.dir / "5" / "10").iterdir()] == ["12.png"])

# failures
def fetch_bad(z, x, y): calls.append(("bad", z, x, y)); raise OSError("offline")
tc2 = T.TileCache(os.path.join(folder, "b"), fetch=fetch_bad, retry_after_s=60)
n0 = len(calls)
check("when the tile server can't be reached the answer is 'no tile', not a crash", tc2.get(4, 1, 1) is None)
check("...and it isn't asked again for a minute (no hammering while offline)", tc2.get(4, 1, 1) is None and tc2.get(4, 1, 1) is None and len(calls) == n0 + 1)
tc2._failed.clear()
check("after the pause it tries again", tc2.get(4, 1, 1) is None and len(calls) == n0 + 2)
for bad, name in [(b"<html>blocked</html>", "an HTML error page"), (b"", "an empty body"), (T.PNG_MAGIC + b"x" * (T.MAX_TILE_BYTES + 1), "an oversized file"), ("str", "text")]:
    tc3 = T.TileCache(os.path.join(folder, "c"), fetch=lambda z, x, y, bad=bad: bad)
    check(f"{name} is never stored or served as a tile", tc3.get(2, 1, 1) is None and not list((tc3.dir).rglob("*.png")))

# stale tiles
old = tc.dir / "5" / "10" / "12.png"; past = time.time() - 40 * 86400; os.utime(old, (past, past))
calls.clear(); d3 = tc.get(5, 10, 12)
check("a tile older than 30 days is refreshed", len(calls) == 1 and d3 == PNG + b"\x05" and time.time() - old.stat().st_mtime < 60)
os.utime(old, (past, past)); tc_off = T.TileCache(folder, fetch=fetch_bad, retry_after_s=0)
check("an old tile is still served when the refresh fails (offline use)", tc_off.get(5, 10, 12) == PNG + b"\x05")

# concurrency
slow_calls = []
def fetch_slow(z, x, y): slow_calls.append(1); time.sleep(0.3); return PNG
tc4 = T.TileCache(os.path.join(folder, "d"), fetch=fetch_slow)
res = []
ths = [threading.Thread(target=lambda: res.append(tc4.get(6, 3, 3))) for _ in range(8)]
[t.start() for t in ths]; [t.join() for t in ths]
check("eight requests for the same tile fetch it once", len(slow_calls) == 1 and all(r == PNG for r in res) and len(res) == 8, (len(slow_calls), len(res)))
active, peak, lock = [0], [0], threading.Lock()
def fetch_count(z, x, y):
    with lock: active[0] += 1; peak[0] = max(peak[0], active[0])
    time.sleep(0.05)
    with lock: active[0] -= 1
    return PNG
tc5 = T.TileCache(os.path.join(folder, "e"), fetch=fetch_count, parallel=3)
ths = [threading.Thread(target=tc5.get, args=(7, i, 0)) for i in range(12)]
[t.start() for t in ths]; [t.join() for t in ths]
check("at most `parallel` tiles are fetched at the same time", 1 <= peak[0] <= 3, peak[0])

# size cap
tc6 = T.TileCache(os.path.join(folder, "f"), max_bytes=1000, fetch=lambda z, x, y: PNG + b"\x00" * 200)
for i in range(10):
    tc6.get(8, i, 0); p_ = tc6._path(8, i, 0); os.utime(p_, (time.time() - 1000 + i, time.time() - 1000 + i))
before = tc6.stats()
removed = tc6.prune()
after = tc6.stats()
check("pruning deletes the OLDEST tiles until the folder is under 80% of the cap", removed > 0 and after["bytes"] <= 800 and before["bytes"] > 1000 and tc6._path(8, 9, 0).exists() and not tc6._path(8, 0, 0).exists(), (before, after))
check("stats count tiles and bytes", after["tiles"] == len(list(tc6.dir.rglob("*.png"))) and after["max_bytes"] == 1000)
check("clear deletes every tile", tc6.clear() == after["tiles"] and tc6.stats()["tiles"] == 0)
check("an empty or missing cache folder is fine", T.TileCache(os.path.join(folder, "nope")).stats()["tiles"] == 0 and T.TileCache(os.path.join(folder, "nope")).prune() == 0)

# HTTP route
db = os.path.join(HERE, "tiles_web.db")
for ext in ("", "-wal", "-shm"):
    try: os.remove(db + ext)
    except OSError: pass
br = b.Bridge(argparse.Namespace(db=db, ollama_url="http://127.0.0.1:9", model="m", access_mode=None, daily_cap=None, no_tool_gate=True, web_host="127.0.0.1", web_port=8094, no_web=True,
                                 command="/ai", port="auto", memory_turns=6, memory_hours=24, memory_chars=3000, max_queue=5, max_chunks=4, cooldown=0, traceroute_timeout=0.2))
check("the bridge keeps its tiles next to its database", str(br.tiles.dir).endswith("tile_cache") and os.path.dirname(str(br.tiles.dir)) == os.path.dirname(db))
shutil.rmtree(br.tiles.dir, ignore_errors=True)
web_calls = []
br.tiles = T.TileCache(os.path.join(folder, "web"), fetch=lambda z, x, y: (web_calls.append((z, x, y)), PNG + b"W")[1])
srv = webui.start(br); time.sleep(0.3); base = "http://127.0.0.1:8094"
r = rq.get(base + "/tiles/3/2/4.png", timeout=5)
check("/tiles/z/x/y.png serves a PNG the browser may cache", r.status_code == 200 and r.headers["Content-Type"] == "image/png" and r.content == PNG + b"W" and "max-age" in r.headers["Cache-Control"] and "no-store" not in r.headers["Cache-Control"], dict(r.headers))
r = rq.get(base + "/tiles/3/2/4.png", timeout=5)
check("the second request is served from the disk cache", r.status_code == 200 and len(web_calls) == 1)
check("other responses are still never cached by the browser", rq.get(base + "/api/status", timeout=5).headers["Cache-Control"] == "no-store")
csp = rq.get(base + "/", timeout=5).headers["Content-Security-Policy"]
check("the page no longer lets the browser load images from openstreetmap.org", "openstreetmap" not in csp and "img-src 'self' data:" in csp, csp)
for path in ["/tiles/3/9/0.png", "/tiles/25/0/0.png", "/tiles/a/b/c.png", "/tiles/3/2/4.jpg", "/tiles/3/2.png", "/tiles/3/2/4.png/extra", "/tiles/../audit.db", "/tiles/3/2/..%2f..%2faudit.db.png", "/tiles/-1/0/0.png", "/tiles/0003/0/0.png", "/tiles//2/4.png"]:
    r = rq.get(base + path, timeout=5); n_ = len(web_calls)
    check(f"bad tile path {path[:40]} is refused without fetching", r.status_code in (404, 400) and len(web_calls) == n_ and (r.status_code != 200), (r.status_code, path))
n_ = len(web_calls); r = rq.get(base + "/tiles/3/2/4.png?x=" + "a" * 3000, timeout=5)
check("a junk query string on a valid tile is ignored (and served from the cache)", r.status_code == 200 and len(web_calls) == n_)
r = rq.get(base + "/tiles/3/2/4.png", headers={"Host": "evil.example"}, timeout=5)
check("tiles honour the Host check like everything else", r.status_code == 403)
br.tiles = T.TileCache(os.path.join(folder, "web2"), fetch=fetch_bad, retry_after_s=60)
check("a tile that can't be fetched is a clean 404", rq.get(base + "/tiles/4/1/1.png", timeout=5).status_code == 404)
r = rq.get(base + "/api/tiles/stats", timeout=5); check("GET /api/tiles/stats", r.status_code == 200 and set(r.json()) == {"tiles", "bytes", "max_bytes"})
br.tiles = T.TileCache(os.path.join(folder, "web"), fetch=lambda z, x, y: PNG)
r = rq.post(base + "/api/tiles/clear", json={}, timeout=5); check("POST /api/tiles/clear deletes the saved tiles", r.status_code == 200 and r.json()["deleted"] == 1 and br.tiles.stats()["tiles"] == 0, r.text)
check("clear needs JSON and a same-site origin", rq.post(base + "/api/tiles/clear", data="x", timeout=5).status_code == 415 and rq.post(base + "/api/tiles/clear", json={}, headers={"Origin": "http://evil.example"}, timeout=5).status_code == 403)
srv.shutdown()
shutil.rmtree(folder, ignore_errors=True)
print(f"\n{len(fails)} failed" if fails else "\nall passed")
sys.exit(1 if fails else 0)
