"""Set the project up to run in Docker, and start it, with one command (called by `python scripts/setup_env.py --docker`).

    python scripts/setup_env.py --docker                 check Docker, find the radio, offer a dashboard password, write .env, start everything
    python scripts/setup_env.py --docker --tcp 192.0.2.7 the radio is a Wi-Fi one at that address (--tcp off goes back to USB)
    python scripts/setup_env.py --docker --lan 192.0.2.10 [--allowed-host radio.test]   publish the dashboard to the network (needs the password)
    python scripts/setup_env.py --docker --demo          a simulated mesh: no radio, no Ollama, no password (loopback only)
    python scripts/setup_env.py --docker-stop            stop and remove the containers (the database volume stays)
    python scripts/setup_env.py --docker --check         only look and report; change nothing

What it writes is `.env` next to docker-compose.yml (git ignores it): which compose files to use, where the password hash file is, the
USB device or the radio's address. Nothing secret goes there: the password is only ever asked for at the keyboard by `--set-password`'s own
code (passwords.set_password_main), stored as a scrypt hash in a mode-600 file, and handed to the container as a Compose secret.
Afterwards a plain `docker compose up -d` (or `down`, `logs`) does the same as this script, because .env carries the choices.

Only the standard library and meshllm/passwords.py (also standard-library only) are used, so it runs before anything is installed.
"""
import glob
import os
import re
import socket
import subprocess
import sys
import time

ENV_NAME = ".env"
EXAMPLE_NAME = ".env.example"
DEFAULT_PORT = 8080
HEALTH_WAIT = 150                      # seconds to wait for the bridge container to report healthy
SERIAL_GLOBS = ("/dev/ttyUSB*", "/dev/ttyACM*")


# ---- small helpers --------------------------------------------------------------------------------------------------------------------------
def ask_default(question, default=True, yes=False, interactive=None):
    """Ask a yes/no question where pressing Enter means `default`. With --yes, or with no keyboard (piped, scheduled), the answer is the default."""
    if yes:
        return default
    if interactive is None:
        interactive = sys.stdin is not None and sys.stdin.isatty()
    if not interactive:
        return default
    try:
        answer = input("       %s [%s] " % (question, "Y/n" if default else "y/N")).strip().lower()
    except EOFError:
        return default
    return default if not answer else answer in ("y", "yes")


def quote_env(value):
    """A value as a .env line wants it: bare when plain, single-quoted when it has spaces or a $ (Compose does not expand inside single quotes)."""
    if re.search(r"[\s$#\"'\\]", value):
        if "'" in value:
            raise ValueError("a value with a single quote in it cannot be written to .env")
        return "'" + value + "'"
    return value


def unquote_env(raw):
    """The value part of a .env line without its quotes or a trailing comment."""
    raw = raw.strip()
    if raw[:1] in ("'", '"') and raw.endswith(raw[:1]) and len(raw) >= 2:
        return raw[1:-1]
    return re.split(r"\s+#", raw, 1)[0].strip()


def get_env(lines, key):
    """The value of KEY in .env lines (a commented-out line does not count), or None."""
    for line in lines:
        m = re.match(r"^\s*" + re.escape(key) + r"\s*=(.*)$", line)
        if m:
            return unquote_env(m.group(1))
    return None


def set_env(lines, key, value):
    """The lines with KEY set to VALUE (or removed when VALUE is None): an existing active line is changed in place, else a commented
    placeholder `#KEY=...` is replaced by the real line, else it is added at the end. Everything else is kept as it was."""
    active = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
    placeholder = re.compile(r"^\s*#\s*" + re.escape(key) + r"\s*=")
    new = None if value is None else "%s=%s" % (key, quote_env(value))
    out, done = [], False
    for line in lines:
        if active.match(line):
            if new is not None and not done:
                out.append(new)
            done = True
        elif placeholder.match(line) and new is not None and not done and not any(active.match(x) for x in lines):
            out.append(new)
            done = True
        else:
            out.append(line)
    if new is not None and not done:
        out.append(new)
    return out


