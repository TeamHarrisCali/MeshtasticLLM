"""Traceroute: ask the mesh which way a packet takes to a node and back, and keep the result.

Each result stores the path in both directions - every node on it with the signal strength it received
with and, when the radio knows it, its position at that moment - so the dashboard can draw the route
on a map later. Only the operator (web UI) can cause a traceroute to be transmitted,
one at a time. There are no automatic or scheduled traceroutes: each
one makes every node along the way repeat a packet.
"""
import json
import re
import threading
import time

from google.protobuf.json_format import MessageToDict
from meshtastic.protobuf import mesh_pb2, portnums_pb2

NODE_ID_RE = re.compile(r"^![0-9a-f]{8}$")
BROADCAST_NUM = 0xFFFFFFFF   # the firmware puts this in a route where a hop's identity wasn't reported
UNKNOWN_SNR = -128           # "unknown" in the reply's signal values (which are in quarter-dB units)

SCHEMA = """
CREATE TABLE IF NOT EXISTS traceroutes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,     -- when the answer arrived (or we gave up)
    node_id         TEXT NOT NULL,     -- the node we traced to
    node_name       TEXT,
    hop_limit       INTEGER,
    status          TEXT NOT NULL,     -- ok | timeout | error
    detail          TEXT,
    latency_ms      INTEGER,
    relays_towards  INTEGER,           -- nodes between us and the target on the way there
    relays_back     INTEGER,           -- ... and on the way back (NULL if the reply didn't say)
    path_towards    TEXT,              -- JSON: [{num, id, name, snr, lat, lon}] us -> target
    path_back       TEXT,              -- JSON: same, target -> us
    raw             TEXT               -- the reply as the radio decoded it, as JSON
);
CREATE INDEX IF NOT EXISTS idx_traceroutes_ts ON traceroutes(ts);
"""


class TracerouteError(Exception):
    """Something the operator should see (bad input, busy, radio away)."""


def _position(node):
    pos = (node or {}).get("position") or {}
    lat, lon = pos.get("latitude"), pos.get("longitude")
    if lat is None and pos.get("latitudeI") is not None:
        lat = pos["latitudeI"] * 1e-7
    if lon is None and pos.get("longitudeI") is not None:
        lon = pos["longitudeI"] * 1e-7
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None, None
    if (lat == 0 and lon == 0) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None  # 0,0 is how "no position" is usually reported
    return lat, lon


def node_entry(num, snr_raw, nodes_by_num):
    """One hop of a path: who it is, the signal it received with (dB, None if unknown), and where it is."""
    snr = None if snr_raw is None or snr_raw == UNKNOWN_SNR else snr_raw / 4
    if num == BROADCAST_NUM:
        return {"num": None, "id": None, "name": "Unknown node", "snr": snr, "lat": None, "lon": None}
    node = nodes_by_num.get(num) or {}
    user = node.get("user") or {}
    lat, lon = _position(node)
    return {"num": num, "id": user.get("id") or f"!{num:08x}", "name": (user.get("longName") or "").strip() or None,
            "snr": snr, "lat": lat, "lon": lon}


def build_path(start_num, relays, end_num, snrs, nodes_by_num):
    """[start] + relays + [end]. `snrs` has one entry per link (so len(relays)+1); it's used only if it does."""
    usable = snrs is not None and len(snrs) == len(relays) + 1
    chain = [start_num, *relays, end_num]
    return [node_entry(n, snrs[i - 1] if usable and i > 0 else None, nodes_by_num) for i, n in enumerate(chain)]


