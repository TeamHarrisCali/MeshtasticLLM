"""Releases and the packaged program: the version, the changelog, the tag check, where a packaged program keeps its data, the build recipe and
the workflows. Offline: nothing is built, nothing is downloaded and no workflow runs (the real proof is the `package` and `release` jobs in CI).

* one version string (meshllm/__init__.py, imports nothing), valid semver, shown by `--version`, in the dashboard's status and by setup --check;
* docs/CHANGELOG.md has a section for it, and the release workflow's tag check (scripts/release_check.py, the same code) agrees;
* meshllm/paths.py: unchanged behaviour from source, a per-user folder when `sys.frozen` (faked here), --data-dir and MESHLLM_DATA_DIR on top;
* the workflows: least privilege, no pull_request_target, no expression inside a `run:`, pinned actions, and the five required check names untouched.
"""
import ast, io, os, re, subprocess, sys, tarfile, tempfile, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)

def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()

import meshllm
import release_check as RC
import build_binary as BB
import setup_env
from meshllm import paths

ENV = {k: v for k, v in os.environ.items() if k != paths.ENV_VAR}
VERSION = meshllm.__version__

# ---- the version: one source of truth
tree = ast.parse(read("meshllm", "__init__.py"))
body = [n for n in tree.body if not (isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant))]
check("meshllm/__init__.py is import-free: a docstring and the version string only",
      len(body) == 1 and isinstance(body[0], ast.Assign) and body[0].targets[0].id == "__version__" and isinstance(body[0].value, ast.Constant), ast.dump(tree)[:200])
check("the version is a valid semantic version", RC.is_semver(VERSION), VERSION)
for bad in ("1", "1.2", "v1.2.3", "01.2.3", "1.2.3.4", "1.2.x", "", None, "1.2.3-"):
    check(f"{bad!r} is not valid semver", not RC.is_semver(bad))
for good in ("0.1.0", "1.0.0", "10.20.30", "1.0.0-rc.1", "1.0.0-alpha+001", "1.0.0+20130313144700"):
    check(f"{good!r} is valid semver", RC.is_semver(good))
check("the release script, the installer and the build script all read the same version",
      RC.read_version() == VERSION == setup_env.read_version(ROOT) == BB.read_version())
p = subprocess.run([sys.executable, "-m", "meshllm", "--version"], cwd=ROOT, capture_output=True, text=True, timeout=120)
check("python -m meshllm --version prints it", p.returncode == 0 and (p.stdout + p.stderr).strip() == f"meshllm {VERSION}", (p.returncode, p.stdout, p.stderr))
src = read("meshllm", "bridge.py")
check("the status JSON carries the version", '"version": __version__' in src)
check("the dashboard shows it (sidebar element and the script that fills it)", 'id="appVer"' in read("meshllm", "static", "index.html") and "appVer" in read("meshllm", "static", "js", "core.js"))
check("Diagnostics has a version line", 'add("version"' in read("meshllm", "diagnostics.py"))
out = io.StringIO()
code = setup_env.main(["--check", "--dir", ROOT], out=out)
check("setup --check prints the version", f"Meshtastic LLM Bridge version {VERSION}" in out.getvalue(), out.getvalue()[:300])
check("setup's version reader gives None, not an error, for a folder without one", setup_env.read_version(tempfile.mkdtemp(prefix="novers_")) is None)

