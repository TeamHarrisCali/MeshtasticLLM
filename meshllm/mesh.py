"""The Home dashboard's data: what the radio is hearing and what it knows about the mesh.

Nothing here transmits. Three sources:
  * the radio's own node list (a live snapshot: who is around, how recently, battery, position, ...);
  * counters we keep ourselves: how many packets of each type, and from which nodes, arrived each hour
    (counts only - never message content); and
  * a periodic snapshot of mesh health (nodes heard, channel use, our own radio's load) so there is a history.
"""
import json
import math
import os
import threading
import time
from collections import Counter, deque

from meshllm import actions
from meshllm.audit import csv_cell
from meshllm.traceroute import _position

# Retention windows: names ending _H are hours, _S are seconds. prune() applies them; DATASETS below repeats them in words for the operator.
PACKET_RETENTION_H = 14 * 24
SAMPLE_RETENTION_S = 30 * 86400
DIRECT_WINDOW_S = 3 * 3600          # a node we heard directly this recently counts as 0 hops away, whatever the node list says
LINK_RETENTION_H = 30 * 24          # hourly signal/hop statistics
TRAIL_RETENTION_S = 90 * 86400      # where nodes have been
TRAIL_MIN_KM = 0.015                # a node has to move this far (15 m) to add a point to its trail
NODE_RETENTION_S = 90 * 86400      # a node we haven't heard for this long is forgotten (with its position)
SENSOR_LIMITS = {"temperature": (-90, 70), "relative_humidity": (0, 100), "barometric_pressure": (300, 1100)}   # a broken sensor can't skew the average
UNKNOWN_SEEN = 1                   # last_seen of a node we have never actually heard (the radio's own stamp wasn't trusted)
CLOCK_TOLERANCE_S = 600            # the radio's 'last heard' must agree with this PC's clock to within this, or it isn't trusted

# "hour" columns throughout are whole hours since the epoch (unix time // 3600, UTC). The mesh_* counters are filled by flush().
SCHEMA = """
CREATE TABLE IF NOT EXISTS mesh_packets (        -- packets heard, per hour and type (counts only)
    hour    INTEGER NOT NULL,                    -- hours since 1970 (UTC)
    portnum TEXT NOT NULL,                       -- TEXT_MESSAGE_APP, TELEMETRY_APP, ..., or ENCRYPTED
    n       INTEGER NOT NULL,
    PRIMARY KEY (hour, portnum)
);
CREATE TABLE IF NOT EXISTS mesh_talkers (        -- packets heard, per hour and sending node
    hour    INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    n       INTEGER NOT NULL,
    PRIMARY KEY (hour, node_id)
);
CREATE TABLE IF NOT EXISTS mesh_samples (        -- mesh health over time
    ts               REAL PRIMARY KEY,
    nodes_total      INTEGER,
    heard_15m        INTEGER,
    heard_1h         INTEGER,
    heard_24h        INTEGER,
    direct           INTEGER,                    -- nodes heard in the last 24 h that are direct neighbours
    avg_channel_util REAL,                       -- mean channel use of nodes heard in the last hour that report it
    us_battery       REAL,
    us_channel_util  REAL,
    us_air_util_tx   REAL,
    packets          INTEGER                     -- packets heard since the previous sample
);
CREATE TABLE IF NOT EXISTS mesh_links (          -- signal of nodes heard DIRECTLY (no relay), per hour
    hour     INTEGER NOT NULL,
    node_id  TEXT NOT NULL,
    n        INTEGER NOT NULL,                   -- packets
    snr_sum  REAL NOT NULL, snr_min REAL NOT NULL, snr_max REAL NOT NULL,      -- dB
    rssi_sum REAL NOT NULL, rssi_min REAL NOT NULL, rssi_max REAL NOT NULL,    -- dBm
    PRIMARY KEY (hour, node_id)
);
CREATE TABLE IF NOT EXISTS mesh_hops (           -- how far packets had travelled when we heard them, per hour (counts only)
    hour  INTEGER NOT NULL,
    hops  INTEGER NOT NULL,
    n     INTEGER NOT NULL,
    PRIMARY KEY (hour, hops)
);
CREATE TABLE IF NOT EXISTS node_positions (      -- a node's trail: a point each time it is seen somewhere new
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    ts      REAL NOT NULL,
    lat     REAL NOT NULL,
    lon     REAL NOT NULL,
    alt     REAL
);
CREATE INDEX IF NOT EXISTS node_positions_node ON node_positions (node_id, id);
CREATE TABLE IF NOT EXISTS mesh_nodes (          -- every node we have heard of, kept even after the radio forgets it
    node_id    TEXT PRIMARY KEY,
    num        INTEGER,
    name       TEXT,
    short      TEXT,
    hw         TEXT,
    role       TEXT,
    first_seen REAL NOT NULL,                    -- when we first saw it
    last_seen  REAL NOT NULL,                    -- when it was last heard (the radio's 'last heard', or our clock)
    lat        REAL,                             -- last position we captured (from the node list or a position packet)
    lon        REAL,
    alt        REAL,
    pos_ts     REAL,
    pos_source TEXT                              -- 'radio' (its node list) or 'packet' (a position broadcast we heard)
);
"""


class PositionError(ValueError):
    """A refused operator action (bad position, unit or radio-admin failure); the message is safe to show the operator."""


def clean(s, n=40):
    """Node names are chosen by strangers: drop control characters and cap the length before showing/sending."""
    if not isinstance(s, str):
        return None
    s = "".join(ch for ch in s if ch.isprintable()).strip()
    return s[:n] or None


def c_to_f(c):
    """Celsius to Fahrenheit."""
    return c * 9 / 5 + 32


def fmt_temp_plain(unit, c):
    """Short form for radio replies: '21.5 C' / '70.7 F'."""
    return f"{c_to_f(c):.1f} F" if unit == "F" else f"{c:.1f} C"


KM_PER_MILE = 1.609344


def _hop(v):
    """A hop count as the firmware sends it: a whole number from 0 to 7."""
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 7


def _num(v):
    """True for a real, finite int/float (not a bool, NaN or infinity)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def fmt_dist_plain(unit, km):
    """Short form for radio replies: '11.1 km' / '6.9 mi'."""
    return f"{km / KM_PER_MILE:.1f} mi" if unit == "mi" else f"{km:.1f} km"


def distance_km(a_lat, a_lon, b_lat, b_lon):
    """Great-circle (haversine) distance in kilometres between two points given in degrees. Uses a mean Earth radius of 6371 km."""
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b_lon - a_lon) / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def ago(secs):
    """Rough age for display: 'just now' under 90 s, then minutes (to 90 min), hours (to 2 days), then days. None means never."""
    if secs is None:
        return "never"
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{round(secs / 60)} min ago"
    if secs < 172800:
        return f"{round(secs / 3600)} h ago"
    return f"{round(secs / 86400)} d ago"


def node_row(entry, my_num, now):
    """One node of the radio's node list, flattened for the dashboard."""
    u, dm, em = entry.get("user") or {}, entry.get("deviceMetrics") or {}, entry.get("environmentMetrics") or {}
    num, last = entry.get("num"), entry.get("lastHeard")
    lat, lon = _position(entry)
    return {
        "id": u.get("id") or (f"!{num:08x}" if isinstance(num, int) else None), "num": num,
        "name": clean(u.get("longName")), "short": clean(u.get("shortName"), 8),
        "role": u.get("role"), "hw": u.get("hwModel"), "hops": entry.get("hopsAway"), "snr": entry.get("snr"),
        "last_heard": last, "age_s": max(0, int(now - last)) if isinstance(last, (int, float)) and last else None,
        "mqtt": bool(entry.get("viaMqtt")), "favorite": bool(entry.get("isFavorite")), "us": num == my_num,
        "battery": dm.get("batteryLevel"), "voltage": dm.get("voltage"), "channel_util": dm.get("channelUtilization"),
        "air_util_tx": dm.get("airUtilTx"), "uptime_s": dm.get("uptimeSeconds"),
        "temperature": em.get("temperature"), "humidity": em.get("relativeHumidity"), "pressure": em.get("barometricPressure"),
        "lat": lat, "lon": lon, "has_key": bool(u.get("publicKey")),
    }


