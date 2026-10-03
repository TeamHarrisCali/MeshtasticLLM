"""setup_docker.py and `setup_env.py --docker`: the one-command Docker start (Docker, the radio, the password and Docker itself are all faked).

Nothing here starts a container, opens a serial port or asks for a real password; the temporary folders hold a copy of just the files the
script reads. The few real things are a loopback socket (the port check) and the compose files in the project (static checks)."""
import argparse, io, os, re, shutil, socket, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import setup_env as S
import setup_docker as D

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)

LIN = S.detect_os("Linux", "6", "x86_64", 'ID=arch\nPRETTY_NAME="Arch"\n', "")
WIN = S.detect_os("Windows", "11", "AMD64")
MAC = S.detect_os("Darwin", "1", "arm64", None, None, "14")
has = lambda *names: (lambda n: "/bin/" + n if n in names else None)
class Rep:
    """A Report that keeps its output."""
    def __new__(cls):
        buf = io.StringIO(); r = S.Report(buf); r.buf = buf; return r

# ---- .env lines -------------------------------------------------------------------------------------------------------------------
example = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read().splitlines()
check("the example has the placeholders the script fills in", all(any(l.lstrip().startswith("#" + k + "=") for l in example) for k in ("MESHLLM_TCP", "MESHLLM_SERIAL_DEVICE", "MESHLLM_SERIAL_GID", "MESHLLM_WEB_BIND", "MESHLLM_ALLOWED_HOSTS", "MESHLLM_ADMIN_HASH_FILE")))
check("get_env ignores commented lines", D.get_env(example, "MESHLLM_TCP") is None and D.get_env(["MESHLLM_TCP=192.0.2.7 # the radio"], "MESHLLM_TCP") == "192.0.2.7")
l1 = D.set_env(example, "MESHLLM_TCP", "192.0.2.7")
check("a commented placeholder becomes the real line, in the same place", D.get_env(l1, "MESHLLM_TCP") == "192.0.2.7" and len(l1) == len(example) and "#MESHLLM_TCP=192.168.1.50" not in l1)
l2 = D.set_env(l1, "MESHLLM_TCP", "192.0.2.9")
check("an active line is changed in place (never duplicated)", D.get_env(l2, "MESHLLM_TCP") == "192.0.2.9" and sum(1 for l in l2 if l.startswith("MESHLLM_TCP=")) == 1)
check("None removes the setting", D.get_env(D.set_env(l2, "MESHLLM_TCP", None), "MESHLLM_TCP") is None)
check("a key with no placeholder is appended; the rest is untouched", D.set_env(["A=1"], "B", "2") == ["A=1", "B=2"])
check("a path with spaces or a $ is single-quoted and read back unchanged", D.get_env(D.set_env([], "P", "C:\\Users\\a b\\admin$.hash"), "P") == "C:\\Users\\a b\\admin$.hash" and D.set_env([], "P", "/a b")[0] == "P='/a b'")
try: D.quote_env("it's"); quote_refused = False
except ValueError: quote_refused = True
check("a value with a single quote is refused rather than mangled (a ValueError)", quote_refused)
check("the compose file list follows the choices (':' on Linux, ';' on Windows)", D.compose_file_value(True, True, ":") == "docker-compose.yml:docker-compose.login.yml:docker-compose.usb.yml"
      and D.compose_file_value(False, False, ";") == "docker-compose.yml" and D.compose_file_value(False, True, ";") == "docker-compose.yml;docker-compose.usb.yml")