# ---- the changelog and the tag check
log = read("docs", "CHANGELOG.md")
check("the changelog has an Unreleased section", re.search(r"^## \[Unreleased\]\s*$", log, re.M) is not None)
notes = RC.changelog_section(log, VERSION)
check("the changelog has a non-empty section for the current version", notes is not None and len(notes) > 500, notes and len(notes))
check("that section is dated (Keep a Changelog: ## [x.y.z] - YYYY-MM-DD)", re.search(r"^## \[" + re.escape(VERSION) + r"\] - \d{4}-\d{2}-\d{2}\s*$", log, re.M) is not None)
check("the notes stop at the next section and do not include the link references", "## [Unreleased]" not in notes and "compare/v" not in notes and "releases/tag" not in notes)
check("the changelog is honest about what was not tried on real hardware", "Not tried on real hardware" in notes and "Wi-Fi" in notes)
sample = "# Changelog\n\n## [Unreleased]\n\n## [1.2.3] - 2030-01-01\n\n### Added\n\n- a thing\n\n## [1.2.2] - 2029-01-01\n\n- old\n\n[Unreleased]: https://example.invalid/x\n[1.2.3]: https://example.invalid/y\n"
check("a section is cut out between its heading and the next one", RC.changelog_section(sample, "1.2.3") == "### Added\n\n- a thing")
check("the last section stops before the link references", RC.changelog_section(sample, "1.2.2") == "- old")
check("an empty section (Unreleased) is reported as missing", RC.changelog_section(sample, "Unreleased") is None)
check("an unknown version has no section, and a prefix of a version is not a match", RC.changelog_section(sample, "9.9.9") is None and RC.changelog_section(sample, "1.2") is None)
check("the tag must be exactly v + version", RC.tag_matches("v" + VERSION, VERSION) and not any(RC.tag_matches(t, VERSION) for t in (VERSION, "V" + VERSION, "refs/tags/v" + VERSION, "v" + VERSION + ".1", "v0.0.0", "")))
check("the current tag passes every check", RC.check_tag("v" + VERSION) == [], RC.check_tag("v" + VERSION))
check("a mismatched tag is refused with the expected one named", any("v" + VERSION in m for m in RC.check_tag("v9.9.9")), RC.check_tag("v9.9.9"))
tmp = tempfile.mkdtemp(prefix="reltest_")
os.makedirs(os.path.join(tmp, "meshllm")); os.makedirs(os.path.join(tmp, "docs"))
open(os.path.join(tmp, "meshllm", "__init__.py"), "w").write('"""x"""\n__version__ = "2.0.0"\n')
open(os.path.join(tmp, "docs", "CHANGELOG.md"), "w").write("## [Unreleased]\n\n## [1.9.0] - 2030-01-01\n\n- x\n")
check("a version with no changelog section is refused", any("no non-empty '## [2.0.0]'" in m for m in RC.check_tag("v2.0.0", tmp)), RC.check_tag("v2.0.0", tmp))
open(os.path.join(tmp, "meshllm", "__init__.py"), "w").write('__version__ = "2.0"\n')
check("a version that is not semver is refused", any("not a valid semantic version" in m for m in RC.check_tag("v2.0", tmp)), RC.check_tag("v2.0", tmp))
for argv, want in ((["check", "v" + VERSION], 0), (["check", "v9.9.9"], 1), (["notes", VERSION], 0), (["notes", "9.9.9"], 1), (["version"], 0), ([], 2)):
    p = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "release_check.py")] + argv, capture_output=True, text=True, timeout=60)
    check(f"release_check.py {' '.join(argv) or '(nothing)'} exits {want}", p.returncode == want, (p.returncode, p.stdout[:100], p.stderr[:200]))
p = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "release_check.py"), "notes", VERSION], capture_output=True, text=True, timeout=60)
check("`notes` prints exactly the section the library finds", p.stdout.strip() == notes, p.stdout[:100])

# ---- where the data lives (meshllm/paths.py)
class A:       # a stand-in for the parsed command line
    def __init__(self, **kw): self.__dict__.update(kw)

class Faked:
    """Pretend to be a packaged program (sys.frozen, sys._MEIPASS) for the duration of a `with`, then put everything back."""
    def __init__(self, meipass, home): self.meipass, self.home = meipass, home
    HOMES = ("HOME", "USERPROFILE", "XDG_DATA_HOME", "APPDATA")      # pointed at a throwaway folder, so nothing is ever made in the real home folder
    def __enter__(self):
        self.had = (hasattr(sys, "frozen"), hasattr(sys, "_MEIPASS"))
        self.env = {k: os.environ.get(k) for k in self.HOMES}
        for k in self.HOMES:
            os.environ[k] = os.path.join(self.home, k)
        sys.frozen = True; sys._MEIPASS = self.meipass
    def __exit__(self, *a):
        if not self.had[0]: del sys.frozen
        if not self.had[1]: del sys._MEIPASS
        for k, v in self.env.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v

