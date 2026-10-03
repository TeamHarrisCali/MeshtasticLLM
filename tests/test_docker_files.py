"""The Docker files: static, text-based checks (no Docker daemon, no PyYAML, no network) that the safety decisions in them stay in place.

By default the dashboard has no login, so the compose file must publish it on the host's loopback only, never publish Ollama, and the image
must run as a numeric non-root user from a base image pinned by digest. The login comes from a Compose secret file (docker-compose.login.yml),
the LAN is an explicit opt-in that the entrypoint refuses without that login, and the healthcheck must work with a login. A real build and
run is done by the `docker` job in CI."""
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
login_yml = read("docker-compose.login.yml")
dockerfile = read("Dockerfile")
ignore = read(".dockerignore")
entry = read("docker", "entrypoint.sh")
env_example = read(".env.example")
blocks = service_blocks(compose)

# ---- compose: who can reach what ------------------------------------------------------------------------------------------------
check("the compose file has the bridge, ollama and demo services", {"bridge", "ollama", "demo"} <= set(blocks), sorted(blocks))
LOOPBACK_DEFAULT = "${MESHLLM_WEB_BIND:-127.0.0.1}"
def published(svc):
    return [l.strip().lstrip("- ").strip('"\'') for l in blocks.get(svc, []) if re.match(r"^\s+-\s*[\"']?[\d${]", l) and ":" in l and "8080" in l]
check("bridge: the dashboard port is published, and only on 127.0.0.1 unless the one opt-in variable MESHLLM_WEB_BIND says otherwise",
      published("bridge") == [LOOPBACK_DEFAULT + ":${MESHLLM_WEB_PORT:-8080}:8080"], published("bridge"))
check("demo: the dashboard port is published on the literal 127.0.0.1, with no variable that could change the address",
      published("demo") == ["127.0.0.1:${MESHLLM_WEB_PORT:-8080}:8080"], published("demo"))
port_lines = [l.strip().lstrip("- ").strip("\"'") for l in code_lines(compose) if re.match(r"^\s+-\s*[\"']?[\d$][^\s]*:\d+[\"']?\s*$", l)]
check("every published port in the compose file is on 127.0.0.1 by default (and there are some)",
      len(port_lines) >= 2 and all(p.startswith("127.0.0.1:") or p.startswith(LOOPBACK_DEFAULT + ":") for p in port_lines), port_lines)
check("MESHLLM_WEB_BIND is used for the bridge's port ONLY, defaults to 127.0.0.1 and is never given an empty or wildcard default",
      compose.count("MESHLLM_WEB_BIND") >= 3 and len(re.findall(r"\$\{MESHLLM_WEB_BIND[^}]*\}", "\n".join(code_lines(compose)))) == 2
      and all(m == "${MESHLLM_WEB_BIND:-127.0.0.1}" for m in re.findall(r"\$\{MESHLLM_WEB_BIND[^}]*\}", "\n".join(code_lines(compose))))
      and "MESHLLM_WEB_BIND" not in "\n".join(blocks.get("demo", [])))
check("the bridge is told the address it is published on, from the SAME variable as the ports line, so the two cannot differ",
      'MESHLLM_PUBLISH_ADDR: "${MESHLLM_WEB_BIND:-127.0.0.1}"' in "\n".join(blocks.get("bridge", [])) and 'MESHLLM_ALLOWED_HOSTS: "${MESHLLM_ALLOWED_HOSTS:-}"' in "\n".join(blocks.get("bridge", [])))
check("no compose file or the Dockerfile binds or publishes on 0.0.0.0 or [::] in a code line",
      not any(re.search(r"0\.0\.0\.0|\[::\]", l) for t in (compose, usb, login_yml, dockerfile) for l in code_lines(t) if not l.startswith("ENV")))
