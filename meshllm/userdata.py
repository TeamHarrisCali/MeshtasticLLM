"""Things you add yourself: a label, a note and a star for any node, and saved snippets of text for quick posting.

Both are local to this PC (the database), never sent over the radio, and never given to the AI. A snippet only fills the
message box; it is never sent until you press Send.
"""
import re
import threading
import time

from meshllm.channel import ChannelError, clean as clean_channel_text

NODE_ID_RE = re.compile(r"^![0-9a-f]{8}$")
LABEL_MAX, NOTE_MAX = 40, 500
SNIPPET_MAX_PER_TARGET = 30
SNIPPET_TARGETS = ("channel", "dm")
DM_MAX = 700                                      # the same limit as the Conversations message box
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

SCHEMA = """
CREATE TABLE IF NOT EXISTS node_notes (
    node_id  TEXT PRIMARY KEY,
    label    TEXT,                 -- your own name for the node ("Lodge repeater")
    note     TEXT,
    starred  INTEGER NOT NULL DEFAULT 0,
    updated  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS snippets (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    target  TEXT NOT NULL,         -- channel | dm
    label   TEXT NOT NULL,
    text    TEXT NOT NULL,
    created REAL NOT NULL
);
"""


class UserDataError(ValueError):
    """Something the operator should see (too long, too many, unknown node)."""


def _text(value, limit, what, multiline=False):
    """Clean operator-typed text: None passes through, non-text and over-long values raise UserDataError.

    Control characters are removed (newline and tab survive). Single-line text has its whitespace collapsed; multiline text
    keeps its line breaks and is only trimmed. `limit` is in characters; `what` names the field in the error message."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise UserDataError(f"The {what} must be text.")
    value = CONTROL.sub("", value.replace("\r\n", "\n").replace("\r", "\n"))
    value = value.strip() if multiline else " ".join(value.split())
    if len(value) > limit:
        raise UserDataError(f"The {what} is too long ({len(value)} of {limit} characters).")
    return value


class UserData:
    """Node labels/notes/stars and saved snippets, stored in the audit database.

    Safe to use from several threads: `_lock` makes read-modify-write steps atomic, and the database itself is only
    touched under the shared audit lock."""

    def __init__(self, bridge):
        self.audit = bridge.audit
        self._lock = threading.Lock()
        with self.audit.lock:
            self.audit.db.executescript(SCHEMA)
            self.audit.db.commit()

    # ---- node labels, notes and stars -----------------------------------------------------------------------------
    def notes(self):
        """{node_id: {label, note, starred}} for every node you have annotated."""
        with self.audit.lock:
            rows = self.audit.db.execute("SELECT node_id, label, note, starred FROM node_notes").fetchall()
        return {r["node_id"]: {"label": r["label"] or "", "note": r["note"] or "", "starred": bool(r["starred"])} for r in rows}

    def annotate(self, rows, key="id"):
        """Add `label`, `note` and `starred` to each node dict (in place) and return the list."""
        notes = self.notes()
        for r in rows:
            n = notes.get(r.get(key)) or {}
            r["label"], r["note"], r["starred"] = n.get("label", ""), n.get("note", ""), n.get("starred", False)
        return rows

    def set_note(self, node_id, label=None, note=None, starred=None):
        """Change any of label / note / starred (None = leave alone). A node with nothing set has no row."""
        node_id = node_id.strip().lower() if isinstance(node_id, str) else ""
        if not NODE_ID_RE.match(node_id):
            raise UserDataError("Node ID must look like !1a2b3c4d")
        label, note = _text(label, LABEL_MAX, "label"), _text(note, NOTE_MAX, "note", multiline=True)
        if starred is not None and not isinstance(starred, bool):
            raise UserDataError("starred must be true or false.")
        with self._lock:                  # read-merge-write must not interleave with another edit of the same node
            cur = self.notes().get(node_id) or {"label": "", "note": "", "starred": False}
            new = {"label": cur["label"] if label is None else label, "note": cur["note"] if note is None else note,
                   "starred": cur["starred"] if starred is None else starred}
            with self.audit.lock:
                if not (new["label"] or new["note"] or new["starred"]):
                    self.audit.db.execute("DELETE FROM node_notes WHERE node_id=?", (node_id,))
                else:
                    self.audit.db.execute("INSERT OR REPLACE INTO node_notes (node_id, label, note, starred, updated) VALUES (?,?,?,?,?)",
                                          (node_id, new["label"], new["note"], int(new["starred"]), time.time()))
                self.audit.db.commit()
        return new

    # ---- snippets ---------------------------------------------------------------------------------------------------
    def snippets(self, target=None):
        """Saved snippets as [{id, target, label, text}], oldest first; only one target ("channel" or "dm") if given."""
        with self.audit.lock:
            if target:
                rows = self.audit.db.execute("SELECT id, target, label, text FROM snippets WHERE target=? ORDER BY id", (target,)).fetchall()
            else:
                rows = self.audit.db.execute("SELECT id, target, label, text FROM snippets ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def add_snippet(self, target, text, label=None):
        """Save a snippet and return its new id. Raises UserDataError for a bad target, empty or too-long text, a duplicate,
        or when the per-target limit is reached. Nothing is transmitted."""
        if target not in SNIPPET_TARGETS:
            raise UserDataError("A snippet is for the public channel or for direct messages.")
        if not isinstance(text, str):
            raise UserDataError("Type the snippet first.")
        try:
            text = clean_channel_text(text) if target == "channel" else _text(text, DM_MAX, "snippet", multiline=True)
        except ChannelError as e:                      # the same size rule as a real post, so a saved snippet can always be posted
            raise UserDataError(str(e))
        if not text:
            raise UserDataError("Type the snippet first.")
        label = _text(label, 30, "label") or (text.replace("\n", " ")[:27] + ("..." if len(text) > 27 else ""))
        with self._lock:
            if len(self.snippets(target)) >= SNIPPET_MAX_PER_TARGET:
                raise UserDataError(f"You already have {SNIPPET_MAX_PER_TARGET} snippets here. Delete one first.")
            if any(s["text"] == text for s in self.snippets(target)):
                raise UserDataError("That snippet is already saved.")
            with self.audit.lock:
                cur = self.audit.db.execute("INSERT INTO snippets (target, label, text, created) VALUES (?,?,?,?)", (target, label, text, time.time()))
                self.audit.db.commit()
                return cur.lastrowid

    def delete_snippet(self, sid):
        """Delete the snippet with this integer id; returns how many rows were removed (0 if it did not exist)."""
        if not isinstance(sid, int) or isinstance(sid, bool):
            raise UserDataError("Pick a snippet to delete.")
        with self.audit.lock:
            n = self.audit.db.execute("DELETE FROM snippets WHERE id=?", (sid,)).rowcount
            self.audit.db.commit()
        return n
