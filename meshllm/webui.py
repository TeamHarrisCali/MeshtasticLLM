"""The dashboard's web server: the HTTP side only (security checks, static files, JSON replies).

The API itself is a table of small functions in webroutes.py. Listens on localhost; audit data contains message text.
"""
import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from meshllm import webroutes
from meshllm.webroutes import HttpError, Reply

STATIC = Path(__file__).parent / "static"
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


def make_handler(bridge):
    """Build the request handler class bound to this bridge (the HTTP server creates one handler instance per request)."""
    class Handler(BaseHTTPRequestHandler):
        """Serves the dashboard: API routes from webroutes, the bundled script, and files from static/."""
        server_version = "MeshLLM"

        def log_message(self, *args):  # keep the console for mesh traffic
            """Silence the default per-request log line."""
            pass

        # Audit data contains message text, so refuse requests from other sites (DNS rebinding /
        # cross-site POSTs) even though the server only listens on localhost by default.
        def _host_ok(self):
            """True if the Host header names this machine (or the configured web host); anything else is refused."""
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            return host in ("localhost", "127.0.0.1", "::1") or host == bridge.args.web_host

        def _send(self, code, body, ctype="application/json", extra=None):
            """Write a full response. Replies are not cached by default (they hold live data); `extra` headers can override that."""
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if "Cache-Control" not in (extra or {}):
                self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            # the map background is served by this bridge (/tiles, cached on disk), so the browser only ever talks to it
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
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
            except (ConnectionError, TimeoutError):   # the browser went away (page reload, tab closed): nothing to report
                pass
            except Exception as e:  # last resort: a clean error reply instead of a dropped connection
                print(f"[web] {what} {self.path[:80]} failed: {e!r}")
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

        def _get(self):
            """GET: an API route (exact path, then pattern), the script bundle, then a static file; 404 otherwise."""
            if not self._host_ok():
                return self._send(403, "forbidden", "text/plain")
            url = urlparse(self.path)
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                fn = webroutes.GET.get(url.path)
                if fn:
                    return self._answer(fn(bridge, qs))
                for rx, fn in webroutes.GET_RE:
                    m = rx.fullmatch(url.path)
                    if m:
                        return self._answer(fn(bridge, qs, m))
            except Exception as e:                      # an error the operator can cause (a bad value) is shown; anything else is a 500
                err = webroutes.error_reply(e)
                if err is None:
                    raise
                return self._json(err[1], err[0])
            if url.path == "/app.js":
                return self._send(200, dashboard_script(), "text/javascript; charset=utf-8")
            # static files (whitelisted to the static dir): resolve() collapses any "..", then the file must sit under STATIC
            rel = "index.html" if url.path in ("/", "") else url.path.lstrip("/")
            f = (STATIC / rel).resolve()
            if STATIC.resolve() in f.parents and f.is_file():
                ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                return self._send(200, f.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
            self._send(404, "not found", "text/plain")

        def _post(self):
            """POST: every POST can change data or transmit, so it also needs a same-origin check and a JSON body (or a raw upload route)."""
            if not self._host_ok():
                return self._send(403, "forbidden", "text/plain")
            origin = self.headers.get("Origin")
            # a page on another site can submit a POST to localhost; browsers always attach Origin to those, so it must match Host
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                return self._send(403, "forbidden", "text/plain")
            path = urlparse(self.path).path
            raw = webroutes.POST_RAW.get(path)            # a file upload: the route reads the bytes itself
            if raw:
                return self._answer_post(lambda: raw(bridge, self))
            # requiring JSON also blocks plain HTML form posts, which cannot send this content type without a CORS preflight
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._send(415, "json only", "text/plain")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0:                     # rfile.read(-1) would wait for the client to hang up, tying up a server thread
                return self._send(400, "bad length", "text/plain")
            if length > MAX_JSON_BODY:         # refuse before reading, so a huge body is never held in memory
                return self._send(413, "too large", "text/plain")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send(400, "bad json", "text/plain")
            if not isinstance(body, dict):
                return self._json({"error": "The request body must be a JSON object."}, 400)
            fn = webroutes.POST.get(path)
            if fn is None:
                return self._send(404, "not found", "text/plain")
            self._answer_post(lambda: fn(bridge, body))

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


def start(bridge):
    """Start the dashboard on bridge.args.web_host:web_port in a background thread and return the server."""
    srv = ThreadingHTTPServer((bridge.args.web_host, bridge.args.web_port), make_handler(bridge))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
