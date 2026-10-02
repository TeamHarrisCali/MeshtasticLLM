"""Diagnostics: one list that answers "why isn't it working?", and a look at the bridge's own log files. Read-only.

Every check is {id, title, status: ok | warn | bad | info, detail, hint}. Nothing here changes anything or transmits.
"""
import os
import platform
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # the project folder (this file is meshllm/diagnostics.py)
LOG_DIR = ROOT / "logs"
# which= value used by the API -> file name inside the log folder
LOG_FILES = {"out": "bridge.log", "err": "bridge.err.log"}
MAX_LOG_LINES = 500
LOG_TAIL_BYTES = 512 * 1024         # only the last 512 KiB of a log is read, so a huge log cannot stall the page
LOW_DISK_BYTES = 2 * 1024 ** 3      # below this (or under 5% free) the disk check warns


def _ago(seconds):
    """Age in seconds as short text ('just now', '12 min ago', '3.0 h ago', '4 days ago'); None means never."""
    if seconds is None:
        return "never"
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h ago"
    return f"{seconds / 86400:.0f} days ago"


def _size(n):
    """Byte count as GB / MB / KB text (decimal units, at least 1 KB)."""
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{max(1, round(n / 1e3))} KB"


def tail_lines(path, lines=200):
    """The last `lines` lines of a text file ([] if it doesn't exist), reading only the end of big files."""
    lines = max(1, min(int(lines), MAX_LOG_LINES))
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL_BYTES))
            data = f.read()
    except OSError:
        return []
    text = data.decode("utf-8", "replace")
    out = text.splitlines()
    if size > LOG_TAIL_BYTES and out:
        out = out[1:]                                   # the first line is probably cut in half
    return out[-lines:]


def read_log(which, lines=200, folder=LOG_DIR):
    """Tail of one of the bridge's log files plus its existence, size and modified time; `which` must be a key of LOG_FILES."""
    if which not in LOG_FILES:
        raise ValueError("which must be out or err")
    path = Path(folder) / LOG_FILES[which]
    return {"which": which, "file": LOG_FILES[which], "exists": path.is_file(), "lines": tail_lines(path, lines),
            "size": path.stat().st_size if path.is_file() else 0, "modified": path.stat().st_mtime if path.is_file() else None}