# ---- USB and install hints --------------------------------------------------------------------------------------------------------
fake_find = lambda pat: {"/dev/ttyUSB*": ["/dev/ttyUSB1", "/dev/ttyUSB0"], "/dev/ttyACM*": ["/dev/ttyACM0", "/dev/ttyUSB0"]}[pat]
check("Linux lists serial devices sorted and without duplicates", D.usb_devices("Linux", fake_find) == ["/dev/ttyACM0", "/dev/ttyUSB0", "/dev/ttyUSB1"])
check("Windows and macOS list none (Docker Desktop cannot pass USB through)", D.usb_devices("Windows", fake_find) == [] and D.usb_devices("Darwin", fake_find) == [])
check("install hints: Desktop on Windows and macOS, the package manager on Linux, a link otherwise",
      "Docker.DockerDesktop" in D.docker_install_hint(WIN) and "brew install --cask docker" in D.docker_install_hint(MAC) and "pacman -S" in D.docker_install_hint(LIN, has("pacman"))
      and "usermod -aG docker" in D.docker_install_hint(LIN, has("apt-get")) and "docs.docker.com" in D.docker_install_hint(LIN, has()))

# ---- is Docker usable --------------------------------------------------------------------------------------------------------------
def runner_for(compose=(0, "5.5.1"), info=(0, "29.8.2"), extra=None):
    """A runner that answers `docker compose version` and `docker info`, plus anything in `extra` (a function cmd -> (code, text) or None)."""
    calls = []
    def run(cmd, cwd=None, timeout=None, shell=False):
        calls.append(list(cmd))
        if extra:
            r = extra(cmd)
            if r is not None: return r
        if cmd[:3] == ["docker", "compose", "version"]: return compose
        if cmd[:2] == ["docker", "info"]: return info
        return 0, ""
    run.calls = calls
    return run
r = Rep(); ok = D.check_docker(r, LIN, has(), runner_for())
check("no docker program: a clear failure with the install command", not ok and "Docker is not installed" in r.buf.getvalue() and r.fails == 1)
r = Rep(); ok = D.check_docker(r, LIN, has("docker"), runner_for(compose=(1, "unknown command")))
check("Docker without Compose v2 is reported", not ok and "'docker compose' (version 2) is not" in r.buf.getvalue())
r = Rep(); ok = D.check_docker(r, LIN, has("docker"), runner_for(info=(1, "permission denied while trying to connect to the Docker daemon socket")))
check("a user outside the docker group is told how to join it", not ok and "usermod -aG docker" in r.buf.getvalue())
r = Rep(); ok = D.check_docker(r, LIN, has("docker"), runner_for(info=(1, "Cannot connect to the Docker daemon")))
check("a stopped engine says how to start it (systemctl on Linux, Docker Desktop elsewhere)", not ok and "systemctl enable --now docker" in r.buf.getvalue())
r = Rep(); ok = D.check_docker(r, WIN, has("docker"), runner_for(info=(1, "error during connect")))
check("...Docker Desktop on Windows", not ok and "Docker Desktop" in r.buf.getvalue())
r = Rep(); ok = D.check_docker(r, LIN, has("docker"), runner_for())
check("a working Docker passes and its version is shown", ok and r.fails == 0 and "Docker 29.8.2" in r.buf.getvalue())

# ---- radio ---------------------------------------------------------------------------------------------------------------------------
ns = lambda **kw: argparse.Namespace(**{**dict(docker=True, docker_stop=False, demo=False, tcp=None, lan=None, allowed_host=None, web_port=None, no_login=False, no_start=False,
                                              check=False, yes=False), **kw})
