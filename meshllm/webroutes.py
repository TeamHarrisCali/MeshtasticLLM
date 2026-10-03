"""The dashboard's JSON API as a route table.

Each route is a small function registered with a decorator:

    @get("/api/status")                     # exact path; the function gets (bridge, query) and returns JSON data
    def status(b, q): return b.status()

    @post("/api/pause")                     # the function gets (bridge, body) where body is a JSON object
    def pause(b, body): ...

    @get_re(r"/api/data/export/([a-z_]+)\\.csv")   # a pattern; the function also gets the regex match

Every decorator also takes who may call the route, `PUBLIC`, `VIEWER` or `ADMIN` (see websecurity.py); a route with no tag is
`ADMIN`. `sensitive=True` on a `VIEWER` route (CSV exports, logs, diagnostics, reports) closes it to viewers unless the owner
opted in. tests/test_websecurity.py lists every route and its tag, so a new route cannot slip in unnoticed.

A function returns data to send as JSON, a `Reply` for anything else (a download, an image), or raises `HttpError`
for an error the page should show. Errors the operator can cause (a bad value, a busy radio) are turned into a 400
with their message by the server, so a route only needs to raise them. `webui.py` does the HTTP side (security
checks, static files) and looks routes up here.
"""
import json
import time

from meshllm import actions
from meshllm import evals
from meshllm import inbox
from meshllm import report
from meshllm.channel import ChannelError
from meshllm.mesh import PositionError
from meshllm.ollama_models import OllamaError
from meshllm.radio_config import ConfigError, RadioMismatch
from meshllm.telemetry import KINDS as TELEMETRY_KINDS, METRICS as TELEMETRY_METRICS, TelemetryError
from meshllm.traceroute import TracerouteError
from meshllm.websecurity import ADMIN, PUBLIC, ROLES, VIEWER

# route tables filled by the decorators below and read by webui.py: exact GET paths, exact POST paths, GET patterns, raw-upload POST paths
GET, POST, GET_RE, POST_RAW = {}, {}, [], {}
# exceptions whose message is meant for the operator: error_reply() turns these into a 400 instead of a 500
USER_ERRORS = (ValueError, ChannelError, TracerouteError, TelemetryError, OllamaError, PositionError, ConfigError)


class HttpError(Exception):
    """An error the page should show: `code` is the HTTP status, `message` the text, `extra` more JSON fields for the reply."""

    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


class Reply:
    """A non-JSON answer: bytes or text with a content type and optional headers (for example a file download)."""

    def __init__(self, body, ctype="application/json", headers=None, code=200):
        self.body, self.ctype, self.headers, self.code = body, ctype, headers or {}, code


# ---- route registration ---------------------------------------------------------------------------------------------
def download(body, ctype, filename):
    """A Reply that makes the browser save `body` as a file called `filename`."""
    return Reply(body, ctype, {"Content-Disposition": f'attachment; filename="{filename}"'})


def _tag(fn, role, sensitive, ctx):
    """Record on the function who may call the route (see websecurity.py). Raises at import time on a nonsense tag."""
    if role not in ROLES or (sensitive and role != VIEWER):
        raise ValueError(f"bad route tag: role={role!r} sensitive={sensitive!r}")
    fn.route_role, fn.route_sensitive, fn.route_ctx = role, sensitive, ctx
    return fn


def get(path, role=ADMIN, sensitive=False, ctx=False):
    """Decorator: register fn as the handler for GET `path` (exact match). fn(bridge, query_dict), or fn(bridge, query_dict, req) with ctx=True.
    `role` is who may call it (PUBLIC, VIEWER or ADMIN; the default, ADMIN, is the safe one); sensitive=True on a VIEWER route
    keeps viewers out unless the owner passed --viewer-exports."""
    def reg(fn):
        """Add fn to the GET table and return it unchanged."""
        GET[path] = _tag(fn, role, sensitive, ctx)
        return fn
    return reg


def get_re(pattern, role=ADMIN, sensitive=False, ctx=False):
    """Decorator: register fn for GET paths matching the regex (full match). fn(bridge, query_dict, match). Tags as for get()."""
    import re
    rx = re.compile(pattern)

    def reg(fn):
        """Add fn to the GET pattern list and return it unchanged."""
        GET_RE.append((rx, _tag(fn, role, sensitive, ctx)))
        return fn
    return reg


def post(path, role=ADMIN, sensitive=False, ctx=False):
    """Decorator: register fn as the handler for POST `path`. fn(bridge, body_dict), where body is the parsed JSON object (and req with ctx=True)."""
    def reg(fn):
        """Add fn to the POST table and return it unchanged."""
        POST[path] = _tag(fn, role, sensitive, ctx)
        return fn
    return reg


