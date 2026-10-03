"""Who may talk to the dashboard: Host allowlist, Origin and CSRF checks, the login, sessions, throttling, roles and TLS.

webui.py asks this module about every request; webroutes.py tags every route with a role. Nothing here touches the radio.

The model, in one paragraph. With no password configured on a loopback bind nothing is checked beyond the Host header (as before this
module existed): everyone who can reach the port is the owner. With a password (`--password-hash-file`) every request needs a session,
except the few `public` routes (the login itself, the session probe, the login page's own files). A session belongs to one of two
accounts: `admin` (all routes) or `viewer` (the routes tagged `viewer`; an admin route is a 403). Routes with no tag are `admin`
(default-deny). A viewer can read the dashboard and ask the AI a log-only question; it cannot transmit, change the radio or the settings,
or download backups. CSV exports, logs, diagnostics and reports (they hold message text) are closed to viewers unless the owner passes
`--viewer-exports`. See SECURITY.md for the threat model and what this does not protect against.
"""
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import ssl
import sys
import threading
import time
from urllib.parse import urlsplit

from meshllm import passwords

PUBLIC, VIEWER, ADMIN = "public", "viewer", "admin"
ROLES = (PUBLIC, VIEWER, ADMIN)                       # the tags a route can carry
RANK = {PUBLIC: 0, VIEWER: 1, ADMIN: 2}
ACCOUNTS = ("admin", "viewer")                        # the two logins
COOKIE = "meshllm_session"
DEVICE_COOKIE = "meshllm_device"                     # set when the ADMIN signs in; lets that browser past the global sign-in wait (never past its own)
DEVICE_DAYS = 30
HOST_PREFIX = "__Host-"                              # over TLS the cookies carry this prefix: the browser then refuses one set by a sibling domain
CSRF_HEADER = "X-CSRF-Token"
LOOPBACK_NAMES = frozenset(("localhost", "127.0.0.1", "::1"))
WILDCARD_HOSTS = ("0.0.0.0", "::", "")                # bind addresses meaning "every interface"
# the browser-visible files of the login page; everything else under static/ needs a session when a password is set
PUBLIC_STATIC = frozenset(("/login.js", "/style.css"))
MAX_LOGIN_BODY = 4096
IDLE_MINUTES, ABSOLUTE_HOURS = 120, 12
MAX_SESSIONS = 64                                    # live sessions PER ACCOUNT: one account filling its slots can never push out the other's


# ---- host names, addresses, Origin ----------------------------------------------------------------------------------------------
_NAME_OK = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$")


def _canon_ip(text):
    """The canonical text of an IP literal (IPv4-mapped IPv6 becomes plain IPv4, IPv6 is compressed), or None if it is not one."""
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return str(ip)


def normalise_host(name):
    """The canonical form of a host name or IP literal (lower case, no brackets, no trailing dot), or None if it is neither."""
    if not isinstance(name, str):
        return None
    n = name.strip().lower()
    if n.startswith("[") and n.endswith("]"):
        n = n[1:-1]
    if not n or len(n) > 253 or not n.isascii():
        return None
    ip = _canon_ip(n)
    if ip:
        return ip
    n = n.rstrip(".")
    return n if _NAME_OK.match(n) else None


def parse_host_header(value):
    """(host, port or None) from a Host header, or None if it is malformed. Understands `name`, `name:port`, `1.2.3.4:port`,
    `[::1]` and `[::1]:port`; an unbracketed IPv6 address, a userinfo, a path or anything with whitespace is refused."""
    if not isinstance(value, str) or not value or len(value) > 300 or not value.isascii():
        return None
    if any(c in value for c in " \t\r\n\0/\\@?#%"):
        return None
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return None
        inner, rest = value[1:end], value[end + 1:]
        if ":" not in inner or _canon_ip(inner) is None:
            return None
    else:
        if value.count(":") > 1:
            return None
        inner, colon, port_text = value.partition(":")
        rest = colon + port_text
    host = normalise_host(inner)
    if host is None:
        return None
    if not rest:
        return host, None
    if not (rest.startswith(":") and rest[1:].isdigit() and 1 <= int(rest[1:]) <= 65535):
        return None
    return host, int(rest[1:])


