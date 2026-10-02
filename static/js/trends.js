// trends: part of the dashboard script (loaded in order by index.html; all files share one global scope)
// ---- link quality: how well we hear the nodes next to us (only nodes heard directly; a relayed packet's signal is the relay's) -----------------
const snrColor = s => s >= 5 ? "#22c55e" : s >= 0 ? "#84cc16" : s >= -5 ? "#f59e0b" : s >= -10 ? "#f97316" : "#ef4444";
let mapLinkData = null;
async function loadLinks() { try { mapLinkData = await api("/api/mesh/linkmap?hours=" + ($("linkHours").value || 24)); } catch {} }

function mapLinkLines() {
  const d = mapLinkData; if (!$("mapLinks").checked || !d || !d.us) return null;
  return d.links.filter(l => l.lat != null).map(l => ({ from: [d.us.lat, d.us.lon], to: [l.lat, l.lon], snr: l.snr, n: l.n,
    title: `${l.name || l.id} · SNR ${l.snr.toFixed(1)} dB (${l.snr_min} to ${l.snr_max}) · RSSI ${Math.round(l.rssi)} dBm · ${l.n} packet${l.n === 1 ? "" : "s"} · ${fmtKm(l.distance_km)}` }));
}

function renderLinkCard() {
  const d = mapLinkData; if (!d) return;
  const table = $("linkTable"); table.replaceChildren();
  const far = d.farthest;
  $("linkNote").textContent = d.links.length ? `${d.links.length} node${d.links.length === 1 ? "" : "s"} heard directly${far ? ` · farthest: ${far.name || far.id}, ${fmtKm(far.distance_km)}` : ""}` : "";
  if (!d.links.length) table.append(el("div", "hint", "No node has been heard directly in this period yet. Packets that arrive through relays don't count."));
  for (const l of d.links) {
    const row = el("div", "lrow"), nm = el("div"), dot = el("i", "snrdot"); dot.style.background = snrColor(l.snr);
    nm.append(dot, el("b", null, l.name || l.id)); if (l.stored) nm.append(el("small", null, "remembered"));
    row.append(nm, el("div", null, `${l.snr.toFixed(1)} dB`), el("div", "hide-sm", `${Math.round(l.rssi)} dBm`), el("div", null, String(l.n)),
      el("div", null, l.distance_km != null ? fmtKm(l.distance_km) : "–"), el("div", "hide-sm", agoStr(Math.max(0, Date.now() / 1000 - l.last_ts))));
    row.addEventListener("click", () => go("nodes", l.id)); table.append(row);
  }
  // signal against distance: the real range picture (each dot is one neighbour)
  const svg = $("linkScatter"); svg.replaceChildren(); const mk = svgMaker(svg), W = 680, H = 250, L = 50, R = 14, T = 14, B = 34;
  const pts = d.links.filter(l => l.distance_km != null);
  if (!pts.length) { mk("text", { x: 20, y: 40 }, d.us ? "Needs directly heard nodes that share a position." : "Our radio has no position yet, so distances are unknown."); return; }
  const unit = distUnit === "mi" ? 1 / 1.609344 : 1, xs = pts.map(p => p.distance_km * unit), xmax = Math.max(...xs, unit) * 1.12;
  const ymin = Math.min(-20, ...pts.map(p => p.snr_min)) - 1, ymax = Math.max(10, ...pts.map(p => p.snr_max)) + 1;
  const X = v => L + v / xmax * (W - L - R), Y = v => H - B - (v - ymin) / (ymax - ymin) * (H - T - B);
  mk("line", { class: "cax", x1: L, y1: T, x2: L, y2: H - B }); mk("line", { class: "cax", x1: L, y1: H - B, x2: W - R, y2: H - B });
  mk("line", { class: "cax", x1: L, y1: Y(0), x2: W - R, y2: Y(0), "stroke-dasharray": "2 4" }); mk("text", { x: L - 6, y: Y(0) + 4, "text-anchor": "end" }, "0 dB");
  mk("line", { class: "cax", x1: L, y1: Y(-17.5), x2: W - R, y2: Y(-17.5), "stroke-dasharray": "6 4", stroke: "#ef4444" }); mk("text", { x: W - R, y: Y(-17.5) - 4, "text-anchor": "end" }, "about the lowest SNR LongFast can decode");
  mk("text", { x: L - 6, y: T + 8, "text-anchor": "end" }, Math.round(ymax) + " dB"); mk("text", { x: L, y: H - 8 }, "0"); mk("text", { x: W - R, y: H - 8, "text-anchor": "end" }, `${xmax < 10 ? xmax.toFixed(1) : Math.round(xmax)} ${distUnit} away`);
  for (const p of pts) {
    const x = X(p.distance_km * unit);
    mk("line", { x1: x, x2: x, y1: Y(p.snr_min), y2: Y(p.snr_max), stroke: snrColor(p.snr), "stroke-width": 2, "stroke-opacity": .45 });     // best-to-worst range at that distance
    mk("circle", { cx: x, cy: Y(p.snr), r: 5, fill: snrColor(p.snr), stroke: "var(--surface)", "stroke-width": 1.5, style: "cursor:pointer" }).append(Object.assign(document.createElementNS("http://www.w3.org/2000/svg", "title"), { textContent: `${p.name || p.id}: ${p.snr.toFixed(1)} dB at ${fmtKm(p.distance_km)}` }));
  }
}