def read_lines(path):
    """The lines of a text file, or [] if it is not there."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()
    except OSError:
        return []


def write_lines(path, lines):
    """Write the lines (with a final newline)."""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")


def port_is_free(port, host="127.0.0.1"):
    """True if nothing is listening on host:port (a quick bind test)."""
    s = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def usb_devices(system, find=None):
    """Serial devices that might be the radio on a Linux host (Docker Desktop on Windows and macOS cannot pass USB devices through)."""
    if system != "Linux":
        return []
    find = find or glob.glob
    found = []
    for pattern in SERIAL_GLOBS:
        found.extend(find(pattern))
    return sorted(set(found))


def docker_install_hint(info, which=None):
    """How to get Docker on this computer, as text."""
    which = which or (lambda n: None)
    system = info.get("system")
    if system == "Windows":
        return "Install Docker Desktop: winget install -e --id Docker.DockerDesktop   (or https://www.docker.com/products/docker-desktop/), start it, then run this again."
    if system == "Darwin":
        return "Install Docker Desktop: brew install --cask docker   (or https://www.docker.com/products/docker-desktop/), start it, then run this again."
    for exe, cmd in (("apt-get", "sudo apt-get install -y docker.io docker-compose-v2"), ("dnf", "sudo dnf install -y docker docker-compose"),
                     ("pacman", "sudo pacman -S --needed docker docker-compose"), ("zypper", "sudo zypper install -y docker docker-compose"),
                     ("apk", "sudo apk add docker docker-cli-compose")):
        if which(exe):
            return "Install Docker with:  %s\nthen:  sudo systemctl enable --now docker  and  sudo usermod -aG docker $USER  (log out and in again)." % cmd
    return "Install Docker Engine with Compose v2: https://docs.docker.com/engine/install/"


# ---- Docker itself --------------------------------------------------------------------------------------------------------------------------
def check_docker(rep, info, which, runner):
    """True if `docker compose` can be used; otherwise reports exactly what is missing and returns False."""
    if not which("docker"):
        rep.fail("Docker is not installed", docker_install_hint(info, which))
        return False
    code, text = runner(["docker", "compose", "version"], timeout=30)
    if code != 0:
        rep.fail("Docker is installed but 'docker compose' (version 2) is not", docker_install_hint(info, which))
        return False
    code, text = runner(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=60)
    if code != 0:
        low = text.lower()
        if "permission denied" in low:
            rep.fail("this user may not use Docker", "Add yourself to the docker group, then log out and in again:  sudo usermod -aG docker $USER")
        else:
            hint = "Start Docker Desktop and wait until it says it is running." if info.get("system") in ("Windows", "Darwin") else "Start it:  sudo systemctl enable --now docker"
            rep.fail("the Docker engine is not running", hint)
        return False
    rep.ok("Docker %s with Compose %s" % (text.strip().splitlines()[-1] if text.strip() else "?", _compose_version(runner)))
    return True


def _compose_version(runner):
    code, text = runner(["docker", "compose", "version", "--short"], timeout=30)
    return text.strip().splitlines()[-1] if code == 0 and text.strip() else "2"


def bridge_container(runner, root, service="bridge"):
    """The id of the service's container (the bridge, or the demo) if it exists, else None."""
    cmd = ["docker", "compose"] + (["--profile", "demo"] if service == "demo" else []) + ["ps", "-aq", service]
    code, text = runner(cmd, cwd=root, timeout=60)
    ids = [t for t in text.split() if re.fullmatch(r"[0-9a-f]{12,64}", t)] if code == 0 else []
    return ids[0] if ids else None


def container_state(runner, cid):
    """(status, health, exit code) of a container, e.g. ('running', 'healthy', 0)."""
    code, text = runner(["docker", "inspect", "--format", "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} {{.State.ExitCode}}", cid], timeout=30)
    parts = text.split()
    if code != 0 or len(parts) < 3:
        return "unknown", "unknown", 0
    return parts[0], parts[1], int(parts[2]) if parts[2].lstrip("-").isdigit() else 0


def wait_for_bridge(rep, runner, root, wait=HEALTH_WAIT, sleep=time.sleep, now=time.monotonic, service="bridge"):
    """Wait until the bridge (or demo) container is healthy. Returns True, or False after saying what is wrong (and showing its last log lines)."""
    end = now() + wait
    last = None
    while True:
        cid = bridge_container(runner, root, service)
        status, health, exit_code = container_state(runner, cid) if cid else ("missing", "none", 0)
        last = (status, health)
        if status == "running" and health == "healthy":
            return True
        if status in ("exited", "dead") or (status == "restarting"):
            break
        if now() >= end:
            break
        sleep(2)
    code, text = runner(["docker", "compose"] + (["--profile", "demo"] if service == "demo" else []) + ["logs", "--no-color", "--tail", "12", service], cwd=root, timeout=60)
    reason = "keeps stopping" if last[0] in ("exited", "dead", "restarting") else "did not become healthy in %d seconds" % wait
    why = "the %s container %s" % (service, reason)
    rep.fail(why, "Its last log lines:\n" + (text.strip() or "(none)"))
    return False