def post_raw(path, role=ADMIN, sensitive=False):
    """A POST that is not JSON (a file upload). The function gets (bridge, handler) and reads handler.rfile itself."""
    def reg(fn):
        """Add fn to the raw-upload POST table and return it unchanged."""
        POST_RAW[path] = _tag(fn, role, sensitive, False)
        return fn
    return reg


# ---- query parsing and error mapping --------------------------------------------------------------------------------
def qint(qs, key, default):
    """An integer query parameter; anything else (missing, 'abc', '1e9', '') falls back to the default."""
    try:
        return max(-2 ** 62, min(int(qs.get(key, default)), 2 ** 62))  # keep it inside SQLite's integer range
    except (TypeError, ValueError):
        return default


def qfloat(qs, key):
    """A float query parameter, or None if it is missing, not a number, NaN/infinite or absurdly large."""
    try:
        v = float(qs.get(key))
        return v if v == v and abs(v) < 1e15 else None    # no NaN / infinity
    except (TypeError, ValueError):
        return None


def error_reply(e):
    """(status code, JSON dict) for an exception a POST route raised, or None if it is not one the operator should see."""
    if isinstance(e, HttpError):
        return e.code, {"error": e.message, **e.extra}
    if isinstance(e, RadioMismatch):          # checked before USER_ERRORS so the page gets the distinct 409 and can offer "restore anyway"
        return 409, {"error": str(e), "mismatch": True}
    if isinstance(e, USER_ERRORS):
        return 400, {"error": str(e)}
    return None


# ======================================================================================================================
# the login: who is asking, sign in, sign out (the only routes the public may call)
# ======================================================================================================================
@get("/api/session", PUBLIC, ctx=True)
def r_session(b, q, req):
    """GET /api/session: whether a login is needed, whether this browser is signed in as which account, and its CSRF token."""
    return b.web_security.session_info(req)


@post("/api/login", PUBLIC, ctx=True)
def w_login(b, body, req):
    """POST /api/login {account, password}: sign in (account is 'admin' or 'viewer'); sets the session cookie. Throttled; every failure looks alike."""
    code, data, headers = b.web_security.login(req, body)
    return Reply(json.dumps(data), "application/json", headers, code)


@post("/api/logout", VIEWER, ctx=True)
def w_logout(b, body, req):
    """POST /api/logout: end this browser's session."""
    code, data, headers = b.web_security.logout(req)
    return Reply(json.dumps(data), "application/json", headers, code)


# ======================================================================================================================
# status, the AI log, conversations
# ======================================================================================================================
@get("/api/status", VIEWER, ctx=True)
def r_status(b, q, req):
    """GET /api/status: live bridge state for the page header (radio, model, queue, paused flag). Works with no radio attached.
    A read-only account gets the kind of connection but not its address (a serial path, a LAN host or a Bluetooth address)."""
    data = b.status()
    if b.web_security.auth_required and req.role != ADMIN:
        from meshllm.connection import KIND_NAMES
        conn = dict(data.get("connection") or {})
        entries = [dict(e, label=KIND_NAMES.get(e.get("kind"), "radio")) for e in conn.get("entries") or []]
        active = next((e["label"] for e in entries if e.get("state") == "active"), None)
        data = dict(data, connection=dict(conn, entries=entries), port=(active or "radio") if data.get("port") else data.get("port"))
    return data


@get("/api/stats", VIEWER)
def r_stats(b, q):
    """GET /api/stats: all-time totals from the audit database for the dashboard."""
    return b.audit.stats()


@get("/api/requests", VIEWER)
def r_requests(b, q):
    """GET /api/requests: the AI log, newest first. Query: limit, before (request id to page back from), q (search text), status."""
    return b.audit.list(limit=qint(q, "limit", 50), before=qint(q, "before", None), q=q.get("q") or None, status=q.get("status") or None)


@get("/api/conversations", VIEWER)
def r_conversations(b, q):
    """GET /api/conversations: one entry per node that has talked to the bridge; ?scope narrows the kind of conversation."""
    return b.conversations(q.get("scope") or None)


@get("/api/conversation", VIEWER)
def r_conversation(b, q):
    """GET /api/conversation?node=: the messages with one node, how many are kept as AI memory, and the node's access rule and 24 h usage."""
    node = q.get("node", "")
    return {"messages": b.audit.conversation(node, qint(q, "limit", 200), scope=q.get("scope") or None), "memory": len(b.history(node)), "access": b.node_access_info(node)}