def parse_response(packet, our_num, nodes_by_num):
    """A traceroute reply -> (path_towards, path_back or None, raw dict), or raises TracerouteError."""
    decoded = packet.get("decoded") or {}
    if decoded.get("portnum") == "ROUTING_APP":
        reason = (decoded.get("routing") or {}).get("errorReason", "NONE")
        raise TracerouteError(f"radio reported {reason}")
    rd = decoded.get("traceroute")
    if rd is None and decoded.get("payload") is not None:   # raw payload: decode it ourselves
        msg = mesh_pb2.RouteDiscovery()
        msg.ParseFromString(decoded["payload"])
        rd = MessageToDict(msg)
    if not isinstance(rd, dict):
        raise TracerouteError("the reply didn't contain a route")
    # the real library adds the protobuf object itself under "raw"; keep only the plain decoded fields
    rd = {k: v for k, v in rd.items() if k != "raw"}
    route, route_back = rd.get("route") or [], rd.get("routeBack") or []
    target = packet.get("from")
    if not isinstance(target, int):
        raise TracerouteError("the reply didn't say who it came from")
    us = packet.get("to") if isinstance(packet.get("to"), int) else our_num
    towards = build_path(us, route, target, rd.get("snrTowards"), nodes_by_num)
    snr_back = rd.get("snrBack")
    # the way back is only meaningful if the reply carries a hop counter and the signal list matches
    back_ok = "hopStart" in packet and snr_back is not None and len(snr_back) == len(route_back) + 1
    back = build_path(target, route_back, us, snr_back, nodes_by_num) if back_ok else None
    return towards, back, rd


class TracerouteStore:
    def __init__(self, audit):
        self.audit = audit
        with audit.lock:
            audit.db.executescript(SCHEMA)
            audit.db.commit()

    @staticmethod
    def _row(r):
        d = dict(r)
        for k in ("path_towards", "path_back", "raw"):
            d[k] = json.loads(d[k]) if d.get(k) else None
        return d

    def add(self, **c):
        cols = ["ts", "node_id", "node_name", "hop_limit", "status", "detail", "latency_ms", "relays_towards",
                "relays_back", "path_towards", "path_back", "raw"]
        # default=str: whatever odd object the radio library hands over must never stop a result being saved
        vals = [json.dumps(c.get(k), default=str) if k in ("path_towards", "path_back", "raw") and c.get(k) is not None else c.get(k)
                for k in cols]
        with self.audit.lock:
            cur = self.audit.db.execute(f"INSERT INTO traceroutes ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
            self.audit.db.commit()
            return cur.lastrowid

    def get(self, rid):
        with self.audit.lock:
            r = self.audit.db.execute("SELECT * FROM traceroutes WHERE id=?", (rid,)).fetchone()
        return self._row(r) if r else None

    def list(self, limit=30, before=None):
        sql, args = "SELECT * FROM traceroutes", []
        if before:
            sql += " WHERE id < ?"
            args.append(int(before))
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 200)))
        with self.audit.lock:
            return [self._row(r) for r in self.audit.db.execute(sql, args)]


