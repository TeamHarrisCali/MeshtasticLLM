"""Demo mode (`--demo`): the whole bridge and dashboard with a simulated radio and a scripted model, no hardware and no Ollama.

Everything is polled with a generous timeout instead of slept on, so none of these checks depends on how fast the machine is."""
import json, math, os, re, shutil, signal, socket, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import requests as rq
from meshllm import actions, bridge as b, demo

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {str(detail)[:300]}"))
    if not cond: fails.append(name)
def until(cond, t=90.0):
    """Poll `cond` until it is truthy (returns its value) or `t` seconds pass (returns the last falsy value)."""
    end, v = time.time() + t, None
    while time.time() < end:
        v = cond()
        if v: return v
        time.sleep(0.05)
    return v
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

REAL_DB = os.path.join(ROOT, "audit.db")
real_before = os.path.getmtime(REAL_DB) if os.path.exists(REAL_DB) else None

# ---- nothing may reach a serial port or leave the machine ----------------------------------------------------
# (The one traffic demo mode can cause beyond this machine is the browser's OpenStreetMap tile fetch on the Map page, through the
# bridge's tile cache. It needs a browser looking at that page, is documented in the README, and is not exercised here.)
serial_calls, remote_connects = [], []
import meshtastic.serial_interface as _si
_orig_serial, _orig_connect = _si.SerialInterface, socket.socket.connect
def _no_serial(*a, **k):
    serial_calls.append((a, k)); raise AssertionError("demo mode must never open a serial port")
_si.SerialInterface = _no_serial
def _watch_connect(self, address, *a, **k):
    host = address[0] if isinstance(address, tuple) else address
    if host not in ("127.0.0.1", "localhost", "::1"): remote_connects.append(address)
    return _orig_connect(self, address, *a, **k)
socket.socket.connect = _watch_connect
try:
    import meshtastic.tcp_interface as _ti
    _ti.TCPInterface = lambda *a, **k: serial_calls.append(("tcp", a)) or (_ for _ in ()).throw(AssertionError("no network radio in demo mode"))
except Exception: pass