@get("/api/export.csv", VIEWER, sensitive=True)
def r_export(b, q):
    """GET /api/export.csv: the whole audit log as a CSV download (it contains message text)."""
    return download(b.audit.export_csv(), "text/csv; charset=utf-8", time.strftime("mesh-llm-audit-%Y%m%d-%H%M%S.csv"))


@get("/api/queue", VIEWER)
def r_queue(b, q):
    """GET /api/queue: the running question followed by the waiting ones."""
    return b.queue_snapshot()


@get("/api/access", ADMIN)
def r_access(b, q):
    """GET /api/access: access mode, default daily cap, and a row per node that has a rule, has asked, or has been heard."""
    return b.access_overview()


@get("/api/nodes", VIEWER)
def r_known_nodes(b, q):
    """GET /api/nodes: nodes this radio has heard, for the recipient picker."""
    return b.known_nodes()


@get("/api/actions", VIEWER)
def r_actions(b, q):
    """GET /api/actions: the tools the AI can use (name, description, permission tier, parameters)."""
    return [{"name": a.name, "description": a.description, "tier": a.tier, "params": list(a.params)} for a in actions.ACTIONS.values()]


@get("/api/models", VIEWER)
def r_models(b, q):
    """GET /api/models: installed models, the current one and downloads in progress; an unreachable Ollama is reported in the reply, not as an error."""
    return b.models_overview()


@post("/api/pause", ADMIN)
def w_pause(b, body):
    """POST /api/pause {paused}: pause or resume the AI's answers to radio questions."""
    b.paused = bool(body.get("paused"))
    print(f"[web] bot {'paused' if b.paused else 'resumed'}")
    return {"paused": b.paused}


@post("/api/send", ADMIN)
def w_send(b, body):
    """POST /api/send {node, text}: queue a direct message to one node. Transmits over the radio."""
    return {"id": b.send_manual(body.get("node"), body.get("text"))}


@post("/api/ai/ask", VIEWER)
def w_ask(b, body):
    """POST /api/ai/ask {prompt}: ask the AI from the browser. The answer is stored in the log only; nothing is sent over the radio."""
    return {"id": b.ask_web(body.get("prompt"))}


@post("/api/memory/clear", ADMIN)
def w_memory_clear(b, body):
    """POST /api/memory/clear {node}: forget the AI's remembered conversation with one node. Deletes data."""
    node = str(body.get("node") or "")
    b.audit.clear_memory(node)
    print(f"[memory] cleared for {node} (web)")
    return {"ok": True}


@post("/api/access/mode", ADMIN)
def w_access_mode(b, body):
    """POST /api/access/mode {mode}: set the access mode ('open' or 'allowlist')."""
    b.set_mode(body.get("mode"))
    return {"ok": True}


@post("/api/access/default_cap", ADMIN)
def w_access_cap(b, body):
    """POST /api/access/default_cap {cap}: set the default daily question cap per node (0 = unlimited)."""
    b.set_default_cap(body.get("cap"))
    return {"ok": True}


@post("/api/access/node", ADMIN)
def w_access_node(b, body):
    """POST /api/access/node {node, ...}: change one node's access, daily cap, tool tier or pinned key; only the fields sent are touched."""
    b.set_node_access(body.get("node"), **{k: body[k] for k in ("access", "daily_cap", "max_tier", "pin_key") if k in body})
    return {"ok": True}


@post("/api/queue/cancel", ADMIN)
def w_queue_cancel(b, body):
    """POST /api/queue/cancel {id}: cancel a waiting question. 409 if it is no longer waiting (a running one cannot be cancelled)."""
    rid = body.get("id")                      # must be a number, not text, before it reaches the queue lookup
    if not isinstance(rid, int) or not b.cancel(rid):
        raise HttpError(409, "That question is no longer waiting")
    return {"ok": True}


@post("/api/model", ADMIN)
def w_model(b, body):
    """POST /api/model {model}: switch the AI model. Returns whether the new model supports tools."""
    m = b.set_model(body.get("model"))
    return {"ok": True, "tools": m["tools"]}


@post("/api/models/pull", ADMIN)
def w_pull(b, body):
    """POST /api/models/pull {name}: start downloading a model through Ollama (one download at a time)."""
    b.models.start_pull(body.get("name"))
    return {"ok": True}


@post("/api/models/pull/cancel", ADMIN)
def w_pull_cancel(b, body):
    """POST /api/models/pull/cancel {name}: stop a model download that is in progress."""
    b.models.cancel_pull(body.get("name"))
    return {"ok": True}


# ======================================================================================================================
# the public channel
# ======================================================================================================================
@get("/api/channel", VIEWER)
def r_channel(b, q):
    """GET /api/channel: the primary public channel's details plus recent messages. Query: limit, after (message id, for polling)."""
    return {"info": b.channel.info(), "messages": b.channel.list(qint(q, "limit", 100), after=qint(q, "after", None))}