def bind_is_loopback(host):
    """True if a --web-host value only reaches this computer ('localhost', 127.0.0.0/8, ::1). Wildcards and any other name are not."""
    if host == "localhost":
        return True
    ip = _canon_ip((host or "").strip("[]"))
    return bool(ip) and ipaddress.ip_address(ip).is_loopback


def is_loopback_peer(address):
    """True if a client address (text from the socket) is this computer."""
    ip = _canon_ip((address or "").split("%")[0])
    return bool(ip) and ipaddress.ip_address(ip).is_loopback


def source_key(address):
    """The throttle bucket of a client: its IPv4 address, or its IPv6 /64 (one household owns a whole /64)."""
    ip = _canon_ip((address or "").split("%")[0])
    if ip is None:
        return "?"
    parsed = ipaddress.ip_address(ip)
    if parsed.version == 6:
        return str(ipaddress.ip_network((parsed, 64), strict=False))
    return ip


def parse_origin(origin):
    """(scheme, host, port) of an Origin header with the default port filled in, or None if it is not a plain http(s) origin."""
    if not isinstance(origin, str) or not origin.isascii():
        return None
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or parts.path or parts.query or parts.fragment or "@" in parts.netloc or not parts.netloc:
        return None
    parsed = parse_host_header(parts.netloc)
    if parsed is None:
        return None
    return parts.scheme, parsed[0], parsed[1] or (443 if parts.scheme == "https" else 80)


# ---- the pieces: throttle, sessions ---------------------------------------------------------------------------------------------
class Throttle:
    """Exponential backoff on failures, per key. After `free` failures each further one makes the next attempt wait base * 2**(n-1)
    seconds, never more than `cap`: slows guessing to a crawl but is never a lockout (the wait ends by itself). A quiet `decay` period
    forgets a key. At most `max_keys` keys are remembered (the oldest is dropped), so a flood of addresses cannot use up memory."""

    def __init__(self, free, base, cap, decay, max_keys=2000, clock=time.monotonic):
        self.free, self.base, self.cap, self.decay, self.max_keys, self.clock = free, base, cap, decay, max_keys, clock
        self.state = {}                               # key -> [failures, blocked until, time of last failure]
        self.lock = threading.Lock()

    def wait(self, key):
        """Seconds this key must still wait (0.0 if it may try now)."""
        with self.lock:
            s = self.state.get(key)
            if s is None:
                return 0.0
            now = self.clock()
            if now - s[2] > self.decay:
                del self.state[key]
                return 0.0
            return max(0.0, s[1] - now)

    def fail(self, key):
        """Record a failed attempt."""
        with self.lock:
            now = self.clock()
            s = self.state.get(key)
            if s is not None and now - s[2] > self.decay:
                s = None
            fails = (s[0] if s else 0) + 1
            over = fails - self.free
            delay = 0.0 if over <= 0 else min(self.cap, self.base * 2 ** min(over - 1, 40))
            self.state[key] = [fails, now + delay, now]
            if len(self.state) > self.max_keys:
                del self.state[min(self.state, key=lambda k: self.state[k][2])]

    def reset(self, key):
        """Forget a key (after a successful login)."""
        with self.lock:
            self.state.pop(key, None)


class Session:
    """One signed-in browser: the hash of its token (the token itself is never kept), its account, times and password generation."""

    def __init__(self, sid, role, generation, now):
        self.sid, self.role, self.generation, self.created, self.last_seen = sid, role, generation, now, now


