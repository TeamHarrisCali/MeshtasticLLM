// radio: the Radio settings page. Reads the radio's config (/api/radio/config, /api/radio/config/pull), keeps edits in the browser,
// and writes only the changed fields (/api/radio/config/save). Also lists, downloads and restores saved backups
// (/api/radio/config/backup, /api/radio/config/restore). Uses post from nav.js and fmtTime from core.js.
// ---- radio settings: pull the radio's config, edit it, push only what changed -------------------------------------------------
// radioData: last config from the server. radioEdits: unsaved changes as {section: {field: value}}, kept between refreshes. radioSection: the open tab.
let radioReady = false, radioData = null, radioEdits = {}, radioSection = null;
// Shows a status message on this page, in error style when bad is set.
const radioNote = (msg, bad) => { const n = $("radioNote"); n.textContent = msg || ""; n.className = "sendnote" + (bad ? " err" : ""); };
// The field definitions of one section.
const radioFields = s => (radioData.sections.find(x => x.name === s) || { fields: [] }).fields;
// True when there are unsaved edits.
const radioDirty = () => Object.values(radioEdits).some(e => Object.keys(e).length);

// One-time setup: pull, discard and save buttons. Pulling reads the radio again and also stores a backup.
function initRadio() {
  if (radioReady) return; radioReady = true;
  $("radioPull").addEventListener("click", async () => {
    if (radioDirty() && !confirm("Discard your unsaved edits and pull the radio's settings again?")) return;
    const b = $("radioPull"); b.disabled = true; radioNote("Reading the radio…");
    const { ok, data } = await post("/api/radio/config/pull", {}); b.disabled = false;
    if (!ok) { radioNote(data.error || "Could not read the radio.", true); return; }
    radioData = data; radioEdits = {}; radioNote(`Pulled from the radio and saved as backup #${data.backup_id}.`); renderRadio();
  });
  $("radioDiscard").addEventListener("click", () => { radioEdits = {}; radioNote(""); renderRadio(); });
  $("radioSave").addEventListener("click", saveRadio);
}

// Loads the radio's config. Skipped while there are unsaved edits unless force is set, which discards them.
async function refreshRadio(force) {
  if (radioDirty() && !force) return;            // never replace what someone is in the middle of editing
  let d; try { d = await api("/api/radio/config"); } catch { return; }
  radioData = d; if (force) radioEdits = {};
  renderRadio();
}

function radioChanges() {       // -> [{section, field, label, old, new, risk}] for the pending edits
  const out = [];
  for (const [s, edits] of Object.entries(radioEdits)) for (const [name, value] of Object.entries(edits)) {
    const f = radioFields(s).find(x => x.name === name); if (f) out.push({ section: s, field: name, label: f.label, old: f.value, new: value, risk: f.risk });
  }
  return out;
}

// Records an edit, or removes it when the value is back to what the radio has.
function setEdit(section, f, value) {
  const same = f.kind === "float" || f.kind === "int" ? Number(value) === f.value : value === f.value;
  radioEdits[section] = radioEdits[section] || {};
  if (same) delete radioEdits[section][f.name]; else radioEdits[section][f.name] = value;
  renderRadioPending(); renderRadioTabs();
}

// Builds the input for one field (checkbox, select, number or text) that records edits as the user types. An empty or non-numeric number drops the edit.
function radioControl(section, f) {
  const cur = (radioEdits[section] || {})[f.name] ?? f.value;
  let c;
  if (f.kind === "bool") { c = el("input"); c.type = "checkbox"; c.checked = !!cur; c.addEventListener("change", () => { setEdit(section, f, c.checked); markRow(c, section, f); }); }
  else if (f.kind === "enum") {
    c = document.createElement("select");
    for (const name of f.choices) { const o = document.createElement("option"); o.value = name; o.textContent = pretty(name); c.append(o); }
    c.value = cur; c.addEventListener("change", () => { setEdit(section, f, c.value); markRow(c, section, f); });
  } else {
    c = el("input"); c.type = f.kind === "string" ? "text" : "number"; c.value = cur ?? "";
    if (f.kind === "int") { c.step = 1; c.min = f.min; c.max = f.max; c.inputMode = "numeric"; } else if (f.kind === "float") c.step = "any"; else c.maxLength = f.max;
    c.addEventListener("input", () => {
      if (f.kind === "string") setEdit(section, f, c.value);
      else if (c.value.trim() === "" || !Number.isFinite(Number(c.value))) { delete (radioEdits[section] || {})[f.name]; renderRadioPending(); renderRadioTabs(); }
      else setEdit(section, f, Number(c.value));
      markRow(c, section, f);
    });
  }
  c.setAttribute("aria-label", f.label);
  return c;
}
// Turns a snake_case name into words with a leading capital.
const pretty = s => s.replace(/_/g, " ").replace(/^./, m => m.toUpperCase());
// Highlights a field row while it has an unsaved edit.
function markRow(ctl, section, f) { const row = ctl.closest(".rfield"); if (row) row.classList.toggle("changed", f.name in (radioEdits[section] || {})); }

// Draws the section tabs, with a count of unsaved edits per section.
function renderRadioTabs() {
  const box = $("radioTabs"); box.replaceChildren();
  for (const s of radioData.sections) {
    const n = Object.keys(radioEdits[s.name] || {}).length;
    const b = el("button", "chip", s.label + (n ? ` • ${n}` : "")); b.setAttribute("aria-pressed", s.name === radioSection);
    b.addEventListener("click", () => { radioSection = s.name; renderRadio(); }); box.append(b);
  }
}