saved_env = os.environ.pop(paths.ENV_VAR, None)
paths.set_data_dir(None)
root = paths.project_root()
check("from source the data folder is still the project folder", paths.data_dir(ENV) == root and paths.default_db() == str(root / "audit.db") and not paths.is_frozen(), paths.data_dir(ENV))
check("from source the log folder is still logs/ in the project", paths.log_dir() == root / "logs")
check("--db defaults to that file, as before", __import__("meshllm.bridge", fromlist=["x"]).build_parser().get_default("db") == str(root / "audit.db"))
check("from source the resources are in the project folder", paths.resource_root() == root and (paths.resource_root() / "meshllm" / "static" / "index.html").is_file())
home = "/home/someone"
win = paths.user_data_dir("win32", {"APPDATA": "C:\\Users\\x\\AppData\\Roaming"}, "C:\\Users\\x")
check("Windows: %APPDATA%\\meshllm", win.name == "meshllm" and str(win).startswith("C:\\Users\\x\\AppData\\Roaming"), win)
nowin = str(paths.user_data_dir("win32", {}, "C:\\Users\\x"))
check("Windows without APPDATA: AppData and Roaming under the home folder", nowin.startswith("C:\\Users\\x") and "AppData" in nowin and "Roaming" in nowin and nowin.endswith("meshllm"), nowin)
check("macOS: ~/Library/Application Support/meshllm", paths.user_data_dir("darwin", {}, home).as_posix() == home + "/Library/Application Support/meshllm")
check("Linux: ~/.local/share/meshllm", paths.user_data_dir("linux", {}, home).as_posix() == home + "/.local/share/meshllm")
check("Linux: $XDG_DATA_HOME wins", paths.user_data_dir("linux", {"XDG_DATA_HOME": "/xdg"}, home).as_posix() == "/xdg/meshllm")
check("Linux (even an old 'linux2'): same rule", paths.user_data_dir("linux2", {}, home).as_posix() == home + "/.local/share/meshllm")

bundle = tempfile.mkdtemp(prefix="meipass_")
fakehome = tempfile.mkdtemp(prefix="fakehome_")
with Faked(bundle, fakehome):
    check("packaged: is_frozen", paths.is_frozen())
    check("packaged: the data folder is the per-user one, not the bundle and not the project",
          paths.data_dir(ENV) == paths.user_data_dir(environ=ENV) and not str(paths.data_dir(ENV)).startswith(bundle) and paths.data_dir(ENV) != root, paths.data_dir(ENV))
    check("packaged: resources come from the bundle", str(paths.resource_root()) == bundle, paths.resource_root())
    check("packaged: MESHLLM_DATA_DIR beats the per-user folder", paths.data_dir({paths.ENV_VAR: bundle + "/mine"}).as_posix().endswith("/mine"))
    check("packaged: the log folder is under the data folder", paths.log_dir() == paths.data_dir() / "logs")
    ns = A(db=paths.default_db(), data_dir=None)
    paths.apply_cli(ns, ns.db)
    check("packaged: a start with no flags makes the per-user data folder, in this throwaway home", os.path.isdir(paths.data_dir()) and str(paths.data_dir()).startswith(fakehome), paths.data_dir())
    if os.name != "nt":
        check("packaged: and it is private to the user", os.stat(paths.data_dir()).st_mode & 0o077 == 0, oct(os.stat(paths.data_dir()).st_mode))
    mine = os.path.join(bundle, "chosen")
    ns = A(db=paths.default_db(), data_dir=mine)
    got = paths.apply_cli(ns, ns.db)
    check("packaged: --data-dir moves the database, creates the folder and wins over everything", ns.db == os.path.join(os.path.realpath(mine), "audit.db") and os.path.isdir(mine) and str(got) == os.path.realpath(mine), (ns.db, got))
    check("--data-dir also moves the log folder", paths.log_dir() == paths.data_dir() / "logs" and str(paths.log_dir()).startswith(os.path.realpath(mine)), paths.log_dir())
    paths.set_data_dir(None)
check("after the fake, everything is as it was", not paths.is_frozen() and paths.data_dir(ENV) == root)

