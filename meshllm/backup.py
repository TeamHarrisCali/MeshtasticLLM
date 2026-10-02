"""Backing up and restoring everything the bridge stores (settings, the AI log, direct messages, channel messages, node data).

A backup is a consistent copy of the database file, made with SQLite's own backup call while the bridge keeps running.
A daily automatic backup keeps the newest few. Restoring never replaces the live database in place (the bridge is using
it): the chosen file is checked and set aside, and swapped in the next time the bridge starts, with the old database kept.
The map tile cache is not included (it refills by itself). A backup holds message text, so treat the file like the database.
"""
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

KEEP_AUTO = 7            # how many automatic / manual backups _prune() keeps (before-restore copies are never pruned)
KEEP_MANUAL = 10
AUTO_EVERY_S = 24 * 3600
MAX_UPLOAD = 1 << 30                                   # 1 GiB
# Only files with these exact names are listed, downloaded or deleted; this also stops path tricks in a requested name.
NAME_RE = re.compile(r"^(auto|manual|before-restore)-\d{8}-\d{6}\.db$")
MAGIC = b"SQLite format 3\x00"
# A file must contain at least these tables to count as a backup of this bridge.
REQUIRED_TABLES = {"requests", "settings"}


class BackupError(ValueError):
    """Something the operator should see."""


def _stamp():
    """Local-time timestamp used in backup file names; sorts the same alphabetically and chronologically."""
    return time.strftime("%Y%m%d-%H%M%S")


def _validate(path):
    """Raise BackupError unless `path` is a healthy bridge database; return {tables, requests, size}."""
    with open(path, "rb") as f:
        if f.read(16) != MAGIC:
            raise BackupError("That file is not a database made by this bridge (it isn't an SQLite file).")
    try:
        # read-only URI so checking a file can never modify it
        con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("That database file is damaged (it failed SQLite's integrity check).")
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not REQUIRED_TABLES <= tables:
                raise BackupError("That database file isn't from this bridge (its tables are missing).")
            n = con.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error as e:
        raise BackupError(f"That file can't be read as a database: {e}")
    return {"tables": len(tables), "requests": n, "size": os.path.getsize(path)}


def apply_staged_restore(db_path, log=print):
    """At start-up, before the database is opened: swap in a restore that was set aside earlier. Returns True if it did."""
    db, staged = Path(db_path), Path(str(db_path) + ".restore")
    if not staged.is_file():
        return False
    try:
        _validate(staged)
    except BackupError as e:
        log(f"[backup] the restore file was not used: {e}")
        staged.rename(staged.with_suffix(".restore.rejected"))
        return False
    folder = db.resolve().parent / "backups"
    folder.mkdir(exist_ok=True)
    if db.is_file():
        shutil.move(str(db), str(folder / f"before-restore-{_stamp()}.db"))
    # leftover write-ahead-log files belong to the old database and would corrupt the restored one if reused
    for ext in ("-wal", "-shm"):
        try:
            os.remove(str(db) + ext)
        except OSError:
            pass
    shutil.move(str(staged), str(db))
    log(f"[backup] restored the database from the file set aside earlier (the old one is in {folder})")
    return True


