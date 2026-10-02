// access: the Access & actions page. Shows /api/access (mode, default daily limit, per-node table) and edits it through
// /api/access/mode, /api/access/default_cap and /api/access/node (allow/block, daily limit, AI tools level, public key pinning).
// /api/actions lists the AI tools. Uses post from nav.js and fmtTime from core.js.
// ---- access tab ---------------------------------------------------------------------------
// accSig: signature of the last drawn table; accData: the last overview from the server.
let accSig = "", accData = null;

// True while the user is typing in a field on this page other than the search box; refreshes hold off so their input is not wiped.
function accEditing() { const a = document.activeElement; return a && $("viewAccess").contains(a) && a.id !== "accQ" && a.tagName === "INPUT"; }

// Shortens a public key for display (first 6 and last 4 characters).
const keyShort = k => k ? k.slice(0, 6) + "…" + k.slice(-4) : "";

// "AI tools" level + public-key pinning for one node
function actionsRow(n) {
  const name = n.node_name || n.node_id;
  const sub = el("div", "sub");

  const lab = el("label", null, "AI tools");
  const tsel = document.createElement("select");
  tsel.setAttribute("aria-label", "AI tools for " + name);
  for (const [v, t] of [["", "Off"], ["0", "Read-only"], ["1", "Read-only + confirmed actions"]]) {
    const o = document.createElement("option"); o.value = v; o.textContent = t; tsel.append(o);
  }
  tsel.value = n.max_tier == null ? "" : String(n.max_tier);
  tsel.addEventListener("change", async () => {
    const { ok, data } = await post("/api/access/node", { node: n.node_id, max_tier: tsel.value === "" ? null : parseInt(tsel.value, 10) });
    if (!ok) alert(data.error || "Could not update");
    refreshAccess(true);
  });
  lab.append(tsel);

  const key = el("span", "keybox");
  // Pinning trusts whoever holds the matching private key to run enabled AI tools, so the full key is shown and the user must confirm.
  const pinTo = async msg => {
    if (!confirm(msg)) return;
    const { ok, data } = await post("/api/access/node", { node: n.node_id, pin_key: n.public_key });
    if (!ok) alert(data.error || "Could not pin the key");
    refreshAccess(true);
  };
  const unpin = async () => {
    await post("/api/access/node", { node: n.node_id, pin_key: null });
    refreshAccess(true);
  };
  const verifyText = `Pin this public key for ${name}?\n\n${n.public_key}\n\nOnly continue if it matches the key shown for that node in its own Meshtastic app. Check in person or by phone - not over the radio. Whoever holds the matching private key will be able to run AI tools you enable for this node.`;
  // key_state: pinned, changed (the key the radio sees differs from the pinned one), missing (pinned but not seen again), unpinned, or no key known.
  if (n.key_state === "pinned") {
    const b = el("button", null, "Unpin"); b.addEventListener("click", unpin);
    key.append(el("span", "badge ok", "Key pinned"), Object.assign(el("code"), { textContent: keyShort(n.pinned_key) }), b);
  } else if (n.key_state === "changed") {
    const b = el("button", null, "Review new key…");
    b.addEventListener("click", () => pinTo(`WARNING: the key this radio has for ${name} CHANGED since you pinned it.\nThat can be a reset or a new device - or an impostor.\n\n` + verifyText));
    const u = el("button", null, "Unpin"); u.addEventListener("click", unpin);
    key.append(el("span", "badge bad", "KEY CHANGED"), Object.assign(el("code"), { textContent: keyShort(n.pinned_key) + " → " + keyShort(n.public_key) }), b, u);
  } else if (n.key_state === "missing") {
    const u = el("button", null, "Unpin"); u.addEventListener("click", unpin);
    key.append(el("span", "badge warn", "Key not seen yet"), Object.assign(el("code"), { textContent: keyShort(n.pinned_key) }), "your pinned key is kept; actions stay off until this radio hears the node again", u);
  } else if (n.key_state === "unpinned") {
    const b = el("button", null, "Verify & pin key…"); b.addEventListener("click", () => pinTo(verifyText));
    key.append(el("span", "badge", "Key not pinned"), Object.assign(el("code"), { textContent: keyShort(n.public_key) }), b);
  } else {
    key.append(el("span", "badge", "No key known"), "the radio hasn't seen a public key for this node");
  }
  sub.append(lab, key);
  if (n.max_tier != null && n.key_state !== "pinned") sub.append(el("span", "badge warn", "Actions stay off until a key is pinned"));
  return sub;
}

