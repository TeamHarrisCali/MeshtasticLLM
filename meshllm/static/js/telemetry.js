// telemetry: the Telemetry page (readings the radio has heard). Uses /api/telemetry (readings, latest per node, stats, paging),
// /api/telemetry/watch plus watch/add, watch/remove and watch/all, /api/telemetry/retention and /api/telemetry/prune.
// Also defines helpers other pages reuse: tempUnit/fmtTemp, fmtMetric, metricPairs, plotMetric and resolveNode.
// ---- telemetry tab (what the radio has heard and recorded) ------------------------------------
// Telemetry kind as stored -> label for the kind filter.
const TEL_KINDS = { device: "Device (battery, airtime)", environment: "Environment (temp, humidity)", air_quality: "Air quality", power: "Power", local_stats: "Radio stats" };
const METRIC_INFO = {   // column -> [label, unit]
  battery_level: ["Battery", "%"], voltage: ["Voltage", " V"], channel_utilization: ["Channel use", "%"], air_util_tx: ["Airtime used", "%"],
  uptime_seconds: ["Uptime", ""], temperature: ["Temperature", " °C"], relative_humidity: ["Humidity", "%"],
  barometric_pressure: ["Pressure", " hPa"], gas_resistance: ["Gas resistance", " MΩ"], iaq: ["Air quality index", ""],
};
// Page state: telRows keeps loaded readings by id across refreshes, *Sig strings skip redundant redraws, pendingTelNode is a node from the URL to preselect once the dropdown is filled.
let telReady = false, telRows = new Map(), telExhausted = false, telSig = "", telStatsSig = "", pendingTelNode = null;

let tempUnit = "C";      // the display unit; the server remembers it (and the AI's replies use it too)
// Converts a Celsius value to the display unit and formats it (null if there is no value). Readings are always stored in Celsius.
const fmtTemp = (c, digits = 1) => { if (c == null) return null; const v = tempUnit === "F" ? c * 9 / 5 + 32 : c, p = 10 ** digits; return Math.round(v * p) / p + " °" + tempUnit; };
// Sets the display unit (C or F) and updates the header button label.
function setTempUnit(u) { tempUnit = u === "F" ? "F" : "C"; const b = $("unitBtn"); if (b) b.textContent = "°" + tempUnit; }