class SessionStore:
    """In-memory sessions (a restart signs everyone out). Tokens are 256 random bits from `secrets`; only their SHA-256 is stored.
    A session ends after `idle` seconds without a request or `absolute` seconds from login, whichever comes first."""

    def __init__(self, idle, absolute, max_sessions=MAX_SESSIONS, clock=time.monotonic):
        self.idle, self.absolute, self.max_sessions, self.clock = idle, absolute, max_sessions, clock
        self.items = {}
        self.lock = threading.Lock()

    @staticmethod
    def _sid(token):
        return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()

    def create(self, role, generation):
        """Start a session; returns (the new token to give the browser once, the Session)."""
        token = secrets.token_urlsafe(32)
        with self.lock:
            now = self.clock()
            self._expire(now)
            mine = [k for k, v in self.items.items() if v.role == role]
            while len(mine) >= self.max_sessions:       # evict only this account's oldest: a viewer cannot sign the admin out
                oldest = min(mine, key=lambda k: self.items[k].last_seen)
                del self.items[oldest]
                mine.remove(oldest)
            s = Session(self._sid(token), role, generation, now)
            self.items[s.sid] = s
        return token, s

    def _expire(self, now):
        for sid in [k for k, s in self.items.items() if now - s.last_seen > self.idle or now - s.created > self.absolute]:
            del self.items[sid]

    def get(self, token):
        """The live Session for a token (and note that it was just used), or None."""
        if not token or len(token) > 200:
            return None
        sid = self._sid(token)
        with self.lock:
            s = self.items.get(sid)
            if s is None:
                return None
            now = self.clock()
            if now - s.last_seen > self.idle or now - s.created > self.absolute:
                del self.items[sid]
                return None
            s.last_seen = now
            return s

    def destroy(self, token_or_session):
        """End one session (by token or Session)."""
        sid = token_or_session.sid if isinstance(token_or_session, Session) else self._sid(token_or_session or "")
        with self.lock:
            self.items.pop(sid, None)

    def destroy_role(self, role):
        """End every session of one account (its password changed)."""
        with self.lock:
            for sid in [k for k, s in self.items.items() if s.role == role]:
                del self.items[sid]


class Credentials:
    """The password hashes of the two accounts, re-read when a file changes so `--set-password` on a running bridge takes effect
    (each hash has a *generation*; a session from an older generation is refused). A file that disappears or stops being valid means
    that account cannot sign in and its sessions end: the login never falls back to open."""

    def __init__(self, files, clock=time.monotonic, recheck=1.0):
        self.files, self.clock, self.recheck = dict(files), clock, recheck
        self.cache = {}                               # role -> (file signature, (hash line, generation) or None)
        self.checked = {}
        self.lock = threading.Lock()

    def get(self, role):
        """(hash line, generation) for an account, or None if it has no usable password."""
        path = self.files.get(role)
        if not path:
            return None
        with self.lock:
            now = self.clock()
            if role in self.cache and now - self.checked.get(role, -1e9) < self.recheck:
                return self.cache[role][1]
            self.checked[role] = now
            try:
                st = os.stat(path)
                sig = (st.st_mtime_ns, st.st_size, st.st_ino)
            except OSError:
                self.cache[role] = (None, None)
                return None
            if role in self.cache and self.cache[role][0] == sig:
                return self.cache[role][1]
            try:
                line = passwords.read_hash_file(path)
                value = (line, hashlib.sha256(line.encode("ascii")).hexdigest())
            except passwords.PasswordError:
                value = None
            self.cache[role] = (sig, value)
            return value


class Req:
    """What the server knows about one request once it has looked at the connection and the headers."""
    peer = client = host = token = session = None
    secure = False
    known_device = False          # carries the cookie the admin's sign-in handed out

    @property
    def role(self):
        """The account of the session, or None."""
        return self.session.role if self.session else None


