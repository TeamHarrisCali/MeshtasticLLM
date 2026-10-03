"""The Docker files: static, text-based checks (no Docker daemon, no PyYAML, no network) that the safety decisions in them stay in place.

The dashboard has no login yet, so the compose file must publish it on the host's loopback only, never publish Ollama, and the image
must run as a numeric non-root user from a base image pinned by digest. A real build and run is done by the `docker` job in CI."""
import os, re, shutil, subprocess, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)

def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()

def code_lines(text):
    """The lines that are not comments or blank (so a warning *about* 0.0.0.0 in a comment is not mistaken for a setting)."""
    return [l for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]

def service_blocks(compose):
    """{service name: its lines} from the top-level `services:` section, by indentation only (no YAML parser needed)."""
    blocks, name, inside = {}, None, False
    for line in code_lines(compose):
        if re.match(r"^\S", line):
            inside = line.startswith("services:")
            name = None
            continue
        if not inside:
            continue
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            name = m.group(1); blocks[name] = []
        elif name:
            blocks[name].append(line)
    return blocks

compose = read("docker-compose.yml")
usb = read("docker-compose.usb.yml")
dockerfile = read("Dockerfile")
ignore = read(".dockerignore")
entry = read("docker", "entrypoint.sh")
env_example = read(".env.example")
blocks = service_blocks(compose)

# ---- compose: who can reach what ------------------------------------------------------------------------------------------------
check("the compose file has the bridge, ollama and demo services", {"bridge", "ollama", "demo"} <= set(blocks), sorted(blocks))
for svc in ("bridge", "demo"):
    ports = [l.strip().lstrip("- ").strip('"\'') for l in blocks.get(svc, []) if re.match(r"^\s+-\s*[\"']?[\d${]", l) and ":" in l and "8080" in l]
    check(f"{svc}: the dashboard port is published, and only on the host's loopback (127.0.0.1)", bool(ports) and all(p.startswith("127.0.0.1:") for p in ports), ports)
port_lines = [l.strip().lstrip("- ").strip("\"'") for l in code_lines(compose) if re.match(r"^\s+-\s*[\"']?[\d$][^\s]*:\d+[\"']?\s*$", l)]
check("every published port in the compose file is on 127.0.0.1 (and there are some)", len(port_lines) >= 2 and all(p.startswith("127.0.0.1:") for p in port_lines), port_lines)
check("ollama has no `ports:` entry (its port stays on the compose network)", "ports:" not in "\n".join(blocks.get("ollama", [])) and bool(blocks.get("ollama")))
check("nothing uses host networking, a published range, or `expose` for ollama", "network_mode" not in compose and "expose:" not in compose)
check("ollama is pinned by an explicit version tag, not latest", re.search(r"image:\s*ollama/ollama:\d+\.\d+\.\d+\s*$", "\n".join(blocks.get("ollama", [])), re.M) is not None)
check("the demo service is behind the `demo` profile and runs --demo", "profiles: [demo]" in "\n".join(blocks.get("demo", [])) and '"--demo"' in "\n".join(blocks.get("demo", [])))
check("the demo does not depend on ollama", "ollama" not in "\n".join(blocks.get("demo", [])))
check("the bridge keeps its data in a named volume mounted at /data", "meshllm-data:/data" in "\n".join(blocks.get("bridge", [])))
check("the bridge and the demo get the hardening block", all("<<: *hardening" in "\n".join(blocks.get(s, [])) for s in ("bridge", "demo")))
check("hardening: all capabilities dropped, no-new-privileges, read-only root, a tmpfs /tmp",
      all(s in compose for s in ("cap_drop: [ALL]", "no-new-privileges:true", "read_only: true", 'tmpfs: ["/tmp"]')))
check("the compose file carries a warning not to publish on 0.0.0.0 or the LAN before a login exists",
      re.search(r"WARNING: do NOT change that address to 0\.0\.0\.0", compose) is not None and "NO LOGIN" in compose and "LAN" in compose)
check("the USB override passes one device and one group, and is not privileged", "devices:" in usb and "group_add:" in usb and "privileged" not in "\n".join(code_lines(usb)) and "/dev/ttyUSB0" in usb)
check("the USB override says it is for Linux hosts only", "LINUX HOSTS ONLY" in usb)

