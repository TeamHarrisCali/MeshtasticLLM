// updates: the Updates card on the Settings page (/api/update, /api/update/check, /api/update/setting, /api/update/apply) and the
// "Update available" pill in the header. Admin only: for anyone else /api/update answers 403 and the card and pill stay hidden.
// Release notes and every other word that came from GitHub are shown with textContent only; the release link is used only if it points into this project's releases.
let updReady = false, updTimer = null, updNoticeTimer = null, updData = null;
const UPD_KINDS = { git: "a git checkout", docker: "Docker", packaged: "the downloadable program", other: "a copy of the files" };
// The one link shape the page follows: this project's release page for a plain vX.Y.Z tag (the server builds it from the validated tag; anything else is ignored).
const UPD_LINK = /^https:\/\/github\.com\/TeamHarrisCali\/MeshtasticLLM\/releases\/tag\/v(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})$/;

// One-time wiring of the Updates card.
function initUpdates() {
  if (updReady) return; updReady = true;
  $("updAuto").addEventListener("change", async e => {
    const { ok, data } = await post("/api/update/setting", { enabled: e.target.checked });
    if (!ok) { e.target.checked = !e.target.checked; $("updNote").textContent = data.error || "Could not change that."; }
    else $("updNote").textContent = e.target.checked ? "The bridge will look for a new release about once a day." : "The bridge will not contact GitHub by itself.";
  });
  $("updCheck").addEventListener("click", async () => {
    $("updCheck").disabled = true; $("updNote").textContent = "Asking GitHub…";
    const { ok, data } = await post("/api/update/check", {});
    $("updCheck").disabled = false;
    if (ok && data.version) { drawUpdates(data); $("updNote").textContent = data.result.message; } else $("updNote").textContent = (data && data.error) || "The check failed.";
    pollUpdateNotice();
  });
  $("updApply").addEventListener("click", async () => {
    const d = updData; if (!d || !d.latest) return;
    const tag = d.latest.tag;
    const venv = d.virtualenv ? "" : "\nThis Python is not a virtual environment: if the new version needs new packages, the update stops before changing anything and tells you to install them yourself.\n";
    const restart = d.restart_possible ? "4. Restart the bridge by itself (it is unavailable for a few seconds; an answer being written at that moment is lost)." : "4. NOT restart the bridge: you restart it yourself afterwards.";
    if (!confirm(`Update from ${d.version} to ${tag.slice(1)}?\n\nThis will:\n1. Make a backup of the database.\n2. Pull ${tag} from GitHub (fast-forward only; it stops if you have uncommitted changes).\n3. Install any new packages it needs, and check the new version starts.\n${restart}\n\nIf step 3 fails the code is put back as it was.${venv}`)) return;
    $("updApply").disabled = true; $("updNote").textContent = "";
    const { ok, data } = await post("/api/update/apply", { tag });
    if (!ok) { $("updNote").textContent = data.error || "The update could not start."; refreshUpdates(true); return; }
    watchUpdate(tag.slice(1));
  });
  $("updPill").addEventListener("click", () => go("settings"));
}

// Follows a running update: reads the progress every 1.5 s. When the bridge says it is restarting, or stops answering (it is being replaced), it waits for the new version.
function watchUpdate(version) {
  clearInterval(updTimer);
  updTimer = setInterval(async () => {
    let d; try { d = await api("/api/update"); } catch { clearInterval(updTimer); waitForRestart(version); return; }
    if (d.version === version) { clearInterval(updTimer); location.reload(); return; }      // already the new program
    drawUpdates(d);
    const p = d.progress || {};
    if (p.phase === "updating") $("updNote").textContent = p.step || "Working…";
    else if (p.phase === "restarting") { clearInterval(updTimer); waitForRestart(version); }
    else { clearInterval(updTimer); $("updNote").textContent = p.message || ""; }
  }, 1500);
}

// After "restarting": polls /api/status until the new version answers, then reloads the page (a login page appears by itself if the bridge has a login).
function waitForRestart(version) {
  $("updNote").textContent = "Restarting the bridge…"; $("updProgress").textContent = "";
  const t0 = Date.now();
  updTimer = setInterval(async () => {
    if (Date.now() - t0 > 180000) { clearInterval(updTimer); $("updNote").textContent = "The bridge has not answered for 3 minutes. Check its log, or reload this page."; return; }
    try { const s = await api("/api/status"); if (s.version === version) { clearInterval(updTimer); location.reload(); } } catch {}
  }, 2000);
}

