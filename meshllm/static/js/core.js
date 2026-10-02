// core: shared helpers used by every other file ($, el, api, time/size formatters, STATUS labels), plus the AI log page
// (the request list: /api/requests with search, status filter and paging) and refreshTop, the header and stat-tile refresh
// (/api/status, /api/stats). Loaded first; all files are joined into one script and share one global scope.
// Shorthand for document.getElementById.
const $ = id => document.getElementById(id);
// Creates an element with an optional class and text. Text goes in via textContent, so it is never parsed as HTML.
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
// Request status from the server -> [label, badge colour class] for the AI log and conversation bubbles.
const STATUS = {
  answered: ["Answered", "ok"], queued: ["In progress", "info"], llm_error: ["Model error", "bad"],
  rate_limited: ["Rate limited", "warn"], usage: ["Usage hint", ""], paused: ["Paused", "warn"],
  reset: ["Memory reset", ""], inbound: ["Inbound DM", ""], manual: ["Operator", "info"],
  blocked: ["Blocked", "bad"], denied: ["Not allowed", "warn"], cap: ["Daily limit", "warn"],
  busy: ["Queue full", "warn"], cancelled: ["Cancelled", ""], expired: ["Expired", ""],
  action_ok: ["AI tool run", "ok"], action_pending: ["Awaiting code", "warn"], action_running: ["Running", "info"],
  action_failed: ["Action failed", "bad"], action_denied: ["Action refused", "warn"],
  help: ["Help", ""], ping: ["Ping", ""], status: ["Status", ""],
};
// Number of AI log rows fetched per request.
const PAGE = 50;
let rows = new Map();          // id -> request, merged across refreshes
let expanded = new Set();
let exhausted = false;
let loadSeq = 0;

// Formats a Unix timestamp (seconds) as a clock time, with the date added when it is not today.
const fmtTime = ts => {
  const d = new Date(ts * 1000), today = new Date();
  const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return d.toDateString() === today.toDateString() ? t : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + t;
};
// Milliseconds as 'N ms' or 'N.N s'; a dash when unknown.
const fmtMs = ms => ms == null ? "–" : ms < 1000 ? ms + " ms" : (ms / 1000).toFixed(1) + " s";
// Uptime in seconds as minutes (under an hour) or hours.
// " · heard 12 s ago": when the radio last sent anything (only connection modes that record it report it)
const lastHeard = s => s.last_rx_age_s == null ? "" : ` · heard ${s.last_rx_age_s < 120 ? s.last_rx_age_s + " s" : Math.round(s.last_rx_age_s / 60) + " min"} ago`;
const fmtUp = s => s < 3600 ? Math.floor(s / 60) + " min" : (s / 3600).toFixed(1) + " h";

// Returns [text, colour class] summarising how many reply chunks were acked, relayed or failed.
function delivery(r) {
  if (!r.chunks) return ["–", ""];
  if (r.failed) return [`${r.delivered}/${r.chunks} · ${r.failed} failed`, "bad"];
  if (r.delivered >= r.chunks) return [`${r.delivered}/${r.chunks} ✓`, "ok"];
  if (r.delivered + r.relayed >= r.chunks) return [`${r.delivered + r.relayed}/${r.chunks} sent`, "warn"];
  return ["pending", ""];
}

// Builds one expandable AI log row. Open rows are remembered in 'expanded' so a redraw keeps them open.
function rowEl(r) {
  const row = el("div", "row" + (expanded.has(r.id) ? " open" : ""));
  const head = el("div", "head");
  head.append(el("div", "time", fmtTime(r.ts)));
  const node = el("div", "node");
  node.append(el("b", null, r.node_name || "Unnamed node"), el("span", null, r.node_id));
  const isManual = r.kind === "manual", isInbound = r.kind === "inbound";
  head.append(node, el("div", "prompt", (isManual ? r.response : r.prompt) || "(empty)"));
  const [label, cls] = STATUS[r.status] || [r.status, ""];
  const badgeWrap = el("div"); badgeWrap.append(el("span", "badge " + cls, label));
  head.append(badgeWrap, el("div", "num llm", fmtMs(r.llm_ms)));
  const [dt, dcls] = delivery(r);
  const d = el("div", "num deliv", dt);
  if (dcls) d.style.color = `var(--${dcls})`;
  head.append(d);
  head.addEventListener("click", () => {
    row.classList.toggle("open");
    row.classList.contains("open") ? expanded.add(r.id) : expanded.delete(r.id);
  });

  const detail = el("div", "detail");
  if (isManual) detail.append(el("h4", null, "Message sent by operator"), el("pre", null, r.response || ""));
  else if (isInbound) detail.append(el("h4", null, "Message (not sent to the AI)"), el("pre", null, r.prompt || ""));
  else detail.append(el("h4", null, "Prompt"), el("pre", null, r.prompt || "(empty)"),
                     el("h4", null, "Reply"), el("pre", null, r.response || "(none yet)"));
  const meta = el("div", "meta");
  const bits = [["Message", "#" + r.id]];
  if (!isManual && !isInbound) bits.push(["Model", r.model || "–"]);
  if (r.action) bits.push(["AI tool", r.action]);
  if (r.auth) bits.push(["Sender check", r.auth]);
  if (r.delivery_note) bits.push(["Delivery note", r.delivery_note]);
  if (r.chunks) bits.push(["Delivered", `${r.delivered} acked, ${r.relayed} relayed, ${r.failed} failed of ${r.chunks} sent`]);
  if (!isManual) bits.push(["Signal", r.rx_snr == null ? "–" : `SNR ${r.rx_snr} dB` + (r.rx_rssi == null ? "" : `, RSSI ${r.rx_rssi} dBm`)],
                           ["Hops", r.hops == null ? "–" : r.hops]);
  for (const [k, v] of bits) meta.append(el("span", null, `${k}: ${v}`));
  detail.append(meta);
  row.append(head, detail);
  return row;
}

