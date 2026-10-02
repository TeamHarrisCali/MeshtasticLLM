const $ = id => document.getElementById(id);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
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
const PAGE = 50;
let rows = new Map();          // id -> request, merged across refreshes
let expanded = new Set();
let exhausted = false;
let loadSeq = 0;

const fmtTime = ts => {
  const d = new Date(ts * 1000), today = new Date();
  const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return d.toDateString() === today.toDateString() ? t : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + t;
};
const fmtMs = ms => ms == null ? "–" : ms < 1000 ? ms + " ms" : (ms / 1000).toFixed(1) + " s";
const fmtUp = s => s < 3600 ? Math.floor(s / 60) + " min" : (s / 3600).toFixed(1) + " h";

function delivery(r) {
  if (!r.chunks) return ["–", ""];
  if (r.failed) return [`${r.delivered}/${r.chunks} · ${r.failed} failed`, "bad"];
  if (r.delivered >= r.chunks) return [`${r.delivered}/${r.chunks} ✓`, "ok"];
  if (r.delivered + r.relayed >= r.chunks) return [`${r.delivered + r.relayed}/${r.chunks} sent`, "warn"];
  return ["pending", ""];
}

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

const filters = () => new URLSearchParams({ q: $("q").value.trim(), status: $("status").value });

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(path + " " + r.status);
  return r.json();
}

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

async function loadOlder() {
  const oldest = Math.min(...rows.keys());
  const p = filters(); p.set("limit", PAGE); p.set("before", oldest);
  const data = await api("/api/requests?" + p);
  for (const r of data) rows.set(r.id, r);
  if (data.length < PAGE) exhausted = true;
  render();
}

async function refreshTop() {
  try {
    const [s, st] = await Promise.all([api("/api/status"), api("/api/stats")]);
    $("radioDot").className = "dot " + (s.connected ? "ok" : (s.searching ? "warn" : "bad"));
    $("radioTxt").textContent = s.connected
      ? `${s.node.long_name || "Node"} · ${s.port} · up ${fmtUp(s.uptime_s)}`
      : (s.searching ? "Looking for a radio… plug one in" + (s.node.long_name ? ` (last: ${s.node.long_name})` : "") : "Radio disconnected");
    if (s.dist_unit && s.dist_unit !== distUnit) { setDistUnit(s.dist_unit); if (view) route(); }
    if (s.temp_unit && s.temp_unit !== tempUnit) { setTempUnit(s.temp_unit); if (view) route(); }     // changed from another browser
    syncRadioBanner(s.radio_change);
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
    const sent = st.delivered + st.relayed + st.failed;
    $("tDel").textContent = sent ? Math.round(100 * st.delivered / (st.delivered + st.relayed + st.failed)) + "%" : "–";
    $("tDelSub").textContent = `${st.delivered} acked · ${st.relayed} relayed · ${st.failed} failed`;
    $("tNodes").textContent = st.unique_nodes;
    $("tTop").textContent = st.top_nodes.length ? "Top: " + st.top_nodes.map(n => `${n.node_name || n.node_id} (${n.n})`).join(", ") : "";
  } catch {
    $("radioDot").className = "dot bad"; $("radioTxt").textContent = "Bridge not reachable";
  }
}
