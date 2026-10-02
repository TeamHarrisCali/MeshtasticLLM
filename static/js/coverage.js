// coverage: part of the dashboard script (loaded in order by index.html; all files share one global scope)
// ---- coverage: how far and how well the radio reaches, and the walk test ---------------------------------------------------------------------
let covReady = false, covData = null, covAt = 0, walkState = null, walkSel = null, walkAt = 0;
const SVGNS = "http://www.w3.org/2000/svg";
const withTitle = (node, text) => { const t = document.createElementNS(SVGNS, "title"); t.textContent = text; node.append(t); return node; };

function initCoverage() {
  if (covReady) return; covReady = true;
  $("covDays").value = lsGet("covDays") || "30"; if (!$("covDays").value) $("covDays").value = "30";
  $("covTiles").checked = lsGet("covTiles") === "1";
  $("covDays").addEventListener("change", () => { lsSet("covDays", $("covDays").value); refreshCoverage(true); });
  $("covTiles").addEventListener("change", e => { lsSet("covTiles", e.target.checked ? "1" : "0"); drawCoverageMap(); });
  $("walkForm").addEventListener("submit", onWalkStart);
  $("walkStop").addEventListener("click", async () => { await post("/api/coverage/walk/stop", {}); refreshWalk(true); });
  let rs; window.addEventListener("resize", () => { clearTimeout(rs); rs = setTimeout(() => { if (view === "coverage") { drawCovScatter(); drawWalkChart(); } }, 150); });
}

async function refreshCoverage(force) {
  if (force || Date.now() - covAt > 30000) {
    covAt = Date.now();
    try { covData = await api("/api/coverage?days=" + $("covDays").value); } catch { covData = null; }
    if (covData) { $("covNote").textContent = covData.us ? `· ${covData.nodes.length} node${covData.nodes.length === 1 ? "" : "s"} heard directly` + (covData.summary && covData.summary.farthest ? `, furthest ${covData.summary.farthest.name} at ${fmtKm(covData.summary.farthest.km)}` : "") : ""; drawCoverageMap(); drawCovScatter(); renderSectors(); }
  }
  refreshWalk(force);
}

function ringSteps(mpp, W, H) {       // a few round distances whose circles fit on the map
  const out = [], maxPx = Math.min(W, H) * 0.48;
  for (const km of [0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500]) { const px = km * 1000 / mpp; if (px >= 55 && px <= maxPx * 2.2) out.push([km, px]); if (out.length === 3) break; }
  return out;
}

function drawCoverageMap() {
  const svg = $("covMap"), d = covData; if (!d) return;
  const W = 680, H = 440, mk = svgMaker(svg); mapBase(svg, W, H);
  if (!d.us) { svg._view = null; dropTiles(svg); mk("text", { class: "muted", x: 24, y: 40 }, d.message || "No position for this radio yet."); $("covMapNote").textContent = d.message || ""; return; }
  const pts = [[d.us.lat, d.us.lon], ...d.nodes.map(n => [n.lat, n.lon])].map(([la, lo]) => merc(la, lo));
  const view = applyView(svg, fitView(pts, W, H, 1.4), W, H);
  if ($("covTiles").checked) addTiles(svg, view); else dropTiles(svg);
  const [ux, uy] = view.P(merc(d.us.lat, d.us.lon));
  const lat = Math.atan(Math.sinh(Math.PI * (1 - 2 * view.cy))) * 180 / Math.PI, mpp = 40075016.686 * Math.cos(lat * Math.PI / 180) / view.S;
  for (const [km, px] of ringSteps(mpp, W, H)) {
    mk("circle", { class: "covring", cx: ux, cy: uy, r: px });
    mk("text", { class: "covringlabel", x: ux, y: uy - px - 3, "text-anchor": "middle" }, fmtKm(km));
  }
  for (const s of d.sectors) {       // the furthest heard in each of the eight directions, drawn as a wedge: a rough picture of the coverage shape
    if (s.max_km == null) continue;
    const r = s.max_km * 1000 / mpp, pt = deg => { const a = deg * Math.PI / 180; return [ux + r * Math.sin(a), uy - r * Math.cos(a)].map(v => v.toFixed(1)); };
    const [x1, y1] = pt(s.center - 22.5), [x2, y2] = pt(s.center + 22.5);
    withTitle(mk("path", { class: "covreach", d: `M${ux.toFixed(1)},${uy.toFixed(1)} L${x1},${y1} A${r.toFixed(1)},${r.toFixed(1)} 0 0 1 ${x2},${y2} Z` }), `${s.name}: heard as far as ${fmtKm(s.max_km)} (${s.node})`);
  }
  const named = d.nodes.length <= 12;
  for (const n of [...d.nodes].sort((a, b) => a.snr - b.snr)) {
    const [x, y] = view.P(merc(n.lat, n.lon)), col = snrColor(n.snr);
    mk("circle", { cx: x, cy: y, r: 13 + Math.min(10, Math.log2(n.n + 1)), fill: col, "fill-opacity": 0.22 });         // a soft halo: stronger and busier links glow more
    const c = mk("circle", { cx: x, cy: y, r: 5, fill: col, stroke: "var(--surface)", "stroke-width": 1.5, style: "cursor:pointer" });
    withTitle(c, `${n.name || n.id}: ${n.snr.toFixed(1)} dB average, ${fmtKm(n.distance_km)} away, ${compass(n.bearing)}`);
    c.addEventListener("click", () => go("nodes", n.id));
    if (named) { const right = x < W - 150; mk("text", { x: right ? x + 10 : x - 10, y: y - 8, "text-anchor": right ? "start" : "end" }, (n.name || n.id).slice(0, 20)); }
  }
  mk("circle", { class: "pt us", cx: ux, cy: uy, r: 8 }); mk("text", { x: ux + 12, y: uy + 4 }, (usNode() ? nodeName(usNode()) : "this radio").slice(0, 22));
  addScale(mk, view); addMapControls(svg, W); enablePanZoom(svg, drawCoverageMap);
  $("covMapNote").textContent = d.summary.without_position ? `${d.summary.without_position} node${d.summary.without_position === 1 ? " was" : "s were"} heard directly but ${d.summary.without_position === 1 ? "has" : "have"} shared no position, so ${d.summary.without_position === 1 ? "it isn't" : "they aren't"} drawn.` : "";
}

