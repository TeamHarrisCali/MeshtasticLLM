#!/usr/bin/env python3
"""Set this project up on whatever computer it is on (Windows, Linux or macOS).

    python setup_env.py                 detect the computer, (re)build the private Python environment (.venv), install the
                                        dependencies, and check Ollama and the radio; safe to run again any time
    python setup_env.py --check         only look and report; change nothing
    python setup_env.py --recreate      throw the environment away and build it again
    python setup_env.py --pull-model    also download the AI model the bridge is set to use (asks first)
    python setup_env.py --install-ollama   install Ollama if it is missing (asks first, shows the exact command)
    python setup_env.py --start         start the bridge in the background when everything is ready
    python setup_env.py --autostart     start the bridge every time you log in (this user only, no admin/sudo; asks first): a Task
                                        Scheduler task on Windows, a systemd user service on Linux, a launchd agent on macOS
    python setup_env.py --no-autostart  remove that again (the --check report says whether it is installed)
    python setup_env.py --yes           don't ask questions (for scripts)

Moving the project to another computer: copy the folder (audit.db holds all your data and settings; the .venv folder is
specific to a computer and is rebuilt automatically when it doesn't work any more) and run setup.bat (Windows) or ./setup.sh
(Linux / macOS), which find Python and call this script. Only the standard library is used here, so it runs before anything is installed,
and the syntax is kept old enough for it to run (and explain itself) on an outdated Python.
"""
import argparse
import hashlib
import json
import ntpath
import os
import platform
import posixpath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import urllib.error
import urllib.request

MIN_PY = (3, 9)                                   # the code uses str.removeprefix; it is tested on 3.12 and 3.13
OLLAMA_URL = "http://127.0.0.1:11434"
IMPORTS = [("meshtastic", "meshtastic"), ("requests", "requests"), ("serial", "pyserial"), ("pubsub", "pypubsub")]
# USB vendor ids of the chips on Meshtastic boards (Adafruit/Nordic, Espressif, Silicon Labs CP210x, WCH CH340, FTDI, ...)
RADIO_VIDS = {0x239A, 0x303A, 0x10C4, 0x1A86, 0x0403, 0x2886, 0x1915, 0x2E8A}     # the chips Meshtastic boards use (the bridge has the authoritative list)
ROOT = os.path.dirname(os.path.abspath(__file__))


# ---- console output -------------------------------------------------------------------------------------------------------------------------
class Report:
    """Prints a checklist and remembers how it went. ASCII only, so any console shows it."""

    def __init__(self, out=None):
        self.fails = self.warns = 0
        self.out = out or sys.stdout

    def _p(self, text=""):
        """Write one line and flush at once, so progress shows up even when output is piped."""
        self.out.write(text + "\n")
        self.out.flush()

    def step(self, title):
        """Start a new section of the checklist."""
        self._p("\n" + title)

    def ok(self, msg):
        """Print a passed check."""
        self._p("  [ok] " + msg)

    def info(self, msg):
        """Print plain detail lines (indented under the last check); does not count as a warning or failure."""
        for line in str(msg).splitlines():
            self._p("       " + line)

    def warn(self, msg, hint=None):
        """Print a problem that does not stop setup, and count it (reported in the final summary)."""
        self.warns += 1
        self._p("  [!!] " + msg)
        if hint:
            self.info(hint)

    def fail(self, msg, hint=None):
        """Print a problem that stops setup, and count it (main() returns 1 if any were counted)."""
        self.fails += 1
        self._p("  [xx] " + msg)
        if hint:
            self.info(hint)


# ---- what kind of computer is this? ---------------------------------------------------------------------------------------------------------
def parse_os_release(text):
    """/etc/os-release text as a dict (KEY=value lines, quotes removed); blank and comment lines are skipped."""
    out = {}
    for line in (text or "").splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def detect_os(system=None, release=None, machine=None, os_release_text=None, proc_version=None, mac_ver=None):
    """{system: Windows|Linux|Darwin, pretty, distro, like, arch, wsl}. Every input can be supplied, for testing."""
    system = system or platform.system()
    arch = machine or platform.machine()
    info = {"system": system, "arch": arch, "distro": "", "like": "", "wsl": False, "pretty": system}
    if system == "Windows":
        info["pretty"] = "Windows " + (release or platform.release())
    elif system == "Darwin":
        info["pretty"] = "macOS " + (mac_ver if mac_ver is not None else (platform.mac_ver()[0] or ""))
    elif system == "Linux":
        if os_release_text is None:
            try:
                with open("/etc/os-release", encoding="utf-8") as f:
                    os_release_text = f.read()
            except OSError:
                os_release_text = ""
        rel = parse_os_release(os_release_text)
        info["distro"], info["like"] = rel.get("ID", "").lower(), rel.get("ID_LIKE", "").lower()
        info["pretty"] = rel.get("PRETTY_NAME") or "Linux"
        if proc_version is None:
            try:
                with open("/proc/version", encoding="utf-8") as f:
                    proc_version = f.read()
            except OSError:
                proc_version = ""
        # WSL kernels put "microsoft" in /proc/version and in the kernel release string; either one is enough
        info["wsl"] = "microsoft" in (proc_version or "").lower() or "microsoft" in (release or platform.release()).lower()
    return info


def package_manager(info, which=shutil.which):
    """The tool that installs software on this computer, or None."""
    if info["system"] == "Windows":
        return next((m for m in ("winget", "choco") if which(m)), None)
    if info["system"] == "Darwin":
        return "brew" if which("brew") else None
    return next((m for m in ("apt-get", "dnf", "yum", "pacman", "zypper", "apk") if which(m)), None)