# ---- Dockerfile -----------------------------------------------------------------------------------------------------------------
froms = [l for l in code_lines(dockerfile) if l.startswith("FROM ")]
check("one FROM, with a tag and an @sha256: digest", len(froms) == 1 and re.match(r"^FROM python:[\w.-]+@sha256:[0-9a-f]{64}$", froms[0]) is not None, froms)
users = [l.split()[1] for l in code_lines(dockerfile) if l.startswith("USER ")]
check("the image ends running as a numeric, non-root user", bool(users) and re.fullmatch(r"[1-9]\d*(:[1-9]\d*)?", users[-1]) is not None, users)
check("dependencies are installed with pip --no-cache-dir from requirements.txt", "pip install --no-cache-dir -r requirements.txt" in dockerfile)
check("it logs unbuffered (to stdout) and does not write bytecode into the read-only code folder", "PYTHONUNBUFFERED=1" in dockerfile and "PYTHONDONTWRITEBYTECODE=1" in dockerfile)
check("it has a healthcheck on /api/status using python's urllib, EXPOSEs 8080 and uses the entrypoint script",
      "HEALTHCHECK" in dockerfile and "urllib.request" in dockerfile and "127.0.0.1:8080/api/status" in dockerfile and "EXPOSE 8080" in dockerfile and "docker/entrypoint.sh" in dockerfile)
copies = [l for l in code_lines(dockerfile) if l.startswith("COPY ")]
check("COPY lists what is needed and never copies the whole folder", not any(re.match(r"^COPY\s+(--\S+\s+)*\.\s", l) for l in copies)
      and all(any(w in l for l in copies) for w in ("requirements.txt", "meshllm/", "docs/", "eval_results/", "LICENSE")), copies)
check("/data exists and is owned by the app user", "mkdir /data" in dockerfile and "chown 10001:10001 /data" in dockerfile)

# ---- .dockerignore ----------------------------------------------------------------------------------------------------------------
ign = set(l.strip() for l in code_lines(ignore))
for needed in ("audit.db", "backups", "logs", "tile_cache", "private", ".git", ".venv", "tests", "__pycache__", ".claude", "*.db", ".env"):
    check(f".dockerignore excludes {needed}", needed in ign, sorted(ign))
check(".env is git-ignored, .env.example is not", re.search(r"^\.env$", read(".gitignore"), re.M) is not None and ".env.example" not in "\n".join(code_lines(read(".gitignore"))))

# ---- entrypoint -------------------------------------------------------------------------------------------------------------------
check("the entrypoint ends with `exec python -m meshllm` so signals reach Python", re.search(r"^exec python -m meshllm\b", entry, re.M) is not None)
check("the entrypoint never runs eval or `sh -c` on the environment", not re.search(r"\beval\b|\bsh -c\b|\bbash -c\b", "\n".join(code_lines(entry))))
for var in ("MESHLLM_TCP", "MESHLLM_PORT", "MESHLLM_FALLBACK", "MESHLLM_OLLAMA_URL", "MESHLLM_MODEL", "MESHLLM_EXTRA_ARGS"):
    check(f"the entrypoint reads {var}", var in entry)
check("the entrypoint binds the container's own 0.0.0.0 and puts the database in /data", "--web-host 0.0.0.0" in entry and '"${MESHLLM_DATA_DIR:-/data}"' in entry and "audit.db" in entry)