const COMPASS = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"];
const compass = deg => COMPASS[Math.floor(((deg % 360) + 22.5) / 45) % 8];

function scatter(svg, pts, o) {       // pts: [{d (km), snr, id?}], o: {trend, empty, line}
  svg.replaceChildren(); const W = svgWidth(svg, o.H || 260), H = o.H || 260, L = 50, R = 14, T = 14, B = 34, mk = svgMaker(svg);
  if (!pts.length) { mk("text", { x: 20, y: 40 }, o.empty); return; }
  const unit = distUnit === "mi" ? 1 / 1.609344 : 1, xmax = Math.max(...pts.map(p => p.d * unit), unit * 0.5) * 1.1;
  const ymin = Math.min(-20, ...pts.map(p => p.snr)) - 1, ymax = Math.max(10, ...pts.map(p => p.snr)) + 1;
  const X = v => L + v / xmax * (W - L - R), Y = v => H - B - (v - ymin) / (ymax - ymin) * (H - T - B);
  mk("line", { class: "cax", x1: L, y1: T, x2: L, y2: H - B }); mk("line", { class: "cax", x1: L, y1: H - B, x2: W - R, y2: H - B });
  mk("line", { class: "cax", x1: L, y1: Y(0), x2: W - R, y2: Y(0), "stroke-dasharray": "2 4" }); mk("text", { x: L - 6, y: Y(0) + 4, "text-anchor": "end" }, "0 dB");
  mk("line", { class: "cax", x1: L, y1: Y(-17.5), x2: W - R, y2: Y(-17.5), "stroke-dasharray": "6 4", stroke: "#ef4444" }); mk("text", { x: W - R, y: Y(-17.5) - 4, "text-anchor": "end" }, "about the lowest SNR LongFast can decode");
  mk("text", { x: L - 6, y: T + 8, "text-anchor": "end" }, Math.round(ymax) + " dB"); mk("text", { x: L, y: H - 8 }, "0"); mk("text", { x: W - R, y: H - 8, "text-anchor": "end" }, `${xmax < 10 ? xmax.toFixed(1) : Math.round(xmax)} ${distUnit} away`);
  if (o.trend) { const f = k => (o.trend.at_0_km + o.trend.slope_db_per_km * k); mk("line", { x1: X(0), y1: Y(f(0)), x2: X(xmax / unit * unit), y2: Y(f(xmax / unit)), stroke: "var(--accent)", "stroke-width": 2, "stroke-dasharray": "5 4", "stroke-opacity": .8 }); }
  for (const p of pts) withTitle(mk("circle", { cx: X(p.d * unit), cy: Y(p.snr), r: 4, fill: snrColor(p.snr), "fill-opacity": 0.7 }), `${p.id ? p.id + ": " : ""}${p.snr.toFixed(1)} dB at ${fmtKm(p.d)}`);
}

function drawCovScatter() {
  const d = covData; if (!d) return;
  const pts = d.points.map(p => ({ d: p.d, snr: p.snr, id: (d.nodes.find(n => n.id === p.id) || {}).name || p.id }));
  scatter($("covScatter"), pts, { trend: d.trend, empty: d.us ? "No signal data in this period yet." : "Needs this radio's position." });
  const t = d.trend;
  $("covTrend").textContent = t ? `Each dot is one node's average for one hour. The dashed line is the straight-line fit: about ${t.slope_db_per_km.toFixed(1)} dB per km${t.r2 != null ? ` (fit ${t.r2.toFixed(2)})` : ""} from ${t.n} points and ${t.nodes} nodes. It describes what was measured here, not a promise of range: terrain and antennas matter more than distance.`
    : pts.length ? "Each dot is one node's average signal for one hour. A trend line appears once there are enough points from at least three nodes at different distances." : "";
}

