"""The update check and the in-app update: versions, the GitHub check (against a local fake), opt-in and throttling, install types, the git update
(real git, scratch repositories, a local bare 'origin'), the restart, the database upgrade, and the routes' security.

No network and no real GitHub: the API address is a module constant that is pointed at a fake server on 127.0.0.1, every name lookup for another host
raises, every command that would run in the real project folder raises, and nothing is really restarted (execv, the shutdown and the restart plan are faked).
Fake data only: no real addresses, hosts or ids."""
import ast, re, contextlib, io, json, os, shutil, socket, sqlite3, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
from fixture import make, Checker
from meshllm import audit as auditmod, backup as backupmod, passwords, paths, updater as U, webroutes, websecurity as ws, webui
from meshllm.updater import UpdateBusy, UpdateError
import requests as rq

check = Checker()
HERE = tempfile.mkdtemp(prefix="meshupd_")
AT = chr(64)                      # spelled out so this file holds no e-mail-looking text
DEFAULT_API = U.API_BASE
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
REAL = {"venv": U.in_virtualenv, "plan": U.restart_plan, "run": U._run, "http": U._http_get, "pip": U._pip_install, "origin_allowed": U.Updater.__dict__["origin_allowed"], "version": U.__version__}


def raises(fn, exc=UpdateError):
    try:
        fn()
        return False
    except exc:
        return True


# ---- guards: nothing may leave this computer, nothing may touch the real project folder ----------------------------------------------------
_gai = socket.getaddrinfo
def guarded_gai(host, *a, **k):
    if host not in ("127.0.0.1", "localhost", "::1", None, ""):
        raise AssertionError(f"a test tried to look up {host!r}")
    return _gai(host, *a, **k)
socket.getaddrinfo = guarded_gai
CALLS = []                        # every command the updater started: (argv, cwd)
def spy_run(argv, cwd, timeout, env=None):
    assert os.path.realpath(str(cwd)) != os.path.realpath(ROOT), "a test tried to run a command in the real project folder"
    CALLS.append((list(argv), str(cwd)))
    return REAL["run"](argv, cwd, timeout, env=env)
U._run = spy_run
cfg = os.path.join(HERE, "gitconfig")
with open(cfg, "w") as f:
    f.write("[user]\n\tname = Tester\n\temail = tester" + AT + "example.invalid\n[init]\n\tdefaultBranch = main\n[advice]\n\tdetachedHead = false\n")
os.environ.update(GIT_CONFIG_GLOBAL=cfg, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
U.__version__ = "0.1.0"           # the program under test is "0.1.0", whatever the real number is now
U.RESTART_GRACE_S = 0
U.in_virtualenv = lambda: True    # CI runs without a virtual environment; the cases about that rule switch it off themselves


# ======================================================================================================================
# versions and tags
# ======================================================================================================================
good = ["v0.1.0", "v1.2.3", "v10.20.30", "v0.0.0", "v123456789.0.1"]
bad = ["v1.2.3; rm -rf /", "--upload-pack=x", "v1.2.3\n", "v1.2.3 ", " v1.2.3", "V1.2.3", "1.2.3", "v1.2", "v1.2.3.4", "v01.2.3", "v1.02.3", "v1.2.03", "v1.2.3-rc.1", "v1.2.3+b", "../v1.2.3", "refs/tags/v1.2.3", "v1.2.3/../x",
       "v١.٢.٣", "v1.2.3\x00", "v1.2.٣", "v1.2.3`id`", "v1.2.3$(id)", "v1.2.3|x", "v1.2.3&&x", "v1.2.3\r\n", "", None, 5, ["v1.2.3"], "v" + "9" * 12 + ".0.0", "-v1.2.3", "v-1.2.3", "main", "HEAD", "v1.2.3​"]
check("valid release tags are accepted", all(U.valid_tag(t) and U.require_tag(t) == t for t in good))
check(f"{len(bad)} look-alike, hostile and malformed tags are refused (injection, option, newline, non-ASCII digit, leading zero, suffix, path)", not any(U.valid_tag(t) for t in bad) and all(raises(lambda t=t: U.require_tag(t)) for t in bad), [t for t in bad if U.valid_tag(t)])
check("a commit id must be a full lower-case hex id", U.require_sha("a" * 40) and U.require_sha("0123456789abcdef" * 4) and not any(U.SHA_RE.match(x) for x in ("a" * 39, "A" * 40, "a" * 40 + "\n", "--upload-pack=x", "HEAD", "main", "g" * 40, "", "a" * 41)))
check("parse_version: strict semver only", all(U.parse_version(v) for v in ("0.1.0", "1.0.0", "1.0.0-rc.1", "1.0.0+build.5", "1.0.0-alpha-1", "10.20.30"))
      and not any(U.parse_version(v) for v in ("1.0", "v1.0.0", "01.0.0", "1.0.0-01", "1.0.0-", "1.0.0.0", "", " 1.0.0", "1.0.0\n", "1.0.0-a..b", "١.0.0", None, 1.0)))
order = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.0.1", "1.1.0", "2.0.0"]
check("the semver precedence example list sorts as the specification says", [U.parse_version(v) for v in order] == sorted(U.parse_version(v) for v in order), order)
check("a build suffix does not change precedence", U.parse_version("1.0.0+a") == U.parse_version("1.0.0+b"))
check("is_newer: higher patch, minor and major; numeric not text order (0.10.0 > 0.9.0)", U.is_newer("v0.1.1", "0.1.0") and U.is_newer("v0.2.0", "0.1.9") and U.is_newer("v1.0.0", "0.99.99") and U.is_newer("v0.10.0", "0.9.0"))
check("is_newer: equal and older are not newer (never offers a downgrade)", not U.is_newer("v0.1.0", "0.1.0") and not U.is_newer("v0.1.0", "0.2.0") and not U.is_newer("v0.9.0", "0.10.0"))
check("is_newer: a release is newer than its own pre-release, a pre-release is not newer than the release", U.is_newer("v1.0.0", "1.0.0-rc.1") and not U.is_newer("v0.9.0", "1.0.0-rc.1"))
check("is_newer: garbage on either side is simply 'no'", not U.is_newer("1.0.0", "0.1.0") and not U.is_newer("v1.0.0", "junk") and not U.is_newer(None, "0.1.0") and not U.is_newer("v1.0.0; x", "0.1.0"))
check("this module's `is_newer` uses the running program's version by default", U.is_newer("v0.1.1") and not U.is_newer("v0.1.0"))
check("the repository is a constant and the API address is HTTPS to GitHub by default (only a test changes it)", U.REPO == "TeamHarrisCali/MeshtasticLLM" and DEFAULT_API == "https://api.github.com" and U.USER_AGENT.startswith("meshllm/") and U.RELEASE_URL_PREFIX == "https://github.com/TeamHarrisCali/MeshtasticLLM/releases/")


# ======================================================================================================================
# the release document
# ======================================================================================================================
def release(tag="v0.2.0", body="Fixes and a new page.", **o):
    d = {"tag_name": tag, "draft": False, "prerelease": False, "html_url": U.RELEASE_URL_PREFIX + "tag/" + str(tag), "body": body, "name": tag}
    d.update(o)
    return json.dumps(d).encode()


C = U.clean_release
check("a stable published release is read: tag, link and notes", C(json.loads(release())) == {"tag": "v0.2.0", "url": U.RELEASE_URL_PREFIX + "tag/v0.2.0", "notes": "Fixes and a new page."})
check("drafts and pre-releases are ignored, and so is a document that does not say either", C(json.loads(release(draft=True))) is None and C(json.loads(release(prerelease=True))) is None
      and C({"tag_name": "v0.2.0", "html_url": "x"}) is None and C(json.loads(release(draft="no"))) is None and C(json.loads(release(draft=0))) is None)
check("a tag that is not v<major>.<minor>.<patch> makes the document unusable", all(C(json.loads(release(tag=t))) is None for t in ("v1.2.3; rm -rf /", "1.2.3", "v1.2", "v1.2.3-rc.1", "latest", "")) and C(json.loads(release(tag=5))) is None and C([1]) is None and C("x") is None and C(None) is None)
check("a link that points anywhere but this repository's releases is replaced by the standard one",
      all(C(json.loads(release(html_url=u)))["url"] == U.RELEASE_URL_PREFIX + "tag/v0.2.0" for u in ("https://evil.test/x", "http://github.com/" + U.REPO + "/releases/x", "javascript:alert(1)", "https://github.com/other/repo/releases/tag/v0.2.0", U.RELEASE_URL_PREFIX + "x y", None, 5, U.RELEASE_URL_PREFIX + "x" * 400)))
std = U.RELEASE_URL_PREFIX + "tag/v0.2.0"
sneaky = [U.RELEASE_URL_PREFIX + "tag/../../../evil", U.RELEASE_URL_PREFIX + "tag/%2e%2e/%2e%2e/x", U.RELEASE_URL_PREFIX + "tag/v0.2.0/../../x", U.RELEASE_URL_PREFIX + "tag/v9.9.9",
          U.RELEASE_URL_PREFIX + "tag/v0.2.0?next=https://evil.test", U.RELEASE_URL_PREFIX + "tag/v0.2.0#x", U.RELEASE_URL_PREFIX + "download/x.exe", "https://github.com/" + U.REPO + "/../other/repo/releases/tag/v0.2.0",
          "https://github.com/" + U.REPO + "/releases/tag/v0.2.0\n", "https://github.com@evil.test/" + U.REPO + "/releases/tag/v0.2.0", "https://github.com:444/" + U.REPO + "/releases/tag/v0.2.0"]
check("the release document's own link is ignored altogether: dot segments, percent-encoding, another tag, a query, userinfo all give the page built from the validated tag", all(C(json.loads(release(html_url=u)))["url"] == std for u in sneaky) and C(json.loads(release(html_url=std)))["url"] == std)
jsrx = re.search(r"const UPD_LINK = /\^(.*)\$/;", open(os.path.join(ROOT, "meshllm", "static", "js", "updates.js"), encoding="utf-8").read()).group(1).replace("\\/", "/")
check("the page's link pattern accepts exactly that shape (this project's release page for a plain vX.Y.Z tag) and nothing else", re.fullmatch(jsrx, std) and re.fullmatch(jsrx, U.release_url("v10.20.30")) and not any(re.fullmatch(jsrx, u) for u in sneaky[:3] + sneaky[4:-2] + ["https://github.com/" + U.REPO + "/releases/tag/v01.2.3", "http://github.com/" + U.REPO + "/releases/tag/v0.2.0", "javascript:alert(1)", "https://github.com/" + U.REPO + "/releases/tag/v0.2.0/"]), jsrx)
notes = C(json.loads(release(body="<script>alert(1)</script> <img src=x onerror=alert(2)>\r\nline 2\x00\x1b[31m red ‮ gnp.exe​")))["notes"]
check("notes stay plain text: HTML is kept as harmless characters (the page uses textContent), control and bidirectional characters are removed", "<script>alert(1)</script>" in notes and "\x00" not in notes and "\x1b" not in notes and "‮" not in notes and "​" not in notes and "\r" not in notes and "line 2" in notes, repr(notes))
long_notes = C(json.loads(release(body="x" * 10000)))["notes"]
check("notes are cut to a limit, and a missing or non-text body is empty", len(long_notes) <= U.NOTES_MAX + 1 and C(json.loads(release(body=None)))["notes"] == "" and C(json.loads(release(body=["x"])))["notes"] == "")


# ======================================================================================================================
# a fake GitHub
# ======================================================================================================================
class GH(BaseHTTPRequestHandler):
    seen = []                      # (path, lower-cased headers) of every request
    plan = {}

    def do_GET(self):
        GH.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
        p = GH.plan
        time.sleep(p.get("delay", 0))
        body = p.get("body", b"")
        self.send_response(p.get("status", 200))
        for k, v in p.get("headers", {}).items():
            self.send_header(k, v)
        if not p.get("no_length"):
            self.send_header("Content-Length", str(p.get("claim_length", len(body))))
        self.end_headers()
        if p.get("drip"):                      # one byte every half second, for as long as the client listens
            try:
                for _ in range(40):
                    self.wfile.write(b"x"); self.wfile.flush(); time.sleep(0.5)
            except OSError:
                pass
            return
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, *a):
        pass