fake_stat = lambda dev: type("St", (), {"st_gid": 986})()
r = Rep(); use_usb, lines = D.choose_radio(r, LIN, list(example), ns(), lambda p: ["/dev/ttyUSB0"] if "USB" in p else [], fake_stat)
check("a USB radio on Linux: the device and its group number go to .env, and USB is used", use_usb and D.get_env(lines, "MESHLLM_SERIAL_DEVICE") == "/dev/ttyUSB0" and D.get_env(lines, "MESHLLM_SERIAL_GID") == "986")
r = Rep(); use_usb, lines = D.choose_radio(r, LIN, list(example), ns(tcp="192.0.2.7"), lambda p: ["/dev/ttyUSB0"], fake_stat)
check("--tcp wins over a plugged-in USB radio and is saved", not use_usb and D.get_env(lines, "MESHLLM_TCP") == "192.0.2.7")
r = Rep(); use_usb, lines2 = D.choose_radio(r, LIN, lines, ns(), lambda p: ["/dev/ttyUSB0"], fake_stat)
check("a saved Wi-Fi address is kept on the next run", not use_usb and D.get_env(lines2, "MESHLLM_TCP") == "192.0.2.7")
r = Rep(); use_usb, lines3 = D.choose_radio(r, LIN, lines, ns(tcp="off"), lambda p: ["/dev/ttyUSB0"], fake_stat)
check("--tcp off goes back to USB", use_usb and D.get_env(lines3, "MESHLLM_TCP") is None)
r = Rep(); use_usb, _ = D.choose_radio(r, LIN, list(example), ns(), lambda p: [], fake_stat)
check("no radio on Linux: a warning, the dashboard still starts", not use_usb and r.warns == 1 and "waits for a radio" in r.buf.getvalue())
r = Rep(); use_usb, _ = D.choose_radio(r, WIN, list(example), ns(), lambda p: ["/dev/ttyUSB0"], fake_stat)
check("Windows: no USB, and the way out (--tcp, or running without Docker) is explained", not use_usb and r.warns == 1 and "--tcp RADIO_ADDRESS" in r.buf.getvalue() and "setup.bat" in r.buf.getvalue())

