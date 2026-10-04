"""Smoke test of a packaged program, the same on Linux, Windows and macOS (CI runs it after every build; you can run it on your own build).

    python scripts/smoke_binary.py dist/meshllm-0.1.0-linux-x86_64          # the folder build_binary.py made

It starts the program the way a user would and checks that:
  1. `--version` prints the version in meshllm/__init__.py, and `--self-check` finds the bundled files, the USB/Wi-Fi interfaces and the
     operating system's Bluetooth backend;
  2. `--demo --demo-scripted` serves the dashboard (index page, script bundle, /api/status with the version, the bundled docs and evaluation
     results) and, where the operating system allows, stops cleanly when asked to;
  3. a run with no demo and a radio address that does not exist (a documentation-only address, so no real radio or port is touched) writes
     audit.db, and a backup made through the dashboard, into the data folder given by MESHLLM_DATA_DIR (a temporary one here);
  4. nothing was written inside the program's own folder.

Standard library only. Exit code 0 = everything passed. Nothing here needs a radio, Ollama or the network (only 127.0.0.1).
"""
import hashlib
import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # the project folder (this file is scripts/smoke_binary.py)
fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"), flush=True)
    if not cond:
        fails.append(name)


def read_version():
    """__version__ from meshllm/__init__.py (as text)."""
    import re
    with open(os.path.join(ROOT, "meshllm", "__init__.py"), encoding="utf-8") as f:
        return re.search(r'^__version__\s*=\s*"([^"]+)"', f.read(), re.M).group(1)


def free_port():
    """A port nothing listens on right now."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def snapshot(folder):
    """{relative path: sha256} of every file under `folder`, to see whether anything changed."""
    out = {}
    for d, _, files in os.walk(folder):
        for f in files:
            p = os.path.join(d, f)
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    h.update(block)
            out[os.path.relpath(p, folder)] = h.hexdigest()
    return out


def get(port, path, method="GET", body=None):
    """(status code, body bytes) of a request to the program's dashboard; never raises for an HTTP error."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=body, headers={"Content-Type": "application/json"} if body else {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def run_program(exe, args, data_dir, port):
    """Start the program in the background with its data folder set; returns the Popen (output goes to a file in the data folder's parent)."""
    env = dict(os.environ, MESHLLM_DATA_DIR=data_dir, PYTHONIOENCODING="utf-8")
    log = open(os.path.join(os.path.dirname(data_dir), os.path.basename(data_dir) + ".log"), "wb")
    kw = {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)} if os.name == "nt" else {}
    return subprocess.Popen([exe] + args + ["--web-port", str(port)], env=env, stdout=log, stderr=subprocess.STDOUT, **kw), log


def wait_ready(port, proc, seconds=90):
    """Poll /api/status until it answers (the program starts slowly the first time on a cold disk); returns the parsed JSON or None."""
    end = time.time() + seconds
    while time.time() < end and proc.poll() is None:
        try:
            code, body = get(port, "/api/status")
            if code == 200:
                return json.loads(body)
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    return None


def stop(proc):
    """Ask the program to stop (SIGTERM; Windows has no such thing for a console program, so it is ended) and return its exit code, or None if it had to be killed."""
    if os.name == "nt":
        proc.terminate()
    else:
        proc.send_signal(signal.SIGTERM)
    try:
        return proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return None


