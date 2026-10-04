"""Where the program keeps its own data, and where it finds the files that ship with it. Standard library only, no side effects on import.

Two different kinds of folder, kept apart on purpose:

* the DATA folder (audit.db, backups/, tile_cache/, logs/): written while running, must survive an upgrade. Run from source (or in
  Docker, which passes `--db /data/audit.db`) it is the project folder, exactly as before. A packaged program (PyInstaller sets
  `sys.frozen`) runs from an install folder or a temporary extraction folder that is read-only or thrown away, so there it is a
  per-user folder instead: `%APPDATA%\\meshllm` on Windows, `~/Library/Application Support/meshllm` on macOS, `$XDG_DATA_HOME/meshllm`
  (else `~/.local/share/meshllm`) on Linux. `--data-dir PATH` or the `MESHLLM_DATA_DIR` variable overrides all of that in every mode.
* the RESOURCE folder (the dashboard's static files, docs/, docs/eval_results/): read-only, shipped with the program. Next to the package
  when run from source, inside the bundle (`sys._MEIPASS`) when packaged.

Nothing in this module creates a folder; the code that writes a file creates what it needs.
"""
import os
import sys
from pathlib import Path

APP_NAME = "meshllm"
ENV_VAR = "MESHLLM_DATA_DIR"
_override = None            # set by set_data_dir() from --data-dir; beats the environment variable


def is_frozen():
    """True in a packaged (PyInstaller) program."""
    return bool(getattr(sys, "frozen", False))


def project_root():
    """The folder that holds the `meshllm` package (the project folder when run from source)."""
    return Path(__file__).resolve().parent.parent


def resource_root():
    """Where the read-only files that ship with the program (docs/, meshllm/static/) are: the bundle when packaged, else the project folder."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", None) or Path(sys.executable).resolve().parent)
    return project_root()


def user_data_dir(platform=None, environ=None, home=None):
    """The per-user data folder for this operating system (the arguments let tests choose the platform without being on it)."""
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    home = os.path.expanduser("~") if home is None else str(home)
    if platform.startswith("win"):
        return Path(environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")) / APP_NAME
    if platform == "darwin":
        return Path(home) / "Library" / "Application Support" / APP_NAME
    return Path(environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")) / APP_NAME


def set_data_dir(path):
    """Use `path` as the data folder for the rest of this process (the --data-dir flag); None or "" goes back to the default."""
    global _override
    _override = Path(os.path.expanduser(str(path))).resolve() if path else None


def data_dir(environ=None, frozen=None):
    """The folder for audit.db, backups/, tile_cache/ and logs/: --data-dir, else MESHLLM_DATA_DIR, else the per-user folder when
    packaged, else the project folder (the behaviour from source has not changed)."""
    environ = os.environ if environ is None else environ
    if _override is not None:
        return _override
    env = (environ.get(ENV_VAR) or "").strip()
    if env:
        return Path(os.path.expanduser(env)).resolve()
    if is_frozen() if frozen is None else frozen:
        return user_data_dir(environ=environ)
    return project_root()


def default_db():
    """The default audit database file, as a string (what `--db` defaults to)."""
    return str(data_dir() / "audit.db")


def log_dir():
    """The folder the Diagnostics page reads bridge.log and bridge.err.log from."""
    return data_dir() / "logs"


def apply_cli(args, default_db_value):
    """After the command line is parsed: remember --data-dir and, if --db was not given, put the database in the data folder and make that
    folder (private to the user on POSIX). `default_db_value` is what `--db` defaults to, so an explicit --db (even to the same file) is
    left alone. Demo mode keeps everything in its own temporary folder, so nothing is created for it. Returns the data folder."""
    explicit = getattr(args, "data_dir", None)
    if explicit:
        set_data_dir(explicit)
    if getattr(args, "db", None) == default_db_value and not getattr(args, "demo", False):
        if explicit:
            args.db = default_db()
        try:
            os.makedirs(data_dir(), mode=0o700, exist_ok=True)
        except OSError:
            pass            # the database open reports it, with the path
    return data_dir()
