// nodes: part of the dashboard script (loaded in order by index.html; all files share one global scope)
// ---- nodes page: the list on the left, everything about one node on the right -----------------------------------
let nodesReady = false, nodeOpen = null, nodesShown = 80, nodePaneSig = "", nodePaneAt = 0, nodeMetric = null;

function initNodes() {
  if (nodesReady) return; nodesReady = true;
  for (const id of ["nodesQ", "nodesFilter", "nodesSort"]) for (const ev of ["input", "change"]) $(id).addEventListener(ev, () => { nodesShown = 80; renderNodeList(); });
  $("nodesMore").addEventListener("click", () => { nodesShown += 80; renderNodeList(); });
}

function visibleNodes() {
  const q = $("nodesQ").value.trim().toLowerCase(), f = $("nodesFilter").value, sort = $("nodesSort").value;
  const keep = n => ({ all: true, hour: n.age_s != null && n.age_s <= 3600, day: n.age_s != null && n.age_s <= 86400, direct: n.hops === 0 && n.age_s != null && n.age_s <= 86400,
    telemetry: n.battery != null || n.temperature != null, position: n.lat != null, low: typeof n.battery === "number" && n.battery <= 20, mqtt: n.mqtt, stored: n.stored, starred: n.starred })[f];
  const hay = n => [n.label, n.note, n.name, n.short, n.id, n.hw, n.role].filter(Boolean).join(" ").toLowerCase();
  const num = (v, dir) => v == null ? Infinity : dir * v;
  const cmp = { heard: (a, b) => num(a.age_s, 1) - num(b.age_s, 1), name: (a, b) => nodeName(a).localeCompare(nodeName(b)), hops: (a, b) => num(a.hops, 1) - num(b.hops, 1),
    battery: (a, b) => num(a.battery, 1) - num(b.battery, 1), util: (a, b) => num(a.channel_util, -1) - num(b.channel_util, -1), snr: (a, b) => num(a.snr, -1) - num(b.snr, -1) }[sort];
  return allNodes.filter(n => keep(n) && (!q || hay(n).includes(q))).sort((a, b) => (b.us - a.us) || cmp(a, b));
}

function nodeItem(n) {
  const d = el("div", "nitem" + (n.id === nodeOpen ? " sel" : "") + (n.us ? " us" : "") + (n.stored ? " stored" : "")); d.dataset.id = n.id;
  const nm = el("div", "nm"); nm.append(el("b", null, (n.starred ? "★ " : "") + nodeName(n)), el("small", null, (n.label && n.name ? n.name + " · " : "") + n.id + (n.role ? " · " + n.role.toLowerCase().replace(/_/g, " ") : "")));
  const rt = el("div", "rt2"); rt.append(el("div", null, n.us ? "now" : agoStr(n.age_s)));
  const low = typeof n.battery === "number" && n.battery <= 20, sub = el("small", null, [hopsText(n), n.battery != null ? battStr(n) : null].filter(Boolean).join(" · "));
  if (low) sub.style.color = "var(--bad)";
  rt.append(sub); d.append(nm, rt);
  d.addEventListener("click", () => go("nodes", n.id));
  return d;
}

function renderNodeList() {
  const all = visibleNodes(), list = all.slice(0, nodesShown), box = $("nodesList"), keep = box.scrollTop; box.replaceChildren();
  const stored = allNodes.filter(n => n.stored).length;
  $("nodesCount").textContent = `${all.length} of ${allNodes.length} node${allNodes.length === 1 ? "" : "s"}${stored ? ` · ${stored} remembered` : ""}`;
  if (!all.length) box.append(el("div", "empty", allNodes.length ? "No nodes match." : "No nodes yet - is the radio connected?"));
  for (const n of list) box.append(nodeItem(n));
  $("nodesMore").hidden = all.length <= nodesShown; box.scrollTop = keep;
  $("viewNodes").classList.toggle("has-sel", !!nodeOpen);
}

function openNode(id) {
  if (id !== nodeOpen) { nodeOpen = id; nodeMetric = null; nodePaneSig = ""; mapViews.delete("nodeMap"); }
  renderNodeList(); refreshNodePane(true);
  if (id) { const row = document.querySelector(`#nodesList .nitem[data-id="${CSS.escape(id)}"]`); if (row) row.scrollIntoView({ block: "nearest" }); }
}

async function refreshNodes(force) {
  const r = await fetchNodes(force);
  if (r === "fresh" || force) renderNodeList();
  refreshNodePane(false);
}