check("ollama has no `ports:` entry (its port stays on the compose network)", "ports:" not in "\n".join(blocks.get("ollama", [])) and bool(blocks.get("ollama")))
check("nothing uses host networking, a published range, or `expose` for ollama", "network_mode" not in compose and "expose:" not in compose)
check("ollama is pinned by an explicit version tag, not latest", re.search(r"image:\s*ollama/ollama:\d+\.\d+\.\d+\s*$", "\n".join(blocks.get("ollama", [])), re.M) is not None)
check("the demo service is behind the `demo` profile and runs --demo", "profiles: [demo]" in "\n".join(blocks.get("demo", [])) and '"--demo"' in "\n".join(blocks.get("demo", [])))
check("the demo does not depend on ollama", "ollama" not in "\n".join(blocks.get("demo", [])))
check("the bridge keeps its data in a named volume mounted at /data", "meshllm-data:/data" in "\n".join(blocks.get("bridge", [])))
check("the bridge and the demo get the hardening block", all("<<: *hardening" in "\n".join(blocks.get(s, [])) for s in ("bridge", "demo")))
check("hardening: all capabilities dropped, no-new-privileges, read-only root, a tmpfs /tmp",
      all(s in compose for s in ("cap_drop: [ALL]", "no-new-privileges:true", "read_only: true", 'tmpfs: ["/tmp"]')))
check("the compose file warns not to hand-edit the address to 0.0.0.0 or the LAN, and says the LAN needs the login",
      re.search(r"WARNING: do NOT hand-edit the \"ports:\" address to 0\.0\.0\.0", compose) is not None and "NO LOGIN" in compose and "LAN" in compose and "docker-compose.login.yml" in compose and "REFUSES TO START" in compose)
check("the USB override passes one device and one group, and is not privileged", "devices:" in usb and "group_add:" in usb and "privileged" not in "\n".join(code_lines(usb)) and "/dev/ttyUSB0" in usb)
check("the USB override says it is for Linux hosts only", "LINUX HOSTS ONLY" in usb)

# ---- Dockerfile -----------------------------------------------------------------------------------------------------------------
froms = [l for l in code_lines(dockerfile) if l.startswith("FROM ")]
check("one FROM, with a tag and an @sha256: digest", len(froms) == 1 and re.match(r"^FROM python:[\w.-]+@sha256:[0-9a-f]{64}$", froms[0]) is not None, froms)
users = [l.split()[1] for l in code_lines(dockerfile) if l.startswith("USER ")]
check("the image ends running as a numeric, non-root user", bool(users) and re.fullmatch(r"[1-9]\d*(:[1-9]\d*)?", users[-1]) is not None, users)
check("dependencies are installed with pip --no-cache-dir from requirements.txt", "pip install --no-cache-dir -r requirements.txt" in dockerfile)
check("it logs unbuffered (to stdout) and does not write bytecode into the read-only code folder", "PYTHONUNBUFFERED=1" in dockerfile and "PYTHONDONTWRITEBYTECODE=1" in dockerfile)
check("it has a healthcheck on the public /api/session route using python's urllib, EXPOSEs 8080 and uses the entrypoint script",
      "HEALTHCHECK" in dockerfile and "urllib.request" in dockerfile and "127.0.0.1:8080/api/session" in dockerfile and "EXPOSE 8080" in dockerfile and "docker/entrypoint.sh" in dockerfile)
copies = [l for l in code_lines(dockerfile) if l.startswith("COPY ")]
check("COPY lists what is needed and never copies the whole folder", not any(re.match(r"^COPY\s+(--\S+\s+)*\.\s", l) for l in copies)
      and all(any(w in l for l in copies) for w in ("requirements.txt", "meshllm/", "docs/", "eval_results/", "LICENSE")), copies)
check("/data exists and is owned by the app user", "mkdir /data" in dockerfile and "chown 10001:10001 /data" in dockerfile)

# ---- .dockerignore ----------------------------------------------------------------------------------------------------------------
ign = set(l.strip() for l in code_lines(ignore))
for needed in ("audit.db", "backups", "logs", "tile_cache", "private", ".git", ".venv", "tests", "__pycache__", ".claude", "*.db", ".env", "*.hash", "secrets"):
    check(f".dockerignore excludes {needed}", needed in ign, sorted(ign))
check(".env is git-ignored, .env.example is not", re.search(r"^\.env$", read(".gitignore"), re.M) is not None and ".env.example" not in "\n".join(code_lines(read(".gitignore"))))

