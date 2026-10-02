// traceroute: the Traceroute page. POST /api/traceroute/request starts a trace, GET /api/traceroute/request?id= is polled for the
// result, and /api/traceroutes lists saved ones. Also holds map plumbing shared by every map (fitView, mapBase, addTiles, addScale,
// merc) and the lsGet/lsSet localStorage helpers. Uses resolveNode (telemetry.js) and applyView, enablePanZoom (mesh-common.js).
// ---- shared map helpers (Web Mercator; used by the traceroute and Home maps) ------------------------
// Computes a map view that fits all the world-coordinate points with padding. Returns { S, x0, y0, cy, W, H, P }, where P maps a world point to SVG pixels.
function fitView(xys, W, H, pad = 1.5, minSpan = 1 / 2 ** 17) {     // xys: world coordinates (0..1); never closer than ~300 m across
  const xs = xys.map(p => p[0]), ys = xys.map(p => p[1]);
  const spanX = Math.max((Math.max(...xs) - Math.min(...xs)) * pad, minSpan), spanY = Math.max((Math.max(...ys) - Math.min(...ys)) * pad, minSpan);
  const S = Math.min(W / spanX, H / spanY);                          // pixels per world unit
  const cx = (Math.max(...xs) + Math.min(...xs)) / 2, cy = (Math.max(...ys) + Math.min(...ys)) / 2, x0 = cx - W / (2 * S), y0 = cy - H / (2 * S);
  return { S, x0, y0, cy, W, H, P: ([x, y]) => [(x - x0) * S, (y - y0) * S] };
}
// Every map keeps one background rectangle and one tile layer for good. A redraw replaces what is drawn on top and only *moves* the
// tiles, so dragging doesn't make the background flash; tiles of the previous zoom stay underneath until the new ones have arrived.
function mapBase(svg, W, H) {
  if (!svg._base) {
    const ns = "http://www.w3.org/2000/svg", rect = document.createElementNS(ns, "rect"), g = document.createElementNS(ns, "g");
    rect.setAttribute("class", "mapbg"); g.setAttribute("class", "tilelayer");
    svg.replaceChildren(rect, g); svg._base = { rect, g, tiles: new Map(), want: new Set(), needed: [] };
  }
  const b = svg._base; b.rect.setAttribute("width", W); b.rect.setAttribute("height", H);
  for (const c of [...svg.children]) if (c !== b.rect && c !== b.g) c.remove();
  return b;
}
// Removes all tile images from a map.
function dropTiles(svg) { const b = svg._base; if (b && b.tiles.size) { b.g.replaceChildren(); b.tiles.clear(); } }
function pruneTiles(svg) {       // forget tiles we no longer need, once everything on screen has loaded (or failed)
  const b = svg._base; if (!b) return;
  const done = b.needed.every(k => { const t = b.tiles.get(k); return t && (t.loaded || t.failed); });
  if (!done && b.tiles.size < 300) return;
  for (const [k, t] of b.tiles) if (!b.want.has(k)) { t.el.remove(); b.tiles.delete(k); }
}
function addTiles(svg, v) {      // OpenStreetMap tiles under the drawing (only when the user opted in); served by this bridge from its disk cache
  const b = svg._base, ns = "http://www.w3.org/2000/svg", { S, x0, y0, W, H } = v;
  const Z = Math.max(0, Math.min(18, Math.floor(Math.log2(S / 160)))), n = 2 ** Z;      // tiles end up 160-320 px wide
  const vx0 = Math.floor(x0 * n), vx1 = Math.floor((x0 + W / S) * n), vy0 = Math.max(0, Math.floor(y0 * n)), vy1 = Math.min(n - 1, Math.floor((y0 + H / S) * n));
  b.want = new Set(); b.needed = [];
  // Only load tiles when 36 or fewer are in view, so a very zoomed-out map does not request a flood of tiles.
  if ((vx1 - vx0 + 1) * (vy1 - vy0 + 1) <= 36) {
    for (let ty = Math.max(0, vy0 - 1); ty <= Math.min(n - 1, vy1 + 1); ty++) for (let tx = vx0 - 1; tx <= vx1 + 1; tx++) {     // one extra ring: dragging reveals tiles that are already loaded
      const key = `${Z}/${tx}/${ty}`; b.want.add(key);
      if (tx >= vx0 && tx <= vx1 && ty >= vy0 && ty <= vy1) b.needed.push(key);
      let t = b.tiles.get(key);
      if (t && t.failed && Date.now() - t.failedAt > 30000) { t.el.remove(); b.tiles.delete(key); t = null; }          // try a failed tile again after a while
      if (!t) {
        const el = document.createElementNS(ns, "image"); t = { el, tx, ty, n, loaded: false, failed: false, failedAt: 0 };
        el.setAttribute("href", `${TILE_URL}/${Z}/${((tx % n) + n) % n}/${ty}.png`); el.setAttribute("draggable", "false");
        el.addEventListener("load", () => { t.loaded = true; pruneTiles(svg); });
        el.addEventListener("error", () => { t.failed = true; t.failedAt = Date.now(); pruneTiles(svg); });
        b.g.append(el); b.tiles.set(key, t);
      }
    }
  }
  for (const t of b.tiles.values()) {     // move everything (including the previous zoom's tiles) to where it belongs now
    t.el.setAttribute("x", ((t.tx / t.n) - x0) * S); t.el.setAttribute("y", ((t.ty / t.n) - y0) * S);
    t.el.setAttribute("width", S / t.n + 0.5); t.el.setAttribute("height", S / t.n + 0.5);
  }
  pruneTiles(svg);
  svgMaker(svg)("text", { x: W - 6, y: H - 6, "text-anchor": "end" }, "© OpenStreetMap contributors");
}
// Draws a scale bar about 110 px long, rounded to a round distance in the chosen unit.
function addScale(mk, v) {
  const lat = Math.atan(Math.sinh(Math.PI * (1 - 2 * v.cy))) * 180 / Math.PI;
  // Metres per pixel: the Earth's equatorial circumference (40,075,016.686 m) times cos(latitude), divided by the pixels across the whole world (v.S).
  const mpp = 40075016.686 * Math.cos(lat * Math.PI / 180) / v.S;
  let m, label;
  if (distUnit === "mi") {     // about 110 px: a round number of miles when it's long, feet when it's short
    const ft = mpp * 110 / 0.3048;
    if (ft >= 2640) { const mi = niceDist(ft / 5280); m = mi * 1609.344; label = mi + " mi"; } else { const f = niceDist(ft); m = f * 0.3048; label = f + " ft"; }
  } else { m = niceDist(mpp * 110); label = fmtDist(m); }
  const len = m / mpp;
  mk("path", { class: "scale", d: `M14 ${v.H - 16} h${len} M14 ${v.H - 21} v10 M${14 + len} ${v.H - 21} v10` });
  mk("text", { x: 14, y: v.H - 26 }, label);
}