async function refreshNodePane(force) {
  const id = nodeOpen, pane = $("nodePane");
  if (!id) { nodePaneSig = ""; pane.replaceChildren(emptyCard("Pick a node on the left to see its details, telemetry history and position.")); return; }
  if (!force && Date.now() - nodePaneAt < 9000) return;
  nodePaneAt = Date.now();
  let d, t;
  let links = [], trail = [];
  try {
    const q = encodeURIComponent(id);
    [d, t, links, trail] = await Promise.all([api("/api/mesh/node?id=" + q), api("/api/telemetry?node=" + q + "&limit=300"),
      api("/api/mesh/link?id=" + q + "&hours=168").catch(() => []), api("/api/mesh/trail?id=" + q + "&days=30").catch(() => [])]);
  }
  catch { if (id === nodeOpen && !nodePaneSig) pane.replaceChildren(emptyCard("No record of that node: the radio doesn't list it and our database hasn't heard of it.")); return; }
  if (id !== nodeOpen) return;
  const { age_s, ...stable } = d.node;
  const sig = JSON.stringify([stable, d.readings, d.traceroute && d.traceroute.id, d.access, t.readings.length, t.readings[0] && t.readings[0].id,
    links.length, links.length && links[links.length - 1].ts + links[links.length - 1].n, trail.length]);
  if (sig !== nodePaneSig) { nodePaneSig = sig; buildNodePane(d, t.readings, links, trail); }
  const a = $("nodeAge"); if (a) a.textContent = d.node.us ? "now" : agoStr(d.node.age_s);
}