# ---- entrypoint -------------------------------------------------------------------------------------------------------------------
check("the entrypoint ends with `exec python -m meshllm` so signals reach Python", re.search(r"^exec python -m meshllm\b", entry, re.M) is not None)
SH_C = "/bin/sh -c 'umask 277 && cat > \"$1\"' sh \"$secret_copy\""       # the one fixed script: the path is an argument, never part of the script text
check("the entrypoint never runs eval or `sh -c` on the environment (one constant `sh -c` writes the secret copy, the path is an argument)",
      SH_C in entry and not re.search(r"\beval\b|\bsh -c\b|\bbash -c\b", "\n".join(code_lines(entry)).replace(SH_C, "")))
for var in ("MESHLLM_TCP", "MESHLLM_PORT", "MESHLLM_FALLBACK", "MESHLLM_OLLAMA_URL", "MESHLLM_MODEL", "MESHLLM_EXTRA_ARGS", "MESHLLM_PUBLISH_ADDR", "MESHLLM_ALLOWED_HOSTS"):
    check(f"the entrypoint reads {var}", var in entry)
check("the entrypoint binds the container's own 0.0.0.0 and puts the database in /data", "--web-host 0.0.0.0" in entry and '"${MESHLLM_DATA_DIR:-/data}"' in entry and "audit.db" in entry)