# ---- the login ---------------------------------------------------------------------------------------------------------------------
tmp = tempfile.mkdtemp(prefix="setup_docker_")
try:
    class FakePasswords:
        """Stands in for meshllm.passwords: remembers the call and writes a file like the real one would."""
        calls = []; code = 0
        @classmethod
        def set_password_main(cls, role="admin", path=None, **kw):
            cls.calls.append((role, path))
            if cls.code == 0:
                open(path, "w").write("scrypt$x\n")
            return cls.code
    default_hash = os.path.join(tmp, "cfg", "admin.hash")
    os.makedirs(os.path.dirname(default_hash))
    yes, no = (lambda q, d=True: True), (lambda q, d=True: False)

    r = Rep(); path, lines = D.choose_login(r, tmp, list(example), ns(no_login=True), yes, default_hash, True, FakePasswords)
    check("--no-login: no password file, nothing asked", path is None and not FakePasswords.calls)
    r = Rep(); path, lines = D.choose_login(r, tmp, list(example), ns(), yes, default_hash, True, FakePasswords)
    check("interactive, none yet, the person says yes: the password is asked for (by the real prompt code) and the file goes to .env",
          path == default_hash and FakePasswords.calls == [("admin", default_hash)] and D.get_env(lines, "MESHLLM_ADMIN_HASH_FILE") == default_hash)
    FakePasswords.calls.clear()
    r = Rep(); path2, lines2 = D.choose_login(r, tmp, list(lines), ns(), no, default_hash, True, FakePasswords)
    check("a password file already named in .env is used without asking", path2 == default_hash and not FakePasswords.calls)
    os.remove(default_hash)
    r = Rep(); path, lines = D.choose_login(r, tmp, list(example), ns(), no, default_hash, True, FakePasswords)
    check("saying no: no login, no file, nothing asked", path is None and not FakePasswords.calls and not os.path.exists(default_hash))
    FakePasswords.code = 1
    r = Rep(); path, lines = D.choose_login(r, tmp, list(example), ns(), yes, default_hash, True, FakePasswords)
    check("a failed or cancelled password entry: a warning and the dashboard starts without a login (loopback only)", path is None and r.warns == 1 and "without a login" in r.buf.getvalue())
    FakePasswords.code = 0; FakePasswords.calls.clear()
    r = Rep(); path, lines = D.choose_login(r, tmp, list(example), ns(yes=True), yes, default_hash, False, FakePasswords)
    check("not at a terminal (--yes, a script): no prompt is attempted and no login is set", path is None and not FakePasswords.calls and "terminal" in r.buf.getvalue())
    open(default_hash, "w").write("scrypt$x\n")
    r = Rep(); path, lines = D.choose_login(r, tmp, list(example), ns(), yes, default_hash, True, FakePasswords)
    check("an existing password from --set-password is offered and used (no new prompt)", path == default_hash and not FakePasswords.calls)
    r = Rep(); path, lines = D.choose_login(r, tmp, list(example), ns(), no, default_hash, False, FakePasswords)
    check("...and when declined, with no terminal, there is no login", path is None)
    r = Rep(); path, lines = D.choose_login(r, tmp, list(example), ns(check=True), no, default_hash, True, FakePasswords)
    check("--check never prompts and never writes", path == default_hash and not FakePasswords.calls and lines == list(example))
    gone = D.set_env(list(example), "MESHLLM_ADMIN_HASH_FILE", os.path.join(tmp, "missing.hash"))
    r = Rep(); path, lines = D.choose_login(r, tmp, gone, ns(), no, default_hash + ".none", False, FakePasswords)
    check("a password file named in .env that has vanished is reported", path is None and r.warns == 1 and "is gone" in r.buf.getvalue())

    # ---- the network opt-in
    r = Rep(); ok, lines = D.choose_lan(r, list(example), ns(), None)
    check("by default the dashboard is for this computer only", ok and D.get_env(lines, "MESHLLM_WEB_BIND") is None)
    r = Rep(); ok, lines = D.choose_lan(r, list(example), ns(lan="192.0.2.10"), None)
    check("--lan without a password is refused", not ok and r.fails == 1 and "needs the dashboard password" in r.buf.getvalue() and D.get_env(lines, "MESHLLM_WEB_BIND") is None)
    r = Rep(); ok, lines = D.choose_lan(r, list(example), ns(lan="192.0.2.10", allowed_host=["radio.test", "192.0.2.10"]), default_hash)
    check("--lan with a password: the address and every name you will type are written (no duplicates), with a clear-text warning",
          ok and D.get_env(lines, "MESHLLM_WEB_BIND") == "192.0.2.10" and D.get_env(lines, "MESHLLM_ALLOWED_HOSTS") == "192.0.2.10,radio.test" and r.warns == 1 and "clear text" in r.buf.getvalue())
    r = Rep(); ok, lines2 = D.choose_lan(r, lines, ns(lan="off"), default_hash)
    check("--lan off goes back to this computer only", ok and D.get_env(lines2, "MESHLLM_WEB_BIND") is None and D.get_env(lines2, "MESHLLM_ALLOWED_HOSTS") is None)
    r = Rep(); ok, _ = D.choose_lan(r, lines, ns(), None)
    check("a network publish left in .env without a password is refused at the next run too", not ok and "needs the login" in r.buf.getvalue())

    # ---- the port
    listener = socket.socket(); listener.bind(("127.0.0.1", 0)); listener.listen(1); busy = listener.getsockname()[1]
    r = Rep(); got = D.check_port(r, runner_for(extra=lambda c: (0, "") if "ps" in c else None), tmp, [], ns(web_port=busy))
    check("a port that something else holds fails early and names the way out", got is None and r.fails == 1 and f"--web-port {busy + 1}" in r.buf.getvalue(), r.buf.getvalue())
    open(os.path.join(tmp, "dummy"), "w").close(); os.makedirs(os.path.join(tmp, "logs")); open(os.path.join(tmp, "logs", "bridge.pid"), "w").write("1")
    r = Rep(); D.check_port(r, runner_for(extra=lambda c: (0, "") if "ps" in c else None), tmp, [], ns(web_port=busy))
    check("...and mentions a bridge started from this folder (it also holds the radio)", "stop_bridge" in r.buf.getvalue())
    r = Rep(); got = D.check_port(r, runner_for(extra=lambda c: (0, "abcdef123456\n") if "ps" in c else None), tmp, [], ns(web_port=busy))
    check("a busy port that is our own running bridge is fine (a re-run)", got == busy and r.fails == 0)
    listener.close()
    r = Rep(); free = D.check_port(r, runner_for(), tmp, ["MESHLLM_WEB_PORT=" + str(busy)], ns())
    check("the port in .env is the one checked", free == busy)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---- waiting for the bridge ----------------------------------------------------------------------------------------------------------