# ---- the fake mesh on its own --------------------------------------------------------------------------------
radio = demo.DemoRadio(speed=50.0)
nodes = list(radio.nodes.values())
check("the fake mesh has a few dozen nodes", 25 <= len(nodes) <= 45, len(nodes))
check("every name is obviously fake and every id is a !d3 id", all(n["user"]["longName"].startswith("Demo ") and re.fullmatch(r"!d3[0-9a-f]{6}", n["user"]["id"]) for n in nodes))
check("no real-looking contact details anywhere in the node list", "@" not in json.dumps(nodes) and not re.search(r"\d{3}[- ]\d{3}[- ]\d{4}", json.dumps(nodes)))
check("a spread of roles and hardware", len({n["user"]["role"] for n in nodes}) >= 5 and len({n["user"]["hwModel"] for n in nodes}) >= 5)
pos = [n["position"] for n in nodes if "position" in n]
km = lambda p: math.hypot((p["latitude"] - demo.CENTER[0]) * 111.0, (p["longitude"] - demo.CENTER[1]) * 111.0 * math.cos(math.radians(demo.CENTER[0])))
check("positions are scattered within a few km of the park", len(pos) >= 25 and all(km(p) < 6 for p in pos))
check("a few sensor nodes report temperature and humidity", sum(1 for n in nodes if "temperature" in (n.get("environmentMetrics") or {}) and "relativeHumidity" in n["environmentMetrics"]) >= 3)
check("some nodes are stale (quiet for hours)", sum(1 for n in nodes if time.time() - n.get("lastHeard", time.time()) > 6 * 3600) >= 3)
check("a few batteries are low among the nodes heard lately", sum(1 for n in nodes if (n.get("deviceMetrics") or {}).get("batteryLevel", 100) <= 20 and time.time() - n.get("lastHeard", 0) < 86400) >= 3)
check("hop counts range from direct to several relays", {n.get("hopsAway") for n in nodes if "hopsAway" in n} >= {0, 1, 2, 3})
check("the fake keys are unique and not real", len({n["user"]["publicKey"] for n in nodes}) == len(nodes))
check("it presents itself like a radio the bridge can use", radio.getMyUser()["id"] == "!d3000001" and radio.myInfo.my_node_num == 0xD3000001 and radio.stream is not None and radio._rxThread.is_alive())
acks = []
radio.sendText("hello", destinationId="!d3000007", wantAck=True, onResponse=acks.append)
check("a sent message is recorded and acknowledged like a real radio would", until(lambda: acks, 30) and acks[0]["decoded"]["routing"]["errorReason"] == "NONE" and radio.sent == [("!d3000007", "hello")], acks)
traced = []
from meshtastic.protobuf import mesh_pb2, portnums_pb2
by_short = lambda r, short: next(n for n in r.neighbours if n.spec.short == short)
radio.sendData(mesh_pb2.RouteDiscovery(), destinationId=by_short(radio, "LAKE").id, portNum=portnums_pb2.PortNum.TRACEROUTE_APP, wantResponse=True, onResponse=traced.append, hopLimit=7)
radio.sendData(mesh_pb2.RouteDiscovery(), destinationId=by_short(radio, "SHED").id, portNum=portnums_pb2.PortNum.TRACEROUTE_APP, wantResponse=True, onResponse=traced.append, hopLimit=7)
check("a traceroute is answered by a node that is awake, and not by one that has been quiet for hours", until(lambda: len(traced) == 1, 30) and traced[0]["decoded"]["portnum"] == "TRACEROUTE_APP", traced)
radio.localNode.setFixedPosition(40.78, -73.97, 10); radio.localNode.setTime(); radio.localNode.beginSettingsTransaction(); radio.localNode.writeConfig("lora"); radio.localNode.commitSettingsTransaction()
check("admin calls are harmless no-ops that change only the fake state", radio.localNode.localConfig.position.fixed_position and radio.us.entry["position"]["latitude"] == 40.78)
radio.close()
check("closing the fake radio ends its reader thread", until(lambda: not radio._rxThread.is_alive(), 30) and radio.stream is None)

# ---- the scripted model ----------------------------------------------------------------------------------------
GOOD = [("how's the mesh doing?", "mesh_summary"), ("which nodes have the lowest battery?", "list_nodes"), ("what's the temperature outside?", "mesh_report"),
        ("where is the nearest router?", "list_nodes"), ("has the battery on Demo Trail Tracker 3 been dropping?", "node_history"),
        ("which nodes have gone quiet?", "mesh_report"), ("how well can we hear nearby nodes?", "mesh_report"), ("what happened in the last hour?", "mesh_report"),
        ("tell me about node Demo Hilltop Router", "node_info"), ("what is the humidity?", "mesh_report"), ("when is the mesh busiest?", "mesh_report")]
ok = True
for text, tool in GOOD:
    plan = demo.plan_tool(text)
    try:
        action, params = actions.validate(plan[0], plan[1], 0)      # the real validator: the scripted model can only ask for what the menu allows
        ok = ok and plan[0] == tool and action.name == tool
    except Exception as e:
        ok = False; print("   bad plan for", text, plan, e)
check("the scripted model picks the right tool from the real menu, with arguments the validator accepts", ok)
check("...and where is the nearest router means nearest, routers only", demo.plan_tool("where is the nearest router?") == ("list_nodes", {"sort": "nearest", "role": "router"}), demo.plan_tool("where is the nearest router?"))
check("questions that are not about the mesh get no tool", all(demo.plan_tool(t) is None for t in ["hello there", "what time is it?", "explain what a router does", "what is a mesh router?", "tell me a joke", "delete everything"]))