gh = ThreadingHTTPServer(("127.0.0.1", 0), GH)
threading.Thread(target=gh.serve_forever, daemon=True).start()
GOOD_BASE = f"http://127.0.0.1:{gh.server_address[1]}"
dead = socket.socket(); dead.bind(("127.0.0.1", 0)); DEAD_BASE = f"http://127.0.0.1:{dead.getsockname()[1]}"; dead.close()      # a port nobody listens on
U.API_BASE = GOOD_BASE


def serve_reply(**plan):
    GH.plan = plan
    GH.seen.clear()


serve_reply(status=200, body=release(), headers={"ETag": '"abc123"'})
r = U.fetch_latest()
check("fetch: a good answer gives the release and its ETag", r["outcome"] == "ok" and r["reached"] and r["release"]["tag"] == "v0.2.0" and r["etag"] == '"abc123"', r)
path, hdr = GH.seen[0]
check("the request is a plain GET of this repository's releases/latest, with a meshllm User-Agent, and nothing else identifying", path == "/repos/TeamHarrisCali/MeshtasticLLM/releases/latest" and hdr["user-agent"] == U.USER_AGENT and hdr["accept"] == "application/vnd.github+json"
      and not {"authorization", "cookie", "x-forwarded-for", "referer", "origin"} & set(hdr) and "if-none-match" not in hdr, (path, hdr))
r = U.fetch_latest(etag='"abc123"')
check("fetch: the saved ETag is sent back (If-None-Match)", GH.seen[-1][1].get("if-none-match") == '"abc123"')
serve_reply(status=304)
r = U.fetch_latest(etag='"abc123"')
check("fetch: 304 means 'no change' and counts as reaching GitHub", r["outcome"] == "not_modified" and r["reached"] and r["release"] is None and r["etag"] == '"abc123"')
check("an ETag that could be an injection (newline, space, huge) is never sent", all("if-none-match" not in (serve_reply(status=200, body=release()), U.fetch_latest(etag=e), GH.seen[-1][1])[2] for e in ('"a"\r\nX: y', '"a b"', "x" * 500, "", None, 5)))
serve_reply(status=403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(time.time()) + 900)})
r = U.fetch_latest()
check("fetch: 403 with a rate-limit reset is 'rate limited', with the time to wait (about 15 minutes) and no crash", r["outcome"] == "rate_limited" and r["reached"] and 800 < r["retry_at"] - time.time() <= 1000, r)
serve_reply(status=429, headers={"Retry-After": "99999"})
r = U.fetch_latest()
check("fetch: a huge Retry-After is capped (never blocks the check for more than an hour)", r["outcome"] == "rate_limited" and r["retry_at"] - time.time() <= U.MAX_BLOCK_S + 2)
serve_reply(status=404, body=b'{"message":"Not Found"}')
check("fetch: 404 means no release has been published", U.fetch_latest()["outcome"] == "no_release")
serve_reply(status=500, body=b"oops")
r = U.fetch_latest(); check("fetch: a server error is an error that never reached a conclusion (retry in hours, not a day)", r["outcome"] == "error" and r["reached"] is False)
serve_reply(status=302, headers={"Location": GOOD_BASE + "/elsewhere"})
r = U.fetch_latest()
check("fetch: a redirect is not followed", r["outcome"] == "bad_response" and len(GH.seen) == 1 and all(p != "/elsewhere" for p, _ in GH.seen), (r, GH.seen))
for what, body in (("not JSON", b"<html>nope</html>"), ("a JSON list", b"[1,2]"), ("an empty object", b"{}"), ("not UTF-8", b"\xff\xfe\x00"), ("a draft", release(draft=True)), ("a pre-release", release(prerelease=True)), ("a hostile tag", release(tag="v1.2.3; rm -rf /"))):
    serve_reply(status=200, body=body)
    r = U.fetch_latest()
    check(f"fetch: an answer that is {what} is ignored cleanly", r["outcome"] == "bad_response" and r["release"] is None and "ignored" in r["message"], r)
serve_reply(status=200, body=b"x", claim_length=5 * 1024 * 1024)
check("fetch: a Content-Length over the cap is refused before reading", U.fetch_latest()["outcome"] == "too_large")
serve_reply(status=200, body=b"{" + b" " * (U.MAX_RESPONSE_BYTES + 5000) + b"}", no_length=True)
check("fetch: a body that streams past the cap with no length is cut off", U.fetch_latest()["outcome"] == "too_large")
U.API_BASE = DEAD_BASE
r = U.fetch_latest()
check("fetch: offline (connection refused) is reported plainly, not raised, and did not reach GitHub", r["outcome"] == "offline" and r["reached"] is False and "online" in r["message"], r)
U.API_BASE = GOOD_BASE
old_read = U.READ_TIMEOUT; U.READ_TIMEOUT = 0.4
serve_reply(status=200, body=release(), delay=1.5)
t0 = time.time(); r = U.fetch_latest(); took = time.time() - t0
U.READ_TIMEOUT = old_read
check("fetch: a server that is too slow times out quickly and is treated as offline", r["outcome"] == "offline" and took < 1.4, (r, took))
old_deadline = U.TOTAL_DEADLINE; U.TOTAL_DEADLINE = 1.0
serve_reply(status=200, body=b"", claim_length=100000, drip=True)
t0 = time.time(); r = U.fetch_latest(); took = time.time() - t0
U.TOTAL_DEADLINE = old_deadline
check("fetch: a server that drips one byte every half second (a long Content-Length, each read in time) cannot hold the check past the total deadline", r["outcome"] == "offline" and "Timeout" in r["message"] and took < 2.5, (r, took, "deadline", U.TOTAL_DEADLINE))
dup = new_dummy = None
home = os.path.join(HERE, "home"); os.makedirs(home)
with open(os.path.join(home, ".netrc"), "w") as f:
    f.write("machine 127.0.0.1 login tester password hunter2\n")
os.chmod(os.path.join(home, ".netrc"), 0o600)
old_home = os.environ.get("HOME")
os.environ["HOME"] = home; os.environ["USERPROFILE"] = home
try:
    serve_reply(status=200, body=release())
    rq.get(GOOD_BASE + "/control", timeout=5)
    control = GH.seen[-1][1]
    U.fetch_latest()
    sent = GH.seen[-1][1]
finally:
    os.environ["HOME"] = old_home if old_home is not None else ""
    if old_home is None: os.environ.pop("HOME")
    os.environ.pop("USERPROFILE", None)
check("a ~/.netrc entry for the host really does make plain requests add an Authorization header (the control), but the update check sends none", "authorization" in control and "authorization" not in sent, (control, sent))
U._http_get = lambda *a, **k: (_ for _ in ()).throw(ValueError("bad header"))
check("fetch: any other surprise is caught too (the check can never take the bridge down)", U.fetch_latest()["outcome"] == "error")
U._http_get = REAL["http"]

# certificates are verified: a self-signed server must be refused (the call does not turn verification off)
cert, keyf = os.path.join(HERE, "c.pem"), os.path.join(HERE, "k.pem")
try:
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", keyf, "-out", cert, "-days", "2", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], check=True, capture_output=True, timeout=60)
    import ssl
    tls = ThreadingHTTPServer(("127.0.0.1", 0), GH)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(cert, keyf)
    tls.socket = ctx.wrap_socket(tls.socket, server_side=True)
    threading.Thread(target=tls.serve_forever, daemon=True).start()
    U.API_BASE = f"https://127.0.0.1:{tls.server_address[1]}"
    serve_reply(status=200, body=release())
    r = U.fetch_latest()
    check("fetch: an untrusted (self-signed) certificate is refused, not accepted", r["outcome"] == "certificate" and r["release"] is None and not GH.seen, r)
    tls.shutdown()
except (OSError, subprocess.SubprocessError):
    print("SKIP certificate check (no openssl on this machine)")
U.API_BASE = GOOD_BASE


# ======================================================================================================================
# install types and the restart plan
# ======================================================================================================================
plain = tempfile.mkdtemp(prefix="meshplain_", dir=HERE)
gitdir = tempfile.mkdtemp(prefix="meshgit_", dir=HERE); os.mkdir(os.path.join(gitdir, ".git"))
with_git, no_git = (lambda name: "/usr/bin/git"), (lambda name: None)
d = U.detect_install(root=gitdir, environ={}, frozen=False, container=False, which=with_git)
check("a folder with .git and a usable git is the one type that can update itself", d["kind"] == "git" and d["can_update"] and d["reason"] is None)
d = U.detect_install(root=gitdir, environ={}, frozen=False, container=False, which=no_git)
check("a checkout with no git on the PATH is 'other' and says why", d["kind"] == "other" and not d["can_update"] and "git" in d["reason"])
d = U.detect_install(root=plain, environ={}, frozen=False, container=False, which=with_git)
check("a folder that is not a checkout is 'other' (notify only)", d["kind"] == "other" and not d["can_update"] and "not a git checkout" in d["reason"])
d = U.detect_install(root=gitdir, environ={}, frozen=True, container=False, which=with_git)
check("a packaged program is 'packaged' and cannot update itself, even if a .git folder is next to it", d["kind"] == "packaged" and not d["can_update"] and "cannot replace itself" in d["reason"] and "Checking a download" in d["reason"])
d = U.detect_install(root=gitdir, environ={"MESHLLM_CONTAINER": "1"}, frozen=False, which=with_git)
check("MESHLLM_CONTAINER=1 means Docker, which never updates itself, even with a .git folder mounted", d["kind"] == "docker" and not d["can_update"] and any("docker compose up -d --build" in s for s in d["steps"]) and any("git pull" in s for s in d["steps"]) and any("setup.sh --docker" in s for s in d["steps"]))
marker = os.path.join(HERE, "dockerenv"); open(marker, "w").close()
check("/.dockerenv alone also means Docker (the file name is a parameter here)", U.in_container({}, dockerenv=marker) and not U.in_container({}, dockerenv=marker + "x") and U.in_container({"MESHLLM_CONTAINER": "1"}, dockerenv=marker + "x") and not U.in_container({"MESHLLM_CONTAINER": "0"}, dockerenv=marker + "x"))
check("the real detection of this very folder returns one of the four kinds", U.detect_install()["kind"] in U.KINDS)