function buildNodePane(d, readings, links = [], trail = []) {
  const x = d.node, pane = $("nodePane"); pane.replaceChildren();
  const newest = col => { const r = readings.find(v => v[col] != null); return r ? r[col] : null; };     // readings are newest first
  const val = (live, col) => live != null ? live : newest(col);

  const head = el("div", "nhead"), back = el("button", "backbtn", "← All nodes"); back.addEventListener("click", () => go("nodes"));
  head.append(back, el("h2", null, (x.starred ? "★ " : "") + nodeName(x)), el("span", "ids", x.id + (x.label && x.name ? " · " + x.name : "")));
  const badges = el("div", "badges");
  const tag = (txt, cls) => badges.append(el("span", "badge" + (cls ? " " + cls : ""), txt));
  if (x.us) tag("our radio", "ok"); else if (x.stored) tag("remembered - the radio no longer lists it", "warn"); else tag(hopsText(x), x.hops === 0 ? "ok" : "info");
  if (x.role) tag(x.role.toLowerCase().replace(/_/g, " ")); if (x.hw) tag(x.hw.replace(/_/g, " ").toLowerCase());
  if (x.mqtt) tag("via MQTT"); if (x.starred) tag("★ starred", "warn"); if (x.favorite) tag("radio favourite"); if (d.watched) tag("on the watch list");
  head.append(badges); pane.append(head);

  const mine = el("div", "card"); mine.append(el("h3", null, "Your label and notes"));
  const mf = el("form", "telform"), lab = el("input"), star = el("button", "starbtn", x.starred ? "★ Starred" : "☆ Star"), sv = el("button", "primary", "Save");
  lab.placeholder = "Your name for this node"; lab.maxLength = 40; lab.value = x.label || ""; lab.setAttribute("aria-label", "Your label for this node"); lab.style.flex = "1 1 220px";
  star.type = "button"; star.setAttribute("aria-pressed", String(!!x.starred)); sv.type = "submit";
  const nt = el("textarea"); nt.placeholder = "Notes (only you see these)"; nt.maxLength = 500; nt.rows = 2; nt.value = x.note || ""; nt.setAttribute("aria-label", "Your notes for this node");
  const msg = el("span", "hint"); mf.append(lab, star, sv); mine.append(mf, nt, msg, el("p", "hint", "Kept on this PC only: never sent over the radio and never shown to the AI. Your label replaces the node's own name throughout this dashboard."));
  const saveMine = async extra => { const { ok, data } = await post("/api/notes/set", { node: x.id, label: lab.value, note: nt.value, ...extra }); msg.textContent = ok ? "Saved." : (data.error || "Could not save."); if (ok) { allNodesAt = 0; nodePaneAt = 0; await fetchNodes(true); renderNodeList(); refreshNodePane(true); } };
  mf.addEventListener("submit", e => { e.preventDefault(); saveMine({}); });
  star.addEventListener("click", () => saveMine({ starred: !x.starred }));
  pane.append(mine);

  const batt = val(x.battery, "battery_level"), volt = val(x.voltage, "voltage"), chan = val(x.channel_util, "channel_utilization");
  const temp = val(x.temperature, "temperature"), hum = val(x.humidity, "relative_humidity"), pres = val(x.pressure, "barometric_pressure"), up = val(x.uptime_s, "uptime_seconds");
  const tiles = el("section", "tiles compact");
  const tile = (label, value, sub, id) => { const t = el("div", "tile"), v = el("div", "value", value); if (id) v.id = id; t.append(el("div", "label", label), v, el("div", "sub", sub || "")); tiles.append(t); };
  tile("Last heard", x.us ? "now" : agoStr(x.age_s), x.last_heard ? fmtTime(x.last_heard) : "", "nodeAge");
  tile("Battery", batt == null ? "–" : batt > 100 ? "Powered" : batt + "%", volt != null ? fmtN(volt, " V") : "");
  tile("Channel use", chan == null ? "–" : fmtN(chan, "%"), x.air_util_tx != null ? "airtime " + fmtN(x.air_util_tx, "%") : "");
  tile("Signal", x.snr == null ? "–" : fmtN(x.snr, " dB"), x.us ? "" : hopsText(x));
  if (x.distance_km != null) tile("Distance", fmtKm(x.distance_km), "from our radio");
  if (temp != null) tile("Temperature", fmtTemp(temp), [hum != null ? Math.round(hum) + "% humidity" : null, pres != null ? Math.round(pres) + " hPa" : null].filter(Boolean).join(" · "));
  if (up != null) tile("Uptime", fmtMetric("uptime_seconds", up), "as last reported");
  pane.append(tiles);

  // telemetry history: one metric at a time, chosen with the chips
  const hist = el("div", "card"); hist.append(el("h3", null, "Telemetry history"));
  const metrics = Object.keys(METRIC_INFO).filter(k => readings.some(r => r[k] != null));
  if (!metrics.length) hist.append(el("p", "hint", "Nothing recorded from this node yet. Nodes with the telemetry module broadcast every so often; everything your radio hears is stored here automatically."));
  else {
    if (!metrics.includes(nodeMetric)) nodeMetric = metrics[0];
    const chips = el("div", "chips"), svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "metricchart"); svg.setAttribute("viewBox", "0 0 640 210"); svg.setAttribute("role", "img");
    const info = el("p", "hint");
    const draw = () => {
      const pts = readings.filter(r => r[nodeMetric] != null).map(r => ({ t: r.ts, v: r[nodeMetric] })).sort((a, b) => a.t - b.t);
      svg.setAttribute("aria-label", METRIC_INFO[nodeMetric][0] + " over time");
      plotMetric(svg, pts, nodeMetric, "No readings of this metric yet.");
      info.textContent = `${pts.length} reading${pts.length === 1 ? "" : "s"} of ${METRIC_INFO[nodeMetric][0].toLowerCase()}`;
      for (const c of chips.children) c.setAttribute("aria-pressed", c.dataset.k === nodeMetric);
    };
    for (const k of metrics) { const c = el("button", "chip", METRIC_INFO[k][0]); c.dataset.k = k; c.addEventListener("click", () => { nodeMetric = k; draw(); }); chips.append(c); }
    hist.append(chips, svg, info); draw();
  }
  pane.append(hist);

  // signal: only for nodes heard directly (for a relayed packet the radio measures the relay, not the sender)
  const sig = el("div", "card"); sig.append(el("h3", null, "Signal history"));
  if (!links.length) sig.append(el("p", "hint", "No packets from this node have reached this radio directly yet, so there is no signal data. A node that is only reachable through relays has none: the radio would be measuring the relay."));
  else {
    const total = links.reduce((a, l) => a + l.n, 0), avg = k => links.reduce((a, l) => a + l[k] * l.n, 0) / total;
    const metrics = [["snr", "SNR"], ["rssi", "RSSI"], ["n", "Packets per hour"]];
    let cur = "snr";
    const chips = el("div", "chips"), svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "metricchart"); svg.setAttribute("viewBox", "0 0 640 210"); svg.setAttribute("role", "img");
    const draw = () => {
      plotMetric(svg, links.map(l => ({ t: l.ts, v: l[cur] })), cur, "");
      svg.setAttribute("aria-label", metrics.find(m => m[0] === cur)[1] + " over time");
      for (const c of chips.children) c.setAttribute("aria-pressed", c.dataset.k === cur);
    };
    for (const [k, label] of metrics) { const c = el("button", "chip", label); c.dataset.k = k; c.addEventListener("click", () => { cur = k; draw(); }); chips.append(c); }
    sig.append(chips, svg, el("p", "hint", `${total} packet${total === 1 ? "" : "s"} heard directly in the last week · average ${avg("snr").toFixed(1)} dB SNR, ${Math.round(avg("rssi"))} dBm RSSI · best ${Math.max(...links.map(l => l.snr_max))} dB, worst ${Math.min(...links.map(l => l.snr_min))} dB`));
    draw();
  }
  pane.append(sig);

  const pos = el("div", "card"); pos.append(el("h3", null, "Position"));
  if (x.lat != null) {
    const us = usNode(), msvg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    msvg.setAttribute("class", "mapsvg"); msvg.setAttribute("viewBox", "0 0 680 300"); msvg.setAttribute("role", "img"); msvg.setAttribute("aria-label", "Where this node is, relative to our radio");
    pos.append(el("p", "hint", `${x.lat.toFixed(4)}, ${x.lon.toFixed(4)} · ${x.pos_source === "packet" ? "from a position broadcast this bridge heard" : "from the radio's node list"}${x.pos_ts ? " · captured " + agoStr(Math.max(0, Date.now() / 1000 - x.pos_ts)) : ""}${x.distance_km != null ? " · " + fmtKm(x.distance_km) + " from our radio" : ""}`), msvg);
    msvg.id = "nodeMap";
    const paint = () => drawNodeMap(msvg, [us, x].filter(Boolean), { H: 300, sel: x.id, labels: true, fitAll: true, tiles: lsGet("homeTiles") === "1", redraw: paint, trails: trail.length > 1 ? { [x.id]: trail.map(p => [p.lat, p.lon]) } : null });
    paint();
  } else pos.append(el("p", "hint", "This node hasn't shared a position, so it isn't on the map."));
  pane.append(pos);

  const rec = el("div", "card"); rec.append(el("h3", null, "Latest readings"));
  if (!readings.length) rec.append(el("p", "hint", "None recorded."));
  for (const r of readings.slice(0, 8)) {
    const row = el("div", "rrow"), vals = metricPairs(r).map(([k, v]) => `${k} ${v}`).join(" · ") || "(no standard values)";
    row.append(el("span", "time", fmtTime(r.ts)), el("span", null, `${r.kind}: ${vals}`)); rec.append(row);
  }
  pane.append(rec);

  const det = el("div", "card"); det.append(el("h3", null, "Details"));
  const dl = el("dl", "kv"), kv = (k, v) => dl.append(el("dt", null, k), el("dd", null, v == null || v === "" ? "–" : String(v)));
  kv("Role", x.role && x.role.toLowerCase().replace(/_/g, " ")); kv("Hardware", x.hw && x.hw.replace(/_/g, " ").toLowerCase()); kv("Hops away", x.us ? "this is us" : x.hops);
  kv("First seen by us", x.first_seen ? fmtTime(x.first_seen) : null); kv("Public key", x.has_key ? "yes" : "no"); kv("Via MQTT", x.mqtt ? "yes" : "no");
  kv("AI access", d.access ? `${d.access.access}${d.access.effective_cap ? `, ${d.access.used_24h} of ${d.access.effective_cap} used today` : ""}` : null);
  det.append(dl);
  if (d.traceroute) det.append(el("p", "hint", `Last traceroute (${fmtTime(d.traceroute.ts)}): ` + (d.traceroute.status === "ok" ? `${d.traceroute.relays_towards} relay(s) there${d.traceroute.relays_back != null ? ", " + d.traceroute.relays_back + " back" : ""}` : d.traceroute.status)));
  const acts = el("div", "acts"), btn = (label, fn) => { const b = el("button", null, label); b.addEventListener("click", fn); acts.append(b); };
  acts.style.cssText = "display:flex;flex-wrap:wrap;gap:8px;margin-top:10px";
  if (!x.us) { btn("Trace route", () => go("traceroute", x.id)); btn("Message", () => go("dm", x.id)); }
  btn("All telemetry", () => go("telemetry", x.id));
  if (x.lat != null) btn("Show on the map", () => go("map", x.id));
  det.append(acts); pane.append(det);
}
