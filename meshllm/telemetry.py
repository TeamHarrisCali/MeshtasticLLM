"""Telemetry the radio hears on its own, kept in the database with timestamps and pruned after a while.

Nodes with the telemetry module switched on broadcast their battery, voltage, channel use, sensors, ...
every so often, and the radio hears them for free. Nothing here transmits or asks anyone anything.

Table `telemetry` has one row per broadcast heard. By default every node's broadcasts are recorded
(setting `telemetry_passive_all`); turn that off to record only the nodes on the watch list. Rows older
than the retention setting (`telemetry_retention_days`, default 30, 0 = keep forever) are deleted by a
background job. (Older databases may still hold rows from the removed "ask a node" feature: same table,
`source` = manual/job. They are pruned like the rest.)
"""
import csv
import io
import json
import re
import threading
import time

from meshllm.audit import csv_cell

NODE_ID_RE = re.compile(r"^![0-9a-f]{8}$")
DEFAULT_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 3650

# our name -> key in the decoded telemetry dict
KINDS = {
    "device": "deviceMetrics",
    "environment": "environmentMetrics",
    "air_quality": "airQualityMetrics",
    "power": "powerMetrics",
    "local_stats": "localStats",
}
# reply key -> column (the rest of a reply is kept in `raw`)
METRIC_COLUMNS = {
    "batteryLevel": "battery_level", "voltage": "voltage", "channelUtilization": "channel_utilization",
    "airUtilTx": "air_util_tx", "uptimeSeconds": "uptime_seconds", "temperature": "temperature",
    "relativeHumidity": "relative_humidity", "barometricPressure": "barometric_pressure",
    "gasResistance": "gas_resistance", "iaq": "iaq",
}
COLUMNS = ["id", "ts", "node_id", "node_name", "kind", "source", "job_id", "status", "detail", "latency_ms",
           "node_time", *METRIC_COLUMNS.values(), "rx_snr", "rx_rssi", "hops", "raw"]
METRICS = list(METRIC_COLUMNS.values())

SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,      -- when the radio heard it, seconds since 1970
    node_id       TEXT NOT NULL,
    node_name     TEXT,
    kind          TEXT NOT NULL,      -- device | environment | air_quality | power | local_stats
    source        TEXT NOT NULL,      -- broadcast (heard) | manual | job (older rows from the removed request feature)
    job_id        INTEGER,
    status        TEXT NOT NULL,      -- ok (broadcasts are always ok)
    detail        TEXT,
    latency_ms    INTEGER,
    node_time     REAL,               -- the node's own timestamp, when it sends one (often unset)
    battery_level REAL, voltage REAL, channel_utilization REAL, air_util_tx REAL, uptime_seconds REAL,
    temperature REAL, relative_humidity REAL, barometric_pressure REAL, gas_resistance REAL, iaq REAL,
    rx_snr        REAL,               -- signal-to-noise ratio it arrived with
    rx_rssi       INTEGER,
    hops          INTEGER,            -- hops it travelled to reach us
    raw           TEXT                -- everything the node sent, as JSON
);
CREATE INDEX IF NOT EXISTS idx_telemetry_node_ts ON telemetry(node_id, ts);
CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry(ts);
CREATE TABLE IF NOT EXISTS telemetry_watch (   -- nodes to record when 'record every node' is off
    node_id TEXT PRIMARY KEY,
    added   REAL NOT NULL
);
"""


class TelemetryError(Exception):
    """Something the operator should see (bad input)."""


def _norm(node_id):
    """Node id from a request body, lower-cased; anything that isn't a string becomes '' (and fails validation)."""
    return node_id.strip().lower() if isinstance(node_id, str) else ""


def parse_reply(decoded):
    """Decoded telemetry dict -> (kind, {column: value}, metrics dict, node_time) or None."""
    for kind, key in KINDS.items():
        if key in decoded:
            metrics = decoded[key] or {}
            cols = {METRIC_COLUMNS[k]: v for k, v in metrics.items()
                    if k in METRIC_COLUMNS and isinstance(v, (int, float)) and not isinstance(v, bool)}
            return kind, cols, metrics, decoded.get("time")
    return None


