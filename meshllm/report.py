"""A written summary of the mesh and the AI over the last few days, as Markdown, from data the bridge already holds.

It holds counts, averages and node names; it never includes the text of any message. Read-only.
"""
import time

from meshllm.mesh import PACKET_RETENTION_H, distance_km, fmt_dist_plain, fmt_temp_plain, summarize

DAY_CHOICES = (1, 7, 30)


def _n(v, digits=0):
    """Format a number with thousands separators (rounded to whole numbers unless digits is given); None becomes "unknown"."""
    return "unknown" if v is None else (f"{v:,.{digits}f}" if digits else f"{int(round(v)):,}")


def _table(rows, header):
    """A Markdown table from a list of row tuples and a header list."""
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _hour_label(h):
    """A one-hour span such as "23:00-00:00" for an hour of the day (0-23)."""
    return f"{h:02d}:00-{(h + 1) % 24:02d}:00"


def build(bridge, days=7):
    """Build the report for the last `days` (1, 7 or 30; anything else means 7).

    Returns {days, generated, markdown}. Only reads the database and the mesh view, and the text contains no message bodies.
    """
    days = days if days in DAY_CHOICES else 7              # fixed choices keep the queries below bounded
    b, m, a = bridge, bridge.mesh, bridge.audit
    m.flush()
    now = time.time()
    since = now - days * 86400
    lines = []
    w = lines.append
    w(f"# Mesh and AI report - last {days} day{'s' if days != 1 else ''}")
    w("")
    w(f"Made {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))} on this PC. Counts and averages only: no message text is included.")
    w("")

    # ---- the mesh now
    rows = m.nodes()
    s = summarize(rows)
    us = m.us(rows)
    w("## The mesh")
    w("")
    w(f"- Radio: {(us or {}).get('name') or 'not connected'}" + (f" ({(us or {}).get('hw')})" if us and us.get("hw") else ""))
    w(f"- Nodes on the radio's list: {s['nodes_total']}; heard in the last hour: {s['heard_1h']}; in the last 24 hours: {s['heard_24h']}")
    w(f"- Direct neighbours (24 h): {s['direct']}; furthest hop count seen: {_n(s['max_hops'])}")
    if s["roles"]:
        w("- Roles: " + ", ".join(f"{k} {v}" for k, v in s["roles"].items()))
    if s["avg_channel_util"] is not None:
        w(f"- Channel use reported by nodes (last hour): {s['avg_channel_util']:.1f}% on average")
    nodes_since = float(a.get_setting("mesh_nodes_since", "0") or 0)          # read before taking the lock: it takes it too
    with m.audit.lock:                    # one lock hold for all the mesh queries; the connection is shared with other threads
        db = m.audit.db
        new_nodes = db.execute("SELECT COUNT(*) FROM mesh_nodes WHERE first_seen > ?", (max(since, nodes_since),)).fetchone()[0]
        cut_h = int(now // 3600) - min(days * 24, PACKET_RETENTION_H)
        packets = db.execute("SELECT COALESCE(SUM(n),0) FROM mesh_packets WHERE hour > ?", (cut_h,)).fetchone()[0]
        by_type = db.execute("SELECT portnum, SUM(n) AS n FROM mesh_packets WHERE hour > ? GROUP BY portnum ORDER BY n DESC LIMIT 6", (cut_h,)).fetchall()
        talkers = db.execute("SELECT node_id, SUM(n) AS n FROM mesh_talkers WHERE hour > ? GROUP BY node_id ORDER BY n DESC LIMIT 5", (cut_h,)).fetchall()
        names = {r["node_id"]: r["name"] or r["short"] for r in db.execute("SELECT node_id, name, short FROM mesh_nodes")}
        tel = db.execute("SELECT COUNT(*) FROM telemetry WHERE ts > ? AND status='ok'", (since,)).fetchone()[0]
        traces = db.execute("SELECT COUNT(*), SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) FROM traceroutes WHERE ts > ?", (since,)).fetchone()
        ch_in, ch_out = (db.execute("SELECT COALESCE(SUM(direction='in'),0), COALESCE(SUM(direction='out'),0) FROM channel_messages WHERE ts > ?", (since,)).fetchone())
    w(f"- New nodes first heard: {new_nodes}")
    w("")
    w("## Traffic")
    w("")
    note = f" (packet counts are kept {PACKET_RETENTION_H // 24} days, so this covers at most that)" if days * 24 > PACKET_RETENTION_H else ""
    w(f"Packets heard: **{_n(packets)}**{note}.")
    w("")
    if by_type:
        w(_table([(t["portnum"].replace("_APP", "").replace("_", " ").title() if t["portnum"] != "ENCRYPTED" else "Encrypted (other channels)", _n(t["n"])) for t in by_type], ["Kind", "Packets"]))
        w("")
    if talkers:
        w("Busiest senders:")
        w("")
        w(_table([(names.get(t["node_id"]) or t["node_id"], _n(t["n"])) for t in talkers], ["Node", "Packets"]))
        w("")
    busy = m.busy_hours(min(days * 24, PACKET_RETENTION_H))
    if busy:
        w(f"Busiest time of day: {_hour_label(busy['best'])} (about {busy['best_avg']:.0f} packets an hour); quietest: {_hour_label(busy['quiet'])} (about {busy['quiet_avg']:.0f}).")
    hops = m.hop_series(hours=min(days * 24, 720))
    if hops["total"]:
        w(f"Hops: packets travelled {hops['mean']:.2f} relays on average; {hops['direct_pct']:.0f}% arrived directly.")
    w("")

    # ---- signal
    lm = m.link_map(hours=min(days * 24, 720))
    w("## Signal and range")
    w("")
    if lm["links"]:
        snrs = sorted(l["snr"] for l in lm["links"])
        w(f"- {len(lm['links'])} node{'s' if len(lm['links']) != 1 else ''} heard directly; median signal {snrs[len(snrs) // 2]:.1f} dB SNR (best {snrs[-1]:.1f}, weakest {snrs[0]:.1f}).")
        far = lm["farthest"]
        if far:
            w(f"- Furthest direct neighbour: {far['name'] or far['id']} at {fmt_dist_plain(m.dist_unit(), far['distance_km'])}.")
    else:
        w("- No node was heard directly in this period.")
    w("")

    # ---- environment
    sens = m.sensors(min(days * 24, 168))
    ov = sens["overall"]
    if ov["nodes"]:
        w("## Sensors")
        w("")
        bits = []
        if ov["temperature"] is not None:
            bits.append("temperature " + fmt_temp_plain(m.temp_unit(), ov["temperature"]))
        if ov["humidity"] is not None:
            bits.append(f"humidity {ov['humidity']:.0f}%")
        if ov["pressure"] is not None:
            bits.append(f"pressure {ov['pressure']:.0f} hPa")
        w(f"Averages across {ov['nodes']} node{'s' if ov['nodes'] != 1 else ''} with sensors ({_n(ov['readings'])} readings): " + ", ".join(bits) + ".")
        w("")
    w(f"Telemetry readings recorded: {_n(tel)}. Traceroutes run: {_n(traces[0])} ({_n(traces[1] or 0)} completed).")
    w("")

    # ---- the AI
    with a.lock:
        ai = a.db.execute("SELECT COUNT(*), COUNT(DISTINCT node_id), AVG(llm_ms) FROM requests WHERE kind='ai' AND ts > ?", (since,)).fetchone()
        st = a.db.execute("SELECT status, COUNT(*) AS n FROM requests WHERE kind='ai' AND ts > ? GROUP BY status ORDER BY n DESC", (since,)).fetchall()
        acts = a.db.execute("SELECT action, COUNT(*) AS n FROM requests WHERE kind='ai' AND ts > ? AND action IS NOT NULL AND action != '' GROUP BY action ORDER BY n DESC LIMIT 5", (since,)).fetchall()
        web_q = a.db.execute("SELECT COUNT(*) FROM requests WHERE kind='web' AND ts > ?", (since,)).fetchone()[0]
    w("## The AI")
    w("")
    w(f"- Model: {b.model}" + ("" if b.ollama_ok() else " (not available from Ollama right now)"))
    w(f"- Questions over the radio: {_n(ai[0])} from {_n(ai[1])} node{'s' if ai[1] != 1 else ''}; questions typed in the browser: {_n(web_q)}")
    if ai[2]:
        w(f"- Average answer time: {ai[2] / 1000:.1f} s")
    if st:
        w("")
        w(_table([(r["status"].replace("_", " "), _n(r["n"])) for r in st], ["Outcome", "Count"]))
    if acts:
        w("")
        w("Tools the AI used: " + ", ".join(f"{r['action']} ({r['n']})" for r in acts) + ".")
    w("")
    w("## Public channel")
    w("")
    w(f"Posts heard: {_n(ch_in)}. Posts you made: {_n(ch_out)}. The AI takes no part in the channel.")
    w("")

    # ---- things to look at
    alerts = [x["text"] for x in m.alerts(rows)]
    w("## Worth a look")
    w("")
    if alerts:
        for t in alerts:
            w(f"- {t}")
    else:
        w("- Nothing flagged right now.")
    w("")
    return {"days": days, "generated": now, "markdown": "\n".join(lines)}
