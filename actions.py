"""The fixed menu of things the AI may look up about the radio mesh.

The model never sees a shell. It can only *ask* for an action by name, with parameters that are
validated here against a declared schema; the code then runs a hand-written handler. Adding a
capability means adding an entry to ACTIONS - nothing the model says can create one.

Tiers:  0 = read-only, runs immediately for an authorised node
        1 = changes something, additionally needs a one-time code confirmed over the radio
"""
import json
import time
from dataclasses import dataclass, field
from typing import Callable


MAX_RESULT_CHARS = 600
MAX_FREE_TEXT = 40          # longest free-text parameter (a node name); it is only ever looked up, never executed
TIER_NAMES = {0: "read-only", 1: "read-only + confirmed actions"}


class ActionError(Exception):
    """A request the code refuses; the message is safe to show the requesting user."""


@dataclass
class Action:
    name: str
    description: str
    tier: int
    handler: Callable
    params: dict = field(default_factory=dict)  # name -> {"description": str, "enum": callable | list | "free": True}


# ---- mesh questions: answered from what the radio has heard, read from our own database. They never transmit. ----
def _label(r):
    return r.get("name") or r.get("short") or r.get("id") or "?"


def _us_position(bridge):
    us = bridge.mesh.us()
    return (us["lat"], us["lon"]) if us and us.get("lat") is not None else None


def mesh_summary(bridge):
    import mesh
    m = bridge.mesh
    live = m.nodes()
    if not live:
        return f"The radio isn't connected right now. I remember {len(m.stored_nodes())} nodes from earlier."
    s = mesh.summarize(live)
    out = [f"Mesh: {s['nodes_total']} nodes known. Heard {s['heard_15m']} in 15 min, {s['heard_1h']} in 1 h, "
           f"{s['heard_24h']} in 24 h; {s['direct']} direct neighbours"
           + (f", farthest {s['max_hops']} hops" if s["max_hops"] is not None else "") + "."]
    if s["avg_channel_util"] is not None:
        out.append(f"Channel use averages {s['avg_channel_util']:.1f}%.")
    t = m.traffic(hours=1)
    if t["total"]:
        top = t["talkers"][0]
        out.append(f"{t['total']} packets heard this hour; busiest sender {clean_name(top['name'] or top['node_id'])} ({top['n']}).")
    low = [r for r in live if not r["us"] and isinstance(r["battery"], (int, float)) and r["battery"] <= 20
           and r["age_s"] is not None and r["age_s"] <= 86400]
    if low:
        out.append("Low battery: " + ", ".join(f"{clean_name(_label(r))} {r['battery']:.0f}%" for r in low[:3]) + ".")
    sen = m.sensors(1)["overall"]
    if sen["nodes"]:
        bits = [mesh.fmt_temp_plain(m.temp_unit(), sen["temperature"]) if sen["temperature"] is not None else None,
                f"{sen['humidity']:.0f}% humidity" if sen["humidity"] is not None else None]
        out.append(f"Sensors (1 h average of {sen['nodes']} node{'s' if sen['nodes'] != 1 else ''}): " + ", ".join(x for x in bits if x) + ".")
    out.append(f"{s['with_position']} have a position.")
    return " ".join(out)


def clean_name(s):
    import mesh
    return mesh.clean(s, 24) or "?"


SORTS = ["recent", "low_battery", "nearest", "farthest"]
ROLES = ["router", "repeater", "client", "tracker", "sensor"]
TOPICS = ["sensors", "signal", "quiet", "busiest", "activity"]
HOURS = ["1", "6", "24"]


