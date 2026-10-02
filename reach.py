"""Coverage: how far and how well this radio reaches, from signal and position data the bridge already collects, plus a walk test.

Everything here is passive. The overview reads the hourly signal statistics of nodes heard directly (nobody relayed them, so
the signal describes that node) and where those nodes were. A walk test records the signal of ONE node you pick, packet by
packet, while it moves (someone carries it); you start and stop it. Neither transmits anything.
"""
import math
import threading
import time
from collections import deque

from mesh import _num, _position, distance_km

MAX_DAYS = 30
MAX_POINTS = 3000
WALK_MAX_SAMPLES = 5000
WALK_MAX_SECONDS = 4 * 3600
SECTORS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS walk_sessions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id   TEXT NOT NULL,
    node_name TEXT,
    started   REAL NOT NULL,
    ended     REAL
);
CREATE TABLE IF NOT EXISTS walk_samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL,
    ts          REAL NOT NULL,
    snr         REAL,
    rssi        REAL,
    hops        INTEGER,                 -- relays the packet passed through (0 = direct); NULL when the sender doesn't say
    lat         REAL, lon REAL,          -- where the node was (its latest known position)
    distance_km REAL                     -- from this radio
);
CREATE INDEX IF NOT EXISTS idx_walk_samples_session ON walk_samples(session_id, ts);
"""


class CoverageError(ValueError):
    """Something the operator should see."""


def bearing(a_lat, a_lon, b_lat, b_lon):
    """Compass bearing in degrees (0 = north, 90 = east) from point a to point b."""
    p1, p2, dl = math.radians(a_lat), math.radians(b_lat), math.radians(b_lon - a_lon)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def sector_of(deg):
    return int(((deg % 360) + 22.5) // 45) % 8


def _fit(xs, ys):
    """Least-squares line through (x, y): (slope, intercept, r2) or None."""
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    icpt = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (icpt + slope * x)) ** 2 for x, y in zip(xs, ys))
    return slope, icpt, (1 - ss_res / ss_tot) if ss_tot > 0 else None


def trend(points, min_nodes=3):
    """Straight-line fit of signal against distance: {slope_db_per_km, at_0_km, n, nodes, r2} or None when there is too little to say.

    points: [(distance_km, snr, node_id)]. Needs at least 8 points from `min_nodes` nodes that span some distance; it is a
    description of what was measured, not a prediction of range."""
    pts = [(d, s, n) for d, s, n in points if _num(d) and _num(s)]
    if len(pts) < 8 or len({n for _, _, n in pts}) < min_nodes:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    if max(xs) - min(xs) < 0.2:
        return None
    fit = _fit(xs, ys)
    if fit is None:
        return None
    return {"slope_db_per_km": round(fit[0], 2), "at_0_km": round(fit[1], 1), "n": len(pts), "nodes": len({n for _, _, n in pts}),
            "r2": round(fit[2], 2) if fit[2] is not None else None}


class Coverage:
    def __init__(self, bridge):
        self.bridge = bridge
        self.audit = bridge.audit
        self._lock = threading.Lock()
        self._active = None                   # {id, node_id, started, n}
        self._seen = deque(maxlen=64)         # recent packet ids, so one packet counts once
        with self.audit.lock:
            self.audit.db.executescript(SCHEMA)
            # a session left open by a restart is closed at its last sample
            self.audit.db.execute("UPDATE walk_sessions SET ended = COALESCE((SELECT MAX(ts) FROM walk_samples w WHERE w.session_id = walk_sessions.id), started) WHERE ended IS NULL")
            self.audit.db.commit()

    # ---- the overview ---------------------------------------------------------------------------------------------
    def overview(self, days=30):
        m = self.bridge.mesh
        days = max(1, min(int(days), MAX_DAYS))
        lm = m.link_map(hours=days * 24)
        us = lm["us"]
        if not us:
            return {"days": days, "us": None, "nodes": [], "points": [], "sectors": [], "trend": None,
                    "message": "This radio has no position yet, so distances can't be worked out. Set it on the Map page (Set radio position)."}
        nodes = []
        for l in lm["links"]:
            if l["lat"] is None or l["distance_km"] is None:
                continue
            nodes.append({"id": l["id"], "name": l["name"], "lat": l["lat"], "lon": l["lon"], "distance_km": l["distance_km"],
                          "bearing": bearing(us["lat"], us["lon"], l["lat"], l["lon"]), "snr": l["snr"], "snr_max": l["snr_max"], "rssi": l["rssi"],
                          "n": l["n"], "last_ts": l["last_ts"]})
        sectors = [{"name": SECTORS[i], "center": i * 45, "max_km": None, "node": None, "nodes": 0} for i in range(8)]
        for n in nodes:
            s = sectors[sector_of(n["bearing"])]
            s["nodes"] += 1
            if s["max_km"] is None or n["distance_km"] > s["max_km"]:
                s["max_km"], s["node"] = n["distance_km"], n["name"] or n["id"]
        # hourly points: the node's signal that hour against how far away it was then
        by_id = {n["id"]: n for n in nodes}
        cutoff = int(time.time() // 3600) - days * 24
        with self.audit.lock:
            rows = self.audit.db.execute("SELECT hour, node_id, n, snr_sum, rssi_sum FROM mesh_links WHERE hour > ? ORDER BY hour DESC LIMIT ?",
                                         (cutoff, MAX_POINTS * 2)).fetchall()
            trails = {}
            for nid in {r["node_id"] for r in rows if r["node_id"] in by_id}:
                trails[nid] = self.audit.db.execute("SELECT ts, lat, lon FROM node_positions WHERE node_id=? ORDER BY ts", (nid,)).fetchall()
        points = []
        for r in rows:
            n = by_id.get(r["node_id"])
            if not n:
                continue
            end = (r["hour"] + 1) * 3600
            pos = None
            for t in trails.get(r["node_id"], []):       # where it was that hour: the newest trail point at or before it
                if t["ts"] <= end:
                    pos = t
                else:
                    break
            lat, lon = (pos["lat"], pos["lon"]) if pos else (n["lat"], n["lon"])
            points.append({"d": round(distance_km(us["lat"], us["lon"], lat, lon), 3), "snr": round(r["snr_sum"] / r["n"], 1),
                           "rssi": round(r["rssi_sum"] / r["n"], 0), "id": r["node_id"], "ts": r["hour"] * 3600})
            if len(points) >= MAX_POINTS:
                break
        far = max(nodes, key=lambda n: n["distance_km"]) if nodes else None
        ds = sorted(n["distance_km"] for n in nodes)
        return {"days": days, "us": us, "nodes": sorted(nodes, key=lambda n: -n["snr"]), "points": points, "sectors": sectors,
                "trend": trend([(p["d"], p["snr"], p["id"]) for p in points]),
                "summary": {"nodes": len(nodes), "farthest": {"name": far["name"] or far["id"], "km": far["distance_km"]} if far else None,
                            "median_km": ds[len(ds) // 2] if ds else None, "without_position": len(lm["links"]) - len(nodes)}}

    # ---- walk test --------------------------------------------------------------------------------------------------
    def start_walk(self, node_id):
        b = self.bridge
        node_id = node_id.strip().lower() if isinstance(node_id, str) else ""
        row = next((r for r in b.mesh.all_nodes() if r["id"] == node_id), None)
        if row is None:
            raise CoverageError("This radio has never heard that node. Pick one from the list.")
        if row["us"]:
            raise CoverageError("Pick the node that will be moving, not this radio.")
        with self._lock:
            if self._active:
                raise CoverageError("A walk test is already running. Stop it first.")
            with self.audit.lock:
                cur = self.audit.db.execute("INSERT INTO walk_sessions (node_id, node_name, started) VALUES (?,?,?)", (node_id, row["name"] or row["short"], time.time()))
                self.audit.db.commit()
            self._active = {"id": cur.lastrowid, "node_id": node_id, "started": time.time(), "n": 0}
            self._seen.clear()
        print(f"[walk] started for {node_id}")
        return self._active["id"]

    def stop_walk(self):
        with self._lock:
            a, self._active = self._active, None
        if a is None:
            return False
        with self.audit.lock:
            self.audit.db.execute("UPDATE walk_sessions SET ended=? WHERE id=?", (time.time(), a["id"]))
            self.audit.db.commit()
        print(f"[walk] stopped after {a['n']} samples")
        return True

    def on_packet(self, packet, interface):
        """Subscribed to every packet. Records one when it is from the node being walked. Must stay cheap."""
        a = self._active
        if a is None:
            return
        try:
            if packet.get("fromId") != a["node_id"]:
                return
            if time.time() - a["started"] > WALK_MAX_SECONDS or a["n"] >= WALK_MAX_SAMPLES:
                self.stop_walk()
                return
            snr, rssi = packet.get("rxSnr"), packet.get("rxRssi")
            if not (_num(snr) and _num(rssi) and -40 <= snr <= 40 and -200 <= rssi < 0):
                return                                          # not a real radio reception (for example relayed in over MQTT)
            pid = packet.get("id")
            if pid is not None and pid in self._seen:
                return
            self._seen.append(pid)
            hs, hl = packet.get("hopStart"), packet.get("hopLimit")
            ok = lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 7
            hops = hs - hl if ok(hs) and ok(hl) and hs > 0 and hl <= hs else None
            lat = lon = None
            pos = (packet.get("decoded") or {}).get("position")
            if isinstance(pos, dict):
                lat, lon = _position({"position": pos})
            if lat is None:
                entry = (getattr(interface, "nodesByNum", None) or {}).get(packet.get("from"))
                lat, lon = _position(entry or {})
            us = self.bridge.mesh.us()
            dist = distance_km(us["lat"], us["lon"], lat, lon) if lat is not None and us and us.get("lat") is not None else None
            with self.audit.lock:
                self.audit.db.execute("INSERT INTO walk_samples (session_id, ts, snr, rssi, hops, lat, lon, distance_km) VALUES (?,?,?,?,?,?,?,?)",
                                      (a["id"], time.time(), snr, rssi, hops, lat, lon, dist))
                self.audit.db.commit()
            a["n"] += 1
        except Exception:
            pass

    def state(self):
        """The running test (if any) and the saved sessions, newest first."""
        with self.audit.lock:
            rows = self.audit.db.execute(
                "SELECT s.id, s.node_id, s.node_name, s.started, s.ended, COUNT(w.id) AS samples, SUM(CASE WHEN w.hops = 0 THEN 1 ELSE 0 END) AS direct, "
                "MAX(CASE WHEN w.hops = 0 THEN w.distance_km END) AS farthest_direct, MIN(w.snr) AS weakest_snr "
                "FROM walk_sessions s LEFT JOIN walk_samples w ON w.session_id = s.id GROUP BY s.id ORDER BY s.id DESC LIMIT 30").fetchall()
        a = self._active
        return {"active": {"id": a["id"], "node_id": a["node_id"], "started": a["started"], "samples": a["n"]} if a else None,
                "sessions": [dict(r, direct=r["direct"] or 0) for r in rows]}

    def session(self, sid):
        with self.audit.lock:
            s = self.audit.db.execute("SELECT * FROM walk_sessions WHERE id=?", (sid,)).fetchone()
            if s is None:
                return None
            samples = [dict(r) for r in self.audit.db.execute(
                "SELECT ts, snr, rssi, hops, lat, lon, distance_km FROM walk_samples WHERE session_id=? ORDER BY ts LIMIT ?", (sid, WALK_MAX_SAMPLES)).fetchall()]
        direct = [x for x in samples if x["hops"] == 0 and x["distance_km"] is not None]
        t = trend([(x["distance_km"], x["snr"], "walk") for x in direct], min_nodes=1)
        return {"session": dict(s), "samples": samples, "trend": t,
                "summary": {"samples": len(samples), "direct": len(direct),
                            "farthest_direct_km": max((x["distance_km"] for x in direct), default=None),
                            "weakest_snr": min((x["snr"] for x in samples), default=None),
                            "strongest_snr": max((x["snr"] for x in samples), default=None)}}

    def delete_session(self, sid):
        if not isinstance(sid, int) or isinstance(sid, bool):
            raise CoverageError("Pick a session.")
        if self._active and self._active["id"] == sid:
            raise CoverageError("Stop the running walk test first.")
        with self.audit.lock:
            self.audit.db.execute("DELETE FROM walk_samples WHERE session_id=?", (sid,))
            n = self.audit.db.execute("DELETE FROM walk_sessions WHERE id=?", (sid,)).rowcount
            self.audit.db.commit()
        return n
