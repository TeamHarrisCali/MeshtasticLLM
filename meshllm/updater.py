"""Looking for a newer release on GitHub, and (for a git checkout only) updating to it from the dashboard.

READ THIS FIRST, it is the whole security story of this file:

* The only thing ever fetched from the network by the CHECK is one small JSON document, `releases/latest` of this repository (REPO below, a
  constant: no request, flag or environment variable can change it), over HTTPS with the certificate checked, with a timeout and a size cap.
  It is parsed strictly: the tag must be `v<major>.<minor>.<patch>` and nothing else is trusted from it except text that is shown as plain text.
  Nothing but that GET is sent: no identifier, no version of this program beyond the User-Agent, no telemetry. The periodic check is OFF until
  the owner turns it on; the "Check now" button always works because the owner pressed it.
* Only a plain git checkout can update itself. A packaged program, a Docker container and anything else are only TOLD that a release exists
  (and how to update by hand): the packaged program cannot overwrite itself and a container would need the Docker socket.
* The git update is a fast-forward to a tag that is on `main` of this repository's own GitHub `origin`: every git call is an argument list (never
  a shell), every tag and commit id is checked against a strict pattern before it reaches one, uncommitted changes to tracked files are refused, and so is a release
  that would add a file over an untracked or ignored one (nothing is ever reset or forced over the owner's work), a database backup is made first, and a failed requirements install or a new version that does not even start
  is rolled back with `git reset --keep` (which refuses to touch uncommitted work).
* `pip install -r requirements.txt` from the pulled release runs code from that release: the trust is the same as installing it by hand.

The module has no side effects on import. Tests change API_BASE (and the origin check) to point at a local fake; there is deliberately no flag
or environment variable for that.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

import requests

from meshllm import __version__, paths

REPO = "TeamHarrisCali/MeshtasticLLM"                  # the one repository this program updates from
API_BASE = "https://api.github.com"                    # tests point this at a local fake server
RELEASE_URL_PREFIX = f"https://github.com/{REPO}/releases/"
USER_AGENT = f"meshllm/{__version__}"

CHECK_EVERY_S = 24 * 3600          # the periodic check asks at most once a day when GitHub answered
RETRY_UNREACHED_S = 3 * 3600       # ...and after an attempt that never reached GitHub (offline), at most every three hours
MIN_GAP_S = 5                      # "Check now" cannot be repeated faster than this
MAX_BLOCK_S = 3600                 # how long a "rate limited" answer can hold the check back
MAX_RESPONSE_BYTES = 256 * 1024    # a release document is a few KB; anything bigger is refused
CONNECT_TIMEOUT, READ_TIMEOUT, TOTAL_DEADLINE = 5, 10, 15
NOTES_MAX = 1500                   # characters of release notes kept and shown
GIT_TIMEOUT, PIP_TIMEOUT, SMOKE_TIMEOUT = 120, 900, 90
START_DELAY_S = 60                 # the periodic check waits this long after start-up, then looks once an hour whether one is due
RESTART_GRACE_S = 2.0              # the page gets this long to read "restarting" before the process is replaced

MAIN_REF = "refs/remotes/origin/main"    # where fetch_main() puts GitHub's main branch

SETTING_ENABLED = "update_check"   # "on" / "off" (default off)
SETTING_STATE = "update_state"     # JSON: the cached check
SETTING_APPLIED = "update_applied" # JSON: the last update (what it was, the commit to go back to, the backup)

NUM = r"(0|[1-9][0-9]{0,8})"
# A tag the update path accepts: v<major>.<minor>.<patch>. `[0-9]` and \Z on purpose: \d also matches non-ASCII digits and $ also matches before a trailing newline.
TAG_RE = re.compile(r"\Av" + NUM + r"\." + NUM + r"\." + NUM + r"\Z", re.ASCII)
SHA_RE = re.compile(r"\A([0-9a-f]{40}|[0-9a-f]{64})\Z", re.ASCII)
_PRE = r"[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*"
SEMVER_RE = re.compile(r"\A" + NUM + r"\." + NUM + r"\." + NUM + r"(?:-(" + _PRE + r"))?(?:\+" + _PRE + r")?\Z", re.ASCII)
# The only `origin` addresses a git update will use (compared in lower case; GitHub names are not case sensitive).
ORIGIN_OK = {f"https://github.com/{REPO}".lower(), f"https://github.com/{REPO}.git".lower(),
             f"git@github.com:{REPO}".lower(), f"git@github.com:{REPO}.git".lower(),
             f"ssh://git@github.com/{REPO}".lower(), f"ssh://git@github.com/{REPO}.git".lower()}


class UpdateError(ValueError):
    """Something the operator should see, in plain words (a ValueError, so the dashboard shows it as a 400)."""


class UpdateBusy(UpdateError):
    """Another check or update is already running."""


# ---- versions ---------------------------------------------------------------------------------------------------------------------------
def valid_tag(tag):
    """True for a string that is exactly `v<major>.<minor>.<patch>` (no leading zeros, ASCII digits, no whitespace)."""
    return isinstance(tag, str) and TAG_RE.match(tag) is not None


def require_tag(tag):
    """Return `tag` if it is a valid release tag, else raise UpdateError. Every tag goes through here before it reaches a command."""
    if not valid_tag(tag):
        raise UpdateError("That is not a release tag the updater accepts (it must look like v1.2.3).")
    return tag


def require_sha(sha):
    """Return `sha` if it is a full lower-case hexadecimal commit id (40 or 64 characters), else raise UpdateError."""
    if not isinstance(sha, str) or SHA_RE.match(sha) is None:
        raise UpdateError("That is not a full commit id.")
    return sha


def parse_version(text):
    """A sortable key for a semantic version (`1.2.3`, `1.2.3-rc.1`, a `+build` part is ignored), or None if it is not strict semver.
    A release sorts above its own pre-releases; pre-release identifiers compare as the semver rules say."""
    if not isinstance(text, str):
        return None
    m = SEMVER_RE.match(text)
    if m is None:
        return None
    major, minor, patch, pre = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
    if pre is None:
        return (major, minor, patch, 1, ())
    ids = []
    for part in pre.split("."):
        if part.isdigit():
            if len(part) > 1 and part[0] == "0":
                return None                        # semver forbids leading zeros in numeric identifiers
            ids.append((0, int(part), ""))
        else:
            ids.append((1, 0, part))
    return (major, minor, patch, 0, tuple(ids))


def is_newer(tag, current=None):
    """True if release `tag` (v1.2.3) is a strictly higher version than `current` (default: this program's). Anything unparsable is not newer."""
    if not valid_tag(tag):
        return False
    a, b = parse_version(tag[1:]), parse_version(__version__ if current is None else current)
    return a is not None and b is not None and a > b


# ---- the release document ---------------------------------------------------------------------------------------------------------------
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩﻿]")