# ---- the steps ------------------------------------------------------------------------------------------------------------------------------
def choose_login(rep, root, lines, opts, ask, default_hash, interactive, passwords):
    """Decide the dashboard login. Returns (the hash file path or None, updated .env lines)."""
    if opts.no_login:
        rep.ok("no dashboard login (--no-login): the dashboard is published on this computer only")
        return None, set_env(lines, "MESHLLM_ADMIN_HASH_FILE", None)
    current = get_env(lines, "MESHLLM_ADMIN_HASH_FILE")
    if current and os.path.isfile(current):
        rep.ok("dashboard login: the password file in .env (sign in as admin)")
        return current, lines
    if current:
        rep.warn("the password file named in .env is gone (%s)" % current, "A new one is made below, or remove the line from .env.")
    if os.path.isfile(default_hash):
        if opts.check:
            rep.ok("dashboard login: the existing password (%s) would be used" % default_hash)
            return default_hash, lines
        if ask("A dashboard password already exists (%s). Use it for the Docker dashboard?" % default_hash, True):
            rep.ok("dashboard login: using the existing password (sign in as admin)")
            return default_hash, set_env(lines, "MESHLLM_ADMIN_HASH_FILE", default_hash)
    if opts.check:
        rep.warn("no dashboard login is set up", "It is recommended; setup asks for one (python scripts/setup_env.py --docker).")
        return None, lines
    if interactive and ask("Protect the dashboard with a password? (recommended; you type it twice, nothing is shown)", True):
        code = passwords.set_password_main("admin", path=default_hash)
        if code == 0 and os.path.isfile(default_hash):
            rep.ok("dashboard login: password saved (sign in as admin)")
            return default_hash, set_env(lines, "MESHLLM_ADMIN_HASH_FILE", default_hash)
        rep.warn("no password was set", "Starting without a login: the dashboard is published on this computer only. Run this again to set one.")
        return None, lines
    rep.ok("no dashboard login: the dashboard is published on this computer only%s" %
           ("" if interactive else " (to set one, run this in a terminal without --yes)"))
    return None, lines


def choose_radio(rep, info, lines, opts, find=None, stat=None):
    """Decide how the container reaches the radio. Returns (use_usb, updated lines)."""
    tcp = opts.tcp
    if tcp is not None and tcp.lower() in ("off", "none", ""):
        lines = set_env(lines, "MESHLLM_TCP", None)
        tcp = None
        saved = None
    else:
        saved = get_env(lines, "MESHLLM_TCP")
    if tcp:
        lines = set_env(lines, "MESHLLM_TCP", tcp)
        rep.ok("radio: Wi-Fi at %s (not yet tested on real hardware; Bluetooth does not work in a container)" % tcp)
        return False, lines
    if saved:
        rep.ok("radio: Wi-Fi at %s (from .env; --tcp off goes back to USB)" % saved)
        return False, lines
    stat = stat or os.stat
    devices = usb_devices(info.get("system"), find)
    if devices:
        dev = devices[0]
        try:
            gid = str(stat(dev).st_gid)
        except OSError:
            gid = None
        lines = set_env(lines, "MESHLLM_SERIAL_DEVICE", dev)
        if gid is not None:
            lines = set_env(lines, "MESHLLM_SERIAL_GID", gid)
        rep.ok("radio: USB, %s%s" % (dev, " (the first of %d serial devices; --tcp ADDRESS uses a Wi-Fi radio instead)" % len(devices) if len(devices) > 1 else ""))
        rep.info("A container gets the device when it is created: after unplugging and replugging the radio, run this again.")
        return True, lines
    if info.get("system") in ("Windows", "Darwin"):
        rep.warn("no radio configured: Docker Desktop cannot hand a USB radio to a container",
                 "Use a Wi-Fi radio:  python scripts/setup_env.py --docker --tcp RADIO_ADDRESS\nor run the bridge without Docker (setup.bat / ./setup.sh), which supports USB and Bluetooth.")
    else:
        rep.warn("no USB radio found (no /dev/ttyUSB* or /dev/ttyACM*)", "Plug it in and run this again, or use a Wi-Fi radio:  python scripts/setup_env.py --docker --tcp RADIO_ADDRESS\n"
                 "The dashboard starts anyway and waits for a radio.")
    return False, lines