class TracerouteService:
    """Sends traceroutes on request. Needs the bridge for its radio and the shared radio lock."""

    def __init__(self, bridge):
        self.bridge = bridge
        a = bridge.args
        self.store = TracerouteStore(bridge.audit)
        self.timeout = getattr(a, "traceroute_timeout", 60.0)
        self.cooldown = getattr(a, "traceroute_cooldown", 30.0)
        self._busy = getattr(bridge, "radio_request_lock", None) or threading.Lock()
        self._last = {}
        self._manual = {}
        self._next = 0

    @staticmethod
    def check(node_id, hop_limit):
        node_id = node_id.strip().lower() if isinstance(node_id, str) else ""
        if not NODE_ID_RE.match(node_id):
            raise TracerouteError("Node ID must look like !1a2b3c4d")
        if isinstance(hop_limit, bool) or not isinstance(hop_limit, int) or not (1 <= hop_limit <= 7):
            raise TracerouteError("Hop limit must be a whole number from 1 to 7.")
        return node_id

    def request(self, node_id, hop_limit=7):
        """Trace the route to one node. Blocks up to the timeout; returns the stored row."""
        node_id = self.check(node_id, hop_limit)
        iface = self.bridge.iface
        name = self.bridge.node_name(node_id)

        def record(status, **kw):
            try:
                rid = self.store.add(ts=time.time(), node_id=node_id, node_name=name, hop_limit=hop_limit, status=status, **kw)
            except Exception as e:  # last resort: still keep a row saying what happened
                print(f"[error] couldn't store the full traceroute: {e!r}")
                rid = self.store.add(ts=time.time(), node_id=node_id, node_name=name, hop_limit=hop_limit, status="error",
                                     detail=f"the route arrived but couldn't be saved ({e})"[:200], latency_ms=kw.get("latency_ms"))
            return self.store.get(rid)

        if iface is None:
            return record("error", detail="radio not connected")
        done, reply = threading.Event(), {}

        def on_response(packet):  # runs on the radio's receive thread
            decoded = packet.get("decoded") or {}
            if decoded.get("portnum") == "ROUTING_APP" and (decoded.get("routing") or {}).get("errorReason", "NONE") == "NONE":
                return  # a plain acknowledgement, not the answer
            reply["packet"] = packet
            done.set()

        t0 = time.monotonic()
        try:
            iface.sendData(mesh_pb2.RouteDiscovery(), destinationId=node_id, portNum=portnums_pb2.PortNum.TRACEROUTE_APP,
                           wantResponse=True, onResponse=on_response, channelIndex=0, hopLimit=hop_limit)
        except Exception as e:
            return record("error", detail=f"could not send: {e}"[:200])
        if not done.wait(self.timeout):
            return record("timeout", latency_ms=int(self.timeout * 1000),
                          detail=f"no answer within {self.timeout:.0f}s (node off or out of reach within {hop_limit} hops)")
        latency = int((time.monotonic() - t0) * 1000)
        try:
            nodes = getattr(iface, "nodesByNum", None) or {}
            our_num = getattr(getattr(iface, "myInfo", None), "my_node_num", None)
            towards, back, raw = parse_response(reply["packet"], our_num, nodes)
        except TracerouteError as e:
            return record("error", detail=str(e), latency_ms=latency)
        except Exception as e:
            return record("error", detail=f"couldn't read the reply: {e}"[:200], latency_ms=latency)
        return record("ok", latency_ms=latency, relays_towards=len(towards) - 2,
                      relays_back=None if back is None else len(back) - 2,
                      path_towards=towards, path_back=back, raw=raw)

    # ---- from the web UI: start in the background, poll for the result ------------------
    def start_manual(self, node_id, hop_limit=7):
        node_id = self.check(node_id, hop_limit)
        if self.bridge.iface is None:
            raise TracerouteError("The radio isn't connected right now.")
        if self._busy.locked():
            raise TracerouteError("Another traceroute is in progress. Wait for it to finish.")
        wait = self.cooldown - (time.monotonic() - self._last.get(node_id, -1e9))
        if wait > 0:
            raise TracerouteError(f"That node was traced moments ago (every traceroute makes nodes on the path "
                                  f"repeat a packet). Try again in {int(wait) + 1}s.")
        self._last[node_id] = time.monotonic()
        self._next += 1
        rid = self._next
        self._manual[rid] = {"id": rid, "status": "waiting", "node_id": node_id}
        for old in sorted(self._manual)[:-20]:
            del self._manual[old]

        def run():
            if not self._busy.acquire(blocking=False):
                self._manual[rid] = {**self._manual[rid], "status": "error", "detail": "another request was in progress"}
                return
            try:
                row = self.request(node_id, hop_limit)
                self._manual[rid] = {**self._manual[rid], "status": row["status"], "detail": row["detail"], "trace": row}
            except Exception as e:
                self._manual[rid] = {**self._manual[rid], "status": "error", "detail": str(e)}
            finally:
                self._busy.release()

        threading.Thread(target=run, daemon=True, name="traceroute").start()
        return rid

    def manual_state(self, rid):
        return self._manual.get(rid)