def summarize(rows):
    """Headline numbers from the node rows."""
    others = [r for r in rows if not r["us"]]
    heard = lambda secs: sum(1 for r in others if r["age_s"] is not None and r["age_s"] <= secs)
    day = [r for r in others if r["age_s"] is not None and r["age_s"] <= 86400]
    hour_util = [r["channel_util"] for r in others if r["age_s"] is not None and r["age_s"] <= 3600 and isinstance(r["channel_util"], (int, float))]
    hops = [r["hops"] for r in day if isinstance(r["hops"], int)]
    return {
        "nodes_total": len(rows), "heard_15m": heard(900), "heard_1h": heard(3600), "heard_24h": heard(86400),
        "direct": sum(1 for r in day if r["hops"] == 0), "max_hops": max(hops) if hops else None,
        "with_position": sum(1 for r in others if r["lat"] is not None),
        "with_telemetry": sum(1 for r in others if r["battery"] is not None or r["temperature"] is not None),
        "via_mqtt": sum(1 for r in others if r["mqtt"]),
        "avg_channel_util": round(sum(hour_util) / len(hour_util), 2) if hour_util else None,
        "roles": dict(Counter(r["role"] or "?" for r in others).most_common(6)),
        "hardware": dict(Counter(r["hw"] or "?" for r in others).most_common(5)),
    }


