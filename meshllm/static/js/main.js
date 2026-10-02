// main: start-up wiring, loaded last. Connects the sidebar and unit buttons, restores saved settings, runs the first route, and starts
// the 3 s poll that refreshes the header and whichever page is open, plus a 30 s refresh of the node list for the chat pages.
for (const b of document.querySelectorAll("#nav button")) b.addEventListener("click", () => go(b.dataset.view));
$("distBtn").addEventListener("click", async () => {
  const next = distUnit === "mi" ? "km" : "mi", { ok } = await post("/api/settings/dist_unit", { unit: next });
  if (ok) { setDistUnit(next); route(); }
});
$("unitBtn").addEventListener("click", async () => {
  const next = tempUnit === "F" ? "C" : "F", { ok } = await post("/api/settings/temp_unit", { unit: next });
  if (ok) { setTempUnit(next); homeTick.home = homeTick.sensors = 0; route(); }
});
// Restore the Home sensor window from localStorage; anything other than 15 min, 1 h, 6 h or 24 h falls back to 1 h.
sensorHours = Number(lsGet("sensorHours")) || 1; if (![0.25, 1, 6, 24].includes(sensorHours)) sensorHours = 1;
$("sensorWindow").value = String(sensorHours);
$("sensorWindow").addEventListener("change", e => { sensorHours = Number(e.target.value); lsSet("sensorHours", sensorHours); homeTick.sensors = 0; refreshHome(false); });
for (const b of document.querySelectorAll("[data-go]")) b.addEventListener("click", () => go(b.dataset.go));
window.addEventListener("hashchange", route);

// Initial load: set up search and notifications, fill the header, load nodes, show the page named in the address and check for unread items.
initSearch(); initNotify(); refreshTop(); loadNodes(); route(); pollUnread();
// Poll tick (every 3 s): refresh the header, then only the page that is open. Pages with their own interval limits (for example Home, Data, Settings) skip the fetch themselves when it is too soon.
setInterval(() => {
  refreshTop();
  if (view === "home") refreshHome(false);
  else if (view === "nodes") refreshNodes(false);
  else if (view === "map") refreshMap(false);
  else if (view === "activity") refreshActivity(false);
  else if (view === "channel") refreshChannel(false);
  else if (view === "data") refreshData(false);
  else if (view === "trends") refreshTrends(false);
  else if (view === "ai") refreshAi(false);
  else if (view === "audit") { refreshRows(false); refreshQueue(); }
  else if (view === "chat" || view === "dm") { refreshConvs(); refreshThread(false); }
  else if (view === "model") refreshModels(false);
  else if (view === "telemetry") refreshTelemetry(false);
  else if (view === "traceroute") refreshTrace(false);
  else if (view === "access") refreshAccess(false);
  else if (view === "coverage") refreshCoverage(false);
  else if (view === "diagnostics") refreshDiagnostics(false);
  else if (view === "settings") refreshSettings(false);
  pollUnread();
}, 3000);
// The known-nodes list used by the chat pages' pickers is refreshed less often (every 30 s).
setInterval(() => { if (view === "chat" || view === "dm") loadNodes(); }, 30000);