def list_nodes(bridge, sort=None, role=None):
    import mesh
    m = bridge.mesh
    sort = sort or ("nearest" if role else "recent")
    rows = [r for r in m.all_nodes() if not r["us"]]
    if role:                                    # 'router' also matches ROUTER_LATE, 'client' also CLIENT_MUTE, ...
        rows = [r for r in rows if role in (r.get("role") or "").lower()]
        if not rows:
            return f"No {role}s known."
    if not rows:
        return "I don't have any nodes in my records yet."
    here = _us_position(bridge)
    if sort == "low_battery":
        rows = sorted((r for r in rows if isinstance(r["battery"], (int, float))), key=lambda r: r["battery"])
        detail = lambda r: f"{r['battery']:.0f}% ({mesh.ago(r['age_s'])})"
        head = "Lowest battery"
    elif sort == "nearest" and here:
        rows = sorted((r for r in rows if r["lat"] is not None), key=lambda r: mesh.distance_km(*here, r["lat"], r["lon"]))
        detail = lambda r: mesh.fmt_dist_plain(m.dist_unit(), mesh.distance_km(*here, r['lat'], r['lon']))
        head = "Nearest"
    elif sort == "nearest":
        rows = sorted((r for r in rows if isinstance(r["hops"], int)), key=lambda r: (r["hops"], r["age_s"] or 0))
        detail = lambda r: f"{r['hops']} hop{'s' if r['hops'] != 1 else ''}"
        head = "Fewest hops (our position is unknown)"
    elif sort == "farthest":
        rows = sorted((r for r in rows if isinstance(r["hops"], int)), key=lambda r: (-r["hops"], r["age_s"] or 0))
        detail = lambda r: f"{r['hops']} hops ({mesh.ago(r['age_s'])})"
        head = "Most hops away"
    else:
        rows = [r for r in rows if r["age_s"] is not None]
        detail = lambda r: mesh.ago(r["age_s"])
        head = "Heard most recently"
    if not rows:
        return "No nodes have that information yet."
    return head + ": " + "; ".join(f"{clean_name(_label(r))} {detail(r)}" for r in rows[:5]) + "."


def _ago_h(secs):
    import mesh
    return mesh.ago(secs)


