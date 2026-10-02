// map: the Map page (large node map with filters, optional signal-quality link lines, movement trails and tile backgrounds) and the
// picker for setting the radio's fixed position. Uses /api/mesh/trails, /api/tiles/stats, /api/tiles/clear and /api/radio/position.
// Drawing is drawNodeMap (mesh-common.js); link data is in trends.js; projection and tile helpers (merc, mapBase, addTiles) in traceroute.js.
// ---- map page -------------------------------------------------------------------------------------------------------
// mapSel: id of the node picked on the map.
let mapReady = false, mapSel = null;

// One-time setup: restores the saved toggles (tiles, link lines, trails, link window) from localStorage and wires the controls.
function initMap() {
  if (mapReady) return; mapReady = true;
  $("mapTiles").checked = lsGet("homeTiles") === "1";
  $("mapTiles").addEventListener("change", e => { lsSet("homeTiles", e.target.checked ? "1" : "0"); drawMapPage(); });
  for (const id of ["mapAge", "mapStored", "mapLabels", "mapAll"]) $(id).addEventListener("change", drawMapPage);
  $("mapLinks").checked = lsGet("mapLinks") === "1"; $("snrLegend").hidden = !$("mapLinks").checked;
  $("mapLinks").addEventListener("change", e => { lsSet("mapLinks", e.target.checked ? "1" : "0"); $("snrLegend").hidden = !e.target.checked; paintMap(); });
  $("linkHours").value = lsGet("linkHours") || "24";
  if (!$("linkHours").value) $("linkHours").value = "24";
  $("linkHours").addEventListener("change", async e => { lsSet("linkHours", e.target.value); await loadLinks(); paintMap(); renderLinkCard(); });
  $("mapTrails").checked = lsGet("mapTrails") === "1";
  $("mapTrails").addEventListener("change", async e => { lsSet("mapTrails", e.target.checked ? "1" : "0"); await loadTrails(); paintMap(); });
  initPos();
  $("tileClear").addEventListener("click", async () => {
    if (!confirm("Delete the saved map tiles from this PC?\n\nThey are downloaded again when you next look at those places.")) return;
    await post("/api/tiles/clear", {}); refreshTileInfo();
  });
}

// Shows how many map tiles are cached on this PC and their size (/api/tiles/stats).
async function refreshTileInfo() {
  let s; try { s = await api("/api/tiles/stats"); } catch { return; }
  $("tileInfo").hidden = !s.tiles; $("tileCount").textContent = `${s.tiles} (${(s.bytes / 1048576).toFixed(1)} MB)`;
}

// Recent movement trails per node; only loaded while the Trails box is checked.
let mapTrailData = {};
// Loads seven days of movement trails (/api/mesh/trails) when the Trails box is checked.
async function loadTrails() { if ($("mapTrails").checked) { try { mapTrailData = await api("/api/mesh/trails?days=7"); } catch {} } }

// Poll refresh: when the node list is fresh (or force), reloads links and redraws. Trails, position and tile info are reloaded only on force.
async function refreshMap(force) { const r = await fetchNodes(force); if (r === "fresh" || force) { if (force) await loadTrails(); await loadLinks(); drawMapPage(); renderLinkCard(); if (force) { refreshPos(); refreshTileInfo(); } } }

function paintMap() {        // just the map and its summary line (this is what redraws while the user drags)
  const maxAge = $("mapAge").value === "all" ? Infinity : +$("mapAge").value, stored = $("mapStored").checked;
  const pool = allNodes.filter(n => (stored || !n.stored) && (n.us || (n.age_s != null ? n.age_s <= maxAge : maxAge === Infinity)));
  const LL = mapLinkLines(), r = drawNodeMap($("mapBig"), pool, { W: 900, H: 560, sel: mapSel, tiles: $("mapTiles").checked, labels: $("mapLabels").checked, fitAll: $("mapAll").checked,
    onPick: n => { mapSel = n.id; drawMapPage(); }, redraw: paintMap, trails: $("mapTrails").checked ? mapTrailData : null, links: LL, focus: LL && LL.length ? [LL[0].from, ...LL.map(l => l.to)] : null, empty: "No node with a captured position matches these filters." });
  const withPos = pool.filter(n => n.lat != null);
  $("mapSummary").textContent = `${r.shown} shown · ${withPos.filter(n => !n.stored).length} on the radio now · ${withPos.filter(n => n.stored).length} remembered · ${withPos.filter(n => n.pos_source === "packet").length} positions came from broadcasts this bridge heard` + (r.outside ? (mapViews.has("mapBig") ? ` · ${r.outside} not in view (↺ resets the view)` : ` · ${r.outside} far away not in view (tick "Fit every node")`) : "");
}