@post("/api/channel/post", ADMIN)
def w_channel_post(b, body):
    """POST /api/channel/post {text}: post one message to the public channel. Transmits over the radio to everyone in range; rate limited."""
    return {"id": b.channel.post(body.get("text"))}


@post("/api/channel/clear", ADMIN)
def w_channel_clear(b, body):
    """POST /api/channel/clear: delete every stored public-channel message. Deletes data."""
    return {"deleted": b.channel.clear()}


# ======================================================================================================================
# telemetry
# ======================================================================================================================
@get("/api/telemetry", VIEWER)
def r_telemetry(b, q):
    """GET /api/telemetry: recorded telemetry readings (filter by node, kind, status, source; limit/before to page) with latest values, stats and settings."""
    tel = b.telemetry
    return {"readings": tel.store.list(node=q.get("node") or None, kind=q.get("kind") or None, status=q.get("status") or None,
                                       source=q.get("source") or None, limit=qint(q, "limit", 100), before=qint(q, "before", None)),
            "latest": tel.store.latest(), "stats": tel.store.stats(), "kinds": list(TELEMETRY_KINDS),
            "metrics": TELEMETRY_METRICS, "retention_days": tel.retention_days(), "record_all": tel.passive_all()}


@get("/api/telemetry/node", VIEWER)
def r_telemetry_node(b, q):
    """GET /api/telemetry/node?id=: what the radio's own node table holds for one node (position left out), to diagnose missing telemetry."""
    # What this radio holds about one node - for working out why a node isn't showing telemetry. Position data is left out on purpose.
    n = (getattr(b.iface, "nodes", None) or {}).get(q.get("id", ""))
    if not n:
        raise HttpError(404, "This radio has no entry for that node.")
    return {"fields_present": sorted(k for k in n if k != "position"), "last_heard": n.get("lastHeard"), "snr": n.get("snr"),
            "hops_away": n.get("hopsAway"), "via_mqtt": n.get("viaMqtt"), "role": (n.get("user") or {}).get("role"),
            "hw_model": (n.get("user") or {}).get("hwModel"), "device_metrics": n.get("deviceMetrics"),
            "environment_metrics": n.get("environmentMetrics")}


@get("/api/telemetry/watch", VIEWER)
def r_telemetry_watch(b, q):
    """GET /api/telemetry/watch: the watch list, whether every node is recorded, and the nodes whose telemetry the radio already holds."""
    tel = b.telemetry
    return {"watched": tel.store.watched(), "all": tel.passive_all(), "heard": tel.heard(), "min_gap_s": tel.passive_gap}


@get("/api/telemetry/export.csv", VIEWER, sensitive=True)
def r_telemetry_export(b, q):
    """GET /api/telemetry/export.csv: all recorded telemetry as a CSV download."""
    return download(b.telemetry.store.export_csv(), "text/csv; charset=utf-8", time.strftime("mesh-telemetry-%Y%m%d-%H%M%S.csv"))


@post("/api/telemetry/watch/add", ADMIN)
def w_watch_add(b, body):
    """POST /api/telemetry/watch/add {node}: start recording a node's telemetry broadcasts."""
    b.telemetry.watch_add(body.get("node"))
    return {"ok": True}


@post("/api/telemetry/watch/remove", ADMIN)
def w_watch_remove(b, body):
    """POST /api/telemetry/watch/remove {node}: stop recording a node's telemetry broadcasts."""
    b.telemetry.watch_remove(body.get("node"))
    return {"ok": True}


@post("/api/telemetry/watch/all", ADMIN)
def w_watch_all(b, body):
    """POST /api/telemetry/watch/all {enabled}: record broadcasts from every node (true) or only watched nodes (false)."""
    if not isinstance(body.get("enabled"), bool):
        raise TelemetryError("enabled must be true or false.")
    b.telemetry.set_passive_all(body["enabled"])
    return {"ok": True}


@post("/api/telemetry/retention", ADMIN)
def w_retention(b, body):
    """POST /api/telemetry/retention {days}: set how many days of readings to keep."""
    b.telemetry.set_retention_days(body.get("days"))
    return {"ok": True, "retention_days": b.telemetry.retention_days()}


@post("/api/telemetry/prune", ADMIN)
def w_prune(b, body):
    """POST /api/telemetry/prune: delete readings older than the retention setting now. Deletes data."""
    return {"deleted": b.telemetry.prune_old()}


# ======================================================================================================================
# the mesh: home, nodes, map, trends, data, activity
# ======================================================================================================================
@get("/api/home", VIEWER)
def r_home(b, q):
    """GET /api/home: the Home page summary of the mesh."""
    return b.mesh.overview()


