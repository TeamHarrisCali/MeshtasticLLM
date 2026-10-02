"""The dashboard's files: the script is split into static/js/*.js and joined by the server, the CSS is its own file."""
import collections, os, re, sys, threading, time, argparse, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import webui
import requests as rq

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)

S = os.path.join(ROOT, "static")
order = [l.strip() for l in open(os.path.join(S, "js", "order.txt"), encoding="utf-8").read().splitlines() if l.strip() and not l.startswith("#")]
on_disk = sorted(f for f in os.listdir(os.path.join(S, "js")) if f.endswith(".js"))
check("order.txt lists every script file exactly once", sorted(order) == on_disk and len(set(order)) == len(order), (order, on_disk))
check("the first file is the core and the last is the start-up code", order[0] == "core.js" and order[-1] == "main.js", order)

src = {n: open(os.path.join(S, "js", n), encoding="utf-8").read() for n in order}
# every file shares one global scope, so two top-level declarations with the same name would stop the whole dashboard loading
decl = collections.defaultdict(list)
for n, text in src.items():
    for m in re.finditer(r"^(?:async\s+)?(?:function\s+([A-Za-z_$][\w$]*)|(?:const|let|var)\s+([A-Za-z_$][\w$]*))", text, re.M):
        decl[m.group(1) or m.group(2)].append(n)
    for m in re.finditer(r"^(?:const|let)\s+\{([^}]*)\}", text, re.M):
        for part in m.group(1).split(","):
            decl[part.split(":")[-1].split("=")[0].strip()].append(n)
    for m in re.finditer(r"^let\s+([^;=\n]*(?:=[^;,\n]*,\s*)+[^;\n]*);", text, re.M):      # let a = 1, b = 2;
        for part in re.split(r",\s*(?=[A-Za-z_$][\w$]*\s*(?:=|,|$))", m.group(1)):
            name = part.split("=")[0].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", name): decl[name].append(n)
dupes = {k: sorted(set(v)) for k, v in decl.items() if len(set(v)) > 1}      # the same name in two different files
check("no name is declared twice at the top level across the files", not dupes, dupes)
check("no file contains Windows line endings after joining", b"\r" not in webui.dashboard_script())
check("the files are in the dashboard script, in order", webui.dashboard_script().decode("utf-8").index("const $ = id") < webui.dashboard_script().decode("utf-8").index("refreshChannel(force)"))
html = open(os.path.join(S, "index.html"), encoding="utf-8").read()
check("index.html loads one script and one stylesheet", html.count('<script src="/app.js"></script>') == 1 and html.count('href="/style.css"') == 1 and "<style>" not in html)
check("the stylesheet is not empty and keeps the colour variables", "--accent" in open(os.path.join(S, "style.css"), encoding="utf-8").read())

# over HTTP
from mesh_llm_bridge import Bridge
import mesh_llm_bridge as b
tmp = tempfile.mkdtemp(prefix="static_test_")
args = argparse.Namespace(db=os.path.join(tmp, "s.db"), port="STUB", model="m", command="/ai", ollama_url="http://127.0.0.1:9", max_tokens=50, num_ctx=4096, max_chunks=4, chunk_delay=0, cooldown=0,
    timeout=2, memory_turns=6, memory_hours=24, memory_chars=3000, no_log_inbound=False, web_host="127.0.0.1", web_port=8101, no_web=False, max_queue=3, queue_ttl=600, no_queue_notice=False,
    access_mode=None, daily_cap=None, confirm_seconds=60, no_tool_gate=True, chunk_bytes=160, send_retries=2, retry_delay=0.05, traceroute_timeout=0.2)
br = Bridge(args); webui.start(br); time.sleep(0.3)
base = "http://127.0.0.1:8101"
r = rq.get(base + "/app.js", timeout=5)
check("/app.js is the joined script", r.status_code == 200 and r.headers["Content-Type"].startswith("text/javascript") and r.content == webui.dashboard_script() and len(r.content) > 100000)
r = rq.get(base + "/style.css", timeout=5)
check("/style.css is served as CSS", r.status_code == 200 and r.headers["Content-Type"].startswith("text/css"), r.headers.get("Content-Type"))
check("the split files are also reachable one by one", rq.get(base + "/js/channel.js", timeout=5).status_code == 200)
check("the page itself loads", rq.get(base + "/", timeout=5).status_code == 200)
check("path traversal out of static/ is still refused", rq.get(base + "/js/..%2f..%2faudit.py", timeout=5).status_code in (403, 404) and rq.get(base + "/..%2fwebui.py", timeout=5).status_code in (403, 404))

print(f"\n{len(fails)} failed" if fails else "\nall passed")
sys.stdout.flush(); os._exit(1 if fails else 0)
