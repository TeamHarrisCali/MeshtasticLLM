// mesh-common: helpers shared by the Home, Nodes, Map and other mesh pages: the shared node list (fetchNodes, /api/mesh/nodes?all=1),
// formatters (agoStr, fmtKm, hop labels), small widgets (tilesInto, spark, barsInto) and drawNodeMap with pan/zoom for every SVG map.
// Map projection and tile helpers (merc, fitView, mapBase, addTiles) are in traceroute.js; they are only called at draw time.
// ---- shared helpers for the mesh pages -------------------------------------------------------------
// Home, Nodes and Map all read one list: the radio's live nodes plus nodes only our database remembers.
// allNodes/allNodesAt: the shared node list and when it was fetched. homeSamples, homeData, homeNew: Home page data kept so partial refreshes can redraw.
let allNodes = [], allNodesAt = 0, homeSamples = [], homeData = null, homeNew = [];
// Time (ms) each Home section was last fetched; refreshHome compares against these to give each section its own interval.
const homeTick = { home: 0, nodes: 0, charts: 0, sensors: 0 };
// Time window in hours for the Home environment readings (restored from localStorage in main.js).
let sensorHours = 1;
// Seconds since an event as a short phrase ('just now', 'N min ago', 'N h ago', 'N d ago').
const agoStr = s => s == null ? "unknown" : s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
// Chart colours for packet types, assigned in order.
const PKT_COLORS = ["#5b8def", "#4ade80", "#fbbf24", "#c084fc", "#f87171", "#2dd4bf", "#fb923c", "#94a3b8"];
// Readable label for a packet type name, e.g. TEXT_MESSAGE_APP -> 'text message'.
const pktLabel = t => t === "ENCRYPTED" ? "encrypted (other channels)" : t.replace(/_APP$/, "").replace(/_/g, " ").toLowerCase();
// Icon for each kind of event in the activity feed.
const FEED_ICON = { ai: "🤖", dm: "✉️", you: "📤", telemetry: "📊", broadcast: "📡", traceroute: "🧭", node: "🆕" };
// Returns mk(tag, attrs, text), which creates an SVG element, appends it to svg and returns it. Text is set with textContent.
const svgMaker = svg => { const ns = "http://www.w3.org/2000/svg"; return (tag, attrs, text) => { const e = document.createElementNS(ns, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); if (text != null) e.textContent = text; svg.append(e); return e; }; };
const nodeName = n => n.label || n.name || n.short || n.id;      // your own label first, if you gave one
// Rounds to two decimals and appends a unit; null when the value is missing.
const fmtN = (v, u = "") => v == null ? null : (Math.round(v * 100) / 100) + u;
// Battery text; a level above 100 means the node reports external power.
const battStr = n => n.battery == null ? "–" : n.battery > 100 ? "powered" : n.battery + "%";
// CSS class for a node's hop distance (us, direct, one hop, two or more, unknown).
const hopsCls = n => n.us ? "us" : n.hops === 0 ? "h0" : n.hops === 1 ? "h1" : n.hops >= 2 ? "h2" : "hx";
// Text for a node's hop distance. Nodes known only from our database (stored) read 'remembered'.
const hopsText = n => n.us ? "us" : n.stored ? "remembered" : n.hops == null ? "hops unknown" : n.hops === 0 ? "direct" : `${n.hops} hop${n.hops === 1 ? "" : "s"}`;
// Our own node (the connected radio) from allNodes, or undefined.
const usNode = () => allNodes.find(n => n.us);
// A card containing only a placeholder message.
const emptyCard = text => el("div", "card empty", text);
// Great-circle (haversine) distance in km between two {lat, lon} points; 6371 is the Earth's mean radius in km.
const distKm = (a, b) => {
  const r = Math.PI / 180, h = Math.sin((b.lat - a.lat) * r / 2) ** 2 + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin((b.lon - a.lon) * r / 2) ** 2;
  return 2 * 6371 * Math.asin(Math.min(1, Math.sqrt(h)));
};
let distUnit = "km";      // km or mi; saved on the server (the AI's distances use it too)
// Sets the display unit (km or mi) and updates the header button label.
function setDistUnit(u) { distUnit = u === "mi" ? "mi" : "km"; const b = $("distBtn"); if (b) b.textContent = distUnit; }
const fmtKm = km => {       // the name is historic: takes kilometres, prints in the chosen unit
  if (distUnit === "mi") { const mi = km / 1.609344; return mi < 0.1 ? Math.round(mi * 5280) + " ft" : (mi < 10 ? mi.toFixed(1) : Math.round(mi)) + " mi"; }
  return km < 1 ? Math.round(km * 1000) + " m" : (km < 10 ? km.toFixed(1) : Math.round(km)) + " km";
};
const homeSelect = id => go("nodes", id);       // the traffic chart's "busiest senders" rows link here