@get("/api/mesh/nodes", VIEWER)
def r_mesh_nodes(b, q):
    """GET /api/mesh/nodes: the radio's node list with signal and position details; ?all=1 also includes remembered nodes."""
    # ?all=1 adds nodes only our database remembers (the radio forgot them or was cleared)
    return b.mesh.all_nodes() if q.get("all") == "1" else b.mesh.nodes()


@get("/api/mesh/sensors", VIEWER)
def r_sensors(b, q):
    """GET /api/mesh/sensors?hours=: temperature, humidity and pressure heard recently, per node and overall."""
    try:
        hours = float(q.get("hours", 1))
    except ValueError:
        hours = 1.0
    return b.mesh.sensors(hours)


@get("/api/mesh/link", VIEWER)
def r_link(b, q):
    """GET /api/mesh/link?id=&hours=: hourly signal history of one directly heard node."""
    return b.mesh.links(q.get("id", ""), hours=qint(q, "hours", 168))


@get("/api/mesh/linkmap", VIEWER)
def r_linkmap(b, q):
    """GET /api/mesh/linkmap?hours=: every directly heard node with its average signal and distance, for the link-quality map."""
    return b.mesh.link_map(hours=qint(q, "hours", 24))


@get("/api/mesh/hops", VIEWER)
def r_hops(b, q):
    """GET /api/mesh/hops?hours=: packets heard per hour by how many relays they passed through."""
    return b.mesh.hop_series(hours=qint(q, "hours", 24))


@get("/api/mesh/trail", VIEWER)
def r_trail(b, q):
    """GET /api/mesh/trail?id=&days=: where one node has been seen, oldest first."""
    return b.mesh.trail(q.get("id", ""), days=qint(q, "days", 30))


@get("/api/mesh/trails", VIEWER)
def r_trails(b, q):
    """GET /api/mesh/trails?days=: movement trails for every node that has moved."""
    return b.mesh.trails(days=qint(q, "days", 7))


@get("/api/mesh/places", VIEWER)
def r_places(b, q):
    """GET /api/mesh/places: every node with a known position, for the map."""
    return b.mesh.places()


@get("/api/mesh/feed", VIEWER)
def r_feed(b, q):
    """GET /api/mesh/feed: recent happenings on the mesh, newest first. Query: limit, types (comma list), q (search text), before (timestamp)."""
    types = [x for x in (q.get("types") or "").split(",") if x]
    # the search text is capped at 80 characters, the same as inbox.search, to bound the LIKE scan
    return b.mesh.feed(limit=qint(q, "limit", 50), types=types or None, q=(q.get("q") or "")[:80] or None, before=qfloat(q, "before"))


@get("/api/ai/overview", VIEWER)
def r_ai_overview(b, q):
    """GET /api/ai/overview: the AI overview page: model, what people asked and how it went."""
    return b.mesh.ai_overview()


@get("/api/mesh/traffic", VIEWER)
def r_traffic(b, q):
    """GET /api/mesh/traffic?hours=: packets heard per hour by type, and the busiest senders."""
    return b.mesh.traffic(hours=qint(q, "hours", 24))


@get("/api/mesh/samples", VIEWER)
def r_samples(b, q):
    """GET /api/mesh/samples?hours=: stored mesh-health snapshots over time."""
    return b.mesh.samples(hours=qint(q, "hours", 24))


@get("/api/mesh/node", VIEWER)
def r_node(b, q):
    """GET /api/mesh/node?id=: full detail for one node; 404 if the radio has no entry for it."""
    detail = b.mesh.node_detail(q.get("id", ""))
    if not detail:
        raise HttpError(404, "This radio has no entry for that node.")
    return detail


@get("/api/data/overview", VIEWER)
def r_data_overview(b, q):
    """GET /api/data/overview: what the bridge has stored, for the Data page."""
    return b.mesh.data_overview()


@get_re(r"/api/data/export/([a-z_]{1,30})\.csv", VIEWER, sensitive=True)
def r_data_export(b, q, m):
    """GET /api/data/export/<dataset>.csv: one stored dataset as a CSV download; 404 if that dataset is not exportable."""
    text = b.mesh.export_csv(m.group(1))
    if text is None:
        raise HttpError(404, "That dataset can't be exported here.")
    return download(text, "text/csv; charset=utf-8", f'mesh-{m.group(1)}-{time.strftime("%Y%m%d")}.csv')


