"""The default public channel: read what people post on it, and post there yourself from the web UI.

This is a human-only feature. The AI has no part in it: nothing on the channel is ever given to the model, a
"/ai ..." typed on the channel is not answered, and the AI has no tool that can post here. Only the operator
(the Channel page, one message at a time) can cause a transmission on the channel. Only the primary channel
(index 0, LongFast with the well-known default key) is recorded and used; private channels are never stored.
Messages are kept for RETENTION_DAYS and can be cleared from the page.
"""
import re
import threading
import time

PRIMARY = 0                       # channel index of the default public channel
CHANNEL_DEST = "^all"             # the library's name for "everyone"
MAX_BYTES = 200                   # one radio message only: a public channel isn't the place for split-up messages
MIN_GAP_S = 5.0                   # between two posts
MAX_PER_HOUR = 30
RETENTION_DAYS = 30
BROADCAST_NUM = 0xFFFFFFFF
DEFAULT_KEY = b"\x01"             # the firmware's shorthand for the default public key ("AQ==")

SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    direction TEXT NOT NULL,        -- in (heard) | out (posted by you)
    node_id   TEXT,                 -- who sent it (NULL for 'out')
    node_name TEXT,
    text      TEXT NOT NULL,
    status    TEXT,                 -- out only: queued | sent | heard | failed
    rx_snr    REAL,
    rx_rssi   REAL,
    hops      INTEGER,
    packet_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_channel_messages_ts ON channel_messages(ts);
"""

CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class ChannelError(Exception):
    """Something the operator should see (empty message, too long, too fast, radio away)."""


def clean(text):
    """The text to post, or ChannelError."""
    if not isinstance(text, str):
        raise ChannelError("Type a message first.")
    text = CONTROL_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n")).strip()
    if not text:
        raise ChannelError("Type a message first.")
    n = len(text.encode("utf-8"))
    if n > MAX_BYTES:
        raise ChannelError(f"Too long for one radio message: {n} of {MAX_BYTES} bytes. Shorten it (public-channel messages are never split).")
    return text


def describe(channels):
    """About the primary channel from the radio's channel list: {name, default_key, known}. `channels` is iface.localNode.channels."""
    for c in channels or []:
        if getattr(c, "index", None) == PRIMARY:
            s = getattr(c, "settings", None)
            name = (getattr(s, "name", "") or "").strip()
            psk = bytes(getattr(s, "psk", b"") or b"")
            return {"name": name or "LongFast (default name)", "default_key": psk == DEFAULT_KEY, "known": True}
    return {"name": None, "default_key": None, "known": False}