# run the entrypoint for real with a stand-in `python` that prints what it was asked to run (POSIX with bash only)
bash = shutil.which("bash")
if bash and os.name == "posix":
    tmp = tempfile.mkdtemp(prefix="entry_")
    real = sys.executable
    fake = os.path.join(tmp, "python")
    # the stand-in python also records the two variables the app itself looks at (to FAKE_LOG, when that is set)
    with open(fake, "w") as f:
        f.write('#!/bin/sh\nif [ "$1" = "-c" ]; then exec "%s" "$@"; fi\n'
                '[ -z "$FAKE_LOG" ] || echo "PUBLISH_LAN=${MESHLLM_PUBLISH_LAN:-unset} HASHENV=${MESHLLM_PASSWORD_HASH_FILE:-unset}" >> "$FAKE_LOG"\n'
                'for a in "$@"; do printf "[%%s]" "$a"; done; echo\n' % real)
    os.chmod(fake, 0o755)
    # stand-ins for `id` (reports FAKE_UID / FAKE_GIDS) and `setpriv` (logs its options to FAKE_LOG, then runs the command as "uid 10001")
    with open(os.path.join(tmp, "id"), "w") as f:
        f.write('#!/bin/sh\ncase "$1" in -u) echo "${FAKE_UID:-1000}";; -G) echo "${FAKE_GIDS:-1000}";; *) exec /usr/bin/id "$@";; esac\n')
    with open(os.path.join(tmp, "setpriv"), "w") as f:
        f.write('#!/bin/sh\nopts=""\nwhile :; do case "$1" in --reuid|--regid|--groups) opts="$opts $1=$2"; shift 2;; --clear-groups) opts="$opts $1"; shift;; *) break;; esac; done\n'
                'echo "setpriv$opts" >> "$FAKE_LOG"\nFAKE_UID=10001 exec "$@"\n')
    for n in ("id", "setpriv"):
        os.chmod(os.path.join(tmp, n), 0o755)

    datadir = os.path.join(tmp, "data")
    os.makedirs(datadir)
    secrets_dir = os.path.join(tmp, "secrets"); os.makedirs(secrets_dir)
    run_dir = os.path.join(tmp, "run"); os.makedirs(run_dir)
    fake_log = os.path.join(tmp, "fake.log")
    SECRET_FILE = os.path.join(secrets_dir, "meshllm_admin_hash")
    COPY_FILE = os.path.join(run_dir, "admin.hash")
    HASH_TEXT = "not-a-real-hash-the-entrypoint-only-copies-bytes"      # the app validates the content, the entrypoint does not

    def run_entry(extra_env, argv=(), full=False):
        env = {"PATH": tmp + os.pathsep + os.environ.get("PATH", ""), "MESHLLM_DATA_DIR": datadir, "MESHLLM_SECRETS_DIR": secrets_dir,
               "MESHLLM_RUN_DIR": run_dir, "FAKE_LOG": fake_log}
        env.update(extra_env)
        r = subprocess.run([bash, os.path.join(ROOT, "docker", "entrypoint.sh"), *argv], env=env, capture_output=True, text=True, timeout=30)
        return (r.returncode, r.stdout.strip(), r.stderr) if full else (r.returncode, r.stdout.strip())

    def log_lines():
        try:
            with open(fake_log) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def reset(secret=None):
        for p in (SECRET_FILE, COPY_FILE, fake_log):
            if os.path.exists(p):
                os.chmod(p, 0o600); os.unlink(p)
        if secret is not None:
            with open(SECRET_FILE, "w") as f:
                f.write(secret)
            os.chmod(SECRET_FILE, 0o600)

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

    # ---- the login: the Compose secret, the root-then-10001 copy, and the LAN rule ---------------------------------------------------
    reset()
    rc, out = run_entry({})
    check("no secret and no login: no --password-hash-file, no --allowed-host, the app is not told about a LAN", rc == 0 and "--password-hash-file" not in out and "--allowed-host" not in out and "PUBLISH_LAN=unset" in " ".join(log_lines()), (rc, out))

    reset(HASH_TEXT)                                  # stage 2 alone (a user who can read the secret itself, e.g. the file was chowned to 10001)
    rc, out = run_entry({})
    check("a readable secret file: --password-hash-file points at it, and --allowed-host 127.0.0.1 is added (the app wants one with any login)",
          rc == 0 and "[--password-hash-file][" + SECRET_FILE + "][--allowed-host][127.0.0.1][--db]" in out and "PUBLISH_LAN=unset" in " ".join(log_lines()), (rc, out))
    check("the entrypoint passes the path as an argument and never puts the hash itself on the command line", HASH_TEXT not in out)
    os.chmod(SECRET_FILE, 0o000)
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        rc, out, err = run_entry({}, full=True)
        check("an unreadable secret (not root, host-owned 600) stops with a message naming the override file and the chown alternative, and starts nothing",
              rc == 1 and out == "" and "not readable by uid" in err and "docker-compose.login.yml" in err and "chown 10001" in err, (rc, out, err))
    os.chmod(SECRET_FILE, 0o600)
    rc, out = run_entry({"MESHLLM_ALLOWED_HOSTS": " 192.0.2.10 ,,radio.test, "})
    check("MESHLLM_ALLOWED_HOSTS becomes one --allowed-host each (spaces and empty entries dropped); with a loopback publish no extra 127.0.0.1",
          rc == 0 and "[--password-hash-file][" + SECRET_FILE + "][--allowed-host][192.0.2.10][--allowed-host][radio.test][--db]" in out, (rc, out))

    # stage 1: started as root with the secret mounted (docker-compose.login.yml)
    reset(HASH_TEXT)
    rc, out, err = run_entry({"FAKE_UID": "0", "FAKE_GIDS": "0 20 44"}, full=True)
    with open(COPY_FILE) as f:
        copied = f.read()
    mode = os.stat(COPY_FILE).st_mode & 0o777
    check("root start: the hash is copied byte for byte, into a file with mode 0400, and the final command reads the COPY (not the host's file)",
          rc == 0 and copied == HASH_TEXT and mode == 0o400 and "[--password-hash-file][" + COPY_FILE + "]" in out and SECRET_FILE not in out, (rc, out, err, oct(mode)))
    check("root start: it drops to 10001:10001 and keeps Docker's group_add groups (20, 44) but not root's group 0, twice (the copy and the re-exec)",
          log_lines().count("setpriv --reuid=10001 --regid=10001 --groups=20,44") == 2 and all("--groups=20,44" in l for l in log_lines() if "--groups" in l), log_lines())
    check("root start: the bridge itself runs after the drop (the stand-in python ran once, as the re-exec'd script)", out.count("[-m][meshllm]") == 1 and any(l.startswith("PUBLISH_LAN=unset") for l in log_lines()), (out, log_lines()))
    reset(HASH_TEXT)
    rc, out, err = run_entry({"FAKE_UID": "0", "FAKE_GIDS": "0"}, full=True)
    check("root start with no supplementary groups: --clear-groups", rc == 0 and "setpriv --reuid=10001 --regid=10001 --clear-groups" in log_lines(), (rc, err, log_lines()))
    reset()
    rc, out, err = run_entry({"FAKE_UID": "0"}, full=True)
    check("root start WITHOUT the secret refuses to run the bridge as root (exit 1, nothing started)", rc == 1 and out == "" and "refusing to run the bridge as root" in err, (rc, out, err))
    rc, out, err = run_entry({"FAKE_UID": "0"}, ["--demo"], full=True)
    check("...and so does --demo", rc == 1 and out == "" and "as root" in err, (rc, out, err))
    reset("")
    rc, out, err = run_entry({"FAKE_UID": "0"}, full=True)
    check("root start with an empty secret file: exit 1 and says so", rc == 1 and out == "" and "is empty" in err, (rc, out, err))
    reset(HASH_TEXT)
    os.makedirs(os.path.join(tmp, "elsewhere"))
    rc, out, err = run_entry({"FAKE_UID": "0", "MESHLLM_RUN_DIR": os.path.join(tmp, "no-such-dir")}, full=True)
    check("root start with no tmpfs to copy into: exit 1 with a message naming docker-compose.login.yml, nothing started", rc == 1 and out == "" and "docker-compose.login.yml" in err and "DAC_OVERRIDE" in err, (rc, out, err))

    # the LAN rule
    reset()
    for addr in ("127.0.0.1", "127.5.5.5", "::1", "[::1]"):
        rc, out = run_entry({"MESHLLM_PUBLISH_ADDR": addr})
        check(f"published on {addr} (this computer): no login needed, starts as before", rc == 0 and "[--web-host][0.0.0.0]" in out and "PUBLISH_LAN=unset" in " ".join(log_lines()), (rc, out))
    for addr in ("192.0.2.10", "0.0.0.0", "::", "[::]", "10.1.2.3", "localhost", "127.evil.test", "127.0.0.1.evil.test", "127.0.0", "fe80::1"):
        reset()
        rc, out, err = run_entry({"MESHLLM_PUBLISH_ADDR": addr, "MESHLLM_ALLOWED_HOSTS": "192.0.2.10"}, full=True)
        check(f"published on {addr} (not this computer) without a login: refuses to start (exit 78), says what to do, starts nothing",
              rc == 78 and out == "" and "needs the login" in err and "docker-compose.login.yml" in err and "Refusing to start" in err, (rc, out, err))
    reset(HASH_TEXT)
    rc, out, err = run_entry({"MESHLLM_PUBLISH_ADDR": "192.0.2.10"}, full=True)
    check("published on the LAN WITH a login but no MESHLLM_ALLOWED_HOSTS: refuses (exit 78) and asks for the names", rc == 78 and out == "" and "MESHLLM_ALLOWED_HOSTS" in err, (rc, out, err))
    rc, out, err = run_entry({"MESHLLM_PUBLISH_ADDR": "192.0.2.10", "MESHLLM_ALLOWED_HOSTS": " , "}, full=True)
    check("...also when the list is only separators", rc == 78 and out == "", (rc, out, err))
    rc, out, err = run_entry({"MESHLLM_PUBLISH_ADDR": "192.0.2.10", "MESHLLM_ALLOWED_HOSTS": "192.0.2.10,radio.test"}, full=True)
    check("published on the LAN with the login AND the allowed hosts: starts, passes both, and tells the app (MESHLLM_PUBLISH_LAN=1)",
          rc == 0 and "[--password-hash-file][" + SECRET_FILE + "][--allowed-host][192.0.2.10][--allowed-host][radio.test][--db]" in out and "PUBLISH_LAN=1" in " ".join(log_lines()), (rc, out, err, log_lines()))
    reset(HASH_TEXT)
    rc, out, err = run_entry({"MESHLLM_PUBLISH_ADDR": "192.0.2.10", "MESHLLM_ALLOWED_HOSTS": "192.0.2.10", "FAKE_UID": "0", "FAKE_GIDS": "0"}, full=True)
    check("the LAN rule also holds through the root copy stage (the re-exec'd script checks it as 10001)", rc == 0 and "PUBLISH_LAN=1" in " ".join(log_lines()) and "[--password-hash-file][" + COPY_FILE + "]" in out, (rc, out, err, log_lines()))
    reset()
    rc, out, err = run_entry({"MESHLLM_PUBLISH_ADDR": "192.0.2.10", "MESHLLM_ALLOWED_HOSTS": "192.0.2.10"}, ["--demo", "--demo-scripted"], full=True)
    check("--demo on a LAN-published address is refused (the demo has a send box and no login)", rc == 78 and out == "" and "--demo" in err, (rc, out, err))
    for spelled in ("--demo", "--demo --demo-scripted", "'--demo'", '"--demo"', "--daily-cap 5 --demo", "--de\\mo"):
        reset(HASH_TEXT)
        rc, out, err = run_entry({"MESHLLM_PUBLISH_ADDR": "192.0.2.10", "MESHLLM_ALLOWED_HOSTS": "192.0.2.10", "MESHLLM_EXTRA_ARGS": spelled}, full=True)
        check(f"a --demo that arrives through MESHLLM_EXTRA_ARGS ({spelled!r}) is refused on a LAN-published address too", rc == 78 and out == "" and "--demo" in err, (rc, out, err))
    reset(HASH_TEXT)
    rc, out, err = run_entry({"MESHLLM_EXTRA_ARGS": "--demo-scripted --command /demo"}, full=True)
    check("...but similar-looking extras (--demo-scripted alone) are not mistaken for --demo", rc == 0 and "--password-hash-file" in out, (rc, out, err))
    reset(HASH_TEXT)
    rc, out, err = run_entry({"MESHLLM_PUBLISH_ADDR": "192.0.2.10", "MESHLLM_ALLOWED_HOSTS": "192.0.2.10"}, ["--demo"], full=True)
    check("...even if a login exists (the demo ignores it)", rc == 78 and out == "", (rc, out, err))
    rc, out = run_entry({"MESHLLM_ALLOWED_HOSTS": "192.0.2.10"}, ["--demo", "--demo-scripted"])
    check("--demo on loopback ignores the secret and the allowed hosts entirely", rc == 0 and out == "[-m][meshllm][--web-host][0.0.0.0][--ollama-url][http://ollama:11434][--demo][--demo-scripted]", (rc, out))
    reset(HASH_TEXT)
    rc, out, err = run_entry({"MESHLLM_PASSWORD_HASH_FILE": os.path.join(tmp, "elsewhere", "missing")}, full=True)
    check("a MESHLLM_PASSWORD_HASH_FILE that does not exist is an error, not a silent no-login start", rc == 1 and out == "" and "not readable" in err, (rc, out, err))
    shutil.rmtree(tmp, ignore_errors=True)
