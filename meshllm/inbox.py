"""What is new since you last looked (for the sidebar badges and alerts), and one search across everything the bridge stores.

Both only read the database. Nothing here transmits or talks to the AI.
"""
import re

SEARCH_LIMIT = 8                  # results per kind
MIN_QUERY = 2


def my_names(bridge):
    """The names other people would use for this radio (long and short), for spotting a mention."""
    try:
        u = bridge.iface.getMyUser() or {}
    except Exception:
        return []
    names = {(u.get("longName") or "").strip(), (u.get("shortName") or "").strip()}
    return sorted((n for n in names if len(n) >= 3), key=len, reverse=True)


def unread(bridge, channel_after=None, dm_after=None):
    """Counts of public-channel posts and direct messages newer than the ids the page last showed, plus current alerts.

    Passing None for an id means "this page has never looked": the counts are then 0 and `latest` is where to start from, so
    a first visit is not flooded with history."""
    # the audit database connection is shared with other threads, so every query runs under its lock
    db, lock = bridge.audit.db, bridge.audit.lock
    with lock:
        ch_latest = db.execute("SELECT COALESCE(MAX(id), 0) FROM channel_messages WHERE direction='in'").fetchone()[0]
        dm_latest = db.execute("SELECT COALESCE(MAX(id), 0) FROM requests WHERE kind='inbound'").fetchone()[0]
        ch_rows = [] if channel_after is None else db.execute(
            "SELECT id, node_id, node_name, text FROM channel_messages WHERE direction='in' AND id > ? ORDER BY id DESC LIMIT 50", (channel_after,)).fetchall()
        dm_rows = [] if dm_after is None else db.execute(
            "SELECT id, node_id, node_name, prompt FROM requests WHERE kind='inbound' AND id > ? ORDER BY id DESC LIMIT 50", (dm_after,)).fetchall()
    names = my_names(bridge)
    # whole-word, case-insensitive match so a short name does not trigger inside a longer word
    rx =re.compile(r"(?<!\w)(?:" + "|".join(re.escape(n) for n in names) + r")(?!\w)", re.I) if names else None
    mentions = sum(1 for r in ch_rows if rx and rx.search(r["text"] or ""))
    newest = lambda rows, text: ({"id": rows[0]["id"], "name": rows[0]["node_name"] or rows[0]["node_id"], "text": rows[0][text]} if rows else None)
    alerts = []
    try:
        alerts = [a for a in bridge.mesh.alerts(bridge.mesh.nodes()) if a.get("kind")]
    except Exception:
        pass
    return {"channel": {"count": len(ch_rows), "latest": ch_latest, "mentions": mentions, "newest": newest(ch_rows, "text")},
            "dm": {"count": len(dm_rows), "latest": dm_latest, "newest": newest(dm_rows, "prompt")},
            "alerts": alerts}