@get_re(r"/tiles/(\d{1,2})/(\d{1,8})/(\d{1,8})\.png", VIEWER)
def r_tile(b, q, m):
    """GET /tiles/<z>/<x>/<y>.png: a map tile from the local cache (fetched once from the upstream tile server on first use); 404 if unavailable."""
    # map background: from the disk cache, fetched once from openstreetmap.org when first looked at
    # (the regex limits the digits and TileCache.valid() range-checks z/x/y, so nothing odd reaches the file path)
    data = b.tiles.get(*(int(g) for g in m.groups()))
    if data is None:
        return Reply("no tile", "text/plain", code=404)
    return Reply(data, "image/png", {"Cache-Control": "private, max-age=86400"})


@get("/api/tiles/stats", VIEWER)
def r_tile_stats(b, q):
    """GET /api/tiles/stats: how many map tiles are cached, their size and the size cap."""
    return b.tiles.stats()


@post("/api/tiles/clear", ADMIN)
def w_tiles_clear(b, body):
    """POST /api/tiles/clear: delete the cached map tiles (they are fetched again when needed)."""
    return {"deleted": b.tiles.clear()}


@post("/api/settings/dist_unit", ADMIN)
def w_dist_unit(b, body):
    """POST /api/settings/dist_unit {unit}: choose the distance unit shown in the dashboard."""
    b.mesh.set_dist_unit(body.get("unit"))
    return {"ok": True, "unit": b.mesh.dist_unit()}


@post("/api/settings/temp_unit", ADMIN)
def w_temp_unit(b, body):
    """POST /api/settings/temp_unit {unit}: choose the temperature unit shown in the dashboard."""
    b.mesh.set_temp_unit(body.get("unit"))
    return {"ok": True, "unit": b.mesh.temp_unit()}


# ======================================================================================================================
# the radio: position, clock, settings, a different radio
# ======================================================================================================================
@get("/api/radio/position", VIEWER)
def r_radio_position(b, q):
    """GET /api/radio/position: whether the radio has a fixed position and where it believes it is."""
    return b.mesh.radio_position()


@get("/api/radio/config", ADMIN)
def r_radio_config(b, q):
    """GET /api/radio/config: the radio's settings as a form for each section, plus saved backups."""
    return b.radio_config.view()


@get("/api/radio/clock", VIEWER)
def r_radio_clock(b, q):
    """GET /api/radio/clock: what is known about the radio's clock, to tell whether it is really wrong."""
    return b.mesh.clock_report()


@get("/api/radio/change", VIEWER)
def r_radio_change(b, q):
    """GET /api/radio/change: the banner about a different radio being connected, or null when there is nothing to show."""
    return b.mesh.radio_change()


@get("/api/radio/config/backup", ADMIN)
def r_radio_backup(b, q):
    """GET /api/radio/config/backup?id=: one saved radio-settings backup as a JSON download; 404 if it does not exist."""
    r = b.radio_config.get_backup(qint(q, "id", 0))
    if not r:
        raise HttpError(404, "That backup doesn't exist.")
    return download(json.dumps(r, indent=2), "application/json", time.strftime("radio-config-%Y%m%d-%H%M%S.json", time.localtime(r["ts"])))


@post("/api/radio/position", ADMIN)
def w_radio_position(b, body):
    """POST /api/radio/position {lat, lon, alt} or {clear: true}: write a fixed position to the radio, or remove it. Changes the radio; it then shares the position with the mesh."""
    if body.get("clear") is True:             # an explicit true is required, so a missing field can never remove the position
        b.mesh.clear_radio_position()
        return {"ok": True, "cleared": True}
    return {"ok": True, **b.mesh.set_radio_position(body.get("lat"), body.get("lon"), body.get("alt", 0))}


@post("/api/radio/config/pull", ADMIN)
def w_config_pull(b, body):
    """POST /api/radio/config/pull: read the radio's settings and save them as a backup. Does not change the radio."""
    return b.radio_config.pull()


@post("/api/radio/config/save", ADMIN)
def w_config_save(b, body):
    """POST /api/radio/config/save {changes}: write settings to the radio after saving a backup of the old ones. Changes the radio, which restarts."""
    return b.radio_config.save(body.get("changes"))


@post("/api/radio/config/restore", ADMIN)
def w_config_restore(b, body):
    """POST /api/radio/config/restore {id, force}: write a saved backup's settings to the radio. 409 if the backup came from a different radio unless force is true."""
    bid = body.get("id")                      # JSON true would pass an int check, so bools are rejected explicitly
    if not isinstance(bid, int) or isinstance(bid, bool):
        raise ConfigError("Pick a backup to restore.")
    return b.radio_config.restore(bid, force=body.get("force") is True)     # only a literal true overrides the different-radio check


@post("/api/radio/change/dismiss", ADMIN)
def w_change_dismiss(b, body):
    """POST /api/radio/change/dismiss: hide the different-radio banner."""
    b.mesh.dismiss_radio_change()
    return {"ok": True}