fake = demo.ScriptedOllama(); fake.start()
try:
    tags = rq.get(fake.url + "/api/tags", timeout=5).json()["models"]
    check("the fake Ollama lists one clearly labelled demo model", [m["name"] for m in tags] == ["demo-scripted"] and "scripted" in tags[0]["details"]["family"], tags)
    check("...which is loaded, and can chat and use tools", rq.get(fake.url + "/api/ps", timeout=5).json()["models"][0]["name"] == "demo-scripted" and set(rq.post(fake.url + "/api/show", json={"model": "x"}, timeout=5).json()["capabilities"]) >= {"completion", "tools"})
    gate = lambda q: rq.post(fake.url + "/api/chat", json={"model": "m", "messages": [{"role": "system", "content": b.GATE_PROMPT}, {"role": "user", "content": q}]}, timeout=5).json()["message"]["content"]
    check("it answers the tool gate YES for mesh questions and NO for the rest", b.gate_says_yes(gate("how many nodes are around?")) and not b.gate_says_yes(gate("what is the capital of France?")))
    chat = lambda q, tools=True: rq.post(fake.url + "/api/chat", json=b.build_chat_body("demo-scripted", q, [], 0 if tools else -1, 50, 4096), timeout=5).json()["message"]
    m = chat("how's the mesh doing?")
    check("with tools offered it returns a tool call", m["tool_calls"][0]["function"]["name"] == "mesh_summary" and m["content"] == "", m)
    m = chat("how's the mesh doing?", tools=False)
    check("without tools it says so in plain chat, signed as the scripted model", "scripted demo model" in m["content"] and "tool_calls" not in m, m)
    check("downloads are refused", rq.post(fake.url + "/api/pull", json={"model": "x"}, timeout=5).status_code == 400)
finally:
    fake.stop()
check("the fake Ollama stops and frees its port", until(lambda: socket.socket().connect_ex(("127.0.0.1", int(fake.url.rsplit(":", 1)[1]))) != 0, 30))

class Stub(BaseHTTPRequestHandler):
    """A stand-in for a real Ollama: one installed model, with or without tool support."""
    tools = True
    def log_message(self, *a): pass
    def _out(self, obj):
        data = json.dumps(obj).encode(); self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self): self._out({"models": [{"name": "real-model:1b", "digest": "abc", "size": 5, "details": {}}]})
    def do_POST(self): self.rfile.read(int(self.headers.get("Content-Length") or 0)); self._out({"capabilities": ["completion"] + (["tools"] if self.tools else [])})
def stub(tools):
    h = type("H", (Stub,), {"tools": tools}); srv = ThreadingHTTPServer(("127.0.0.1", 0), h); threading.Thread(target=srv.serve_forever, daemon=True).start(); return srv
s1, s2 = stub(True), stub(False)
check("a real Ollama with a tool-capable model is picked up", demo.pick_real_model("http://127.0.0.1:%d" % s1.server_address[1]) == "real-model:1b")
check("a real Ollama whose models cannot use tools is not used", demo.pick_real_model("http://127.0.0.1:%d" % s2.server_address[1]) is None)
check("no Ollama at all means the scripted model", demo.pick_real_model("http://127.0.0.1:%d" % free_port()) is None)
s1.shutdown(); s2.shutdown()

# ---- the whole bridge in demo mode, in this process --------------------------------------------------------------
port = free_port()
args = b.build_parser().parse_args(["--demo", "--demo-scripted", "--demo-speed", "25", "--web-port", str(port), "--cooldown", "0"])
demo.configure(args)
demo_dir = args.demo_dir
check("the database and every folder are temporary, never the real audit.db", os.path.dirname(args.db) == demo_dir and demo_dir.startswith(tempfile.gettempdir()) and os.path.realpath(args.db) != os.path.realpath(REAL_DB), args.db)
check("the model is the scripted demo model on a localhost port, and nothing warms Ollama up", args.model == "demo-scripted" and args.ollama_url.startswith("http://127.0.0.1:") and args.no_warm_up)
br = b.Bridge(args)
runner = threading.Thread(target=br.run, name="demo-bridge", daemon=True)
runner.start()
base = "http://127.0.0.1:%d" % port
get = lambda p, **kw: rq.get(base + p, timeout=10, **kw)
def connected_status():
    """The status once the dashboard answers and the simulated radio is attached; None before that."""
    try:
        s = rq.get(base + "/api/status", timeout=5).json()
    except Exception:
        return None
    return s if s.get("connected") else None