// Redraws the map, the selected-node box and the list of nodes that have no position (first 40).
function drawMapPage() {
  paintMap();
  const stored = $("mapStored").checked;

  const box = $("mapPicked"), n = mapSel && allNodes.find(v => v.id === mapSel);
  box.hidden = !n; box.replaceChildren();
  if (n) {
    const us = usNode(), km = us && us.lat != null && n.lat != null && !n.us ? distKm(us, n) : null;
    box.append(el("h3", null, "Selected node"), el("b", null, nodeName(n)),
      el("p", "hint", [n.id, hopsText(n), n.us ? null : "heard " + agoStr(n.age_s), km != null ? fmtKm(km) + " from us" : null, n.lat != null ? (n.pos_source === "packet" ? "position from a broadcast we heard" : "position from the radio") : "no position"].filter(Boolean).join(" · ")));
    const acts = el("div"); acts.style.cssText = "display:flex;flex-wrap:wrap;gap:8px";
    for (const [label, fn] of [["Open node", () => go("nodes", n.id)], ...(n.us ? [] : [["Trace route", () => go("traceroute", n.id)]]), ["Clear selection", () => { mapSel = null; drawMapPage(); }]]) { const b = el("button", null, label); b.addEventListener("click", fn); acts.append(b); }
    box.append(acts);
  }
  const np = allNodes.filter(v => v.lat == null && (stored || !v.stored)), list = $("mapNoPos"); list.replaceChildren();
  $("mapNoPosCount").textContent = np.length ? `${np.length} node${np.length === 1 ? "" : "s"}` : "";
  if (!np.length) list.append(el("div", "hint", "Every node we know has a position."));
  for (const v of np.slice(0, 40)) { const row = el("div", "irow"); row.append(el("span", null, nodeName(v)), el("small", null, v.us ? "us" : agoStr(v.age_s))); row.addEventListener("click", () => go("nodes", v.id)); list.append(row); }
  if (np.length > 40) list.append(el("div", "hint", `…and ${np.length - 40} more (see the Nodes page).`));
}

// ---- setting the radio's position: pick a spot on an OpenStreetMap view, then write it to the radio ----------------------------
// State of the position picker map: centre (0..1 world coordinates), tile zoom level, marker, drag in progress, current view and a pending animation frame.
const pick = { cx: 0.5, cy: 0.5, z: 4, mark: null, drag: null, view: null, raf: 0 };
// Inverse of merc: world x, y (0..1) to [latitude, longitude].
const unmerc = (x, y) => [Math.atan(Math.sinh(Math.PI * (1 - 2 * y))) * 180 / Math.PI, x * 360 - 180];
// Shows a message under the position picker, in error style when bad is set.
const posNote = (msg, bad) => { const n = $("posNote"); n.textContent = msg || ""; n.className = "sendnote" + (bad ? " err" : ""); };

// Shows whether the radio has a position, and whether it is a fixed one you set (/api/radio/position).
async function refreshPos() {
  let d; try { d = await api("/api/radio/position"); } catch { return; }
  $("posNow").textContent = !d.connected ? "The radio isn't connected." : d.lat != null
    ? `The radio is at ${d.lat.toFixed(5)}, ${d.lon.toFixed(5)}${d.fixed ? " (a fixed position you set)" : " (its own or GPS position)"}.`
    : d.fixed ? "A fixed position is set, but the radio hasn't reported it back yet." : "The radio has no position, so it shares none.";
  $("posClear").hidden = !d.fixed;
}