let actionsLoaded = false;
// Fills the list of available AI tools once (/api/actions); later calls do nothing.
async function loadActions() {
  if (actionsLoaded) return;
  let list; try { list = await api("/api/actions"); } catch { return; }
  actionsLoaded = true;
  const box = $("actList"); box.replaceChildren();
  for (const a of list) {
    const r = el("div", "actrow");
    r.append(el("code", null, a.name), el("span", "badge " + (a.tier ? "warn" : "ok"), a.tier ? "needs radio code" : "read-only"),
             el("span", null, a.description));
    box.append(r);
  }
}

// Fetches the overview and redraws the mode, default limit and per-node table. Does nothing if nothing changed (unless force) or while a field is being edited.
async function refreshAccess(force) {
  let data; try { data = await api("/api/access"); } catch { return; }
  accData = data;
  if (!accEditing()) {
    if (document.activeElement !== $("accMode")) $("accMode").value = data.mode;
    if (document.activeElement !== $("accCap")) $("accCap").value = data.default_cap;
  }
  const allowed = data.nodes.filter(n => n.access === "allow").length;
  const warn = $("accWarn");
  warn.hidden = !(data.mode === "allowlist" && !allowed);
  warn.textContent = "Allowlist mode is on but no node is allowed yet, so nobody can use the AI. Set at least one node to Allowed below.";
  const q = $("accQ").value.trim().toLowerCase();
  const list = data.nodes.filter(n => !q || (n.node_name || "").toLowerCase().includes(q) || n.node_id.includes(q));
  const sig = JSON.stringify([data.mode, data.default_cap, list, q]);
  if ((sig === accSig && !force) || accEditing()) return;
  accSig = sig;
  const box = $("accRows"); box.replaceChildren();
  if (!list.length) box.append(el("div", "empty", "No nodes yet. Nodes appear here once your radio hears them or they message you."));
  const defLabel = data.mode === "allowlist" ? "Default (not allowed)" : "Default (allowed)";
  for (const n of list) {
    const row = el("div", "row acc"); const head = el("div", "head");
    const who = el("div", "node"); who.append(el("b", null, n.node_name || "Unnamed node"), el("span", null, n.node_id + (n.asked ? "" : " · no questions yet")));
    const sel = document.createElement("select");
    sel.setAttribute("aria-label", "Access for " + (n.node_name || n.node_id));
    for (const [v, t] of [["default", defLabel], ["allow", "Allowed"], ["block", "Blocked"]]) {
      const o = document.createElement("option"); o.value = v; o.textContent = t; sel.append(o);
    }
    sel.value = n.access;
    sel.addEventListener("change", async () => {
      const { ok, data: d } = await post("/api/access/node", { node: n.node_id, access: sel.value });
      if (!ok) alert(d.error || "Could not update");
      refreshAccess(true);
    });
    const cap = document.createElement("input");
    cap.type = "number"; cap.min = 0; cap.step = 1; cap.inputMode = "numeric";
    cap.placeholder = data.default_cap ? "default " + data.default_cap : "default ∞";
    cap.setAttribute("aria-label", "Daily limit for " + (n.node_name || n.node_id));
    if (n.daily_cap != null) cap.value = n.daily_cap;
    // Daily limit input: blank means use the default; anything else must be a whole number.
    const saveCap = async () => {
      const raw = cap.value.trim();
      if (raw !== "" && !/^\d+$/.test(raw)) { alert("Enter a whole number, or leave blank to use the default."); cap.value = n.daily_cap ?? ""; return; }
      const v = raw === "" ? null : parseInt(raw, 10);
      if (v === n.daily_cap) return;
      const { ok, data: d } = await post("/api/access/node", { node: n.node_id, daily_cap: v });
      if (!ok) alert(d.error || "Could not update");
      refreshAccess(true);
    };
    cap.addEventListener("change", saveCap);
    const s = el("div"); s.append(sel); const c = el("div"); c.append(cap);
    head.append(who, s, c,
      el("div", "num", n.used_24h + (n.effective_cap ? " / " + n.effective_cap : "")),
      el("div", "time", n.last_ts ? fmtTime(n.last_ts) : "–"));
    row.append(head, actionsRow(n)); box.append(row);
  }
}

// Top-of-page controls: access mode, default daily limit, and a debounced (200 ms) node filter.
$("accMode").addEventListener("change", async e => {
  const { ok, data } = await post("/api/access/mode", { mode: e.target.value });
  if (!ok) alert(data.error || "Could not change mode");
  refreshAccess(true);
});
$("accCapSave").addEventListener("click", async () => {
  const raw = $("accCap").value.trim();
  if (!/^\d+$/.test(raw)) { alert("Enter a whole number (0 = unlimited)."); return; }
  const { ok, data } = await post("/api/access/default_cap", { cap: parseInt(raw, 10) });
  if (!ok) alert(data.error || "Could not save");
  refreshAccess(true);
});
let accDebounce;
$("accQ").addEventListener("input", () => { clearTimeout(accDebounce); accDebounce = setTimeout(() => refreshAccess(true), 200); });