status = until(connected_status, 120)
check("it starts with no hardware and no Ollama, and the dashboard reports a connected radio", bool(status) and status["port"] == "demo" and status["node"]["id"] == "!d3000001", status)
check("the status flags demo mode (the dashboard shows its badge) and names the scripted model", status["demo"] is True and status["model"] == "demo-scripted" and status["ollama_ok"] is True, status)
check("the only interface is the DemoRadio, and nothing opened a serial port", isinstance(br.iface, demo.DemoRadio) and serial_calls == [], serial_calls)

nodes_api = until(lambda: (lambda n: n if len(n) >= 25 else None)(get("/api/mesh/nodes").json()), 60)
check("/api/mesh/nodes lists the fake mesh", bool(nodes_api) and all(n["name"].startswith("Demo ") for n in nodes_api), nodes_api and nodes_api[:1])
check("/api/nodes (the recipient picker) lists them too", len(get("/api/nodes").json()) >= 25)
home = until(lambda: (lambda h: h if h["sensors"]["overall"]["temperature"] is not None else None)(get("/api/home").json()), 60) or get("/api/home").json()
check("Home has headline numbers, a map's worth of positions and sensors", home["summary"]["nodes_total"] >= 25 and home["places"]["total"] >= 25 and home["sensors"]["overall"]["temperature"] is not None, home["summary"])
traffic_api = until(lambda: (lambda t: t if t["total"] > 100 and t["talkers"] else None)(get("/api/mesh/traffic").json()), 60)
check("Trends: packet counts per hour and the busiest senders are filled", bool(traffic_api) and len(traffic_api["types"]) >= 5, traffic_api and traffic_api["types"])
check("Trends: a day of health snapshots", len(get("/api/mesh/samples").json()) >= 48)
check("Trends: hop statistics", get("/api/mesh/hops").json()["total"] > 100)
check("Map: trails for the nodes that move", len(get("/api/mesh/trails").json()) >= 3)
check("Map: remembered nodes the radio has forgotten", get("/api/mesh/places").json()["stored_only"] >= 3)
lm = get("/api/mesh/linkmap").json()
check("Coverage: directly heard nodes with signal and distance", len(lm["links"]) >= 5 and lm["farthest"] is not None, lm["links"][:1])
check("Coverage page data loads", get("/api/coverage").status_code == 200)
check("Activity: the feed has several kinds of event (polled: AI questions arrive as the demo runs)",
      until(lambda: len({e["type"] for e in get("/api/mesh/feed?limit=200").json()}) >= 3, 90))
check("Telemetry: readings are recorded and the sensors report", get("/api/telemetry").status_code == 200 and until(lambda: get("/api/mesh/sensors?hours=24").json()["overall"]["nodes"] >= 3, 60))
pages = ["/api/status", "/api/stats", "/api/requests", "/api/conversations", "/api/queue", "/api/access", "/api/actions", "/api/models", "/api/channel", "/api/mesh/feed",
         "/api/ai/overview", "/api/data/overview", "/api/tiles/stats", "/api/radio/position", "/api/radio/config", "/api/radio/clock", "/api/traceroutes", "/api/unread",
         "/api/backups", "/api/diagnostics", "/api/report", "/api/evals", "/api/telemetry/watch"]
bad = [p for p in pages if get(p).status_code != 200]
check("every dashboard page's data loads", not bad, bad)
rc = get("/api/radio/config").json()
check("Radio settings render from real protobuf objects, secrets left out", rc["connected"] and len(rc["sections"]) >= 8 and not any(w in json.dumps(rc) for w in ("private_key", "wifi_psk", "admin_key", "fixed_pin")), [s["name"] for s in rc["sections"]])
check("Model page: only the scripted demo model, able to use tools", [m["name"] for m in get("/api/models").json()["installed"]] == ["demo-scripted"] and get("/api/models").json()["installed"][0]["tools"])
check("the public channel has chatter", until(lambda: len(get("/api/channel").json()["messages"]) >= 5, 60))
check("Diagnostics and the log viewer read the temporary folder, not the real logs", get("/api/logs?which=out").json()["exists"] is False)