// Refreshes allNodes from /api/mesh/nodes?all=1, which also includes nodes only the database remembers.
// A copy younger than 8 s is reused so switching pages does not refetch.
async function fetchNodes(force) {       // -> "fresh" | "cached" | false
  if (!force && Date.now() - allNodesAt < 8000) return "cached";
  try { allNodes = await api("/api/mesh/nodes?all=1"); allNodesAt = Date.now(); return "fresh"; } catch { return false; }
}

// Fills box with summary tiles, optionally with a sparkline and a click target.
function tilesInto(box, specs) {         // specs: [label, value, sub, sparkline svg | null, page to open on click | null]
  box.replaceChildren();
  for (const [label, value, sub, sparkSvg, target] of specs) {
    const t = el("div", "tile" + (target ? " clickable" : ""));
    t.append(el("div", "label", label), el("div", "value", String(value)), el("div", "sub", sub || ""));
    if (sparkSvg) t.append(sparkSvg);
    if (target) t.addEventListener("click", () => go(target));
    box.append(t);
  }
}

// Builds a tiny sparkline SVG of samples[key] over time, or null with fewer than three points. Non-scaling stroke keeps the line width constant when the SVG is stretched.
function spark(samples, key) {
  const pts = samples.filter(s => s[key] != null);
  if (pts.length < 3) return null;
  const t0 = pts[0].ts, t1 = pts[pts.length - 1].ts, vs = pts.map(p => p[key]), lo = Math.min(...vs), span = (Math.max(...vs) - lo) || 1;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); svg.setAttribute("class", "spark"); svg.setAttribute("viewBox", "0 0 100 26"); svg.setAttribute("preserveAspectRatio", "none");
  svgMaker(svg)("polyline", { points: pts.map(p => `${((p.ts - t0) / (t1 - t0 || 1) * 100).toFixed(1)},${(24 - (p[key] - lo) / span * 22).toFixed(1)}`).join(" "), "vector-effect": "non-scaling-stroke" });
  return svg;
}

// Draws horizontal bars for a {name: count} map, scaled to the largest count.
function barsInto(box, counts) {
  box.replaceChildren();
  const ent = Object.entries(counts || {});
  if (!ent.length) { box.append(el("div", "hint", "Nothing yet.")); return; }
  const max = Math.max(...ent.map(e => e[1]));
  for (const [k, v] of ent) {
    const r = el("div", "hbar"), track = el("div", "track"), fill = el("i"); fill.style.width = (100 * v / max) + "%"; track.append(fill);
    r.append(el("span", null, k.replace(/_/g, " ").toLowerCase()), track, el("b", null, String(v))); box.append(r);
  }
}

// ---- pan and zoom for every map: drag to move, wheel to zoom at the pointer, double-click to zoom in, buttons on the map ----------
const mapViews = new Map();      // svg id -> {S, cx, cy}: the view the user chose; absent = the automatic framing
// Zoom limits. S is pixels per unit of the 0..1 Mercator world, so 256 * 2^z matches map tile zoom level z (here 2 to 19).
const MAP_S_MIN = 256 * 4, MAP_S_MAX = 256 * 2 ** 19;
// Keeps the view centre's vertical position inside the Mercator world, away from the poles.
const clampCy = y => Math.min(0.999, Math.max(0.001, y));