else:
    print("SKIP running the entrypoint (needs bash on a POSIX system)")

# ---- secrets and real mesh data ----------------------------------------------------------------------------------------------------
suspicious = re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|pin)\b\s*[=:]\s*[\"']?[^\s\"'$#{]+|[0-9A-F]{2}(:[0-9A-F]{2}){5}|![0-9a-f]{8}\b|[\w.+-]+@[\w-]+\.[a-z]{2,}")
for name, text in (("Dockerfile", dockerfile), ("docker-compose.yml", compose), ("docker-compose.usb.yml", usb), ("docker-compose.login.yml", login_yml), (".env.example", env_example),
                   ("docker/entrypoint.sh", entry), (".dockerignore", ignore)):
    hits = [m.group(0) for l in code_lines(text) for m in [suspicious.search(l)] if m]
    check(f"{name}: no PIN, password, token, node id, Bluetooth address or e-mail address", not hits, hits)
check(".env.example sets nothing by default (every line is a comment) so it cannot hold a secret", all(l.lstrip().startswith("#") for l in env_example.splitlines() if l.strip()))

# ---- the login override (docker-compose.login.yml): a secret FILE, and exactly the three capabilities the copy needs ------------------
login_code = "\n".join(code_lines(login_yml))
lb = service_blocks(login_yml)
check("the login override touches the bridge service only", list(lb) == ["bridge"], list(lb))
check("the hash is a Compose secret: a `file:` secret whose path must be given in MESHLLM_ADMIN_HASH_FILE (an unset variable is a clear error)",
      re.search(r"^secrets:\s*\n\s+meshllm_admin_hash:\s*\n\s+file: \"\$\{MESHLLM_ADMIN_HASH_FILE:\?[^}]+\}\"\s*$", login_code, re.M) is not None
      and "meshllm_admin_hash" in "\n".join(lb["bridge"]))