# package manager -> command that installs Python (shown to the user, never run by this script)
PYTHON_INSTALL = {
    "winget": "winget install -e --id Python.Python.3.12",
    "choco": "choco install python",
    "brew": "brew install python",
    "apt-get": "sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip",
    "dnf": "sudo dnf install -y python3 python3-pip",
    "yum": "sudo yum install -y python3 python3-pip",
    "pacman": "sudo pacman -S --needed python python-pip",
    "zypper": "sudo zypper install -y python3 python3-pip",
    "apk": "sudo apk add python3 py3-pip",
}


def python_install_hint(info, which=shutil.which):
    """One-line instruction for installing a new enough Python on this computer."""
    cmd = PYTHON_INSTALL.get(package_manager(info, which) or "")
    if cmd:
        return "Install Python " + ".".join(map(str, MIN_PY)) + " or newer with:  " + cmd
    return "Install Python " + ".".join(map(str, MIN_PY)) + " or newer from https://www.python.org/downloads/"


# Ollama has no package-manager route on every Linux distro, so Linux uses Ollama's own install script.
OLLAMA_INSTALL = {"winget": "winget install -e --id Ollama.Ollama", "brew": "brew install ollama",
                  "linux": "curl -fsSL https://ollama.com/install.sh | sh"}


def ollama_install_command(info, which=shutil.which):
    """(command, note) to install Ollama here, or (None, where to get it)."""
    if info["system"] == "Windows":
        return (OLLAMA_INSTALL["winget"], "") if which("winget") else (None, "Download the installer from https://ollama.com/download")
    if info["system"] == "Darwin":
        return (OLLAMA_INSTALL["brew"], "") if which("brew") else (None, "Download the app from https://ollama.com/download")
    if which("curl"):
        return OLLAMA_INSTALL["linux"], "This is Ollama's own install script; it needs sudo and downloads from ollama.com."
    return None, "Install curl, or follow https://ollama.com/download/linux"


# ---- the private Python environment ---------------------------------------------------------------------------------------------------------
def venv_python(root, system=None):
    """Path of the python inside <root>/.venv. The layout differs: Windows uses Scripts/python.exe, Linux and macOS bin/python."""
    sub = ("Scripts", "python.exe") if (system or platform.system()) == "Windows" else ("bin", "python")
    return os.path.join(root, ".venv", *sub)


def run(cmd, cwd=None, timeout=None, shell=False):
    """(exit code, combined output). Never raises: a missing program or a timeout is reported as a failure."""
    try:
        p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, shell=shell)
        return p.returncode, (p.stdout or b"").decode("utf-8", "replace")       # whatever the program prints, never a decoding error
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except (OSError, ValueError) as e:
        return 127, str(e)


def python_version_of(exe):
    """(major, minor) of a Python executable, or None if it doesn't run."""
    code, out = run([exe, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], timeout=30)
    m = re.match(r"(\d+)\.(\d+)", out.strip()) if code == 0 else None
    return (int(m.group(1)), int(m.group(2))) if m else None


def venv_state(root, system=None):
    """('missing' | 'ok' | 'stale', reason). 'stale' = it exists but can't work here (typically copied from another computer)."""
    venv = os.path.join(root, ".venv")
    if not os.path.isdir(venv):
        return "missing", "no environment yet"
    py = venv_python(root, system)
    if not os.path.isfile(py):
        # the other platform's folder exists: the project folder was copied between Windows and Linux/macOS
        other = os.path.isdir(os.path.join(venv, "bin" if (system or platform.system()) == "Windows" else "Scripts"))
        return "stale", "it was made on a different kind of computer" if other else "its Python is missing"
    # pyvenv.cfg records the base interpreter's folder; if that is gone (copied folder, uninstalled Python) the venv cannot start
    cfg = os.path.join(venv, "pyvenv.cfg")
    try:
        with open(cfg, encoding="utf-8") as f:
            home = next((l.split("=", 1)[1].strip() for l in f if l.lower().startswith("home")), "")
    except OSError:
        home = ""
    if home and not os.path.isdir(home):
        return "stale", "the Python it was built from isn't on this computer (%s)" % home
    v = python_version_of(py)
    if v is None:
        return "stale", "its Python doesn't run here"
    if v < MIN_PY:
        return "stale", "it uses Python %d.%d, which is too old" % v
    return "ok", "Python %d.%d" % v


def create_venv(rep, root, python_exe, info, which=shutil.which):
    """Build <root>/.venv with `python_exe`. Reports a failure (and removes the partial folder) and returns False on error."""
    venv = os.path.join(root, ".venv")
    code, out = run([python_exe, "-m", "venv", venv], timeout=300)
    if code != 0 or not os.path.isfile(venv_python(root, info["system"])):
        shutil.rmtree(venv, ignore_errors=True)            # never leave a half-made environment behind
        hint = out.strip()[-400:]
        # Debian/Ubuntu ship Python without the venv module unless python3-venv is installed
        if "ensurepip" in out or "python3-venv" in out or "No module named venv" in out:
            hint = "Python's venv module is not installed.  " + (PYTHON_INSTALL["apt-get"] if package_manager(info, which) == "apt-get" else python_install_hint(info, which))
        rep.fail("could not create the environment", hint)
        return False
    return True


def deps_hash(root, py_version, system):
    """Fingerprint of requirements.txt plus the Python version and OS; when it changes the dependencies are reinstalled."""
    try:
        with open(os.path.join(root, "requirements.txt"), "rb") as f:
            data = f.read()
    except OSError:
        data = b""
    return hashlib.sha256(data + ("|%d.%d|%s" % (py_version[0], py_version[1], system)).encode()).hexdigest()