class TelemetryStore:
    """SQL side. Shares the audit database (and its lock)."""

    def __init__(self, audit):
        self.audit = audit
        with audit.lock:
            audit.db.executescript(SCHEMA)
            audit.db.commit()

    def add(self, ts, node_id, node_name, kind, source, status, job_id=None, detail=None, latency_ms=None,
            node_time=None, values=None, rx_snr=None, rx_rssi=None, hops=None, raw=None):
        """Insert one reading and return its row id. `values` is {column: number} from parse_reply; `raw` is stored as JSON text."""
        # column names come only from this method and METRIC_COLUMNS (never from the mesh), so building the SQL text here is safe
        cols = {"ts": ts, "node_id": node_id, "node_name": node_name, "kind": kind, "source": source,
                "job_id": job_id, "status": status, "detail": detail, "latency_ms": latency_ms,
                "node_time": node_time, "rx_snr": rx_snr, "rx_rssi": rx_rssi, "hops": hops,
                "raw": json.dumps(raw, sort_keys=True, default=str) if raw is not None else None, **(values or {})}
        names = ", ".join(cols)
        with self.audit.lock:
            cur = self.audit.db.execute(f"INSERT INTO telemetry ({names}) VALUES ({', '.join('?' * len(cols))})",
                                        list(cols.values()))
            self.audit.db.commit()
            return cur.lastrowid

    def get(self, rid):
        """One reading by row id as a dict, or None."""
        with self.audit.lock:
            row = self.audit.db.execute("SELECT * FROM telemetry WHERE id=?", (rid,)).fetchone()
        return dict(row) if row else None

    def list(self, node=None, kind=None, status=None, limit=100, before=None, since=None, source=None):
        """Readings newest first, filtered by any of the given columns. `before` is a row id (for paging), `since` a unix
        timestamp; `limit` is clamped to 1..1000. Unset filters are ignored."""
        where, args = [], []
        for col, val in (("node_id", node), ("kind", kind), ("status", status), ("source", source)):
            if val:
                where.append(f"{col} = ?")
                args.append(val)
        if before:
            where.append("id < ?")
            args.append(int(before))
        if since:
            where.append("ts >= ?")
            args.append(float(since))
        sql = "SELECT * FROM telemetry" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        with self.audit.lock:
            return [dict(r) for r in self.audit.db.execute(sql, args)]

    def latest(self):
        """The newest successful reading for every node and kind."""
        # "newest" is the highest row id (insertion order), not the highest ts
        with self.audit.lock:
            rows = self.audit.db.execute(
                "SELECT t.* FROM telemetry t JOIN (SELECT node_id, kind, MAX(id) AS mid FROM telemetry "
                "WHERE status='ok' GROUP BY node_id, kind) m ON t.id = m.mid ORDER BY t.node_id, t.kind").fetchall()
        return [dict(r) for r in rows]

    def stats(self):
        """Row counts (total, ok, failed, distinct nodes) and the oldest/newest timestamps; the timestamps are None when empty."""
        with self.audit.lock:
            one = lambda sql: self.audit.db.execute(sql).fetchone()[0]
            return {"total": one("SELECT COUNT(*) FROM telemetry"),
                    "ok": one("SELECT COUNT(*) FROM telemetry WHERE status='ok'"),
                    "failed": one("SELECT COUNT(*) FROM telemetry WHERE status!='ok'"),
                    "nodes": one("SELECT COUNT(DISTINCT node_id) FROM telemetry"),
                    "oldest_ts": one("SELECT MIN(ts) FROM telemetry"), "newest_ts": one("SELECT MAX(ts) FROM telemetry")}

    def prune(self, older_than_ts):
        """Delete readings older than a timestamp. Returns how many were deleted."""
        with self.audit.lock:
            n = self.audit.db.execute("DELETE FROM telemetry WHERE ts < ?", (older_than_ts,)).rowcount
            self.audit.db.commit()
        return n

    def export_csv(self):
        """Every reading as CSV text with the COLUMNS header. Reads the database only."""
        with self.audit.lock:
            rows = self.audit.db.execute("SELECT * FROM telemetry ORDER BY id").fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(COLUMNS)
        for r in rows:  # neutralise spreadsheet formulas in text that came from the mesh
            w.writerow([csv_cell(r[c]) for c in COLUMNS])
        return buf.getvalue()

    # ---- watch list ------------------------------------------------------
    def watch_add(self, node_id, now):
        """Put a node on the watch list (no-op if already there). `now` is the time to record as when it was added; the id is not validated here."""
        with self.audit.lock:
            self.audit.db.execute("INSERT OR IGNORE INTO telemetry_watch (node_id, added) VALUES (?, ?)", (node_id, now))
            self.audit.db.commit()

    def watch_remove(self, node_id):
        """Take a node off the watch list. Returns True if it was on it."""
        with self.audit.lock:
            n = self.audit.db.execute("DELETE FROM telemetry_watch WHERE node_id=?", (node_id,)).rowcount
            self.audit.db.commit()
        return n > 0

    def is_watched(self, node_id):
        """True if the node is on the watch list."""
        with self.audit.lock:
            return self.audit.db.execute("SELECT 1 FROM telemetry_watch WHERE node_id=?", (node_id,)).fetchone() is not None

    def watched(self):
        """Watch list, oldest entry first, each with the latest known name, how many broadcasts were recorded and when the last one was heard."""
        q = ("SELECT w.node_id, w.added,"
             " (SELECT node_name FROM telemetry t WHERE t.node_id=w.node_id AND t.node_name IS NOT NULL ORDER BY id DESC LIMIT 1) AS node_name,"
             " (SELECT COUNT(*) FROM telemetry t WHERE t.node_id=w.node_id AND t.source='broadcast') AS readings,"
             " (SELECT MAX(ts) FROM telemetry t WHERE t.node_id=w.node_id AND t.source='broadcast') AS last_ts"
             " FROM telemetry_watch w ORDER BY w.added")
        with self.audit.lock:
            return [dict(r) for r in self.audit.db.execute(q)]