def main(argv):
    if len(argv) != 1:
        print(__doc__)
        return 2
    folder = os.path.abspath(argv[0])
    exe = os.path.join(folder, "meshllm.exe" if os.name == "nt" else "meshllm")
    version = read_version()
    check("the program is in the folder", os.path.isfile(exe), exe)
    if fails:
        return 1
    before = snapshot(folder)
    scratch = tempfile.mkdtemp(prefix="meshllm_smoke_")

    # 1. --version and --self-check
    p = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=120)
    check("--version prints the version", p.returncode == 0 and version in (p.stdout + p.stderr), (p.returncode, p.stdout, p.stderr))
    p = subprocess.run([exe, "--self-check"], capture_output=True, text=True, timeout=120, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    print(p.stdout.rstrip() + ("\n" + p.stderr.rstrip() if p.stderr.strip() else ""), flush=True)
    check("--self-check finds every bundled part", p.returncode == 0 and "FAIL" not in p.stdout, (p.returncode, p.stdout[-400:]))
    check("the self-check names this program's version and says packaged", f"meshllm {version} (packaged)" in p.stdout, p.stdout[:200])

    # 2. demo mode
    port = free_port()
    demo_data = os.path.join(scratch, "demo-data")
    proc, log = run_program(exe, ["--demo", "--demo-scripted"], demo_data, port)
    try:
        status = wait_ready(port, proc)
        check("demo: the dashboard answers /api/status", status is not None, "no answer; see the log below")
        if status is not None:
            check("demo: it reports the version and demo mode", status.get("version") == version and status.get("demo") is True, status.get("version"))
            deadline = time.time() + 30
            while not status.get("connected") and time.time() < deadline:
                time.sleep(0.5)
                status = json.loads(get(port, "/api/status")[1])
            check("demo: the simulated radio is connected", status.get("connected") is True, status)
            code, body = get(port, "/")
            check("demo: the index page is served", code == 200 and b"Mesh LLM" in body, code)
            code, body = get(port, "/app.js")
            check("demo: the script bundle is served", code == 200 and len(body) > 50000, (code, len(body)))
            code, body = get(port, "/api/docs?name=setup")
            check("demo: a bundled doc is readable", code == 200 and b"Setting up" in body, code)
            code, body = get(port, "/api/evals")
            check("demo: the bundled evaluation results are listed", code == 200 and len(json.loads(body).get("runs", [])) > 0, code)
            code, body = get(port, "/api/diagnostics")
            checks = json.loads(body)["checks"] if code == 200 else []
            check("demo: Diagnostics shows the version", any(c["id"] == "version" and version in c["detail"] for c in checks), code)
            check("demo: Diagnostics has no start-at-login check (the installer's feature; the installer is not in the program)", checks and not any(c["id"] == "autostart" for c in checks), [c["id"] for c in checks])
    finally:
        rc = stop(proc)
        log.close()
    check("demo: it stops when asked" + (" (cleanly, exit code 0)" if os.name != "nt" else ""), rc is not None and (os.name == "nt" or rc == 0), rc)
    if fails:
        print("--- demo log", flush=True)
        print(open(demo_data + ".log", "rb").read().decode("utf-8", "replace")[-3000:])

    # 3. a normal (not demo) start with an unreachable Wi-Fi radio: the database and backups belong in MESHLLM_DATA_DIR
    port = free_port()
    live_data = os.path.join(scratch, "live-data")
    proc, log = run_program(exe, ["--tcp", "192.0.2.1", "--no-warm-up"], live_data, port)
    try:
        status = wait_ready(port, proc)
        check("live: the dashboard answers with no radio", status is not None and status.get("demo") is False, status)
        if status is not None:
            code, body = get(port, "/api/backups/create", "POST", b"{}")
            check("live: a backup can be made", code == 200, (code, body[:200]))
    finally:
        rc = stop(proc)
        log.close()
    check("live: audit.db is in the data folder", os.path.isfile(os.path.join(live_data, "audit.db")), os.listdir(live_data) if os.path.isdir(live_data) else "no data folder")
    backups = os.path.join(live_data, "backups")
    check("live: the backup is in the data folder", os.path.isdir(backups) and any(n.endswith(".db") for n in os.listdir(backups)))
    if len([f for f in fails if f.startswith("live")]):
        print("--- live log", flush=True)
        print(open(live_data + ".log", "rb").read().decode("utf-8", "replace")[-3000:])

    # 4. nothing inside the program's own folder changed
    after = snapshot(folder)
    check("nothing was written inside the program's folder", after == before,
          [k for k in set(before) | set(after) if before.get(k) != after.get(k)][:10])
    shutil.rmtree(scratch, ignore_errors=True)
    print("\n" + (f"{len(fails)} failed: " + ", ".join(fails) if fails else "all passed"), flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