function applyView(svg, auto, W, H) {       // the user's view if they moved the map, otherwise the automatic one
  const o = mapViews.get(svg.id); let v = auto;
  if (o) { const x0 = o.cx - W / (2 * o.S), y0 = o.cy - H / (2 * o.S); v = { S: o.S, x0, y0, cy: o.cy, W, H, P: ([x, y]) => [(x - x0) * o.S, (y - y0) * o.S] }; }
  svg._view = v; svg._W = W; svg._H = H; return v;
}
// The map's current view: the user's chosen one, or one derived from the automatic framing.
const viewNow = svg => mapViews.get(svg.id) || { S: svg._view.S, cx: svg._view.x0 + svg._W / (2 * svg._view.S), cy: svg._view.cy };
// Stores the user's view (zoom clamped) and schedules one redraw per animation frame, so a burst of drag or wheel events redraws once.
function setMapView(svg, v) {
  mapViews.set(svg.id, { S: Math.min(MAP_S_MAX, Math.max(MAP_S_MIN, v.S)), cx: v.cx, cy: clampCy(v.cy) });
  if (!svg._raf) svg._raf = requestAnimationFrame(() => { svg._raf = 0; if (svg._redraw) svg._redraw(); });
}
function zoomMap(svg, f, px, py) {          // zoom by factor f, keeping the point under (px, py) where it is
  if (!svg._view) return;
  const v = viewNow(svg), W = svg._W, H = svg._H, S2 = Math.min(MAP_S_MAX, Math.max(MAP_S_MIN, v.S * f));
  const wx = v.cx - W / (2 * v.S) + px / v.S, wy = v.cy - H / (2 * v.S) + py / v.S;
  setMapView(svg, { S: S2, cx: wx - px / S2 + W / (2 * S2), cy: wy - py / S2 + H / (2 * S2) });
}
// Adds drag, wheel and double-click handling to a map SVG once (guarded by svg._pz); later calls only update the redraw callback.
function enablePanZoom(svg, redraw) {
  svg._redraw = redraw;
  if (svg._pz) return; svg._pz = true;
  const scale = () => svg._W / svg.getBoundingClientRect().width;
  const at = e => { const r = svg.getBoundingClientRect(), s = scale(); return [(e.clientX - r.left) * s, (e.clientY - r.top) * s]; };
  let drag = null;
  svg.addEventListener("dragstart", e => e.preventDefault());       // the browser's own picture-drag would steal the mouse mid-pan
  svg.addEventListener("pointerdown", e => { if (e.button === 0 && svg._view) drag = { id: e.pointerId, sx: e.clientX, sy: e.clientY, x: e.clientX, y: e.clientY, moved: false }; });
  svg.addEventListener("pointermove", e => {
    if (!drag || e.pointerId !== drag.id) return;
    if (!drag.moved) { if (Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) < 5) return; drag.moved = true; try { svg.setPointerCapture(e.pointerId); } catch {} svg.classList.add("dragging"); }
    const v = viewNow(svg), s = scale();
    setMapView(svg, { S: v.S, cx: v.cx - (e.clientX - drag.x) * s / v.S, cy: v.cy - (e.clientY - drag.y) * s / v.S }); drag.x = e.clientX; drag.y = e.clientY;
  });
  const end = e => { if (drag && e.pointerId === drag.id) { svg._dragged = drag.moved; drag = null; svg.classList.remove("dragging"); setTimeout(() => { svg._dragged = false; }, 0); } };
  svg.addEventListener("pointerup", end); svg.addEventListener("pointercancel", end);
  svg.addEventListener("click", e => { if (svg._dragged) { e.stopPropagation(); e.preventDefault(); } }, true);      // a drag isn't a click on a node
  // Wheel zoom is proportional to the scroll amount, clamped so one fast wheel event cannot jump too far.
  svg.addEventListener("wheel", e => { e.preventDefault(); const [px, py] = at(e); zoomMap(svg, Math.exp(-Math.max(-300, Math.min(300, e.deltaY)) * 0.002), px, py); }, { passive: false });
  svg.addEventListener("dblclick", e => { e.preventDefault(); const [px, py] = at(e); zoomMap(svg, 2, px, py); });
}
function addMapControls(svg, W) {           // + / - / reset, drawn on the map (for touch screens and for finding the way back)
  const ns = "http://www.w3.org/2000/svg", H = svg._H;
  const btn = (i, label, tip, fn) => {
    const g = document.createElementNS(ns, "g"), r = document.createElementNS(ns, "rect"), tx = document.createElementNS(ns, "text"), ti = document.createElementNS(ns, "title");
    g.setAttribute("class", "mapbtn"); g.setAttribute("transform", `translate(${W - 38} ${10 + i * 34})`);
    r.setAttribute("width", 28); r.setAttribute("height", 28); r.setAttribute("rx", 6); tx.setAttribute("x", 14); tx.setAttribute("y", 15); tx.textContent = label; ti.textContent = tip;
    g.append(r, tx, ti);
    g.addEventListener("pointerdown", e => e.stopPropagation()); g.addEventListener("dblclick", e => e.stopPropagation());
    g.addEventListener("click", e => { e.stopPropagation(); fn(); });
    svg.append(g);
  };
  btn(0, "+", "Zoom in", () => zoomMap(svg, 1.6, W / 2, H / 2));
  btn(1, "−", "Zoom out", () => zoomMap(svg, 1 / 1.6, W / 2, H / 2));
  if (mapViews.has(svg.id)) btn(2, "↺", "Back to the automatic view", () => { mapViews.delete(svg.id); if (svg._redraw) svg._redraw(); });
}