# Run inside the venv (not here) so it reports what the environment itself can import; one JSON line on stdout.
IMPORT_CHECK = r"""
import importlib, json, sys
try:
    from importlib import metadata
except ImportError:
    metadata = None
out = {}
for mod, dist in %s:
    try:
        importlib.import_module(mod)
        out[mod] = metadata.version(dist) if metadata else "?"
    except Exception as e:
        out[mod] = None
print(json.dumps(out))
""" % repr(IMPORTS)


def check_imports(vpy):
    """{module: version or None} as the environment sees it."""
    code, out = run([vpy, "-c", IMPORT_CHECK], timeout=90)
    try:
        return json.loads(out.strip().splitlines()[-1]) if code == 0 else {m: None for m, _ in IMPORTS}
    except (ValueError, IndexError):
        return {m: None for m, _ in IMPORTS}


def install_deps(rep, root, vpy, info, force=False):
    """pip-install requirements.txt into the venv unless a stamp file says it is already current. Returns True on success."""
    v = python_version_of(vpy) or (0, 0)
    stamp = os.path.join(root, ".venv", ".deps.sha256")
    want = deps_hash(root, v, info["system"])
    have = ""
    try:
        with open(stamp, encoding="utf-8") as f:
            have = f.read().strip()
    except OSError:
        pass
    got = check_imports(vpy)
    if not force and have == want and all(got.values()):
        rep.ok("dependencies already installed and up to date")
        return True
    rep.info("installing the dependencies (needs internet the first time; this can take a minute)...")
    run([vpy, "-m", "pip", "install", "--upgrade", "pip"], timeout=300)                 # a newer pip is nice, never essential
    code, out = run([vpy, "-m", "pip", "install", "-r", os.path.join(root, "requirements.txt")], timeout=1800)
    if code != 0:
        offline = any(s in out for s in ("Could not find a version", "Temporary failure", "Connection", "getaddrinfo", "Max retries", "timed out"))
        rep.fail("installing the dependencies failed", ("It looks like there is no internet connection; the first install needs one.\n" if offline else "") + out.strip()[-600:])
        return False
    try:
        with open(stamp, "w", encoding="utf-8") as f:
            f.write(want)
    except OSError:
        pass
    rep.ok("dependencies installed")
    return True


def verify(rep, root, vpy):
    """Confirm every required package imports and that meshllm.bridge loads. Returns True if both pass."""
    got = check_imports(vpy)
    missing = [m for m, v in got.items() if not v]
    if missing:
        rep.fail("these packages still can't be imported: " + ", ".join(missing), "Run:  python setup_env.py --recreate")
        return False
    rep.ok("packages: " + ", ".join("%s %s" % (m, got[m]) for m, _ in IMPORTS))
    code, out = run([vpy, "-c", "import meshllm.bridge"], cwd=root, timeout=120)
    if code != 0:
        rep.fail("the bridge itself doesn't load", out.strip()[-500:])
        return False
    rep.ok("the bridge loads")
    return True