// ---- traceroute tab -----------------------------------------------------------------------
const TILE_URL = "/tiles";      // the bridge serves (and caches on disk) the OpenStreetMap tiles
// traceCur: the traceroute being shown. traceHistSig: signature of the history list, to avoid redrawing it when unchanged.
let traceReady = false, traceCur = null, traceHistSig = "";
// Display name for a hop in a route.
const hopName = h => h.name || h.id || "Unknown node";
// Latitude and longitude to Web Mercator world coordinates in 0..1 (x runs east, y runs south), the same system map tiles use.
const merc = (lat, lon) => { const s = Math.sin(lat * Math.PI / 180); return [(lon + 180) / 360, 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)]; };  // Web Mercator, 0..1 world
// Rounds a distance down to 1, 2 or 5 times a power of ten, for scale bars.
const niceDist = m => { const p = 10 ** Math.floor(Math.log10(m)), n = m / p; return (n >= 5 ? 5 : n >= 2 ? 2 : 1) * p; };
// Metres as 'N m' or 'N km'.
const fmtDist = m => m >= 1000 ? (m / 1000) + " km" : m + " m";
// localStorage read and write that tolerate storage being unavailable (private windows, blocked site data).
const lsGet = k => { try { return localStorage.getItem(k); } catch { return null; } };
const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch {} };