// One map for Home, the Map page and the node page. `nodes` are rows from /api/mesh/nodes?all=1; only those with a position are drawn.
// Options: W, H, sel (highlighted id), tiles, labels (name every node), fitAll (don't ignore far-away nodes), onPick(node), empty,
// redraw (called when the user drags or zooms; the svg needs an id). Once the user has moved the map their view is kept.
// Returns { shown, outside }: how many nodes with a position are drawn (or on screen, once the user has moved the map) and how many are not.
function drawNodeMap(svg, nodes, o = {}) {
  const W = o.W || 680, H = o.H || 420, mk = svgMaker(svg); mapBase(svg, W, H);
  const withPos = nodes.filter(n => n.lat != null);
  if (!withPos.length) { svg._view = null; dropTiles(svg); mk("text", { class: "muted", x: 24, y: 40 }, o.empty || (nodes.length ? "No node here has a captured position yet." : "No nodes yet.")); return { shown: 0, outside: 0 }; }
  const pts = withPos.map(n => ({ n, xy: merc(n.lat, n.lon) }));
  let inView = pts;
  if (!o.fitAll && !mapViews.has(svg.id)) {   // a few nodes far away (e.g. via the internet) shouldn't shrink everyone else to a dot: frame the nearer 90%
    const mx = [...pts].map(p => p.xy[0]).sort((a, b) => a - b)[pts.length >> 1], my = [...pts].map(p => p.xy[1]).sort((a, b) => a - b)[pts.length >> 1];
    const dist = p => Math.hypot(p.xy[0] - mx, p.xy[1] - my), sorted = pts.map(dist).sort((a, b) => a - b), cut = Math.max(sorted[Math.floor(sorted.length * 0.9) - (sorted.length > 1 ? 1 : 0)] * 1.4, 1 / 2 ** 14);
    inView = pts.filter(p => p.n.us || p.n.id === o.sel || dist(p) <= cut);
  }
  const framed = o.focus && o.focus.length > 1 ? o.focus.map(([la, lo]) => merc(la, lo)) : null;      // e.g. link quality: frame us and our neighbours, not the whole mesh
  const view = applyView(svg, framed ? fitView(framed, W, H, 1.7) : fitView(inView.map(p => p.xy), W, H, 1.3), W, H);
  if (o.tiles) addTiles(svg, view); else dropTiles(svg);
  for (const pts of Object.values(o.trails || {})) {          // where nodes have been (only nodes that moved have a trail)
    if (pts.length > 1) mk("polyline", { class: "trail", points: pts.map(([la, lo]) => view.P(merc(la, lo)).map(v => v.toFixed(1)).join(",")).join(" ") });
  }
  const shared = new Map(), seen = new Map();                 // nodes at the same spot would draw on top of each other: fan those lines out side by side
  const keyOf = l => view.P(merc(...l.to)).map(v => Math.round(v / 5)).join(",");
  for (const l of o.links || []) shared.set(keyOf(l), (shared.get(keyOf(l)) || 0) + 1);
  for (const l of o.links || []) {                            // link quality: from us to a node heard directly, coloured by SNR, thicker = more packets
    let [x1, y1] = view.P(merc(...l.from)), [x2, y2] = view.P(merc(...l.to));
    const k = keyOf(l), idx = seen.get(k) || 0, count = shared.get(k); seen.set(k, idx + 1);
    if (count > 1) { const dx = x2 - x1, dy = y2 - y1, len = Math.hypot(dx, dy) || 1, off = (idx - (count - 1) / 2) * 6; x1 += -dy / len * off; y1 += dx / len * off; x2 += -dy / len * off; y2 += dx / len * off; }
    mk("line", { class: "linkline", x1, y1, x2, y2, stroke: snrColor(l.snr), "stroke-width": (2.5 + Math.min(3.5, Math.log2(l.n + 1) / 2)).toFixed(1) }).append(Object.assign(document.createElementNS("http://www.w3.org/2000/svg", "title"), { textContent: l.title }));
  }
  // Draw order: other nodes first, then the selected one, then our own node on top.
  const ordered = [...inView].sort((a, b) => (a.n.us ? 1 : 0) - (b.n.us ? 1 : 0) || (a.n.id === o.sel ? 1 : 0) - (b.n.id === o.sel ? 1 : 0));
  const named = o.labels && inView.length <= 80;
  for (const { n, xy } of ordered) {
    const [px, py] = view.P(xy), faded = !n.us && !n.stored && (n.age_s == null || n.age_s > 86400), sel = n.id === o.sel;
    const c = mk("circle", { class: `pt ${hopsCls(n)}${faded ? " faded" : ""}${n.stored ? " stored" : ""}${sel ? " sel" : ""}`, cx: px, cy: py, r: n.us || sel ? 8 : 5 });
    c.append(Object.assign(document.createElementNS("http://www.w3.org/2000/svg", "title"), { textContent: `${nodeName(n)} · ${hopsText(n)}${n.us ? "" : " · " + agoStr(n.age_s)}` }));
    if (o.onPick) c.addEventListener("click", () => o.onPick(n));
    if (n.us || sel || named) { const right = px < W - 160; mk("text", { x: right ? px + 12 : px - 12, y: py + 4, "text-anchor": right ? "start" : "end" }, nodeName(n).slice(0, 22)); }
  }
  addScale(mk, view); addMapControls(svg, W); enablePanZoom(svg, o.redraw);
  if (mapViews.has(svg.id)) {        // the user moved the map: "shown" means "currently on screen"
    const vis = pts.filter(p => { const [x, y] = view.P(p.xy); return x >= 0 && x <= W && y >= 0 && y <= H; }).length;
    return { shown: vis, outside: pts.length - vis };
  }
  return { shown: inView.length, outside: pts.length - inView.length };
}