P = REAL["plan"]
exe = sys.executable
cmd, why = P(platform="linux", container=False, main_spec="meshllm.__main__", argv=["--web-port", "8091", "--no-warm-up"], executable=exe, flags=type("F", (), {"dont_write_bytecode": 0})(), unbuffered=True)
check("restart plan: `python -u -m meshllm` plus the same flags, for a process started with -m", cmd == [exe, "-u", "-m", "meshllm", "--web-port", "8091", "--no-warm-up"] and why is None, (cmd, why))
cmd, why = P(platform="darwin", container=False, main_spec="meshllm.bridge", argv=[], executable=exe, flags=type("F", (), {"dont_write_bytecode": 1})(), unbuffered=False)
check("restart plan: `-m meshllm.bridge` counts, and -B is kept", cmd == [exe, "-B", "-m", "meshllm"], cmd)
check("restart plan: never on Windows (an exec there is a new process with a new id)", P(platform="win32", container=False, main_spec="meshllm.__main__", argv=["x"], executable=exe)[0] is None and "Windows" in P(platform="win32", container=False, main_spec="meshllm.__main__", argv=["x"], executable=exe)[1])
check("restart plan: never in Docker", P(platform="linux", container=True, main_spec="meshllm.__main__", argv=["x"], executable=exe)[0] is None)
check("restart plan: not when it was not started with -m meshllm (a script, an IDE, another launcher)", all(P(platform="linux", container=False, main_spec=s, argv=["x"], executable=exe)[0] is None for s in (None, "launcher", "scripts.launcher", "pytest", "meshllm.other", "evil")))
check("restart plan: not when the Python program cannot be found again", P(platform="linux", container=False, main_spec="meshllm.__main__", argv=["x"], executable=os.path.join(HERE, "no-python"))[0] is None and P(platform="linux", container=False, main_spec="meshllm.__main__", argv=["x"], executable="")[0] is None)
frozen_was = paths.is_frozen
paths.is_frozen = lambda: True
check("restart plan: not for the packaged program", P(platform="linux", container=False, main_spec="meshllm.__main__", argv=["x"], executable=exe)[0] is None)
paths.is_frozen = frozen_was


class FakeServer:
    def __init__(self, log): self.log = log
    def shutdown(self): self.log.append("web.shutdown")
    def server_close(self): self.log.append("web.close")


def fake_bridge(log, command, server=True):
    b = type("B", (), {})()
    b.restart_command = command
    b.mesh = type("M", (), {"stop": lambda s: log.append("mesh.stop")})()
    b.audit = type("A", (), {"commit_and_flush": lambda s: log.append("db.flush")})()
    b.web_server = FakeServer(log) if server else None
    return b


log, execs = [], []
U.finish_restart(fake_bridge(log, ["/fake/py", "-u", "-m", "meshllm", "--x"]), execv=lambda p, a: (log.append("exec"), execs.append((p, a))))
check("finish_restart: closes the mesh counters, the web server and the database, THEN replaces the process with the same command line", log == ["mesh.stop", "web.shutdown", "web.close", "db.flush", "exec"] and execs == [("/fake/py", ["/fake/py", "-u", "-m", "meshllm", "--x"])], (log, execs))
log = []
def failing_exec(p, a): raise OSError("no such file")
err = io.StringIO()
with contextlib.redirect_stderr(err):
    try: U.finish_restart(fake_bridge(log, ["/fake/py", "-m", "meshllm"]), execv=failing_exec); code = None
    except SystemExit as e: code = e.code
check("finish_restart: if the exec fails it exits with an error code (a supervisor that restarts on failure then starts it) and says so", code == 1 and "could not start the bridge again" in err.getvalue(), (code, err.getvalue()))
log = []
bb = fake_bridge(log, ["/fake/py"]); bb.mesh.stop = lambda: (_ for _ in ()).throw(RuntimeError("x"))
U.finish_restart(bb, execv=lambda p, a: log.append("exec"))
check("finish_restart: one failing close step does not stop the others or the restart", log[-1] == "exec" and "db.flush" in log)


# ======================================================================================================================
# the code itself: no shell, no switched-off verification, tags validated before commands
# ======================================================================================================================
src = open(os.path.join(ROOT, "meshllm", "updater.py"), encoding="utf-8").read()
tree = ast.parse(src)
parents = {}
for node in ast.walk(tree):
    for child in ast.iter_child_nodes(node):
        parents[child] = node


def enclosing(node):
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return None


def call_name(call):
    f = call.func
    parts = []
    while isinstance(f, ast.Attribute):
        parts.append(f.attr); f = f.value
    if isinstance(f, ast.Name):
        parts.append(f.id)
    return ".".join(reversed(parts))


calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
sub_calls = [c for c in calls if call_name(c).startswith("subprocess.") and call_name(c) != "subprocess.TimeoutExpired"]
check("the only place a program is started is `_run` (one subprocess.Popen, plus the CompletedProcess it returns)", sorted(call_name(c) for c in sub_calls) == ["subprocess.CompletedProcess", "subprocess.Popen"] and all(enclosing(c) == "_run" for c in sub_calls), [(call_name(c), enclosing(c)) for c in sub_calls])
check("no call in the module passes shell= at all (shell=True cannot appear)", not any(k.arg == "shell" for c in calls for k in c.keywords) and "shell=True" not in src.replace("shell=True cannot", ""))
check("no os.system, os.popen, eval, exec, pickle, shell-style string commands or Popen", not any(call_name(c) in ("os.system", "os.popen", "eval", "exec", "os.spawnl", "os.spawnv", "subprocess.run", "subprocess.call", "subprocess.check_output", "subprocess.getoutput", "pickle.loads") for c in calls))
check("the request passes a no-op `auth` (so ~/.netrc is never read) and every wait is bounded: the GET runs on a helper thread that is joined with the total deadline", any(k.arg == "auth" for c in calls for k in c.keywords) and "worker.join(deadline)" in src and "def _no_auth" in src)
check("certificate verification is never turned off (no verify= anywhere), and redirects are not followed", not any(k.arg == "verify" for c in calls for k in c.keywords) and any(k.arg == "allow_redirects" and getattr(k.value, "value", None) is False for c in calls for k in c.keywords))
run_fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_run")
check("_run refuses anything that is not a list of strings (a command line string would need a shell)", "isinstance(argv, list)" in ast.get_source_segment(src, run_fn) and "raise TypeError" in ast.get_source_segment(src, run_fn))
check("_run's callers pass lists: every argument is a list display or a list concatenation", all(isinstance(c.args[0], (ast.List, ast.BinOp)) for c in calls if call_name(c) == "_run"), [ast.dump(c.args[0])[:40] for c in calls if call_name(c) == "_run"])
git_cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "Git")
unchecked = []
for fn in [n for n in git_cls.body if isinstance(n, ast.FunctionDef)]:
    params = [a.arg for a in fn.args.args[1:]]
    body = ast.get_source_segment(src, fn)
    for p in params:
        wanted = {"tag": "require_tag", "sha": "require_sha", "previous": "require_sha", "old": "require_sha", "new": "require_sha", "older": "require_sha", "newer": "require_sha"}.get(p)
        if wanted and wanted not in body:
            unchecked.append((fn.name, p))
check("every Git method that takes a tag or a commit id validates it (require_tag / require_sha) before it builds a command", not unchecked, unchecked)
apply_src = ast.get_source_segment(src, next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "apply"))
start_src = ast.get_source_segment(src, next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "start_apply"))
check("the update entry points validate the tag first (start_apply and apply both call require_tag before anything else)", apply_src.split("require_tag", 1)[0].count("git.") == 0 and start_src.index("require_tag") < start_src.index("self.install()"))
check("the tag pattern is anchored with \\A and \\Z over ASCII digits (not ^, $ or \\d)", U.TAG_RE.pattern.startswith("\\Av") and U.TAG_RE.pattern.endswith("\\Z") and "\\d" not in U.TAG_RE.pattern and U.TAG_RE.flags & __import__("re").ASCII)
check("the API address is a module constant that the request and the environment cannot change (no os.environ / argv read in the module)", "os.environ[" not in src and "environ.get(\"MESHLLM_API" not in src and "sys.argv[1" not in src.replace("sys.argv[1:]", ""))
js = open(os.path.join(ROOT, "meshllm", "static", "js", "updates.js"), encoding="utf-8").read()
check("the page code uses textContent only: no innerHTML, insertAdjacentHTML, document.write, eval, outerHTML or inline handlers", not any(w in js for w in ("innerHTML", "insertAdjacentHTML", "document.write", "eval(", "outerHTML", "onclick=", "new Function")))
check("the page code only follows a release link that points into this project's releases", "UPD_LINK.test(" in js and "startsWith" not in js and 'rel = "noopener noreferrer"' in js)
html = open(os.path.join(ROOT, "meshllm", "static", "index.html"), encoding="utf-8").read()
check("the Updates card and the header pill start hidden, and the page has no inline script", 'id="updCard" hidden' in html and 'id="updPill" type="button" hidden' in html and "<script>" not in html and "api.github.com" in html and "IP address" in html)


# ======================================================================================================================
# the service: opt-in, throttling, the cached answer
# ======================================================================================================================
NOT_DOCKER = lambda environ=None, dockerenv="/.dockerenv": False       # these tests may well run inside a container; the Docker cases patch it back on
U.in_container = NOT_DOCKER


def new_bridge(**over):
    br, radio, tmp = make(tmp=tempfile.mkdtemp(prefix="meshfx_", dir=HERE), **over)
    br.updater.root = plain                     # never the real project folder
    return br


class Clock:
    def __init__(self, t=1_000_000.0): self.t = t
    def __call__(self): return self.t


br = new_bridge()
up = br.updater
check("the periodic check is OFF by default", up.enabled() is False and br.audit.get_setting(U.SETTING_ENABLED) is None)

