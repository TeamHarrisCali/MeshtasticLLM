"""The dashboard's web server: the HTTP side only (security checks, static files, JSON replies).

The API itself is a table of small functions in webroutes.py; who may call which route, the login, sessions and the Host/Origin/CSRF
rules are in websecurity.py. Listens on localhost by default; audit data contains message text. On a non-loopback address it needs a
login (see .github/SECURITY.md). Every request goes through the same order of checks: Host, then (for a POST) Origin, then the session and
role, then the CSRF token, then the content type, and only then is the body read and the route run.
"""
import json
import mimetypes
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from meshllm import paths, webroutes
from meshllm.webroutes import HttpError, Reply
from meshllm.websecurity import (CSRF_HEADER, MAX_LOGIN_BODY, PUBLIC, PUBLIC_STATIC, VIEWER, WILDCARD_HOSTS, WebSecurity, tls_context)

STATIC = paths.resource_root() / "meshllm" / "static"      # beside the package, or inside the bundle in a packaged program
_bundle = {"stamp": None, "data": b""}
MAX_JSON_BODY = 1024 * 1024    # the biggest JSON body any dashboard action sends is a few KB; more is a mistake or an attack


def dashboard_script():
    """static/js/*.js joined in the order listed in static/js/order.txt (rebuilt when any of them changes)."""
    folder = STATIC / "js"
    names = [l.strip() for l in (folder / "order.txt").read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    stamp = tuple((n, (folder / n).stat().st_mtime_ns) for n in names)
    if stamp != _bundle["stamp"]:
        lf = bytes([10])
        _bundle["data"] = lf.join((folder / n).read_bytes().replace(bytes([13, 10]), lf) for n in names)
        _bundle["stamp"] = stamp
    return _bundle["data"]


def media_type(value):
    """The media type of a Content-Type header, lower case, without parameters (`application/json; charset=utf-8` -> `application/json`)."""
    return (value or "").split(";", 1)[0].strip().lower()


def make_handler(bridge):
    """Build the request handler class bound to this bridge (the HTTP server creates one handler instance per request)."""
    security = getattr(bridge, "web_security", None)
    if security is None:
        security = bridge.web_security = WebSecurity.from_args(bridge.args)
    tls = bool(getattr(bridge.args, "tls_cert", None))

    class Handler(BaseHTTPRequestHandler):
        """Serves the dashboard: API routes from webroutes, the bundled script, and files from static/."""
        server_version = "MeshLLM"
        timeout = 30         # a client that stops sending frees its thread after this long (slow-loris)

        def setup(self):
            """With TLS the handshake happens here, in this request's own thread and under a time limit, so one stalled client cannot hold up the others."""
            if tls:
                self.request.settimeout(10)
                self.request.do_handshake()
            super().setup()

        def log_message(self, *args):  # keep the console for mesh traffic
            """Silence the default per-request log line."""
            pass

        def _begin(self):
            """Look at the connection and headers and apply the Host check (audit data contains message text, so a request that does not
            name one of the allowed hosts is refused: DNS rebinding). Returns the Req, or None after answering 403."""
            req = security.request(self.client_address[0], self.headers)
            if not security.host_ok(req):
                self._send(403, "forbidden", "text/plain")
                return None
            return req

        def _refuse(self, code, message):
            """Answer a refused request: JSON for the API, plain text for everything else."""
            if urlparse(self.path).path.startswith("/api/"):
                return self._json({"error": message}, code)
            self._send(code, message, "text/plain")

        def _send(self, code, body, ctype="application/json", extra=None):
            """Write a full response. Replies are not cached by default (they hold live data); `extra` headers can override that."""
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if "Cache-Control" not in (extra or {}):
                self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            # the map background is served by this bridge (/tiles, cached on disk), so the browser only ever talks to it;
            # frame-ancestors / X-Frame-Options: nobody may put the dashboard in a frame (clickjacking)
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                                        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            for k, v in (extra or {}).items():
                for item in (v if isinstance(v, list) else [v]):      # a list sends the header once per entry (two Set-Cookie lines)
                    self.send_header(k, item)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, code=200):
            """Send obj as a JSON reply."""
            self._send(code, json.dumps(obj))

        def _answer(self, result):
            """Send what a route returned: a Reply is sent as given, anything else as JSON."""
            if isinstance(result, Reply):
                return self._send(result.code, result.body, result.ctype, result.headers)
            self._json(result)

        def _guarded(self, fn, what):
            """Run a request handler so no exception escapes: a vanished client is ignored, any other failure becomes a 500."""
            try:
                fn()
            except (ConnectionError, TimeoutError, socket.timeout):   # the browser went away (page reload, tab closed): nothing to report
                pass
            except Exception as e:  # last resort: a clean error reply instead of a dropped connection
                print(f"[web] {what} {self.path[:80]!r} failed: {e!r}")
                try:
                    self._json({"error": "internal error"}, 500)
                except Exception:
                    pass

        def do_GET(self):
            """Entry point for GET requests."""
            self._guarded(self._get, "GET")

        def do_POST(self):
            """Entry point for POST requests."""
            self._guarded(self._post, "POST")

        def _find_get(self, path):
            """(route function, regex match or None) for a GET path, or None."""
            fn = webroutes.GET.get(path)
            if fn:
                return fn, None
            for rx, fn in webroutes.GET_RE:
                m = rx.fullmatch(path)
                if m:
                    return fn, m
            return None

        def _static_file(self, path):
            """The file under static/ for a URL path, or None. resolve() collapses any "..", then the file must sit under STATIC."""
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            f = (STATIC / rel).resolve()
            return f if STATIC.resolve() in f.parents and f.is_file() else None

        def _get(self):
            """GET: an API route (exact path, then pattern), the script bundle, then a static file; 404 otherwise. Default-deny: with a
            login set, everything that is not a `public` route or one of the login page's files needs a session first."""
            req = self._begin()
            if req is None:
                return
            url = urlparse(self.path)
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            found = self._find_get(url.path)
            if found:
                fn, m = found
                role, sensitive = getattr(fn, "route_role", "admin"), getattr(fn, "route_sensitive", False)
            else:      # the dashboard's own files: a session is enough, except the few the login page needs
                fn, m, sensitive = None, None, False
                role = PUBLIC if url.path in PUBLIC_STATIC or url.path in ("/", "") else VIEWER
            denied = security.authorize(req, role, sensitive)
            if denied:
                return self._refuse(*denied)
            if fn:
                try:
                    args = (bridge, qs) + ((m,) if m is not None else ()) + ((req,) if getattr(fn, "route_ctx", False) else ())
                    return self._answer(fn(*args))
                except Exception as e:                  # an error the operator can cause (a bad value) is shown; anything else is a 500
                    err = webroutes.error_reply(e)
                    if err is None:
                        raise
                    return self._json(err[1], err[0])
            if url.path in ("/", "") and security.auth_required and req.session is None:
                page = STATIC / "login.html"            # not signed in: the login page is what lives at /
                return self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            if url.path == "/app.js":
                return self._send(200, dashboard_script(), "text/javascript; charset=utf-8")
            f = self._static_file(url.path)
            if f:
                ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                return self._send(200, f.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
            self._send(404, "not found", "text/plain")

        def _post(self):
            """POST: every POST can change data or transmit. In order: Host, Origin, session and role, CSRF token, content type
            (JSON, or octet-stream for the upload route), body size, then the route."""
            req = self._begin()
            if req is None:
                return
            # a page on another site can submit a POST to this server; browsers always attach Origin to those. With a login it must be
            # present and exactly this page's own; Sec-Fetch-Site (sent by browsers) must also say same-origin
            if not security.origin_ok(req, self.headers.get("Origin"), self.headers.get("Sec-Fetch-Site")):
                return self._send(403, "forbidden", "text/plain")
            path = urlparse(self.path).path
            raw = webroutes.POST_RAW.get(path)            # a file upload: the route reads the bytes itself
            fn = raw or webroutes.POST.get(path)
            role, sensitive = (getattr(fn, "route_role", "admin"), getattr(fn, "route_sensitive", False)) if fn else (VIEWER, False)
            denied = security.authorize(req, role, sensitive)
            if denied:
                return self._refuse(*denied)
            if fn is None:
                return self._send(404, "not found", "text/plain")
            if security.auth_required and role != PUBLIC and not security.csrf_ok(req, self.headers.get(CSRF_HEADER)):
                return self._refuse(403, "The page's security token is missing or old: reload the page.")
            if raw:
                # not a "simple" content type, so a browser would need a CORS preflight to send it from another site
                if media_type(self.headers.get("Content-Type")) != "application/octet-stream":
                    return self._send(415, "octet-stream only", "text/plain")
                return self._answer_post(lambda: raw(bridge, self))
            # requiring JSON also blocks plain HTML form posts, which cannot send this content type without a CORS preflight
            if media_type(self.headers.get("Content-Type")) != "application/json":
                return self._send(415, "json only", "text/plain")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0:                     # rfile.read(-1) would wait for the client to hang up, tying up a server thread
                return self._send(400, "bad length", "text/plain")
            if length > (MAX_LOGIN_BODY if role == PUBLIC else MAX_JSON_BODY):   # refuse before reading, so a huge body is never held in memory
                return self._send(413, "too large", "text/plain")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send(400, "bad json", "text/plain")
            if not isinstance(body, dict):
                return self._json({"error": "The request body must be a JSON object."}, 400)
            self._answer_post(lambda: fn(bridge, body, req) if getattr(fn, "route_ctx", False) else fn(bridge, body))

        def _answer_post(self, call):
            """Run a POST route and send its result; operator-caused errors become JSON error replies, others propagate to _guarded."""
            try:
                return self._answer(call())
            except Exception as e:
                err = webroutes.error_reply(e)
                if err is None:
                    raise
                self._json(err[1], err[0])

    return Handler


class Server(ThreadingHTTPServer):
    """The dashboard's HTTP server: IPv4 or IPv6 as the bind address says, optionally TLS (handshake done per connection in its own
    thread, see Handler.setup), and a one-line note instead of a traceback when a client misbehaves."""
    daemon_threads = True
    MAX_CONNECTIONS = 64     # connections served at once; more are closed at once, so a LAN client holding sockets open cannot exhaust threads

    def __init__(self, address, handler, tls_ctx=None):
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        self._slots = threading.BoundedSemaphore(self.MAX_CONNECTIONS)
        super().__init__(address, handler)
        if tls_ctx is not None:
            self.socket = tls_ctx.wrap_socket(self.socket, server_side=True, do_handshake_on_connect=False)

    def process_request(self, request, client_address):
        """Start a thread for the connection unless MAX_CONNECTIONS are already being served."""
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        """A failed handshake or a connection that died mid-request: one line, no traceback."""
        print(f"[web] connection error: {sys.exc_info()[0].__name__}")


def start(bridge):
    """Start the dashboard on bridge.args.web_host:web_port in a background thread and return the server."""
    ctx = tls_context(bridge.args.tls_cert, bridge.args.tls_key) if getattr(bridge.args, "tls_cert", None) else None
    srv = Server((bridge.args.web_host, bridge.args.web_port), make_handler(bridge), ctx)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