# ---- the whole thing ------------------------------------------------------------------------------------------------------------
class WebSecurity:
    """Every access decision for one dashboard server, built from the command-line arguments (see `from_args`)."""

    def __init__(self, admin_file=None, viewer_file=None, allowed_hosts=(), trusted_proxies=(), tls=False, viewer_exports=False,
                 idle_minutes=IDLE_MINUTES, absolute_hours=ABSOLUTE_HOURS, clock=time.monotonic):
        self.creds = Credentials({"admin": admin_file, "viewer": viewer_file}, clock=clock)
        self.auth_required = bool(admin_file)         # decided once, at start: a vanished hash file locks the dashboard, it does not open it
        self.allowed = set(LOOPBACK_NAMES)
        self.allowed.update(h for h in (normalise_host(a) for a in allowed_hosts) if h)
        self.proxies = [ipaddress.ip_network(p, strict=False) for p in trusted_proxies]
        self.tls, self.viewer_exports = bool(tls), bool(viewer_exports)
        self.sessions = SessionStore(idle_minutes * 60, absolute_hours * 3600, clock=clock)
        self.per_source = Throttle(free=0, base=1.0, cap=300.0, decay=3600.0, clock=clock)
        self.global_ = Throttle(free=10, base=1.0, cap=60.0, decay=3600.0, clock=clock)
        self.slots = threading.BoundedSemaphore(2)    # at most two password hashes (32 MiB each) at once
        self._dummy = passwords.dummy_hash() if self.auth_required else None    # verified against when the account does not exist, so timing tells nothing
        self._csrf_key = secrets.token_bytes(32)
        self._device_key = secrets.token_bytes(32)    # per run: after a restart a browser simply needs one more successful sign-in

    @classmethod
    def from_args(cls, args):
        """Build from parsed command-line arguments (missing attributes mean the default, so older test namespaces still work).
        Demo mode never has a login."""
        demo = bool(getattr(args, "demo", False))
        allowed = list(getattr(args, "allowed_host", None) or ())
        bind = getattr(args, "web_host", None)
        if bind and bind not in WILDCARD_HOSTS and bind_is_loopback(bind) and _canon_ip(bind.strip("[]")):
            allowed.append(bind)       # a loopback bind on another address (say 127.0.1.1) answers to its own address, as it always did
        return cls(admin_file=None if demo else getattr(args, "password_hash_file", None),
                   viewer_file=None if demo else getattr(args, "viewer_password_hash_file", None),
                   allowed_hosts=allowed, trusted_proxies=getattr(args, "trusted_proxy", None) or (),
                   tls=bool(getattr(args, "tls_cert", None)), viewer_exports=bool(getattr(args, "viewer_exports", False)),
                   idle_minutes=getattr(args, "session_idle_minutes", None) or IDLE_MINUTES,
                   absolute_hours=getattr(args, "session_hours", None) or ABSOLUTE_HOURS)

    # -- the connection --
    def _peer_trusted(self, peer):
        ip = _canon_ip((peer or "").split("%")[0])
        return bool(ip) and any(ipaddress.ip_address(ip) in net for net in self.proxies)

    def client_address(self, peer, headers):
        """The address of the browser: the socket peer, unless the peer is a --trusted-proxy, in which case the rightmost
        X-Forwarded-For entry that is not itself a trusted proxy (an entry a client could have forged is never reached)."""
        if not self.proxies or not self._peer_trusted(peer):
            return peer
        entries = [e.strip() for h in (headers.get_all("X-Forwarded-For") or []) for e in h.split(",")]
        for entry in reversed(entries):
            ip = _canon_ip(entry)
            if ip is None:
                return peer                           # something odd: do not guess, keep the proxy's own address
            if not self._peer_trusted(ip):
                return ip
        return peer

    def is_secure(self, peer, headers):
        """True if the browser reached us over TLS: our own TLS, or a trusted proxy that says it terminated TLS."""
        if self.tls:
            return True
        if self.proxies and self._peer_trusted(peer):
            proto = (headers.get("X-Forwarded-Proto") or "").split(",")[-1].strip().lower()
            return proto == "https"
        return False

    def request(self, peer, headers):
        """The Req for a request: addresses, TLS, host and the session its cookie names (if valid)."""
        req = Req()
        req.peer = peer
        req.client = self.client_address(peer, headers)
        req.secure = self.is_secure(peer, headers)
        req.host = parse_host_header(headers.get("Host"))
        req.token = self.cookie_token(headers, req.secure)
        req.known_device = self.auth_required and self.device_ok(self.cookie_token(headers, req.secure, DEVICE_COOKIE))
        if self.auth_required and req.token:
            s = self.sessions.get(req.token)
            if s is not None:
                cred = self.creds.get(s.role)
                if cred is None or not hmac.compare_digest(cred[1], s.generation):    # password changed (or file gone): sign out
                    self.sessions.destroy(s)
                    s = None
            req.session = s
        return req

    @staticmethod
    def cookie_name(secure, base=COOKIE):
        """The cookie's name: with the __Host- prefix over TLS (see HOST_PREFIX), plain otherwise."""
        return (HOST_PREFIX if secure else "") + base

    @classmethod
    def cookie_token(cls, headers, secure=False, base=COOKIE):
        """The value of our cookie from the Cookie header (the last cookie of that name wins), or None. Over TLS only the prefixed name counts."""
        want, token = cls.cookie_name(secure, base), None
        for header in headers.get_all("Cookie") or []:
            for part in header.split(";"):
                name, _, value = part.strip().partition("=")
                if name == want and value:
                    token = value
        return token

    def new_device_token(self):
        """A fresh 'known device' value: random part plus its HMAC under the per-run key (nothing is stored)."""
        nonce = secrets.token_hex(16)
        return nonce + "." + hmac.new(self._device_key, b"device|" + nonce.encode(), hashlib.sha256).hexdigest()

    def device_ok(self, value):
        """True if `value` is a device token this run handed out (constant-time)."""
        if not isinstance(value, str) or value.count(".") != 1 or len(value) > 200:
            return False
        nonce, _, sig = value.partition(".")
        good = hmac.new(self._device_key, b"device|" + nonce.encode("utf-8", "replace"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(good.encode(), sig.encode("utf-8", "replace"))

    # -- Host, Origin, CSRF --
    def host_ok(self, req):
        """True if the Host header names one of the allowed names (this computer's own, plus every --allowed-host)."""
        return req.host is not None and req.host[0] in self.allowed

    def origin_ok(self, req, origin, fetch_site):
        """True if a POST may proceed as far as its Origin goes. When a login exists the Origin must be present and be exactly
        this page's own (scheme, host, port); without a login an absent Origin is accepted (curl, scripts) as before. Browsers also
        say how a request relates to the page (Sec-Fetch-Site); anything but same-origin is refused when they say it."""
        if fetch_site is not None and fetch_site.lower() not in ("same-origin", "none"):
            return False
        if origin is None:
            return not self.auth_required
        parsed = parse_origin(origin)
        if parsed is None or req.host is None:
            return False
        scheme = "https" if req.secure else "http"
        return parsed == (scheme, req.host[0], req.host[1] or (443 if scheme == "https" else 80))

    def csrf_token(self, session):
        """The CSRF token of a session: an HMAC of its id under a per-run key (nothing extra to store)."""
        return hmac.new(self._csrf_key, session.sid.encode("ascii"), hashlib.sha256).hexdigest()

    def csrf_ok(self, req, header):
        """True if the request carries its session's CSRF token (constant-time comparison)."""
        if req.session is None or not isinstance(header, str):
            return False
        return hmac.compare_digest(self.csrf_token(req.session).encode(), header.encode("utf-8", "replace"))

    # -- roles --
    def authorize(self, req, role, sensitive=False):
        """None if the request may use a route tagged `role` (and `sensitive`), else (status, message): 401 not signed in, 403 not allowed."""
        if not self.auth_required or role == PUBLIC:
            return None
        if req.session is None:
            return 401, "Sign in first."
        if RANK[req.session.role] < RANK[role]:
            return 403, "Your account is not allowed to do that."
        if sensitive and req.session.role != ADMIN and not self.viewer_exports:
            return 403, "Your account is not allowed to see that."
        return None

    # -- login, logout, session probe --
    def cookie(self, token, secure, clear=False, base=COOKIE, max_age=None):
        """The Set-Cookie value: HttpOnly, SameSite=Strict, Path=/, no Domain, Secure (and the __Host- name) over TLS."""
        age = "; Max-Age=0" if clear else (f"; Max-Age={max_age}" if max_age else "")
        return f"{self.cookie_name(secure, base)}={'' if clear else token}; HttpOnly; SameSite=Strict; Path=/{age}" + ("; Secure" if secure else "")

    def login(self, req, body):
        """POST /api/login {account, password}: returns (status, JSON dict, extra headers). Every failure looks the same (401, same
        text, a password hash is computed either way); a throttled source gets 429 without its password being looked at."""
        from meshllm.webroutes import HttpError
        if not self.auth_required:
            raise HttpError(400, "No login is set up on this dashboard.")
        account, password = body.get("account"), body.get("password")
        if not isinstance(account, str) or not isinstance(password, str) or not password:
            raise HttpError(400, "Choose an account and type its password.")
        key = source_key(req.client)
        wait = self._login_wait(req, key)
        if wait > 0:
            return self._throttled(wait)
        if not self.slots.acquire(timeout=3):
            return self._throttled(2.0)
        try:
            wait = self._login_wait(req, key)     # another attempt may have failed while we queued
            if wait > 0:
                return self._throttled(wait)
            cred = self.creds.get(account) if account in ACCOUNTS else None
            ok = passwords.verify_password(password, cred[0] if cred else self._dummy)
            ok = ok and cred is not None
        finally:
            self.slots.release()
        if not ok:
            self.per_source.fail(key)
            self.global_.fail("*")
            print(f"[web] failed login from {key}")
            return 401, {"error": "Wrong account or password."}, {}
        self.per_source.reset(key)
        if req.token:
            self.sessions.destroy(req.token)          # a brand new token at every login (no session fixation)
        token, session = self.sessions.create(account, cred[1])
        print(f"[web] {account} signed in from {key}")
        headers = {"Set-Cookie": self.cookie(token, req.secure)}
        if account == ADMIN:       # a second cookie: this browser has proven the admin password, so the global wait does not apply to it
            headers["Set-Cookie"] = [headers["Set-Cookie"], self.cookie(self.new_device_token(), req.secure, base=DEVICE_COOKIE, max_age=DEVICE_DAYS * 86400)]
        return 200, {"ok": True, "role": account, "csrf": self.csrf_token(session)}, headers

    def _login_wait(self, req, key):
        """Seconds this request must wait before a password is looked at. The global wait (which slows a guesser using many addresses)
        does not apply to a browser the admin has already signed in from, or a stranger could keep the owner out for as long as they like
        by failing once a minute. The per-source wait always applies."""
        return max(self.per_source.wait(key), 0.0 if req.known_device else self.global_.wait("*"))

    @staticmethod
    def _throttled(wait):
        seconds = int(wait) + 1
        return 429, {"error": f"Too many attempts. Wait {seconds} seconds and try again.", "retry_after": seconds}, {"Retry-After": str(seconds)}

    def logout(self, req):
        """POST /api/logout: end the session and tell the browser to drop its cookie."""
        if req.session is not None:
            self.sessions.destroy(req.session)
        return 200, {"ok": True}, {"Set-Cookie": self.cookie("", req.secure, clear=True)} if self.auth_required else {}

    def session_info(self, req):
        """GET /api/session: what the page needs to know to draw itself (and the CSRF token of a live session)."""
        if not self.auth_required:
            return {"auth_required": False, "authenticated": True, "role": ADMIN, "csrf": None, "insecure_transport": False, "viewer_exports": True}
        return {"auth_required": True, "authenticated": req.session is not None, "role": req.role,
                "csrf": self.csrf_token(req.session) if req.session else None, "viewer_exports": self.viewer_exports,
                # the password travels in clear: neither TLS nor a loopback client. The login page says so.
                "insecure_transport": not req.secure and not is_loopback_peer(req.client)}


# ---- start-up checks and TLS ---------------------------------------------------------------------------------------------------
def add_arguments(p):
    """The command-line flags of this feature, added to the bridge's parser."""
    p.add_argument("--set-password", action="store_true",
                   help="ask for a dashboard password (no echo), store only its scrypt hash in a file readable by you alone, and exit")
    p.add_argument("--role", choices=passwords.ROLES, default="admin", help="with --set-password: the account to set (default admin)")
    p.add_argument("--password-hash-file", default=None, metavar="PATH",
                   help="turn the dashboard login on: the file --set-password wrote (or set MESHLLM_PASSWORD_HASH_FILE to its path); "
                        "mandatory when --web-host is not a loopback address")
    p.add_argument("--viewer-password-hash-file", default=None, metavar="PATH",
                   help="optional second, read-only account for the dashboard (MESHLLM_VIEWER_PASSWORD_HASH_FILE)")
    p.add_argument("--viewer-exports", action="store_true",
                   help="let the viewer account also use CSV exports, reports, diagnostics and logs (they contain message text)")
    p.add_argument("--allowed-host", action="append", default=None, metavar="NAME",
                   help="a host name or address the dashboard may be reached by; repeatable; required when --web-host is not a loopback address")
    p.add_argument("--trusted-proxy", action="append", default=None, metavar="ADDR_OR_CIDR",
                   help="a reverse proxy whose X-Forwarded-For and X-Forwarded-Proto headers are believed; repeatable")
    p.add_argument("--tls-cert", default=None, metavar="PEM", help="serve the dashboard over HTTPS with this certificate (needs --tls-key)")
    p.add_argument("--tls-key", default=None, metavar="PEM", help="the private key for --tls-cert")
    p.add_argument("--session-idle-minutes", type=int, default=IDLE_MINUTES, help="sign a browser out after this long without a request")
    p.add_argument("--session-hours", type=int, default=ABSOLUTE_HOURS, help="sign a browser out this long after login, whatever it does")


def tls_context(cert, key):
    """A server-side TLS context (TLS 1.2 and up) for the certificate and key files; raises ssl.SSLError or OSError if they are unusable."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


def check_args(parser, args, environ=None, err=None):
    """Validate the web-security flags after parsing; ends with a clear `parser.error` on a mistake and fills in defaults
    (password files from MESHLLM_PASSWORD_HASH_FILE / MESHLLM_VIEWER_PASSWORD_HASH_FILE, normalised host and proxy lists)."""
    environ = os.environ if environ is None else environ
    err = err or (lambda text: print(text, file=sys.stderr))
    if args.set_password or args.ble_scan or args.demo or args.no_web:
        return                                        # nothing is served (or, in demo, nothing is protected and non-loopback is refused there)
    args.password_hash_file = args.password_hash_file or environ.get("MESHLLM_PASSWORD_HASH_FILE") or None
    args.viewer_password_hash_file = args.viewer_password_hash_file or environ.get("MESHLLM_VIEWER_PASSWORD_HASH_FILE") or None
    args.password_hash_file = os.path.abspath(args.password_hash_file) if args.password_hash_file else None      # a relative path must not depend on the folder later
    args.viewer_password_hash_file = os.path.abspath(args.viewer_password_hash_file) if args.viewer_password_hash_file else None
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("--tls-cert and --tls-key go together.")
    if args.tls_cert:
        try:
            tls_context(args.tls_cert, args.tls_key)
        except (ssl.SSLError, OSError) as e:
            parser.error(f"the TLS certificate or key cannot be used ({type(e).__name__}); check both files and that the key matches.")
    hosts = []
    for h in args.allowed_host or []:
        n = normalise_host(h)
        if n is None or "*" in h:
            parser.error(f"--allowed-host {h!r} is not a plain host name or address (no scheme, port, path or wildcard).")
        hosts.append(n)
    args.allowed_host = hosts
    for p in args.trusted_proxy or []:
        try:
            ipaddress.ip_network(p, strict=False)
        except ValueError:
            parser.error(f"--trusted-proxy {p!r} is not an IP address or network (for example 127.0.0.1 or 192.0.2.0/24).")
    for role, path in (("admin", args.password_hash_file), ("viewer", args.viewer_password_hash_file)):
        if not path:
            continue
        try:
            passwords.read_hash_file(path)
        except passwords.PasswordError as e:
            parser.error(f"the {role} password file: {e} (create it with python -m meshllm --set-password"
                         f"{' --role viewer' if role == 'viewer' else ''}).")
        if passwords.loose_permissions(path):
            err(f"warning: {path} can be read by other users; run chmod 600 on it.")
    if args.viewer_password_hash_file and not args.password_hash_file:
        parser.error("a viewer account needs the admin password too (--password-hash-file).")
    if bind_is_loopback(args.web_host):
        return
    container = environ.get("MESHLLM_CONTAINER") == "1" and args.web_host in WILDCARD_HOSTS
    if container and not args.password_hash_file:
        return                                        # the Docker image: the published port, bound to the host's loopback, is the only exposure
    if not args.password_hash_file:
        parser.error(f"--web-host {args.web_host} is reachable from other computers, so the dashboard needs a login. "
                     "Run  python -m meshllm --set-password  and start with  --password-hash-file PATH  (it prints the path).")
    if not args.allowed_host:
        parser.error(f"--web-host {args.web_host} needs --allowed-host NAME: the host name or address you will type in the browser "
                     "(repeatable, for example --allowed-host 192.0.2.10 --allowed-host radio.test).")
    if not args.tls_cert and not args.trusted_proxy:
        err("warning: no TLS: the password and the dashboard travel in clear text on this network. Use --tls-cert/--tls-key "
            "or a reverse proxy (docs/setup.md), and only on a network you trust.")