# no network of any kind while it is off: a fake that raises on any connection attempt
real_connect, real_create = socket.socket.connect, socket.create_connection
attempts = []
def no_connect(*a, **k): attempts.append(a); raise AssertionError("a connection was attempted")
socket.socket.connect = no_connect; socket.create_connection = no_connect
U._http_get = no_connect
try:
    due_off = up.due(); asked = up.maybe_check()
    U.START_DELAY_S = 0.05
    up.start(); time.sleep(0.6); up.stop()
    st = up.status()
    probes = [up.latest(), up.available(), up.enabled(), up.install()]
finally:
    socket.socket.connect, socket.create_connection, U._http_get = real_connect, real_create, REAL["http"]
check("OFF: due() is false, maybe_check() asks nobody, the background thread never connects, status() reads only what is saved", due_off is False and asked is False and not attempts and st["check_enabled"] is False and st["latest"] is None)

clk = Clock()
up.clock = clk
serve_reply(status=200, body=release(), headers={"ETag": '"v2"'})
up.set_enabled(True)
check("ON: set_enabled stores 'on'; due() is true before the first check", up.enabled() and br.audit.get_setting(U.SETTING_ENABLED) == "on" and up.due())
check("set_enabled accepts real booleans only", all(raises(lambda v=v: up.set_enabled(v)) for v in ("on", 1, 0, None, "true", [True], {})) and up.enabled())
check("the periodic check asks once and saves the answer (latest tag, link, notes, time)", up.maybe_check() is True and len(GH.seen) == 1 and up.latest()["tag"] == "v0.2.0" and up.available()["tag"] == "v0.2.0" and up._load(U.SETTING_STATE)["checked"] == clk.t)
for hours, expect in ((0.5, False), (12, False), (23.9, False), (24.01, True)):
    clk.t = 1_000_000.0 + hours * 3600
    check(f"24 h throttle with a fake clock: {hours} h after a check that reached GitHub, due() is {expect}", up.due() is expect)
n = len(GH.seen)
clk.t = 1_000_000.0 + 23 * 3600
up.maybe_check(); up.maybe_check()
check("...and maybe_check() sends nothing while it is not due", len(GH.seen) == n)
serve_reply(status=304)
clk.t = 1_000_000.0 + 25 * 3600
check("the next check sends the ETag, a 304 keeps the saved release and refreshes the time", up.maybe_check() and GH.seen[-1][1].get("if-none-match") == '"v2"' and up.latest()["tag"] == "v0.2.0" and up._load(U.SETTING_STATE)["checked"] == clk.t)
# offline: retry sooner than a day, never more than every 3 hours
U.API_BASE = DEAD_BASE
clk.t += 25 * 3600
t_off = clk.t
check("an attempt that could not reach GitHub is recorded as such (and the old answer is kept)", up.maybe_check() and up._load(U.SETTING_STATE)["reached"] is False and up.latest()["tag"] == "v0.2.0" and "online" in up.status()["last_error"])
for hours, expect in ((1, False), (2.9, False), (3.01, True)):
    clk.t = t_off + hours * 3600
    check(f"after an offline attempt the next periodic try is {hours} h later: due() is {expect}", up.due() is expect)
U.API_BASE = GOOD_BASE
# rate limit: honours the pause for periodic and manual checks alike
serve_reply(status=403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(clk.t) + 600)})
clk.t = t_off + 4 * 3600
up.maybe_check(); n = len(GH.seen)
check("a rate-limited answer blocks further asking until the reset, for the timer and for the button", up.due() is False and up.check(manual=True)["outcome"] == "rate_limited" and len(GH.seen) == n and up._blocked_until(clk.t))
clk.t += 700
serve_reply(status=200, body=release(tag="v0.3.0"))
check("...and once the pause is over a manual check works again and replaces the saved release", up.check(manual=True)["ok"] and up.latest()["tag"] == "v0.3.0" and up._blocked_until(clk.t) is None)
# garbage keeps the last good answer
serve_reply(status=200, body=b"<<garbage>>")
clk.t += 100
res = up.check(manual=True)
check("a garbage answer is reported, the last good release is kept, and the page gets one plain sentence", res["ok"] is False and res["outcome"] == "bad_response" and up.latest()["tag"] == "v0.3.0" and "ignored" in up.status()["last_error"])
# manual: works with the setting off, and cannot be hammered
up.set_enabled(False)
serve_reply(status=200, body=release(tag="v0.4.0", body="notes <b>x</b>"))
clk.t += 100; n = len(GH.seen)
out = up.check_now()
check("Check now works with the periodic check OFF (the owner pressed it) and returns the status and the outcome", len(GH.seen) == n + 1 and out["result"]["ok"] and out["latest"]["tag"] == "v0.4.0" and out["check_enabled"] is False and out["version"] == "0.1.0", out.get("result"))
check("pressing it again within a few seconds does not ask again", up.check_now()["result"]["outcome"] == "recent" and len(GH.seen) == n + 1)
check("the status carries the notes as plain text and the update flag", out["latest"]["notes"] == "notes <b>x</b>" and out["update_available"] is True)
# single flight
gate = threading.Event(); started = threading.Event()
def slow_fetch(etag=None, now=None):
    started.set(); gate.wait(5)
    return {"outcome": "ok", "reached": True, "message": "Checked.", "release": {"tag": "v0.5.0", "url": U.RELEASE_URL_PREFIX + "tag/v0.5.0", "notes": ""}, "etag": None, "retry_at": None}
real_fetch = U.fetch_latest; U.fetch_latest = slow_fetch
clk.t += 100
box = []
th = threading.Thread(target=lambda: box.append(up.check(manual=True))); th.start(); started.wait(5)
second = up.check(manual=True)
gate.set(); th.join(5); U.fetch_latest = real_fetch
check("only one check runs at a time (a second caller is told so and sends nothing)", second["outcome"] == "busy" and box and box[0]["ok"])
# a tampered saved answer cannot put anything odd on the page
br.audit.set_setting(U.SETTING_STATE, json.dumps({"latest": {"tag": "v1.0.0; rm -rf /", "url": "javascript:x", "notes": "n"}, "error": "e" * 1000}))
check("a damaged or tampered saved check is re-validated: a bad tag is dropped", up.latest() is None and up.available() is None and len(up.status()["last_error"]) <= 301)
br.audit.set_setting(U.SETTING_STATE, json.dumps({"latest": {"tag": "v0.9.0", "url": "javascript:x", "notes": "n\x00"}}))
check("...and a bad link is replaced by the standard releases link", up.latest()["url"].startswith(U.RELEASE_URL_PREFIX) and "\x00" not in up.latest()["notes"])
br.audit.set_setting(U.SETTING_STATE, "not json"); br.audit.set_setting(U.SETTING_APPLIED, "[1]")
check("...and settings that are not JSON objects are treated as empty", up.latest() is None and up.status()["applied"] is None and up.due() is False)
br.audit.set_setting(U.SETTING_APPLIED, json.dumps({"from_sha": "x; rm", "to_version": "<b>", "backup": "../../x", "ts": "now", "result": "done"}))
ap = up.status()["applied"]
check("the last-update record is shown only if each field has its expected shape", ap == {"from_sha": None, "to_sha": None, "from_version": None, "to_version": None, "ts": None, "backup": None, "result": "done"}, ap)
br.audit.delete_setting(U.SETTING_STATE); br.audit.delete_setting(U.SETTING_APPLIED)

# demo mode contacts nothing and updates nothing
demo = new_bridge(demo=True)
n = len(GH.seen)
demo.updater.set_enabled(True)
check("demo mode: due() is false even when switched on, Check now and Update now are refused, nothing is sent", demo.updater.due() is False and raises(lambda: demo.updater.check_now()) and raises(lambda: demo.updater.start_apply("v0.2.0")) and len(GH.seen) == n)
demo.updater.start()
check("demo mode: the background thread is never started", demo.updater._thread is None and demo.updater.status()["can_update"] is False and demo.updater.status()["reason"] == "Demo mode does not update.")


# ======================================================================================================================
# the git update, with real git in scratch repositories
# ======================================================================================================================
def git(cwd, *args, ok=True):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if ok and p.returncode != 0:
        raise RuntimeError(f"git {args} failed in {cwd}: {p.stderr}")
    return p.stdout.strip() if ok else p


def write_release(folder, version, req="requests\n", main="ok", extra=None):
    os.makedirs(os.path.join(folder, "meshllm"), exist_ok=True)
    open(os.path.join(folder, "meshllm", "__init__.py"), "w").write(f'"""fake."""\n__version__ = "{version}"\n')
    body = {"ok": 'import sys\nfrom meshllm import __version__\nif "--version" in sys.argv:\n    print("meshllm " + __version__)\n',
            "crash": 'raise SystemExit("boom: this release does not start")\n',
            "wrong": 'print("meshllm 9.9.9")\n',
            "dirty_crash": 'open("README.md", "a").write("a local change made while starting\\n")\nraise SystemExit("boom: this release modifies a tracked file and then crashes")\n'}[main]
    open(os.path.join(folder, "meshllm", "__main__.py"), "w").write(body)
    open(os.path.join(folder, "requirements.txt"), "w").write(req)
    open(os.path.join(folder, "README.md"), "w").write("readme\n")
    for name, text in (extra or {}).items():
        open(os.path.join(folder, name), "w").write(text)


def publish(dev, version, tag=None, main_branch=True, **kw):
    write_release(dev, version, **kw)
    git(dev, "add", "-A"); git(dev, "commit", "-q", "-m", f"release {version}")
    tag = tag or f"v{version}"
    git(dev, "tag", "-a", tag, "-m", tag)
    git(dev, "push", "-q", "origin", "main" if main_branch else f"HEAD:refs/heads/side-{version}", tag)


class Scn:
    pass


def scenario(name, v2_version="0.2.0", base_extra=None, shallow=False, **v2):
    s = Scn()
    base = os.path.join(HERE, name); os.makedirs(base)
    s.origin, s.dev, s.inst = (os.path.join(base, x) for x in ("origin.git", "dev", "inst"))
    git(base, "init", "-q", "--bare", "-b", "main", s.origin)
    git(base, "clone", "-q", s.origin, s.dev)
    git(s.dev, "checkout", "-q", "-B", "main")
    write_release(s.dev, "0.1.0", extra=base_extra)
    git(s.dev, "add", "-A"); git(s.dev, "commit", "-q", "-m", "release 0.1.0"); git(s.dev, "tag", "-a", "v0.1.0", "-m", "v0.1.0")
    git(s.dev, "push", "-q", "-u", "origin", "main", "v0.1.0")
    if shallow:
        git(base, "clone", "-q", "--depth", "1", "file://" + s.origin, s.inst)
    else:
        git(base, "clone", "-q", s.origin, s.inst)
    s.v1 = git(s.inst, "rev-parse", "HEAD")
    if v2 is not None:
        publish(s.dev, v2_version, **v2)
        s.v2 = git(s.dev, "rev-parse", "HEAD")
    return s