// Draws the card from a /api/update reply.
function drawUpdates(d) {
  updData = d; $("updCard").hidden = false;
  $("updInstalled").textContent = `Version ${d.version}, installed as ${UPD_KINDS[d.install.kind] || "an unknown kind of install"}.`;
  $("updAuto").checked = d.check_enabled; $("updAuto").disabled = d.demo;
  const latest = $("updLatest"); latest.replaceChildren();
  if (d.latest) {
    latest.append(el("b", null, d.latest.tag), el("span", null, d.update_available ? " is newer than this version." : " is the newest release; this version is up to date."));
    if (d.checked) latest.append(el("small", null, ` Checked ${fmtTime(d.checked)}.`));
    if (typeof d.latest.url === "string" && UPD_LINK.test(d.latest.url)) {
      const a = el("a", "updlink", " Release page"); a.href = d.latest.url; a.target = "_blank"; a.rel = "noopener noreferrer"; latest.append(a);
    }
  } else latest.append(el("span", "hint", d.checked ? "GitHub has no release to offer." : "Not checked yet."));
  $("updNotes").hidden = !(d.latest && d.latest.notes && d.update_available); $("updNotes").textContent = d.latest && d.update_available ? d.latest.notes : "";
  $("updErr").hidden = !d.last_error; $("updErr").textContent = d.last_error || "";
  const how = $("updHow"); how.replaceChildren();
  if ((d.update_available || d.stuck) && !d.can_update) {
    how.append(el("p", null, d.reason || "This installation cannot update itself."));
    if (d.install.steps.length) how.append(el("pre", null, d.install.steps.join("\n")));
  } else if (d.restart_pending && !(d.progress && d.progress.phase === "restarting")) how.append(el("p", null, "Already updated: restart the bridge to run the new version."));
  else if (d.update_available && !d.restart_possible) how.append(el("p", "hint", "After the update you restart the bridge yourself. " + (d.restart_note || "")));
  how.hidden = !how.childNodes.length;
  const running = d.progress && (d.progress.phase === "updating" || d.progress.phase === "restarting");
  $("updApply").hidden = !d.can_apply; $("updApply").disabled = !!running;
  if (d.can_apply) $("updApply").textContent = "Update now to " + d.latest.tag;
  $("updProgress").textContent = d.progress && d.progress.phase !== "idle" ? (d.progress.message || d.progress.step || "") : "";
  $("updProgress").className = "hint" + (d.progress && d.progress.ok === false ? " err" : "");
  const ap = $("updApplied"); ap.replaceChildren();
  const a = d.applied;
  if (a && a.result && a.from_sha) {
    const what = { done: "Last update", rolled_back: "The last update was rolled back", failed: "The last update failed", running: "The last update did not finish" }[a.result];
    ap.append(el("p", null, `${what}: ${a.from_version || "?"} to ${a.to_version || "?"}${a.ts ? ", " + fmtTime(a.ts) : ""}.`));
    if (a.result === "done" || a.result === "failed" || a.result === "running") {
      ap.append(el("p", "hint", "To go back: stop the bridge, run this in the project folder, and start it again. If the new version changed the data in a way the old one cannot read, restore the backup first (Backups above" + (a.backup ? ", " + a.backup : "") + ")."));
      ap.append(el("pre", null, "git checkout " + a.from_sha));
    }
  }
  ap.hidden = !ap.childNodes.length;
}

// Refreshes the card; hides it for anyone who may not see it (a viewer gets 403).
async function refreshUpdates(force) {
  let d; try { d = await api("/api/update"); } catch { $("updCard").hidden = true; return; }
  drawUpdates(d); updateNotice(d);
}

// The header pill: shown to an admin only, from the saved check (this makes no request to GitHub).
function updateNotice(d) {
  const p = $("updPill"); p.hidden = !d.update_available || d.restart_pending;
  if (!p.hidden) { p.textContent = "Update available: " + d.latest.tag; p.title = "A newer release is out. Open Settings to see what it is and how to update."; }
}
async function pollUpdateNotice() { try { updateNotice(await api("/api/update")); } catch {} }
// Called once from main.js for an admin (or when there is no login): look at start-up and every 10 minutes.
function startUpdateNotice() { pollUpdateNotice(); updNoticeTimer = setInterval(pollUpdateNotice, 600000); }