// Formats a metric value by column name with its unit; temperature follows tempUnit and uptime is shown as days/hours/minutes.
function fmtMetric(k, v) {
  if (k === "snr") return v.toFixed(1) + " dB";
  if (k === "rssi") return Math.round(v) + " dBm";
  if (k === "n") return String(Math.round(v));
  if (k === "temperature") return fmtTemp(v);
  if (k === "uptime_seconds") { const d = Math.floor(v / 86400), h = Math.floor(v % 86400 / 3600), m = Math.floor(v % 3600 / 60); return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`; }
  return (Number.isInteger(v) ? v : Math.round(v * 100) / 100) + (METRIC_INFO[k] ? METRIC_INFO[k][1] : "");
}
// [label, formatted value] for each standard metric present in a reading.
const metricPairs = r => Object.keys(METRIC_INFO).filter(k => r[k] != null).map(k => [METRIC_INFO[k][0], fmtMetric(k, r[k])]);
// Display name for a reading's node, falling back to its ID.
const nodeLabel = r => r.node_name || r.node_id;

// Turns typed text (a !xxxxxxxx ID or a node name this radio has heard) into a node ID, or null. Also used by the traceroute and coverage pages.
function resolveNode(v) {
  v = v.trim(); if (/^![0-9a-f]{8}$/i.test(v)) return v.toLowerCase();
  const m = knownNodes.find(n => [n.long_name, n.short_name].some(s => s && s.trim().toLowerCase() === v.toLowerCase()));
  return m ? m.id : null;   // names are compared trimmed: some nodes have a trailing space in their name
}

// One-time page setup: fills the dropdowns and attaches the filter, retention, record-all and watch-list handlers.
function initTelemetry() {
  if (telReady) return; telReady = true;
  for (const [k, t] of Object.entries(TEL_KINDS)) $("fltKind").append(Object.assign(document.createElement("option"), { value: k, textContent: t }));
  for (const [k, [label]] of Object.entries(METRIC_INFO)) $("chartMetric").append(Object.assign(document.createElement("option"), { value: k, textContent: label }));
  for (const id of ["fltNode", "fltKind"]) $(id).addEventListener("change", () => refreshTelemetry(true));
  $("chartNode").addEventListener("change", drawChart); $("chartMetric").addEventListener("change", drawChart);
  $("telMore").addEventListener("click", loadOlderTelemetry);
  $("retForm").addEventListener("submit", async e => {
    e.preventDefault();
    const n = $("retNote"), days = parseInt($("retDays").value, 10);
    if (!(days >= 0)) { n.textContent = "Enter a whole number of days (0 keeps everything)."; n.className = "sendnote err"; return; }
    const { ok, data } = await post("/api/telemetry/retention", { days });
    n.textContent = ok ? (days ? `Readings older than ${days} days will be deleted automatically.` : "Readings will be kept forever.") : (data.error || "Could not save."); n.className = "sendnote" + (ok ? "" : " err");
    telStatsSig = ""; refreshTelemetry(true);
  });
  $("pruneNow").addEventListener("click", async () => {
    const { ok, data } = await post("/api/telemetry/prune", {}), n = $("retNote");
    n.textContent = ok ? (data.deleted ? `Deleted ${data.deleted} old reading${data.deleted === 1 ? "" : "s"}.` : "Nothing older than the retention period.") : "Could not prune."; n.className = "sendnote" + (ok ? "" : " err");
    refreshTelemetry(true);
  });
  $("recordAll").addEventListener("change", async e => { await post("/api/telemetry/watch/all", { enabled: e.target.checked }); watchSig = ""; refreshTelemetry(true); });
  $("watchForm").addEventListener("submit", async e => {
    e.preventDefault();
    const id = resolveNode($("watchNode").value), note = $("watchNote");
    if (!id) { note.textContent = "Unknown node. Use a name your radio has heard, or an ID like !1a2b3c4d."; note.className = "sendnote err"; return; }
    const { ok, data } = await post("/api/telemetry/watch/add", { node: id });
    note.textContent = ok ? "Added to the watch list." : (data.error || "Could not add it."); note.className = "sendnote" + (ok ? "" : " err");
    if (ok) $("watchNode").value = ""; watchSig = ""; refreshTelemetry(true);
  });
}

// Node and kind filters as query parameters for /api/telemetry.
const telFilters = () => new URLSearchParams({ node: $("fltNode").value, kind: $("fltKind").value });

let watchSig = "";
// Redraws the watch list (nodes whose telemetry is recorded) from /api/telemetry/watch; skipped when unchanged.
async function refreshWatch() {
  let w; try { w = await api("/api/telemetry/watch"); } catch { return; }
  const sig = JSON.stringify(w.watched); if (sig === watchSig) return; watchSig = sig;
  const list = $("watchList"); list.replaceChildren();
  if (!w.watched.length) list.append(el("div", "empty", "No nodes on the watch list."));
  for (const n of w.watched) {
    const row = el("div", "wrow"), who = el("div"); who.append(el("b", null, n.node_name || n.node_id), el("small", null, n.node_id));
    const stats = el("div", null, n.readings ? `${n.readings} reading${n.readings === 1 ? "" : "s"} recorded · last ${fmtTime(n.last_ts)}` : "waiting for its next broadcast…");
    const rm = el("button", null, "Remove"); rm.addEventListener("click", async () => { rm.disabled = true; await post("/api/telemetry/watch/remove", { node: n.node_id }); watchSig = ""; refreshTelemetry(true); });
    row.append(who, stats, rm); list.append(row);
  }
}

// Fetches readings for the current filters and redraws the table. force=true drops the loaded rows first. The latest-per-node cards, dropdowns and chart are rebuilt only when stats or the latest set changed.
async function refreshTelemetry(force) {
  let d;
  try { const p = telFilters(); p.set("limit", 50); d = await api("/api/telemetry?" + p); } catch { return; }
  // Do not overwrite a control the user is currently using.
  if (document.activeElement !== $("retDays")) $("retDays").value = d.retention_days;
  if (document.activeElement !== $("recordAll")) $("recordAll").checked = d.record_all;
  const st = d.stats;
  $("telStats").textContent = st.total ? `${st.total} readings from ${st.nodes} node${st.nodes === 1 ? "" : "s"} · oldest ${fmtTime(st.oldest_ts)} · newest ${fmtTime(st.newest_ts)}` : "No readings yet. They appear as nodes around you broadcast telemetry.";
  if (force) { telRows = new Map(); telExhausted = false; }
  for (const r of d.readings) telRows.set(r.id, r);
  renderTelRows();
  refreshWatch();
  const ssig = JSON.stringify([st, d.latest.map(r => r.id)]);
  if (ssig !== telStatsSig || force) { telStatsSig = ssig; renderLatest(d.latest); fillNodeSelects(d.latest); drawChart(); }
}

// Rebuilds the node dropdowns from the latest readings, keeping the current choice. A node passed in the URL (pendingTelNode) is selected once it appears.
function fillNodeSelects(latest) {
  const seen = new Map(); for (const r of latest) seen.set(r.node_id, nodeLabel(r));
  for (const [id, keepAll] of [["fltNode", true], ["chartNode", false]]) {
    const sel = $(id), cur = pendingTelNode && id === "fltNode" ? pendingTelNode : sel.value; sel.replaceChildren();
    if (keepAll) sel.append(Object.assign(document.createElement("option"), { value: "", textContent: "All nodes" }));
    for (const [nid, label] of seen) sel.append(Object.assign(document.createElement("option"), { value: nid, textContent: label }));
    if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
  }
  if (pendingTelNode) {
    if ($("fltNode").value === pendingTelNode) { $("chartNode").value = pendingTelNode; pendingTelNode = null; refreshTelemetry(true); }
    else pendingTelNode = null;
  }
}

// Draws one card per node with its newest readings.
function renderLatest(latest) {
  const box = $("telLatest"); box.replaceChildren();
  if (!latest.length) { box.append(el("div", "empty", "No readings yet.")); return; }
  for (const r of latest) {
    const c = el("div", "lcard");
    c.append(el("b", null, nodeLabel(r)), el("small", null, `${TEL_KINDS[r.kind] || r.kind} · ${fmtTime(r.ts)}`));
    const dl = el("dl"); for (const [k, v] of metricPairs(r)) dl.append(el("dt", null, k), el("dd", null, v));
    c.append(dl); box.append(c);
  }
}

// Draws the readings table, newest first; the Load more button is hidden when the last page came back short.
function renderTelRows() {
  const list = [...telRows.values()].sort((a, b) => b.id - a.id);
  const sig = JSON.stringify(list) + telExhausted; if (sig === telSig) return; telSig = sig;
  const box = $("telRows"); box.replaceChildren();
  if (!list.length) box.append(el("div", "empty", "Nothing recorded yet."));
  for (const r of list) {
    const row = el("div", "trow");
    const via = r.source === "broadcast" ? "heard 📡" : r.source === "job" || r.source === "manual" ? "asked (old)" : r.source;
    const vals = metricPairs(r).map(([k, v]) => `${k} ${v}`).join(" · ") || "(no standard values; see CSV)";
    const link = [r.rx_snr != null ? `SNR ${r.rx_snr}` : null, r.hops != null ? `${r.hops} hop${r.hops === 1 ? "" : "s"}` : null].filter(Boolean).join(" · ") || "–";
    row.append(el("div", "time", fmtTime(r.ts)), el("div", "mname", nodeLabel(r)), el("div", null, r.kind), el("div", null, via), el("div", "vals", vals), el("div", "time", link));
    box.append(row);
  }
  $("telMore").hidden = telExhausted || list.length < 50;
}

// Fetches the next 50 readings older than the oldest one loaded (paged by reading id).
async function loadOlderTelemetry() {
  const p = telFilters(); p.set("limit", 50); p.set("before", Math.min(...telRows.keys()));
  const d = await api("/api/telemetry?" + p);
  for (const r of d.readings) telRows.set(r.id, r);
  if (d.readings.length < 50) telExhausted = true;
  renderTelRows();
}
// Draws a line chart of one metric into an SVG element. Also used by the Nodes page. Labels are added with textContent.
function plotMetric(svg, pts, metric, emptyMsg) {       // one metric over time; pts = [{t, v}] oldest first
  svg.replaceChildren();
  const ns = "http://www.w3.org/2000/svg", mk = (tag, attrs, text) => { const e = document.createElementNS(ns, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); if (text != null) e.textContent = text; svg.append(e); return e; };
  if (pts.length < 2) { mk("text", { x: 20, y: 40 }, pts.length ? "One reading so far - a line needs at least two." : emptyMsg); return; }
  const L = 52, R = 12, T = 12, B = 26, W = 640, H = 210;
  const t0 = pts[0].t, t1 = pts[pts.length - 1].t; let v0 = Math.min(...pts.map(p => p.v)), v1 = Math.max(...pts.map(p => p.v));
  if (v0 === v1) { v0 -= 1; v1 += 1; }
  const X = t => L + (t1 === t0 ? 0 : (t - t0) / (t1 - t0)) * (W - L - R), Y = v => H - B - (v - v0) / (v1 - v0) * (H - T - B);
  mk("line", { class: "ax", x1: L, y1: T, x2: L, y2: H - B }); mk("line", { class: "ax", x1: L, y1: H - B, x2: W - R, y2: H - B });
  mk("text", { x: L - 6, y: T + 8, "text-anchor": "end" }, fmtMetric(metric, v1)); mk("text", { x: L - 6, y: H - B, "text-anchor": "end" }, fmtMetric(metric, v0));
  mk("text", { x: L, y: H - 6 }, fmtTime(t0)); mk("text", { x: W - R, y: H - 6, "text-anchor": "end" }, fmtTime(t1));
  mk("polyline", { class: "ln", points: pts.map(p => `${X(p.t).toFixed(1)},${Y(p.v).toFixed(1)}`).join(" ") });
  for (const p of pts) mk("circle", { class: "dot", cx: X(p.t).toFixed(1), cy: Y(p.v).toFixed(1), r: 2.5 });
}

// Charts the chosen metric for the chosen node from its latest 300 readings.
async function drawChart() {
  const svg = $("telChart"), node = $("chartNode").value, metric = $("chartMetric").value;
  if (!node) { plotMetric(svg, [], metric, "Readings will be charted here once a node has been heard."); $("chartInfo").textContent = ""; return; }
  let d; try { d = await api(`/api/telemetry?node=${encodeURIComponent(node)}&limit=300`); } catch { return; }
  const pick = m => d.readings.filter(r => r[m] != null).map(r => ({ t: r.ts, v: r[m] })).sort((a, b) => a.t - b.t);
  const pts = pick(metric);
  if (!pts.length) {   // this node never reports the chosen metric (e.g. battery for a weather node): show one it does report
    const alt = Object.keys(METRIC_INFO).find(k => d.readings.some(r => r[k] != null));
    if (alt) { $("chartMetric").value = alt; return drawChart(); }
  }
  $("chartInfo").textContent = `${pts.length} reading${pts.length === 1 ? "" : "s"}`;
  plotMetric(svg, pts, metric, "No readings of this metric for this node yet.");
}