function renderSectors() {
  const d = covData, box = $("covSectors"); box.replaceChildren(); if (!d) return;
  const max = Math.max(...d.sectors.map(s => s.max_km || 0), 0.001);
  for (const s of d.sectors) {
    const r = el("div", "hbar"), track = el("div", "track"), fill = el("i"); fill.style.width = (100 * (s.max_km || 0) / max) + "%"; track.append(fill);
    r.append(el("span", null, s.name), track, el("b", null, s.max_km != null ? fmtKm(s.max_km) : "–")); if (s.node) r.title = `${s.node}${s.nodes > 1 ? " and " + (s.nodes - 1) + " more" : ""}`; box.append(r);
  }
}

// ---- walk test --------------------------------------------------------------------------------------------------------------------------------
async function onWalkStart(e) {
  e.preventDefault(); const id = resolveNode($("walkNode").value);
  if (!id) { $("walkStatus").textContent = "Unknown node. Use a name this radio has heard, or an ID like !1a2b3c4d."; return; }
  const { ok, data } = await post("/api/coverage/walk/start", { node: id });
  if (!ok) { $("walkStatus").textContent = data.error || "Could not start."; return; }
  walkSel = data.id; refreshWalk(true);
}

async function refreshWalk(force) {
  if (!force && Date.now() - walkAt < (walkState && walkState.active ? 2500 : 12000)) return; walkAt = Date.now();
  try { walkState = await api("/api/coverage/walk"); } catch { return; }
  const a = walkState.active;
  $("walkStart").hidden = !!a; $("walkStop").hidden = !a; $("walkNode").disabled = !!a;
  if (a) { walkSel = a.id; $("walkStatus").textContent = `Listening to ${a.node_id}: ${a.samples} packet${a.samples === 1 ? "" : "s"} logged since ${fmtTime(a.started)}. Carry the node around, then press Stop.`; }
  else if (!force || !$("walkStatus").textContent.startsWith("Unknown")) $("walkStatus").textContent = "";
  if (walkSel == null && walkState.sessions.length) walkSel = walkState.sessions[0].id;
  renderWalkList();
  if (walkSel != null) { try { walkData = await api("/api/coverage/walk/session?id=" + walkSel); } catch { walkData = null; } } else walkData = null;
  drawWalkChart();
}

let walkData = null;
function drawWalkChart() {
  const svg = $("walkChart"), d = walkData; svg.hidden = !d;
  if (!d) { $("walkSummary").textContent = ""; return; }
  const direct = d.samples.filter(s => s.hops === 0 && s.distance_km != null).map(s => ({ d: s.distance_km, snr: s.snr }));
  scatter(svg, direct, { trend: d.trend, H: 240, empty: "No directly heard packets with a known distance yet." });
  const s = d.summary;
  $("walkSummary").textContent = s.samples ? `${s.samples} packets logged, ${s.direct} heard directly with a distance` + (s.farthest_direct_km != null ? ` · furthest direct: ${fmtKm(s.farthest_direct_km)}` : "") + (s.weakest_snr != null ? ` · SNR from ${s.weakest_snr.toFixed(1)} to ${s.strongest_snr.toFixed(1)} dB` : "")
    + (d.trend ? ` · about ${d.trend.slope_db_per_km.toFixed(1)} dB per km` : "") : "Nothing logged in this test yet.";
}

function renderWalkList() {
  const box = $("walkList"); box.replaceChildren();
  if (!walkState.sessions.length) { box.append(el("p", "hint", "No walk tests yet.")); return; }
  for (const s of walkState.sessions) {
    const row = el("div", "setrow bk" + (s.id === walkSel ? " sel" : "")), what = el("span"), tools = el("span", "bkbtns");
    what.append(el("b", null, s.node_name || s.node_id), el("small", null, ` ${fmtTime(s.started)}${s.ended ? "" : " · running"} · ${s.samples} packets${s.farthest_direct != null ? " · furthest direct " + fmtKm(s.farthest_direct) : ""}`));
    const view = el("button", null, "Show"), del = el("button", null, "Delete");
    view.addEventListener("click", () => { walkSel = s.id; refreshWalk(true); });
    del.disabled = !s.ended;
    del.addEventListener("click", async () => { if (!confirm("Delete this walk test and its samples?")) return; const { ok, data } = await post("/api/coverage/walk/delete", { id: s.id }); if (ok && walkSel === s.id) walkSel = null; else if (!ok) alert(data.error || "Could not delete."); refreshWalk(true); });
    tools.append(view, del); row.append(what, tools); box.append(row);
  }
}
