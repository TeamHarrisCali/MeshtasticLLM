// home: the Home page. Uses /api/home (summary tiles and alerts), /api/mesh/feed (newly heard nodes), /api/mesh/sensors (environment
// readings), /api/mesh/traffic and /api/mesh/samples (charts drawn by renderTraffic/renderHealth in charts.js) and /api/radio/time.
// The node list and map drawing come from mesh-common.js.
// ---- home ---------------------------------------------------------------------------------------------
// Refreshes the Home page. Each section has its own minimum interval (homeTick): summary 4 s, nodes 9 s, sensors 15 s, charts 55 s,
// so the 3 s page tick does not refetch heavier data. force=true refreshes everything.
async function refreshHome(force) {
  const now = Date.now(), due = (k, ms) => force || now - homeTick[k] >= ms;
  if (due("home", 4000)) {
    homeTick.home = now;
    try { const [d, fresh] = await Promise.all([api("/api/home"), api("/api/mesh/feed?types=node&limit=6")]); homeData = d; homeNew = fresh; renderHome(d); } catch {}
  }
  if (due("sensors", 15000)) { homeTick.sensors = now; try { renderSensors(await api("/api/mesh/sensors?hours=" + sensorHours)); } catch {} }
  if (due("nodes", 9000)) { homeTick.nodes = now; if (await fetchNodes(force)) renderHomeNodes(); }
  if (due("charts", 55000)) {
    homeTick.charts = now;
    try { const [t, s] = await Promise.all([api("/api/mesh/traffic?hours=24"), api("/api/mesh/samples?hours=24")]); homeSamples = s; renderTraffic(t); renderHealth(s); if (homeData) renderHome(homeData); } catch {}
  }
}

// Draws the summary tiles, alerts, newly heard nodes, role and hardware bars and the AI status from the /api/home data d.
function renderHome(d) {
  const s = d.summary, us = d.us || {}, r = d.radio, sp = k => spark(homeSamples, k);
  const pct = v => v == null ? "–" : (Math.round(v * 10) / 10) + "%";
  tilesInto($("homeTiles"), [
    ["Radio", r.connected ? (r.node.long_name || "Connected") : r.searching ? "Searching…" : "Disconnected", `${r.port || ""}${r.connected ? " · up " + fmtUp(r.uptime_s) + lastHeard(r) : ""}`],
    ["Nodes known", s.nodes_total, `${d.places.total} on the map · ${s.with_telemetry} with telemetry`, null, "nodes"],
    ["Heard in the last hour", s.heard_1h, `15 min: ${s.heard_15m} · 24 h: ${s.heard_24h}`, sp("heard_1h"), "nodes"],
    ["Direct neighbours", s.direct, s.max_hops != null ? `furthest heard: ${s.max_hops} hops` : "heard in the last 24 h", sp("direct"), "nodes"],
    ["Channel use (our radio)", pct(us.channel_util), `mesh average ${pct(s.avg_channel_util)} · our airtime ${pct(us.air_util_tx)}`, sp("us_channel_util")],
    ["Our battery", us.battery == null ? "–" : us.battery > 100 ? "Powered" : us.battery + "%", us.voltage ? us.voltage.toFixed(2) + " V" : "–", sp("us_battery")],
    ["AI questions · 24 h", d.activity.ai_24h, `${r.model}${r.queue_depth ? " · queue " + r.queue_depth : ""}${r.paused ? " · PAUSED" : ""}`, null, "ai"],
    ["Telemetry heard · 24 h", d.activity.telemetry_24h, `${d.activity.traceroutes} traceroute${d.activity.traceroutes === 1 ? "" : "s"} saved`, null, "telemetry"],
  ]);
  const al = $("homeAlerts"); al.replaceChildren();
  for (const a of d.alerts) {
    const row = el("div", "alert " + a.level, a.text); if (a.node_id) { row.style.cursor = "pointer"; row.addEventListener("click", () => go("nodes", a.node_id)); }
    // An alert can offer to fix the radio's clock; this sets it from this PC's time after confirmation.
    if (a.action === "sync_clock") {
      const b = el("button", null, "Set the radio's clock"); b.style.marginLeft = "auto"; b.style.flex = "0 0 auto";
      b.addEventListener("click", async () => {
        if (!confirm("Tell the radio the current time from this PC?\n\nThis changes the radio's clock only. Afterwards it stamps the nodes it hears correctly.")) return;
        b.disabled = true; const { ok, data } = await post("/api/radio/time", {});
        if (!ok) { alert(data.error || "Could not set the clock."); b.disabled = false; return; }
        b.textContent = "Clock set"; homeTick.home = 0;
      });
      row.append(b);
    }
    al.append(row);
  }

  const fresh = $("homeNew"); fresh.replaceChildren();
  if (!homeNew.length) fresh.append(el("div", "hint", "No new node has been heard since this bridge started remembering them."));
  for (const f of homeNew) {
    const row = el("div", "irow"); row.append(el("span", null, f.text.replace(/^New node heard: /, "")), el("small", null, agoStr(Math.max(0, Date.now() / 1000 - f.ts))));
    row.addEventListener("click", () => go("nodes", f.node_id)); fresh.append(row);
  }
  barsInto($("homeRoles"), s.roles); barsInto($("homeHw"), s.hardware);
  const ai = $("homeAi"); ai.replaceChildren();
  for (const [k, v] of [["Model", `${r.model} (${r.ollama_ok ? "ready" : "unavailable"})`], ["State", r.paused ? "paused" : "answering"], ["Waiting in the queue", r.queue_depth], ["Questions in 24 h", d.activity.ai_24h]]) {
    const row = el("div", "irow static"); row.append(el("span", null, k), el("small", null, String(v))); ai.append(row);
  }
}