# ---- Ollama ------------------------------------------------------------------------------------------------------------------------------------
def find_ollama(info, which=shutil.which, env=None):
    """Path of the ollama program: on PATH if possible, else the usual install locations for this OS; None if not found."""
    env = env if env is not None else os.environ
    found = which("ollama")
    if found:
        return found
    candidates = []
    if info["system"] == "Windows":
        candidates = [os.path.join(env.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe"), os.path.join(env.get("ProgramFiles", ""), "Ollama", "ollama.exe")]
    elif info["system"] == "Darwin":
        candidates = ["/usr/local/bin/ollama", "/opt/homebrew/bin/ollama", "/Applications/Ollama.app/Contents/Resources/ollama"]
    else:
        candidates = ["/usr/local/bin/ollama", "/usr/bin/ollama"]
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def ollama_models(url=OLLAMA_URL, timeout=3):
    """Installed model names, or None if Ollama isn't answering."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=timeout) as r:
            return [m.get("name", "") for m in json.loads(r.read().decode("utf-8")).get("models", [])]
    except (urllib.error.URLError, OSError, ValueError):
        return None


def ro_uri(path):
    """A read-only SQLite URI for a file (handles Windows drive letters and spaces)."""
    import pathlib
    return pathlib.Path(os.path.abspath(path)).as_uri() + "?mode=ro"


def configured_model(root):
    """The model the bridge will use: the one saved in its database (chosen in the web UI), else its built-in default."""
    db = os.path.join(root, "audit.db")
    if os.path.isfile(db):
        try:
            con = sqlite3.connect(ro_uri(db), uri=True, timeout=2)
            row = con.execute("SELECT value FROM settings WHERE key='model'").fetchone()
            con.close()
            if row and row[0]:
                return row[0], "saved in the database"
        except sqlite3.Error:
            pass
    try:
        with open(os.path.join(root, "meshllm", "bridge.py"), encoding="utf-8") as f:
            m = re.search(r'^DEFAULT_MODEL\s*=\s*"([^"]+)"', f.read(), re.M)
        if m:
            return m.group(1), "the built-in default"
    except OSError:
        pass
    return "llama3.2:3b", "the built-in default"


def same_model(a, b):
    """True if two model names are the same to Ollama ('llama3.2' equals 'llama3.2:latest')."""
    norm = lambda n: n if ":" in n else n + ":latest"
    return norm(a) == norm(b)


def ask_yes(question, yes=False, interactive=None):
    """True only on an explicit yes. Without a keyboard (piped, scheduled) the answer is no unless --yes was given."""
    if yes:
        return True
    if interactive is None:
        interactive = sys.stdin is not None and sys.stdin.isatty()
    if not interactive:
        return False
    try:
        return input("       %s [y/N] " % question).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def check_ollama(rep, root, info, opts, which=shutil.which):
    """Report whether Ollama is installed, running, and has the configured model; optionally install it or pull the model
    (only with the matching option, only outside --check, and only after ask_yes). Never fails the run: problems are warnings."""
    binary = find_ollama(info, which)
    models = ollama_models(opts.ollama_url)
    model, why = configured_model(root)
    if models is None and not binary:
        cmd, note = ollama_install_command(info, which)
        rep.warn("Ollama (the local AI) is not installed", ("Install it with:  " + cmd + ("\n" + note if note else "")) if cmd else note)
        if opts.install_ollama and cmd and not opts.check:
            if ask_yes("Run this now?  " + cmd, opts.yes):
                subprocess.call(cmd, shell=True)
                binary = find_ollama(info, which)
                (rep.ok if binary else rep.warn)("Ollama installed" if binary else "the install didn't finish; install it by hand and run this script again")
            else:
                rep.info("skipped")
        elif opts.install_ollama and not cmd:
            rep.info(note)
        if not binary:
            return
    if models is None:
        rep.warn("Ollama is installed but not running", {"Windows": "Start it from the Start menu (Ollama), or run:  ollama serve", "Darwin": "Open the Ollama app, or run:  ollama serve"}.get(info["system"], "Start it with:  ollama serve   (or: sudo systemctl start ollama)"))
        return
    rep.ok("Ollama is running (%d model%s installed)" % (len(models), "" if len(models) == 1 else "s"))
    if any(same_model(model, m) for m in models):
        rep.ok("the AI model '%s' (%s) is installed" % (model, why))
        return
    rep.warn("the AI model '%s' (%s) is not installed yet" % (model, why), "Download it with:  ollama pull %s   (or choose another on the Model page)" % model)
    if opts.pull_model and binary and not opts.check:
        if ask_yes("Download '%s' now? (can be several GB)" % model, opts.yes):
            code = subprocess.call([binary, "pull", model])
            (rep.ok if code == 0 else rep.warn)("downloaded '%s'" % model if code == 0 else "the download didn't finish; run:  ollama pull %s" % model)


# ---- the radio ---------------------------------------------------------------------------------------------------------------------------------
# Run inside the venv because pyserial is installed there, not necessarily in the Python running this script.
PORT_SNIPPET = r"""
import json
from serial.tools import list_ports
print(json.dumps([{"device": p.device, "vid": p.vid, "pid": p.pid, "desc": p.description or "", "maker": p.manufacturer or ""} for p in list_ports.comports()]))
"""


def list_serial(vpy):
    """Serial ports as dicts (device, vid, pid, desc, maker), as seen by the venv's pyserial; [] on any error."""
    code, out = run([vpy, "-c", PORT_SNIPPET], timeout=60)
    try:
        return json.loads(out.strip().splitlines()[-1]) if code == 0 else []
    except (ValueError, IndexError):
        return []


def linux_serial_advice(device, groups_of_user, group_of_device):
    """Hint text if this Linux user can't open the serial device, else None. Both lists are supplied, for testing."""
    if group_of_device and group_of_device not in groups_of_user:
        return "Your user isn't in the '%s' group, which owns %s.  Run:  sudo usermod -aG %s $USER   then log out and back in." % (group_of_device, device, group_of_device)
    return None


def check_radio(rep, info, vpy):
    """Report whether a Meshtastic radio is plugged in, plus OS-specific causes of "radio not found" (permissions, ModemManager, brltty, WSL)."""
    ports = list_serial(vpy)
    radios = [p for p in ports if p.get("vid") in RADIO_VIDS]
    if not radios:
        rep.info("no radio plugged in right now" + (" (%d other serial device%s)" % (len(ports), "" if len(ports) == 1 else "s") if ports else ""))
        hints = {"Windows": "Plug it in; if no COM port appears in Device Manager, install the CP210x or CH340 USB driver for your board.",
                 "Darwin": "Plug it in; newer macOS needs no driver (older ones need the CH340/CP210x driver).",
                 "Linux": "Plug it in; check with:  dmesg | tail"}
        rep.info(hints.get(info["system"], "") + " The bridge waits for it and connects by itself.")
    for p in radios:
        rep.ok("radio found: %s  (%s)" % (p["device"], (p["desc"] or p["maker"] or "USB serial").strip()))
        if info["system"] == "Linux":
            try:
                import grp
                gname = grp.getgrgid(os.stat(p["device"]).st_gid).gr_name
                mine = {grp.getgrgid(g).gr_name for g in os.getgroups()}
            except (ImportError, KeyError, OSError):
                gname, mine = "", set()
            # on Linux the serial device is owned by a group (often dialout/uucp) the user must belong to
            if not os.access(p["device"], os.R_OK | os.W_OK):
                rep.warn("this user can't open %s" % p["device"], linux_serial_advice(p["device"], mine, gname) or "Check the permissions of the device.")
    if info["system"] == "Linux":
        # ModemManager and brltty probe new USB serial devices and can hold them open, which blocks the bridge
        active = run(["systemctl", "is-active", "ModemManager"], timeout=5)
        if active[0] == 0 and active[1].strip() == "active":
            rep.warn("ModemManager is running and can grab Meshtastic radios", "If the radio isn't found or keeps disconnecting:  sudo systemctl disable --now ModemManager")
        if shutil.which("dpkg") and run(["dpkg", "-s", "brltty"], timeout=5)[0] == 0:
            rep.warn("brltty is installed and can grab CH340 radios", "If the radio isn't found:  sudo apt remove brltty")
    if info["wsl"]:
        rep.warn("this is WSL: USB devices aren't visible here by default", "Run the bridge natively on Windows, or attach the radio with usbipd-win.")


# ---- the rest --------------------------------------------------------------------------------------------------------------------------------
def fix_line_endings(path):
    """Turn Windows line endings (CRLF) into Unix ones in a shell script. True if the file was changed.
    A script copied from Windows fails on Linux/macOS ("bad interpreter") because the shebang line ends in a carriage return."""
    with open(path, "rb") as f:
        data = f.read()
    crlf, lf = bytes([13, 10]), bytes([10])
    if crlf not in data:
        return False
    with open(path, "wb") as f:
        f.write(data.replace(crlf, lf))
    return True


def prepare_folder(rep, root, info):
    """Create logs/, make the .sh scripts runnable on Linux/macOS, and say whether an existing audit.db is usable. Reads the database read-only."""
    os.makedirs(os.path.join(root, "logs"), exist_ok=True)
    if info["system"] != "Windows":
        for name in ("setup.sh", "start_bridge.sh", "stop_bridge.sh"):
            path = os.path.join(root, name)
            if os.path.isfile(path):
                fixed = fix_line_endings(path)
                if fixed:
                    rep.ok("%s had Windows line endings (it would not run here): fixed" % name)
                os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    db = os.path.join(root, "audit.db")
    if os.path.isfile(db):
        try:
            con = sqlite3.connect(ro_uri(db), uri=True, timeout=2)
            n = con.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            con.close()
            rep.ok("existing data found (audit.db, %d logged messages): it is used as it is" % n)
        except sqlite3.Error:
            rep.warn("audit.db exists but can't be read", "It may be damaged or from another program. Move it aside to start fresh.")
    else:
        rep.info("no database yet: a new one is created the first time the bridge starts")


def start_command(info):
    """The command a user types to start the bridge on this OS (shown in the final message)."""
    return r"powershell -NoProfile -ExecutionPolicy Bypass -File .\start_bridge.ps1" if info["system"] == "Windows" else "./start_bridge.sh"


def find_python(explicit=None):
    """The interpreter to build the environment with: the one asked for, else the one running this script."""
    return explicit or sys.executable


# ---- start at login (per user, no administrator / sudo) -----------------------------------------------------------------------------------
# Windows: a Task Scheduler task.  Linux: a systemd USER unit.  macOS: a launchd agent.  Everything below up to autostart_state() is pure
# (it returns text and command lists and touches nothing), so the tests can check every OS from any OS.
TASK_NAME = "MeshLLMBridge"
UNIT_NAME = "mesh-llm-bridge.service"
LAUNCHD_LABEL = "com.meshllm.bridge"
SCHTASKS_TR_LIMIT = 261                                   # Task Scheduler refuses a /TR command longer than this
SYSTEMD_FALLBACK = ("Add ./start_bridge.sh to your desktop's startup applications instead (it starts the bridge when you log in to the desktop).\n"
                    "On WSL, run the bridge on the Windows side, or turn systemd on (/etc/wsl.conf:  [boot]  systemd=true) and run this again.")


def bridge_python(root, info, state_fn=None):
    """(python to run the bridge with, venv state, reason): the project's .venv when it works here, else the interpreter running this script."""
    state, reason = (state_fn or venv_state)(root, info["system"])
    return (venv_python(root, info["system"]) if state == "ok" else sys.executable), state, reason


def systemd_arg(text, is_exec=True):
    """One word of a systemd unit line: double-quoted when it has spaces or quotes, with % (and, in ExecStart, $) doubled as systemd wants."""
    text = text.replace("%", "%%")
    if is_exec:
        text = text.replace("$", "$$")
    if any(c in text for c in " \t\"'\\;"):
        text = '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def systemd_unit_text(root, python_exe):
    """Text of the systemd user unit. Restart=on-failure so a deliberate stop is not undone; WantedBy=default.target is the user-level 'at login'."""
    return "\n".join([
        "[Unit]",
        "Description=Meshtastic to Ollama bridge (python -m meshllm)",
        "After=network.target",
        "",
        "[Service]",
        "Type=simple",
        "WorkingDirectory=" + systemd_arg(root, False),
        "ExecStart=%s -u -m meshllm" % systemd_arg(python_exe),
        "Restart=on-failure",
        "RestartSec=10",
        "",
        "[Install]",
        "WantedBy=default.target",
        ""])


def launchd_plist_text(root, python_exe):
    """The launchd agent as XML (plistlib escapes &, < and > in paths). Restarts only after a failure; logs go under <root>/logs."""
    import plistlib
    data = {"Label": LAUNCHD_LABEL,
            "ProgramArguments": [python_exe, "-u", "-m", "meshllm"],
            "WorkingDirectory": root,
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "StandardOutPath": posixpath.join(root, "logs", "bridge.log"),
            "StandardErrorPath": posixpath.join(root, "logs", "bridge.err.log")}
    return plistlib.dumps(data, sort_keys=False).decode("utf-8")


def schtasks_create_cmd(root):
    """Create (or replace: /F) a task that runs start_bridge.ps1 hidden when this user logs on. No /RU and /RL LIMITED: it runs as the current user, never elevated."""
    script = ntpath.join(root, "start_bridge.ps1")
    tr = 'powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%s"' % script
    return ["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "ONLOGON", "/TR", tr, "/RL", "LIMITED", "/F"]


def autostart_plan(info, root, python_exe, home, uid):
    """Everything needed to turn start-at-login on, off, or look at it, as data:
    {kind: schtasks|systemd|launchd|none, files: {path: text}, dirs: [...], pre_cmds: [...] (failures ignored), install_cmds: [[...]],
     remove_cmds: [[...]], after_remove_cmds: [[...]], query_cmd: [...], summary, note, problem}"""
    plan = {"kind": "none", "files": {}, "dirs": [], "pre_cmds": [], "install_cmds": [], "remove_cmds": [], "after_remove_cmds": [],
            "query_cmd": [], "summary": "", "note": "", "problem": ""}
    system = info["system"]
    if system == "Windows":
        cmd = schtasks_create_cmd(root)
        plan.update(kind="schtasks", install_cmds=[cmd], remove_cmds=[["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]],
                    query_cmd=["schtasks", "/Query", "/TN", TASK_NAME],
                    summary='a Task Scheduler task named "%s" (this user only) that runs start_bridge.ps1 hidden each time you log in' % TASK_NAME,
                    note="It takes effect at your next login. To start the bridge right now:  powershell -NoProfile -ExecutionPolicy Bypass -File .\\start_bridge.ps1")
        if len(cmd[cmd.index("/TR") + 1]) > SCHTASKS_TR_LIMIT:
            plan["problem"] = "the project folder's path is too long for Task Scheduler (%d characters is the limit for the command)" % SCHTASKS_TR_LIMIT
    elif system == "Darwin":
        plist = posixpath.join(home, "Library", "LaunchAgents", LAUNCHD_LABEL + ".plist")
        target = "gui/%s" % uid
        plan.update(kind="launchd", files={plist: launchd_plist_text(root, python_exe)}, dirs=[posixpath.join(root, "logs")],
                    pre_cmds=[["launchctl", "bootout", "%s/%s" % (target, LAUNCHD_LABEL)]],       # in case an older copy is loaded (bootstrap would refuse)
                    install_cmds=[["launchctl", "bootstrap", target, plist]],
                    remove_cmds=[["launchctl", "bootout", "%s/%s" % (target, LAUNCHD_LABEL)]],
                    query_cmd=["launchctl", "print", "%s/%s" % (target, LAUNCHD_LABEL)],
                    summary="a launchd agent (%s) that starts the bridge when you log in and restarts it if it crashes" % plist,
                    note="It starts now and at every login. To stop it for good:  python setup_env.py --no-autostart")
    elif system == "Linux":
        unit = posixpath.join(home, ".config", "systemd", "user", UNIT_NAME)
        plan.update(kind="systemd", files={unit: systemd_unit_text(root, python_exe)},
                    install_cmds=[["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", UNIT_NAME]],
                    remove_cmds=[["systemctl", "--user", "disable", "--now", UNIT_NAME]],
                    after_remove_cmds=[["systemctl", "--user", "daemon-reload"]],
                    query_cmd=["systemctl", "--user", "is-enabled", UNIT_NAME],
                    summary="a systemd user service (%s) that starts the bridge when you log in and restarts it if it crashes" % unit,
                    note="It starts now and at every login. To start it at boot instead of at login (before you log in), run once:  loginctl enable-linger $USER\n"
                         "To stop it for good:  python setup_env.py --no-autostart   (or: systemctl --user stop mesh-llm-bridge)")
    else:
        plan["problem"] = "start at login isn't supported on this kind of computer (%s)" % system
    return plan


def autostart_blocker(plan, which=shutil.which, isdir=os.path.isdir):
    """(message, hint) if the machinery this plan needs isn't here, else None."""
    # /run/systemd/system exists only when systemd is actually running as init (not in most containers or older WSL)
    if plan["kind"] == "none":
        return plan["problem"] or "start at login isn't supported here", ""
    if plan["kind"] == "systemd" and (not which("systemctl") or not isdir("/run/systemd/system")):
        return "systemd isn't running here (normal for WSL without systemd and for containers), so start at login can't be set up", SYSTEMD_FALLBACK
    if plan["kind"] == "launchd" and not which("launchctl"):
        return "launchctl isn't available", "Add the bridge to System Settings > General > Login Items by hand (start_bridge.sh)."
    if plan["kind"] == "schtasks" and not which("schtasks"):
        return "schtasks (Task Scheduler) isn't available", "Put a shortcut to start_bridge.ps1 in the Startup folder instead (Win+R, then: shell:startup)."
    return None


def autostart_state(plan, runner=run, exists=os.path.isfile):
    """('installed' | 'not installed' | 'unknown', detail). Only looks; changes nothing."""
    if plan["kind"] == "none":
        return "unknown", plan["problem"]
    code, out = runner(plan["query_cmd"], timeout=30)
    if code == 127:
        return "unknown", "%s isn't available here" % plan["query_cmd"][0]
    if plan["kind"] == "systemd" and ("Failed to connect" in out or "not been booted" in out or "No medium found" in out):
        return "unknown", "systemd isn't running here"
    if code == 0:
        return "installed", ""
    present = [p for p in plan["files"] if exists(p)]
    return "not installed", ("the file %s exists but isn't active" % present[0]) if present else ""


# ---- applying the plan (these run commands and write files) ---------------------------------------------------------------------------------
def autostart_install(rep, plan, runner=run, ask=ask_yes, yes=False, which=shutil.which, isdir=os.path.isdir):
    """Turn start-at-login on. Asks first unless yes. False only if something went wrong (declining is not a failure)."""
    blocked = autostart_blocker(plan, which, isdir)
    if blocked:
        rep.warn(*blocked) if blocked[1] else rep.warn(blocked[0])
        return False if plan["kind"] == "none" else True          # "can't here" is explained, not an error; nothing was changed
    if plan["problem"]:
        rep.fail(plan["problem"])
        return False
    rep.info("This will create " + plan["summary"] + ".")
    if not ask("Set up start at login for this user?", yes):
        rep.info("skipped: nothing was changed")
        return True
    try:
        for d in plan["dirs"]:
            os.makedirs(d, exist_ok=True)
        for path, text in plan["files"].items():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
    except OSError as e:
        rep.fail("couldn't write the start-at-login file", str(e))
        return False
    for cmd in plan["pre_cmds"]:     # best-effort cleanup (e.g. unloading an older launchd copy); failures are expected and ignored
        runner(cmd, timeout=60)
    for cmd in plan["install_cmds"]:
        code, out = runner(cmd, timeout=120)
        if code != 0:
            hint = out.strip()[-400:]
            if plan["kind"] == "schtasks":
                hint += "\nIf it says access is denied, run this from a terminal opened with 'Run as administrator', or put a shortcut to start_bridge.ps1 in the Startup folder (Win+R, then: shell:startup)."
            rep.fail("couldn't set up start at login (%s failed)" % " ".join(cmd[:3]), hint)
            return False
    state, detail = autostart_state(plan, runner)
    if state == "not installed":
        rep.warn("the commands ran but start at login doesn't show as active", detail)
        return False
    rep.ok("start at login is on")
    rep.info(plan["note"])
    return True


def autostart_remove(rep, plan, runner=run, which=shutil.which, isdir=os.path.isdir, exists=os.path.isfile):
    """Turn start-at-login off and delete what --autostart made. False only if something went wrong."""
    state, _ = autostart_state(plan, runner, exists)
    left = [p for p in plan["files"] if exists(p)]
    if state != "installed" and not left:
        rep.ok("start at login is not installed: nothing to remove")
        return True
    if not autostart_blocker(plan, which, isdir):
        for cmd in plan["remove_cmds"]:
            runner(cmd, timeout=60)                              # not loaded / already gone is fine: the files go either way
    for path in left:
        try:
            os.remove(path)
        except OSError as e:
            rep.fail("couldn't delete " + path, str(e))
            return False
    if not autostart_blocker(plan, which, isdir):
        for cmd in plan["after_remove_cmds"]:
            runner(cmd, timeout=60)
    if autostart_state(plan, runner, exists)[0] == "installed":
        rep.fail("start at login still shows as installed", "Remove it by hand:  " + " ".join(plan["remove_cmds"][0]))
        return False
    rep.ok("start at login removed (the bridge itself is untouched; it is stopped by stop_bridge, not by this)")
    return True


def run_autostart(rep, root, info, opts, runner=run, ask=ask_yes, which=shutil.which, home=None, uid=None, isdir=os.path.isdir):
    """--autostart / --no-autostart as a section of its own. Returns the exit code."""
    rep.step("Start at login")
    py, state, reason = bridge_python(root, info)
    plan = autostart_plan(info, root, py, home or os.path.expanduser("~"), uid if uid is not None else (os.getuid() if hasattr(os, "getuid") else 0))
    if opts.no_autostart:
        return 0 if autostart_remove(rep, plan, runner, which, isdir) else 1
    if state != "ok":
        rep.warn("the private environment (.venv) isn't ready (%s)" % reason, "Run  python setup_env.py  first; until then the bridge would start with " + py)
    return 0 if autostart_install(rep, plan, runner, ask, opts.yes, which, isdir) else 1


# ---- command line ---------------------------------------------------------------------------------------------------------------------------
def main(argv=None, root=None, out=None, which=shutil.which, runner=run, ask=ask_yes, home=None, uid=None, info=None, isdir=os.path.isdir):
    """runner, ask, home, uid, info and isdir are for the tests (they stand in for the machine); the defaults are the real thing."""
    ap = argparse.ArgumentParser(description="Set this project up on this computer.", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n", 1)[1])
    ap.add_argument("--check", action="store_true", help="only report; change nothing")
    ap.add_argument("--recreate", action="store_true", help="delete the .venv and build it again")
    ap.add_argument("--force", action="store_true", help="reinstall the dependencies even if they look current")
    ap.add_argument("--pull-model", action="store_true", help="download the configured AI model if it is missing (asks first)")
    ap.add_argument("--install-ollama", action="store_true", help="install Ollama if it is missing (asks first)")
    ap.add_argument("--start", action="store_true", help="start the bridge in the background when everything is ready")
    ap.add_argument("--autostart", action="store_true", help="start the bridge at every login of this user (asks first)")
    ap.add_argument("--no-autostart", action="store_true", help="remove the start-at-login setup")
    ap.add_argument("--yes", "-y", action="store_true", help="answer yes to questions")
    ap.add_argument("--python", help="build the environment with this Python (default: the one running this script)")
    ap.add_argument("--dir", help="the project folder (default: where this script is)")
    ap.add_argument("--ollama-url", default=OLLAMA_URL, help=argparse.SUPPRESS)
    opts = ap.parse_args(argv)
    # the sections below are checks first, changes second: --check must never modify anything
    if opts.autostart and opts.no_autostart:
        ap.error("--autostart and --no-autostart can't be used together")
    root = os.path.abspath(opts.dir or root or ROOT)
    rep = Report(out)
    info = info or detect_os()

    rep.step("This computer")
    rep.ok("%s (%s), folder %s" % (info["pretty"], info["arch"], root))
    if not os.path.isfile(os.path.join(root, "meshllm", "bridge.py")):
        rep.fail("this isn't the project folder (meshllm/bridge.py is missing)", "Run this script from the project folder, or pass --dir.")
        return 1

    if (opts.autostart or opts.no_autostart) and not opts.check:          # its own job: the rest of the setup is not run (that is python setup_env.py)
        return run_autostart(rep, root, info, opts, runner, ask, which, home, uid, isdir)

    rep.step("Python")
    py = find_python(opts.python)
    v = python_version_of(py)
    if v is None:
        rep.fail("can't run Python (%s)" % py, python_install_hint(info, which))
        return 1
    if v < MIN_PY:
        rep.fail("Python %d.%d is too old (needs %d.%d or newer)" % (v + MIN_PY), python_install_hint(info, which) + "\nThen run this script with the new one, or:  python setup_env.py --python /path/to/python")
        return 1
    rep.ok("Python %d.%d (%s)" % (v[0], v[1], py))

    rep.step("Private environment (.venv)")
    state, reason = venv_state(root, info["system"])
    if opts.check:
        if state == "ok":
            rep.ok("%s: %s" % (state, reason))
        else:
            rep.warn("%s: %s" % (state, reason), "Run:  python setup_env.py")
    else:
        # a venv cannot delete itself while its own python is running this script (on Windows the files are locked)
        if (state == "stale" or (opts.recreate and state != "missing")) and os.path.abspath(sys.prefix).startswith(os.path.join(root, ".venv")):
            rep.fail("this script is running from inside the environment it needs to rebuild",
                     "Run it with the system Python instead:  python setup_env.py --recreate   (not the Python inside the .venv folder)")
            return 1
        if state == "stale" or (opts.recreate and state != "missing"):
            rep.warn("rebuilding the environment (%s)" % ("you asked for it" if opts.recreate and state == "ok" else reason))
            shutil.rmtree(os.path.join(root, ".venv"), ignore_errors=True)
            if os.path.isdir(os.path.join(root, ".venv")):
                rep.fail("couldn't remove the old .venv folder", "Stop the bridge (stop_bridge) and close anything using it, then run this again.")
                return 1
            state = "missing"
        if state == "missing":
            rep.info("creating the environment...")
            if not create_venv(rep, root, py, info, which):
                return 1
            rep.ok("environment created")
        else:
            rep.ok("environment is fine (%s)" % reason)
    vpy = venv_python(root, info["system"])
    if os.path.isfile(vpy) and state != "stale":
        if opts.check:
            got = check_imports(vpy)
            missing = [m for m, x in got.items() if not x]
            if missing:
                rep.warn("missing packages: " + ", ".join(missing), "Run:  python setup_env.py")
            else:
                rep.ok("packages installed")
        else:
            rep.step("Dependencies")
            if not install_deps(rep, root, vpy, info, opts.force or opts.recreate):
                return 1
            if not verify(rep, root, vpy):
                return 1
        rep.step("Ollama (the local AI)")
        check_ollama(rep, root, info, opts, which)
        rep.step("Radio")
        check_radio(rep, info, vpy)
    else:
        rep.step("Ollama (the local AI)")
        check_ollama(rep, root, info, opts, which)
    if opts.check:
        rep.step("Start at login")
        plan = autostart_plan(info, root, bridge_python(root, info)[0], home or os.path.expanduser("~"), uid if uid is not None else (os.getuid() if hasattr(os, "getuid") else 0))
        state, detail = autostart_state(plan, runner)
        rep.ok("start at login: %s%s" % (state, " (%s)" % detail if detail else (" (turn it on with: python setup_env.py --autostart)" if state == "not installed" else "")))
        if opts.autostart or opts.no_autostart:
            rep.info("--check only looks: nothing was changed")
    rep.step("Folder")
    if not opts.check:
        prepare_folder(rep, root, info)
    else:
        rep.info("(check only: nothing changed)")

    rep.step("Result")
    if rep.fails:
        rep.fail("%d problem%s to fix (see [xx] above)" % (rep.fails, "" if rep.fails == 1 else "s"))
        return 1
    if opts.check:
        rep.ok("nothing failed" + (" (%d warning%s)" % (rep.warns, "" if rep.warns == 1 else "s") if rep.warns else ""))
        return 0
    rep.ok("ready" + (" (%d thing%s worth a look above)" % (rep.warns, "" if rep.warns == 1 else "s") if rep.warns else ""))
    rep.info("Start the bridge:   " + start_command(info))
    rep.info("Then open:         http://127.0.0.1:8080/")
    rep.info("Optional:          python setup_env.py --autostart   (start the bridge at every login)")
    # --start runs the platform's own launcher script, which detaches the bridge; Windows needs powershell to run a .ps1
    if opts.start:
        script = os.path.join(root, "start_bridge.ps1" if info["system"] == "Windows" else "start_bridge.sh")
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script] if info["system"] == "Windows" else [script]
        code, text = run(cmd, cwd=root, timeout=120)
        rep.ok("bridge started") if code == 0 else rep.fail("couldn't start the bridge", text.strip()[-300:])
        return 0 if code == 0 else 1
    return 0


if __name__ == "__main__":
    # replace characters the console cannot show instead of crashing (older Windows consoles are not UTF-8)
    try:
        sys.stdout.reconfigure(errors="replace")            # any console can show the output
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