// ---- trends: when the mesh is busiest, and how far packets travel ---------------------------------------------------------------------------
let trendsReady = false, trendsAt = 0, trafficData = null, hopData = null;
const hourLabel = h => `${h % 12 || 12} ${h < 12 ? "AM" : "PM"}`;
const HOP_COLORS = ["#5b8def", "#c084fc", "#fbbf24", "#94a3b8"], HOP_NAMES = ["direct", "1 relay", "2 relays", "3+ relays"];

function initTrends() {
  if (trendsReady) return; trendsReady = true;
  $("hopWindow").value = lsGet("hopHours") || "24"; if (!$("hopWindow").value) $("hopWindow").value = "24";
  $("hopWindow").addEventListener("change", async e => { lsSet("hopHours", e.target.value); hopData = await api("/api/mesh/hops?hours=" + e.target.value).catch(() => null); renderHops(); });
  $("busyType").addEventListener("change", renderBusy);
  let rs; window.addEventListener("resize", () => { clearTimeout(rs); rs = setTimeout(() => { if (view === "trends") { renderHops(); renderBusy(); } }, 150); });
}

async function refreshTrends(force) {
  if (!force && Date.now() - trendsAt < 30000) return; trendsAt = Date.now();
  try { [trafficData, hopData] = await Promise.all([api("/api/mesh/traffic?hours=336"), api("/api/mesh/hops?hours=" + $("hopWindow").value)]); } catch { return; }
  const sel = $("busyType"), cur = sel.value; sel.replaceChildren(Object.assign(el("option"), { value: "", textContent: "all packets" }));
  for (const ty of trafficData.types) sel.append(Object.assign(el("option"), { value: ty, textContent: pktLabel(ty) }));
  sel.value = [...sel.options].some(o => o.value === cur) ? cur : "";
  renderHops(); renderBusy();      // hops first: the busiest-hours chart is sized to match that card
}