// Draws the environment readings (overall averages plus one card per node) from /api/mesh/sensors, and adopts the temperature unit the server reports.
function renderSensors(s) {
  setTempUnit(s.unit);
  const head = $("sensorHead"), nodes = $("sensorNodes"), o = s.overall; head.replaceChildren(); nodes.replaceChildren();
  const span = s.hours >= 1 ? `${Math.round(s.hours)} h` : `${Math.round(s.hours * 60)} min`;
  if (!s.nodes.length) {
    $("sensorNote").textContent = "";
    head.append(el("div", "hint", `No environment readings in the last ${span}.` + (s.latest_any ? ` The latest came from ${s.latest_any.name}, ${agoStr(Math.max(0, Date.now() / 1000 - s.latest_any.ts))}; try a longer window.` : " Nodes with a sensor broadcast them every so often, and everything the radio hears is recorded.")));
    return;
  }
  $("sensorNote").textContent = `average of ${o.nodes} node${o.nodes === 1 ? "" : "s"} · ${o.readings} reading${o.readings === 1 ? "" : "s"}`;
  const wrap = el("div", "sensorhead");
  for (const [label, txt] of [["Temperature", o.temperature != null ? fmtTemp(o.temperature) : null], ["Humidity", o.humidity != null ? Math.round(o.humidity) + "%" : null], ["Pressure", o.pressure != null ? Math.round(o.pressure) + " hPa" : null]]) {
    if (!txt) continue; const b = el("div", "big"); b.append(el("b", null, txt), el("small", null, label)); wrap.append(b);
  }
  head.append(wrap);
  for (const n of s.nodes) {
    const bits = [n.temperature != null ? fmtTemp(n.temperature) : null, n.humidity != null ? Math.round(n.humidity) + "% humidity" : null, n.pressure != null ? Math.round(n.pressure) + " hPa" : null].filter(Boolean);
    const card = el("div", "snode"); card.append(el("b", null, n.name || n.id), el("span", null, bits.join(" · ") || "no values"), el("small", null, `${n.n} reading${n.n === 1 ? "" : "s"} · ${agoStr(Math.max(0, Date.now() / 1000 - n.last_ts))}`));
    card.addEventListener("click", () => go("nodes", n.id)); nodes.append(card);
  }
}

// Draws the Home map and its caption. Map tile backgrounds are an opt-in kept in localStorage.
function paintHomeMap() {
  const r = drawNodeMap($("homeMap"), allNodes, { tiles: lsGet("homeTiles") === "1", onPick: n => go("nodes", n.id), redraw: paintHomeMap, empty: allNodes.length ? "None of the nodes we know has shared a position yet." : "No nodes yet." });
  const stored = allNodes.filter(n => n.lat != null && n.stored).length;
  $("homeMapInfo").textContent = r.shown ? `${r.shown} shown${stored ? ` · ${stored} remembered` : ""}${r.outside ? ` · ${r.outside} not in view` : ""}` : "";
}

// Lists starred nodes (battery at or below 20% is shown in the warning colour); the card is hidden when there are none.
function renderStars() {
  const box = $("homeStars"), rows = allNodes.filter(n => n.starred && !n.us);
  $("starCard").hidden = !rows.length; box.replaceChildren();
  for (const n of rows) {
    const low = typeof n.battery === "number" && n.battery <= 20, d = el("div", "nitem"), nm = el("div", "nm"), rt = el("div", "rt2");
    nm.append(el("b", null, "★ " + nodeName(n)), el("small", null, (n.label && n.name ? n.name + " · " : "") + hopsText(n)));
    rt.append(el("div", null, agoStr(n.age_s))); const sub = el("small", null, n.battery != null ? battStr(n) : ""); if (low) sub.style.color = "var(--bad)"; rt.append(sub);
    d.append(nm, rt); d.addEventListener("click", () => go("nodes", n.id)); box.append(d);
  }
}

// Draws starred nodes, the map and the five nearest nodes (by distance if our position is known, otherwise by hop count).
function renderHomeNodes() {
  renderStars();
  paintHomeMap();
  const near = $("homeNear"); near.replaceChildren();
  const us = usNode(), hasUs = us && us.lat != null;
  const rows = allNodes.filter(n => !n.us && (hasUs ? n.lat != null : !n.stored && n.hops != null))
    .sort(hasUs ? (a, b) => distKm(us, a) - distKm(us, b) : (a, b) => a.hops - b.hops || (a.age_s ?? Infinity) - (b.age_s ?? Infinity)).slice(0, 5);
  if (!rows.length) near.append(el("div", "hint", "Needs nodes that share a position (or a hop count)."));
  for (const n of rows) {
    const row = el("div", "irow"); row.append(el("span", null, nodeName(n)), el("small", null, (hasUs ? fmtKm(distKm(us, n)) : hopsText(n)) + " · " + agoStr(n.age_s)));
    row.addEventListener("click", () => go("nodes", n.id)); near.append(row);
  }
}