class Clock:
    def __init__(self): self.t = 0.0
    def now(self): return self.t
    def sleep(self, s): self.t += s
def state_runner(states):
    """ps returns an id; inspect walks through `states` (status, health, exit), repeating the last."""
    seq = list(states)
    def extra(cmd):
        if cmd[:3] == ["docker", "compose", "ps"]: return 0, "0123456789ab\n"
        if cmd[:2] == ["docker", "inspect"]:
            s = seq.pop(0) if len(seq) > 1 else seq[0]
            return 0, "%s %s %d" % s
        if cmd[:3] == ["docker", "compose", "logs"]: return 0, "boom: the last log line"
    return runner_for(extra=extra)
c = Clock(); r = Rep()
ok = D.wait_for_bridge(r, state_runner([("running", "starting", 0), ("running", "starting", 0), ("running", "healthy", 0)]), "/x", wait=60, sleep=c.sleep, now=c.now)
check("it waits through 'starting' and returns when the bridge is healthy", ok and r.fails == 0 and c.t >= 4)
c = Clock(); r = Rep()
ok = D.wait_for_bridge(r, state_runner([("running", "starting", 0)]), "/x", wait=20, sleep=c.sleep, now=c.now)
check("a bridge that never gets healthy fails after the wait, with its log lines", not ok and r.fails == 1 and "did not become healthy in 20 seconds" in r.buf.getvalue() and "boom" in r.buf.getvalue())
c = Clock(); r = Rep()
ok = D.wait_for_bridge(r, state_runner([("running", "starting", 0), ("exited", "none", 78)]), "/x", wait=60, sleep=c.sleep, now=c.now)
check("a bridge that exits (a refused configuration) fails at once, not after the wait", not ok and "keeps stopping" in r.buf.getvalue() and c.t < 10)
c = Clock(); r = Rep()
ok = D.wait_for_bridge(r, runner_for(extra=lambda cmd: (0, "") if cmd[:3] == ["docker", "compose", "ps"] else ((0, "logs") if cmd[:3] == ["docker", "compose", "logs"] else None)), "/x", wait=6, sleep=c.sleep, now=c.now)
check("no bridge container at all also fails after the wait", not ok and r.fails == 1)

# ---- the whole run (a temporary project folder; Docker faked) -----------------------------------------------------------------------
def make_root():
    d = tempfile.mkdtemp(prefix="setup_docker_root_")
    os.makedirs(os.path.join(d, "meshllm"))
    for f in ("__init__.py", "passwords.py", "bridge.py"):
        shutil.copy(os.path.join(ROOT, "meshllm", f), os.path.join(d, "meshllm", f))
    shutil.copy(os.path.join(ROOT, ".env.example"), d)
    return d
def go(argv, root, runner=None, streamed=None, which=None, answer=True, find=None, info=None):
    """S.main with the machine faked. Returns (exit code, output, the commands that were streamed to the console)."""
    buf, streamed = io.StringIO(), [] if streamed is None else streamed
    real_stream, real_glob = D.stream, D.glob.glob
    D.stream = lambda cmd, cwd=None: (streamed.append(list(cmd)), 0)[1]
    D.glob.glob = find or (lambda p: ["/dev/ttyUSB0"] if "USB" in p else [])
    real_stat = os.stat
    try:
        D.sys.stdin = D.sys.stdin     # unchanged; the questions are answered through `ask` below
        code = S.main(argv + ["--dir", root], out=buf, which=which or has("docker"), runner=runner or ok_runner(), info=info or LIN)
    finally:
        D.stream, D.glob.glob = real_stream, real_glob
    return code, buf.getvalue(), streamed
def ok_runner(healthy=True):
    return runner_for(extra=lambda c: ((0, "0123456789ab\n") if "ps" in c else None) if c[:2] == ["docker", "compose"] and "ps" in c
                      else ((0, "running healthy 0") if c[:2] == ["docker", "inspect"] and healthy else ((0, "exited none 1") if c[:2] == ["docker", "inspect"] else None)))