@post("/api/radio/time", ADMIN)
def w_radio_time(b, body):
    """POST /api/radio/time: set the radio's clock from this PC. Changes the radio."""
    b.mesh.sync_radio_clock()
    return {"ok": True}


# ======================================================================================================================
# traceroute
# ======================================================================================================================
@get("/api/traceroutes", VIEWER)
def r_traceroutes(b, q):
    """GET /api/traceroutes: stored traceroute results, newest first (limit/before to page)."""
    return b.traceroute.store.list(limit=qint(q, "limit", 30), before=qint(q, "before", None))


@get("/api/traceroute", VIEWER)
def r_traceroute(b, q):
    """GET /api/traceroute?id=: one stored traceroute; 404 if unknown."""
    row = b.traceroute.store.get(qint(q, "id", 0))
    if not row:
        raise HttpError(404, "unknown traceroute")
    return row


@get("/api/traceroute/request", VIEWER)
def r_traceroute_state(b, q):
    """GET /api/traceroute/request?id=: progress of a traceroute started from the page; 404 if unknown or aged out."""
    rid = q.get("id", "")                     # isdigit() guards int(): a bad id is a 404, not a server error
    st = b.traceroute.manual_state(int(rid)) if rid.isdigit() else None
    if not st:
        raise HttpError(404, "unknown request")
    return st


@post("/api/traceroute/request", ADMIN)
def w_traceroute(b, body):
    """POST /api/traceroute/request {node, hop_limit}: start a traceroute to a node in the background. Transmits over the radio; poll the GET route for the result."""
    return {"id": b.traceroute.start_manual(body.get("node"), body.get("hop_limit", 7))}


# ======================================================================================================================
# what is new, search, your labels and snippets
# ======================================================================================================================
@get("/api/unread", VIEWER)
def r_unread(b, q):
    """GET /api/unread?channel=&dm=: counts of new channel posts and DMs since the ids the page last saw, plus current alerts."""
    return inbox.unread(b, qint(q, "channel", None), qint(q, "dm", None))


@get("/api/search", VIEWER)
def r_search(b, q):
    """GET /api/search?q=: search nodes, labels, direct messages, AI conversations and channel posts."""
    return inbox.search(b, q.get("q", ""))


@post("/api/notes/set", ADMIN)
def w_note(b, body):
    """POST /api/notes/set {node, label, note, starred}: set your own label, note or star for a node. Local only; never sent over the radio."""
    return {"note": b.userdata.set_note(body.get("node"), body.get("label"), body.get("note"), body.get("starred"))}


@get("/api/snippets", VIEWER)
def r_snippets(b, q):
    """GET /api/snippets: saved text snippets, optionally only for one target ('channel' or 'dm')."""
    return {"snippets": b.userdata.snippets(q.get("target") or None)}


@post("/api/snippets/add", ADMIN)
def w_snippet_add(b, body):
    """POST /api/snippets/add {target, text, label}: save a snippet. Local only; it is only ever put in the message box, not sent."""
    return {"id": b.userdata.add_snippet(body.get("target"), body.get("text"), body.get("label"))}


@post("/api/snippets/delete", ADMIN)
def w_snippet_delete(b, body):
    """POST /api/snippets/delete {id}: delete a saved snippet."""
    return {"deleted": b.userdata.delete_snippet(body.get("id"))}


# ======================================================================================================================
# backups
# ======================================================================================================================
@get("/api/backups", ADMIN)
def r_backups(b, q):
    """GET /api/backups: the database backups, the auto-backup setting, any staged restore, and the folder paths."""
    return {"auto": b.backups.auto_enabled(), "items": b.backups.list(), "staged": b.backups.staged(), "folder": str(b.backups.dir),
            "database": str(b.backups.db_path)}


@post("/api/backups/create", ADMIN)
def w_backup_create(b, body):
    """POST /api/backups/create: make a manual backup of the database now."""
    return {"name": b.backups.create("manual")}


@post("/api/backups/auto", ADMIN)
def w_backup_auto(b, body):
    """POST /api/backups/auto {enabled}: turn the daily automatic backup on or off."""
    b.backups.set_auto(body.get("enabled"))
    return {"auto": b.backups.auto_enabled()}


@post("/api/backups/delete", ADMIN)
def w_backup_delete(b, body):
    """POST /api/backups/delete {name}: delete one backup file. Deletes data."""
    b.backups.delete(body.get("name"))
    return {"ok": True}