def plain_text(value, limit=NOTES_MAX):
    """`value` as safe plain text: not a string -> empty; control and bidirectional-override characters removed; at most `limit` characters.
    It is only ever shown with textContent, never as HTML; this just keeps what is stored tidy."""
    if not isinstance(value, str):
        return ""
    text = _CONTROL.sub("", value.replace("\r\n", "\n").replace("\r", "\n")).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def release_url(tag):
    """This repository's release page for a (validated) tag: the only link the page is ever given."""
    return RELEASE_URL_PREFIX + "tag/" + require_tag(tag)


def clean_release(data):
    """{tag, url, notes} from a GitHub release document, or None if it is not a published, stable release with a valid tag. The document's own
    `html_url` is ignored: the link is always built from the validated tag, so it can only ever be this repository's page for that tag."""
    if not isinstance(data, dict) or data.get("draft") is not False or data.get("prerelease") is not False:
        return None
    tag = data.get("tag_name")
    if not valid_tag(tag):
        return None
    return {"tag": tag, "url": release_url(tag), "notes": plain_text(data.get("body"))}


# ---- the one network call ------------------------------------------------------------------------------------------------------------------
class _TooLarge(Exception):
    pass


def _no_auth(request):
    """A do-nothing `auth` for requests. Giving one stops requests from reading ~/.netrc and attaching credentials of its own to the request, so nothing is sent but the GET itself
    (the proxy settings in the environment are still used)."""
    return request


def _fetch_body(url, headers, abandon, box, deadline):
    """The worker of _http_get: the GET itself, read in small pieces so that `abandon` and the deadline are noticed."""
    started = time.monotonic()
    r = requests.get(url, headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), allow_redirects=False, stream=True, auth=_no_auth)
    box["response"] = r
    try:
        if int(r.headers.get("Content-Length") or 0) > MAX_RESPONSE_BYTES:
            raise _TooLarge()
        body = b""
        for chunk in r.iter_content(64):
            if abandon.is_set() or time.monotonic() - started > deadline:
                raise requests.Timeout("the answer took too long")
            body += chunk
            if len(body) > MAX_RESPONSE_BYTES:
                raise _TooLarge()
        return r.status_code, r.headers, body
    finally:
        r.close()


def _http_get(url, headers, deadline=None):
    """GET `url` and return (status, headers, body bytes). HTTPS certificates are verified (requests' default: never switched off here), redirects are not followed,
    no credentials of the user's are added (no ~/.netrc), the body has a size cap, and the WHOLE exchange (name lookup, connecting, headers, body) has a deadline
    that is enforced from outside: the request runs on a helper thread and the caller stops waiting for it when the time is up, however slowly the server drips
    its answer. Raises requests.RequestException or _TooLarge. Tests replace this function."""
    deadline = TOTAL_DEADLINE if deadline is None else deadline
    box, abandon = {}, threading.Event()

    def work():
        try:
            box["result"] = _fetch_body(url, headers, abandon, box, deadline)
        except BaseException as e:             # handed to the caller below
            box["error"] = e

    worker = threading.Thread(target=work, daemon=True, name="update-http")
    worker.start()
    worker.join(deadline)
    if worker.is_alive():
        abandon.set()
        resp = box.get("response")
        if resp is not None:        # closing can wait for the blocked read, so never from this thread; the worker also stops by itself at its next piece
            threading.Thread(target=lambda: resp.close(), daemon=True, name="update-http-close").start()
        raise requests.Timeout("the answer took too long")
    if "error" in box:
        raise box["error"]
    return box["result"]