const svgWidth = (svg, H) => {       // draw at the width the chart really has, so text stays readable on a phone and on a wide screen
  const W = Math.max(320, Math.floor(svg.getBoundingClientRect().width || svg.parentElement.clientWidth || 640));
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`); return W;
};
const dayLabel = d => d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
const svgTitle = text => Object.assign(document.createElementNS("http://www.w3.org/2000/svg", "title"), { textContent: text });

function renderBusy() {
  const t = trafficData; if (!t) return;
  const type = $("busyType").value, total = h => Object.values(h.counts).reduce((a, b) => a + b, 0), val = h => type ? (h.counts[type] || 0) : total(h);
  const all = t.hours.map(h => ({ hour: h.hour, v: val(h), any: total(h) })), start = all.findIndex(h => h.any > 0);
  const svg = $("busyHour"), heat = $("busyHeat"); svg.replaceChildren(); heat.replaceChildren();
  // side by side with the hop card: make this chart as tall as that card's own content (measured from its children, so it can't ratchet up on every refresh)
  const grid = svg.closest(".trendgrid"), two = grid && getComputedStyle(grid).gridTemplateColumns.trim().split(/\s+/).length === 2, sib = $("hopChart").closest(".card");
  const sibContent = [...sib.children].reduce((a, c) => a + c.getBoundingClientRect().height + 12, 0), mine = svg.parentElement.querySelector(".chead").getBoundingClientRect().height + 14;
  const H = two ? Math.max(260, Math.min(420, Math.round(sibContent - mine))) : 260, W = svgWidth(svg, H), HH = 7 * 30 + 64, W2 = svgWidth(heat, HH), mk = svgMaker(svg), mk2 = svgMaker(heat), note = $("busyNote"); note.replaceChildren();
  if (start < 0) { mk("text", { x: 20, y: 40 }, "Nothing counted yet. Counting starts when the bridge starts."); return; }
  const used = all.slice(start, all.length > start + 1 ? -1 : undefined);     // leave out the hour in progress
  const sum = Array(24).fill(0), cnt = Array(24).fill(0);
  for (const h of used) { const hh = new Date(h.hour * 3600e3).getHours(); sum[hh] += h.v; cnt[hh]++; }
  const avg = sum.map((s, i) => cnt[i] ? s / cnt[i] : null), have = avg.filter(v => v != null), max = Math.max(...have, 1);
  const L = 52, R = 14, T = 18, B = 40, bw = (W - L - R) / 24, step = W < 560 ? 6 : 3, num = v => v >= 10 ? Math.round(v) : Math.round(v * 10) / 10;
  mk("line", { class: "cax", x1: L, y1: T, x2: L, y2: H - B }); mk("line", { class: "cax", x1: L, y1: H - B, x2: W - R, y2: H - B });
  mk("text", { x: L - 8, y: T + 10, "text-anchor": "end" }, String(num(max))); mk("text", { x: L - 8, y: H - B + 4, "text-anchor": "end" }, "0");
  const best = avg.indexOf(Math.max(...have)), quiet = avg.indexOf(Math.min(...have));
  avg.forEach((v, i) => {
    if (v == null) return; const hgt = v / max * (H - T - B);
    mk("rect", { x: L + i * bw + 3, y: H - B - hgt, width: Math.max(3, bw - 6), height: Math.max(1, hgt), rx: 3, fill: i === best ? "var(--accent)" : "var(--muted)", "fill-opacity": i === best ? 1 : .55 })
      .append(svgTitle(`${hourLabel(i)}: about ${num(v)} packets an hour (${cnt[i]} hour${cnt[i] === 1 ? "" : "s"} of data)`));
  });
  for (let i = 0; i < 24; i += step) mk("text", { x: L + i * bw + bw / 2, y: H - B + 20, "text-anchor": "middle" }, hourLabel(i));
  note.append(el("div", null, `Busiest ${hourLabel(best)} (about ${num(avg[best])} packets an hour), quietest ${hourLabel(quiet)} (${num(avg[quiet])}).`),
    el("div", null, used.length < 48 ? `Based on ${used.length} hour${used.length === 1 ? "" : "s"} so far; it sharpens as days add up.` : `Based on ${used.length} hours of data.`));
  // the same, one square per hour for the last 7 days
  const byHour = new Map(all.map(h => [h.hour, h])), now = new Date(), days = [];
  for (let d = 6; d >= 0; d--) days.push(new Date(now.getFullYear(), now.getMonth(), now.getDate() - d));
  const first = all[start].hour, lastHour = all[all.length - 1].hour, labelW = 116, cw = (W2 - labelW - 8) / 24, ch = 30, peak = Math.max(...all.map(h => h.v), 1);
  days.forEach((day, r) => {
    mk2("text", { x: labelW - 10, y: 12 + r * ch + ch / 2 + 1, "text-anchor": "end" }, dayLabel(day));
    for (let hh = 0; hh < 24; hh++) {
      const idx = Math.floor(new Date(day.getFullYear(), day.getMonth(), day.getDate(), hh).getTime() / 3600e3), cell = byHour.get(idx), known = cell && idx >= first && idx <= lastHour;
      mk2("rect", { x: labelW + hh * cw + 1.5, y: 10 + r * ch, width: Math.max(2, cw - 3), height: ch - 4, rx: 4, fill: known ? "var(--accent)" : "var(--idle-bg)", "fill-opacity": known ? (.12 + .88 * (cell.v / peak)).toFixed(2) : 1 })
        .append(svgTitle(known ? `${dayLabel(day)}, ${hourLabel(hh)}: ${cell.v} packet${cell.v === 1 ? "" : "s"}` : "nothing recorded"));
    }
  });
  for (let hh = 0; hh < 24; hh += step) mk2("text", { x: labelW + hh * cw + cw / 2, y: 10 + 7 * ch + 18, "text-anchor": "middle" }, hourLabel(hh));
}

function renderHops() {
  const d = hopData; if (!d) return;
  const span = Number($("hopWindow").value), svg = $("hopChart"), leg = $("hopLegend"); svg.replaceChildren(); leg.replaceChildren();
  const cat = k => Math.min(3, Number(k)), H = 250, W = svgWidth(svg, H), mk = svgMaker(svg), L = 52, R = 14, T = 18, B = 40;
  const maxRelays = Math.max(-1, ...d.hours.flatMap(h => Object.keys(h.counts).map(Number)));
  tilesInto($("hopTiles"), [["Average relays", d.mean == null ? "–" : d.mean.toFixed(2), "per packet heard"], ["Heard directly", d.direct_pct == null ? "–" : Math.round(d.direct_pct) + "%", "nobody relayed them"],
    ["Packets counted", d.total.toLocaleString(), `last ${span >= 24 ? span / 24 + " d" : span + " h"}`], ["Furthest seen", maxRelays < 0 ? "–" : maxRelays === 0 ? "direct only" : maxRelays + " relay" + (maxRelays === 1 ? "" : "s"), "most on one packet"]]);
  const note = $("hopNote"); note.textContent = d.total ? "Packets heard, by how many relays they passed through." : "Nothing counted in this period yet.";
  if (!d.total) { mk("text", { x: 20, y: 40 }, "No packets with hop data yet."); return; }
  const size = Math.max(1, Math.ceil(d.hours.length / 72)), buckets = [];          // at most ~72 bars: group hours when the period is long
  for (let i = 0; i < d.hours.length; i += size) {
    const b = { ts: d.hours[i].ts, c: [0, 0, 0, 0] };
    for (const h of d.hours.slice(i, i + size)) for (const [k, n] of Object.entries(h.counts)) b.c[cat(k)] += n;
    buckets.push(b);
  }
  const tot = b => b.c.reduce((a, x) => a + x, 0), max = Math.max(...buckets.map(tot), 1), bw = (W - L - R) / buckets.length, Y = v => H - B - v / max * (H - T - B);
  mk("line", { class: "cax", x1: L, y1: T, x2: L, y2: H - B }); mk("line", { class: "cax", x1: L, y1: H - B, x2: W - R, y2: H - B });
  mk("text", { x: L - 8, y: T + 10, "text-anchor": "end" }, String(max)); mk("text", { x: L - 8, y: H - B + 4, "text-anchor": "end" }, "0");
  buckets.forEach((b, i) => {
    let acc = 0;
    b.c.forEach((n, k) => { if (!n) return; mk("rect", { x: L + i * bw + 1, y: Y(acc + n), width: Math.max(2, bw - 2), height: Math.max(.5, Y(acc) - Y(acc + n)), fill: HOP_COLORS[k] })
      .append(svgTitle(`${new Date(b.ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric" })}: ${n} ${HOP_NAMES[k]}`)); acc += n; });
  });
  const when = ts => new Date(ts * 1000).toLocaleString([], span > 24 ? { month: "short", day: "numeric" } : { hour: "numeric" });
  [0, Math.floor(buckets.length / 2), buckets.length - 1].forEach((i, k) => mk("text", { x: k === 0 ? L : k === 2 ? W - R : L + i * bw, y: H - B + 20, "text-anchor": k === 0 ? "start" : k === 2 ? "end" : "middle" }, when(buckets[i].ts)));
  HOP_NAMES.forEach((nm, k) => { const s = el("span"), key = el("i", "key"); key.style.background = HOP_COLORS[k]; s.append(key, `${nm} (${buckets.reduce((a, b) => a + b.c[k], 0).toLocaleString()})`); leg.append(s); });
}