tmpdata = tempfile.mkdtemp(prefix="datadir_")
default = str(root / "audit.db")
ns = A(db=default, data_dir=os.path.join(tmpdata, "a", "b"))
paths.apply_cli(ns, default)
check("--data-dir PATH: the database goes there and the folder is made", ns.db == os.path.join(os.path.realpath(os.path.join(tmpdata, "a", "b")), "audit.db") and os.path.isdir(os.path.join(tmpdata, "a", "b")), ns.db)
paths.set_data_dir(None)
ns = A(db="/explicit/other.db", data_dir=os.path.join(tmpdata, "c"))
paths.apply_cli(ns, default)
check("an explicit --db is left alone", ns.db == "/explicit/other.db" and not os.path.exists(os.path.join(tmpdata, "c", "audit.db")), ns.db)
paths.set_data_dir(None)
ns = A(db=default, data_dir=os.path.join(tmpdata, "d"), demo=True)
paths.apply_cli(ns, default)
check("--demo keeps its own temporary folder: the database setting and the disk are left alone", ns.db == default and not os.path.exists(os.path.join(tmpdata, "d")), ns.db)
paths.set_data_dir(None)
ns = A(db=default)
paths.apply_cli(ns, default)
check("no flag and no variable: nothing changes from source", ns.db == default and paths.data_dir(ENV) == root)
os.environ[paths.ENV_VAR] = os.path.join(tmpdata, "fromenv")
check("MESHLLM_DATA_DIR sets the data folder in every mode", str(paths.data_dir()) == (os.path.realpath(os.path.join(tmpdata, "fromenv"))) and paths.default_db().endswith("audit.db") and "fromenv" in paths.default_db())
paths.set_data_dir(os.path.join(tmpdata, "fromflag"))
check("--data-dir beats MESHLLM_DATA_DIR", "fromflag" in str(paths.data_dir()), paths.data_dir())
paths.set_data_dir(None)
del os.environ[paths.ENV_VAR]
os.environ[paths.ENV_VAR] = "   "
check("a blank MESHLLM_DATA_DIR is ignored", paths.data_dir() == root)
del os.environ[paths.ENV_VAR]

from meshllm import bridge as bm
_, args = bm.parse_cli(["--data-dir", os.path.join(tmpdata, "cli"), "--no-web"])
check("the real command line: --data-dir sets --db and the folder exists", args.db == os.path.join(os.path.realpath(os.path.join(tmpdata, "cli")), "audit.db") and os.path.isdir(os.path.join(tmpdata, "cli")), args.db)
from meshllm.diagnostics import Diagnostics
check("the Diagnostics log folder follows --data-dir", Diagnostics(object()).log_dir == paths.data_dir() / "logs" and "cli" in str(Diagnostics(object()).log_dir), Diagnostics(object()).log_dir)
paths.set_data_dir(None)
if saved_env is not None:
    os.environ[paths.ENV_VAR] = saved_env

# read-only package data must come from the bundle in a packaged program (a fresh interpreter, so no module keeps its old folder)
tree = tempfile.mkdtemp(prefix="bundle_")
os.makedirs(os.path.join(tree, "meshllm", "static")); os.makedirs(os.path.join(tree, "docs", "eval_results"))
code = ("import sys; sys.frozen = True; sys._MEIPASS = sys.argv[1]\n"
        "from meshllm import evals, webui, paths, diagnostics\n"
        "print(evals.ROOT, evals.RESULTS, evals.DOCS, webui.STATIC, paths.data_dir(), diagnostics.ROOT)")
env = dict(ENV, HOME=tmpdata, XDG_DATA_HOME=os.path.join(tmpdata, "xdg"), APPDATA=os.path.join(tmpdata, "appdata"), PYTHONDONTWRITEBYTECODE="1")
p = subprocess.run([sys.executable, "-c", code, tree], cwd=ROOT, capture_output=True, text=True, env=env, timeout=120)
got = p.stdout.split()
check("packaged: evals and the dashboard read docs and static from the bundle, the data goes to the user folder",
      p.returncode == 0 and len(got) == 6 and got[0] == tree and got[1] == os.path.join(tree, "docs", "eval_results") and got[2] == os.path.join(tree, "docs")
      and got[3] == os.path.join(tree, "meshllm", "static") and got[4].startswith(tmpdata) and "meshllm" in got[4], (p.stdout, p.stderr[-300:]))