// Lists the pending changes (old -> new, risky ones flagged) and enables Save and Discard.
function renderRadioPending() {
  const ch = radioChanges(), box = $("radioPending"); box.replaceChildren();
  $("radioSave").disabled = !ch.length || !radioData.connected; $("radioDiscard").disabled = !ch.length;
  for (const c of ch) {
    const row = el("div", "irow static"); row.append(el("span", null, `${radioData.sections.find(s => s.name === c.section).label}: ${c.label}: ${pretty(String(c.old))} → ${pretty(String(c.new))}`), el("small", null, c.risk ? "⚠ risky" : ""));
    box.append(row);
  }
}

// Draws the open section's form and the backups list from radioData.
function renderRadio() {
  const d = radioData, warn = $("radioWarn");
  warn.hidden = d.connected && !d.error; warn.textContent = d.error || "The radio isn't connected. Settings can be edited once it is back.";
  $("radioInfo").textContent = d.radio ? d.radio : "";
  $("radioPull").disabled = !d.connected;
  if (!d.sections.length) { $("radioTabs").replaceChildren(); $("radioForm").replaceChildren(el("p", "hint", d.connected ? "The radio hasn't sent its settings yet." : "No settings to show.")); renderRadioPending(); renderBackups(d.backups); return; }
  if (!radioSection || !d.sections.some(s => s.name === radioSection)) radioSection = d.sections[0].name;
  renderRadioTabs(); renderRadioPending();
  const form = $("radioForm"); form.replaceChildren();
  const sec = d.sections.find(s => s.name === radioSection);
  form.append(el("h3", null, sec.label + (sec.kind === "module" ? " module" : " settings")));
  for (const f of sec.fields) {
    const row = el("div", "rfield" + (f.name in (radioEdits[sec.name] || {}) ? " changed" : ""));
    const lab = el("div"); lab.append(el("b", null, f.label));
    if (f.help) lab.append(el("small", null, f.help));
    if (f.risk) lab.append(el("small", "risk", "⚠ " + f.risk));
    const right = el("div", "rctl"); right.append(radioControl(sec.name, f));
    if (f.kind === "int" && f.max < 2 ** 31) right.append(el("small", null, `${f.min} to ${f.max}`));
    row.append(lab, right); form.append(row);
  }
  renderBackups(d.backups);
}

// Draws the saved backups with Download and Restore buttons. If the server says a backup came from a different radio, the user is asked again and the restore is retried with force.
function renderBackups(list) {
  const box = $("radioBackups"); box.replaceChildren();
  if (!list.length) { box.append(el("div", "hint", "No backups yet. Pull the radio's settings to save the first one.")); return; }
  for (const b of list) {
    const row = el("div", "rrow"); row.style.gridTemplateColumns = "150px 1fr auto";
    const acts = el("div"); acts.style.cssText = "display:flex;gap:8px;flex-wrap:wrap";
    const dl = el("a", "btn", "Download"); dl.href = "/api/radio/config/backup?id=" + b.id; dl.download = "";
    const rs = el("button", null, "Restore"); rs.disabled = !radioData.connected;
    rs.addEventListener("click", async () => {
      if (!confirm(`Put the radio's settings back to backup #${b.id} (${fmtTime(b.ts)}, ${b.reason})?\n\nOnly values that differ are written. The current settings are backed up first, and the radio restarts.`)) return;
      rs.disabled = true; let { ok, data } = await post("/api/radio/config/restore", { id: b.id });
      if (!ok && data.mismatch) {
        if (!confirm(data.error + "\n\nRestore it anyway?")) { rs.disabled = false; return; }
        ({ ok, data } = await post("/api/radio/config/restore", { id: b.id, force: true }));
      }
      if (!ok) { radioNote(data.error || "Could not restore.", true); rs.disabled = false; return; }
      // The radio restarts after a restore, so its settings are re-read after 4 s and again after 15 s.
      radioNote(data.message || "Restored."); radioEdits = {}; setTimeout(() => refreshRadio(true), 4000); setTimeout(() => refreshRadio(true), 15000);
    });
    acts.append(rs, dl);
    row.append(el("span", "time", fmtTime(b.ts)), el("span", null, `#${b.id} · ${b.reason}${b.radio ? " · " + b.radio : ""}${b.radio && radioData.radio && b.radio !== radioData.radio ? " · another radio" : ""}`), acts); box.append(row);
  }
}

// Confirms with a list of every change (and any risk warnings), then writes the changed fields to the radio.
async function saveRadio() {
  const ch = radioChanges(); if (!ch.length) return;
  const risky = [...new Set(ch.filter(c => c.risk).map(c => c.risk))];
  const lines = ch.map(c => `• ${c.label}: ${pretty(String(c.old))} → ${pretty(String(c.new))}`).join("\n");
  if (!confirm(`Write these settings to the radio?\n\n${lines}\n\n` + (risky.length ? "WARNING:\n" + risky.map(r => "• " + r).join("\n") + "\n\n" : "") + "The current settings are backed up first. The radio restarts to apply the change, so it will be unreachable for a few seconds; the bridge reconnects by itself.")) return;
  const changes = {}; for (const c of ch) (changes[c.section] = changes[c.section] || {})[c.field] = c.new;
  const b = $("radioSave"); b.disabled = true; radioNote("Writing to the radio…");
  const { ok, data } = await post("/api/radio/config/save", { changes });
  if (!ok) { radioNote(data.error || "Could not save.", true); renderRadioPending(); return; }
  radioEdits = {}; radioNote(data.message || "Saved."); renderRadio();
  // The radio restarts to apply the change; re-read its settings after 4 s and again after 15 s.
  setTimeout(() => refreshRadio(true), 4000); setTimeout(() => refreshRadio(true), 15000);
}
