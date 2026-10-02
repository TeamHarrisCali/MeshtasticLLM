"""The dashboard's JSON API as a route table.

Each route is a small function registered with a decorator:

    @get("/api/status")                     # exact path; the function gets (bridge, query) and returns JSON data
    def status(b, q): return b.status()

    @post("/api/pause")                     # the function gets (bridge, body) where body is a JSON object
    def pause(b, body): ...

    @get_re(r"/api/data/export/([a-z_]+)\\.csv")   # a pattern; the function also gets the regex match

A function returns data to send as JSON, a `Reply` for anything else (a download, an image), or raises `HttpError`
for an error the page should show. Errors the operator can cause (a bad value, a busy radio) are turned into a 400
with their message by the server, so a route only needs to raise them. `webui.py` does the HTTP side (security
checks, static files) and looks routes up here.
"""
import json
import time

import actions
import evals
import inbox
import report
from channel import ChannelError
from mesh import PositionError
from ollama_models import OllamaError
from radio_config import ConfigError, RadioMismatch
from telemetry import KINDS as TELEMETRY_KINDS, METRICS as TELEMETRY_METRICS, TelemetryError
from traceroute import TracerouteError

GET, POST, GET_RE, POST_RAW = {}, {}, [], {}
USER_ERRORS = (ValueError, ChannelError, TracerouteError, TelemetryError, OllamaError, PositionError, ConfigError)


class HttpError(Exception):
    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


class Reply:
    """A non-JSON answer: bytes or text with a content type and optional headers (for example a file download)."""

    def __init__(self, body, ctype="application/json", headers=None, code=200):
        self.body, self.ctype, self.headers, self.code = body, ctype, headers or {}, code


def download(body, ctype, filename):
    return Reply(body, ctype, {"Content-Disposition": f'attachment; filename="{filename}"'})


def get(path):
    def reg(fn):
        GET[path] = fn
        return fn
    return reg


def get_re(pattern):
    import re
    rx = re.compile(pattern)

    def reg(fn):
        GET_RE.append((rx, fn))
        return fn
    return reg


def post(path):
    def reg(fn):
        POST[path] = fn
        return fn
    return reg


def post_raw(path):
    """A POST that is not JSON (a file upload). The function gets (bridge, handler) and reads handler.rfile itself."""
    def reg(fn):
        POST_RAW[path] = fn
        return fn
    return reg


def qint(qs, key, default):
    """An integer query parameter; anything else (missing, 'abc', '1e9', '') falls back to the default."""
    try:
        return max(-2 ** 62, min(int(qs.get(key, default)), 2 ** 62))  # keep it inside SQLite's integer range
    except (TypeError, ValueError):
        return default


def qfloat(qs, key):
    try:
        v = float(qs.get(key))
        return v if v == v and abs(v) < 1e15 else None    # no NaN / infinity
    except (TypeError, ValueError):
        return None


def error_reply(e):
    """(status code, JSON dict) for an exception a POST route raised, or None if it is not one the operator should see."""
    if isinstance(e, HttpError):
        return e.code, {"error": e.message, **e.extra}
    if isinstance(e, RadioMismatch):
        return 409, {"error": str(e), "mismatch": True}
    if isinstance(e, USER_ERRORS):
        return 400, {"error": str(e)}
    return None


# ======================================================================================================================
# status, the AI log, conversations
# ======================================================================================================================
@get("/api/status")
def r_status(b, q):
    return b.status()


@get("/api/stats")
def r_stats(b, q):
    return b.audit.stats()


@get("/api/requests")
def r_requests(b, q):
    return b.audit.list(limit=qint(q, "limit", 50), before=qint(q, "before", None), q=q.get("q") or None, status=q.get("status") or None)


@get("/api/conversations")
def r_conversations(b, q):
    return b.conversations(q.get("scope") or None)


@get("/api/conversation")
def r_conversation(b, q):
    node = q.get("node", "")
    return {"messages": b.audit.conversation(node, qint(q, "limit", 200), scope=q.get("scope") or None), "memory": len(b.history(node)), "access": b.node_access_info(node)}


@get("/api/export.csv")
def r_export(b, q):
    return download(b.audit.export_csv(), "text/csv; charset=utf-8", time.strftime("mesh-llm-audit-%Y%m%d-%H%M%S.csv"))