// Wires the position picker: open and cancel, zoom, typed coordinates, drag to pan, click to place the marker, wheel zoom, and removing a fixed position.
function initPos() {
  $("posOpen").addEventListener("click", openPicker);
  $("posCancel").addEventListener("click", () => { $("posPicker").hidden = true; });
  $("posZoomIn").addEventListener("click", () => zoomPick(1)); $("posZoomOut").addEventListener("click", () => zoomPick(-1));
  for (const id of ["posLat", "posLon"]) $(id).addEventListener("change", () => {
    const la = parseFloat($("posLat").value), lo = parseFloat($("posLon").value);
    if (Math.abs(la) <= 90 && Math.abs(lo) <= 180) { pick.mark = [la, lo]; [pick.cx, pick.cy] = merc(la, lo); drawPick(); }
  });
  $("posSet").addEventListener("click", setRadioPosition);
  $("posClear").addEventListener("click", async () => {
    if (!confirm("Remove the fixed position from the radio?\n\nIt will report no position unless it has a GPS.")) return;
    const { ok, data } = await post("/api/radio/position", { clear: true });
    posNote(ok ? "Fixed position removed." : data.error || "Could not remove it.", !ok);
    // Give the radio a few seconds to report the change before re-reading its position and the node list.
    refreshPos(); setTimeout(() => { refreshPos(); fetchNodes(true).then(drawMapPage); }, 3000);
  });
  const svg = $("pickMap");
  svg.addEventListener("dragstart", e => e.preventDefault());
  const toSvg = e => { const r = svg.getBoundingClientRect(), k = 900 / r.width; return [(e.clientX - r.left) * k, (e.clientY - r.top) * k, k]; };
  svg.addEventListener("pointerdown", e => { try { svg.setPointerCapture(e.pointerId); } catch {} pick.drag = { x: e.clientX, y: e.clientY, sx: e.clientX, sy: e.clientY, moved: false }; });
  svg.addEventListener("pointermove", e => {
    const d = pick.drag; if (!d) return;
    if (!d.moved && Math.hypot(e.clientX - d.sx, e.clientY - d.sy) < 4) return;
    d.moved = true; const k = toSvg(e)[2], S = 256 * 2 ** pick.z;
    pick.cx -= (e.clientX - d.x) * k / S; pick.cy = Math.min(0.999, Math.max(0.001, pick.cy - (e.clientY - d.y) * k / S)); d.x = e.clientX; d.y = e.clientY; schedulePick();
  });
  svg.addEventListener("pointerup", e => {
    const d = pick.drag; pick.drag = null;
    if (!d || d.moved || !pick.view) return;
    const [px, py] = toSvg(e), v = pick.view;
    setMark(...unmerc(v.x0 + px / v.S, v.y0 + py / v.S));
  });
  svg.addEventListener("pointercancel", () => { pick.drag = null; });
  svg.addEventListener("wheel", e => { e.preventDefault(); const [px, py] = toSvg(e); zoomPick(-Math.max(-300, Math.min(300, e.deltaY)) * 0.004, px, py); }, { passive: false });
}

// Places the marker and fills the latitude and longitude boxes.
function setMark(lat, lon) {
  pick.mark = [lat, lon]; $("posLat").value = lat.toFixed(6); $("posLon").value = lon.toFixed(6); posNote(""); drawPick();
}
function zoomPick(d, px = 450, py = 260) {       // zoom, keeping the point under (px, py) where it is (the map is 900 x 520)
  const v = pick.view, z2 = Math.max(3, Math.min(18, pick.z + d));
  if (v) { const S2 = 256 * 2 ** z2, wx = v.x0 + px / v.S, wy = v.y0 + py / v.S; pick.cx = wx - px / S2 + 450 / S2; pick.cy = clampCy(wy - py / S2 + 260 / S2); }
  pick.z = z2; schedulePick();
}
// Coalesces redraws into one per animation frame.
function schedulePick() { if (!pick.raf) pick.raf = requestAnimationFrame(() => { pick.raf = 0; drawPick(); }); }

