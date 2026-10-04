// nav: page routing and a few shared helpers. Pages are addressed by the URL hash (#/page/arg); route() reads it and
// showView() shows one page and calls that page's init/refresh functions from the other files. Also defines post().
// ---- conversations ------------------------------------------------------------------------
// State: 'view' is the current page, 'selected' the open conversation, *Sig strings the last drawn data (to skip redundant redraws), knownNodes comes from /api/nodes.
let chatScope = "ai";      // the Direct messages page ("dm") and the AI conversations page ("ai") share one layout
let view = "", selected = null, selectedName = null, convSig = "", threadSig = "", knownNodes = [];

// POSTs a JSON body. Returns { ok, data }, where data is the parsed reply or {} if it was not JSON. Does not throw on HTTP errors; callers show data.error.
async function post(path, body) {
  const r = await authFetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  let data = {}; try { data = await r.json(); } catch {}
  return { ok: r.ok, data };
}

// Shows a message under the direct-message send box, styled as an error when isErr is set.
function note(msg, isErr) { const n = $("sendNote"); n.textContent = msg || ""; n.className = "sendnote" + (isErr ? " err" : ""); }

// ---- navigation: a sidebar of pages, each with its own address (#/nodes/!1a2b3c4d) ---------------
// Page id -> title. Ids are the #/id addresses and the data-view values of the sidebar buttons.
const PAGES = { home: "Home", nodes: "Nodes", map: "Map", coverage: "Coverage", channel: "Public channel", dm: "Direct messages", chat: "AI conversations", activity: "Activity",
  telemetry: "Telemetry", traceroute: "Traceroute", radio: "Radio settings", connection: "Connection", trends: "Trends", data: "Data", report: "Report", diagnostics: "Diagnostics", settings: "Settings",
  search: "Search", ai: "AI overview", model: "Model", access: "Access & actions", audit: "AI log", eval: "Evaluation" };
const VIEW_OF = { dm: "chat" };            // pages that are drawn in another page's layout
// The container element for a page (viewHome, viewNodes, ...).
const viewEl = name => $("view" + capital(VIEW_OF[name] || name));
// Upper-cases the first letter.
const capital = s => s[0].toUpperCase() + s.slice(1);

// Navigates to a page by setting the hash (arg is URL-encoded); re-routes directly if the hash would not change.
function go(v, arg) {
  const h = "#/" + v + (arg ? "/" + encodeURIComponent(arg) : "");
  if (location.hash === h) route(); else location.hash = h;
}

// Parses the current hash and shows that page; unknown pages fall back to Home.
function route() {
  const m = /^#\/([a-z]+)(?:\/(.+))?$/.exec(location.hash);
  let arg = null; if (m && m[2]) { try { arg = decodeURIComponent(m[2]); } catch {} }
  showView(m && PAGES[m[1]] ? m[1] : "home", arg);
}

// Shows page v, hides the others, updates the sidebar and title, then calls that page's init and refresh functions with force=true.
// The final else branch is the AI log page ('audit').
function showView(v, arg) {
  const changed = v !== view; view = v;
  for (const name of Object.keys(PAGES)) viewEl(name).hidden = viewEl(name) !== viewEl(v);
  for (const b of document.querySelectorAll("#nav button")) {
    const on = b.dataset.view === v;
    b.setAttribute("aria-current", on ? "page" : "false");
    if (on && changed) b.scrollIntoView({ block: "nearest", inline: "center" });
  }
  $("pageTitle").textContent = PAGES[v]; refreshTitle();
  if (changed) window.scrollTo(0, 0);
  if (v === "chat" || v === "dm") {
    const scope = v === "dm" ? "dm" : "ai";
    // Switching between Direct messages and AI conversations (same layout) clears the selection; re-opening the page clears the cached signatures so it redraws.
    if (scope !== chatScope || !changed) { if (scope !== chatScope) { chatScope = scope; selected = selectedName = null; } convSig = threadSig = ""; }
    applyChatScope(); refreshConvs(); loadNodes(); markRead(v);
    if (arg) selectNode(arg); else refreshThread(true);
  }
  else if (v === "access") { accSig = ""; refreshAccess(true); loadActions(); }
  else if (v === "model") { modelSig = ""; refreshModels(true); }
  else if (v === "telemetry") { if (arg) pendingTelNode = arg; initTelemetry(); loadNodes(); refreshTelemetry(true); }
  else if (v === "traceroute") { initTrace(); loadNodes(); if (arg) $("traceNode").value = arg; refreshTrace(true); }
  else if (v === "home") refreshHome(true);
  else if (v === "nodes") { initNodes(); openNode(arg); refreshNodes(true); }
  else if (v === "map") { initMap(); if (arg) mapSel = arg; refreshMap(true); }
  else if (v === "activity") { initActivity(); refreshActivity(true); }
  else if (v === "channel") { initChannel(); refreshChannel(true); markRead("channel"); }
  else if (v === "coverage") { initCoverage(); loadNodes(); refreshCoverage(true); }
  else if (v === "report") { initReport(); refreshReport(true); }
  else if (v === "diagnostics") { initDiagnostics(); refreshDiagnostics(true); }
  else if (v === "settings") { initSettings(); refreshSettings(true); }
  else if (v === "eval") { refreshEval(true); }
  else if (v === "search") { runSearch(arg || ""); }
  else if (v === "data") refreshData(true);
  else if (v === "trends") { initTrends(); refreshTrends(true); }
  else if (v === "radio") { initRadio(); refreshRadio(false); }
  else if (v === "connection") { initConnection(); refreshConnection(true); }
  else if (v === "ai") refreshAi(true);
  else { refreshRows(false); refreshQueue(); }
}