@get("/api/queue")
def r_queue(b, q):
    return b.queue_snapshot()


@get("/api/access")
def r_access(b, q):
    return b.access_overview()


@get("/api/nodes")
def r_known_nodes(b, q):
    return b.known_nodes()


@get("/api/actions")
def r_actions(b, q):
    return [{"name": a.name, "description": a.description, "tier": a.tier, "params": list(a.params)} for a in actions.ACTIONS.values()]


@get("/api/models")
def r_models(b, q):
    return b.models_overview()


@post("/api/pause")
def w_pause(b, body):
    b.paused = bool(body.get("paused"))
    print(f"[web] bot {'paused' if b.paused else 'resumed'}")
    return {"paused": b.paused}


@post("/api/send")
def w_send(b, body):
    return {"id": b.send_manual(body.get("node"), body.get("text"))}


@post("/api/ai/ask")
def w_ask(b, body):
    return {"id": b.ask_web(body.get("prompt"))}


@post("/api/memory/clear")
def w_memory_clear(b, body):
    node = str(body.get("node") or "")
    b.audit.clear_memory(node)
    print(f"[memory] cleared for {node} (web)")
    return {"ok": True}


@post("/api/access/mode")
def w_access_mode(b, body):
    b.set_mode(body.get("mode"))
    return {"ok": True}


@post("/api/access/default_cap")
def w_access_cap(b, body):
    b.set_default_cap(body.get("cap"))
    return {"ok": True}


@post("/api/access/node")
def w_access_node(b, body):
    b.set_node_access(body.get("node"), **{k: body[k] for k in ("access", "daily_cap", "max_tier", "pin_key") if k in body})
    return {"ok": True}


@post("/api/queue/cancel")
def w_queue_cancel(b, body):
    rid = body.get("id")
    if not isinstance(rid, int) or not b.cancel(rid):
        raise HttpError(409, "That question is no longer waiting")
    return {"ok": True}


@post("/api/model")
def w_model(b, body):
    m = b.set_model(body.get("model"))
    return {"ok": True, "tools": m["tools"]}


@post("/api/models/pull")
def w_pull(b, body):
    b.models.start_pull(body.get("name"))
    return {"ok": True}


@post("/api/models/pull/cancel")
def w_pull_cancel(b, body):
    b.models.cancel_pull(body.get("name"))
    return {"ok": True}


# ======================================================================================================================
# the public channel
# ======================================================================================================================
@get("/api/channel")
def r_channel(b, q):
    return {"info": b.channel.info(), "messages": b.channel.list(qint(q, "limit", 100), after=qint(q, "after", None))}


@post("/api/channel/post")
def w_channel_post(b, body):
    return {"id": b.channel.post(body.get("text"))}


@post("/api/channel/clear")
def w_channel_clear(b, body):
    return {"deleted": b.channel.clear()}


# ======================================================================================================================
# telemetry
# ======================================================================================================================
@get("/api/telemetry")
def r_telemetry(b, q):
    tel = b.telemetry
    return {"readings": tel.store.list(node=q.get("node") or None, kind=q.get("kind") or None, status=q.get("status") or None,
                                       source=q.get("source") or None, limit=qint(q, "limit", 100), before=qint(q, "before", None)),
            "latest": tel.store.latest(), "stats": tel.store.stats(), "kinds": list(TELEMETRY_KINDS),
            "metrics": TELEMETRY_METRICS, "retention_days": tel.retention_days(), "record_all": tel.passive_all()}


@get("/api/telemetry/node")
def r_telemetry_node(b, q):
    # What this radio holds about one node - for working out why a node isn't showing telemetry. Position data is left out on purpose.
    n = (getattr(b.iface, "nodes", None) or {}).get(q.get("id", ""))
    if not n:
        raise HttpError(404, "This radio has no entry for that node.")
    return {"fields_present": sorted(k for k in n if k != "position"), "last_heard": n.get("lastHeard"), "snr": n.get("snr"),
            "hops_away": n.get("hopsAway"), "via_mqtt": n.get("viaMqtt"), "role": (n.get("user") or {}).get("role"),
            "hw_model": (n.get("user") or {}).get("hwModel"), "device_metrics": n.get("deviceMetrics"),
            "environment_metrics": n.get("environmentMetrics")}