class MeshService:
    """Counts what the radio hears, remembers every node ever seen, and answers the dashboard's questions from the database.

    on_packet() runs on the radio's receive thread and only updates in-memory counters under self._lock; a background
    thread (_loop) writes them to the shared audit database. Nothing here sends anything over the air; the only writes to
    the radio itself are the operator actions sync_radio_clock(), set_radio_position() and clear_radio_position()."""

    def __init__(self, bridge):
        self.bridge = bridge
        a = bridge.args
        self.audit = bridge.audit
        with self.audit.lock:
            self.audit.db.executescript(SCHEMA)
            self.audit.db.commit()
        self.sample_interval = getattr(a, "mesh_sample_interval", 300.0)
        self.tick = getattr(a, "mesh_tick", 5.0)
        self.prune_interval = getattr(a, "mesh_prune_interval", 3600.0)
        self.stats_enabled = not getattr(a, "no_mesh_stats", False)   # counting/sampling; the prune job always runs
        self._lock = threading.Lock()
        self._packets, self._talkers = Counter(), Counter()
        self._heard_since_sample = 0
        self._positions = {}          # node id -> (lat, lon, alt, ts) heard in position packets, written by flush()
        self._direct_seen = {}        # node id -> when we last heard it with no relay in between (measured; the node list's hops-away goes stale)
        self._links = {}              # (hour, node) -> [n, snr_sum, snr_min, snr_max, rssi_sum, rssi_min, rssi_max], written by flush()
        self._hops = Counter()        # (hour, hops) -> packets
        self._known = {}              # node id -> last values written to mesh_nodes (skips unchanged rows)
        # "Last heard" in the radio's node list is stamped with the RADIO's clock, and the entries don't always refresh. On a radio
        # that has never had the time set it can be weeks off. So we note when a packet from each node really arrived (this PC's
        # clock), and compare: if the radio's stamps disagree with what we saw, we stop trusting them.
        self._seen = {}               # node id -> when a packet from it last arrived (this PC's clock)
        self._seen_new = {}           # the same, waiting to be written to mesh_nodes by flush()
        self._clock_samples = deque(maxlen=50)   # node-list stamp age of nodes we just heard: diagnostics only, NOT a clock signal
        self._rx_offsets = deque(maxlen=200)     # (arrival, radio's own rxTime - arrival): the radio stamps every packet it receives
        self._rx_timed = self._rx_untimed = 0    # radio receptions with / without a time stamp (none = the radio has no valid time)
        saved = self.audit.get_setting("radio_clock")
        self._clock = True if saved == "ok" else False if saved == "bad" else None    # None = not known yet
        self._thread, self._stopping = None, False

    # ---- counting what the radio hears --------------------------------------------------------
    def on_packet(self, packet, interface):
        """Subscribed to every packet the radio hears (meshtastic.receive). Counts only; must stay cheap."""
        try:
            num = packet.get("from")
            if num is None or num == interface.myInfo.my_node_num:
                return
            dec = packet.get("decoded") or {}
            node = packet.get("fromId") or (f"!{num:08x}" if isinstance(num, int) else None)
            if node:
                now = time.time()
                rx, rf = packet.get("rxTime"), _num(packet.get("rxRssi")) and packet.get("rxRssi") != 0     # rf: a real radio reception (not relayed in over MQTT)
                if rf and _num(rx) and 1_000_000_000 < rx < 4_300_000_000:        # a believable date (2001-2106); the radio sends 0 when it has no time
                    self._rx_offsets.append((now, rx - now)); self._rx_timed += 1
                elif rf:
                    self._rx_untimed += 1                 # a real radio reception that carries no usable time: the radio doesn't know the time
                hl0, hs0 = packet.get("hopLimit"), packet.get("hopStart")
                if rf and _hop(hl0) and _hop(hs0) and hs0 > 0 and hs0 == hl0:
                    self._direct_seen[node] = now         # nobody relayed it: that node is a direct neighbour right now
                with self._lock:
                    self._seen[node] = self._seen_new[node] = now
                entry = (getattr(interface, "nodesByNum", None) or {}).get(num)
                heard = entry.get("lastHeard") if isinstance(entry, dict) else None
                if isinstance(heard, (int, float)) and heard > 0:
                    self._clock_samples.append(abs(now - heard))   # diagnostics only (the library refreshes these stamps rarely)
            pos = dec.get("position")
            if node and isinstance(pos, dict):        # a position broadcast: remember where that node is
                lat, lon = _position({"position": pos})
                if lat is not None:
                    alt = pos.get("altitude")
                    with self._lock:
                        self._positions[node] = (lat, lon, alt if isinstance(alt, (int, float)) else None, time.time())
            if not self.stats_enabled:
                return
            # from here on: hourly counters, kept in memory and written out by flush(); all guarded by self._lock
            hour = int(time.time() // 3600)
            portnum = dec.get("portnum") or "ENCRYPTED"
            hl, hs = packet.get("hopLimit"), packet.get("hopStart")       # hops travelled = start - remaining (hopStart 0: sender doesn't say)
            hops = hs - hl if _hop(hl) and _hop(hs) and hs > 0 and hl <= hs else None
            snr, rssi = packet.get("rxSnr"), packet.get("rxRssi")
            with self._lock:
                self._packets[(hour, str(portnum)[:40])] += 1
                if node:
                    self._talkers[(hour, node)] += 1
                if hops is not None:
                    self._hops[(hour, int(hops))] += 1
                # the signal we measure is that of the LAST hop, so it only describes the sender when nobody relayed it
                if node and hops == 0 and _num(snr) and _num(rssi) and -40 <= snr <= 40 and -200 <= rssi < 0:
                    l = self._links.get((hour, node))
                    if l is None:
                        self._links[(hour, node)] = [1, snr, snr, snr, rssi, rssi, rssi]
                    else:
                        l[0] += 1; l[1] += snr; l[2] = min(l[2], snr); l[3] = max(l[3], snr); l[4] += rssi; l[5] = min(l[5], rssi); l[6] = max(l[6], rssi)
                self._heard_since_sample += 1
        except Exception:
            pass

    def flush(self):
        """Write the in-memory counters, positions and last-heard times to the database and reset them. Safe to call from any thread."""
        with self._lock:
            # swap the buffers out under the lock and write them after releasing it, so the receive thread is never held up by SQLite
            packets, talkers, positions, seen = self._packets, self._talkers, self._positions, self._seen_new
            links, hops = self._links, self._hops
            self._packets, self._talkers, self._positions, self._seen_new = Counter(), Counter(), {}, {}
            self._links, self._hops = {}, Counter()
        if not packets and not talkers and not positions and not seen and not links and not hops:
            return
        with self.audit.lock:
            db = self.audit.db
            # every write is an upsert that ADDS to the existing hour row, because flush() runs many times within an hour
            for (hour, node), l in links.items():
                db.execute("INSERT INTO mesh_links (hour, node_id, n, snr_sum, snr_min, snr_max, rssi_sum, rssi_min, rssi_max) VALUES (?,?,?,?,?,?,?,?,?) "
                           "ON CONFLICT(hour, node_id) DO UPDATE SET n=n+excluded.n, snr_sum=snr_sum+excluded.snr_sum, snr_min=MIN(snr_min, excluded.snr_min), "
                           "snr_max=MAX(snr_max, excluded.snr_max), rssi_sum=rssi_sum+excluded.rssi_sum, rssi_min=MIN(rssi_min, excluded.rssi_min), "
                           "rssi_max=MAX(rssi_max, excluded.rssi_max)", (hour, node, *l))
            for (hour, h), n in hops.items():
                db.execute("INSERT INTO mesh_hops (hour, hops, n) VALUES (?,?,?) ON CONFLICT(hour, hops) DO UPDATE SET n = n + excluded.n", (hour, h, n))
            for node, (lat, lon, alt, ts) in positions.items():
                self._trail(db, node, lat, lon, alt, ts)
            for node, ts in seen.items():
                db.execute("INSERT INTO mesh_nodes (node_id, first_seen, last_seen) VALUES (?,?,?) "
                           "ON CONFLICT(node_id) DO UPDATE SET last_seen=MAX(last_seen, excluded.last_seen)", (node, ts, ts))
            for node, (lat, lon, alt, ts) in positions.items():
                db.execute("INSERT INTO mesh_nodes (node_id, first_seen, last_seen, lat, lon, alt, pos_ts, pos_source) "
                           "VALUES (?,?,?,?,?,?,?, 'packet') ON CONFLICT(node_id) DO UPDATE SET "
                           "lat=excluded.lat, lon=excluded.lon, alt=excluded.alt, pos_ts=excluded.pos_ts, "
                           "pos_source='packet', last_seen=MAX(last_seen, excluded.last_seen)", (node, ts, ts, lat, lon, alt, ts))
            for (hour, p), n in packets.items():
                db.execute("INSERT INTO mesh_packets (hour, portnum, n) VALUES (?,?,?) "
                           "ON CONFLICT(hour, portnum) DO UPDATE SET n = n + excluded.n", (hour, p, n))
            for (hour, node), n in talkers.items():
                db.execute("INSERT INTO mesh_talkers (hour, node_id, n) VALUES (?,?,?) "
                           "ON CONFLICT(hour, node_id) DO UPDATE SET n = n + excluded.n", (hour, node, n))
            db.commit()

    @staticmethod
    def _trail(db, node, lat, lon, alt, ts):
        """Add a point to a node's trail if it is somewhere new (caller holds the audit lock)."""
        last = db.execute("SELECT lat, lon FROM node_positions WHERE node_id=? ORDER BY id DESC LIMIT 1", (node,)).fetchone()
        if last is None or distance_km(last["lat"], last["lon"], lat, lon) >= TRAIL_MIN_KM:
            db.execute("INSERT INTO node_positions (node_id, ts, lat, lon, alt) VALUES (?,?,?,?,?)", (node, ts, lat, lon, alt))

    # ---- the radio's node list ---------------------------------------------------------------------
    def nodes(self):
        """The radio's live node list as flattened rows (see node_row), with last-heard reconciled against this PC's clock.
        Returns [] when no radio is connected. Reads only."""
        iface, now = self.bridge.iface, time.time()
        if iface is None:
            return []
        try:
            my_num = iface.myInfo.my_node_num
            entries = list(iface.nodes.values())
        except Exception:
            return []
        rows = [r for r in (node_row(e, my_num, now) for e in entries) if r["id"]]
        trust = self.clock_ok() is not False
        for r in rows:                                   # prefer what this PC saw; use the radio's stamp only while its clock checks out
            last = r["last_heard"] if trust and isinstance(r["last_heard"], (int, float)) and r["last_heard"] else None
            # the newer of the radio's stamp (capped at now, in case its clock runs ahead) and when this PC last got a packet from the node
            best = max(min(last, now) if last else 0, self._seen.get(r["id"], 0)) or None
            r["last_heard"], r["age_s"] = best, (max(0, int(now - best)) if best else None)
            if not r["us"] and now - self._direct_seen.get(r["id"], 0) < DIRECT_WINDOW_S:
                r["hops"], r["hops_measured"] = 0, True     # the radio's hops-away is only as fresh as its last node-info push; what we just measured wins
        return self.bridge.userdata.annotate(rows)

    # ---- is the radio's clock right? -----------------------------------------------------------------------------------
    def clock_ok(self):
        """True/False: does the radio's clock agree with this PC's? None until we have heard enough.

        The evidence is the time the radio itself stamps on every packet it receives (rxTime) compared with when that packet
        reached us; the median of the last ten decides. A radio that has never been told the time stamps nothing at all.
        The node list's "last heard" values are NOT used: the library only refreshes them when the radio pushes a node-info
        update, so they are often old for a node we have just heard, which says nothing about the clock."""
        recent = [abs(o) for _, o in list(self._rx_offsets)[-10:]]
        verdict = self._clock
        if recent:
            verdict = sorted(recent)[len(recent) // 2] <= CLOCK_TOLERANCE_S
        elif self._rx_untimed >= 3 and self._rx_timed == 0:
            verdict = False                       # radio receptions that carry no time: the radio doesn't know the time
        # persist changes so the verdict survives a restart
        if verdict != self._clock:
            self._clock = verdict
            self.audit.set_setting("radio_clock", "ok" if verdict else "bad")
        return self._clock

    def clock_message(self):
        """A sentence saying how the radio's clock is wrong (only meaningful when clock_ok() is False)."""
        def span(s):
            """A duration in seconds as a rounded phrase in the largest sensible unit (seconds, minutes, hours, days)."""
            s = abs(s)
            return f"{s:.0f} seconds" if s < 120 else f"{s / 60:.0f} minutes" if s < 7200 else f"{s / 3600:.0f} hours" if s < 172800 else f"{s / 86400:.0f} days"
        offs = sorted(o for _, o in list(self._rx_offsets)[-10:])
        if offs:
            med = offs[len(offs) // 2]
            return f"The radio's clock is about {span(med)} {'behind' if med < 0 else 'ahead of'} this PC."
        if self._rx_untimed >= 3 and self._rx_timed == 0:
            return "The radio has no time set: what it receives carries no time stamp."
        return "The radio's clock looked wrong the last time it was checked."

    def clock_report(self):
        """Everything we know about the radio's clock, for working out whether it is really wrong."""
        offs = sorted(o for _, o in self._rx_offsets)
        med = offs[len(offs) // 2] if offs else None
        ent = sorted(self._clock_samples)
        return {"verdict": self.clock_ok(), "saved_verdict": self.audit.get_setting("radio_clock"),
                "packets_with_time": self._rx_timed, "packets_without_time": self._rx_untimed,
                "rx_offset_s": {"n": len(offs), "median": med, "min": offs[0] if offs else None, "max": offs[-1] if offs else None,
                                "recent": [round(o, 1) for _, o in list(self._rx_offsets)[-8:]]},
                "node_list_gap_s": {"n": len(ent), "min": ent[0] if ent else None, "median": ent[len(ent) // 2] if ent else None, "max": ent[-1] if ent else None},
                "synced_at": self.audit.get_setting("radio_clock_synced_at"), "now": time.time()}

    def sync_radio_clock(self):
        """Tell the radio the time from this PC. Afterwards it stamps what it hears correctly."""
        self._admin("radio clock set from this PC", lambda n: n.setTime())
        self.audit.set_setting("radio_clock_synced_at", time.time())
        # forget the old evidence: it was measured against the previous clock, so the verdict is learned afresh from new packets
        self._clock_samples.clear(); self._rx_offsets.clear(); self._rx_timed = self._rx_untimed = 0; self._clock = None
        self.audit.set_setting("radio_clock", "")

    def us(self, rows=None):
        """This radio's own row from the node list (or from `rows` if given), or None."""
        rows = self.nodes() if rows is None else rows
        return next((r for r in rows if r["us"]), None)

    # ---- every node we have ever heard (survives the radio clearing or rotating its list) --------------------
    def remember(self, rows=None):
        """Copy the radio's node list into mesh_nodes: names, hardware, last position, first/last seen."""
        rows = self.nodes() if rows is None else rows
        now, changed = time.time(), []
        for r in rows:
            key = (r["name"], r["short"], r["hw"], r["role"], r["lat"], r["lon"], r["last_heard"])
            if self._known.get(r["id"]) != key:
                changed.append((r, key))
        if not changed:
            return 0
        if self.audit.get_setting("mesh_nodes_since") is None:
            self.audit.set_setting("mesh_nodes_since", now)
        with self.audit.lock:
            db = self.audit.db
            # one upsert per changed node. COALESCE keeps the stored value when the radio's list has dropped a field;
            # pos_ts / pos_source only change when the position actually moved. A node is stamped with our clock on first sight.
            for r, _ in changed:
                # cap at now (a fast radio clock would otherwise put it in the future); UNKNOWN_SEEN means we have no real hearing time for it
                seen = min(r["last_heard"], now) if isinstance(r["last_heard"], (int, float)) and r["last_heard"] else UNKNOWN_SEEN
                db.execute(
                    "INSERT INTO mesh_nodes (node_id, num, name, short, hw, role, first_seen, last_seen, lat, lon, pos_ts, pos_source) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET num=excluded.num, "
                    "name=COALESCE(excluded.name, name), short=COALESCE(excluded.short, short), hw=COALESCE(excluded.hw, hw), "
                    "role=COALESCE(excluded.role, role), last_seen=MAX(last_seen, excluded.last_seen), "
                    "lat=COALESCE(excluded.lat, lat), lon=COALESCE(excluded.lon, lon), "
                    "pos_ts=CASE WHEN excluded.lat IS NOT NULL AND (lat IS NOT excluded.lat OR lon IS NOT excluded.lon) "
                    "THEN excluded.pos_ts ELSE pos_ts END, "
                    "pos_source=CASE WHEN excluded.lat IS NOT NULL AND (lat IS NOT excluded.lat OR lon IS NOT excluded.lon) "
                    "THEN 'radio' ELSE pos_source END",
                    (r["id"], r["num"], clean(r["name"]), clean(r["short"], 8), str(r["hw"])[:40] if r["hw"] else None,
                     str(r["role"])[:40] if r["role"] else None, now, seen,
                     r["lat"], r["lon"], now if r["lat"] is not None else None, "radio" if r["lat"] is not None else None))
                if r["lat"] is not None:
                    self._trail(db, r["id"], r["lat"], r["lon"], None, now)
            db.commit()
        for r, key in changed:
            self._known[r["id"]] = key
        return len(changed)

    def stored_nodes(self):
        """Every row of mesh_nodes as dicts. Flushes pending counters first so last_seen is current."""
        self.flush()
        with self.audit.lock:
            return [dict(r) for r in self.audit.db.execute("SELECT * FROM mesh_nodes")]

    def all_nodes(self):
        """The radio's live list plus nodes only our database remembers (marked stored=True), newest first."""
        live = self.nodes()
        by_id = {r["id"]: r for r in live}
        now = time.time()
        for r in live:
            r["stored"] = False
        for s in self.stored_nodes():
            if s["node_id"] in by_id:
                r = by_id[s["node_id"]]
                r["first_seen"] = s["first_seen"]
                if UNKNOWN_SEEN < s["last_seen"] > (r["last_heard"] or 0) and s["last_seen"] <= now:      # we heard it more recently than the radio says
                    r["last_heard"], r["age_s"] = s["last_seen"], max(0, int(now - s["last_seen"]))
                if r["lat"] is None and s["lat"] is not None:          # the radio dropped the position, we kept it
                    r["lat"], r["lon"], r["pos_source"], r["pos_ts"] = s["lat"], s["lon"], s["pos_source"], s["pos_ts"]
                else:
                    r["pos_source"], r["pos_ts"] = s["pos_source"], s["pos_ts"]
                continue
            live.append({
                "id": s["node_id"], "num": s["num"], "name": s["name"], "short": s["short"], "role": s["role"], "hw": s["hw"],
                "hops": None, "snr": None, "last_heard": s["last_seen"] if s["last_seen"] > UNKNOWN_SEEN else None,
                "age_s": max(0, int(now - s["last_seen"])) if s["last_seen"] > UNKNOWN_SEEN else None,
                "mqtt": False, "favorite": False, "us": False, "battery": None, "voltage": None, "channel_util": None,
                "air_util_tx": None, "uptime_s": None, "temperature": None, "humidity": None, "pressure": None,
                "lat": s["lat"], "lon": s["lon"], "has_key": False, "stored": True, "first_seen": s["first_seen"],
                "pos_source": s["pos_source"], "pos_ts": s["pos_ts"]})
        return sorted(self.bridge.userdata.annotate(live), key=lambda r: (r["age_s"] is None, r["age_s"] or 0))

    def places(self):
        """Every node with a position we have captured, for the map: live or remembered."""
        rows = [r for r in self.all_nodes() if r["lat"] is not None]
        return {"nodes": rows, "total": len(rows), "stored_only": sum(1 for r in rows if r["stored"]),
                "from_packets": sum(1 for r in rows if r.get("pos_source") == "packet")}

    def find_node(self, text):
        """Best match for a name the user typed: exact id/name/short name first, then a unique partial match."""
        q = (text or "").strip().lstrip("!").lower()
        if not q:
            return None, []
        rows = self.all_nodes()
        for key in ("id", "name", "short"):
            hit = [r for r in rows if (r[key] or "").lstrip("!").lower() == q]
            if len(hit) == 1:
                return hit[0], []
        part = [r for r in rows if q in (r["name"] or "").lower() or q in (r["short"] or "").lower() or q in (r["id"] or "").lower()]
        return (part[0], []) if len(part) == 1 else (None, part[:5])

    # ---- a different radio was connected ----------------------------------------------------------------------------------
    def note_radio_change(self, old_id, new_id, old_name, new_name):
        """Called when the radio we're talking to is not the one we used last time. The clock verdict belonged to the old
        radio, so start over; and leave a banner (with a checklist) until the operator dismisses it."""
        # the clock evidence belonged to the previous radio
        self._clock_samples.clear(); self._rx_offsets.clear(); self._rx_timed = self._rx_untimed = 0; self._clock = None
        self.audit.set_setting("radio_clock", "")
        self.audit.set_setting("radio_change", json.dumps({"from": old_id, "from_name": old_name, "to": new_id, "to_name": new_name,
                                                           "ts": time.time(), "dismissed": False}))

    def _radio_change_event(self):
        """The saved "different radio" event if one exists and has not been dismissed, else None."""
        try:
            ev = json.loads(self.audit.get_setting("radio_change") or "null")
        except ValueError:
            return None
        return ev if isinstance(ev, dict) and not ev.get("dismissed") else None

    def radio_change_active(self):
        """True while the "different radio" banner is showing."""
        return self._radio_change_event() is not None

    def dismiss_radio_change(self):
        """Hide the "different radio" banner (the event stays saved, marked dismissed)."""
        ev = self._radio_change_event()
        if ev:
            ev["dismissed"] = True
            self.audit.set_setting("radio_change", json.dumps(ev))

    def radio_change(self):
        """The banner's content: what changed and what is still worth doing on the new radio. None when there is nothing to show."""
        ev = self._radio_change_event()
        if ev is None:
            return None
        b, items = self.bridge, []
        pos = self.radio_position()
        items.append({"id": "position", "done": pos["lat"] is not None, "page": "map", "label": "Give the radio a position",
                      "detail": f"It is at {pos['lat']:.4f}, {pos['lon']:.4f}." if pos["lat"] is not None else "The new radio has no position, so it isn't on the map. Pick one on the Map page."})
        clock = self.clock_ok()
        items.append({"id": "clock", "done": clock is True, "action": "sync_clock" if clock is False else None,
                      "label": "Check the radio's clock",
                      "detail": {True: "Its time agrees with this PC.", None: "Checking: this needs a packet or two from other nodes.",
                                 False: self.clock_message()}[clock]})
        try:
            nodes = b.access_overview()["nodes"]
        except Exception:
            nodes = []
        enabled = [n for n in nodes if n["max_tier"] is not None]
        waiting = [n for n in enabled if n["key_state"] != "pinned"]
        items.append({"id": "keys", "done": not waiting, "page": "access", "label": "Let the radio learn the keys of nodes with AI tools",
                      "detail": (f"{len(waiting)} of {len(enabled)} node{'s' if len(enabled) != 1 else ''} with AI tools can't be used yet: the radio hasn't seen "
                                 "their key (it learns them as they broadcast), or it changed.") if waiting else
                                (f"All {len(enabled)} node{'s' if len(enabled) != 1 else ''} with AI tools match their pinned key." if enabled else "No node has AI tools enabled.")})
        with self.audit.lock:
            have = self.audit.db.execute("SELECT COUNT(*) FROM radio_config_backups WHERE radio = ?", (b.radio_id,)).fetchone()[0]
        items.append({"id": "backup", "done": have > 0, "page": "radio", "label": "Back up this radio's settings",
                      "detail": "A backup of this radio's settings exists." if have else "Pull its settings on the Radio settings page to save a copy."})
        return {**ev, "items": items, "all_done": all(i["done"] for i in items)}

    # ---- our own radio's position (operator-only: there is no AI action for this) ------------------------------
    def radio_position(self):
        """{fixed, lat, lon, alt}: whether the radio is set to a fixed position, and where it believes it is."""
        b, out = self.bridge, {"connected": False, "fixed": None, "lat": None, "lon": None, "alt": None}
        if b.iface is None:
            return out
        out["connected"] = True
        try:
            out["fixed"] = bool(b.iface.localNode.localConfig.position.fixed_position)
        except Exception:
            pass
        us = self.us()
        if us and us["lat"] is not None:
            out["lat"], out["lon"] = us["lat"], us["lon"]
        try:
            alt = b.iface.nodesByNum[b.iface.myInfo.my_node_num].get("position", {}).get("altitude")
            out["alt"] = alt if isinstance(alt, (int, float)) else None
        except Exception:
            pass
        return out

    def _admin(self, label, call):
        """Run call(localNode) as a write to our own radio, one radio request at a time (shared radio_request_lock).
        Raises PositionError if the radio is away, busy for more than 5 s, or refuses the change."""
        b = self.bridge
        if b.iface is None:
            raise PositionError("The radio isn't connected.")
        if not b.radio_request_lock.acquire(timeout=5):      # a traceroute is on the air; don't talk over it
            raise PositionError("The radio is busy with another request (a traceroute or a settings change); try again in a moment.")
        try:
            call(b.iface.localNode)
        except Exception as e:
            raise PositionError(f"The radio didn't accept it: {e}")
        finally:
            b.radio_request_lock.release()
        print(f"[radio] {label} (web)")

    def set_radio_position(self, lat, lon, alt=0):
        """Write a fixed position to the radio. The radio will then share it with the mesh on its normal schedule."""
        num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
        if not (num(lat) and num(lon)):
            raise PositionError("Latitude and longitude must be numbers.")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise PositionError("That isn't a valid position (latitude -90..90, longitude -180..180).")
        if lat == 0 and lon == 0:
            raise PositionError("0, 0 means 'no position'; pick a real place.")
        if alt in (None, ""):
            alt = 0
        if not num(alt) or not -500 <= alt <= 100000:
            raise PositionError("Altitude must be a number of metres between -500 and 100000.")
        lat, lon, alt = round(float(lat), 6), round(float(lon), 6), int(round(alt))
        self._admin(f"fixed position set to {lat}, {lon} ({alt} m)", lambda n: n.setFixedPosition(lat, lon, alt))
        return {"lat": lat, "lon": lon, "alt": alt}

    def clear_radio_position(self):
        """Remove the radio's fixed position so it goes back to using its own GPS (if any)."""
        self._admin("fixed position removed", lambda n: n.removeFixedPosition())

    # ---- history -------------------------------------------------------------------------------------
    def sample(self, rows=None):
        """Store one snapshot of mesh health. Returns it, or None if there's no radio."""
        rows = self.nodes() if rows is None else rows
        if not rows:
            return None
        s, us = summarize(rows), self.us(rows) or {}
        with self._lock:
            packets, self._heard_since_sample = self._heard_since_sample, 0
        row = (time.time(), s["nodes_total"], s["heard_15m"], s["heard_1h"], s["heard_24h"], s["direct"],
               s["avg_channel_util"], us.get("battery"), us.get("channel_util"), us.get("air_util_tx"), packets)
        with self.audit.lock:
            self.audit.db.execute("INSERT OR REPLACE INTO mesh_samples VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
            self.audit.db.commit()
        return row

    def samples(self, hours=24):
        """Stored mesh-health snapshots (see sample) from the last N hours (1 hour to 30 days), oldest first."""
        since = time.time() - max(1, min(int(hours), 24 * 30)) * 3600
        with self.audit.lock:
            return [dict(r) for r in self.audit.db.execute("SELECT * FROM mesh_samples WHERE ts >= ? ORDER BY ts", (since,))]

    def traffic(self, hours=24):
        """Packets heard per hour by type (oldest hour first, empty hours included) and the top senders."""
        self.flush()
        hours = max(1, min(int(hours), PACKET_RETENTION_H))
        now_h = int(time.time() // 3600)
        first = now_h - hours + 1
        with self.audit.lock:
            rows = self.audit.db.execute("SELECT hour, portnum, n FROM mesh_packets WHERE hour >= ?", (first,)).fetchall()
            # top senders cover only the current and previous hour (hour >= now_h - 1), whatever `hours` is
            talk = self.audit.db.execute(
                "SELECT node_id, SUM(n) AS n FROM mesh_talkers WHERE hour >= ? GROUP BY node_id ORDER BY n DESC, node_id LIMIT 10",
                (now_h - 1,)).fetchall()
        by_hour = {h: {} for h in range(first, now_h + 1)}
        totals = Counter()
        for r in rows:
            by_hour[r["hour"]][r["portnum"]] = r["n"]
            totals[r["portnum"]] += r["n"]
        names = {r["id"]: r["name"] for r in self.nodes()}
        return {"hours": [{"hour": h, "counts": c} for h, c in sorted(by_hour.items())],
                "types": [t for t, _ in totals.most_common()], "total": sum(totals.values()),
                "talkers": [{"node_id": t["node_id"], "name": names.get(t["node_id"]), "n": t["n"]} for t in talk]}

    # ---- link quality, hop counts, trails, and a map of what we collect -----------------------------------------------------
    def links(self, node_id, hours=168):
        """Hourly signal of one node heard directly, oldest first (empty for nodes only ever heard through relays)."""
        self.flush()
        hours = max(1, min(int(hours), LINK_RETENTION_H))
        with self.audit.lock:
            rows = self.audit.db.execute("SELECT * FROM mesh_links WHERE node_id=? AND hour > ? ORDER BY hour", (node_id, int(time.time() // 3600) - hours)).fetchall()
        return [{"ts": r["hour"] * 3600, "n": r["n"], "snr": r["snr_sum"] / r["n"], "snr_min": r["snr_min"], "snr_max": r["snr_max"],
                 "rssi": r["rssi_sum"] / r["n"], "rssi_min": r["rssi_min"], "rssi_max": r["rssi_max"]} for r in rows]

    def link_map(self, hours=24):
        """Every node heard directly in the last N hours with its average signal, and how far from us it is: the data behind the
        link-quality map. Nodes only ever reached through relays are not here (the radio would be measuring the relay)."""
        self.flush()
        hours = max(1, min(int(hours), LINK_RETENTION_H))
        with self.audit.lock:
            rows = self.audit.db.execute(
                "SELECT node_id, SUM(n) AS n, SUM(snr_sum) AS ss, MIN(snr_min) AS smin, MAX(snr_max) AS smax, SUM(rssi_sum) AS rs, MIN(rssi_min) AS rmin, "
                "MAX(rssi_max) AS rmax, MAX(hour) AS last_hour FROM mesh_links WHERE hour > ? GROUP BY node_id", (int(time.time() // 3600) - hours,)).fetchall()
        nodes = {r["id"]: r for r in self.all_nodes()}
        us = next((r for r in nodes.values() if r["us"]), None)
        here = {"lat": us["lat"], "lon": us["lon"]} if us and us["lat"] is not None else None
        links = []
        for r in rows:
            nd = nodes.get(r["node_id"]) or {}
            lat, lon = nd.get("lat"), nd.get("lon")
            km = distance_km(here["lat"], here["lon"], lat, lon) if here and lat is not None else None
            links.append({"id": r["node_id"], "name": nd.get("name") or nd.get("short"), "n": r["n"], "snr": r["ss"] / r["n"], "snr_min": r["smin"], "snr_max": r["smax"],
                          "rssi": r["rs"] / r["n"], "rssi_min": r["rmin"], "rssi_max": r["rmax"], "last_ts": r["last_hour"] * 3600,
                          "lat": lat, "lon": lon, "distance_km": km, "stored": bool(nd.get("stored"))})
        links.sort(key=lambda l: (-l["snr"], l["id"]))
        placed = [l for l in links if l["distance_km"] is not None]
        return {"hours": hours, "us": here, "links": links, "farthest": max(placed, key=lambda l: l["distance_km"]) if placed else None}

    def hop_series(self, hours=24):
        """Packets heard per hour by how many relays they had passed through (oldest hour first, empty hours included)."""
        self.flush()
        hours = max(1, min(int(hours), LINK_RETENTION_H))
        now_h = int(time.time() // 3600)
        first = now_h - hours + 1
        with self.audit.lock:
            rows = self.audit.db.execute("SELECT hour, hops, n FROM mesh_hops WHERE hour >= ?", (first,)).fetchall()
        by = {h: {} for h in range(first, now_h + 1)}
        total = weighted = direct = 0
        for r in rows:
            by[r["hour"]][str(r["hops"])] = r["n"]
            total += r["n"]; weighted += r["hops"] * r["n"]; direct += r["n"] if r["hops"] == 0 else 0
        return {"hours": [{"ts": h * 3600, "counts": c} for h, c in sorted(by.items())], "total": total,
                "mean": weighted / total if total else None, "direct_pct": 100 * direct / total if total else None}

    def busy_hours(self, hours=336):
        """When the mesh is busiest: average packets heard per hour of the local day, from the hourly counts we keep."""
        t = self.traffic(hours)
        series = [(h["hour"], sum(h["counts"].values())) for h in t["hours"]]
        start = next((i for i, (_, v) in enumerate(series) if v > 0), None)
        if start is None:
            return None
        used = series[start:-1] if len(series) > start + 1 else series[start:]          # leave out the hour in progress
        sums, cnts = [0] * 24, [0] * 24
        for hr, v in used:
            hh = time.localtime(hr * 3600).tm_hour
            sums[hh] += v; cnts[hh] += 1
        avg = {i: sums[i] / cnts[i] for i in range(24) if cnts[i]}
        if not avg:
            return None
        best, quiet = max(avg, key=lambda i: (avg[i], -i)), min(avg, key=lambda i: (avg[i], i))
        return {"best": best, "best_avg": avg[best], "quiet": quiet, "quiet_avg": avg[quiet], "hours_of_data": len(used)}

    def activity(self, hours=1):
        """What happened in the last N hours (hourly counts, so 'about N hours'): packets, who was busiest, new nodes, readings."""
        self.flush()
        hours = max(1, min(int(hours), 24))
        now = time.time(); since_h = int(now // 3600) - hours; since_ts = now - hours * 3600
        with self.audit.lock:
            db = self.audit.db
            packets = db.execute("SELECT COALESCE(SUM(n), 0) FROM mesh_packets WHERE hour >= ?", (since_h,)).fetchone()[0]
            talkers = db.execute("SELECT node_id, SUM(n) AS n FROM mesh_talkers WHERE hour >= ? GROUP BY node_id ORDER BY n DESC, node_id", (since_h,)).fetchall()
            readings = db.execute("SELECT COUNT(*) FROM telemetry WHERE ts >= ? AND status='ok'", (since_ts,)).fetchone()[0]
            asked = db.execute("SELECT COUNT(*) FROM requests WHERE kind='ai' AND ts >= ?", (since_ts,)).fetchone()[0]
            traces = db.execute("SELECT COUNT(*) FROM traceroutes WHERE ts >= ?", (since_ts,)).fetchone()[0]
        names = {s["node_id"]: s["name"] for s in self.stored_nodes()}
        began = float(self.audit.get_setting("mesh_nodes_since", "inf") or "inf")
        new = [s for s in self.stored_nodes() if s["first_seen"] > max(since_ts, began)]
        return {"hours": hours, "packets": packets, "senders": len(talkers), "busiest": [(names.get(t["node_id"]) or t["node_id"], t["n"]) for t in talkers[:2]],
                "new_nodes": [s["name"] or s["node_id"] for s in sorted(new, key=lambda s: -s["first_seen"])], "readings": readings, "asked": asked, "traces": traces}

    def node_history(self, node_id, column, hours=24):
        """One node's readings of one metric over the last N hours, oldest first: [(ts, value)]."""
        since = time.time() - hours * 3600
        rows = self.bridge.telemetry.store.list(node=node_id, status="ok", since=since, limit=1000)
        return sorted((r["ts"], r[column]) for r in rows if isinstance(r.get(column), (int, float)))

    def hop_counts(self, hours=24):
        """Packets heard in the last N hours by how many relays they passed through: {0: n, 1: n, ...}."""
        self.flush()
        hours = max(1, min(int(hours), LINK_RETENTION_H))
        with self.audit.lock:
            rows = self.audit.db.execute("SELECT hops, SUM(n) AS n FROM mesh_hops WHERE hour > ? GROUP BY hops ORDER BY hops", (int(time.time() // 3600) - hours,)).fetchall()
        return {r["hops"]: r["n"] for r in rows}

    def trail(self, node_id, days=30):
        """One node's recorded positions from the last N days (1 to 90) as [{ts, lat, lon}], oldest first."""
        days = max(1, min(int(days), 90))
        with self.audit.lock:
            rows = self.audit.db.execute("SELECT ts, lat, lon FROM node_positions WHERE node_id=? AND ts > ? ORDER BY id", (node_id, time.time() - days * 86400)).fetchall()
        return [dict(r) for r in rows]

    def trails(self, days=7, max_points=150):
        """Every node that has moved: {node_id: [[lat, lon, ts], ...]} (nodes with a single point have no trail)."""
        days, max_points = max(1, min(int(days), 90)), max(2, min(int(max_points), 500))
        with self.audit.lock:
            rows = self.audit.db.execute("SELECT node_id, ts, lat, lon FROM node_positions WHERE ts > ? ORDER BY id", (time.time() - days * 86400,)).fetchall()
        by = {}
        for r in rows:
            by.setdefault(r["node_id"], []).append([r["lat"], r["lon"], r["ts"]])
        out = {}
        for nid, pts in by.items():
            if len(pts) < 2:
                continue
            if len(pts) > max_points:                      # keep the ends, thin the middle evenly
                step = (len(pts) - 1) / (max_points - 1)
                pts = [pts[round(i * step)] for i in range(max_points)]
            out[nid] = pts
        return out

    # ---- what we store: overview, CSV export, pruning ------------------------------------------------------------
    # The "kept" text below is for the operator and must agree with the retention constants at the top and with prune().
    DATASETS = [   # table, label, what it holds, time expression, how long it is kept, downloadable as CSV
        ("requests", "AI log", "Every message to and from the AI, with its status, timing and delivery. Holds message text for the AI and direct messages; the public channel has its own table below.", "ts", "until you delete it", False),
        ("telemetry", "Telemetry readings", "Battery, voltage, channel use and sensor readings every node broadcast.", "ts", "the retention setting on the Telemetry page", False),
        ("mesh_packets", "Packets per hour by type", "How many packets of each kind the radio heard each hour (counts only, never content).", "hour*3600", "14 days", True),
        ("mesh_talkers", "Packets per hour by sender", "How many packets each node sent each hour (counts only).", "hour*3600", "14 days", True),
        ("mesh_links", "Signal per node per hour", "Average, best and worst SNR and RSSI of nodes heard directly (nobody relayed them).", "hour*3600", "30 days", True),
        ("mesh_hops", "Hops per hour", "How many packets had passed through 0, 1, 2... relays when they reached us (counts only).", "hour*3600", "30 days", True),
        ("mesh_samples", "Mesh health snapshots", "Nodes heard, channel use and our battery, every few minutes.", "ts", "30 days", True),
        ("mesh_nodes", "Nodes we have heard of", "Name, hardware, role, first/last seen and last position of every node, even after the radio forgets it.", "last_seen", "90 days after last heard", True),
        ("node_positions", "Position trails", "Where each node has been seen (a point when it moves 15 m).", "ts", "90 days", True),
        ("channel_messages", "Public channel messages", "What people posted on the default public channel and what you posted there. Message text, stored on this PC only; clear it on the Channel page.", "ts", "30 days", False),
        ("node_notes", "Node labels and notes", "The labels, notes and stars you gave nodes. Yours only: never sent over the radio or shown to the AI.", "updated", "until you delete them", False),
        ("snippets", "Saved snippets", "Text you saved for one-click posting and messaging.", "created", "until you delete them", False),
        ("walk_samples", "Walk test samples", "Signal and distance of the node you walked, packet by packet.", "ts", "90 days", False),
        ("traceroutes", "Traceroutes", "Every route you traced, with its hops, signal and positions.", "ts", "until you delete it", False),
        ("radio_config_backups", "Radio settings backups", "Copies of the radio's settings (never keys, Wi-Fi or MQTT credentials).", "ts", "the newest 40", False),
    ]
    EXPORTABLE = {d[0] for d in DATASETS if d[5]}

    def data_overview(self):
        """For the Data page: row count, oldest/newest time, retention text and CSV availability of each stored dataset,
        plus database size in bytes and the map-tile cache and hop statistics."""
        out = []
        with self.audit.lock:
            db = self.audit.db
            for table, label, what, tex, keep, csv_ok in self.DATASETS:
                # `table` and `tex` come from DATASETS (constants), never from a request, so formatting them into the SQL is safe
                try:
                    n, lo, hi = db.execute(f"SELECT COUNT(*), MIN({tex}), MAX({tex}) FROM {table}").fetchone()
                except Exception:
                    n, lo, hi = 0, None, None
                out.append({"name": table, "label": label, "what": what, "rows": n, "oldest": lo, "newest": hi, "kept": keep, "csv": csv_ok})
        try:
            size = sum(os.path.getsize(f) for f in (self.bridge.args.db, self.bridge.args.db + "-wal") if os.path.exists(f))   # the main file plus its write-ahead log
        except Exception:
            size = None
        return {"datasets": out, "db_bytes": size, "tiles": self.bridge.tiles.stats(), "hops": self.hop_counts(24)}

    def export_csv(self, table):
        """CSV text of one whole table, or None if it is not in EXPORTABLE. The allowlist also keeps request input out of the SQL."""
        if table not in self.EXPORTABLE:
            return None
        self.flush()
        import csv
        import io
        with self.audit.lock:
            cur = self.audit.db.execute(f"SELECT * FROM {table}")
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        buf = io.StringIO(); w = csv.writer(buf); w.writerow(cols)
        for r in rows:
            w.writerow([csv_cell(r[c]) for c in cols])
        return buf.getvalue()

    def prune(self):
        """Delete rows past their retention window, then run the other prune jobs (telemetry, channel messages, the daily
        backup if due, old walk-test samples). Called from _loop every prune_interval."""
        with self.audit.lock:
            db = self.audit.db
            cutoff = int(time.time() // 3600) - PACKET_RETENTION_H
            db.execute("DELETE FROM mesh_packets WHERE hour < ?", (cutoff,))
            db.execute("DELETE FROM mesh_talkers WHERE hour < ?", (cutoff,))
            db.execute("DELETE FROM mesh_samples WHERE ts < ?", (time.time() - SAMPLE_RETENTION_S,))
            db.execute("DELETE FROM mesh_nodes WHERE last_seen < ? AND last_seen > ?", (time.time() - NODE_RETENTION_S, UNKNOWN_SEEN))
            lh = int(time.time() // 3600) - LINK_RETENTION_H
            db.execute("DELETE FROM mesh_links WHERE hour < ?", (lh,))
            db.execute("DELETE FROM mesh_hops WHERE hour < ?", (lh,))
            db.execute("DELETE FROM node_positions WHERE ts < ?", (time.time() - TRAIL_RETENTION_S,))
            db.commit()
        self.bridge.telemetry.prune_old()   # telemetry readings follow their own retention setting
        self.bridge.channel.prune()         # so do the public-channel messages (30 days)
        try:
            self.bridge.backups.maybe_auto()   # the daily backup, when it is due
        except Exception as e:
            print(f"[error] backup: {e}")
        with self.audit.lock:               # old walk-test samples (90 days)
            self.audit.db.execute("DELETE FROM walk_samples WHERE session_id IN (SELECT id FROM walk_sessions WHERE started < ?)", (time.time() - 90 * 86400,))
            self.audit.db.execute("DELETE FROM walk_sessions WHERE started < ?", (time.time() - 90 * 86400,))
            self.audit.db.commit()

    # ---- the Home view -------------------------------------------------------------------------------------
    def alerts(self, rows):
        """Banners for the Home page from the node rows and radio state: [{level, kind, text, ...}]. Reads only."""
        out, b = [], self.bridge
        if b.iface is None:
            out.append({"level": "bad", "kind": "radio", "text": "The radio isn't connected - looking for one."})
        if not b.ollama_ok():
            out.append({"level": "warn", "kind": "ai", "text": f"The AI model '{b.model}' isn't available from Ollama right now."})
        if self.clock_ok() is False:
            out.append({"level": "info", "kind": "clock", "action": "sync_clock",
                        "text": self.clock_message() + " Its own \"last heard\" times can't be trusted, so only nodes this PC has heard itself show when they were "
                                "last heard. Setting the radio's clock from this PC fixes it."})
        low = sorted((r for r in rows if not r["us"] and isinstance(r["battery"], (int, float)) and r["battery"] <= 20
                      and r["age_s"] is not None and r["age_s"] <= 86400), key=lambda r: r["battery"])
        if low:  # one line, not one banner per node
            names = ", ".join(f"{r['name'] or r['id']} {r['battery']:.0f}%" for r in low[:4]) + (f" and {len(low) - 4} more" if len(low) > 4 else "")
            out.append({"level": "warn", "kind": "battery", "node_id": low[0]["id"],
                        "text": f"{len(low)} node{'s' if len(low) != 1 else ''} low on battery (20% or less): {names}"})
        now = time.time()
        for w in b.telemetry.store.watched():
            if w["readings"] and w["last_ts"] and now - w["last_ts"] > 3 * 3600:
                out.append({"level": "info", "kind": "quiet", "node_id": w["node_id"],
                            "text": f"{w['node_name'] or w['node_id']} (watched) hasn't broadcast telemetry for {int((now - w['last_ts']) // 3600)} h"})
        return out

    FEED_TYPES = ("ai", "dm", "you", "telemetry", "broadcast", "traceroute", "node")

    def feed(self, limit=25, types=None, q=None, before=None):
        """Recent happenings, newest first. `types` limits the kinds, `q` searches the text, `before` pages back."""
        b, items = self.bridge, []
        want = set(types) & set(self.FEED_TYPES) if types else set(self.FEED_TYPES)
        names = {s["node_id"]: s["name"] for s in self.stored_nodes()}
        who = lambda nid, name=None: name or names.get(nid) or nid
        if want & {"ai", "dm", "you"}:
            for r in b.audit.list(limit=120):
                w = who(r["node_id"], r["node_name"])
                if r["kind"] == "inbound":
                    items.append({"ts": r["ts"], "type": "dm", "node_id": r["node_id"], "text": f"Direct message from {w}"})
                elif r["kind"] == "manual":
                    items.append({"ts": r["ts"], "type": "you", "node_id": r["node_id"], "text": f"You messaged {w}"})
                else:
                    items.append({"ts": r["ts"], "type": "ai", "node_id": r["node_id"],
                                  "text": f"{w} asked the AI ({r['status'].replace('_', ' ')}): {(r['prompt'] or '')[:60]}"})
        if want & {"telemetry", "broadcast"}:
            for t_ in b.telemetry.store.list(limit=120):
                w = who(t_["node_id"], t_["node_name"])
                if t_["status"] == "ok":
                    bits = [f"battery {t_['battery_level']:.0f}%" if t_["battery_level"] is not None else None,
                            self.fmt_temp(t_["temperature"])]
                    items.append({"ts": t_["ts"], "type": "broadcast" if t_["source"] == "broadcast" else "telemetry",
                                  "node_id": t_["node_id"],
                                  "text": f"Telemetry from {w}" + (": " + ", ".join(x for x in bits if x) if any(bits) else "")})
                else:
                    items.append({"ts": t_["ts"], "type": "telemetry", "node_id": t_["node_id"],
                                  "text": f"Telemetry request to {w}: {t_['status']}"})
        if "traceroute" in want:
            for r in b.traceroute.store.list(limit=60):
                w = who(r["node_id"], r["node_name"])
                items.append({"ts": r["ts"], "type": "traceroute", "node_id": r["node_id"],
                              "text": f"Traceroute to {w}: " + (f"{r['relays_towards']} relay(s) there" if r["status"] == "ok" else r["status"])})
        if "node" in want:
            since = float(self.audit.get_setting("mesh_nodes_since", "inf") or "inf")
            for s in self.stored_nodes():
                if s["first_seen"] > since:
                    items.append({"ts": s["first_seen"], "type": "node", "node_id": s["node_id"],
                                  "text": f"New node heard: {s['name'] or s['node_id']}" + (f" ({s['hw']})" if s["hw"] else "")})
        if before:
            items = [i for i in items if i["ts"] < before]
        if q:
            ql = q.lower()
            items = [i for i in items if ql in i["text"].lower() or ql in (i["node_id"] or "").lower()]
        return sorted(items, key=lambda i: -i["ts"])[:max(1, min(int(limit), 200))]

    def overview(self):
        """Everything the Home page shows in one call: radio status, our own node, headline numbers, alerts, a short feed, sensors."""
        rows = self.nodes()
        st = self.bridge.status()
        us = self.us(rows)
        day_ago = time.time() - 86400
        s = self.bridge.audit.stats()
        with self.audit.lock:
            tel_today = self.audit.db.execute("SELECT COUNT(*) FROM telemetry WHERE ts >= ? AND status='ok'", (day_ago,)).fetchone()[0]
            trace_count = self.audit.db.execute("SELECT COUNT(*) FROM traceroutes").fetchone()[0]
        return {"radio": {"connected": st["connected"], "searching": st["searching"], "port": st["port"], "node": st["node"],
                          "uptime_s": st["uptime_s"], "model": st["model"], "ollama_ok": st["ollama_ok"], "paused": st["paused"],
                          "queue_depth": st["queue_depth"]},
                "us": us, "summary": summarize(rows), "alerts": self.alerts(rows), "feed": self.feed(limit=6),
                "activity": {"ai_24h": s["last_24h"], "telemetry_24h": tel_today, "traceroutes": trace_count},
                "places": self.places_counts(), "sensors": self.sensors(1), "temp_unit": self.temp_unit()}

    def places_counts(self):
        """Counts only of the nodes with a known position (see places)."""
        p = self.places()
        return {"total": p["total"], "stored_only": p["stored_only"], "from_packets": p["from_packets"]}

    # ---- temperature unit (a display preference shared by the web pages and the AI's answers) ----------------------------------
    def temp_unit(self):
        """The chosen display unit for temperatures, "C" or "F" (default C)."""
        return "F" if self.audit.get_setting("temp_unit", "C") == "F" else "C"

    def set_temp_unit(self, unit):
        """Save the temperature display unit; raises PositionError for anything but "C" or "F"."""
        if unit not in ("C", "F"):
            raise PositionError("The temperature unit must be C or F.")
        self.audit.set_setting("temp_unit", unit)

    def dist_unit(self):
        """The chosen display unit for distances, "km" or "mi" (default km)."""
        return "mi" if self.audit.get_setting("dist_unit", "km") == "mi" else "km"

    def set_dist_unit(self, unit):
        """Save the distance display unit; raises PositionError for anything but "km" or "mi"."""
        if unit not in ("km", "mi"):
            raise PositionError("The distance unit must be km or mi.")
        self.audit.set_setting("dist_unit", unit)

    def fmt_temp(self, c, digits=1):
        """'21.5 °C' or '70.7 °F' from a Celsius value, in the chosen unit."""
        if c is None:
            return None
        return f"{c_to_f(c):.{digits}f} \u00b0F" if self.temp_unit() == "F" else f"{c:.{digits}f} \u00b0C"

    # ---- sensors: what the nodes around us report, averaged over a window of recorded broadcasts ----------------------------------
    def sensors(self, hours=1):
        """Environment readings (temperature, humidity, pressure) heard in the last `hours`, averaged per node and across nodes.
        Read from the telemetry we recorded (timestamps are this PC's clock), not from the radio's node list."""
        try:
            hours = max(0.25, min(float(hours), 168.0))
        except (TypeError, ValueError):
            hours = 1.0
        now = time.time()
        cols = tuple(SENSOR_LIMITS)
        with self.audit.lock:
            rows = [dict(r) for r in self.audit.db.execute(
                "SELECT ts, node_id, node_name, temperature, relative_humidity, barometric_pressure FROM telemetry "
                "WHERE status='ok' AND ts >= ? AND (temperature IS NOT NULL OR relative_humidity IS NOT NULL OR barometric_pressure IS NOT NULL) "
                "ORDER BY ts", (now - hours * 3600,))]
            newest = self.audit.db.execute(
                "SELECT ts, node_id, node_name FROM telemetry WHERE status='ok' AND (temperature IS NOT NULL OR relative_humidity IS NOT NULL "
                "OR barometric_pressure IS NOT NULL) ORDER BY ts DESC LIMIT 1").fetchone()
        names = {s["node_id"]: s["name"] for s in self.stored_nodes()}
        per = {}
        for r in rows:
            n = per.setdefault(r["node_id"], {"id": r["node_id"], "name": clean(r["node_name"]) or names.get(r["node_id"]), "last_ts": 0, "n": 0, "vals": {c: [] for c in cols}})
            n["last_ts"] = max(n["last_ts"], r["ts"]); n["n"] += 1
            for c in cols:
                v, (lo, hi) = r[c], SENSOR_LIMITS[c]
                if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and lo <= v <= hi:
                    n["vals"][c].append(v)
        mean = lambda xs: sum(xs) / len(xs) if xs else None
        nodes = []
        for n in per.values():
            nodes.append({"id": n["id"], "name": n["name"], "n": n["n"], "last_ts": n["last_ts"], "temperature": mean(n["vals"]["temperature"]),
                          "humidity": mean(n["vals"]["relative_humidity"]), "pressure": mean(n["vals"]["barometric_pressure"])})
        nodes.sort(key=lambda n: -n["last_ts"])
        # across nodes: the mean of each node's own mean, so one chatty node doesn't outweigh a quiet one
        overall = {"temperature": mean([n["temperature"] for n in nodes if n["temperature"] is not None]),
                   "humidity": mean([n["humidity"] for n in nodes if n["humidity"] is not None]),
                   "pressure": mean([n["pressure"] for n in nodes if n["pressure"] is not None]),
                   "nodes": len(nodes), "readings": sum(n["n"] for n in nodes)}
        last = None
        if not nodes and newest:
            last = {"ts": newest["ts"], "name": clean(newest["node_name"]) or names.get(newest["node_id"]) or newest["node_id"]}
        return {"hours": hours, "nodes": nodes, "overall": overall, "latest_any": last, "unit": self.temp_unit()}

    # ---- AI page and per-node detail ---------------------------------------------------------------------------
    def ai_overview(self):
        """Everything the AI overview page shows: the model, what people asked, how it went."""
        b, a = self.bridge, self.bridge.audit
        counts = a.window_counts(24)
        ok = sum(v for k, v in counts.items() if k in ("answered", "action_ok"))
        return {"model": b.model, "ollama_ok": b.ollama_ok(), "paused": b.paused, "queue_depth": b.depth(),
                "mode": b.mode, "counts_24h": counts, "total_24h": sum(counts.values()), "ok_24h": ok,
                "avg_ms": a.avg_latency_ms(24), "top_askers": a.top_askers(24), "recent": a.recent_ai(8),
                "actions": [{"name": x.name, "tier": x.tier, "description": x.description} for x in actions.ACTIONS.values()]}

    def node_detail(self, node_id):
        """One node for its detail page: its row, distance from us, latest readings, last traceroute and access info. None if unknown."""
        b = self.bridge
        row = next((r for r in self.all_nodes() if r["id"] == node_id), None)
        if not row:
            return None
        traces = [t_ for t_ in b.traceroute.store.list(limit=200) if t_["node_id"] == node_id]
        us = self.us()
        if us and us.get("lat") is not None and row.get("lat") is not None and not row["us"]:
            row["distance_km"] = round(distance_km(us["lat"], us["lon"], row["lat"], row["lon"]), 2)
        return {"node": row, "readings": b.telemetry.store.list(node=node_id, status="ok", limit=5),
                "traceroute": traces[0] if traces else None,
                "access": b.node_access_info(node_id), "watched": b.telemetry.store.is_watched(node_id)}

    # ---- background thread -------------------------------------------------------------------------------------
    def _loop(self):
        """Background thread body. Wakes every `tick` seconds and runs whichever periodic job is due (flush every 30 s,
        health sample every sample_interval, node remember every 60 s, prune every prune_interval). Errors are printed, not raised."""
        last_flush = last_sample = last_prune = last_remember = 0.0
        first_sample_at = time.time() + 20          # let the node list fill in after a (re)connect
        while not self._stopping:
            time.sleep(self.tick)
            now = time.time()
            try:
                if self.stats_enabled and now - last_flush >= 30:
                    self.flush(); last_flush = now
                if (self.stats_enabled and now >= first_sample_at and now - last_sample >= self.sample_interval
                        and self.bridge.iface is not None):
                    self.sample(); last_sample = now
                if now - last_remember >= 60 and self.bridge.iface is not None:
                    self.remember(); last_remember = now
                if now - last_prune >= self.prune_interval:
                    self.prune(); last_prune = now
            except Exception as e:
                print(f"[error] mesh stats: {e}")

    def start(self):
        """Start the background thread (does nothing if already started)."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="mesh-stats")
            self._thread.start()

    def stop(self):
        """Ask the background thread to exit and write out the counters still in memory."""
        self._stopping = True
        self.flush()
