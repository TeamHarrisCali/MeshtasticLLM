// activity: part of the dashboard script (loaded in order by index.html; all files share one global scope)
// ---- activity page ----------------------------------------------------------------------------------------------------
const ACT_CHIPS = [{ key: "ai", label: "AI questions", types: ["ai"] }, { key: "dm", label: "Direct messages", types: ["dm"] }, { key: "you", label: "Sent by you", types: ["you"] },
  { key: "telemetry", label: "Telemetry", types: ["broadcast", "telemetry"] }, { key: "traceroute", label: "Traceroutes", types: ["traceroute"] }, { key: "node", label: "New nodes", types: ["node"] }];
let actReady = false, actOn = new Set(ACT_CHIPS.map(c => c.key)), actItems = [], actExhausted = false, actSig = "", actSeq = 0, actDebounce;
const ACT_PAGE = 60;

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

function actQuery() {
  const p = new URLSearchParams({ limit: ACT_PAGE });
  if (actOn.size !== ACT_CHIPS.length) p.set("types", ACT_CHIPS.filter(c => actOn.has(c.key)).flatMap(c => c.types).join(","));
  if ($("actQ").value.trim()) p.set("q", $("actQ").value.trim());
  return p;
}

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

async function loadOlderActivity() {
  const p = actQuery(); p.set("before", actItems[actItems.length - 1].ts);
  let data; try { data = await api("/api/mesh/feed?" + p); } catch { return; }
  actItems = actItems.concat(data); if (data.length < ACT_PAGE) actExhausted = true;
  renderActivity();
}

function renderActivity() {
  const sig = JSON.stringify(actItems) + actExhausted + actOn.size;
  if (sig === actSig) return; actSig = sig;
  const box = $("actRows"); box.replaceChildren();
  if (!actItems.length) box.append(el("div", "empty", actOn.size ? "Nothing matches yet." : "Pick at least one kind of activity above."));
  for (const f of actItems) {
    const row = el("div", "feedrow"); row.append(el("span", null, FEED_ICON[f.type] || "•"), el("span", null, f.text), el("small", null, `${fmtTime(f.ts)} · ${agoStr(Math.max(0, Date.now() / 1000 - f.ts))}`));
    if (f.node_id) row.addEventListener("click", () => go(["ai", "dm", "you"].includes(f.type) ? "chat" : "nodes", f.node_id));
    box.append(row);
  }
  $("actMore").hidden = actExhausted || actItems.length < ACT_PAGE;
}
