// activity: the Activity page, a filterable feed of what happened on the mesh (AI questions, direct messages, things you sent,
// telemetry, traceroutes, new nodes) from /api/mesh/feed, with kind chips, text search and paging. Uses FEED_ICON and agoStr from mesh-common.js.
// ---- activity page ----------------------------------------------------------------------------------------------------
// Filter chips: each button covers one or more feed event types.
const ACT_CHIPS = [{ key: "ai", label: "AI questions", types: ["ai"] }, { key: "dm", label: "Direct messages", types: ["dm"] }, { key: "you", label: "Sent by you", types: ["you"] },
  { key: "telemetry", label: "Telemetry", types: ["broadcast", "telemetry"] }, { key: "traceroute", label: "Traceroutes", types: ["traceroute"] }, { key: "node", label: "New nodes", types: ["node"] }];
// actOn is the set of enabled chips, actItems the loaded feed, actSeq lets a newer request supersede older replies.
let actReady = false, actOn = new Set(ACT_CHIPS.map(c => c.key)), actItems = [], actExhausted = false, actSig = "", actSeq = 0, actDebounce;
// Feed items fetched per page.
const ACT_PAGE = 60;

// One-time setup: builds the filter chips and wires the search box (debounced 250 ms) and Show more.
function initActivity() {
  if (actReady) return; actReady = true;
  for (const c of ACT_CHIPS) {
    const b = el("button", "chip", `${FEED_ICON[c.key] || "📊"} ${c.label}`); b.dataset.k = c.key; b.setAttribute("aria-pressed", "true");
    b.addEventListener("click", () => { actOn.has(c.key) ? actOn.delete(c.key) : actOn.add(c.key); b.setAttribute("aria-pressed", actOn.has(c.key)); refreshActivity(true); });
    $("actChips").append(b);
  }
  $("actQ").addEventListener("input", () => { clearTimeout(actDebounce); actDebounce = setTimeout(() => refreshActivity(true), 250); });
  $("actMore").addEventListener("click", loadOlderActivity);
}

// Builds the /api/mesh/feed query; the types filter is only sent when some chips are switched off.
function actQuery() {
  const p = new URLSearchParams({ limit: ACT_PAGE });
  if (actOn.size !== ACT_CHIPS.length) p.set("types", ACT_CHIPS.filter(c => actOn.has(c.key)).flatMap(c => c.types).join(","));
  if ($("actQ").value.trim()) p.set("q", $("actQ").value.trim());
  return p;
}

// Reloads the newest page. reset=true replaces everything; otherwise older pages the user already loaded are kept.
async function refreshActivity(reset) {
  const seq = ++actSeq;
  if (!actOn.size) { actItems = []; actExhausted = true; renderActivity(); return; }
  let data; try { data = await api("/api/mesh/feed?" + actQuery()); } catch { return; }
  if (seq !== actSeq) return;
  // a refresh replaces the newest page but keeps any older pages the user already loaded
  actItems = reset || data.length < ACT_PAGE || !actItems.length ? data : data.concat(actItems.filter(i => i.ts < data[data.length - 1].ts));
  if (reset || data.length < ACT_PAGE) actExhausted = data.length < ACT_PAGE;
  renderActivity();
}

// Loads the next page of items older than the last one shown (paged by timestamp).
async function loadOlderActivity() {
  const p = actQuery(); p.set("before", actItems[actItems.length - 1].ts);
  let data; try { data = await api("/api/mesh/feed?" + p); } catch { return; }
  actItems = actItems.concat(data); if (data.length < ACT_PAGE) actExhausted = true;
  renderActivity();
}

// Redraws the feed. AI, direct-message and sent items link to the 'chat' page; other items link to the node page.
function renderActivity() {
  const sig = JSON.stringify(actItems) + actExhausted + actOn.size;
  if (sig === actSig) return; actSig = sig;
  const box = $("actRows"); box.replaceChildren();
  if (!actItems.length) box.append(el("div", "empty", actOn.size ? "Nothing matches yet." : "Pick at least one kind of activity above."));
  for (const f of actItems) {
    const row = el("div", "feedrow"); row.append(el("span", null, FEED_ICON[f.type] || "•"), el("span", null, f.text), el("small", null, `${fmtTime(f.ts)} · ${agoStr(Math.max(0, Date.now() / 1000 - f.ts))}`));
    if (f.node_id) row.addEventListener("click", () => go(f.type === "ai" ? "chat" : ["dm", "you"].includes(f.type) ? "dm" : "nodes", f.node_id));
    box.append(row);
  }
  $("actMore").hidden = actExhausted || actItems.length < ACT_PAGE;
}
