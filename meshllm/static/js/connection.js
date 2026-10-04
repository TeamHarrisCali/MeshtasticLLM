// connection: the Connection page. Shows how the bridge is linked to the radio (/api/connection), whether the radio's Bluetooth is on and its
// pairing mode, and (admin only) finds the radio's Bluetooth address with a host scan (/api/connection/ble/scan), saves it as the Bluetooth
// fallback (/api/connection/ble/save) or forgets it (/api/connection/ble/clear). The pairing PIN is never sent to the page. Uses post from nav.js.
// ---- connection: the link, the radio's Bluetooth, the Bluetooth address finder -------------------------------------------------------------
// connReady: one-time setup done. connSig: the last drawn data, so the 3 s poll redraws only when something changed.
let connReady = false, connSig = "";
// What a chain entry's state means, in words.
const CONN_STATE = { active: "connected now", available: "available", standby: "standby", unavailable: "not present", parked: "waiting to retry" };
// Shows a message under the scan button, in error style when bad is set.
const connNote = (msg, bad) => { const n = $("connNote"); n.textContent = msg || ""; n.className = "sendnote" + (bad ? " err" : ""); };

// One-time setup: the scan button. The scan runs on the server for up to 25 s; the page poll shows the result when it is done.
function initConnection() {
  if (connReady) return; connReady = true;
  $("connScan").addEventListener("click", async () => {
    $("connScan").disabled = true; connNote("Starting the scan…");
    const { ok, data } = await post("/api/connection/ble/scan", {});
    if (!ok) connNote(data.error || "Could not start the scan.", true);
    connSig = ""; refreshConnection(true);
  });
}

// Loads the connection data and redraws the page when it changed (or when forced).
async function refreshConnection(force) {
  let d; try { d = await api("/api/connection"); } catch { return; }
  const sig = JSON.stringify(d);
  if (!force && sig === connSig) return; connSig = sig;
  renderConnection(d);
}

// Draws the chain, the radio's Bluetooth state and, for the admin, the address finder and the saved fallback.
function renderConnection(d) {
  const kind = k => ({ usb: "USB", tcp: "Wi-Fi", ble: "Bluetooth" })[k] || "Radio";
  $("connWhen").textContent = d.connected ? "· connected" + (d.kind ? " over " + kind(d.kind) : "") : "· searching for the radio";
  const chain = $("connChain"); chain.replaceChildren();
  const rows = d.entries.length ? d.entries : [{ kind: d.kind, label: d.port || "", state: d.connected ? "active" : "unavailable" }];
  for (const e of rows) {
    const row = el("div", "irow static");
    row.append(el("span", null, kind(e.kind) + (d.admin && e.label && e.label !== kind(e.kind) ? " · " + e.label : "")), el("small", null, CONN_STATE[e.state] || e.state || ""));
    chain.append(row);
  }
  $("connText").textContent = d.failover ? (d.text || "Failover is set up: the first entry that works is used.") : "One connection, no failover.";
  const bt = d.bluetooth, box = $("connBt"); box.replaceChildren();
  if (!bt.known) box.append(el("div", "hint", bt.reason || "Not known."));
  else {
    const on = el("div", "irow static"); on.append(el("span", null, "Bluetooth"), el("small", null, bt.enabled ? "on" : "off"));
    const mode = el("div", "irow static"); mode.append(el("span", null, "Pairing"), el("small", null, bt.mode_text));
    box.append(on, mode);
    if (!bt.enabled) box.append(el("div", "hint", "Bluetooth is switched off on the radio, so nothing can be found or connected over it until it is turned on (Radio settings, Bluetooth)."));
  }
  for (const id of ["connFindCard", "connSavedCard"]) $(id).hidden = !d.admin;
  if (!d.admin) return;
  const scan = d.scan || { state: "idle" }, scanning = scan.state === "scanning";
  $("connExpect").textContent = d.expected_name ? `This radio should advertise as ${d.expected_name} (its name is Meshtastic_ plus the last four characters of its node id). The radio does not report its Bluetooth address over USB, so it is found by scanning.` : "The radio is not connected, so its Bluetooth name is not known yet.";
  $("connScan").disabled = !!d.scan_blocked || scanning;
  if (d.scan_blocked) connNote(d.scan_blocked, true);
  else if (scanning) connNote("Scanning… this takes up to 25 seconds.");
  else if (scan.state === "idle") connNote("");
  else connNote(scan.message || "", scan.state !== "done");
  const found = $("connFound"); found.replaceChildren();
  for (const c of scan.candidates || []) {
    const row = el("div", "irow static"), btn = el("button", null, "Use as Bluetooth fallback");
    btn.disabled = !c.can_save; if (!c.can_save) btn.title = "This system reports the address in a form that cannot be saved (only AA:BB:CC:DD:EE:FF addresses can).";
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      const { ok, data } = await post("/api/connection/ble/save", { address: c.address });
      connNote(data.message || data.error || (ok ? "Saved." : "Could not save."), !ok); connSig = ""; refreshConnection(true);
    });
    row.append(el("span", null, `${c.name} · ${c.address}`), btn); found.append(row);
  }
  const saved = $("connSaved"); saved.replaceChildren();
  if (!d.saved) saved.append(el("div", "hint", "No Bluetooth fallback is saved."));
  else {
    const row = el("div", "irow static"), clear = el("button", null, "Clear");
    clear.addEventListener("click", async () => {
      clear.disabled = true;
      const { ok, data } = await post("/api/connection/ble/clear", {});
      connNote(data.message || data.error || (ok ? "Cleared." : "Could not clear."), !ok); connSig = ""; refreshConnection(true);
    });
    row.append(el("span", null, "Saved: " + d.saved), clear); saved.append(row);
  }
  if (d.restart_needed) saved.append(el("div", "warnbox", "Restart the bridge (stop_bridge, then start_bridge) for this to take effect."));
  if (d.flags_override) saved.append(el("div", "hint", "This bridge was started with --fallback, --tcp or --ble. Those always win, so the saved address is not used until you start it without them."));
  else if (d.saved && d.in_use === d.saved) saved.append(el("div", "hint", "This bridge started with this address as its Bluetooth fallback."));
}