check("the secret is never put in an environment variable: no environment: block in the override, and no compose file sets a password variable",
      "environment" not in login_code and not any(re.search(r"(?i)password|hash", l) for t in (compose, usb) for l in code_lines(t) if "MESHLLM_" in l))
check("the override adds exactly DAC_OVERRIDE, SETUID and SETGID (no CHOWN, no privileged, no SYS_ADMIN)",
      re.search(r"^\s+cap_add: \[DAC_OVERRIDE, SETUID, SETGID\]\s*$", login_code, re.M) is not None and not re.search(r"privileged|CHOWN|SYS_|NET_|FOWNER|ALL\b(?!\])", login_code))
check("the override starts the bridge container as root only for the copy (user 0:0), and still does not touch read_only, cap_drop or no-new-privileges",
      re.search(r'^\s+user: "0:0"\s*$', login_code, re.M) is not None and not re.search(r"read_only|cap_drop|security_opt|network_mode|ports:|privileged", login_code))
check("the copy goes to a memory-only tmpfs owned by 10001, mode 0700, noexec, small",
      re.search(r'"/run/meshllm:uid=10001,gid=10001,mode=0700,size=\dm,noexec,nosuid,nodev"', login_code) is not None)
check("the entrypoint's copy directory is that same /run/meshllm and the secret is read from /run/secrets (Compose's mount point)",
      '"${MESHLLM_RUN_DIR:-/run/meshllm}/admin.hash"' in entry and '"${MESHLLM_SECRETS_DIR:-/run/secrets}/meshllm_admin_hash"' in entry)