class Backups:
    """Creates, lists, prunes and stages restores of database backups in a `backups` folder next to the database."""

    def __init__(self, bridge):
        self.bridge = bridge
        self.audit = bridge.audit
        self.db_path = Path(bridge.args.db).resolve()
        self.dir = self.db_path.parent / "backups"
        self._lock = threading.Lock()

    # ---- making and listing ---------------------------------------------------------------------------------------
    def create(self, kind="manual"):
        """Write a new backup file ('auto' or 'manual'), prune old ones, and return its file name."""
        if kind not in ("auto", "manual"):
            raise ValueError(kind)
        with self._lock:
            self.dir.mkdir(exist_ok=True)
            name = f"{kind}-{_stamp()}.db"
            dest = self.dir / name
            tmp = dest.with_suffix(".tmp")      # written under a temp name, then renamed, so a half-written file is never listed
            dst = sqlite3.connect(str(tmp))
            try:
                with self.audit.lock:       # hold the audit lock so no thread writes while SQLite copies pages
                    self.audit.db.backup(dst)
            finally:
                dst.close()
            os.replace(tmp, dest)
            self._prune()
        return name

    def _prune(self):
        """Delete the oldest auto and manual backups beyond their limits (names sort by time, newest first)."""
        for prefix, keep in (("auto-", KEEP_AUTO), ("manual-", KEEP_MANUAL)):
            for f in sorted(self.dir.glob(prefix + "*.db"), reverse=True)[keep:]:
                try:
                    f.unlink()
                except OSError:
                    pass

    def list(self):
        """Backups on disk as dicts (name, kind, size, ts), newest first; ignores files that do not match NAME_RE."""
        if not self.dir.is_dir():
            return []
        out = []
        for f in self.dir.glob("*.db"):
            if NAME_RE.match(f.name):
                st = f.stat()
                out.append({"name": f.name, "kind": f.name.rsplit("-", 2)[0], "size": st.st_size, "ts": st.st_mtime})
        return sorted(out, key=lambda x: -x["ts"])

    def path_of(self, name):
        """Full path of a named backup; raises BackupError for an invalid or missing name."""
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise BackupError("Unknown backup.")
        p = self.dir / name
        if not p.is_file():
            raise BackupError("That backup no longer exists.")
        return p

    def delete(self, name):
        """Remove one backup file (name is validated by path_of)."""
        self.path_of(name).unlink()

    def snapshot_bytes(self):
        """A fresh copy of the live database as bytes, for a download (nothing is kept on disk)."""
        with tempfile.TemporaryDirectory(prefix="meshbackup_") as d:
            dst = sqlite3.connect(os.path.join(d, "snapshot.db"))
            try:
                with self.audit.lock:
                    self.audit.db.backup(dst)
            finally:
                dst.close()
            with open(os.path.join(d, "snapshot.db"), "rb") as f:
                return f.read()

    # ---- the daily automatic copy ---------------------------------------------------------------------------------
    def auto_enabled(self):
        """True unless the operator switched the daily backup off; it is on by default."""
        return self.audit.get_setting("auto_backup", "on") != "off"

    def set_auto(self, enabled):
        """Turn the daily automatic backup on or off (must be a real bool)."""
        if not isinstance(enabled, bool):
            raise BackupError("enabled must be true or false.")
        self.audit.set_setting("auto_backup", "on" if enabled else "off")

    def maybe_auto(self):
        """Called about hourly: make the daily copy if it is due. Returns the new file's name or None."""
        if not self.auto_enabled():
            return None
        newest = max((b["ts"] for b in self.list() if b["kind"] == "auto"), default=0)
        if time.time() - newest >= AUTO_EVERY_S:
            name = self.create("auto")
            print(f"[backup] daily backup written: {name}")
            return name
        return None

    # ---- restoring ----------------------------------------------------------------------------------------------------
    @property
    def staged_path(self):
        """Where a pending restore waits: the database path plus '.restore'. apply_staged_restore() looks here at start-up."""
        return Path(str(self.db_path) + ".restore")

    def staged(self):
        """Info about the pending restore file ({tables, requests, size, ts}), {'error': ...} if it is invalid, or None if there is none."""
        p = self.staged_path
        if not p.is_file():
            return None
        try:
            info = _validate(p)
        except BackupError as e:
            return {"error": str(e)}
        return {**info, "ts": p.stat().st_mtime}

    def stage_file(self, source):
        """Check `source` (a path) and set it aside to be swapped in at the next start. Returns what it contains."""
        info = _validate(source)
        shutil.copyfile(source, self.staged_path)
        return info

    def stage_upload(self, rfile, length):
        """Receive an uploaded database (from the request body), check it, and set it aside. Returns what it contains."""
        if length <= 0:
            raise BackupError("No file was sent.")
        if length > MAX_UPLOAD:
            raise BackupError("That file is too large to be a backup of this bridge.")
        # receive into a side file and only rename it into place once it validates
        tmp = self.staged_path.with_suffix(".upload")
        try:
            with open(tmp, "wb") as f:
                left = length
                while left > 0:
                    chunk = rfile.read(min(1 << 20, left))
                    if not chunk:
                        raise BackupError("The upload stopped early. Try again.")
                    f.write(chunk)
                    left -= len(chunk)
            info = _validate(tmp)
            os.replace(tmp, self.staged_path)
            return info
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def cancel_staged(self):
        """Discard a pending restore. Returns True if a file was removed."""
        try:
            self.staged_path.unlink()
            return True
        except OSError:
            return False