# run the entrypoint for real with a stand-in `python` that prints what it was asked to run (POSIX with bash only)
bash = shutil.which("bash")
if bash and os.name == "posix":
    tmp = tempfile.mkdtemp(prefix="entry_")
    real = sys.executable
    fake = os.path.join(tmp, "python")
    with open(fake, "w") as f:
        f.write('#!/bin/sh\nif [ "$1" = "-c" ]; then exec "%s" "$@"; fi\nfor a in "$@"; do printf "[%%s]" "$a"; done; echo\n' % real)
    os.chmod(fake, 0o755)

    datadir = os.path.join(tmp, "data")
    os.makedirs(datadir)

    def run_entry(extra_env, argv=()):
        env = {"PATH": tmp + os.pathsep + os.environ.get("PATH", ""), "MESHLLM_DATA_DIR": datadir}
        env.update(extra_env)
        r = subprocess.run([bash, os.path.join(ROOT, "docker", "entrypoint.sh"), *argv], env=env, capture_output=True, text=True, timeout=30)
        return r.returncode, r.stdout.strip()

    rc, out = run_entry({})
    check("entrypoint, nothing set: the defaults only", rc == 0 and out == "[-m][meshllm][--web-host][0.0.0.0][--ollama-url][http://ollama:11434][--db][" + datadir + "/audit.db]", (rc, out))
    rc, out = run_entry({"MESHLLM_TCP": "192.0.2.7:4403", "MESHLLM_PORT": "/dev/ttyUSB0", "MESHLLM_MODEL": "llama3.2:3b", "MESHLLM_OLLAMA_URL": "http://other:1"})
    check("entrypoint maps each variable to its flag", rc == 0 and out.endswith("[--tcp][192.0.2.7:4403][--port][/dev/ttyUSB0][--model][llama3.2:3b]") and "[--ollama-url][http://other:1]" in out, (rc, out))
    rc, out = run_entry({"MESHLLM_TCP": "192.0.2.7", "MESHLLM_FALLBACK": "usb:/dev/ttyUSB1,,tcp:[fe80::1]:4403,"})
    check("MESHLLM_FALLBACK becomes one --fallback per comma-separated entry, in order, empty entries skipped",
          rc == 0 and out.endswith("[--tcp][192.0.2.7][--fallback][usb:/dev/ttyUSB1][--fallback][tcp:[fe80::1]:4403]"), (rc, out))
    rc, out = run_entry({"MESHLLM_FALLBACK": "usb:/dev/ttyUSB1"}, ["--demo"])
    check("MESHLLM_FALLBACK is left out in --demo, like the other connection variables", rc == 0 and "--fallback" not in out, (rc, out))
    rc, out = run_entry({"MESHLLM_EXTRA_ARGS": "--daily-cap 20 --command '/a b' \"--x=$(echo hi)\""})
    check("MESHLLM_EXTRA_ARGS is split like a shell would, and nothing in it is executed", rc == 0 and out.endswith("[--daily-cap][20][--command][/a b][--x=$(echo hi)]"), (rc, out))
    rc, out = run_entry({"MESHLLM_EXTRA_ARGS": "--oops 'unterminated"})
    check("a malformed MESHLLM_EXTRA_ARGS stops the container instead of starting with half the flags", rc != 0 and "meshllm" not in out, (rc, out))
    rc, out = run_entry({"MESHLLM_TCP": "192.0.2.7", "MESHLLM_MODEL": "x"}, ["--demo", "--demo-scripted"])
    check("for --demo the radio, database and model settings are left out and the command's arguments come last",
          rc == 0 and out == "[-m][meshllm][--web-host][0.0.0.0][--ollama-url][http://ollama:11434][--demo][--demo-scripted]", (rc, out))
    if hasattr(os, "geteuid") and os.geteuid() != 0:        # root can write anywhere, so this check cannot be made as root
        os.chmod(datadir, 0o500)
        rc, out = run_entry({})
        check("an unwritable data folder gives exit 1 and no command line, not a database traceback", rc == 1 and out == "", (rc, out))
        r = subprocess.run([bash, os.path.join(ROOT, "docker", "entrypoint.sh")], env={"PATH": tmp + os.pathsep + os.environ.get("PATH", ""), "MESHLLM_DATA_DIR": datadir},
                           capture_output=True, text=True, timeout=30)
        check("...and says so in one line that points at docs/setup.md", "is not writable by uid" in r.stderr and "docs/setup.md" in r.stderr and len(r.stderr.strip().splitlines()) == 1, r.stderr)
        os.chmod(datadir, 0o700)
    rc, out = run_entry({}, ["--demo"])
    check("--demo does not need the data folder", rc == 0 and "--db" not in out, (rc, out))
    shutil.rmtree(tmp, ignore_errors=True)
else:
    print("SKIP running the entrypoint (needs bash on a POSIX system)")

# ---- secrets and real mesh data ----------------------------------------------------------------------------------------------------
suspicious = re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|pin)\b\s*[=:]\s*[\"']?[^\s\"'$#{]+|[0-9A-F]{2}(:[0-9A-F]{2}){5}|![0-9a-f]{8}\b|[\w.+-]+@[\w-]+\.[a-z]{2,}")
for name, text in (("Dockerfile", dockerfile), ("docker-compose.yml", compose), ("docker-compose.usb.yml", usb), (".env.example", env_example),
                   ("docker/entrypoint.sh", entry), (".dockerignore", ignore)):
    hits = [m.group(0) for l in code_lines(text) for m in [suspicious.search(l)] if m]
    check(f"{name}: no PIN, password, token, node id, Bluetooth address or e-mail address", not hits, hits)
check(".env.example sets nothing by default (every line is a comment) so it cannot hold a secret", all(l.lstrip().startswith("#") for l in env_example.splitlines() if l.strip()))

# ---- CI and Dependabot ------------------------------------------------------------------------------------------------------------
workflow = read(".github", "workflows", "tests.yml")
check("CI has a docker job that builds the image and does not log in to or push to a registry",
      re.search(r"^  docker:\s*$", workflow, re.M) is not None and "docker build" in workflow and "docker login" not in workflow and "docker push" not in workflow and "push: true" not in workflow)