def _like(q):
    """A LIKE pattern matching q anywhere, with the wildcard characters in q taken literally."""
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _snip(text, q, width=90):
    """A short piece of text around the first match, for the result list."""
    text = " ".join((text or "").split())
    i = text.lower().find(q.lower())
    if i < 0 or len(text) <= width:
        return text[:width]
    a = max(0, i - width // 3)
    return ("..." if a else "") + text[a:a + width] + ("..." if a + width < len(text) else "")


def search(bridge, q):
    """Find q (case-insensitive, anywhere) in nodes and your labels, direct messages, AI conversations and public-channel posts."""
    q = " ".join(q.split()) if isinstance(q, str) else ""
    if len(q) < MIN_QUERY:
        return {"q": q, "groups": [], "error": f"Type at least {MIN_QUERY} characters."}
    q = q[:80]                        # cap the query length; the LIKE patterns below scan whole columns
    # q is bound as a parameter (never pasted into the SQL); ESCAPE makes %, _ and \ in it match literally
    pat, esc =_like(q), " ESCAPE '\\'"
    db, lock = bridge.audit.db, bridge.audit.lock
    groups = []
    with lock:
        nodes = db.execute(
            "SELECT n.node_id AS id, n.name, n.short, n.role, n.last_seen, u.label, u.note, u.starred FROM mesh_nodes n LEFT JOIN node_notes u ON u.node_id = n.node_id "
            f"WHERE n.node_id LIKE ?{esc} OR n.name LIKE ?{esc} OR n.short LIKE ?{esc} OR u.label LIKE ?{esc} OR u.note LIKE ?{esc} "
            "ORDER BY u.starred DESC, n.last_seen DESC LIMIT ?", (pat, pat, pat, pat, pat, SEARCH_LIMIT)).fetchall()
        # labelled nodes the radio never reported to us still count
        extra = db.execute(
            "SELECT u.node_id AS id, NULL AS name, NULL AS short, NULL AS role, NULL AS last_seen, u.label, u.note, u.starred FROM node_notes u "
            f"WHERE u.node_id NOT IN (SELECT node_id FROM mesh_nodes) AND (u.node_id LIKE ?{esc} OR u.label LIKE ?{esc} OR u.note LIKE ?{esc}) LIMIT ?",
            (pat, pat, pat, SEARCH_LIMIT)).fetchall()
        msgs = {}
        for scope, kinds in (("dm", ("manual", "inbound")), ("ai", ("ai",))):
            marks = ",".join("?" * len(kinds))
            msgs[scope] = db.execute(
                f"SELECT id, ts, kind, node_id, node_name, prompt, response FROM requests WHERE kind IN ({marks}) AND (prompt LIKE ?{esc} OR response LIKE ?{esc} OR node_name LIKE ?{esc}) "
                "ORDER BY id DESC LIMIT ?", (*kinds, pat, pat, pat, SEARCH_LIMIT)).fetchall()
        chan = db.execute(f"SELECT id, ts, direction, node_id, node_name, text FROM channel_messages WHERE text LIKE ?{esc} OR node_name LIKE ?{esc} ORDER BY id DESC LIMIT ?",
                          (pat, pat, SEARCH_LIMIT)).fetchall()
    items = []
    for r in list(nodes) + list(extra):
        title = r["label"] or r["name"] or r["short"] or r["id"]
        sub = " · ".join(x for x in (r["name"] if r["label"] and r["name"] else None, r["id"], r["role"], ("★" if r["starred"] else None)) if x)
        items.append({"title": title, "sub": sub, "text": _snip(r["note"], q) if r["note"] and q.lower() in r["note"].lower() else "", "go": ["nodes", r["id"]], "ts": r["last_seen"]})
    groups.append({"key": "nodes", "label": "Nodes", "items": items[:SEARCH_LIMIT]})
    for scope, label in (("dm", "Direct messages"), ("ai", "AI conversations")):
        its = []
        for r in msgs[scope]:
            ql = q.lower()
            if r["kind"] == "manual":
                who, text = "you", r["response"]
            elif scope == "ai":
                in_prompt = ql in (r["prompt"] or "").lower() or ql in (r["node_name"] or "").lower()
                who, text = ("asked", r["prompt"]) if in_prompt else ("AI answered", r["response"])
            else:
                who, text = "them", r["prompt"]
            its.append({"title": r["node_name"] or r["node_id"], "sub": who, "text": _snip(text, q), "go": ["dm" if scope == "dm" else "chat", r["node_id"]], "ts": r["ts"]})
        groups.append({"key": scope, "label": label, "items": its})
    groups.append({"key": "channel", "label": "Public channel", "items": [
        {"title": r["node_name"] or r["node_id"] or "You", "sub": "you" if r["direction"] == "out" else "heard", "text": _snip(r["text"], q), "go": ["channel", None], "ts": r["ts"]} for r in chan]})
    return {"q": q, "groups": [g for g in groups if g["items"]], "total": sum(len(g["items"]) for g in groups)}