def mesh_report(bridge, topic=None, hours=None):
    import mesh
    m, unit = bridge.mesh, bridge.mesh.temp_unit()
    h = int(hours) if hours else None
    if topic == "sensors":
        h = h or 1
        s = m.sensors(h)
        if not s["nodes"]:
            last = s["latest_any"]
            return f"No sensor readings in the last {h} h." + (f" The latest came from {clean_name(last['name'])}, {mesh.ago(time.time() - last['ts'])}." if last else " No node is reporting temperature or humidity.")
        o = s["overall"]
        bits = [mesh.fmt_temp_plain(unit, o["temperature"]) if o["temperature"] is not None else None, f"{o['humidity']:.0f}% humidity" if o["humidity"] is not None else None,
                f"{o['pressure']:.0f} hPa" if o["pressure"] is not None else None]
        per = "; ".join(f"{clean_name(n['name'] or n['id'])} " + ", ".join(x for x in (mesh.fmt_temp_plain(unit, n["temperature"]) if n["temperature"] is not None else None,
                                                                            f"{n['humidity']:.0f}%" if n["humidity"] is not None else None) if x) for n in s["nodes"][:3])
        return f"Sensors, last {h} h: {o['nodes']} node{'s report' if o['nodes'] != 1 else ' reports'}. Average " + ", ".join(x for x in bits if x) + f". {per}."
    if topic == "signal":
        d = m.link_map(h or 24)
        if not d["links"]:
            return f"No node has been heard directly in the last {d['hours']} h."
        L = lambda l: f"{clean_name(l['name'] or l['id'])} {l['snr']:+.1f} dB" + (f" ({mesh.fmt_dist_plain(m.dist_unit(), l['distance_km'])})" if l["distance_km"] is not None else "")
        out = f"Direct links, last {d['hours']} h: {len(d['links'])} node{'s' if len(d['links']) != 1 else ''}. Best: " + "; ".join(L(l) for l in d["links"][:2])
        rest = d["links"][2:]                          # never name a link twice
        if rest:
            out += ". Weakest: " + "; ".join(L(l) for l in rest[::-1][:2])
        return out + "."
    if topic == "quiet":
        h = h or 6
        rows = sorted((r for r in m.all_nodes() if not r["us"] and r["age_s"] is not None and h * 3600 <= r["age_s"] <= 7 * 86400), key=lambda r: r["age_s"])
        if not rows:
            return f"No node that was heard in the last week has been quiet for {h}+ h."
        return f"Quiet for {h}+ h (heard within a week): " + "; ".join(f"{clean_name(_label(r))} {mesh.ago(r['age_s']).replace(' ago', '')}" for r in rows[:5]) + (f" (+{len(rows) - 5} more)." if len(rows) > 5 else ".")
    if topic == "busiest":
        b = m.busy_hours()
        if not b:
            return "Not enough traffic has been counted yet to say."
        hl = lambda x: f"{x % 12 or 12} {'AM' if x < 12 else 'PM'}"
        return f"Busiest hour {hl(b['best'])} (about {b['best_avg']:.0f} packets an hour), quietest {hl(b['quiet'])} ({b['quiet_avg']:.0f}). Based on {b['hours_of_data']} hour{'s' if b['hours_of_data'] != 1 else ''} of data."
    if topic == "activity":
        a = m.activity(h or 1)
        out = f"Last {a['hours']} h: {a['packets']} packets from {a['senders']} nodes"
        if a["busiest"]:
            out += f" (busiest {clean_name(a['busiest'][0][0])}, {a['busiest'][0][1]})"
        out += f"; {a['readings']} telemetry readings; {a['asked']} AI question{'s' if a['asked'] != 1 else ''}"
        if a["traces"]:
            out += f"; {a['traces']} traceroute{'s' if a['traces'] != 1 else ''}"
        if a["new_nodes"]:
            out += "; new nodes: " + ", ".join(clean_name(n) for n in a["new_nodes"][:3]) + (f" (+{len(a['new_nodes']) - 3})" if len(a["new_nodes"]) > 3 else "")
        return out + "."
    return "Which report? sensors, signal, quiet, busiest or activity."


HISTORY_METRICS = {"battery": ("battery_level", "%", 0), "voltage": ("voltage", " V", 2), "temperature": ("temperature", None, 1),
                   "humidity": ("relative_humidity", "%", 0), "channel_use": ("channel_utilization", "%", 1)}


def node_history(bridge, node=None, metric=None):
    import mesh
    m, metric = bridge.mesh, metric or "battery"
    if not node:
        return "Which node? Give its name or ID."
    r, options = m.find_node(node)
    if r is None:
        return ("More than one match: " + ", ".join(clean_name(_label(o)) for o in options) + ". Be more specific.") if options else f"I have no node matching '{clean_name(node)}'."
    col, suffix, digits = HISTORY_METRICS[metric]
    pts = m.node_history(r["id"], col, 24)
    name = clean_name(_label(r))
    if not pts:
        return f"No {metric.replace('_', ' ')} readings for {name} in the last 24 h."
    fmt = (lambda v: mesh.fmt_temp_plain(m.temp_unit(), v)) if metric == "temperature" else (lambda v: f"{v:.{digits}f}{suffix}")
    first, last = pts[0], pts[-1]
    span_h = (last[0] - first[0]) / 3600
    if len(pts) < 2 or span_h < 0.25:
        return f"{name} {metric.replace('_', ' ')}: {fmt(last[1])} ({mesh.ago(time.time() - last[0])}); only {len(pts)} reading in 24 h, so no trend yet."
    delta = last[1] - first[1]
    rate = delta / span_h
    out = f"{name} {metric.replace('_', ' ')}: {fmt(first[1])} to {fmt(last[1])} over {span_h:.0f} h ({len(pts)} readings, low {fmt(min(v for _, v in pts))}, high {fmt(max(v for _, v in pts))})"
    scale = 1.8 if metric == "temperature" and m.temp_unit() == "F" else 1
    if abs(delta) < (0.5 if metric in ("battery", "humidity", "channel_use") else 0.05):
        out += "; steady"
    else:
        out += f"; {'up' if delta > 0 else 'down'} about {abs(rate * scale):.1f}{'' if metric == 'temperature' else (suffix or '')}/h" if metric != "temperature" else f"; {'up' if delta > 0 else 'down'} about {abs(rate * scale):.1f} degrees/h"
        if metric == "battery" and rate < -0.3 and len(pts) >= 3 and span_h >= 1 and last[1] <= 100:
            out += f", roughly {last[1] / -rate:.0f} h left at that rate"
    return out + "."