def choose_lan(rep, lines, opts, hash_file):
    """The opt-in network publish. Returns (ok, updated lines); not ok when it was asked for without a login."""
    if opts.lan is None:
        current = get_env(lines, "MESHLLM_WEB_BIND")
        if current and hash_file:
            rep.ok("dashboard published on %s (from .env)" % current)
        elif current:
            rep.fail("MESHLLM_WEB_BIND=%s in .env publishes the dashboard to the network, which needs the login" % current,
                     "Set a password (run this again), or remove MESHLLM_WEB_BIND from .env.")
            return False, lines
        else:
            rep.ok("dashboard published on this computer only (http://127.0.0.1)")
        return True, lines
    if opts.lan.lower() in ("off", "none", "local", ""):
        lines = set_env(set_env(lines, "MESHLLM_WEB_BIND", None), "MESHLLM_ALLOWED_HOSTS", None)
        rep.ok("dashboard published on this computer only (http://127.0.0.1)")
        return True, lines
    if not hash_file:
        rep.fail("--lan needs the dashboard password", "A dashboard reachable from the network must have a login. Run without --no-login in a terminal, so you can set one.")
        return False, lines
    names = [opts.lan] + [h for h in (opts.allowed_host or []) if h != opts.lan]
    lines = set_env(set_env(lines, "MESHLLM_WEB_BIND", opts.lan), "MESHLLM_ALLOWED_HOSTS", ",".join(names))
    rep.ok("dashboard published on %s, for the host names %s" % (opts.lan, ", ".join(names)))
    rep.warn("without HTTPS the password and the dashboard travel in clear text on your network", "Only on a network you trust (docs/setup.md, the LAN login section).")
    return True, lines


def compose_file_value(use_login, use_usb, sep=os.pathsep):
    """The COMPOSE_FILE value for the chosen overrides (paths relative to the project folder, where docker-compose.yml is; the overrides live in docker/)."""
    files = ["docker-compose.yml"] + (["docker/docker-compose.login.yml"] if use_login else []) + (["docker/docker-compose.usb.yml"] if use_usb else [])
    return sep.join(files)


def check_port(rep, runner, root, lines, opts):
    """Fail early (instead of with Docker's own message) when the dashboard's port is taken by something else."""
    port = opts.web_port or int(get_env(lines, "MESHLLM_WEB_PORT") or DEFAULT_PORT)
    if port_is_free(port):
        return port
    code, text = runner(["docker", "compose", "--profile", "demo", "ps", "-q", "bridge", "demo"], cwd=root, timeout=60)
    if code == 0 and text.strip():
        return port                                               # it is our own bridge (or demo), already running: this is a re-run
    host_bridge = os.path.isfile(os.path.join(root, "logs", "bridge.pid"))
    rep.fail("port %d is already in use" % port,
             ("A bridge started from this folder may be running: stop it first (./scripts/stop_bridge.sh, or scripts/stop_bridge.ps1 on Windows); it also holds the radio.\n" if host_bridge else "")
             + "Or pick another port:  python scripts/setup_env.py --docker --web-port %d" % (port + 1))
    return None


def stream(cmd, cwd=None):
    """Run a command with its output going straight to the console (builds and downloads are long). Returns the exit code."""
    try:
        return subprocess.call(cmd, cwd=cwd)
    except OSError:
        return 127