@get("/api/telemetry/watch")
def r_telemetry_watch(b, q):
    tel = b.telemetry
    return {"watched": tel.store.watched(), "all": tel.passive_all(), "heard": tel.heard(), "min_gap_s": tel.passive_gap}


@get("/api/telemetry/export.csv")
def r_telemetry_export(b, q):
    return download(b.telemetry.store.export_csv(), "text/csv; charset=utf-8", time.strftime("mesh-telemetry-%Y%m%d-%H%M%S.csv"))


@post("/api/telemetry/watch/add")
def w_watch_add(b, body):
    b.telemetry.watch_add(body.get("node"))
    return {"ok": True}


@post("/api/telemetry/watch/remove")
def w_watch_remove(b, body):
    b.telemetry.watch_remove(body.get("node"))
    return {"ok": True}


@post("/api/telemetry/watch/all")
def w_watch_all(b, body):
    if not isinstance(body.get("enabled"), bool):
        raise TelemetryError("enabled must be true or false.")
    b.telemetry.set_passive_all(body["enabled"])
    return {"ok": True}


@post("/api/telemetry/retention")
def w_retention(b, body):
    b.telemetry.set_retention_days(body.get("days"))
    return {"ok": True, "retention_days": b.telemetry.retention_days()}


@post("/api/telemetry/prune")
def w_prune(b, body):
    return {"deleted": b.telemetry.prune_old()}


# ======================================================================================================================
# the mesh: home, nodes, map, trends, data, activity
# ======================================================================================================================
@get("/api/home")
def r_home(b, q):
    return b.mesh.overview()


@get("/api/mesh/nodes")
def r_mesh_nodes(b, q):
    # ?all=1 adds nodes only our database remembers (the radio forgot them or was cleared)
    return b.mesh.all_nodes() if q.get("all") == "1" else b.mesh.nodes()


@get("/api/mesh/sensors")
def r_sensors(b, q):
    try:
        hours = float(q.get("hours", 1))
    except ValueError:
        hours = 1.0
    return b.mesh.sensors(hours)


@get("/api/mesh/link")
def r_link(b, q):
    return b.mesh.links(q.get("id", ""), hours=qint(q, "hours", 168))


@get("/api/mesh/linkmap")
def r_linkmap(b, q):
    return b.mesh.link_map(hours=qint(q, "hours", 24))


@get("/api/mesh/hops")
def r_hops(b, q):
    return b.mesh.hop_series(hours=qint(q, "hours", 24))


@get("/api/mesh/trail")
def r_trail(b, q):
    return b.mesh.trail(q.get("id", ""), days=qint(q, "days", 30))


@get("/api/mesh/trails")
def r_trails(b, q):
    return b.mesh.trails(days=qint(q, "days", 7))


@get("/api/mesh/places")
def r_places(b, q):
    return b.mesh.places()


@get("/api/mesh/feed")
def r_feed(b, q):
    types = [x for x in (q.get("types") or "").split(",") if x]
    return b.mesh.feed(limit=qint(q, "limit", 50), types=types or None, q=(q.get("q") or "")[:80] or None, before=qfloat(q, "before"))


@get("/api/ai/overview")
def r_ai_overview(b, q):
    return b.mesh.ai_overview()


@get("/api/mesh/traffic")
def r_traffic(b, q):
    return b.mesh.traffic(hours=qint(q, "hours", 24))


@get("/api/mesh/samples")
def r_samples(b, q):
    return b.mesh.samples(hours=qint(q, "hours", 24))


@get("/api/mesh/node")
def r_node(b, q):
    detail = b.mesh.node_detail(q.get("id", ""))
    if not detail:
        raise HttpError(404, "This radio has no entry for that node.")
    return detail


@get("/api/data/overview")
def r_data_overview(b, q):
    return b.mesh.data_overview()


@get_re(r"/api/data/export/([a-z_]{1,30})\.csv")
def r_data_export(b, q, m):
    text = b.mesh.export_csv(m.group(1))
    if text is None:
        raise HttpError(404, "That dataset can't be exported here.")
    return download(text, "text/csv; charset=utf-8", f'mesh-{m.group(1)}-{time.strftime("%Y%m%d")}.csv')