let lastSig = "";
// Redraws the AI log list, newest request first, and updates the Load more button and the count note.
function render() {
  const list = [...rows.values()].sort((a, b) => b.id - a.id);
  const sig = JSON.stringify(list) + exhausted;
  if (sig === lastSig) return;   // nothing changed; keep the DOM (and any text selection) intact
  lastSig = sig;
  const box = $("rows");
  box.replaceChildren();
  if (!list.length) { box.append(el("div", "empty", "No requests yet. Send “/ai <question>” as a direct message to this node.")); }
  for (const r of list) box.append(rowEl(r));
  $("moreBtn").hidden = exhausted || list.length < PAGE;
  $("rangeNote").textContent = list.length ? `Showing ${list.length} request${list.length === 1 ? "" : "s"}` : "";
}

// Search text and status filter as query parameters for /api/requests.
const filters = () => new URLSearchParams({ q: $("q").value.trim(), status: $("status").value });

// GET a JSON endpoint and return the parsed body; throws on a non-2xx status so callers can catch and skip that refresh.
// Used by every page.
async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(path + " " + r.status);
  return r.json();
}

// Fetches the newest page of requests and merges it into 'rows'; reset=true first drops what was loaded (filters changed).
async function refreshRows(reset) {
  const seq = ++loadSeq;
  const p = filters(); p.set("limit", PAGE);
  let data;
  try { data = await api("/api/requests?" + p); } catch { return; }
  if (seq !== loadSeq) return;               // a newer request superseded this one
  if (reset) { rows = new Map(); exhausted = false; }
  for (const r of data) rows.set(r.id, r);
  render();
}

// Loads the next page of requests older than the oldest one shown ('before' is a request id, not a time).
async function loadOlder() {
  const oldest = Math.min(...rows.keys());
  const p = filters(); p.set("limit", PAGE); p.set("before", oldest);
  const data = await api("/api/requests?" + p);
  for (const r of data) rows.set(r.id, r);
  if (data.length < PAGE) exhausted = true;
  render();
}

// Updates the header (radio and Ollama status, pause button) and the stat tiles. Called every poll tick from main.js.
// A failed fetch is shown as 'Bridge not reachable'.
async function refreshTop() {
  try {
    const [s, st] = await Promise.all([api("/api/status"), api("/api/stats")]);
    $("radioDot").className = "dot " + (s.connected ? "ok" : (s.searching ? "warn" : "bad"));
    $("radioTxt").textContent = s.connected
      ? `${s.node.long_name || "Node"} · ${s.port} · up ${fmtUp(s.uptime_s)}${lastHeard(s)}`
      : (s.searching ? "Looking for a radio…" + (s.node.long_name ? ` (last: ${s.node.long_name})` : "") : "Radio disconnected");
    // Units are stored on the server: adopt a change made in another browser and redraw the current page in the new unit.
    if (s.dist_unit && s.dist_unit !== distUnit) { setDistUnit(s.dist_unit); if (view) route(); }
    if (s.temp_unit && s.temp_unit !== tempUnit) { setTempUnit(s.temp_unit); if (view) route(); }     // changed from another browser
    syncRadioBanner(s.radio_change);
    $("demoBadge").hidden = !s.demo;      // --demo: simulated radio and mesh
    $("llmDot").className = "dot " + (s.ollama_ok ? "ok" : "bad");
    $("llmTxt").textContent = s.ollama_ok ? `Ollama · ${s.model}` : `Ollama: ${s.model} unavailable`;
    const pb = $("pauseBtn");
    pb.textContent = s.paused ? "Paused — Resume" : "Active — Pause";
    pb.className = s.paused ? "primary" : "";
    pb.dataset.paused = s.paused ? "1" : "";

    $("tRecent").textContent = st.last_24h;
    $("tTotal").textContent = `${st.total} all time`;
    $("tLat").textContent = st.avg_llm_ms == null ? "–" : fmtMs(Math.round(st.avg_llm_ms));
    $("tErr").textContent = `${st.llm_errors} model error${st.llm_errors === 1 ? "" : "s"} · ${st.rate_limited} rate limited`;
    // Delivery rate is acked chunks out of everything sent (acked + relayed + failed).
    const sent = st.delivered + st.relayed + st.failed;
    $("tDel").textContent = sent ? Math.round(100 * st.delivered / (st.delivered + st.relayed + st.failed)) + "%" : "–";
    $("tDelSub").textContent = `${st.delivered} acked · ${st.relayed} relayed · ${st.failed} failed`;
    $("tNodes").textContent = st.unique_nodes;
    $("tTop").textContent = st.top_nodes.length ? "Top: " + st.top_nodes.map(n => `${n.node_name || n.node_id} (${n.n})`).join(", ") : "";
  } catch {
    $("radioDot").className = "dot bad"; $("radioTxt").textContent = "Bridge not reachable";
  }
}