# ---- the entry point ------------------------------------------------------------------------------------------------------------------------
def run_docker(rep, root, info, opts, runner, ask, which, interactive=None, find=None, stat=None, run_stream=None, sleep=time.sleep, now=time.monotonic):
    """The whole `--docker` job. `runner(cmd, cwd, timeout)` returns (code, text); `ask(question, default)` returns a bool. Returns the exit code."""
    run_stream = run_stream or stream
    sys.path.insert(0, root)
    try:
        from meshllm import passwords
    finally:
        sys.path.pop(0)
    if interactive is None:
        interactive = sys.stdin is not None and sys.stdin.isatty()
    env_path, example_path = os.path.join(root, ENV_NAME), os.path.join(root, EXAMPLE_NAME)

    rep.step("Docker")
    if not check_docker(rep, info, which, runner):
        return 1

    if opts.docker_stop:
        rep.step("Stopping")
        if opts.check:
            rep.info("(check only: nothing stopped)")
            return 0
        code = run_stream(["docker", "compose", "--profile", "demo", "down"], cwd=root)
        rep.ok("stopped (the database and the downloaded models are kept; docker compose down -v would delete them)") if code == 0 else rep.fail("docker compose down failed")
        return 0 if code == 0 else 1

    if opts.demo:
        rep.step("Demo")
        port = check_port(rep, runner, root, [], opts)
        if port is None:
            return 1
        if opts.check:
            rep.ok("the demo (a simulated mesh, no radio, no Ollama) would start on http://127.0.0.1:%d/" % port)
            return 0
        code = _with_port(run_stream, ["docker", "compose", "--profile", "demo", "up", "-d", "--build", "demo"], root, port)
        if code != 0:
            rep.fail("docker compose could not start the demo")
            return 1
        if not wait_for_bridge(rep, runner, root, sleep=sleep, now=now, service="demo"):
            return 1
        rep.ok("the demo is running: open http://127.0.0.1:%d/   (a simulated mesh; nothing is transmitted or kept)" % port)
        rep.info("Stop it:  python scripts/setup_env.py --docker-stop")
        return 0

    lines = read_lines(env_path) or read_lines(example_path)
    original = list(lines)

    rep.step("Radio")
    use_usb, lines = choose_radio(rep, info, lines, opts, find, stat)

    rep.step("Dashboard login")
    default_hash = passwords.default_path("admin")
    hash_file, lines = choose_login(rep, root, lines, opts, ask, default_hash, interactive and not opts.yes, passwords)
    ok, lines = choose_lan(rep, lines, opts, hash_file)
    if not ok:
        return 1

    if opts.web_port:
        lines = set_env(lines, "MESHLLM_WEB_PORT", str(opts.web_port) if opts.web_port != DEFAULT_PORT else None)
    lines = set_env(lines, "COMPOSE_FILE", compose_file_value(bool(hash_file), use_usb))

    rep.step("Port")
    port = check_port(rep, runner, root, lines, opts)
    if port is None:
        return 1
    rep.ok("the dashboard will be on port %d" % port)

    rep.step("Settings")
    if opts.check:
        rep.info("(check only: .env not written, nothing started)")
        return 0 if not rep.fails else 1
    if lines != original or not os.path.isfile(env_path):
        write_lines(env_path, lines)
        rep.ok(".env written (git ignores it; no secrets are in it)")
    else:
        rep.ok(".env is up to date")
    if opts.no_start:
        rep.info("Not started (--no-start). Start it with:  docker compose up -d --build")
        return 0

    rep.step("Starting")
    rep.info("Building the image the first time takes a few minutes.")
    code = run_stream(["docker", "compose", "up", "-d", "--build"], cwd=root)
    if code != 0:
        rep.fail("docker compose could not start the containers", "Run  docker compose up -d --build  to see the whole message.")
        return 1
    rep.info("Waiting for the dashboard...")
    if not wait_for_bridge(rep, runner, root, sleep=sleep, now=now):
        return 1
    where = get_env(lines, "MESHLLM_WEB_BIND") or "127.0.0.1"
    rep.step("Done")
    rep.ok("the dashboard is running: open http://%s:%d/" % (where, port) + ("  and sign in as admin" if hash_file else ""))
    rep.info("The AI model downloads in the background the first time (about 2 GB):  docker compose logs -f model-pull")
    rep.info("Logs:  docker compose logs -f bridge        Stop:  python scripts/setup_env.py --docker-stop")
    rep.info("It starts again after a reboot by itself (restart: unless-stopped) once Docker is running.")
    return 0


def _with_port(run_stream, cmd, root, port):
    """Run a compose command with MESHLLM_WEB_PORT set for it (the demo does not touch .env)."""
    old = os.environ.get("MESHLLM_WEB_PORT")
    os.environ["MESHLLM_WEB_PORT"] = str(port)
    try:
        return run_stream(cmd, cwd=root)
    finally:
        if old is None:
            os.environ.pop("MESHLLM_WEB_PORT", None)
        else:
            os.environ["MESHLLM_WEB_PORT"] = old