@get_re(r"/tiles/(\d{1,2})/(\d{1,8})/(\d{1,8})\.png")
def r_tile(b, q, m):
    # map background: from the disk cache, fetched once from openstreetmap.org when first looked at
    data = b.tiles.get(*(int(g) for g in m.groups()))
    if data is None:
        return Reply("no tile", "text/plain", code=404)
    return Reply(data, "image/png", {"Cache-Control": "public, max-age=86400"})


@get("/api/tiles/stats")
def r_tile_stats(b, q):
    return b.tiles.stats()


@post("/api/tiles/clear")
def w_tiles_clear(b, body):
    return {"deleted": b.tiles.clear()}


@post("/api/settings/dist_unit")
def w_dist_unit(b, body):
    b.mesh.set_dist_unit(body.get("unit"))
    return {"ok": True, "unit": b.mesh.dist_unit()}


@post("/api/settings/temp_unit")
def w_temp_unit(b, body):
    b.mesh.set_temp_unit(body.get("unit"))
    return {"ok": True, "unit": b.mesh.temp_unit()}


# ======================================================================================================================
# the radio: position, clock, settings, a different radio
# ======================================================================================================================
@get("/api/radio/position")
def r_radio_position(b, q):
    return b.mesh.radio_position()


@get("/api/radio/config")
def r_radio_config(b, q):
    return b.radio_config.view()


@get("/api/radio/clock")
def r_radio_clock(b, q):
    return b.mesh.clock_report()


@get("/api/radio/change")
def r_radio_change(b, q):
    return b.mesh.radio_change()


@get("/api/radio/config/backup")
def r_radio_backup(b, q):
    r = b.radio_config.get_backup(qint(q, "id", 0))
    if not r:
        raise HttpError(404, "That backup doesn't exist.")
    return download(json.dumps(r, indent=2), "application/json", time.strftime("radio-config-%Y%m%d-%H%M%S.json", time.localtime(r["ts"])))


@post("/api/radio/position")
def w_radio_position(b, body):
    if body.get("clear") is True:
        b.mesh.clear_radio_position()
        return {"ok": True, "cleared": True}
    return {"ok": True, **b.mesh.set_radio_position(body.get("lat"), body.get("lon"), body.get("alt", 0))}


@post("/api/radio/config/pull")
def w_config_pull(b, body):
    return b.radio_config.pull()


@post("/api/radio/config/save")
def w_config_save(b, body):
    return b.radio_config.save(body.get("changes"))


@post("/api/radio/config/restore")
def w_config_restore(b, body):
    bid = body.get("id")
    if not isinstance(bid, int) or isinstance(bid, bool):
        raise ConfigError("Pick a backup to restore.")
    return b.radio_config.restore(bid, force=body.get("force") is True)


@post("/api/radio/change/dismiss")
def w_change_dismiss(b, body):
    b.mesh.dismiss_radio_change()
    return {"ok": True}


@post("/api/radio/time")
def w_radio_time(b, body):
    b.mesh.sync_radio_clock()
    return {"ok": True}


# ======================================================================================================================
# traceroute
# ======================================================================================================================
@get("/api/traceroutes")
def r_traceroutes(b, q):
    return b.traceroute.store.list(limit=qint(q, "limit", 30), before=qint(q, "before", None))


@get("/api/traceroute")
def r_traceroute(b, q):
    row = b.traceroute.store.get(qint(q, "id", 0))
    if not row:
        raise HttpError(404, "unknown traceroute")
    return row


@get("/api/traceroute/request")
def r_traceroute_state(b, q):
    rid = q.get("id", "")
    st = b.traceroute.manual_state(int(rid)) if rid.isdigit() else None
    if not st:
        raise HttpError(404, "unknown request")
    return st


@post("/api/traceroute/request")
def w_traceroute(b, body):
    return {"id": b.traceroute.start_manual(body.get("node"), body.get("hop_limit", 7))}


# ======================================================================================================================
# what is new, search, your labels and snippets
# ======================================================================================================================
@get("/api/unread")
def r_unread(b, q):
    return inbox.unread(b, qint(q, "channel", None), qint(q, "dm", None))