// Opens the picker centred on the radio's own position if it has one, otherwise on the middle of the known nodes, otherwise on a wide default view.
async function openPicker() {
  $("posPicker").hidden = false; posNote("");
  let here = null; try { const d = await api("/api/radio/position"); if (d.lat != null) here = [d.lat, d.lon]; } catch {}
  const known = allNodes.filter(n => n.lat != null).map(n => merc(n.lat, n.lon));
  if (here) { [pick.cx, pick.cy] = merc(...here); pick.z = 13; pick.mark = here; $("posLat").value = here[0].toFixed(6); $("posLon").value = here[1].toFixed(6); }
  else if (known.length) {                       // centre on the middle of the nodes we know
    pick.cx = known.map(p => p[0]).sort((a, b) => a - b)[known.length >> 1]; pick.cy = known.map(p => p[1]).sort((a, b) => a - b)[known.length >> 1]; pick.z = 11; pick.mark = null;
  } else { [pick.cx, pick.cy] = merc(39.8, -98.6); pick.z = 4; pick.mark = null; }
  drawPick(); $("posPicker").scrollIntoView({ block: "nearest", behavior: "smooth" });
}

// Draws the picker map: tiles, the known nodes for context, and the chosen marker.
function drawPick() {
  const svg = $("pickMap"), W = 900, H = 520, mk = svgMaker(svg), S = 256 * 2 ** pick.z; mapBase(svg, W, H);
  const v = pick.view = { S, x0: pick.cx - W / (2 * S), y0: pick.cy - H / (2 * S), W, H, cy: pick.cy, P: ([x, y]) => [(x - (pick.cx - W / (2 * S))) * S, (y - (pick.cy - H / (2 * S))) * S] };
  addTiles(svg, v);
  for (const n of allNodes) if (n.lat != null) {       // context: where the known nodes are
    const [px, py] = v.P(merc(n.lat, n.lon)); if (px < -10 || py < -10 || px > W + 10 || py > H + 10) continue;
    mk("circle", { class: `pt ${hopsCls(n)}${n.stored ? " stored" : ""}`, cx: px, cy: py, r: n.us ? 7 : 4 }).append(Object.assign(document.createElementNS("http://www.w3.org/2000/svg", "title"), { textContent: nodeName(n) }));
  }
  if (pick.mark) {
    const [px, py] = v.P(merc(...pick.mark));
    mk("circle", { class: "pt tg", cx: px, cy: py, r: 9 }); mk("line", { class: "scale", x1: px - 16, y1: py, x2: px + 16, y2: py }); mk("line", { class: "scale", x1: px, y1: py - 16, x2: px, y2: py + 16 });
  }
  addScale(mk, v);
}

// Validates the typed coordinates and, after confirmation, writes them to the radio as a fixed position (POST /api/radio/position). The confirmation warns that the radio will share it with the mesh.
async function setRadioPosition() {
  const lat = parseFloat($("posLat").value), lon = parseFloat($("posLon").value), altRaw = $("posAlt").value.trim(), alt = altRaw === "" ? 0 : Number(altRaw);
  if (!(Math.abs(lat) <= 90) || !(Math.abs(lon) <= 180)) { posNote("Click the map, or type a valid latitude and longitude.", true); return; }
  if (!Number.isFinite(alt)) { posNote("Altitude must be a number of metres (or blank).", true); return; }
  if (!confirm(`Write this fixed position to the radio?\n\n${lat.toFixed(6)}, ${lon.toFixed(6)}${alt ? `, ${alt} m` : ""}\n\nThe radio will then share it with the mesh on its normal position schedule, so anyone in range can see where it is. It stays until you remove it.`)) return;
  const btn = $("posSet"); btn.disabled = true; posNote("Sending to the radio…");
  const { ok, data } = await post("/api/radio/position", { lat, lon, alt });
  btn.disabled = false;
  if (!ok) { posNote(data.error || "Could not set the position.", true); return; }
  posNote("Done. The radio has the new position; it may take a moment to show up as our node on the map.");
  // Same delay as above: the node list shows the new position only after the radio reports it.
  refreshPos(); setTimeout(() => { refreshPos(); fetchNodes(true).then(drawMapPage); }, 3000);
}