check("packaged: importing wrote nothing into the data folder (it is created only when the bridge starts)", not os.path.exists(os.path.join(tmpdata, "xdg")) and not os.path.exists(os.path.join(tmpdata, "appdata")))

# ---- the build recipe
check("the build name is meshllm-<version>-<os>-<arch>", BB.build_name("0.1.0", "linux", "x86_64") == "meshllm-0.1.0-linux-x86_64" and BB.build_name("1.2.3", "win32", "AMD64") == "meshllm-1.2.3-windows-x86_64"
      and BB.build_name("1.2.3", "darwin", "arm64") == "meshllm-1.2.3-macos-arm64" and BB.build_name("1.2.3", "linux", "aarch64") == "meshllm-1.2.3-linux-arm64")
check("archives: .zip on Windows, .tar.gz elsewhere", BB.archive_extension("win32") == ".zip" and BB.archive_extension("linux") == ".tar.gz" and BB.archive_extension("darwin") == ".tar.gz")
check("the current machine gets a name", BB.build_name().startswith(f"meshllm-{VERSION}-"), BB.build_name())
folder = os.path.join(tmp, "meshllm-9.9.9-linux-x86_64")
os.makedirs(os.path.join(folder, "_internal")); open(os.path.join(folder, "meshllm"), "w").write("x"); open(os.path.join(folder, "_internal", "lib.so"), "w").write("y")
os.chmod(os.path.join(folder, "meshllm"), 0o755)
tgz = BB.make_archive(folder, folder + ".tar.gz")
with tarfile.open(tgz) as t:
    names = t.getnames()
    mode = t.getmember("meshllm-9.9.9-linux-x86_64/meshllm").mode
check("the tar.gz has the folder as its one top-level entry, and keeps the executable bit", all(n.split("/")[0] == "meshllm-9.9.9-linux-x86_64" for n in names) and "meshllm-9.9.9-linux-x86_64/_internal/lib.so" in names and (os.name == "nt" or mode & 0o111), (names, mode))
zp = BB.make_archive(folder, folder + ".zip")
with zipfile.ZipFile(zp) as z:
    zn = z.namelist()
check("the zip has the same layout", sorted(n.replace("\\", "/") for n in zn) == ["meshllm-9.9.9-linux-x86_64/_internal/lib.so", "meshllm-9.9.9-linux-x86_64/meshllm"], zn)
check("folder_size adds the files up", BB.folder_size(folder) == 2)
spec = read("scripts", "meshllm.spec")
check("the recipe bundles the dashboard files and docs, and leaves out screenshots and the hand-off queue", "meshllm/static" in spec and '"docs"' in spec and "screenshots" in spec and "TODO.md" in spec)
check("the recipe names the Bluetooth backend of every operating system, and pyserial", all(w in spec for w in ("bleak.backends.bluezdbus", "dbus_fast", "bleak.backends.winrt", "winrt", "bleak.backends.corebluetooth", "objc", "serial.tools.list_ports")))
check("the recipe is one-folder (exclude_binaries) and unpacked by nothing at start-up", "exclude_binaries=True" in spec and "COLLECT(" in spec and "upx=False" in spec)
check("the entry point calls the bridge's main", "from meshllm.bridge import main" in read("scripts", "launcher.py"))
req = read("scripts", "requirements-build.txt")
check("the build tool is pinned to a range, in its own requirements file (not requirements.txt)", re.search(r"^pyinstaller>=\d+(\.\d+)*,<\d+", req, re.M) is not None and "pyinstaller" not in read("requirements.txt").lower())
check("the build output is ignored by git", "dist/" in read(".gitignore").splitlines() and "build/" in read(".gitignore").splitlines())

# ---- the workflows
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")
names = sorted(f for f in os.listdir(WORKFLOWS) if f.endswith(".yml"))
check("the three workflows exist", {"tests.yml", "package.yml", "release.yml"} <= set(names), names)
try:
    import yaml
except ImportError:
    yaml = None
    print("note: PyYAML is not installed; the workflow checks below read the text instead of the parsed file")