real_stat = os.stat
os.stat = lambda p, *a, **k: type("St", (), {"st_gid": 986})() if str(p) == "/dev/ttyUSB0" else real_stat(p, *a, **k)
try:
    root = make_root()
    home = tempfile.mkdtemp(prefix="setup_docker_home_")
    os.environ["XDG_CONFIG_HOME"] = home
    try:
        code, out, streamed = go(["--docker", "--yes", "--no-login", "--web-port", "18123"], root)
        env = open(os.path.join(root, ".env"), encoding="utf-8").read()
        check("--docker (no login) writes .env with the USB override and the port, and starts the containers with --build", code == 0 and "COMPOSE_FILE=docker-compose.yml" + os.pathsep + "docker-compose.usb.yml" in env
              and "MESHLLM_SERIAL_DEVICE=/dev/ttyUSB0" in env and "MESHLLM_SERIAL_GID=986" in env and "MESHLLM_WEB_PORT=18123" in env and streamed == [["docker", "compose", "up", "-d", "--build"]], (code, env[-300:], streamed, out[-300:]))
        check("it says where the dashboard is and how the model comes", "http://127.0.0.1:18123/" in out and "model-pull" in out and "sign in as admin" not in out, out[-500:])
        active = [l for l in env.splitlines() if l.strip() and not l.lstrip().startswith("#")]
        check("the .env it wrote holds no password, hash or secret (its active lines are only file lists, paths and numbers)", active and not any(re.search(r"(?i)scrypt|password|secret|token", l) for l in active), active)
        before = env
        code, out, streamed = go(["--docker", "--yes", "--no-login", "--web-port", "18123"], root)
        check("running it again changes nothing in .env (idempotent) and just starts again", open(os.path.join(root, ".env"), encoding="utf-8").read() == before and code == 0 and ".env is up to date" in out)
        code, out, streamed = go(["--docker", "--check", "--yes"], root)
        check("--docker --check writes nothing and starts nothing", code == 0 and not streamed and open(os.path.join(root, ".env"), encoding="utf-8").read() == before and "check only" in out, out[-300:])
        os.remove(os.path.join(root, ".env"))
        code, out, streamed = go(["--docker", "--check", "--yes"], root)
        check("...and creates no .env when there was none", code == 0 and not os.path.exists(os.path.join(root, ".env")) and not streamed)
        code, out, streamed = go(["--docker", "--yes", "--no-login", "--no-start", "--tcp", "192.0.2.7"], root)
        env = open(os.path.join(root, ".env"), encoding="utf-8").read()
        check("--no-start writes .env (Wi-Fi radio: no USB override) and starts nothing", code == 0 and not streamed and "MESHLLM_TCP=192.0.2.7" in env and "usb" not in env.split("COMPOSE_FILE=")[1].split("\n")[0], env[-300:])
        # with a password already made
        os.makedirs(os.path.join(home, "meshllm")); hf = os.path.join(home, "meshllm", "admin.hash"); open(hf, "w").write("scrypt$32768$8$1$aa$bb\n")
        os.remove(os.path.join(root, ".env"))
        code, out, streamed = go(["--docker", "--yes", "--lan", "192.0.2.10", "--allowed-host", "radio.test"], root)
        env = open(os.path.join(root, ".env"), encoding="utf-8").read()
        check("with a password file the login override is added and --lan publishes on the address with its names",
              code == 0 and "docker-compose.login.yml" in env and "MESHLLM_ADMIN_HASH_FILE=" + hf in env and "MESHLLM_WEB_BIND=192.0.2.10" in env and "MESHLLM_ALLOWED_HOSTS=192.0.2.10,radio.test" in env
              and "http://192.0.2.10:8080/" in out and "sign in as admin" in out, (code, env, out[-400:]))
        os.remove(hf)
        os.remove(os.path.join(root, ".env"))
        code, out, streamed = go(["--docker", "--yes", "--lan", "192.0.2.10"], root)
        check("--lan with no password on a non-interactive run is refused, nothing written, nothing started", code == 1 and not streamed and not os.path.exists(os.path.join(root, ".env")), (code, out[-300:]))
        code, out, streamed = go(["--docker", "--yes", "--no-login"], root, runner=ok_runner(healthy=False))
        check("a bridge that exits right after starting is a failure with its log, exit code 1", code == 1 and "keeps stopping" in out, out[-300:])
        code, out, streamed = go(["--docker", "--yes", "--no-login"], root, which=has())
        check("no Docker: exit 1 with the install hint, nothing written or started", code == 1 and "Docker is not installed" in out and not streamed)
        code, out, streamed = go(["--docker", "--demo"], root)
        check("--docker --demo starts only the demo service (profile demo), no .env", code == 0 and streamed == [["docker", "compose", "--profile", "demo", "up", "-d", "--build", "demo"]] and "simulated mesh" in out, (code, streamed, out[-300:]))
        code, out, streamed = go(["--docker-stop"], root)
        check("--docker-stop runs docker compose down (including the demo profile)", code == 0 and streamed == [["docker", "compose", "--profile", "demo", "down"]] and "database" in out)
    finally:
        os.environ.pop("XDG_CONFIG_HOME", None)
        shutil.rmtree(root, ignore_errors=True); shutil.rmtree(home, ignore_errors=True)