# the scripted conversations the generator starts by itself (nobody here asks anything)
auto = until(lambda: [r for r in get("/api/requests?limit=50").json() if r["kind"] == "ai" and r["status"] in ("action_ok", "answered") and r["node_name"].startswith("Demo ")], 120)
check("a fake node asks the bridge an /ai question on its own and it is answered", bool(auto), get("/api/requests?limit=5").json())
check("...through the model and tools, and the reply is acked like a real radio's", until(lambda: any(r["delivered"] + r["relayed"] > 0 for r in get("/api/requests?limit=50").json() if r["kind"] == "ai"), 30))
check("...the AI log shows which model answered", all(r["model"] == "demo-scripted" for r in get("/api/requests?limit=50").json() if r["status"] in ("action_ok", "answered")))
check("conversations appear on the AI conversations page", len(get("/api/conversations").json()) >= 1)

# a scripted question from a node of my own choosing (not one the generator uses, so it is never rate-limited by a question in flight)
tr = br.demo["traffic"]
asker = next(n for n in br.iface.neighbours if n.spec.short == "ROOF")
br.set_node_access(asker.id, max_tier=0, pin_key=br.radio_key(asker.id))
def ask(node, text):
    tr.ask(node, text)
    prompt = text.replace("/ai ", "", 1)
    return until(lambda: next((r for r in get("/api/requests?limit=100").json() if r["node_id"] == node.id and r["prompt"] == prompt and r["status"] not in ("queued",)), None), 90)
row = ask(asker, "/ai how's the mesh doing?")
check("a scripted /ai question gets an answered audit row via the fake model", bool(row) and row["status"] == "action_ok" and row["action"] == "mesh_summary" and row["model"] == "demo-scripted" and row["response"].startswith("Mesh: "), row)
check("...with the real numbers from the simulated mesh", bool(row) and re.search(r"Mesh: \d\d nodes known", row["response"]), row and row["response"])
row = ask(asker, "/ai has the battery on Demo Trail Tracker 3 been dropping?")
check("a tool with arguments works: one node's battery history", bool(row) and row["action"] == "node_history" and "Demo Trail Tracker 3" in row["response"] and "%" in row["response"], row)
row = ask(asker, "/ai what is a mesh router?")
check("an ordinary question is answered in plain chat, signed as the scripted model", bool(row) and row["status"] == "answered" and "scripted demo model" in row["response"], row)
visitor = next(n for n in br.iface.neighbours if n.spec.short == "KSK")
row = ask(visitor, "/ai how many nodes are around?")
check("a node without AI tools gets no lookups, as the access rules say", bool(row) and row["status"] == "answered" and not row["action"] and "can't look" in row["response"], row)
check("replies were sent through the fake radio only", until(lambda: len(br.iface.sent) >= 4, 90) and all(d.startswith("!d3") or d == "^all" for d, _ in br.iface.sent), br.iface.sent[:3])

# operator actions go to the fake radio too
cfg = rq.post(base + "/api/radio/config/pull", json={}, timeout=10)
check("pulling the radio's settings works", cfg.status_code == 200 and cfg.json().get("backup_id"), cfg.text[:200])
tid = rq.post(base + "/api/traceroute/request", json={"node": by_short(br.iface, "LAKE").id, "hop_limit": 7}, timeout=10).json().get("id")
done = until(lambda: (lambda s: s if s["status"] != "waiting" else None)(get("/api/traceroute/request?id=%s" % tid).json()), 90)
check("a traceroute from the dashboard completes against the fake mesh", bool(done) and done["status"] == "ok" and done["trace"]["path_towards"], done)
post = rq.post(base + "/api/channel/post", json={"text": "hello from the demo operator"}, timeout=10)
check("posting on the public channel goes to the fake radio and is 'heard'", post.status_code == 200 and until(lambda: any(m["text"] == "hello from the demo operator" and m["status"] == "heard" for m in get("/api/channel").json()["messages"]), 30), post.text)

