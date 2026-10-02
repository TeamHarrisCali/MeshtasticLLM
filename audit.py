"""SQLite audit log: one row per message the bridge handles, updated as it moves through the pipeline.

kind = 'ai'      an /ai request and the model's reply (or a canned reply such as rate-limit notices)
       'inbound' a plain DM to this node that was not an /ai command (logged, never sent to the model)
       'manual'  a message the operator sent from the web UI
"""
import csv
import io
import sqlite3
import threading
import time

COLUMNS = [
    "id", "ts", "kind", "node_id", "node_name", "prompt", "response", "status", "model", "llm_ms",
    "chunks", "delivered", "relayed", "failed", "rx_snr", "rx_rssi", "hops", "action", "auth",
    "delivery_note",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    node_id   TEXT NOT NULL,
    node_name TEXT,
    prompt    TEXT,
    response  TEXT,
    status    TEXT NOT NULL,      -- queued | answered | llm_error | rate_limited | usage | paused | reset |
                                  -- blocked | denied | cap | busy | cancelled | expired | inbound | manual |
                                  -- action_pending | action_running | action_ok | action_failed | action_denied
    model     TEXT,
    llm_ms    INTEGER,
    chunks    INTEGER DEFAULT 0,  -- messages sent to the node
    delivered INTEGER DEFAULT 0,  -- acked by the destination node
    relayed   INTEGER DEFAULT 0,  -- only heard being rebroadcast by another node
    failed    INTEGER DEFAULT 0,  -- NAK / retries exhausted
    rx_snr    REAL,
    rx_rssi   INTEGER,
    hops      INTEGER,
    kind      TEXT NOT NULL DEFAULT 'ai',
    action    TEXT,               -- name of the AI tool the model asked for, if any
    auth      TEXT,               -- how the sender was verified (PKI / pinned key / actions tier)
    delivery_note TEXT            -- radio failures and resends for this reply, e.g. "part (1/2): MAX_RETRANSMIT, resent"
);
CREATE TABLE IF NOT EXISTS memory_reset (   -- the model forgets a node's history before this time
    node_id TEXT PRIMARY KEY,
    ts      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (       -- operator-editable runtime settings
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS node_access (    -- per-node access rules; absent row = default rules
    node_id   TEXT PRIMARY KEY,
    access    TEXT NOT NULL DEFAULT 'default',   -- default | allow | block
    daily_cap INTEGER,                           -- NULL = follow the default cap, 0 = unlimited
    pinned_key TEXT,                             -- base64 public key the operator verified for this node
    max_tier  INTEGER                            -- NULL = AI tools off; 0 = read-only; 1 = + confirmed actions
);
"""

_UNSET = object()
# statuses that used a model run (count toward caps)
_COUNTED = "('queued','answered','llm_error','action_ok','action_failed','action_pending','action_denied')"

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_requests_node ON requests(node_id, id);
"""

_UPDATABLE = {"response", "status", "model", "llm_ms", "chunks", "node_name", "action"}
_DELIVERY = {"delivered", "relayed", "failed"}


class Audit:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            # databases created before conversations existed have no `kind` column
            for table, wanted in (
                ("requests", {"kind": "TEXT NOT NULL DEFAULT 'ai'", "action": "TEXT", "auth": "TEXT",
                              "delivery_note": "TEXT"}),
                ("node_access", {"pinned_key": "TEXT", "max_tier": "INTEGER"}),
            ):
                have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
                for col, decl in wanted.items():
                    if col not in have:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            self.db.executescript(_INDEXES)
            self.db.commit()

    def new_request(self, node_id, node_name, prompt, status="queued", rx_snr=None, rx_rssi=None,
                    hops=None, response=None, kind="ai", action=None, auth=None):
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO requests (ts, node_id, node_name, prompt, response, status, rx_snr,"
                " rx_rssi, hops, kind, action, auth) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), node_id, node_name, prompt, response, status, rx_snr, rx_rssi, hops, kind,
                 action, auth),
            )
            self.db.commit()
            return cur.lastrowid

    def update(self, rid, **fields):
        bad = set(fields) - _UPDATABLE
        if bad:
            raise ValueError(f"not updatable: {bad}")
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE requests SET {sets} WHERE id=?", (*fields.values(), rid))
            self.db.commit()

    def add_note(self, rid, text):
        """Append a delivery remark (kept short; several parts/retries can add to it)."""
        with self.lock:
            cur = self.db.execute("SELECT delivery_note FROM requests WHERE id=?", (rid,)).fetchone()
            note = f"{cur['delivery_note']}; {text}" if cur and cur["delivery_note"] else text
            self.db.execute("UPDATE requests SET delivery_note=? WHERE id=?", (note[:400], rid))
            self.db.commit()

    def bump(self, rid, kind):
        if kind not in _DELIVERY:
            raise ValueError(kind)
        with self.lock:
            self.db.execute(f"UPDATE requests SET {kind}={kind}+1 WHERE id=?", (rid,))
            self.db.commit()

    def list(self, limit=50, before=None, q=None, status=None):
        where, args = [], []
        if before:
            where.append("id < ?")
            args.append(int(before))
        if status:
            where.append("status = ?")
            args.append(status)
        if q:
            where.append("(prompt LIKE ? OR response LIKE ? OR node_id LIKE ? OR node_name LIKE ?)")
            args += [f"%{q}%"] * 4
        sql = "SELECT * FROM requests"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args)]

    # ---- per-node memory -------------------------------------------------
    def history(self, node_id, turns, ttl_s, max_chars):
        """Chat messages the model should remember for this node, oldest first.

        Only that node's own answered /ai exchanges and operator messages are used, so
        conversations never mix between users. Plain inbound DMs are deliberately excluded.
        """
        if turns <= 0:
            return []
        with self.lock:
            since = time.time() - ttl_s if ttl_s else 0.0
            reset = self.db.execute("SELECT ts FROM memory_reset WHERE node_id=?", (node_id,)).fetchone()
            if reset:
                since = max(since, reset["ts"])
            rows = self.db.execute(
                "SELECT kind, prompt, response FROM requests WHERE node_id=? AND ts>? AND"
                " ((kind IN ('ai', 'web') AND status='answered') OR kind='manual') ORDER BY id DESC LIMIT ?",
                (node_id, since, turns),
            ).fetchall()
        picked, used = [], 0
        for r in rows:  # newest first, so the char budget drops the oldest turns
            if r["kind"] == "manual":
                msgs = [{"role": "assistant", "content": "[message from the human operator] " + (r["response"] or "")}]
            else:
                msgs = [{"role": "user", "content": r["prompt"] or ""},
                        {"role": "assistant", "content": r["response"] or ""}]
            size = sum(len(m["content"]) for m in msgs)
            if picked and used + size > max_chars:
                break
            picked.append(msgs)
            used += size
        return [m for msgs in reversed(picked) for m in msgs]

    def clear_memory(self, node_id):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO memory_reset (node_id, ts) VALUES (?, ?)",
                            (node_id, time.time()))
            self.db.commit()

    # ---- settings and access rules --------------------------------------
    def get_setting(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key, value):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
            self.db.commit()

    def get_access(self, node_id):
        """(access, daily_cap override or None) for a node."""
        with self.lock:
            row = self.db.execute("SELECT access, daily_cap FROM node_access WHERE node_id=?", (node_id,)).fetchone()
        return (row["access"], row["daily_cap"]) if row else ("default", None)

    def get_node(self, node_id):
        """Every access setting for a node, with defaults for unknown nodes."""
        with self.lock:
            row = self.db.execute("SELECT * FROM node_access WHERE node_id=?", (node_id,)).fetchone()
        return dict(row) if row else {"node_id": node_id, "access": "default", "daily_cap": None,
                                      "pinned_key": None, "max_tier": None}

    def set_access(self, node_id, access=_UNSET, daily_cap=_UNSET, pinned_key=_UNSET, max_tier=_UNSET):
        cur = self.get_node(node_id)
        new = {
            "access": cur["access"] if access is _UNSET else access,
            "daily_cap": cur["daily_cap"] if daily_cap is _UNSET else daily_cap,
            "pinned_key": cur["pinned_key"] if pinned_key is _UNSET else pinned_key,
            "max_tier": cur["max_tier"] if max_tier is _UNSET else max_tier,
        }
        with self.lock:
            if new["access"] == "default" and all(new[k] is None for k in ("daily_cap", "pinned_key", "max_tier")):
                self.db.execute("DELETE FROM node_access WHERE node_id=?", (node_id,))
            else:
                self.db.execute(
                    "INSERT OR REPLACE INTO node_access (node_id, access, daily_cap, pinned_key, max_tier)"
                    " VALUES (?,?,?,?,?)",
                    (node_id, new["access"], new["daily_cap"], new["pinned_key"], new["max_tier"]))
            self.db.commit()

    def access_rows(self):
        with self.lock:
            return {r["node_id"]: dict(r) for r in self.db.execute("SELECT * FROM node_access")}

    def used_24h(self, node_id=None):
        """Model runs in the last 24 h: an int for one node, or {node_id: count} for all."""
        since = time.time() - 86400
        with self.lock:
            if node_id is not None:
                return self.db.execute(
                    f"SELECT COUNT(*) FROM requests WHERE kind='ai' AND node_id=? AND ts>? AND status IN {_COUNTED}",
                    (node_id, since)).fetchone()[0]
            return {r["node_id"]: r["n"] for r in self.db.execute(
                f"SELECT node_id, COUNT(*) AS n FROM requests WHERE kind='ai' AND ts>? AND status IN {_COUNTED}"
                " GROUP BY node_id", (since,))}

    def pending_queue(self, max_age_s):
        """Questions still 'queued' from a previous run, oldest first.

        Fresh ones are returned to be answered; older ones are marked expired instead, since
        replying hours late would just confuse the sender.
        """
        cutoff = time.time() - max_age_s
        with self.lock:
            rows = [dict(r) for r in self.db.execute(
                "SELECT id, ts, node_id, prompt FROM requests WHERE kind='ai' AND status='queued' ORDER BY id")]
            stale = [r["id"] for r in rows if r["ts"] < cutoff]
            for rid in stale:
                self.db.execute("UPDATE requests SET status='expired', response=? WHERE id=?",
                                ("Expired while the bridge was offline.", rid))
            self.db.commit()
        return [r for r in rows if r["id"] not in stale]

    def cancel_stale_web(self):
        """Browser-chat questions still 'queued' when the bridge starts were never answered (the queue isn't restored for them)."""
        with self.lock:
            self.db.execute("UPDATE requests SET status='cancelled', response='The bridge restarted before this was answered.' WHERE kind='web' AND status='queued'")
            self.db.commit()

    # ---- conversations ---------------------------------------------------
    # which kinds of request make up a conversation: everything but the browser chat, or only your own direct messages
    # (you wrote, or they wrote without /ai), or only the AI exchanges
    SCOPES = {None: ("ai", "manual", "inbound"), "dm": ("manual", "inbound"), "ai": ("ai",)}

    def _kinds(self, scope):
        if scope not in self.SCOPES:
            raise ValueError("scope must be dm or ai")
        return self.SCOPES[scope]

    def conversations(self, scope=None):
        kinds = self._kinds(scope)
        inl = "(" + ",".join("?" * len(kinds)) + ")"
        with self.lock:
            rows = self.db.execute(
                "SELECT r.node_id AS node_id,"
                " (SELECT node_name FROM requests n WHERE n.node_id=r.node_id AND n.node_name IS NOT NULL"
                "  ORDER BY n.id DESC LIMIT 1) AS node_name,"
                " MAX(r.ts) AS last_ts, COUNT(*) AS count,"
                f" (SELECT COALESCE(NULLIF(l.response,''), l.prompt) FROM requests l WHERE l.node_id=r.node_id AND l.kind IN {inl}"
                "  ORDER BY l.id DESC LIMIT 1) AS last_text,"
                f" (SELECT l.kind FROM requests l WHERE l.node_id=r.node_id AND l.kind IN {inl} ORDER BY l.id DESC LIMIT 1) AS last_kind"
                f" FROM requests r WHERE r.kind IN {inl} GROUP BY r.node_id ORDER BY last_ts DESC",
                kinds * 3,
            ).fetchall()
        return [dict(r) for r in rows]

    def conversation(self, node_id, limit=200, scope=None):
        kinds = self._kinds(scope) if scope else None          # no scope: every row for the node, the browser chat's included
        marks = "" if kinds is None else f" AND kind IN ({','.join('?' * len(kinds))})"
        with self.lock:
            rows = self.db.execute(
                f"SELECT * FROM (SELECT * FROM requests WHERE node_id=?{marks} ORDER BY id DESC LIMIT ?) ORDER BY id",
                (node_id, *(kinds or ()), max(1, min(int(limit), 1000))),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- AI activity summaries (for the AI overview page) ------------------
    def window_counts(self, hours=24):
        """AI request statuses in the last N hours: {status: count}."""
        since = time.time() - hours * 3600
        with self.lock:
            return {r["status"]: r["n"] for r in self.db.execute(
                "SELECT status, COUNT(*) AS n FROM requests WHERE kind='ai' AND ts > ? GROUP BY status", (since,))}

    def avg_latency_ms(self, hours=24):
        since = time.time() - hours * 3600
        with self.lock:
            v = self.db.execute("SELECT AVG(llm_ms) FROM requests WHERE kind='ai' AND ts > ? AND llm_ms IS NOT NULL", (since,)).fetchone()[0]
        return None if v is None else int(v)

    def top_askers(self, hours=24, limit=5):
        """Who used the AI most, counting real questions (not usage hints, resets or rate-limit replies)."""
        since = time.time() - hours * 3600
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT node_id, (SELECT node_name FROM requests n WHERE n.node_id=r.node_id AND n.node_name IS NOT NULL"
                " ORDER BY n.id DESC LIMIT 1) AS node_name, COUNT(*) AS n FROM requests r WHERE kind='ai' AND ts > ?"
                " AND status NOT IN ('busy','cap','cancelled','expired') GROUP BY node_id"
                " ORDER BY n DESC, node_id LIMIT ?", (since, limit))]

    def recent_ai(self, limit=8):
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT id, ts, node_id, node_name, prompt, status, llm_ms, action FROM requests WHERE kind='ai'"
                " ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 50)),))]

    # ---- summary ---------------------------------------------------------
    def stats(self):
        day_ago = time.time() - 86400
        with self.lock:
            one = lambda sql, *a: self.db.execute(sql, a).fetchone()[0]
            top = [dict(r) for r in self.db.execute(
                "SELECT node_id, (SELECT node_name FROM requests n WHERE n.node_id=r.node_id AND"
                " n.node_name IS NOT NULL ORDER BY n.id DESC LIMIT 1) AS node_name, COUNT(*) AS n"
                " FROM requests r WHERE kind='ai' GROUP BY node_id ORDER BY n DESC LIMIT 5")]
            return {
                "total": one("SELECT COUNT(*) FROM requests WHERE kind='ai'"),
                "last_24h": one("SELECT COUNT(*) FROM requests WHERE kind='ai' AND ts > ?", day_ago),
                "answered": one("SELECT COUNT(*) FROM requests WHERE status='answered'"),
                "llm_errors": one("SELECT COUNT(*) FROM requests WHERE status='llm_error'"),
                "rate_limited": one("SELECT COUNT(*) FROM requests WHERE status='rate_limited'"),
                "avg_llm_ms": one("SELECT AVG(llm_ms) FROM requests WHERE llm_ms IS NOT NULL"),
                "delivered": one("SELECT COALESCE(SUM(delivered),0) FROM requests"),
                "relayed": one("SELECT COALESCE(SUM(relayed),0) FROM requests"),
                "failed": one("SELECT COALESCE(SUM(failed),0) FROM requests"),
                "unique_nodes": one("SELECT COUNT(DISTINCT node_id) FROM requests WHERE kind='ai'"),
                "top_nodes": top,
            }

    def export_csv(self):
        with self.lock:
            rows = self.db.execute("SELECT * FROM requests ORDER BY id").fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(COLUMNS)
        for r in rows:
            # prefix formula-looking cells so Excel doesn't execute mesh-supplied text
            w.writerow([("'" + v) if isinstance(v, str) and v and v[0] in "=+-@\t\r" else v
                        for v in (r[c] for c in COLUMNS)])
        return buf.getvalue()