def fetch_latest(etag=None, now=None):
    """Ask GitHub for the latest release and describe what happened. Never raises. Returns a dict:
    outcome  ok | not_modified | no_release | rate_limited | offline | certificate | bad_response | too_large | error
    reached  True if GitHub answered at all (it decides how soon the periodic check may ask again)
    message  one plain sentence for the dashboard
    release  {tag, url, notes} when outcome is ok;  etag  the validator to send next time;  retry_at  a time to wait for (rate limit)"""
    now = time.time() if now is None else now
    url = f"{API_BASE}/repos/{REPO}/releases/latest"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if isinstance(etag, str) and 0 < len(etag) < 200 and re.fullmatch(r"[\x21-\x7e]+", etag):
        headers["If-None-Match"] = etag
    out = {"outcome": "error", "reached": False, "message": "", "release": None, "etag": None, "retry_at": None}
    try:
        status, resp_headers, body = _http_get(url, headers)
    except _TooLarge:
        out.update(outcome="too_large", reached=True, message="GitHub's answer was far bigger than expected, so it was ignored.")
        return out
    except requests.exceptions.SSLError:
        out.update(outcome="certificate", message="GitHub's certificate could not be verified, so nothing was fetched. If this computer is behind a company proxy, that may be why.")
        return out
    except requests.RequestException as e:
        out.update(outcome="offline", message=f"GitHub could not be reached ({type(e).__name__}). Is this computer online?")
        return out
    except Exception as e:                                  # whatever else: the check must never take anything down
        out.update(message=f"The check failed ({type(e).__name__}).")
        return out
    out["reached"] = True
    if status == 304:
        out.update(outcome="not_modified", etag=etag, message="No change since the last check.")
    elif status in (403, 429):
        wait = MAX_BLOCK_S
        try:
            reset, retry = resp_headers.get("X-RateLimit-Reset"), resp_headers.get("Retry-After")
            if retry and str(retry).isdigit():
                wait = int(retry)
            elif reset and str(reset).isdigit():
                wait = int(reset) - now
        except Exception:
            pass
        wait = max(60, min(int(wait), MAX_BLOCK_S))
        out.update(outcome="rate_limited", retry_at=now + wait, message=f"GitHub is limiting requests from this address right now; it will be asked again after about {max(1, round(wait / 60))} minutes.")
    elif status == 404:
        out.update(outcome="no_release", message="GitHub has no published release of this project (yet).")
    elif 300 <= status < 400:
        out.update(outcome="bad_response", message="GitHub answered with a redirect, which this check does not follow.")
    elif status != 200:
        out.update(outcome="error", reached=not (isinstance(status, int) and status >= 500), message=f"GitHub answered with an error (HTTP {status if isinstance(status, int) else '?'}).")
    else:
        try:
            release = clean_release(json.loads(body.decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            release = None
        if release is None:
            out.update(outcome="bad_response", message="GitHub's answer was not a release this check understands, so it was ignored.")
        else:
            tag_etag = resp_headers.get("ETag") if hasattr(resp_headers, "get") else None
            out.update(outcome="ok", release=release, etag=tag_etag if isinstance(tag_etag, str) and len(tag_etag) < 200 else None, message="Checked.")
    return out


# ---- what kind of installation this is ----------------------------------------------------------------------------------------------------
KINDS = ("git", "docker", "packaged", "other")
DOCKER_STEPS = ["git pull", "./setup.sh --docker        (if you started it that way; on Windows: setup.bat --docker)",
                "docker compose up -d --build   (if you start it with Compose yourself)"]


def in_container(environ=None, dockerenv="/.dockerenv"):
    """True inside Docker: the image sets MESHLLM_CONTAINER=1, and Docker creates /.dockerenv."""
    environ = os.environ if environ is None else environ
    return environ.get("MESHLLM_CONTAINER") == "1" or os.path.exists(dockerenv)


def detect_install(root=None, environ=None, frozen=None, container=None, which=shutil.which, exists=os.path.exists):
    """{kind, can_update, reason, steps}: git | docker | packaged | other. Only `git` can update itself (and only with a usable git on PATH).
    Order matters: a packaged program is never a checkout, and a container is never updated from inside even if a .git folder is mounted."""
    root = paths.project_root() if root is None else root
    frozen = paths.is_frozen() if frozen is None else frozen
    container = in_container(environ) if container is None else container
    if frozen:
        return {"kind": "packaged", "can_update": False, "steps": [],
                "reason": "This is the downloadable program. It cannot replace itself while it runs: download the new archive from the release page and check it first (docs/releasing.md, Checking a download)."}
    if container:
        return {"kind": "docker", "can_update": False, "steps": list(DOCKER_STEPS),
                "reason": "This runs in Docker, which cannot update itself (that would need the Docker socket, which is never given to it). On the computer that runs Docker, in the project folder:"}
    if exists(os.path.join(str(root), ".git")):
        if which("git"):
            return {"kind": "git", "can_update": True, "reason": None, "steps": []}
        return {"kind": "other", "can_update": False, "steps": [], "reason": "This is a git checkout but git was not found on this computer's PATH, so it cannot update itself. Update it by hand (git pull)."}
    return {"kind": "other", "can_update": False, "steps": [],
            "reason": "This installation is not a git checkout (a copied folder, a zip or a pip install), so it cannot update itself. Replace it with the new release the way you installed it."}


def restart_plan(platform=None, container=None, main_spec=None, argv=None, executable=None, flags=None, unbuffered=None, isfile=os.path.isfile, access=os.access):
    """(command list or None, reason or None): how the running bridge could start itself again in place (`argv` is the arguments after the program name, default sys.argv[1:]). It repeats `python [-u] -m meshllm <same flags>`
    and only when the process was started exactly that way (`python -m meshllm`, as the start scripts, the systemd unit and the launchd agent do), outside Docker,
    and not on Windows, where an exec starts a new process with a new PID and the old console and PID file would point at a dead process."""
    platform = sys.platform if platform is None else platform
    if platform.startswith("win"):
        return None, "On Windows the bridge cannot replace itself in place (its process id would change under the start script and the console)."
    if (in_container() if container is None else container):
        return None, "A container is restarted from outside."
    if paths.is_frozen():
        return None, "The packaged program is not restarted by the updater."
    if main_spec is None:
        main_spec = getattr(getattr(sys.modules.get("__main__"), "__spec__", None), "name", None)
    if main_spec not in ("meshllm.__main__", "meshllm.bridge", "meshllm"):
        return None, "The bridge was not started with `python -m meshllm`, so the updater cannot be sure how to start it again."
    exe = sys.executable if executable is None else executable
    if not exe or not isfile(exe) or not access(exe, os.X_OK):
        return None, "The Python program that runs the bridge could not be found again."
    flags = sys.flags if flags is None else flags
    if unbuffered is None:
        unbuffered = bool(getattr(sys.stdout, "write_through", False))      # `python -u` (sys.flags has no field for it; the start scripts and units use it so log lines appear at once)
    command = [exe] + (["-u"] if unbuffered else []) + (["-B"] if getattr(flags, "dont_write_bytecode", 0) else []) + ["-m", "meshllm"] + list(sys.argv[1:] if argv is None else argv)
    return command, None


def finish_restart(bridge, execv=os.execv, flush=True):
    """Called by main() after Bridge.run() has returned because an update asked for a restart: close what needs closing, then replace this process
    with the new command (same PID, same stdout/stderr, same folder). If the exec fails the bridge is already stopped, so it exits with an error
    code (a supervisor that restarts on failure then starts it)."""
    command = bridge.restart_command
    print("[update] restarting the bridge now", flush=True)
    for step in (lambda: bridge.mesh.stop(),
                 lambda: (bridge.web_server.shutdown(), bridge.web_server.server_close()) if bridge.web_server is not None else None,
                 lambda: bridge.audit.commit_and_flush()):
        try:
            step()
        except Exception as e:
            print(f"[update] while closing: {type(e).__name__}: {e}", flush=True)
    if flush:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
    try:
        execv(command[0], command)
    except OSError as e:
        print(f"[update] could not start the bridge again ({e}); start it yourself. The update itself is done.", file=sys.stderr, flush=True)
        sys.exit(1)


# ---- git ----------------------------------------------------------------------------------------------------------------------------------
def _scrub(text):
    """Error text from a command with any user:password@ part of a URL hidden, shortened to its tail."""
    text = re.sub(r"(://)[^/@\s]*@", r"\1***@", text or "").strip()
    return text[-400:] if len(text) > 400 else text


def _kill_group(proc):
    """Stop a child and everything it started: on POSIX the child leads its own session (see _run), so its whole process group is killed; elsewhere only the child can be."""
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.communicate(timeout=10)            # reap it and close its pipes
    except Exception:
        pass


def _run(argv, cwd, timeout, env=None):
    """The only place a program is started. `argv` is a list of strings (never a shell command line); output is captured, there is no stdin, a time limit applies,
    and on POSIX the child has no controlling terminal (so nothing can prompt for a password) and leads its own process group, which is killed as a whole if the
    time runs out or the wait is interrupted (a pip or git that started helpers leaves none behind)."""
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise TypeError("argv must be a non-empty list of strings")
    kw = {"start_new_session": True} if os.name == "posix" else {}
    proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", **kw)
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException:                       # a timeout, Ctrl+C, anything
        _kill_group(proc)
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _git_env():
    """The environment for git: the caller's, minus variables that would point git at some other repository, and never ask a question."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k in ("GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSL_CAINFO", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    return env


class Git:
    """Runs git in the project folder. Every call is an argument list; run() returns (exit code, stdout, stderr) and raises UpdateError on a timeout or a
    missing program, never on a non-zero exit (the caller decides what that means)."""

    def __init__(self, root, exe=None):
        self.root = root
        self.exe = exe or shutil.which("git")
        if not self.exe:
            raise UpdateError("git was not found on this computer.")

    def run(self, *args, timeout=GIT_TIMEOUT):
        """(returncode, stdout, stderr) of `git <args>`; the arguments are plain strings built by the methods below."""
        try:
            p = _run([self.exe] + list(args), self.root, timeout, env=_git_env())
        except subprocess.TimeoutExpired:
            raise UpdateError(f"git {args[0]} took longer than {timeout} seconds and was stopped.")
        except OSError as e:
            raise UpdateError(f"git could not be run ({e}).")
        return p.returncode, p.stdout.rstrip("\r\n"), _scrub(p.stderr)

    def ok(self, *args, what, timeout=GIT_TIMEOUT):
        """stdout of a git command that must succeed; otherwise an UpdateError that says `what` failed and why."""
        code, out, err = self.run(*args, timeout=timeout)
        if code != 0:
            raise UpdateError(f"{what} failed: {err or 'git gave no reason'}")
        return out

    def head(self):
        """The full id of the commit that is checked out."""
        return require_sha(self.ok("rev-parse", "--verify", "HEAD", what="Reading the current commit"))

    def branch(self):
        """The checked-out branch's name, or None when HEAD is detached."""
        code, out, _ = self.run("symbolic-ref", "--short", "-q", "HEAD")
        return out if code == 0 and out else None

    def dirty_files(self):
        """Tracked files with uncommitted changes (modified or staged), at most 5 names; untracked files do not count."""
        out = self.ok("status", "--porcelain", "--untracked-files=no", what="Checking for uncommitted changes")
        return [l[2:].strip() for l in out.splitlines() if l.strip()][:5]

    def origin_url(self):
        """The address of the `origin` remote."""
        return self.ok("remote", "get-url", "origin", what="Reading the origin address")

    def fetch_tag(self, tag):
        """Fetch exactly one tag from origin (never another ref, never all tags). Git refuses to overwrite a local tag of the same name that points elsewhere."""
        require_tag(tag)
        self.ok("-c", "transfer.fsckObjects=true", "fetch", "--no-tags", "--no-recurse-submodules", "origin", f"refs/tags/{tag}:refs/tags/{tag}", what=f"Fetching {tag} from GitHub")

    def fetch_main(self):
        """Fetch origin's main branch into origin/main (the tag must be on it). Not forced: a rewritten main is refused."""
        self.ok("-c", "transfer.fsckObjects=true", "fetch", "--no-tags", "--no-recurse-submodules", "origin", f"refs/heads/main:{MAIN_REF}", what="Fetching main from GitHub")

    def tag_commit(self, tag):
        """The commit id a tag points at (an annotated tag is followed to its commit)."""
        require_tag(tag)
        return require_sha(self.ok("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}", what=f"Reading {tag}"))

    def is_ancestor(self, older, newer):
        """True if commit `older` is an ancestor of (or the same as) `newer`; each is a full commit id or the fixed name of origin's main."""
        for side in (older, newer):
            if side != MAIN_REF:
                require_sha(side)
        code, _, err = self.run("merge-base", "--is-ancestor", older, newer)
        if code not in (0, 1):
            raise UpdateError(f"Comparing commits failed: {err}")
        return code == 0

    def version_at(self, sha):
        """The `__version__` written in meshllm/__init__.py at that commit, or None."""
        require_sha(sha)
        code, out, _ = self.run("show", f"{sha}:meshllm/__init__.py")
        m = re.search(r'^__version__\s*=\s*"([^"\n]{1,40})"\s*$', out, re.M) if code == 0 else None
        return m.group(1) if m else None

    def changed(self, old, new, path):
        """True if `path` differs between two commits."""
        require_sha(old), require_sha(new)
        return bool(self.ok("diff", "--name-only", old, new, "--", path, what="Comparing the two versions"))

    def would_overwrite(self, old, new, root):
        """Paths (at most 5) that the release adds and that already exist in the working tree. They cannot be tracked files (those are the clean-tree check's job), so they are
        untracked or ignored files of the owner's, and a fast-forward would silently replace the ignored ones."""
        require_sha(old), require_sha(new)
        out = self.ok("diff", "--name-only", "-z", "--diff-filter=A", old, new, what="Comparing the two versions")
        clash = []
        for name in [n for n in out.split("\0") if n]:
            parts = name.replace("\\", "/").split("/")
            if name.startswith("/") or ".." in parts or os.path.isabs(name):
                raise UpdateError("The release contains a path that is not inside the project folder, so it is refused.")
            if os.path.lexists(os.path.join(str(root), *parts)):
                clash.append(name)
        return clash[:5]

    def fast_forward(self, sha):
        """Move the checked-out branch forward to `sha`, only if that is a fast-forward and no local change is in the way."""
        require_sha(sha)
        self.ok("merge", "--ff-only", "--quiet", sha, what="Applying the update", timeout=GIT_TIMEOUT)

    def roll_back(self, previous):
        """Put the branch back on `previous` with `reset --keep`, which refuses rather than overwrite anything with local changes."""
        require_sha(previous)
        self.ok("reset", "--keep", previous, what="Rolling back")


NO_VENV_MESSAGE = ("This Python is not a virtual environment, so the update will not install packages for you: update the packages yourself with "
                   "pip install -r requirements.txt after pulling, or use the installer (./setup.sh).")


def in_virtualenv():
    """True if this Python runs inside a virtual environment (the installer's .venv is one). The updater only runs pip there: it will not change a system-wide Python."""
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix) or hasattr(sys, "real_prefix")


def _pip_install(root):
    """Install requirements.txt with the interpreter that runs the bridge. Returns (ok, message)."""
    try:
        p = _run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt"], root, PIP_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"pip took longer than {PIP_TIMEOUT // 60} minutes and was stopped."
    except OSError as e:
        return False, f"pip could not be run ({e})."
    return p.returncode == 0, _scrub(p.stderr or p.stdout)


def _smoke(root, version):
    """Start the pulled code just far enough to print its version (`python -m meshllm --version`): proves it imports with the installed packages. Returns (ok, message)."""
    try:
        p = _run([sys.executable, "-m", "meshllm", "--version"], root, SMOKE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "The new version did not answer `--version` in time."
    except OSError as e:
        return False, f"The new version could not be started ({e})."
    if p.returncode != 0:
        return False, _scrub(p.stderr or p.stdout) or "The new version exited with an error."
    if p.stdout.strip() != f"meshllm {version}":
        return False, f"The new version reported {p.stdout.strip()[:60]!r} instead of meshllm {version}."
    return True, ""


# ---- the service the bridge owns ----------------------------------------------------------------------------------------------------------
class Updater:
    """Settings, the cached check, the periodic thread and the git update for one bridge. All state that must survive a restart lives in the settings table."""

    def __init__(self, bridge, root=None, clock=time.time):
        self.bridge = bridge
        self.audit = bridge.audit
        self.root = paths.project_root() if root is None else root
        self.clock = clock
        self._lock = threading.Lock()              # the saved state
        self._check_lock = threading.Lock()        # one check at a time
        self._apply_lock = threading.Lock()        # one update at a time
        self._stop = threading.Event()
        self._thread = None
        self.progress = {"phase": "idle", "step": "", "message": "", "ok": None, "restart": None, "tag": None}
        self.stuck = None            # set (in memory only) when an update failed and could not be rolled back: the code on disk is then not the code that is running

    # ---- small things ---------------------------------------------------------------------------------------------------------------------
    @property
    def demo(self):
        """True in --demo: the simulated bridge contacts nothing and updates nothing."""
        return bool(getattr(self.bridge.args, "demo", False))

    def install(self):
        """What kind of installation this is (see detect_install); looked up each time, it is cheap."""
        return detect_install(self.root)

    def enabled(self):
        """True only if the owner turned the periodic check on (default off)."""
        return self.audit.get_setting(SETTING_ENABLED, "off") == "on"

    def set_enabled(self, value):
        """Turn the periodic check on or off; `value` must be a real bool. Turning it on does not check at once (the next hourly look does, if due)."""
        if not isinstance(value, bool):
            raise UpdateError("enabled must be true or false.")
        self.audit.set_setting(SETTING_ENABLED, "on" if value else "off")
        print(f"[update] periodic check {'on' if value else 'off'}")

    def _load(self, key):
        """A saved JSON object, or {} if there is none or it is damaged."""
        try:
            data = json.loads(self.audit.get_setting(key, "") or "{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self, **changes):
        with self._lock:
            state = self._load(SETTING_STATE)
            state.update(changes)
            self.audit.set_setting(SETTING_STATE, json.dumps(state))
            return state

    def latest(self):
        """The cached latest release {tag, url, notes}, re-validated, or None."""
        data = self._load(SETTING_STATE).get("latest")
        if not isinstance(data, dict):
            return None
        tag = data.get("tag")
        if not valid_tag(tag):
            return None
        return {"tag": tag, "url": release_url(tag), "notes": plain_text(data.get("notes"))}

    def restart_pending(self):
        """True after a successful update whose new code is on disk but not yet running (a restart that was not possible or not done): another update is pointless until the bridge restarts."""
        done = self._clean_applied(self._load(SETTING_APPLIED))
        return bool(done and done["result"] == "done" and done["to_version"] and done["to_version"] != __version__ and is_newer("v" + done["to_version"]))

    def available(self):
        """The cached release if it is newer than this program, else None."""
        latest = self.latest()
        return latest if latest and is_newer(latest["tag"]) else None

    # ---- the check ------------------------------------------------------------------------------------------------------------------------
    def _blocked_until(self, now):
        until = self._load(SETTING_STATE).get("blocked_until")
        return until if isinstance(until, (int, float)) and now < until < now + MAX_BLOCK_S + 60 else None

    def due(self, now=None):
        """True if the periodic check is switched on, this is not a demo, GitHub has not asked us to wait, and the last attempt is old enough."""
        now = self.clock() if now is None else now
        if self.demo or not self.enabled() or self._blocked_until(now):
            return False
        state = self._load(SETTING_STATE)
        attempt = state.get("attempt")
        if not isinstance(attempt, (int, float)) or attempt > now + 60:      # never asked, or the clock moved back
            return True
        return now - attempt >= (CHECK_EVERY_S if state.get("reached") else RETRY_UNREACHED_S)

    def check(self, manual=False):
        """Ask GitHub now (the caller already decided that is allowed) and save the answer. Returns {ok, message, outcome}; never raises for a network problem.
        One at a time: a second caller gets the saved answer with a message instead of a second request."""
        now = self.clock()
        if self.demo:
            raise UpdateError("Demo mode contacts nothing, so there is nothing to check.")
        if not self._check_lock.acquire(blocking=False):
            return {"ok": False, "outcome": "busy", "message": "A check is already running."}
        try:
            state = self._load(SETTING_STATE)
            blocked = self._blocked_until(now)
            if blocked:
                return {"ok": False, "outcome": "rate_limited", "message": f"GitHub asked for a pause; the next check can be made in about {max(1, round((blocked - now) / 60))} minutes."}
            attempt = state.get("attempt")
            if manual and isinstance(attempt, (int, float)) and 0 <= now - attempt < MIN_GAP_S:
                return {"ok": True, "outcome": "recent", "message": "Just checked."}
            res = fetch_latest(state.get("etag"), now=now)
            changes = {"attempt": now, "reached": res["reached"], "error": None if res["outcome"] in ("ok", "not_modified", "no_release") else res["message"]}
            if res["outcome"] == "ok":
                changes.update(latest=res["release"], etag=res["etag"], checked=now)
            elif res["outcome"] == "not_modified":
                changes.update(checked=now)
            elif res["outcome"] == "no_release":
                changes.update(latest=None, etag=None, checked=now)
            if res["outcome"] == "rate_limited":
                changes["blocked_until"] = res["retry_at"]
            else:
                changes["blocked_until"] = None
            self._save_state(**changes)
            return {"ok": res["outcome"] in ("ok", "not_modified", "no_release"), "outcome": res["outcome"], "message": res["message"]}
        finally:
            self._check_lock.release()

    def check_now(self):
        """The 'Check now' button: always allowed (the owner asked), even with the periodic check off. Returns the status plus how the check went."""
        res = self.check(manual=True)
        return {**self.status(), "result": res}

    def maybe_check(self):
        """The periodic check: asks GitHub only if switched on and due. Returns True if it asked."""
        if not self.due():
            return False
        self.check(manual=False)
        return True

    def start(self):
        """Start the background thread (idle while the setting is off, and it does nothing at all in demo mode)."""
        if self._thread is None and not self.demo:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="update-check")
            self._thread.start()

    def stop(self):
        """Ask the background thread to end."""
        self._stop.set()

    def _loop(self):
        """Wait a minute after start-up (so it never competes with it), then once an hour see whether a check is due."""
        wait = START_DELAY_S
        while not self._stop.wait(wait):
            wait = 3600
            try:
                self.maybe_check()
            except Exception as e:
                print(f"[update] check failed: {type(e).__name__}: {e}")

    # ---- what the dashboard shows ---------------------------------------------------------------------------------------------------------------
    def status(self):
        """Everything the Updates section shows (admin only): versions, the install type and what it can do, the saved check, progress and the last update."""
        install = self.install()
        state = self._load(SETTING_STATE)
        latest, available = self.latest(), self.available()
        can_apply, why = install["can_update"] and not self.demo, install["reason"]
        pending = self.restart_pending()
        with self._lock:
            stuck = dict(self.stuck) if self.stuck else None
        if self.demo:
            can_apply, why = False, "Demo mode does not update."
        elif stuck:
            can_apply, why = False, "The code on disk is not the code that is running: an update failed and could not be rolled back. " + stuck["hint"] + "."
        elif pending:
            can_apply, why = False, "Already updated: restart the bridge to run the new version."
        plan, plan_why = restart_plan()
        if stuck:
            plan, plan_why = None, "The code on disk is not the running code; fix that first. " + stuck["hint"] + "."
        applied = self._load(SETTING_APPLIED)
        with self._lock:
            progress = dict(self.progress)
        return {
            "version": __version__, "demo": self.demo, "check_enabled": self.enabled(),
            "install": {"kind": install["kind"], "steps": install["steps"]},
            "can_apply": bool(can_apply and available), "can_update": bool(can_apply), "reason": why,
            "checked": state.get("checked") if isinstance(state.get("checked"), (int, float)) else None,
            "last_error": plain_text(state.get("error"), 300) or None,
            "latest": latest, "update_available": bool(available), "restart_pending": pending, "stuck": stuck, "virtualenv": in_virtualenv(),
            "restart_possible": plan is not None, "restart_note": plan_why,
            "progress": progress, "applied": self._clean_applied(applied),
        }

    @staticmethod
    def _clean_applied(data):
        """The last update's record, only fields of the expected shape (so a damaged setting cannot put anything odd on the page)."""
        if not data:
            return None
        out = {}
        for key in ("from_sha", "to_sha"):
            out[key] = data[key] if isinstance(data.get(key), str) and SHA_RE.match(data[key]) else None
        for key in ("from_version", "to_version"):
            out[key] = data[key] if isinstance(data.get(key), str) and parse_version(data[key]) else None
        out["ts"] = data["ts"] if isinstance(data.get("ts"), (int, float)) else None
        out["backup"] = data["backup"] if isinstance(data.get("backup"), str) and re.fullmatch(r"manual-\d{8}-\d{6}\.db", data["backup"]) else None
        out["result"] = data["result"] if data.get("result") in ("done", "rolled_back", "failed", "running") else None
        return out

    # ---- the update -----------------------------------------------------------------------------------------------------------------------
    def _set(self, **fields):
        with self._lock:
            self.progress.update(fields)

    def start_apply(self, tag, who="admin"):
        """The 'Update now' request: check everything that can be checked without touching git, then do the update on a background thread and return at once.
        `tag` must be exactly the release this server last saw (a client cannot pick another version)."""
        if self.demo:
            raise UpdateError("Demo mode does not update.")
        require_tag(tag)
        install = self.install()
        if not install["can_update"]:
            raise UpdateError(install["reason"] or "This installation cannot update itself.")
        available = self.available()
        if available is None:
            raise UpdateError("No newer release is known. Use Check now first.")
        if self.stuck:
            raise UpdateError("An earlier update failed and could not be rolled back. " + self.stuck["hint"] + ".")
        if self.restart_pending():
            raise UpdateError("The code was already updated; restart the bridge to run it.")
        if tag != available["tag"]:
            raise UpdateError(f"The newest release this bridge knows is {available['tag']}, not {tag}. Reload the page.")
        if self.bridge.backups.staged():
            raise UpdateError("A database restore is waiting for the next start. Cancel it, or restart the bridge to apply it, before updating.")
        if not self._apply_lock.acquire(blocking=False):
            raise UpdateBusy("An update is already running.")
        self._set(phase="updating", step="Starting", message="", ok=None, restart=None, tag=tag)
        print(f"[update] {who} started the update {__version__} -> {tag[1:]}")
        threading.Thread(target=self._apply_thread, args=(tag,), daemon=True, name="update-apply").start()
        return {"started": True, "tag": tag}

    def _apply_thread(self, tag):
        """Runs the update and always ends with a progress record and a released lock, whatever happens."""
        try:
            self.apply(tag)
        except UpdateError as e:
            self._set(phase="failed", message=str(e), ok=False, restart=None)
            print(f"[update] failed: {e}")
        except Exception as e:                      # a bug here must not leave the page waiting for ever
            self._set(phase="failed", message=f"The update stopped unexpectedly ({type(e).__name__}). Nothing was restarted; see the bridge's log.", ok=False, restart=None)
            print(f"[update] unexpected error: {type(e).__name__}: {e}")
        finally:
            self._apply_lock.release()

    def _record(self, **fields):
        data = self._load(SETTING_APPLIED)
        data.update(fields)
        self.audit.set_setting(SETTING_APPLIED, json.dumps(data))

    def apply(self, tag):
        """The git update, step by step, each with its own failure message. Raises UpdateError on a refusal or failure (after rolling back when it had changed anything)."""
        require_tag(tag)
        version = tag[1:]
        git = Git(self.root)
        self._set(step="Checking this folder")
        if not ((git.run("rev-parse", "--is-inside-work-tree")[1]) == "true"):
            raise UpdateError("This folder is not a git checkout any more.")
        if not self.origin_allowed(git.origin_url()):
            raise UpdateError("This checkout's `origin` is not this project's GitHub repository, so it will not update from it. Update by hand (git pull).")
        dirty = git.dirty_files()
        if dirty:
            raise UpdateError("These files have uncommitted changes, so nothing was touched: " + ", ".join(dirty) + ". Commit or stash them, or update by hand.")
        branch = git.branch()
        if not branch:
            raise UpdateError("This checkout is not on a branch (a detached HEAD), so it cannot be fast-forwarded. Switch to a branch first (git switch main).")
        previous = git.head()

        self._set(step=f"Fetching {tag} from GitHub")
        git.fetch_tag(tag)
        git.fetch_main()
        target = git.tag_commit(tag)
        if not git.is_ancestor(target, MAIN_REF):
            raise UpdateError(f"{tag} is not on this project's main branch, so it is refused.")
        if git.version_at(target) != version:
            raise UpdateError(f"{tag} does not contain version {version} in meshllm/__init__.py, so it is refused.")
        if git.is_ancestor(target, previous):
            raise UpdateError(f"This folder already has {tag}'s code. Restart the bridge to run it.")
        if not git.is_ancestor(previous, target):
            raise UpdateError(f"This checkout has its own commits that {tag} does not contain, so it cannot be fast-forwarded. Update by hand (git pull, or merge {tag}).")
        wants_pip = git.changed(previous, target, "requirements.txt")
        if wants_pip and not in_virtualenv():
            raise UpdateError(NO_VENV_MESSAGE + " Nothing was changed.")
        clash = git.would_overwrite(previous, target, self.root)
        if clash:
            raise UpdateError(f"{tag} adds files that already exist here as untracked or ignored files of yours ({', '.join(clash)}), and updating would replace them, so nothing was touched. Move them away, or update by hand.")

        self._set(step="Backing up the database")
        try:
            backup = self.bridge.backups.create("manual")
            self.bridge.backups.verify(backup)
        except Exception as e:
            raise UpdateError(f"The backup could not be made or did not check out ({e}), so nothing was changed.")
        self._record(from_sha=previous, from_version=__version__, to_sha=target, to_version=version, ts=self.clock(), backup=backup, result="running")

        self._set(step=f"Applying {tag}")
        try:
            git.fast_forward(target)
        except UpdateError:
            self._record(result="failed")
            raise
        problem = None
        if wants_pip:
            self._set(step="Installing the packages this version needs")
            ok, msg = _pip_install(self.root)
            if not ok:
                problem = f"Installing the required packages failed: {msg}"
        if problem is None:
            self._set(step="Checking that the new version starts")
            ok, msg = _smoke(self.root, version)
            if not ok:
                problem = f"The new version did not start: {msg}"
        if problem is not None:
            self._set(step="Rolling back")
            try:
                if git.head() != target:
                    raise UpdateError("the checkout moved while updating")
                git.roll_back(previous)
                if git.head() != previous:
                    raise UpdateError("the commit did not change")
            except UpdateError as e:
                try:
                    files = [plain_text(f, 120) for f in git.dirty_files()]
                except UpdateError:
                    files = []
                hint = f"stash or discard the changes to {', '.join(files) if files else 'the changed files'}, then run git reset --keep {previous}"
                with self._lock:
                    self.stuck = {"previous": previous, "files": files, "hint": hint}
                self._record(result="failed")
                raise UpdateError(f"{problem} Rolling back also failed ({e}). The code on disk is now the new version but the bridge is still running the old one, so it will not be restarted. To fix it: {hint}.")
            self._record(result="rolled_back")
            extra = " Packages may already have been upgraded; run `pip install -r requirements.txt` from the old version if something misbehaves." if wants_pip else ""
            raise UpdateError(f"{problem} The code was rolled back to {__version__}; the bridge keeps running the old version.{extra}")

        self._record(result="done")
        print(f"[update] updated {__version__} -> {version} (back-up {backup}; to go back: git checkout {previous})")
        command, why = restart_plan()
        if command is None:
            self._set(phase="done", ok=True, restart="manual", step="Done", message=f"Updated to {version}. Restart the bridge to run it. ({why})")
            return
        self._set(phase="restarting", ok=True, restart="auto", step="Restarting", message=f"Updated to {version}. The bridge is restarting.")
        time.sleep(RESTART_GRACE_S)                 # let the page read that before the process is replaced
        if self.bridge.backups.staged():            # asked again just before: a restore set aside meanwhile would be applied by this restart
            self._set(phase="done", ok=True, restart="manual", step="Done", message=f"Updated to {version}. A database restore was set aside while this ran and a restart would apply it, so the bridge was not restarted. Restart it yourself when you want that.")
            return
        self.bridge.request_restart(command)

    @staticmethod
    def origin_allowed(url):
        """True only for this project's own GitHub address (tests replace this to allow a local folder)."""
        return isinstance(url, str) and url.strip().lower() in ORIGIN_OK