check("the entrypoint drops to the Dockerfile's user id and the copy is made by that user (no chown anywhere)",
      "APP_UID=10001" in entry and "chown 10001:10001 /data" in dockerfile and "USER 10001:10001" in dockerfile and not re.search(r"\bchown\b|\bchmod\b", "\n".join(l for l in code_lines(entry) if " die " not in l)))
check("the entrypoint tells the app about a LAN publish, and the app's own check honours it (defence in depth)", "export MESHLLM_PUBLISH_LAN=1" in entry and "MESHLLM_PUBLISH_LAN" in read("meshllm", "websecurity.py"))
check(".env.example documents the login and the LAN opt-in, every line still a comment",
      all(w in env_example for w in ("MESHLLM_ADMIN_HASH_FILE", "MESHLLM_WEB_BIND", "MESHLLM_ALLOWED_HOSTS", "COMPOSE_FILE", "docker-compose.login.yml"))
      and all(l.lstrip().startswith("#") for l in env_example.splitlines() if l.strip()))

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
check("a named --web-host is no longer enough by itself: the name must be in --allowed-host", status_with_host("dash.example:%d" % port) == 403)
br.web_security.allowed.add("dash.example")        # what --allowed-host dash.example does
check("a name given with --allowed-host is accepted", status_with_host("dash.example:%d" % port) == 200)
check("another name is refused", status_with_host("evil.example:%d" % port) == 403)

# ---- the healthcheck with a login, and the app's own start-up rule for a LAN publish ----------------------------------------------
import json, urllib.request, contextlib, io
from meshllm import passwords, websecurity as ws, webroutes
hc = re.search(r'HEALTHCHECK[^\n]*\\\n\s*CMD \["python", "-c", "(.*)"\]', dockerfile)
check("the Dockerfile's HEALTHCHECK is a python -c one-liner this test can run", hc is not None)
hash_dir = tempfile.mkdtemp(prefix="dockerhash_")
hash_file = os.path.join(hash_dir, "admin.hash")
passwords.write_hash_file(hash_file, passwords.hash_password("throwaway-docker-test-pw"))       # a throwaway password, hashed by the app's own helper
if hc:
    hport = 19461 + os.getpid() % 400
    hbr, _radio, _tmp2 = make(web_port=hport, password_hash_file=hash_file, allowed_host=["radio.test"])
    c = http.client.HTTPConnection("127.0.0.1", hport, timeout=5)
    c.request("GET", "/api/status"); status_code = c.getresponse().status; c.close()
    check("sanity: this dashboard has a login (the status route answers 401 without a session)", hbr.web_security.auth_required and status_code == 401, status_code)
    cmd = hc.group(1).replace("127.0.0.1:8080", "127.0.0.1:%d" % hport)
    r = subprocess.run([sys.executable, "-c", cmd], capture_output=True, text=True, timeout=30)
    check("the Dockerfile's healthcheck command succeeds against a dashboard WITH a login", r.returncode == 0, (r.returncode, r.stderr[-300:]))
    old = subprocess.run([sys.executable, "-c", cmd.replace("/api/session", "/api/status")], capture_output=True, text=True, timeout=30)
    check("...whereas the old /api/status healthcheck would fail with 401, which is why it changed", old.returncode != 0 and "401" in old.stderr, (old.returncode, old.stderr[-200:]))
    probe = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/api/session" % hport, timeout=5).read())
    check("the route it uses is tagged PUBLIC and, signed out, says only whether a login is needed (no token, role or data)",
          webroutes.GET["/api/session"].route_role == ws.PUBLIC and probe["auth_required"] is True and probe["authenticated"] is False and probe["csrf"] is None and probe["role"] is None, probe)