PIP, RESTARTS, PIPRES = [], [], [(True, "")]
U._pip_install = lambda root: (PIP.append(str(root)), PIPRES[0])[1]
REAL_PLAN_OK = lambda: (["/fake/python", "-u", "-m", "meshllm"], None)
U.restart_plan = REAL_PLAN_OK


def prepared(s, tag="v0.2.0", plan=REAL_PLAN_OK, origin_ok=True):
    br = new_bridge()
    up = br.updater = U.Updater(br, root=s.inst)
    br.request_restart = lambda command: RESTARTS.append(list(command))
    U.Updater.origin_allowed = staticmethod(lambda url, o=s.origin: url.strip() in (o, "file://" + o)) if origin_ok else REAL["origin_allowed"]
    U.restart_plan = plan
    PIP.clear(); RESTARTS.clear(); PIPRES[0] = (True, ""); CALLS.clear()
    if tag:
        up._save_state(latest={"tag": tag, "url": U.RELEASE_URL_PREFIX + "tag/" + tag, "notes": "n"}, checked=time.time())
    return br, up


def update_msg(up, tag="v0.2.0"):
    try:
        up.apply(tag)
        return None
    except UpdateError as e:
        return str(e)


def backups_of(br):
    return sorted(os.listdir(br.backups.dir)) if os.path.isdir(br.backups.dir) else []


# -- a clean update ----------------------------------------------------------------------------------------------------------------------
s = scenario("clean")
br, up = prepared(s)
out = io.StringIO()
with contextlib.redirect_stdout(out):
    msg = update_msg(up)
head = git(s.inst, "rev-parse", "HEAD")
check("clean update: the checkout is fast-forwarded to the release commit", msg is None and head == s.v2 and 'version = "0.2.0"'.replace("version", "__version__") in open(os.path.join(s.inst, "meshllm", "__init__.py")).read(), msg)
check("clean update: a backup was made first, it is a healthy copy, and it is named in the record", len(backups_of(br)) == 1 and br.backups.verify(backups_of(br)[0])["tables"] >= 3 and up._load(U.SETTING_APPLIED)["backup"] == backups_of(br)[0])
rec = up.status()["applied"]
check("clean update: the previous commit and versions are recorded so the page can say how to go back", rec["from_sha"] == s.v1 and rec["to_sha"] == s.v2 and rec["from_version"] == "0.1.0" and rec["to_version"] == "0.2.0" and rec["result"] == "done", rec)
check("clean update: requirements.txt did not change, so pip was not run", PIP == [])
check("clean update: it then asks for a restart with the same command line, and the page is told it is restarting", RESTARTS == [["/fake/python", "-u", "-m", "meshllm"]] and up.progress["phase"] == "restarting" and up.progress["restart"] == "auto")
check("clean update: the log line names the way back", f"git checkout {s.v1}" in out.getvalue() and "[update] updated 0.1.0 -> 0.2.0" in out.getvalue(), out.getvalue())
check("clean update: nothing was left half-done (clean tree, on the same branch)", git(s.inst, "status", "--porcelain", "--untracked-files=no") == "" and git(s.inst, "symbolic-ref", "--short", "HEAD") == "main")
subs = [(a[3] if a[1] == "-c" else a[1]) for a, _ in CALLS if os.path.basename(a[0]).startswith("git")]
allowed = {"rev-parse", "remote", "status", "symbolic-ref", "fetch", "merge-base", "merge", "show", "diff", "reset"}
fetches = [a for a, _ in CALLS if len(a) > 3 and a[3] == "fetch"]
check("every command was an argument list: git subcommands are only from the expected set, and nothing else but the Python smoke check ran", set(subs) <= allowed and all(isinstance(a, list) and all(isinstance(x, str) for x in a) for a, _ in CALLS)
      and all(os.path.basename(a[0]).startswith("git") or a[0] == sys.executable for a, _ in CALLS) and [a for a, _ in CALLS if a[0] == sys.executable] == [[sys.executable, "-m", "meshllm", "--version"]], [a for a, _ in CALLS][:20])