# ---- nothing leaked, and a clean shutdown ----------------------------------------------------------------------------
check("the real audit.db was not created or touched", (os.path.exists(REAL_DB) == (real_before is not None)) and (real_before is None or os.path.getmtime(REAL_DB) == real_before))
check("the temporary database is the one in use", os.path.exists(args.db))
br.stop()
runner.join(30)
check("the bridge returns from run() when asked to stop", not runner.is_alive())
check("...the radio is detached and closed", br.iface is None and not tr._threads[0].is_alive() and not tr._threads[1].is_alive() and not br.demo["radio"]._rxThread.is_alive())
check("...the dashboard no longer answers and the fake Ollama has gone", until(lambda: socket.socket().connect_ex(("127.0.0.1", port)) != 0, 30) and socket.socket().connect_ex(("127.0.0.1", int(args.ollama_url.rsplit(":", 1)[1]))) != 0)
check("...and the temporary folder is deleted", os.name == "nt" or until(lambda: not os.path.exists(demo_dir), 30), demo_dir)
check("nothing ever tried to reach beyond this machine", remote_connects == [] and serial_calls == [], (remote_connects, serial_calls))

# ---- the real command line: python -m meshllm --demo ----------------------------------------------------------------
scratch = tempfile.mkdtemp(prefix="demo_cli_")
demo_dirs = lambda: {d for d in os.listdir(tempfile.gettempdir()) if d.startswith("meshllm_demo_")}
RUNNING = "Demo mode is running"
POSIX = os.name != "nt"

def run_cli(flags, setup="", sig=None, wait_running=True, wait_status=False):
    """Run `python -m meshllm --demo <flags>` as a child. `setup` is Python run before it starts (to interrupt it half way through
    starting); `sig` is sent once the "Demo mode is running" line has been printed (and, with wait_status, the dashboard answers).
    Returns (exit code, output, status or None)."""
    code = ("import os, runpy, signal, sys, time\n"
            "signal.signal(signal.SIGINT, signal.default_int_handler)\n"            # a test runner may have started us with SIGINT ignored
            + setup +
            "sys.argv = ['meshllm', '--demo'] + sys.argv[1:]\n"
            "runpy.run_module('meshllm', run_name='__main__')\n")
    path = os.path.join(scratch, "out%d.txt" % len(os.listdir(scratch)))
    st = None
    with open(path, "wb") as out:
        proc = subprocess.Popen([sys.executable, "-u", "-c", code] + flags, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT)
        text = lambda: open(path, encoding="utf-8", errors="replace").read()
        if wait_running:
            until(lambda: RUNNING in text() or proc.poll() is not None, 120)
        if wait_status:
            def cli_status():
                if proc.poll() is not None: return False
                try:
                    r = rq.get("http://127.0.0.1:%s/api/status" % flags[flags.index("--web-port") + 1], timeout=3).json()
                except Exception:
                    return None
                return r if r.get("connected") else None
            st = until(cli_status, 120)
        if sig is not None and proc.poll() is None:
            proc.send_signal(sig) if sig != "terminate" else proc.terminate()
        try:
            rc_code = proc.wait(60)
        except subprocess.TimeoutExpired:
            proc.kill(); rc_code = "timeout"
    return rc_code, text(), st
def folder_of(text):
    m = re.search(r"logs: (\S+)", text)
    return m.group(1) if m else None

port2 = free_port()
before = demo_dirs()
rc_code, text, st = run_cli(["--demo-scripted", "--web-port", str(port2), "--port", "COM9", "--model", "some-model"], sig=signal.SIGINT if POSIX else "terminate", wait_status=True)
folder = folder_of(text)
check("python -m meshllm --demo starts with the banner and a connected simulated radio", bool(st) and st["demo"] and "DEMO MODE: simulated radio and mesh; nothing is transmitted" in text, text[-600:])
check("...says where its temporary files are, and that the real database is not used", bool(folder) and "temporary" in text and "real audit.db is not used" in text, text[:600])
check("...says --port and --model are ignored, and still uses the demo radio and model", "--port, --model are ignored" in text and st and st["port"] == "demo" and st["model"] == "demo-scripted", text[:400])
check("...and stops cleanly on Ctrl+C, leaving no folder behind", not POSIX or (rc_code == 0 and bool(folder) and not os.path.exists(folder)), (rc_code, text[-400:]))
for flag in (["--tcp", "radio.invalid"], ["--ble", "AA:BB:CC:DD:EE:FF"]):
    # the connection flags are ignored in demo mode: the simulated radio must still be reported as connected (wait_status requires it)
    rc_code, text, st = run_cli(["--demo-scripted", "--web-port", str(free_port())] + flag, sig=signal.SIGINT if POSIX else "terminate", wait_status=True)
    check(f"--demo {flag[0]} is ignored with a note and the demo radio still shows connected", bool(st) and st["connected"] and st["port"] == "demo" and f"{flag[0]} is ignored" in text, (st, text[:400]))