def parse_args(argv, environ):
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            pr = bridge.build_parser(); parsed = pr.parse_args(argv); ws.check_args(pr, parsed, environ=environ)
        except SystemExit:
            return None, err.getvalue()
    return parsed, err.getvalue()
a, e = parse_args(["--web-host", "0.0.0.0"], {"MESHLLM_CONTAINER": "1"})
check("app: container + wildcard + no login still starts while only loopback is published (MESHLLM_PUBLISH_LAN unset)", a is not None, e)
a, e = parse_args(["--web-host", "0.0.0.0"], {"MESHLLM_CONTAINER": "1", "MESHLLM_PUBLISH_LAN": "1"})
check("app: ...but with MESHLLM_PUBLISH_LAN=1 it refuses to start without a login", a is None and "needs a login" in e, (a, e))
a, e = parse_args(["--web-host", "0.0.0.0", "--password-hash-file", os.path.join(hash_dir, "missing")], {"MESHLLM_CONTAINER": "1", "MESHLLM_PUBLISH_LAN": "1"})
check("app: ...and a missing hash file is an error there too", a is None and "password file" in e, e)
a, e = parse_args(["--web-host", "0.0.0.0", "--password-hash-file", hash_file], {"MESHLLM_CONTAINER": "1", "MESHLLM_PUBLISH_LAN": "1"})
check("app: a LAN publish with a login but no --allowed-host is refused", a is None and "--allowed-host" in e, e)
a, e = parse_args(["--web-host", "0.0.0.0", "--password-hash-file", hash_file, "--allowed-host", "192.0.2.10"], {"MESHLLM_CONTAINER": "1", "MESHLLM_PUBLISH_LAN": "1"})
check("app: a LAN publish with the login and an allowed host starts", a is not None and a.allowed_host == ["192.0.2.10"], (a, e))
a, e = parse_args(["--demo"], {"MESHLLM_CONTAINER": "1", "MESHLLM_PUBLISH_LAN": "1"})
check("app: --demo with MESHLLM_PUBLISH_LAN=1 is refused (defence in depth behind the entrypoint)", a is None and "--demo" in e, (a, e))
a, e = parse_args(["--demo"], {"MESHLLM_CONTAINER": "1"})
check("app: --demo inside the container with only a loopback publish still starts", a is not None, (a, e))
a, e = parse_args(["--web-host", "0.0.0.0", "--password-hash-file", hash_file, "--allowed-host", "127.0.0.1"], {"MESHLLM_CONTAINER": "1"})
check("app: a login on a loopback publish (the entrypoint adds --allowed-host 127.0.0.1) starts", a is not None, (a, e))
a, e = parse_args(["--web-host", "192.0.2.10"], {"MESHLLM_CONTAINER": "1", "MESHLLM_PUBLISH_LAN": "1"})
check("app: the container exception was never for a named address, with or without the LAN flag", a is None)
shutil.rmtree(hash_dir, ignore_errors=True)

check("the Dockerfile sets MESHLLM_CONTAINER=1 (what lets the demo bind 0.0.0.0 in the container)", "MESHLLM_CONTAINER=1" in dockerfile)
NOTE = "-p 127.0.0.1:8080:8080"
check("the Dockerfile, setup.md and README all say to publish a plain `docker run` as 127.0.0.1 only, never -p 8080:8080",
      all(NOTE in t and "never `-p 8080:8080`" in t for t in (dockerfile, read("docs", "setup.md"), read("README.md"))))

print(f"\n{len(fails)} failed" if fails else "\nall passed")
sys.stdout.flush(); os._exit(1 if fails else 0)