@get("/api/backups/download", ADMIN)
def r_backup_download(b, q):
    """GET /api/backups/download[?name=]: a saved backup, or a fresh snapshot of the live database if no name is given, as a download (contains message text)."""
    name = q.get("name")
    if name:                                  # path_of() only accepts names of real backup files, so this cannot read other paths
        data =b.backups.path_of(name).read_bytes()
    else:
        data, name = b.backups.snapshot_bytes(), time.strftime("mesh-llm-%Y%m%d-%H%M%S.db")
    return download(data, "application/octet-stream", name)


@post_raw("/api/backups/restore", ADMIN)
def w_backup_upload(b, handler):
    """POST /api/backups/restore (raw upload): receive a database file and stage it for restore at the next start. Does not replace the live data yet."""
    # the size limit and validation live in stage_upload(); a missing or bad length is treated as "no file"
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        length = 0
    return {"ok": True, **b.backups.stage_upload(handler.rfile, length)}


@post("/api/backups/restore/existing", ADMIN)
def w_backup_stage(b, body):
    """POST /api/backups/restore/existing {name}: stage a saved backup for restore at the next start."""
    return {"ok": True, **b.backups.stage_file(b.backups.path_of(body.get("name")))}


@post("/api/backups/restore/cancel", ADMIN)
def w_backup_cancel(b, body):
    """POST /api/backups/restore/cancel: discard the staged restore."""
    return {"cancelled": b.backups.cancel_staged()}


# ======================================================================================================================
# diagnostics and logs, coverage, the written report
# ======================================================================================================================
@get("/api/diagnostics", VIEWER, sensitive=True)
def r_diagnostics(b, q):
    """GET /api/diagnostics: the diagnostics checklist (each check ok/warn/bad, plus the worst status overall). Read-only."""
    return b.diagnostics.report()


@get("/api/logs", VIEWER, sensitive=True)
def r_logs(b, q):
    """GET /api/logs?which=&lines=: the end of the bridge's 'out' or 'err' log file."""
    return b.diagnostics.logs(q.get("which", "out"), qint(q, "lines", 200))


@get("/api/coverage", VIEWER)
def r_coverage(b, q):
    """GET /api/coverage?days=: range and signal statistics for directly heard nodes. Passive; reads the database only."""
    return b.coverage.overview(qint(q, "days", 30))


@get("/api/coverage/walk", VIEWER)
def r_walk(b, q):
    """GET /api/coverage/walk: the running walk test (if any) and the saved ones."""
    return b.coverage.state()


@get("/api/coverage/walk/session", VIEWER)
def r_walk_session(b, q):
    """GET /api/coverage/walk/session?id=: the samples and summary of one saved walk test; 404 if unknown."""
    s = b.coverage.session(qint(q, "id", 0))
    if s is None:
        raise HttpError(404, "That walk test doesn't exist.")
    return s


@post("/api/coverage/walk/start", ADMIN)
def w_walk_start(b, body):
    """POST /api/coverage/walk/start {node}: start recording the signal of one moving node. Passive; transmits nothing."""
    return {"id": b.coverage.start_walk(body.get("node"))}


@post("/api/coverage/walk/stop", ADMIN)
def w_walk_stop(b, body):
    """POST /api/coverage/walk/stop: end the running walk test."""
    return {"stopped": b.coverage.stop_walk()}


@post("/api/coverage/walk/delete", ADMIN)
def w_walk_delete(b, body):
    """POST /api/coverage/walk/delete {id}: delete a saved walk test and its samples. Deletes data."""
    return {"deleted": b.coverage.delete_session(body.get("id"))}


@get("/api/report", VIEWER, sensitive=True)
def r_report(b, q):
    """GET /api/report?days=: the written mesh and AI report for the last 1, 7 or 30 days, as JSON with the Markdown inside."""
    return report.build(b, qint(q, "days", 7))


@get("/api/report.md", VIEWER, sensitive=True)
def r_report_md(b, q):
    """GET /api/report.md?days=: the same report as a Markdown file download."""
    r = report.build(b, qint(q, "days", 7))
    return download(r["markdown"], "text/markdown; charset=utf-8", time.strftime(f"mesh-report-{r['days']}d-%Y%m%d.md"))


# ======================================================================================================================
# evaluation results and the project's write-ups
# ======================================================================================================================
@get("/api/evals", VIEWER)
def r_evals(b, q):
    """GET /api/evals: saved evaluation results and the list of project write-ups. Read-only."""
    return evals.overview()


@get("/api/docs", VIEWER)
def r_docs(b, q):
    """GET /api/docs?name=: the text of one project write-up; 404 if the name is not a plain existing document."""
    name = q.get("name", "")
    text = evals.read_doc(name)
    if text is None:
        raise HttpError(404, "There is no such document.")
    return {"name": name, "text": text}