finally:
    os.stat = real_stat

# ---- option checks ---------------------------------------------------------------------------------------------------------------------
def rejected(argv):
    err = io.StringIO(); real = sys.stderr; sys.stderr = err
    try: S.main(argv, out=io.StringIO()); return None
    except SystemExit as e: return err.getvalue() if e.code else ""
    finally: sys.stderr = real
check("--tcp, --lan and the other Docker options need --docker", all(rejected(a) for a in (["--tcp", "192.0.2.7"], ["--lan", "192.0.2.10"], ["--demo"], ["--web-port", "9"], ["--no-login"])))
check("--docker does not combine with the Python-environment options", all(rejected(["--docker"] + a) for a in (["--start"], ["--recreate"], ["--autostart"], ["--pull-model"])) and rejected(["--docker", "--docker-stop"]))
check("--demo cannot be combined with a radio, the network or a login choice", all(rejected(["--docker", "--demo"] + a) for a in (["--tcp", "192.0.2.7"], ["--lan", "192.0.2.10"], ["--no-login"])))
check("--web-port is range-checked", rejected(["--docker", "--web-port", "70000"]) and rejected(["--docker", "--web-port", "0"]))

# ---- the compose files themselves ------------------------------------------------------------------------------------------------------
compose = open(os.path.join(ROOT, "docker-compose.yml"), encoding="utf-8").read()
usb = open(os.path.join(ROOT, "docker-compose.usb.yml"), encoding="utf-8").read()
mp = compose[compose.index("  model-pull:"):compose.index("  demo:")]
check("model-pull: same pinned ollama image, no ports, never restarts, hardened like the others",
      re.search(r"image:\s*ollama/ollama:\d+\.\d+\.\d+\s*$", mp, re.M) and "ports:" not in mp and 'restart: "no"' in mp and "cap_drop: [ALL]" in mp and "read_only: true" in mp and "no-new-privileges:true" in mp)
check("model-pull talks to the ollama service by name and can be turned off", "OLLAMA_HOST: \"http://ollama:11434\"" in mp and "MESHLLM_PULL_MODEL" in mp and "exit 0" in mp)
check("the USB override takes the device from MESHLLM_SERIAL_DEVICE in both places, defaulting to ttyUSB0", usb.count("${MESHLLM_SERIAL_DEVICE:-/dev/ttyUSB0}") == 3, usb.count("${MESHLLM_SERIAL_DEVICE:-/dev/ttyUSB0}"))
check("the setup script's files use Unix line endings where they are scripts", b"\r" not in open(os.path.join(ROOT, "setup_docker.py"), "rb").read())

print(f"\n{len(fails)} failed" if fails else "\nall passed")
sys.exit(1 if fails else 0)