if POSIX:
    rc_code, text, st = run_cli(["--demo-scripted", "--web-port", str(free_port())], sig=signal.SIGTERM, wait_status=True)
    check("kill (SIGTERM) stops the demo cleanly and deletes the folder", rc_code == 0 and bool(folder_of(text)) and not os.path.exists(folder_of(text)) and "Traceback" not in text, (rc_code, text[-400:]))
    rc_code, text, st = run_cli(["--demo-scripted", "--web-port", str(free_port())], sig=signal.SIGHUP, wait_status=True)
    check("closing the terminal (SIGHUP) does the same", rc_code == 0 and bool(folder_of(text)) and not os.path.exists(folder_of(text)) and "Traceback" not in text, (rc_code, text[-400:]))
    # an interrupt that lands while the demo is still starting up (the patched step signals its own process, then waits)
    for name, sig in (("Ctrl+C", "SIGINT"), ("SIGTERM", "SIGTERM")):
        setup = "import meshllm.demo as d\nd.seed_history = lambda *a, **k: (os.kill(os.getpid(), signal.%s), time.sleep(60))\n" % sig
        rc_code, text, st = run_cli(["--demo-scripted", "--web-port", str(free_port())], setup=setup, wait_running=False)
        check("%s during start-up still deletes the temporary folder and exits cleanly" % name,
              rc_code == 0 and bool(folder_of(text)) and not os.path.exists(folder_of(text)) and RUNNING not in text and "Traceback" not in text, (rc_code, text[-500:]))
    setup = "import meshllm.bridge as bb\nbb.Bridge.run = lambda self: (_ for _ in ()).throw(KeyboardInterrupt())\n"
    rc_code, text, st = run_cli(["--demo-scripted", "--web-port", str(free_port())], setup=setup, wait_running=False)
    check("an interrupt before the bridge even runs leaves no folder either", rc_code == 0 and bool(folder_of(text)) and not os.path.exists(folder_of(text)), (rc_code, text[-300:]))
# the web port is already taken (for example by the real bridge)
taken = socket.socket(); taken.bind(("127.0.0.1", 0)); taken.listen(1); busy = taken.getsockname()[1]
rc_code, text, st = run_cli(["--demo-scripted", "--web-port", str(busy)], wait_running=False)
taken.close()
check("a busy web port gives one friendly line, a failure exit code and no leftover folder", rc_code == 1 and ("Port %d is already in use" % busy) in text and "--web-port" in text
      and "Traceback" not in text and bool(folder_of(text)) and not os.path.exists(folder_of(text)), (rc_code, text[-500:]))
rc_code, text, st = run_cli(["--demo-scripted", "--web-host", "0.0.0.0", "--web-port", str(free_port())], wait_running=False)
check("--web-host 0.0.0.0 is refused in demo mode, before anything is created", rc_code == 2 and "only listens on this computer" in text and "DEMO MODE" not in text, (rc_code, text[-300:]))
check("no demo folder was left behind by any of those runs", demo_dirs() == before, demo_dirs() - before)
shutil.rmtree(scratch, ignore_errors=True)
check("the real audit.db was still not touched by the command-line run", (os.path.exists(REAL_DB) == (real_before is not None)) and (real_before is None or os.path.getmtime(REAL_DB) == real_before))

print(f"\n{len(fails)} failed" if fails else "\nall passed")
sys.stdout.flush(); os._exit(1 if fails else 0)