check("CI keeps the token read-only", re.search(r"^permissions:\s*\n\s+contents: read\s*$", workflow, re.M) is not None and "write" not in "\n".join(code_lines(workflow)))
dep = read(".github", "dependabot.yml")
check("Dependabot watches the docker ecosystem monthly with the ci prefix", re.search(r'package-ecosystem: "docker"\s*\n\s+directory: "/"\s*\n\s+schedule:\s*\n\s+interval: "monthly"\s*\n\s+commit-message:\s*\n\s+prefix: "ci"', dep) is not None)
check("Dependabot also watches the compose file's image tags", 'package-ecosystem: "docker-compose"' in dep)

# ---- the two small code changes the image needs ---------------------------------------------------------------------------------------
from meshllm import demo, bridge
check("--demo may bind 0.0.0.0 only inside the container (MESHLLM_CONTAINER=1)", demo.web_host_allowed("0.0.0.0", {"MESHLLM_CONTAINER": "1"}) and not demo.web_host_allowed("0.0.0.0", {}))
check("--demo still refuses a LAN address or :: even inside the container, and always allows loopback",
      not demo.web_host_allowed("192.168.1.5", {"MESHLLM_CONTAINER": "1"}) and not demo.web_host_allowed("::", {"MESHLLM_CONTAINER": "1"}) and demo.web_host_allowed("127.0.0.1", {}))
try:
    bridge._stop_on_sigterm(15, None)
    raised = False
except KeyboardInterrupt:
    raised = True
check("the SIGTERM handler ends the bridge like Ctrl+C (so `docker stop` is a clean, quick stop)", raised)
# main() must actually INSTALL the SIGTERM handler (a handler nobody installs would leave `docker stop` waiting 10 s)
import argparse, signal, http.client
saved_attrs = {n: getattr(bridge, n) for n in ("parse_cli", "apply_staged_restore", "Bridge")}
old_handler = signal.getsignal(signal.SIGTERM)
seen = {}
class FakeBridge:
    def __init__(self, args): pass
    def run(self): seen["handler"] = signal.getsignal(signal.SIGTERM)
try:
    bridge.parse_cli = lambda argv=None: (None, argparse.Namespace(ble_scan=False, ble=None, demo=False, db="x.db"))
    bridge.apply_staged_restore = lambda db: None
    bridge.Bridge = FakeBridge
    bridge.main()
finally:
    for n, v in saved_attrs.items(): setattr(bridge, n, v)
    signal.signal(signal.SIGTERM, old_handler)
check("main() installs the SIGTERM handler before the bridge runs", seen.get("handler") is bridge._stop_on_sigterm, seen)

# the dashboard must not accept a Host equal to a wildcard bind address (0.0.0.0 / ::), but still accepts loopback
sys.path.insert(0, os.path.join(ROOT, "tests"))
from fixture import make
br, radio, _tmp = make(web_port=18461 + os.getpid() % 500)
port = br.args.web_port
def status_with_host(host):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest("GET", "/api/status", skip_host=True); c.putheader("Host", host); c.endheaders()
    code = c.getresponse().status; c.close(); return code
br.args.web_host = "0.0.0.0"                       # as in the container (the test server itself listens on loopback only)
check("bind 0.0.0.0: Host 0.0.0.0:PORT is refused (403)", status_with_host("0.0.0.0:%d" % port) == 403)
check("bind 0.0.0.0: Host 127.0.0.1:PORT is still accepted", status_with_host("127.0.0.1:%d" % port) == 200)
br.args.web_host = "::"
check("bind ::: Host [::]:PORT is refused", status_with_host("[::]:%d" % port) == 403)
br.args.web_host = "dash.example"
check("an explicitly named non-wildcard host is still accepted", status_with_host("dash.example:%d" % port) == 200)
check("another name is refused", status_with_host("evil.example:%d" % port) == 403)

check("the Dockerfile sets MESHLLM_CONTAINER=1 (what lets the demo bind 0.0.0.0 in the container)", "MESHLLM_CONTAINER=1" in dockerfile)
NOTE = "-p 127.0.0.1:8080:8080"
check("the Dockerfile, setup.md and README all say to publish a plain `docker run` as 127.0.0.1 only, never -p 8080:8080",
      all(NOTE in t and "never `-p 8080:8080`" in t for t in (dockerfile, read("docs", "setup.md"), read("README.md"))))

print(f"\n{len(fails)} failed" if fails else "\nall passed")
sys.stdout.flush(); os._exit(1 if fails else 0)