// One-time setup: restores the map tile toggle and wires the trace form.
function initTrace() {
  if (traceReady) return; traceReady = true;
  $("traceTiles").checked = lsGet("traceTiles") === "1";
  $("traceTiles").addEventListener("change", e => { lsSet("traceTiles", e.target.checked ? "1" : "0"); if (traceCur) drawTraceMap(traceCur); });
  $("traceForm").addEventListener("submit", onTrace);
}

// Lists the hops of the 'there' and 'back' paths with signal badges, or the reason a trace failed or timed out.
function renderTracePaths(tr) {
  const box = $("tracePaths"); box.replaceChildren();
  $("traceTitle").textContent = `${fmtTime(tr.ts)} · ${tr.node_name || tr.node_id}`;
  if (tr.status !== "ok") { box.append(el("div", "warnbox", `${tr.status === "timeout" ? "No answer" : "Failed"}: ${tr.detail || ""}`)); return; }
  for (const [title, path, relays] of [["There (us → target)", tr.path_towards, tr.relays_towards], ["Back (target → us)", tr.path_back, tr.relays_back]]) {
    const sec = el("div", "pathsec");
    sec.append(el("h4", null, title + (relays === 0 ? " — direct, no relays" : relays != null ? ` — via ${relays} relay${relays === 1 ? "" : "s"}` : "")));
    if (!path) sec.append(el("div", "hint", "The reply didn't include the way back."));
    else {
      const ol = el("ol", "hops");
      path.forEach((h, i) => {
        const li = el("li"); li.append(el("b", null, hopName(h)));
        if (h.id) li.append(el("small", null, h.id));
        if (h.snr != null) li.append(el("span", "badge " + (h.snr >= 0 ? "ok" : h.snr > -10 ? "warn" : "bad"), `${h.snr} dB`));
        if (i > 0 && h.snr == null) li.append(el("span", "badge", "signal unknown"));
        li.append(el("span", "badge" + (h.lat == null ? "" : " info"), h.lat == null ? "no position" : "on map"));
        ol.append(li);
      });
      sec.append(ol);
    }
    box.append(sec);
  }
}

// Draws the route: one marker per distinct node with a known position (us, target, relays) and a line per direction, nudged apart so they do not overlap.
function drawTraceMap(tr) {
  const svg = $("traceMap");
  const ns = "http://www.w3.org/2000/svg", W = 680, H = 400, tiles = $("traceTiles").checked; mapBase(svg, W, H);
  const mk = (tag, attrs, text) => { const e = document.createElementNS(ns, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); if (text != null) e.textContent = text; svg.append(e); return e; };
  const towards = tr.path_towards || [], back = tr.path_back || [], note = $("mapNote");
  // one marker per distinct node; roles come from the "there" path (first = us, last = target)
  const nodes = new Map();
  const add = (h, role) => { const key = h.id || h.num; if (h.lat == null || key == null) return; if (!nodes.has(key)) nodes.set(key, { h, role, xy: merc(h.lat, h.lon) }); };
  towards.forEach((h, i) => add(h, i === 0 ? "us" : i === towards.length - 1 ? "tg" : "rl")); back.forEach(h => add(h, "rl"));
  if (!nodes.size) {
    dropTiles(svg); svg._view = null;
    mk("text", { class: "muted", x: 24, y: 40 }, tr.status === "ok" ? "No node on this route has a known position, so there is nothing to plot." : "No route to draw.");
    note.textContent = tr.status === "ok" ? "Nodes only appear on the map if they share their position (and your radio has heard it). The hop list above always shows the full route." : "";
    return;
  }
  const view = applyView(svg, fitView([...nodes.values()].map(p => p.xy), W, H), W, H), P = view.P;

  if (tiles) addTiles(svg, view); else dropTiles(svg);

  const drawPath = (entries, cls, sign) => {
    let last = null;
    entries.forEach((h, i) => {
      if (h.lat == null) return;
      const p = P(merc(h.lat, h.lon));
      if (last) {
        const dx = p[0] - last.p[0], dy = p[1] - last.p[1], len = Math.hypot(dx, dy) || 1, ox = -dy / len * 2.5 * sign, oy = dx / len * 2.5 * sign;   // nudge so there/back don't overlap
        const a = { x1: last.p[0] + ox, y1: last.p[1] + oy, x2: p[0] + ox, y2: p[1] + oy };
        mk("line", { class: "halo", ...a });
        mk("line", { class: `rt ${cls}${i - last.i > 1 ? " gap" : ""}`, ...a });
      }
      last = { p, i };
    });
  };
  drawPath(towards, "t", 1); drawPath(back, "b", -1);
  for (const { h, role, xy } of nodes.values()) {
    const [px, py] = P(xy);
    mk("circle", { class: `pt ${role}`, cx: px, cy: py, r: role === "rl" ? 6 : 8 });
    const label = hopName(h).slice(0, 22), right = px < W - 140;
    mk("text", { x: right ? px + 12 : px - 12, y: py + 4, "text-anchor": right ? "start" : "end" }, label);
  }
  addScale(mk, view); addMapControls(svg, W); enablePanZoom(svg, () => drawTraceMap(tr));
  const unplaced = [...towards, ...back].filter(h => h.lat == null).length;
  note.textContent = unplaced ? "Dotted lines cross nodes whose position isn't known; those nodes appear in the hop list but not on the map." : "";
}

