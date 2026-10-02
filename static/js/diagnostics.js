// diagnostics: part of the dashboard script (loaded in order by index.html; all files share one global scope)
// ---- diagnostics: a health check and the bridge's own log ---------------------------------------------------------------------------------
let diagReady = false, diagAt = 0, logAt = 0, logText = "";

function initDiagnostics() {
  if (diagReady) return; diagReady = true;
  for (const id of ["logWhich", "logLines"]) $(id).addEventListener("change", () => refreshLog(true));
  $("logFollow").addEventListener("change", () => { if ($("logFollow").checked) { const b = $("logBox"); b.scrollTop = b.scrollHeight; } });
  $("logCopy").addEventListener("click", async () => { try { await navigator.clipboard.writeText(logText); $("logNote").textContent = "Copied."; } catch { $("logNote").textContent = "Your browser wouldn't allow copying; select the text and copy it."; } });
}

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

async function refreshLog(force) {
  if (!force && Date.now() - logAt < 4000) return; logAt = Date.now();
  let d; try { d = await api(`/api/logs?which=${$("logWhich").value}&lines=${$("logLines").value}`); } catch { return; }
  const box = $("logBox"), text = d.lines.join("\n");
  logText = text;
  $("logNote").textContent = !d.exists ? "This log doesn't exist yet. It is created when the bridge is started with start_bridge." : `${d.file} · ${fmtBytes(d.size)}${d.modified ? " · last written " + fmtTime(d.modified) : ""}. Showing the last ${d.lines.length} line${d.lines.length === 1 ? "" : "s"}.`;
  if (box.textContent === text) return;
  const stick = $("logFollow").checked || box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.textContent = text || (d.exists ? "(empty)" : "");
  if (stick) box.scrollTop = box.scrollHeight;
}
