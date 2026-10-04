// diagnostics: the Diagnostics page. A health check list from /api/diagnostics and a viewer for the bridge's log files from /api/logs
// (choice of log and line count, follow, copy). Uses fmtBytes from data.js and fmtTime from core.js.
// ---- diagnostics: a health check and the bridge's own log ---------------------------------------------------------------------------------
// diagAt and logAt throttle the two refreshes; logText is the last log text, kept for the Copy button.
let diagReady = false, diagAt = 0, logAt = 0, logText = "";

// One-time setup: log selector, follow and copy controls.
function initDiagnostics() {
  if (diagReady) return; diagReady = true;
  for (const id of ["logWhich", "logLines"]) $(id).addEventListener("change", () => refreshLog(true));
  $("logFollow").addEventListener("change", () => { if ($("logFollow").checked) { const b = $("logBox"); b.scrollTop = b.scrollHeight; } });
  $("logCopy").addEventListener("click", async () => { try { await navigator.clipboard.writeText(logText); $("logNote").textContent = "Copied."; } catch { $("logNote").textContent = "Your browser wouldn't allow copying; select the text and copy it."; } });
}

// Reloads the health checks at most every 10 s unless forced; in between, only the log is refreshed (which has its own 4 s limit).
async function refreshDiagnostics(force) {
  if (!force && Date.now() - diagAt < 10000) { refreshLog(false); return; }
  diagAt = Date.now();
  let d; try { d = await api("/api/diagnostics"); } catch { return; }
  const box = $("diagList"); box.replaceChildren();
  $("diagWhen").textContent = d.overall === "ok" ? "· everything looks fine" : d.overall === "warn" ? "· something needs a look" : "· something is wrong";
  for (const c of d.checks) {
    const row = el("div", "diag " + c.status), head = el("div", "dh"), dot = el("span", "dot " + (c.status === "info" ? "" : c.status));
    head.append(dot, el("b", null, c.title), el("span", "dd", c.detail)); row.append(head);
    if (c.hint) row.append(el("div", "dhint", c.hint));
    box.append(row);
  }
  refreshLog(true);
}

// Fetches the last N lines of the chosen log (throttled to 4 s unless forced) and updates the box only if the text changed.
async function refreshLog(force) {
  if (!force && Date.now() - logAt < 4000) return; logAt = Date.now();
  let d; try { d = await api(`/api/logs?which=${$("logWhich").value}&lines=${$("logLines").value}`); } catch { return; }
  const box = $("logBox"), text = d.lines.join("\n");
  logText = text;
  $("logNote").textContent = !d.exists ? "This log doesn't exist yet. It is created when the bridge is started with scripts/start_bridge." : `${d.file} · ${fmtBytes(d.size)}${d.modified ? " · last written " + fmtTime(d.modified) : ""}. Showing the last ${d.lines.length} line${d.lines.length === 1 ? "" : "s"}.`;
  if (box.textContent === text) return;
  // Keep following the end of the log if Follow is on or the reader is already at the bottom; otherwise leave their scroll position alone.
  const stick = $("logFollow").checked || box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  // Log lines are set as plain text, never as HTML.
  box.textContent = text || (d.exists ? "(empty)" : "");
  if (stick) box.scrollTop = box.scrollHeight;
}