class ChannelService:
    """Records the primary public channel and posts to it for the operator. Never involves the AI.

    on_text runs on the radio's receive thread, post on web-server threads, and transmit on the sender thread; database
    access goes through the shared audit lock and `_lock` guards the post-rate bookkeeping."""

    def __init__(self, bridge):
        self.bridge = bridge
        self.audit = bridge.audit
        with self.audit.lock:
            self.audit.db.executescript(SCHEMA)
            self.audit.db.commit()
        self.gap = getattr(bridge.args, "channel_gap", MIN_GAP_S)
        self.per_hour = getattr(bridge.args, "channel_per_hour", MAX_PER_HOUR)
        self._posts = []                     # times of recent posts
        self._lock = threading.Lock()

    # ---- hearing (passive) ------------------------------------------------------------------------------------------
    def on_text(self, packet, interface=None):
        """Every text message the radio hears; keeps the ones posted to the primary channel. Never replies, never enqueues."""
        try:
            text = (packet.get("decoded") or {}).get("text") or ""
            sender = packet.get("fromId")
            if not text or not isinstance(text, str) or not sender:
                return
            # only broadcasts on the primary channel are stored; DMs and private channels must never reach this table
            if packet.get("to") != BROADCAST_NUM or (packet.get("channel") or 0) != PRIMARY:
                return                                      # a DM, or some other channel
            my = getattr(getattr(interface, "myInfo", None), "my_node_num", None)
            if my is not None and packet.get("from") == my:
                return                                      # our own post echoed back: already stored when it was sent
            pid = packet.get("id")
            num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
            whole = lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 7
            hs, hl = packet.get("hopStart"), packet.get("hopLimit")      # hops used = start - remaining; unknown if either is missing
            hops = hs - hl if whole(hs) and whole(hl) and hs >= hl else None
            with self.audit.lock:
                if pid is not None and self.audit.db.execute(
                        "SELECT 1 FROM channel_messages WHERE packet_id=? AND direction='in' AND ts > ?", (pid, time.time() - 3600)).fetchone():
                    return                                  # the same packet heard twice
                self.audit.db.execute(
                    "INSERT INTO channel_messages (ts, direction, node_id, node_name, text, rx_snr, rx_rssi, hops, packet_id) VALUES (?,?,?,?,?,?,?,?,?)",
                    (time.time(), "in", sender, self.bridge.node_name(sender), CONTROL_RE.sub("", text)[:500],
                     num(packet.get("rxSnr")), num(packet.get("rxRssi")), hops, pid if isinstance(pid, int) else None))
                self.audit.db.commit()
            print(f"[channel] {sender}: {text[:80]}")
        except Exception as e:                              # a malformed packet must never disturb the radio thread
            print(f"[error] channel: {e}")

    # ---- posting (operator only) ----------------------------------------------------------------------------------------
    def post(self, text):
        """Queue one post for the radio. Returns the row id. Raises ChannelError."""
        text = clean(text)
        if self.bridge.iface is None:
            raise ChannelError("No radio is connected right now.")
        # rate limits protect the shared channel: everyone in range hears every post
        with self._lock:
            now = time.time()
            self._posts = [t for t in self._posts if now - t < 3600]
            if self._posts and now - self._posts[-1] < self.gap:
                raise ChannelError(f"Wait {int(self.gap - (now - self._posts[-1])) + 1} s between posts: everyone in range hears each one.")
            if len(self._posts) >= self.per_hour:
                raise ChannelError(f"That's {self.per_hour} posts in the last hour. Give the channel a rest.")
            self._posts.append(now)
        with self.audit.lock:
            cur = self.audit.db.execute("INSERT INTO channel_messages (ts, direction, node_name, text, status) VALUES (?,?,?,?,?)",
                                        (now, "out", "You", text, "queued"))
            self.audit.db.commit()
            rid = cur.lastrowid
        self.bridge.outbox.put((rid, CHANNEL_DEST, [text], 0))
        print(f"[channel] posting: {text}")
        return rid

    def transmit(self, iface, rid, text):
        """Called by the bridge's sender thread (the only thread that writes to the radio)."""
        def onAckNak(packet):                               # a neighbour rebroadcasting it counts as an acknowledgement
            """Radio callback with the delivery result: routing error NONE marks the post "heard", anything else "failed"."""
            reason = (packet.get("decoded", {}).get("routing", {}) or {}).get("errorReason", "NONE")
            self.set_status(rid, "heard" if reason == "NONE" else "failed")
        try:
            iface.sendText(text, destinationId=CHANNEL_DEST, channelIndex=PRIMARY, wantAck=True, onResponse=onAckNak)
            self.set_status(rid, "sent")
            print(f"[tx] channel: {text}")
        except Exception as e:
            print(f"[error] channel send: {e}")
            self.set_status(rid, "failed")

    def give_up(self, rid):
        """Mark a queued post as failed (the sender thread could not send it)."""
        self.set_status(rid, "failed")

    def set_status(self, rid, status):
        """Set the delivery status (queued/sent/heard/failed) of one of your posts; unknown ids are ignored."""
        with self.audit.lock:
            cur = self.audit.db.execute("SELECT status FROM channel_messages WHERE id=?", (rid,)).fetchone()
            if cur is None or (cur["status"] == "heard" and status == "sent"):
                return                                      # never step back from 'heard'
            self.audit.db.execute("UPDATE channel_messages SET status=? WHERE id=?", (status, rid))
            self.audit.db.commit()

    # ---- reading and housekeeping ----------------------------------------------------------------------------------
    def info(self):
        """The Channel page header: primary channel details, connection state, limits and posts made in the last hour."""
        b = self.bridge
        chans = None
        try:
            chans = b.iface.localNode.channels if b.iface else None
        except Exception:
            pass
        d = describe(chans)
        d.update(connected=b.iface is not None, max_bytes=MAX_BYTES, gap=self.gap, per_hour=self.per_hour, retention_days=RETENTION_DAYS,
                 sent_last_hour=len([t for t in self._posts if time.time() - t < 3600]))
        return d

    def list(self, limit=100, after=None):
        """Newest `limit` messages, oldest first (chat order). `after` = only rows with a larger id."""
        limit = max(1, min(int(limit), 300))               # cap so one request cannot pull the whole table
        with self.audit.lock:
            if after is not None:
                rows = self.audit.db.execute("SELECT * FROM channel_messages WHERE id > ? ORDER BY id LIMIT ?", (int(after), limit)).fetchall()
            else:
                rows = list(reversed(self.audit.db.execute("SELECT * FROM channel_messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()))
        out = [dict(r) for r in rows]
        labels = self.bridge.userdata.notes() if hasattr(self.bridge, "userdata") else {}
        for r in out:
            r["label"] = (labels.get(r["node_id"]) or {}).get("label", "")
        return out

    def prune(self):
        """Delete messages older than RETENTION_DAYS; returns how many were removed."""
        with self.audit.lock:
            n = self.audit.db.execute("DELETE FROM channel_messages WHERE ts < ?", (time.time() - RETENTION_DAYS * 86400,)).rowcount
            self.audit.db.commit()
        return n

    def clear(self):
        """Delete every stored channel message (the operator's Clear button); returns how many were removed."""
        with self.audit.lock:
            n = self.audit.db.execute("DELETE FROM channel_messages").rowcount
            self.audit.db.commit()
        return n