class Diagnostics:
    """Builds the diagnostics checklist from the running bridge's state. Read-only; never transmits."""

    def __init__(self, bridge, log_dir=None):
        self.bridge = bridge
        self.log_dir = Path(log_dir) if log_dir else LOG_DIR
        self._auto = (0.0, None)                          # when the start-at-login state was last asked, and the answer

    def _autostart(self):
        """Start-at-login state from setup_env (None if it cannot be determined); cached for a minute because the
        page refreshes often and the answer rarely changes."""
        if time.time() - self._auto[0] < 60:
            return self._auto[1]
        result = None
        try:
            if str(ROOT) not in sys.path:             # setup_env.py is the installer script in the project folder, outside the package
                sys.path.insert(0, str(ROOT))
            import setup_env as S
            plan = S.autostart_plan(S.detect_os(), str(ROOT), sys.executable, str(Path.home()), os.getuid() if hasattr(os, "getuid") else 0)
            result = S.autostart_state(plan)
        except Exception:
            result = None                             # diagnostics must never fail just because this check did
        self._auto = (time.time(), result)
        return result

    def report(self):
        """Run every check and return {checks, overall, generated}; overall is the worst status (bad > warn > ok).
        Checks that need an optional subsystem are skipped if it raises, rather than failing the whole report."""
        b, checks = self.bridge, []

        def add(cid, title, status, detail, hint=""):
            """Append one check; status is ok | warn | bad | info, and `hint` says what to do about it (may be empty)."""
            checks.append({"id": cid, "title": title, "status": status, "detail": detail, "hint": hint})

        st = b.status()
        # radio
        node = st["node"] or {}
        if st["connected"]:
            add("radio", "Radio", "ok", f"Connected on {st['port']}" + (f" as {node.get('long_name') or node.get('id')}" if node else "") + ".")
        elif st["searching"]:
            add("radio", "Radio", "bad", "No radio is connected; the bridge is looking for one.",
                "Plug it in by USB, and close any other program that has its serial port open (only one can hold it).")
        else:
            add("radio", "Radio", "warn", "The radio connection looks stalled.", "Unplug and replug the radio; the bridge reconnects by itself.")
        # last packet
        try:
            last = max(b.mesh._seen.values(), default=None)
        except Exception:
            last = None
        if st["connected"]:
            age = None if last is None else time.time() - last
            add("packets", "Last packet heard", "ok" if age is not None and age < 900 else "warn" if age is not None and age < 6 * 3600 else "info",
                f"{_ago(age)}." if age is not None else "Nothing heard since the bridge started.",
                "" if age is not None and age < 900 else "A quiet mesh is normal; if it stays silent for hours, check the antenna and the radio's region setting.")
        # clock
        ok = None
        try:
            ok = b.mesh.clock_ok()
        except Exception:
            pass
        if ok is False:
            add("clock", "Radio clock", "warn", b.mesh.clock_message(), "Set it from the Home page; until then \"last heard\" times from the radio aren't trusted.")
        elif ok:
            add("clock", "Radio clock", "ok", "Agrees with this PC.")
        # ollama
        if st["ollama_ok"]:
            avg = None
            try:
                avg = b.audit.avg_latency_ms(24)
            except Exception:
                pass
            add("ollama", "AI model", "ok", f"'{st['model']}' is available from Ollama." + (f" Average answer time today: {avg / 1000:.1f} s." if avg else ""))
        else:
            add("ollama", "AI model", "bad", f"The model '{st['model']}' isn't available from Ollama.", "Start Ollama, or pick an installed model on the Model page.")
        add("queue", "AI queue", "ok" if st["queue_depth"] < st["max_queue"] else "warn", f"{st['queue_depth']} of {st['max_queue']} places in use" + (" (paused)." if st["paused"] else "."))
        # disk and database
        folder = Path(b.args.db).resolve().parent
        try:
            du = shutil.disk_usage(folder)
            low = du.free < LOW_DISK_BYTES or du.free < du.total * 0.05
            add("disk", "Disk space", "warn" if low else "ok", f"{_size(du.free)} free of {_size(du.total)} where the database lives.",
                "Free some space: the database, map cache and backups all grow over time." if low else "")
        except OSError:
            pass
        try:
            db_size = sum(os.path.getsize(f) for f in (b.args.db, b.args.db + "-wal") if os.path.exists(f))
            add("db", "Database", "ok", f"{_size(db_size)} ({b.args.db}).")
        except OSError:
            pass
        try:
            t = b.tiles.stats()
            add("tiles", "Map cache", "ok" if t["bytes"] < t["max_bytes"] else "warn", f"{t['tiles']} tiles, {_size(t['bytes'])} of {_size(t['max_bytes'])} allowed.")
        except Exception:
            pass
        # backups
        try:
            items = b.backups.list()
            newest = max((i["ts"] for i in items), default=None)
            if not b.backups.auto_enabled() and newest is None:
                add("backup", "Backups", "warn", "Automatic backups are off and there are no backups.", "Turn them on in Settings, or use Back up now.")
            elif newest is None:
                add("backup", "Backups", "info", "No backup yet; the first automatic one is made within the hour.")
            else:
                age = time.time() - newest
                add("backup", "Backups", "ok" if age < 3 * 86400 else "warn", f"Newest backup {_ago(age)}; {len(items)} kept.",
                    "" if age < 3 * 86400 else "Check that automatic backups are on in Settings.")
        except Exception:
            pass
        # logs: the error log counts any non-blank line; the main log only lines the bridge tagged "[error]"
        err_lines = tail_lines(self.log_dir / LOG_FILES["err"], 200)
        out_errors = [l for l in tail_lines(self.log_dir / LOG_FILES["out"], 300) if l.startswith("[error]")]
        real_err = [l for l in err_lines if l.strip()]
        if real_err or out_errors:
            add("errors", "Recent errors", "warn", f"{len(out_errors)} error line(s) in the log and {len(real_err)} line(s) in the error log recently.", "Open the Logs tab below to read them.")
        else:
            add("errors", "Recent errors", "ok", "None in the recent log.")
        # web: a non-loopback bind address exposes message text to the network, so it is flagged
        host = b.args.web_host
        add("web", "Web dashboard", "ok" if host in ("127.0.0.1", "localhost", "::1") else "warn", f"Listening on {host}:{b.args.web_port}" + (" (this PC only)." if host in ("127.0.0.1", "localhost", "::1") else "."),
            "" if host in ("127.0.0.1", "localhost", "::1") else "The dashboard shows message text; anyone who can reach this address can read it.")
        auto = self._autostart()
        if auto:
            add("autostart", "Start at login", "ok" if auto[0] == "installed" else "info", "Installed." if auto[0] == "installed" else "Not set up: the bridge only runs when you start it.",
                "" if auto[0] == "installed" else "Run  python setup_env.py --autostart  to start it when you log in.")
        add("system", "This computer", "info", f"{platform.system()} {platform.release()}, Python {platform.python_version()}, bridge up {_ago(st['uptime_s']).replace(' ago', '') if st['uptime_s'] >= 90 else 'under 2 min'}.")
        # "info" checks never affect the overall result
        worst = "bad" if any(c["status"] == "bad" for c in checks) else "warn" if any(c["status"] == "warn" for c in checks) else "ok"
        return {"checks": checks, "overall": worst, "generated": time.time()}

    def logs(self, which, lines=200):
        """Tail of the 'out' or 'err' log from this instance's log folder."""
        return read_log(which, lines, self.log_dir)