def node_info(bridge, node=None):
    import mesh
    m = bridge.mesh
    if not node:
        return "Which node? Give its name or ID."
    r, options = m.find_node(node)
    if r is None:
        if options:
            return "More than one match: " + ", ".join(clean_name(_label(o)) for o in options) + ". Be more specific."
        return f"I have no node matching '{clean_name(node)}'."
    bits = [f"{clean_name(_label(r))} ({r['id']})" + (f", {r['hw']}" if r.get("hw") else "")]
    bits.append(("heard " + mesh.ago(r["age_s"]) if r["age_s"] is not None else "last heard time unknown")
                + (" (not in the radio's list now)" if r.get("stored") else ""))
    if isinstance(r["hops"], int):
        bits.append(f"{r['hops']} hop{'s' if r['hops'] != 1 else ''} away")
    if isinstance(r["snr"], (int, float)):
        bits.append(f"SNR {r['snr']:.1f} dB")
    here = _us_position(bridge)
    if here and r["lat"] is not None:
        bits.append(f"{mesh.fmt_dist_plain(m.dist_unit(), mesh.distance_km(*here, r['lat'], r['lon']))} from us")
    elif r["lat"] is not None:
        bits.append("has a known position")
    live = {k: r[k] for k in ("battery", "voltage", "channel_util", "temperature", "humidity", "pressure") if isinstance(r[k], (int, float))}
    last = [x for x in bridge.telemetry.store.latest() if x["node_id"] == r["id"]]
    for x in last:                                     # fill gaps from what we recorded ourselves
        for col, key in (("battery_level", "battery"), ("voltage", "voltage"), ("channel_utilization", "channel_util"),
                         ("temperature", "temperature"), ("relative_humidity", "humidity"), ("barometric_pressure", "pressure")):
            if key not in live and isinstance(x.get(col), (int, float)):
                live[key] = x[col]
    if "battery" in live:     # the radios report 101% when they are on external power and no battery is being measured
        bits.append("externally powered" if live["battery"] > 100 else f"battery {live['battery']:.0f}%" + (f" ({live['voltage']:.2f} V)" if live.get("voltage") else ""))
    elif live.get("voltage"):
        bits.append(f"{live['voltage']:.2f} V")
    if "channel_util" in live:
        bits.append(f"channel use {live['channel_util']:.1f}%")
    if "temperature" in live:
        bits.append(mesh.fmt_temp_plain(m.temp_unit(), live["temperature"]) + (f", {live['humidity']:.0f}% humidity" if "humidity" in live else ""))
    if "pressure" in live:
        bits.append(f"{live['pressure']:.0f} hPa")
    if not live:
        bits.append("no telemetry recorded for it yet")
    return "; ".join(bits) + "."


def demo_confirm(bridge):
    return "Demo action confirmed. Nothing was changed."