@get("/api/search")
def r_search(b, q):
    return inbox.search(b, q.get("q", ""))


@post("/api/notes/set")
def w_note(b, body):
    return {"note": b.userdata.set_note(body.get("node"), body.get("label"), body.get("note"), body.get("starred"))}


@get("/api/snippets")
def r_snippets(b, q):
    return {"snippets": b.userdata.snippets(q.get("target") or None)}


@post("/api/snippets/add")
def w_snippet_add(b, body):
    return {"id": b.userdata.add_snippet(body.get("target"), body.get("text"), body.get("label"))}


@post("/api/snippets/delete")
def w_snippet_delete(b, body):
    return {"deleted": b.userdata.delete_snippet(body.get("id"))}


# ======================================================================================================================
# backups
# ======================================================================================================================
@get("/api/backups")
def r_backups(b, q):
    return {"auto": b.backups.auto_enabled(), "items": b.backups.list(), "staged": b.backups.staged(), "folder": str(b.backups.dir),
            "database": str(b.backups.db_path)}


@post("/api/backups/create")
def w_backup_create(b, body):
    return {"name": b.backups.create("manual")}


@post("/api/backups/auto")
def w_backup_auto(b, body):
    b.backups.set_auto(body.get("enabled"))
    return {"auto": b.backups.auto_enabled()}


@post("/api/backups/delete")
def w_backup_delete(b, body):
    b.backups.delete(body.get("name"))
    return {"ok": True}


@get("/api/backups/download")
def r_backup_download(b, q):
    name = q.get("name")
    if name:
        data = b.backups.path_of(name).read_bytes()
    else:
        data, name = b.backups.snapshot_bytes(), time.strftime("mesh-llm-%Y%m%d-%H%M%S.db")
    return download(data, "application/octet-stream", name)


@post_raw("/api/backups/restore")
def w_backup_upload(b, handler):
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        length = 0
    return {"ok": True, **b.backups.stage_upload(handler.rfile, length)}


@post("/api/backups/restore/existing")
def w_backup_stage(b, body):
    return {"ok": True, **b.backups.stage_file(b.backups.path_of(body.get("name")))}


@post("/api/backups/restore/cancel")
def w_backup_cancel(b, body):
    return {"cancelled": b.backups.cancel_staged()}


# ======================================================================================================================
# diagnostics and logs, coverage, the written report
# ======================================================================================================================
@get("/api/diagnostics")
def r_diagnostics(b, q):
    return b.diagnostics.report()


@get("/api/logs")
def r_logs(b, q):
    return b.diagnostics.logs(q.get("which", "out"), qint(q, "lines", 200))


@get("/api/coverage")
def r_coverage(b, q):
    return b.coverage.overview(qint(q, "days", 30))


@get("/api/coverage/walk")
def r_walk(b, q):
    return b.coverage.state()


@get("/api/coverage/walk/session")
def r_walk_session(b, q):
    s = b.coverage.session(qint(q, "id", 0))
    if s is None:
        raise HttpError(404, "That walk test doesn't exist.")
    return s


@post("/api/coverage/walk/start")
def w_walk_start(b, body):
    return {"id": b.coverage.start_walk(body.get("node"))}


@post("/api/coverage/walk/stop")
def w_walk_stop(b, body):
    return {"stopped": b.coverage.stop_walk()}


@post("/api/coverage/walk/delete")
def w_walk_delete(b, body):
    return {"deleted": b.coverage.delete_session(body.get("id"))}


@get("/api/report")
def r_report(b, q):
    return report.build(b, qint(q, "days", 7))


@get("/api/report.md")
def r_report_md(b, q):
    r = report.build(b, qint(q, "days", 7))
    return download(r["markdown"], "text/markdown; charset=utf-8", time.strftime(f"mesh-report-{r['days']}d-%Y%m%d.md"))


# ======================================================================================================================
# evaluation results and the project's write-ups
# ======================================================================================================================
@get("/api/evals")
def r_evals(b, q):
    return evals.overview()


@get("/api/docs")
def r_docs(b, q):
    name = q.get("name", "")
    text = evals.read_doc(name)
    if text is None:
        raise HttpError(404, "There is no such document.")
    return {"name": name, "text": text}