check("it fetched exactly the one tag and origin's main, with --no-tags and explicit refspecs (never all tags, never another ref)", [f[-1] for f in fetches] == ["refs/tags/v0.2.0:refs/tags/v0.2.0", "refs/heads/main:refs/remotes/origin/main"] and all(f[1:5] == ["-c", "transfer.fsckObjects=true", "fetch", "--no-tags"] and f[5:7] == ["--no-recurse-submodules", "origin"] for f in fetches), fetches)
check("the only history-changing command was `merge --ff-only <full commit id>`, never a reset, force or checkout", [a for a, _ in CALLS if len(a) > 1 and a[1] in ("merge", "reset", "checkout", "pull", "rebase", "clean", "stash")] and all(a[1:4] == ["merge", "--ff-only", "--quiet"] and U.SHA_RE.match(a[4]) for a, _ in CALLS if len(a) > 1 and a[1] in ("merge", "reset", "checkout", "pull", "rebase", "clean", "stash")))
check("git runs with no stdin question possible: GIT_TERMINAL_PROMPT=0 and variables that redirect git (GIT_DIR...) are dropped", U._git_env()["GIT_TERMINAL_PROMPT"] == "0" and not [k for k in (os.environ.update(GIT_DIR="/x", GIT_WORK_TREE="/y", GIT_INDEX_FILE="/z"), U._git_env())[1] if k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")])
for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"): os.environ.pop(k, None)

# -- no automatic restart possible: tells the owner ----------------------------------------------------------------------------------------
s = scenario("manual")
br, up = prepared(s, plan=lambda: (None, "On Windows the bridge cannot replace itself in place."))
with contextlib.redirect_stdout(io.StringIO()): msg = update_msg(up)
check("when a restart is not safe: updated, nothing is restarted, and the page says restart it yourself", msg is None and git(s.inst, "rev-parse", "HEAD") == s.v2 and RESTARTS == [] and up.progress["phase"] == "done" and up.progress["restart"] == "manual" and "Restart the bridge" in up.progress["message"] and "Windows" in up.progress["message"], up.progress)

st = up.status()
check("after an update that still needs a restart, the page is told so: Update now is no longer offered, and another update is refused until the bridge runs the new code", st["restart_pending"] is True and st["can_apply"] is False and "restart the bridge" in st["reason"] and raises(lambda: up.start_apply("v0.2.0")))
U.__version__ = "0.2.0"
check("...and once the new version is the one running, that state is gone", up.status()["restart_pending"] is False and up.status()["update_available"] is False)
U.__version__ = "0.1.0"

# -- requirements changed -----------------------------------------------------------------------------------------------------------------
s = scenario("pipok", req="requests\nsomething-new>=1\n")
br, up = prepared(s)
with contextlib.redirect_stdout(io.StringIO()): msg = update_msg(up)
check("requirements changed: pip runs once, in the project folder, after the update, and the update then completes", msg is None and PIP == [s.inst] and git(s.inst, "rev-parse", "HEAD") == s.v2 and RESTARTS)
check("pip is started with the running interpreter and `-r requirements.txt` as an argument list", REAL["pip"] is not None and "[sys.executable, \"-m\", \"pip\", \"install\", \"--disable-pip-version-check\", \"-r\", \"requirements.txt\"]" in src)
s = scenario("pipfail", req="requests\nsomething-new>=1\n")
br, up = prepared(s)
PIPRES[0] = (False, "ERROR: no matching distribution")
with contextlib.redirect_stdout(io.StringIO()): msg = update_msg(up)
check("pip fails: the checkout is rolled back to the previous commit, nothing restarts, and the message says so", msg is not None and "rolled back" in msg and "no matching distribution" in msg and git(s.inst, "rev-parse", "HEAD") == s.v1 and RESTARTS == [] and open(os.path.join(s.inst, "meshllm", "__init__.py")).read().count("0.1.0") == 1, msg)
check("...the rollback is recorded (and says how to go back), the backup is kept, the tree is clean", up._load(U.SETTING_APPLIED)["result"] == "rolled_back" and len(backups_of(br)) == 1 and git(s.inst, "status", "--porcelain", "--untracked-files=no") == "" and "pip install -r requirements.txt" in msg)
rollbacks = [a for a, _ in CALLS if len(a) > 1 and a[1] == "reset"]
check("the rollback is `reset --keep <full id>` (which refuses to overwrite uncommitted work), never --hard", len(rollbacks) == 1 and rollbacks[0][2] == "--keep" and U.SHA_RE.match(rollbacks[0][3]) and not any("--hard" in a for a, _ in CALLS))
s = scenario("pipfail2", req="requests\nsomething-new>=1\n")
br, up = prepared(s)
PIPRES[0] = (False, "boom")
# the checkout moved on while pip ran: the rollback must refuse to touch it (a human committed something)
real_pip = U._pip_install
def pip_then_commit(root):
    open(os.path.join(s.inst, "mine.txt"), "w").write("mine"); git(s.inst, "add", "mine.txt"); git(s.inst, "commit", "-q", "-m", "mine")
    return False, "boom"
U._pip_install = pip_then_commit
with contextlib.redirect_stdout(io.StringIO()): msg = update_msg(up)
U._pip_install = real_pip
check("if the owner's own commit appeared during the update, the rollback refuses and says how to go back by hand", msg is not None and "Rolling back also failed" in msg and "git reset --keep" in msg and git(s.inst, "log", "-1", "--format=%s") == "mine", msg)

# -- a release that does not start --------------------------------------------------------------------------------------------------------
for what, main in (("crashes on start", "crash"), ("reports another version", "wrong")):
    s = scenario("smoke_" + main, main=main)
    br, up = prepared(s)
    with contextlib.redirect_stdout(io.StringIO()): msg = update_msg(up)
    check(f"a release that {what} is rolled back before any restart", msg is not None and "did not start" in msg and "rolled back" in msg and git(s.inst, "rev-parse", "HEAD") == s.v1 and RESTARTS == [] and up._load(U.SETTING_APPLIED)["result"] == "rolled_back", msg)

# -- refusals ----------------------------------------------------------------------------------------------------------------------------
s = scenario("dirty")
br, up = prepared(s)
open(os.path.join(s.inst, "README.md"), "a").write("my edit\n")
msg = update_msg(up)
check("uncommitted change to a tracked file: refused, naming the file, before anything was fetched, backed up or changed", msg is not None and "README.md" in msg and "uncommitted" in msg and git(s.inst, "rev-parse", "HEAD") == s.v1 and backups_of(br) == [] and git(s.inst, "tag", "-l", "v0.2.0") == "" and open(os.path.join(s.inst, "README.md")).read().endswith("my edit\n"), msg)
check("...and nothing in the record claims an update happened", up.status()["applied"] is None)
git(s.inst, "checkout", "-q", "--", "README.md")
open(os.path.join(s.inst, "README.md"), "a").write("staged edit\n"); git(s.inst, "add", "README.md")
check("a staged change is refused too", "uncommitted" in (update_msg(up) or "") and git(s.inst, "rev-parse", "HEAD") == s.v1)
git(s.inst, "reset", "-q", "--hard")
open(os.path.join(s.inst, "notes_untracked.txt"), "w").write("keep me")
with contextlib.redirect_stdout(io.StringIO()): msg = update_msg(up)
check("an untracked file that does not collide does not stop the update, and is left alone", msg is None and git(s.inst, "rev-parse", "HEAD") == s.v2 and open(os.path.join(s.inst, "notes_untracked.txt")).read() == "keep me")

s = scenario("collide", extra={"added_in_release.txt": "from the release\n"})
br, up = prepared(s)
open(os.path.join(s.inst, "added_in_release.txt"), "w").write("my own untracked file\n")
with contextlib.redirect_stdout(io.StringIO()): msg = update_msg(up)
check("an untracked file that the release also adds: refused before anything is changed or backed up, naming the file, which is untouched", msg is not None and "added_in_release.txt" in msg and "nothing was touched" in msg and git(s.inst, "rev-parse", "HEAD") == s.v1 and open(os.path.join(s.inst, "added_in_release.txt")).read() == "my own untracked file\n" and backups_of(br) == [], msg)

s = scenario("diverged")
br, up = prepared(s)
open(os.path.join(s.inst, "mine.txt"), "w").write("mine"); git(s.inst, "add", "mine.txt"); git(s.inst, "commit", "-q", "-m", "my own commit")
mine = git(s.inst, "rev-parse", "HEAD")
msg = update_msg(up)
check("local commits that the release does not contain: not fast-forwardable, refused, nothing changed, no backup made", msg is not None and "fast-forwarded" in msg and git(s.inst, "rev-parse", "HEAD") == mine and backups_of(br) == [], msg)

s = scenario("detached")
br, up = prepared(s)
git(s.inst, "checkout", "-q", "--detach", "HEAD")
msg = update_msg(up)
check("a detached HEAD is refused (no branch to fast-forward)", msg is not None and "not on a branch" in msg and git(s.inst, "rev-parse", "HEAD") == s.v1 and backups_of(br) == [], msg)

s = scenario("side", v2_version="0.3.0", main_branch=False)
br, up = prepared(s, tag="v0.3.0")
msg = update_msg(up, "v0.3.0")
check("a tag whose commit is not on origin's main is refused (the rule release.yml uses), nothing changed, no backup", msg is not None and "not on this project's main" in msg and git(s.inst, "rev-parse", "HEAD") == s.v1 and backups_of(br) == [] and RESTARTS == [], msg)

s = scenario("mismatch", v2_version="0.3.9", tag="v0.4.0")
br, up = prepared(s, tag="v0.4.0")
msg = update_msg(up, "v0.4.0")
check("a tag whose commit says another version in meshllm/__init__.py is refused", msg is not None and "does not contain version 0.4.0" in msg and git(s.inst, "rev-parse", "HEAD") == s.v1 and backups_of(br) == [], msg)

s = scenario("already")
br, up = prepared(s)
git(s.inst, "pull", "-q", "--ff-only")
msg = update_msg(up)
check("a checkout that already has the release's code (pulled by hand, bridge not restarted) is told to restart, and nothing else happens", msg is not None and "already has" in msg and "Restart" in msg and backups_of(br) == [] and RESTARTS == [], msg)

s = scenario("noorigin")
br, up = prepared(s, origin_ok=False)
msg = update_msg(up)
check("an origin that is not this project's GitHub repository (a fork, a local folder) is refused before anything is fetched", msg is not None and "not this project's GitHub repository" in msg and git(s.inst, "tag", "-l", "v0.2.0") == "" and backups_of(br) == [] and not any("fetch" in a[:4] for a, _ in CALLS), msg)
OA = REAL["origin_allowed"].__func__          # a staticmethod object is only callable itself from Python 3.10 on
repo = U.REPO
yes = [f"https://github.com/{repo}", f"https://github.com/{repo}.git", f"git@github.com:{repo}.git", f"ssh://git@github.com/{repo}", f"https://github.com/{repo.lower()}.git", " " + f"https://github.com/{repo}.git\n"]
no = ["https://github.com/other/repo", f"https://github.com/{repo}.evil.test/x", f"http://github.com/{repo}", f"https://user:token{AT}github.com/{repo}.git", f"https://github.com.evil.test/{repo}", f"https://github.com/{repo}/../x", f"file:///{repo}", "/some/local/folder", "", None, 5, f"ext::sh -c id", f"https://github.com/{repo}x", f"https://evil.test/https://github.com/{repo}"]
check("origin_allowed: only this repository's own GitHub addresses (https, ssh, scp style), nothing that merely contains them", all(OA(u) for u in yes) and not any(OA(u) for u in no), [u for u in no if OA(u)])

s = scenario("backupfail")
br, up = prepared(s)
def broken_backup(kind="manual"): raise OSError("disk full")
br.backups.create = broken_backup
msg = update_msg(up)
check("if the backup cannot be made nothing is changed and the update stops with a clear message", msg is not None and "backup could not be made" in msg and "disk full" in msg and "nothing was changed" in msg and git(s.inst, "rev-parse", "HEAD") == s.v1 and RESTARTS == [], msg)
s = scenario("backupbad")
br, up = prepared(s)
br.backups.verify = lambda name: (_ for _ in ()).throw(backupmod.BackupError("damaged"))
msg = update_msg(up)
check("if the backup does not check out (integrity) it is treated the same", msg is not None and "did not check out" in msg and git(s.inst, "rev-parse", "HEAD") == s.v1, msg)

# -- N3: an IGNORED file of the owner's that the release adds (git itself would overwrite it silently) ------------------------------------------------------
s = scenario("ignored", extra={"local.cfg": "from the release\n"})
br, up = prepared(s)
open(os.path.join(s.inst, ".git", "info", "exclude"), "a").write("local.cfg\n")        # ignored on this machine only
open(os.path.join(s.inst, "local.cfg"), "w").write("my private settings\n")
check("the owner's ignored file is really invisible to git (the situation the check exists for)", git(s.inst, "status", "--porcelain") == "")
msg = update_msg(up)
check("a release that would add a file over an IGNORED file of the owner's is refused before anything is changed, and the file is intact", msg is not None and "local.cfg" in msg and "nothing was touched" in msg and git(s.inst, "rev-parse", "HEAD") == s.v1 and open(os.path.join(s.inst, "local.cfg")).read() == "my private settings\n" and backups_of(br) == [] and RESTARTS == [], msg)
os.rename(os.path.join(s.inst, "local.cfg"), os.path.join(s.inst, "local.cfg.moved"))
with contextlib.redirect_stdout(io.StringIO()): msg = update_msg(up)
check("...and once the file is moved away the update goes through", msg is None and git(s.inst, "rev-parse", "HEAD") == s.v2 and open(os.path.join(s.inst, "local.cfg.moved")).read() == "my private settings\n")

# -- S3: no pip outside a virtual environment ----------------------------------------------------------------------------------------------------
real_prefix = sys.prefix
sys.prefix = sys.base_prefix; outside = REAL["venv"]()
sys.prefix = sys.base_prefix + "-venv"; inside = REAL["venv"]()
sys.prefix = real_prefix
check("in_virtualenv: false when sys.prefix is sys.base_prefix, true when they differ", outside is False and inside is True)
s = scenario("novenv", req="requests\nsomething-new>=1\n")
br, up = prepared(s)
U.in_virtualenv = lambda: False
st = up.status()
msg = update_msg(up)
check("requirements changed and this Python is not a virtual environment: the update stops BEFORE the backup and the merge, with the plain message, and nothing ran", msg is not None and msg.startswith(U.NO_VENV_MESSAGE) and "pip install -r requirements.txt" in msg and "./setup.sh" in msg
      and git(s.inst, "rev-parse", "HEAD") == s.v1 and backups_of(br) == [] and PIP == [] and RESTARTS == [] and not [a for a, _ in CALLS if "merge" in a[:2]], msg)
check("...and the status tells the page (for the confirmation dialog)", st["virtualenv"] is False and U.NO_VENV_MESSAGE.startswith("This Python is not a virtual environment, so the update will not install packages for you"))
s = scenario("novenv_ok")
br, up = prepared(s)
with contextlib.redirect_stdout(io.StringIO()): msg = update_msg(up)
check("outside a virtual environment an update that needs no new packages still works", msg is None and git(s.inst, "rev-parse", "HEAD") == s.v2 and PIP == [])
U.in_virtualenv = lambda: True

# -- N2: a failure that cannot be rolled back --------------------------------------------------------------------------------------------------
s = scenario("stuck", main="dirty_crash", extra={"README.md": "readme, second edition\n"})
br, up = prepared(s)
with contextlib.redirect_stdout(io.StringIO()): msg = update_msg(up)
st = up.status()
check("a new release that modifies a tracked file and then crashes cannot be rolled back (reset --keep refuses): the message says why and what to do", msg is not None and "Rolling back also failed" in msg and "README.md" in msg and f"git reset --keep {s.v1}" in msg and "stash or discard the changes to README.md" in msg, msg)
check("...nothing restarts, HEAD is left on the new commit and the owner's change is untouched", RESTARTS == [] and git(s.inst, "rev-parse", "HEAD") == s.v2 and "a local change made while starting" in open(os.path.join(s.inst, "README.md")).read())
check("...and the status says the code on disk is not the running code: no Update now, no restart, with the hint", st["stuck"] is not None and st["can_apply"] is False and st["can_update"] is False and st["restart_possible"] is False and "not the code that is running" in st["reason"] and f"git reset --keep {s.v1}" in st["reason"] and "README.md" in st["restart_note"], st)
check("...another update is refused until that is fixed, and the marker is per process (a restart starts clean)", raises(lambda: up.start_apply("v0.2.0")) and U.Updater(br, root=s.inst).stuck is None)

# -- N6: a restore set aside while the update runs must not be applied by its restart -----------------------------------------------------------------
s = scenario("stagedrestart")
br, up = prepared(s)
br.backups.stage_file(br.backups.path_of(br.backups.create("manual")))
with contextlib.redirect_stdout(io.StringIO()): msg = update_msg(up)
check("a database restore waiting at the moment of the restart: the code is updated but the restart is refused (it would apply the restore), and the page says so", msg is None and git(s.inst, "rev-parse", "HEAD") == s.v2 and RESTARTS == [] and up.progress["phase"] == "done" and up.progress["restart"] == "manual" and "restore" in up.progress["message"], up.progress)
br.backups.cancel_staged()

# -- shallow clones --------------------------------------------------------------------------------------------------------------------------------
s = scenario("shallow", shallow=True)
br, up = prepared(s)
check("(setup) the checkout under test really is a shallow clone", git(s.inst, "rev-parse", "--is-shallow-repository") == "true")
with contextlib.redirect_stdout(io.StringIO()): msg = update_msg(up)
check("a shallow clone (--depth 1) updates normally: tag and main fetched, ancestry checks pass, fast-forward, restart requested", msg is None and git(s.inst, "rev-parse", "HEAD") == s.v2 and RESTARTS and git(s.inst, "rev-parse", "--is-shallow-repository") == "true", msg)

# -- N5: a timeout kills the child's whole process group -----------------------------------------------------------------------------------------------
if os.name == "posix":
    child = os.path.join(HERE, "slow_child.py"); pidfile = os.path.join(HERE, "grandchild.pid")
    open(child, "w").write("import os, subprocess, sys, time\n"
                           "g = subprocess.Popen([sys.executable, '-c', 'import os, sys, time; open(sys.argv[1], \"w\").write(str(os.getpid())); time.sleep(300)', sys.argv[1]])\n"
                           "while not os.path.exists(sys.argv[1]): time.sleep(0.05)\n"
                           "time.sleep(300)\n")
    def alive(pid):
        try:
            with open(f"/proc/{pid}/stat") as f:
                return f.read().rsplit(")", 1)[1].split()[0] != "Z"
        except OSError:
            return False
    t0 = time.time()
    try:
        REAL["run"]([sys.executable, child, pidfile], HERE, 3); expired = False
    except subprocess.TimeoutExpired:
        expired = True
    grand = int(open(pidfile).read())
    for _ in range(40):
        if not alive(grand): break
        time.sleep(0.1)
    check("a command that runs past its time limit is stopped together with the helper it started (the whole process group), and the wait returns promptly", expired and not alive(grand) and time.time() - t0 < 20, (expired, grand, alive(grand)))
    if alive(grand): os.kill(grand, 9)
else:
    print("SKIP process-group kill (not POSIX)")

# -- injection attempts and the entry point -------------------------------------------------------------------------------------------------------
s = scenario("inject")
br, up = prepared(s)
ran = []
U._run = lambda argv, cwd, timeout, env=None: ran.append(argv)
for t in bad:
    up._save_state(latest={"tag": t if isinstance(t, str) else "v0.2.0", "url": "", "notes": ""})
    check(f"start_apply refuses {t!r} and starts nothing", raises(lambda t=t: up.start_apply(t)) and not ran)
g = U.Git(s.inst, exe="git")
leaks = []
for t in bad:
    for fn in (g.fetch_tag, g.tag_commit):
        if not raises(lambda fn=fn, t=t: fn(t)): leaks.append((fn.__name__, t))
for x in ("--upload-pack=x", "HEAD", "main", "abc", "A" * 40, "a" * 40 + "\n", "$(id)", "a" * 40 + ";id", None):
    for fn in (g.version_at, g.fast_forward, g.roll_back):
        if not raises(lambda fn=fn, x=x: fn(x)): leaks.append((fn.__name__, x))
    if not raises(lambda x=x: g.is_ancestor(x, "a" * 40)): leaks.append(("is_ancestor", x))
    if not raises(lambda x=x: g.changed(x, "a" * 40, "requirements.txt")): leaks.append(("changed", x))
check("the Git helpers refuse every hostile tag and commit string (fetch_tag, tag_commit, version_at, fast_forward, roll_back, is_ancestor, changed) without starting any program", not leaks and not ran, (leaks, ran))
U._run = spy_run
check("an attacker-chosen tag in a saved release cannot reach git either: a tampered cache with a hostile tag offers no update", (br.audit.set_setting(U.SETTING_STATE, json.dumps({"latest": {"tag": "--upload-pack=x", "url": "", "notes": ""}})), up.available() is None and raises(lambda: up.start_apply("--upload-pack=x")))[1])

s = scenario("entry")
br, up = prepared(s)
check("start_apply: a tag other than the one this server saw is refused (a client cannot choose another version)", raises(lambda: up.start_apply("v0.9.9")) and raises(lambda: up.start_apply("v0.1.0")) and raises(lambda: up.start_apply("v1.0.0")))
up._save_state(latest={"tag": "v0.1.0", "url": "", "notes": ""})
check("...a saved release that is not newer than this version is not offered or applied", up.available() is None and raises(lambda: up.start_apply("v0.1.0")))
up._save_state(latest=None)
check("...no saved release at all: nothing to apply", raises(lambda: up.start_apply("v0.2.0")))
up._save_state(latest={"tag": "v0.2.0", "url": "", "notes": ""})
for bad_install, why in (("docker", "Docker"), ("packaged", "downloadable program"), ("none", "not a git checkout")):
    u2 = U.Updater(br, root=s.inst if bad_install != "none" else plain)
    saved = (U.in_container, paths.is_frozen)
    if bad_install == "docker": U.in_container = lambda environ=None, dockerenv="/.dockerenv": True
    if bad_install == "packaged": paths.is_frozen = lambda: True
    try:
        m = None
        try: u2.start_apply("v0.2.0")
        except UpdateError as e: m = str(e)
        st = u2.status()
    finally:
        U.in_container, paths.is_frozen = saved
    check(f"start_apply on a {bad_install} install is refused ({why}) and nothing was started; the status says so", m is not None and why in m and st["can_apply"] is False and st["can_update"] is False and st["update_available"] is True and git(s.inst, "rev-parse", "HEAD") == s.v1)
br.backups.stage_file(br.backups.path_of(br.backups.create("manual")))
check("a database restore waiting for the next start blocks the update (it would be applied by the restart)", raises(lambda: up.start_apply("v0.2.0")))
br.backups.cancel_staged()

# single flight and the background thread
s = scenario("flight")
br, up = prepared(s)
gate = threading.Event(); entered = threading.Event()
def slow_apply(tag): entered.set(); gate.wait(10)
up.apply = slow_apply
with contextlib.redirect_stdout(io.StringIO()):
    first = up.start_apply("v0.2.0", who="admin")
    entered.wait(5)
    second_busy = raises(lambda: up.start_apply("v0.2.0"), UpdateBusy)
    status_running = up.status()["progress"]["phase"]
    gate.set()
    for _ in range(100):
        if not up._apply_lock.locked(): break
        time.sleep(0.05)
check("single flight: one update runs, a second request is refused as busy, and the lock is released when it ends", first == {"started": True, "tag": "v0.2.0"} and second_busy and status_running == "updating" and not up._apply_lock.locked())
def crashing_apply(tag): raise RuntimeError("bug")
up.apply = crashing_apply
with contextlib.redirect_stdout(io.StringIO()):
    up.start_apply("v0.2.0")
    for _ in range(100):
        if not up._apply_lock.locked(): break
        time.sleep(0.05)
check("a bug inside the update ends in a failed progress record, never a hung page, and the lock is released", up.progress["phase"] == "failed" and up.progress["ok"] is False and "RuntimeError" in up.progress["message"] and not up._apply_lock.locked())
up.apply = U.Updater.apply.__get__(up)
with contextlib.redirect_stdout(io.StringIO()):
    up.start_apply("v0.2.0")
    for _ in range(300):
        if not up._apply_lock.locked(): break
        time.sleep(0.05)
check("through the background thread the whole update works the same (done, restart requested, record written)", git(s.inst, "rev-parse", "HEAD") == s.v2 and RESTARTS and up.progress["phase"] == "restarting", up.progress)

# Bridge.request_restart stops the bridge and keeps the command for main()
b2 = new_bridge()
b2.request_restart(["/fake/python", "-m", "meshllm"])
check("Bridge.request_restart keeps the command for main() and asks the connect loop to finish (the normal stop path)", b2.restart_command == ["/fake/python", "-m", "meshllm"] and b2._stopping is True)
mainsrc = open(os.path.join(ROOT, "meshllm", "bridge.py"), encoding="utf-8").read()
check("main() calls finish_restart only after Bridge.run() has returned", mainsrc.index("bridge.run()") < mainsrc.index("finish_restart(bridge)") and "execv" not in mainsrc)

U.restart_plan = REAL["plan"]
U.Updater.origin_allowed = REAL["origin_allowed"]
U._pip_install = REAL["pip"]


# ======================================================================================================================
# the database upgrades itself when newer code opens an older one (and never downgrades)
# ======================================================================================================================
OLD_SCHEMA = """
CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, node_id TEXT NOT NULL, node_name TEXT, prompt TEXT, response TEXT, status TEXT NOT NULL,
    model TEXT, llm_ms INTEGER, chunks INTEGER DEFAULT 0, delivered INTEGER DEFAULT 0, relayed INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, rx_snr REAL, rx_rssi INTEGER, hops INTEGER);
CREATE TABLE memory_reset (node_id TEXT PRIMARY KEY, ts REAL NOT NULL);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE node_access (node_id TEXT PRIMARY KEY, access TEXT NOT NULL DEFAULT 'default', daily_cap INTEGER);
"""
tmp = tempfile.mkdtemp(prefix="meshold_", dir=HERE)
old = sqlite3.connect(os.path.join(tmp, "t.db"))
old.executescript(OLD_SCHEMA)
old.execute("INSERT INTO requests (ts, node_id, node_name, prompt, response, status) VALUES (1.0, '!0000aaaa', 'Lodge', 'old question', 'old answer', 'answered')")
old.execute("INSERT INTO settings VALUES ('dist_unit', 'mi')"); old.execute("INSERT INTO node_access VALUES ('!0000aaaa', 'allow', 5)")
old.commit(); old.close()
brn, _, _ = make(tmp=tmp)
cols = lambda t: {r[1] for r in brn.audit.db.execute(f"PRAGMA table_info({t})")}
check("an older database opened by the newer code gains the columns it lacks (kind, action, auth, delivery_note, pinned_key, max_tier)", {"kind", "action", "auth", "delivery_note"} <= cols("requests") and {"pinned_key", "max_tier"} <= cols("node_access"), (cols("requests"), cols("node_access")))
row = brn.audit.list(limit=5)[0]
check("...and keeps every row and setting, with the new columns defaulting sensibly", row["prompt"] == "old question" and row["kind"] == "ai" and row["response"] == "old answer" and brn.audit.get_setting("dist_unit") == "mi" and brn.audit.get_node("!0000aaaa")["access"] == "allow" and brn.audit.get_node("!0000aaaa")["daily_cap"] == 5)
tables = {r[0] for r in brn.audit.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
check("...and the other services created their own tables (nodes, telemetry, traceroutes, channel, walk tests...)", {"mesh_nodes", "telemetry", "traceroutes", "channel_messages", "walk_sessions", "node_notes", "snippets"} <= tables, tables)
new_id = brn.audit.new_request("!0000aaaa", "Lodge", "a new question", status="answered", response="fine", kind="ai", action="x", auth="PKI")
check("...so the newer code can write rows with the new columns into it", brn.audit.list(limit=1)[0]["id"] == new_id)
brn.audit.db.close()
# a second open is harmless, and unknown extras (from a still newer version) are left alone: nothing here ever drops or downgrades
con = sqlite3.connect(os.path.join(tmp, "t.db")); con.execute("ALTER TABLE requests ADD COLUMN from_the_future TEXT"); con.execute("CREATE TABLE from_the_future_table (x)"); con.execute("INSERT INTO from_the_future_table VALUES (1)"); con.commit(); con.close()
again = auditmod.Audit(os.path.join(tmp, "t.db"))
check("opening it again changes nothing, and columns or tables this version does not know are never dropped (no downgrade path exists)", {"from_the_future"} <= {r[1] for r in again.db.execute("PRAGMA table_info(requests)")} and again.db.execute("SELECT COUNT(*) FROM from_the_future_table").fetchone()[0] == 1 and len(again.list(limit=10)) == 2)
audit_src = open(os.path.join(ROOT, "meshllm", "audit.py")).read().upper()
check("audit.py contains no DROP TABLE or DROP COLUMN, so nothing it does can remove what an upgrade added", "DROP TABLE" not in audit_src and "DROP COLUMN" not in audit_src)


# ======================================================================================================================
# the routes: roles, CSRF, Origin, demo, the wrong tag
# ======================================================================================================================
ADMIN_PW, VIEWER_PW = "correct horse battery", "viewer staple lantern"
def hash_file(name, pw):
    path = os.path.join(HERE, name)
    passwords.write_hash_file(path, passwords.hash_password(pw))
    return path


def serve(security=None, **over):
    b = new_bridge(**over)
    b.args.web_host, b.args.web_port = "127.0.0.1", 0
    if security is not None:
        b.web_security = security
    srv = webui.start(b)
    time.sleep(0.1)
    return b, f"http://127.0.0.1:{srv.server_address[1]}"


def call(method, base, path, tok=None, csrf=None, origin=True, body=None, ctype="application/json"):
    h = {}
    if method == "POST":
        h["Content-Type"] = ctype
        if origin: h["Origin"] = base if origin is True else origin
    if csrf: h["X-CSRF-Token"] = csrf
    kw = dict(headers=h, timeout=15, allow_redirects=False)
    if tok: kw["cookies"] = {ws.WebSecurity.cookie_name(False): tok}
    if method == "POST": kw["data"] = json.dumps({} if body is None else body)
    return rq.request(method, base + path, **kw)


def login(base, account, pw):
    r = call("POST", base, "/api/login", body={"account": account, "password": pw})
    return r.cookies.get(ws.COOKIE), r.json().get("csrf")


sec = ws.WebSecurity(admin_file=hash_file("admin.hash", ADMIN_PW), viewer_file=hash_file("viewer.hash", VIEWER_PW), allowed_hosts=[])
with contextlib.redirect_stdout(io.StringIO()):
    brw, base = serve(sec)
    tok_a, csrf_a = login(base, "admin", ADMIN_PW)
    tok_v, csrf_v = login(base, "viewer", VIEWER_PW)
applied = []
brw.updater._apply_thread = lambda tag: applied.append(tag)        # the route tests never run an update
serve_reply(status=200, body=release(), headers={"ETag": '"r1"'})
GH.seen.clear()
ROUTES = [("GET", "/api/update", None), ("POST", "/api/update/check", {}), ("POST", "/api/update/setting", {"enabled": True}), ("POST", "/api/update/apply", {"tag": "v0.2.0"})]
check("anonymous: every update route is 401, and nothing was sent to GitHub", all(call(m, base, p, body=b).status_code == 401 for m, p, b in ROUTES) and not GH.seen)
check("a viewer is refused on every update route (403), even with a valid CSRF token", all(call(m, base, p, tok=tok_v, csrf=csrf_v, body=b).status_code == 403 for m, p, b in ROUTES) and not GH.seen and brw.updater.enabled() is False and not applied)
vs = call("GET", base, "/api/status", tok=tok_v).json()
check("a viewer sees nothing about updates anywhere: not in /api/status, not on the session probe (stricter than 'an update is available')", not [k for k in vs if "updat" in k.lower() or "latest" in k.lower()] and "update" not in json.dumps(call("GET", base, "/api/session", tok=tok_v).json()).lower())
check("the admin can read it: version, install kind, nothing newer yet, the check is off", call("GET", base, "/api/update", tok=tok_a).json()["version"] == "0.1.0" and call("GET", base, "/api/update", tok=tok_a).json()["check_enabled"] is False)
check("POST without the session's CSRF token is 403 on every update route (nothing changed, nothing sent)", all(call("POST", base, p, tok=tok_a, body=b).status_code == 403 for m, p, b in ROUTES if m == "POST") and brw.updater.enabled() is False and not GH.seen and not applied)
check("a wrong CSRF token is 403", all(call("POST", base, p, tok=tok_a, csrf="0" * 64, body=b).status_code == 403 for m, p, b in ROUTES if m == "POST"))
check("a foreign or missing Origin is 403 on every update route (a web page elsewhere cannot trigger a check or an update)", all(call("POST", base, p, tok=tok_a, csrf=csrf_a, origin=o, body=b).status_code == 403 for m, p, b in ROUTES if m == "POST" for o in ("http://evil.test", False, "null", base.replace("http://", "https://"))) and not GH.seen and not applied)
check("a form-style content type is 415 (a plain HTML form cannot send these)", all(call("POST", base, p, tok=tok_a, csrf=csrf_a, ctype="text/plain", body=b).status_code == 415 for m, p, b in ROUTES if m == "POST"))
r = call("POST", base, "/api/update/setting", tok=tok_a, csrf=csrf_a, body={"enabled": "yes"})
check("the setting route takes a real boolean only", r.status_code == 400 and brw.updater.enabled() is False and all(call("POST", base, "/api/update/setting", tok=tok_a, csrf=csrf_a, body=b).status_code == 400 for b in ({}, {"enabled": 1}, {"enabled": None}, {"enabled": "true"})))
r = call("POST", base, "/api/update/setting", tok=tok_a, csrf=csrf_a, body={"enabled": True})
check("...and with a real boolean it turns the check on, without checking right now", r.status_code == 200 and r.json() == {"check_enabled": True} and brw.updater.enabled() and not GH.seen)
r = call("POST", base, "/api/update/check", tok=tok_a, csrf=csrf_a)
j = r.json()
check("Check now: one GET to the (fake) GitHub, then the status with the outcome and the release", r.status_code == 200 and len(GH.seen) == 1 and j["result"]["ok"] and j["latest"]["tag"] == "v0.2.0" and j["update_available"] and j["version"] == "0.1.0", r.text[:300])
check("...and a viewer still cannot read the result", call("GET", base, "/api/update", tok=tok_v).status_code == 403)
for t in ("v9.9.9", "v0.1.0", "v0.2.0; rm -rf /", "--upload-pack=x", "", None, 5, ["v0.2.0"], "V0.2.0", "v0.2.0\n"):
    r = call("POST", base, "/api/update/apply", tok=tok_a, csrf=csrf_a, body={"tag": t})
    check(f"apply with the wrong or hostile tag {t!r} is refused (400) and no update starts", r.status_code == 400 and not applied, (r.status_code, r.text[:100]))
r = call("POST", base, "/api/update/apply", tok=tok_a, csrf=csrf_a, body={})
check("apply with no tag is refused", r.status_code == 400 and not applied)
brw.updater.root = plain
r = call("POST", base, "/api/update/apply", tok=tok_a, csrf=csrf_a, body={"tag": "v0.2.0"})
check("apply with the right tag on an install that cannot update itself is refused with the reason", r.status_code == 400 and "not a git checkout" in r.json()["error"] and not applied)
s = scenario("route")
brw.updater.root = s.inst
U.Updater.origin_allowed = staticmethod(lambda url, o=s.origin: url.strip() == o)
r = call("POST", base, "/api/update/apply", tok=tok_a, csrf=csrf_a, body={"tag": "v0.2.0"})
check("apply with the exact tag the server saw, on a git checkout, starts the update and answers at once (the work is on a background thread)", r.status_code == 200 and r.json() == {"started": True, "tag": "v0.2.0"} and applied == ["v0.2.0"], (r.status_code, r.text[:100]))
r = call("POST", base, "/api/update/apply", tok=tok_a, csrf=csrf_a, body={"tag": "v0.2.0"})
check("a second apply while one is running is 409 (single flight over HTTP)", r.status_code == 409 and applied == ["v0.2.0"], r.text[:100])
check("the status shows the running update, the install kind and no secret", call("GET", base, "/api/update", tok=tok_a).json()["progress"]["phase"] == "updating" and call("GET", base, "/api/update", tok=tok_a).json()["install"]["kind"] == "git")
U.Updater.origin_allowed = REAL["origin_allowed"]

# legacy mode (loopback, no login): everyone on this computer is the owner, Origin is still checked when sent
with contextlib.redirect_stdout(io.StringIO()):
    brl, basel = serve()
brl.updater._apply_thread = lambda tag: applied.append(tag)
serve_reply(status=200, body=release())
check("no-login mode: the update routes work for the owner on this computer, and a foreign Origin is still refused", rq.get(basel + "/api/update", timeout=10).status_code == 200 and call("POST", basel, "/api/update/check").status_code == 200 and call("POST", basel, "/api/update/check", origin="http://evil.test").status_code == 403)
# demo mode through the routes
with contextlib.redirect_stdout(io.StringIO()):
    brd, based = serve(demo=True)
GH.seen.clear()
rd = {p: call(m, based, p, body=b) for m, p, b in ROUTES}
check("demo mode over HTTP: check and apply are refused (400), the setting still saves, the status says why, and nothing was sent to GitHub", rd["/api/update/check"].status_code == 400 and "Demo" in rd["/api/update/check"].json()["error"] and rd["/api/update/apply"].status_code == 400
      and rd["/api/update"].json()["demo"] is True and rd["/api/update"].json()["can_update"] is False and not GH.seen)
U.MIN_GAP_S = 0
serve_reply(status=200, body=release(body="<script>alert(1)</script>"))
call("POST", base, "/api/update/check", tok=tok_a, csrf=csrf_a)
check("the release notes come back as plain JSON text with the markup intact (the page shows them with textContent only)", call("GET", base, "/api/update", tok=tok_a).json()["latest"]["notes"] == "<script>alert(1)</script>")

check("this file never started a program in the real project folder and never contacted another host", all(os.path.realpath(c) != os.path.realpath(ROOT) for _, c in CALLS))
check.done()
sys.exit(1 if check.fails else 0)