ACTIONS = {a.name: a for a in [
    Action("mesh_summary", "How the radio mesh is doing right now, from stored data: how many nodes are around or were "
                           "heard recently, direct neighbours, channel use, packet traffic, low batteries, sensor averages. "
                           "Use it for 'how's the mesh', 'how many nodes', or any overall mesh status question.", 0, mesh_summary),
    Action("list_nodes", "List nodes from the mesh database ranked by one criterion: most recently heard, lowest "
                         "battery, nearest, or farthest away in hops. Can be limited to routers, repeaters, clients, trackers or "
                         "sensors (e.g. 'where is the nearest router').", 0, list_nodes,
           {"sort": {"description": "How to rank the nodes (optional; default recent)", "enum": SORTS},
            "role": {"description": "Only nodes with this role (optional)", "enum": ROLES}}),
    Action("mesh_report", "A report from the mesh database on one topic. sensors: the temperature, humidity and pressure that "
                          "nodes report (use for 'what's the temperature outside', 'is it humid'). signal: how well we hear the "
                          "nodes next to us (best and weakest links). quiet: nodes that have gone silent. busiest: the busiest and "
                          "quietest times of day. activity: what happened recently.", 0, mesh_report,
           {"topic": {"description": "Which report", "enum": TOPICS},
            "hours": {"description": "How many hours back (optional)", "enum": HOURS}}),
    Action("node_history", "How one node's battery, voltage, temperature, humidity or channel use has changed over the last 24 hours "
                           "(use for 'is X's battery draining').", 0, node_history,
           {"node": {"description": "The node's name, short name or !id", "free": True},
            "metric": {"description": "What to look at (default battery)", "enum": list(HISTORY_METRICS)}}),
    Action("node_info", "Look up ONE specific mesh node, given by its name or its !id such as !a1b2c3d4: last heard, hops, "
                        "signal, battery, temperature, distance. Use it for 'tell me about node X'.", 0, node_info,
           {"node": {"description": "The node's name, short name or !id", "free": True}}),
    Action("demo_confirm", "DEMO ONLY: does nothing except prove the confirmation flow works. "
                           "Use only if the user explicitly asks to run the confirmation demo.", 1, demo_confirm),
]}


def allowed(max_tier):
    return [a for a in ACTIONS.values() if a.tier <= max_tier]


def _choices(spec):
    e = spec.get("enum")
    return list(e() if callable(e) else e or [])


def tool_specs(max_tier):
    """Tool definitions in the shape Ollama expects, limited to what this node may use."""
    out = []
    for a in allowed(max_tier):
        props = {}
        for pname, spec in a.params.items():
            props[pname] = {"type": "string", "description": spec["description"]}
            if not spec.get("free"):
                props[pname]["enum"] = _choices(spec)
        out.append({"type": "function", "function": {
            "name": a.name, "description": a.description,
            "parameters": {"type": "object", "properties": props, "required": []}}})
    return out


def validate(name, args, max_tier):
    """Turn a model's tool call into (Action, clean params) or raise ActionError.

    Checked here, never trusted from the model: the action must exist, be within the node's tier,
    and every parameter must be declared and inside its allowed values. Undeclared parameters
    are dropped rather than passed on.
    """
    action = ACTIONS.get(name) if isinstance(name, str) else None
    if action is None:
        raise ActionError("that isn't something I can do")
    if action.tier > max_tier:
        raise ActionError("you aren't authorised for that")
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except ValueError:
            raise ActionError("I couldn't understand the request details")
    if not isinstance(args, dict):
        args = {}
    clean = {}
    for pname, spec in action.params.items():
        if args.get(pname) in (None, ""):
            continue
        value = str(args[pname]).strip()
        if spec.get("free"):
            value = "".join(ch for ch in value if ch.isprintable())[:MAX_FREE_TEXT].strip()
            if value:
                clean[pname] = value
            continue
        match = next((c for c in _choices(spec) if str(c).lower() == value.lower()), None)
        if match is None:
            raise ActionError(f"'{value}' isn't a valid {pname}")
        clean[pname] = match
    return action, clean


def run(bridge, action, params):
    """Run a validated action; returns result text capped to a sane length."""
    result = str(action.handler(bridge, **params))
    return result if len(result) <= MAX_RESULT_CHARS else result[:MAX_RESULT_CHARS - 1] + "…"
