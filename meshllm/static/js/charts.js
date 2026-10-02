// charts: SVG charts for the Home page. renderTraffic draws packets per hour by type plus the busiest senders (/api/mesh/traffic);
// renderHealth draws line charts of mesh health samples (/api/mesh/samples). Both are called from refreshHome in home.js and use
// svgMaker, PKT_COLORS, pktLabel and homeSelect from mesh-common.js.
// ---- charts -----------------------------------------------------------------------------------------------------------------
// Draws the stacked hourly packet chart with its legend and the busiest senders (top 6; clicking one opens the node).
function renderTraffic(t) {
  const svg = $("homeTraffic"); svg.replaceChildren(); const mk = svgMaker(svg), leg = $("homeTrafficLegend"); leg.replaceChildren();
  const W = 680, H = 220, L = 42, R = 8, T = 10, B = 26;
  if (!t.total) { mk("text", { x: 20, y: 40 }, "No packets counted yet. Counting starts when the bridge starts."); $("homeTalkers").replaceChildren(); return; }
  const types = t.types, color = i => PKT_COLORS[Math.min(i, PKT_COLORS.length - 1)];
  const sums = t.hours.map(h => Object.values(h.counts).reduce((a, b) => a + b, 0)), max = Math.max(...sums, 1);
  const bw = (W - L - R) / t.hours.length, Y = v => H - B - v / max * (H - T - B);
  mk("line", { class: "cax", x1: L, y1: T, x2: L, y2: H - B }); mk("line", { class: "cax", x1: L, y1: H - B, x2: W - R, y2: H - B });
  mk("text", { x: L - 6, y: T + 8, "text-anchor": "end" }, String(max)); mk("text", { x: L - 6, y: H - B, "text-anchor": "end" }, "0");
  t.hours.forEach((h, i) => {
    let acc = 0;
    types.forEach((ty, k) => {
      const v = h.counts[ty] || 0; if (!v) return;
      // h.hour is hours since the Unix epoch, hence 3600e3 (milliseconds per hour) when building the tooltip time.
      const r = mk("rect", { x: L + i * bw + 1, y: Y(acc + v), width: Math.max(1, bw - 2), height: Math.max(0.5, Y(acc) - Y(acc + v)), fill: color(k) });
      r.append(Object.assign(document.createElementNS("http://www.w3.org/2000/svg", "title"), { textContent: `${pktLabel(ty)}: ${v} · ${new Date(h.hour * 3600e3).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` }));
      acc += v;
    });
    if (i % 4 === 0 || i === t.hours.length - 1) mk("text", { x: L + i * bw + bw / 2, y: H - 8, "text-anchor": "middle" }, new Date(h.hour * 3600e3).toLocaleTimeString([], { hour: "2-digit" }));
  });
  types.forEach((ty, k) => { const s = el("span"); const key = el("i", "key"); key.style.background = color(k); s.append(key, `${pktLabel(ty)} (${t.hours.reduce((a, h) => a + (h.counts[ty] || 0), 0)})`); leg.append(s); });
  const tk = $("homeTalkers"); tk.replaceChildren();
  if (!t.talkers.length) tk.append(el("div", "empty", "No packets in the last hour."));
  for (const x of t.talkers.slice(0, 6)) { const r = el("div", "talker"), a = el("span", null, x.name || x.node_id); r.append(a, el("small", null, `${x.n} packet${x.n === 1 ? "" : "s"}`)); r.style.cursor = "pointer"; r.addEventListener("click", () => homeSelect(x.node_id)); tk.append(r); }
}

// Draws one line per series definition in defs ({key, cls, label}) from the samples; unit is appended to the axis maximum.
function lineChart(svg, samples, defs, unit) {
  svg.replaceChildren(); const mk = svgMaker(svg), W = 680, H = 190, L = 42, R = 8, T = 10, B = 24;
  const series = defs.map(d => ({ ...d, pts: samples.filter(s => s[d.key] != null).map(s => ({ t: s.ts, v: s[d.key] })) }));
  const all = series.flatMap(s => s.pts);
  if (samples.length < 2 || !all.length) { mk("text", { x: 20, y: 40 }, "Collecting history… one snapshot is taken every few minutes."); return; }
  const t0 = Math.min(...samples.map(s => s.ts)), t1 = Math.max(...samples.map(s => s.ts)), vmax = Math.max(...all.map(p => p.v), unit === "%" ? 5 : 1);
  const X = t => L + (t1 === t0 ? 0 : (t - t0) / (t1 - t0)) * (W - L - R), Y = v => H - B - v / vmax * (H - T - B);
  mk("line", { class: "cax", x1: L, y1: T, x2: L, y2: H - B }); mk("line", { class: "cax", x1: L, y1: H - B, x2: W - R, y2: H - B });
  mk("text", { x: L - 6, y: T + 8, "text-anchor": "end" }, (Math.round(vmax * 10) / 10) + (unit || "")); mk("text", { x: L - 6, y: H - B, "text-anchor": "end" }, "0");
  mk("text", { x: L, y: H - 6 }, new Date(t0 * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })); mk("text", { x: W - R, y: H - 6, "text-anchor": "end" }, new Date(t1 * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
  for (const s of series) if (s.pts.length > 1) mk("polyline", { class: `cl ${s.cls}`, points: s.pts.map(p => `${X(p.t).toFixed(1)},${Y(p.v).toFixed(1)}`).join(" ") });
}
// Draws the two health charts (nodes heard over time; channel use and airtime) and their legends.
function renderHealth(samples) {
  const defs1 = [{ key: "heard_15m", cls: "cl3", label: "heard in 15 min" }, { key: "heard_1h", cls: "cl1", label: "heard in 1 h" }, { key: "heard_24h", cls: "cl2", label: "heard in 24 h" }];
  const defs2 = [{ key: "avg_channel_util", cls: "cl1", label: "mesh average channel use" }, { key: "us_channel_util", cls: "cl2", label: "our channel use" }, { key: "us_air_util_tx", cls: "cl3", label: "our airtime" }];
  lineChart($("homeHealth1"), samples, defs1, ""); lineChart($("homeHealth2"), samples, defs2, "%");
  for (const [id, defs] of [["homeHealth1Legend", defs1], ["homeHealth2Legend", defs2]]) { const leg = $(id); leg.replaceChildren(); for (const d of defs) { const s = el("span"), k = el("i", "lk " + d.cls); s.append(k, d.label); leg.append(s); } }
}