// Shows a traceroute (a saved one or a fresh result) and resets any pan or zoom on its map.
function showTrace(tr) {
  traceCur = tr; mapViews.delete("traceMap"); renderTracePaths(tr); drawTraceMap(tr);
  for (const r of document.querySelectorAll("#traceHistory .hrow")) r.classList.toggle("sel", r.dataset.id === String(tr.id));
}

// Loads the 30 most recent traceroutes into the history list (redrawn only when changed, or force) and opens one if none is showing.
async function refreshTrace(force) {
  let list; try { list = await api("/api/traceroutes?limit=30"); } catch { return; }
  const sig = JSON.stringify(list.map(r => [r.id, r.status]));
  if (sig !== traceHistSig || force) {
    traceHistSig = sig; const box = $("traceHistory"); box.replaceChildren();
    if (!list.length) box.append(el("div", "empty", "No traceroutes yet. Trace a node above."));
    for (const r of list) {
      const row = el("div", "hrow" + (traceCur && traceCur.id === r.id ? " sel" : "")); row.dataset.id = r.id;
      const res = r.status === "ok" ? `${r.relays_towards} relay${r.relays_towards === 1 ? "" : "s"} there${r.relays_back != null ? ", " + r.relays_back + " back" : ""}` : r.detail || r.status;
      const b = el("div"); b.append(el("span", "badge " + (r.status === "ok" ? "ok" : r.status === "timeout" ? "warn" : "bad"), r.status === "ok" ? "OK" : r.status === "timeout" ? "No answer" : "Error"));
      row.append(el("div", "time", fmtTime(r.ts)), el("div", null, r.node_name || r.node_id), b, el("small", null, res));
      row.addEventListener("click", () => showTrace(r)); box.append(row);
    }
    if (!traceCur && list.length) showTrace(list.find(r => r.status === "ok") || list[0]);
    else if (!traceCur) drawTraceMap({ status: "none" });
  }
}

// Sends a traceroute request to the node typed in the form, then waits for the result.
async function onTrace(e) {
  e.preventDefault();
  const out = $("traceStatus"), id = resolveNode($("traceNode").value);
  if (!id) { out.textContent = "Unknown node. Use a name your radio has heard, or an ID like !1a2b3c4d."; return; }
  const btn = e.submitter || e.target.querySelector("button"); btn.disabled = true;
  out.textContent = "Sending the traceroute…";
  const { ok, data } = await post("/api/traceroute/request", { node: id, hop_limit: parseInt($("traceHops").value, 10) });
  if (!ok) { out.textContent = data.error || "Could not start the traceroute."; btn.disabled = false; return; }
  out.textContent = "Waiting for the route to come back (up to a minute)…";
  // Poll once a second, at most 100 times; a 'waiting' status means keep going.
  for (let i = 0; i < 100; i++) {
    await new Promise(r => setTimeout(r, 1000));
    let st; try { st = await api("/api/traceroute/request?id=" + data.id); } catch { continue; }
    if (st.status === "waiting") continue;
    out.textContent = st.status === "ok" ? "Done." : `${st.status === "timeout" ? "No answer" : "Failed"}: ${st.detail || ""}`;
    if (st.trace) showTrace(st.trace);
    break;
  }
  btn.disabled = false; refreshTrace(true);
}
