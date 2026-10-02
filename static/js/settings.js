// settings: part of the dashboard script (loaded in order by index.html; all files share one global scope)
// ---- settings: display units, notifications, backups, storage, start at login ------------------------------------------------------------------
let settingsReady = false, settingsAt = 0, backupsSig = "";
const NT_BOXES = { ntChannel: "channel", ntDm: "dm", ntAlerts: "alerts", ntSound: "sound", ntDesktop: "desktop", ntText: "text" };

function initSettings() {
  if (settingsReady) return; settingsReady = true;
  for (const b of $("setTemp").children) b.addEventListener("click", async () => {
    const { ok } = await post("/api/settings/temp_unit", { unit: b.dataset.v }); if (ok) { setTempUnit(b.dataset.v); homeTick.home = homeTick.sensors = 0; showUnits(); }
  });
  for (const b of $("setDist").children) b.addEventListener("click", async () => {
    const { ok } = await post("/api/settings/dist_unit", { unit: b.dataset.v }); if (ok) { setDistUnit(b.dataset.v); showUnits(); }
  });
  for (const [id, key] of Object.entries(NT_BOXES)) $(id).addEventListener("change", async e => {
    if (key === "desktop" && e.target.checked) {
      if (!("Notification" in window)) { e.target.checked = false; $("ntNote").textContent = "This browser can't show desktop notifications."; return; }
      const r = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
      if (r !== "granted") { e.target.checked = false; $("ntNote").textContent = "The browser didn't allow notifications for this page. You can change that in the site settings next to the address."; return; }
    }
    ntSet(key, e.target.checked); showBadges(); showNtNote();
  });
  $("bkAuto").addEventListener("change", async e => { const { ok, data } = await post("/api/backups/auto", { enabled: e.target.checked }); if (!ok) { e.target.checked = !e.target.checked; $("bkNote").textContent = data.error || "Could not change that."; } });
  $("bkNow").addEventListener("click", async () => {
    $("bkNow").disabled = true; const { ok, data } = await post("/api/backups/create", {}); $("bkNow").disabled = false;
    $("bkNote").textContent = ok ? "Backup made: " + data.name : (data.error || "The backup failed."); refreshBackups(true);
  });
  $("bkUpload").addEventListener("change", async e => {
    const f = e.target.files[0]; e.target.value = ""; if (!f) return;
    if (!confirm(`Restore everything from "${f.name}"?\n\nNothing changes now. The file is checked and set aside, and it replaces the current data the next time the bridge starts. The current data is kept as a backup first.`)) return;
    $("bkNote").textContent = "Checking the file…";
    let r, data = {}; try { r = await fetch("/api/backups/restore", { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: f }); data = await r.json(); } catch { r = { ok: false }; }
    $("bkNote").textContent = r.ok ? "The file looks good and is waiting for the next start." : (data.error || "That file could not be used."); refreshBackups(true);
  });
  $("stTilesClear").addEventListener("click", async () => {
    if (!confirm("Delete the saved map pictures? They are downloaded again when you look at the map with its background on.")) return;
    const { ok, data } = await post("/api/tiles/clear", {}); $("stNote").textContent = ok ? `Deleted ${data.deleted} saved tiles.` : (data.error || "Could not clear."); refreshSettings(true);
  });
  $("stRetentionSave").addEventListener("click", async () => {
    const days = parseInt($("stRetention").value, 10), { ok, data } = await post("/api/telemetry/retention", { days });
    $("stNote").textContent = ok ? (data.retention_days ? `Telemetry is now kept for ${data.retention_days} days.` : "Telemetry is now kept forever.") : (data.error || "Could not save that.");
  });
}

function showUnits() {
  for (const b of $("setTemp").children) b.setAttribute("aria-pressed", String(b.dataset.v === tempUnit));
  for (const b of $("setDist").children) b.setAttribute("aria-pressed", String(b.dataset.v === distUnit));
}

function showNtNote() {
  const perm = "Notification" in window ? Notification.permission : "unsupported";
  $("ntNote").textContent = ntGet("desktop") ? (perm === "granted" ? "Desktop notifications are on. They appear only while this tab is in the background." : "Desktop notifications were switched on but the browser isn't allowing them.") : "";
}

async function refreshBackups(force) {
  let d; try { d = await api("/api/backups"); } catch { return; }
  $("bkAuto").checked = d.auto;
  const st = $("bkStaged");
  if (d.staged) {
    st.hidden = false; st.replaceChildren();
    if (d.staged.error) st.append(el("p", null, "A restore file is waiting but can't be used: " + d.staged.error));
    else st.append(el("p", null, `A restore is waiting: a copy holding ${d.staged.requests} AI-log entries (${fmtBytes(d.staged.size)}). It replaces the current data the next time the bridge starts, so stop it and start it again (stop_bridge, then start_bridge). The current data is kept as a backup first.`));
    const c = el("button", null, "Cancel the restore"); c.addEventListener("click", async () => { await post("/api/backups/restore/cancel", {}); refreshBackups(true); }); st.append(c);
  } else st.hidden = true;
  const sig = JSON.stringify(d.items) + JSON.stringify(!!d.staged);
  if (sig === backupsSig && !force) return; backupsSig = sig;
  const box = $("bkList"); box.replaceChildren();
  if (!d.items.length) box.append(el("p", "hint", "No backups yet."));
  for (const b of d.items) {
    const row = el("div", "setrow bk"), what = el("span"), tools = el("span", "bkbtns");
    what.append(el("b", null, b.kind === "auto" ? "Automatic" : b.kind === "manual" ? "Manual" : "Kept before a restore"), el("small", null, ` ${fmtTime(b.ts)} · ${fmtBytes(b.size)}`));
    const dl = el("a", "btn", "Download"); dl.href = "/api/backups/download?name=" + encodeURIComponent(b.name);
    const rs = el("button", null, "Restore"), del = el("button", null, "Delete");
    rs.addEventListener("click", async () => {
      if (!confirm("Restore this backup? Nothing changes now: it replaces the current data the next time the bridge starts, and the current data is kept as a backup first.")) return;
      const { ok, data } = await post("/api/backups/restore/existing", { name: b.name }); $("bkNote").textContent = ok ? "Set aside. It is applied the next time the bridge starts." : (data.error || "Could not use that backup."); refreshBackups(true);
    });
    del.addEventListener("click", async () => { if (!confirm("Delete this backup file?")) return; await post("/api/backups/delete", { name: b.name }); refreshBackups(true); });
    tools.append(dl, rs, del); row.append(what, tools); box.append(row);
  }
}

async function refreshSettings(force) {
  if (!force && Date.now() - settingsAt < 15000) return; settingsAt = Date.now();
  showUnits();
  for (const [id, key] of Object.entries(NT_BOXES)) $(id).checked = ntGet(key);
  showNtNote();
  refreshBackups(force);
  try { const t = await api("/api/tiles/stats"); $("stTiles").textContent = `${t.tiles} tiles · ${fmtBytes(t.bytes)} of ${fmtBytes(t.max_bytes)} allowed`; } catch {}
  try { const t = await api("/api/telemetry?limit=1"); if (document.activeElement !== $("stRetention")) $("stRetention").value = t.retention_days; } catch {}
  if (force) {
    try {
      const d = await api("/api/diagnostics"), a = d.checks.find(c => c.id === "autostart");
      $("autoState").textContent = a ? (a.status === "ok" ? "Set up: the bridge starts when you log in to this computer." : "Not set up: the bridge only runs when you start it.") : "This computer's start-at-login state can't be read from here.";
    } catch { $("autoState").textContent = ""; }
  }
}