def load(name):
    doc = yaml.safe_load(read(".github", "workflows", name))
    if True in doc and "on" not in doc:       # YAML 1.1 reads the key `on` as the boolean True
        doc["on"] = doc.pop(True)
    return doc

REQUIRED = ["test (3.9)", "test (3.10)", "test (3.12)", "test (3.13)", "docker"]
def job_names(doc):
    """The check names a workflow's jobs produce (a matrix job's name is `<job> (<each matrix value>)` unless it sets `name:`)."""
    out = []
    for key, job in doc["jobs"].items():
        if "uses" in job:
            out.append(key)
        elif "strategy" in job and "matrix" in job["strategy"] and "name" not in job:
            vals = [str(v) for v in next(iter(job["strategy"]["matrix"].values()))]
            out += [f"{key} ({v})" for v in vals]
        else:
            out.append(job.get("name", key))
    return out

if yaml:
    docs = {n: load(n) for n in names}
    check("tests.yml still produces exactly the five required check names", sorted(job_names(docs["tests.yml"])) == sorted(REQUIRED), job_names(docs["tests.yml"]))
    pkg_names = ["package (" + o["label"] + ")" for o in docs["package.yml"]["jobs"]["package"]["strategy"]["matrix"]["include"]]
    check("the packaging jobs are named distinctly from the required checks", pkg_names == ["package (linux)", "package (windows)", "package (macos)"] and not set(pkg_names) & set(REQUIRED), pkg_names)
    check("the packaging job's name field produces those names", docs["package.yml"]["jobs"]["package"]["name"] == "package (${{ matrix.label }})", docs["package.yml"]["jobs"]["package"]["name"])
    pk_on = docs["package.yml"]["on"]
    check("package.yml runs on pull requests (with paths, so it can be skipped), as a reusable workflow, and by hand", set(pk_on) == {"pull_request", "workflow_call", "workflow_dispatch"} and "meshllm/**" in pk_on["pull_request"]["paths"]
          and "scripts/meshllm.spec" in pk_on["pull_request"]["paths"] and ".github/workflows/package.yml" in pk_on["pull_request"]["paths"], pk_on)
    check("package.yml builds on all three operating systems", [o["os"] for o in docs["package.yml"]["jobs"]["package"]["strategy"]["matrix"]["include"]] == ["ubuntu-latest", "windows-latest", "macos-latest"])
    rel = docs["release.yml"]
    check("release.yml runs only when a version tag is pushed", rel["on"] == {"push": {"tags": ["v*.*.*"]}}, rel["on"])
    check("release.yml jobs: verify, test, build, checksums, publish, in that chain", list(rel["jobs"]) == ["verify", "test", "build", "checksums", "publish"]
          and rel["jobs"]["test"]["needs"] == "verify" and set(rel["jobs"]["build"]["needs"]) == {"verify", "test"} and rel["jobs"]["build"]["uses"] == "./.github/workflows/package.yml"
          and rel["jobs"]["checksums"]["needs"] == "build" and rel["jobs"]["publish"]["needs"] == "checksums", list(rel["jobs"]))
    for n, d in docs.items():
        check(f"{n}: the workflow-level token is read-only", d.get("permissions") == {"contents": "read"}, d.get("permissions"))
        check(f"{n}: no pull_request_target", "pull_request_target" not in str(d["on"]) and "pull_request_target" not in read(".github", "workflows", n).replace("pull_request_target`", ""), d["on"])
    writers = {n: [j for j, job in d["jobs"].items() if any(v != "read" for v in (job.get("permissions") or {}).values())] for n, d in docs.items()}
    check("only release.yml's checksums and publish jobs ask for more than read", writers == {"tests.yml": [], "package.yml": [], "release.yml": ["checksums", "publish"]}, writers)
    check("only the publish job can write contents; only checksums gets id-token and attestations",
          rel["jobs"]["publish"]["permissions"] == {"contents": "write"} and rel["jobs"]["checksums"]["permissions"] == {"contents": "read", "id-token": "write", "attestations": "write"}, (rel["jobs"]["publish"].get("permissions"), rel["jobs"]["checksums"].get("permissions")))

    def steps(d):
        for j, job in d["jobs"].items():
            for s in job.get("steps", []):
                yield j, s
    for n, d in docs.items():
        bad = [(j, s.get("name", s["run"][:40])) for j, s in steps(d) if "run" in s and "${{" in s["run"]]
        check(f"{n}: no ${{{{ }}}} expression inside a run: script (values go through env:)", not bad, bad)
        unpinned = [s["uses"] for j, s in steps(d) if "uses" in s and not re.match(r"^(actions/[a-z-]+@v\d+|\./\.github/workflows/[a-z.]+)$", s["uses"])]
        check(f"{n}: every action is a first-party one at a major-version tag", not unpinned, unpinned)
        for j, job in d["jobs"].items():
            if "uses" in job:
                check(f"{n}: reusable workflow {j} is a local file", job["uses"].startswith("./.github/workflows/") and os.path.isfile(os.path.join(ROOT, job["uses"][2:])), job["uses"])
    texts = {j: str(s) for j, s in steps(rel)}
    verify = [s for j, s in steps(rel) if j == "verify"]
    check("release.yml verify: runs the same tag check as the tests, with the tag passed through env", any(s.get("env", {}).get("TAG") == "${{ github.ref_name }}" and 'release_check.py check "$TAG"' in s.get("run", "") for s in verify), verify)
    check("release.yml verify: the tagged commit must be an ancestor of origin/main", any("merge-base --is-ancestor" in s.get("run", "") and "refs/remotes/origin/main" in s.get("run", "") for s in verify))
    check("release.yml verify: fetches the full history", any(s.get("with", {}).get("fetch-depth") == 0 for s in verify))
    check("release.yml runs the test suite before building", any("scripts/run_tests.py" in s.get("run", "") for j, s in steps(rel) if j == "test"))
    pub = [s for j, s in steps(rel) if j == "publish"]
    check("release.yml publish: gh release create with the changelog notes, the archives and SHA256SUMS", any("gh release create" in s.get("run", "") and "--notes-file notes.md" in s["run"] and "SHA256SUMS" in s["run"] and "release_check.py notes" in s["run"] and "--verify-tag" in s["run"] for s in pub), pub)
    check("release.yml publish: the token reaches gh through env", any(s.get("env", {}).get("GH_TOKEN") == "${{ github.token }}" for s in pub))
    check("release.yml checksums: attests the build provenance of the archives", any(s.get("uses", "").startswith("actions/attest-build-provenance@") and "meshllm-*.tar.gz" in s["with"]["subject-path"] and "meshllm-*.zip" in s["with"]["subject-path"] for j, s in steps(rel) if j == "checksums"))
    check("release.yml checksums: SHA256SUMS lists all three archives", any("sha256sum" in s.get("run", "") and "SHA256SUMS" in s["run"] and '= 3' in s["run"] for j, s in steps(rel) if j == "checksums"))
    pk = [s for j, s in steps(docs["package.yml"])]
    check("package.yml: every job builds, smoke-tests and uploads", any("build_binary.py" in s.get("run", "") for s in pk) and any("smoke_binary.py" in s.get("run", "") for s in pk) and any(s.get("uses", "").startswith("actions/upload-artifact@") for s in pk))
    check("package.yml: uses bash on every operating system, so the same commands run", docs["package.yml"]["jobs"]["package"]["defaults"]["run"]["shell"] == "bash")
    check("package.yml: pip is installed from the two requirements files", any("-r requirements.txt -r scripts/requirements-build.txt" in s.get("run", "") for s in pk))
else:
    for n in ("release.yml", "package.yml", "tests.yml"):
        t = read(".github", "workflows", n)
        check(f"{n}: read-only token at the top and no pull_request_target", re.search(r"^permissions:\n  contents: read\n", t, re.M) is not None and "pull_request_target" not in t.replace("no pull_request_target", ""))
    t = read(".github", "workflows", "release.yml")
    check("release.yml: tag trigger, tag check and ancestor check are there", 'tags:\n      - "v*.*.*"' in t and 'release_check.py check "$TAG"' in t and "merge-base --is-ancestor" in t and "gh release create" in t)

print(f"\n{len(fails)} failed" if fails else "\nall passed")
sys.exit(1 if fails else 0)