class TelemetryService:
    """Records the telemetry broadcasts the radio hears and prunes old readings. Never transmits."""

    def __init__(self, bridge):
        self.bridge = bridge
        a = bridge.args
        self.store = TelemetryStore(bridge.audit)
        self.passive_gap = getattr(a, "telemetry_passive_gap", 60.0)   # min seconds between stored broadcasts per node+kind
        self._seen_pkts = set()        # (sender num, packet id) pairs, to drop duplicates of one broadcast; cleared when it grows large
        self._last_passive = {}        # (node_id, kind) -> time.monotonic() of the last stored broadcast, for passive_gap
        days = getattr(a, "telemetry_retention_days", None)
        if days is not None:                                           # a flag overrides and saves the setting
            self.set_retention_days(days)

    # ---- settings -------------------------------------------------------------
    def passive_all(self):
        """Record every node's broadcasts (default), or only the watched nodes'."""
        return self.bridge.audit.get_setting("telemetry_passive_all", "1") == "1"

    def set_passive_all(self, enabled):
        """Choose between recording every node's broadcasts (True) and only the watched nodes' (False). Saved in settings."""
        self.bridge.audit.set_setting("telemetry_passive_all", "1" if enabled else "0")
        print(f"[telemetry] recording broadcasts from {'every node' if enabled else 'watched nodes only'}")

    def retention_days(self):
        """Days to keep readings (0 = forever). Falls back to the default if the saved value isn't a number."""
        try:
            return int(self.bridge.audit.get_setting("telemetry_retention_days", str(DEFAULT_RETENTION_DAYS)))
        except ValueError:
            return DEFAULT_RETENTION_DAYS

    def set_retention_days(self, days):
        """Save the retention period (whole days, 0 to MAX_RETENTION_DAYS; 0 = keep forever). Raises TelemetryError otherwise."""
        if isinstance(days, bool) or not isinstance(days, int) or not (0 <= days <= MAX_RETENTION_DAYS):
            raise TelemetryError(f"Days must be a whole number from 0 to {MAX_RETENTION_DAYS} (0 = keep everything).")
        self.bridge.audit.set_setting("telemetry_retention_days", days)
        print(f"[telemetry] keeping readings for {days} days" if days else "[telemetry] keeping readings forever")

    def prune_old(self):
        """Delete readings older than the retention setting. Returns the number deleted."""
        days = self.retention_days()
        if days <= 0:
            return 0
        n = self.store.prune(time.time() - days * 86400)
        if n:
            print(f"[telemetry] pruned {n} reading(s) older than {days} days")
        return n

    # ---- watch list -------------------------------------------------------------
    def watch_add(self, node_id):
        """Validate a '!xxxxxxxx' node id from the operator and add it to the watch list. Raises TelemetryError if malformed."""
        node_id = _norm(node_id)
        if not NODE_ID_RE.match(node_id):
            raise TelemetryError("Node ID must look like !1a2b3c4d")
        self.store.watch_add(node_id, time.time())
        print(f"[telemetry] now recording broadcasts from {node_id}")

    def watch_remove(self, node_id):
        """Remove a node from the watch list. Raises TelemetryError if it wasn't on it."""
        if not self.store.watch_remove(_norm(node_id)):
            raise TelemetryError("That node isn't on the watch list.")

    # ---- listening ------------------------------------------------------------------
    def on_packet(self, packet, interface):
        """Every telemetry packet the radio hears (subscribed to meshtastic.receive.telemetry)."""
        # runs on the radio's receive thread: filter cheaply, de-duplicate, rate-limit per node+kind, then store
        try:
            decoded = packet.get("decoded") or {}
            node = packet.get("fromId")
            if not node or decoded.get("requestId"):
                return                                   # a reply to somebody's request, not a broadcast
            if packet.get("from") == interface.myInfo.my_node_num:
                return                                   # our own radio's telemetry isn't a reading from the mesh
            if not (self.passive_all() or self.store.is_watched(node)):
                return
            parsed = parse_reply(decoded.get("telemetry") or {})
            if not parsed:
                return
            pkt_key = (packet.get("from"), packet.get("id"))
            if pkt_key in self._seen_pkts:
                return                                   # the same packet heard twice (relayed)
            if len(self._seen_pkts) > 2000:
                self._seen_pkts.clear()
            self._seen_pkts.add(pkt_key)
            kind, values, metrics, node_time = parsed
            now = time.monotonic()
            if now - self._last_passive.get((node, kind), -1e9) < self.passive_gap:
                return                                   # a chatty node must not flood the database
            self._last_passive[(node, kind)] = now
            if len(self._last_passive) > 5000:
                self._last_passive.clear()
            # hops travelled = hopStart (limit at send) - hopLimit (remaining on arrival); None if either is missing or implausible
            # (hopStart 0 means the sender doesn't say), the same rule mesh.py and reach.py use, so a malformed packet can't store a negative count
            hs, hl = packet.get("hopStart"), packet.get("hopLimit")
            valid = lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 7
            hops = hs - hl if valid(hs) and valid(hl) and hs > 0 and hl <= hs else None
            self.store.add(time.time(), node, self.bridge.node_name(node), kind, "broadcast", "ok",
                           node_time=node_time or None, values=values, rx_snr=packet.get("rxSnr"),
                           rx_rssi=packet.get("rxRssi"), hops=hops, raw=metrics)
        except Exception as e:  # never let an odd packet break the radio's callback thread
            print(f"[error] telemetry broadcast: {e}")

    def heard(self):
        """Nodes whose telemetry this radio already holds (it keeps the latest values of everything it hears)."""
        try:
            nodes = list(self.bridge.iface.nodes.values())
            mine = self.bridge.iface.getMyUser().get("id")
        except Exception:
            return []
        out = []
        for n in nodes:
            u = n.get("user") or {}
            if not u.get("id") or u["id"] == mine or not (n.get("deviceMetrics") or n.get("environmentMetrics")):
                continue
            out.append({"node_id": u["id"], "node_name": u.get("longName"), "last_heard": n.get("lastHeard") or 0,
                        "device": n.get("deviceMetrics"), "environment": n.get("environmentMetrics")})
        return sorted(out, key=lambda x: -x["last_heard"])
