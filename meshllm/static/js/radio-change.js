// radio-change: the banner shown when a different radio is plugged in. Reads /api/radio/change, lists what still needs
// attention on the new node, and can set its clock (POST /api/radio/time) or dismiss the banner (POST /api/radio/change/dismiss).
// Uses api and el from core.js and post, go from nav.js.
// ---- "a different radio is connected": what changed, and what is still worth doing on it -------------------------------------------
// bannerAt/bannerSig: time of the last fetch and a signature of what is drawn, so polling does not rebuild the banner needlessly.
let bannerAt = 0, bannerSig = "";
// Called with the 'radio_change' flag from /api/status. Fetches the details at most every 6 s while the banner is visible,
// and hides the banner when no change is pending.
function syncRadioBanner(active) {
  const box = $("radioBanner");
  if (!active) { box.hidden = true; bannerSig = ""; return; }
  if (Date.now() - bannerAt < 6000 && !box.hidden) return;
  bannerAt = Date.now();
  api("/api/radio/change").then(d => { if (d) renderRadioBanner(d); else box.hidden = true; }).catch(() => {});
}

// Draws the banner for the details in d (old and new node, checklist items with their action buttons); skipped when nothing changed.
function renderRadioBanner(d) {
  const box = $("radioBanner"), sig = JSON.stringify(d); if (sig === bannerSig && !box.hidden) return; bannerSig = sig;
  box.replaceChildren(); box.hidden = false;
  const name = (n, id) => n ? `${n} (${id})` : id;
  box.append(el("h3", null, "A different radio is connected"), el("p", null, `${name(d.to_name, d.to)} replaced ${name(d.from_name, d.from)}. It is a new node on the mesh with its own settings, clock and list of known nodes, so a few things need another look:`));
  for (const i of d.items) {
    const row = el("div", "crow" + (i.done ? " done" : "")), txt = el("div");
    txt.append(el("b", null, i.label), el("small", null, i.detail)); row.append(el("span", "tick", i.done ? "✓" : "○"), txt);
    const acts = el("div");
    if (!i.done && i.action === "sync_clock") {
      const b = el("button", null, "Set the radio's clock");
      b.addEventListener("click", async () => {
        if (!confirm("Tell the radio the current time from this PC?\n\nThis changes the radio's clock only.")) return;
        b.disabled = true; const { ok, data } = await post("/api/radio/time", {});
        if (!ok) { alert(data.error || "Could not set the clock."); b.disabled = false; return; }
        bannerAt = 0;
      });
      acts.append(b);
    } else if (!i.done && i.page) { const b = el("button", null, "Open"); b.addEventListener("click", () => go(i.page)); acts.append(b); }
    row.append(acts); box.append(row);
  }
  const bar = el("div", "bar2"), dismiss = el("button", d.all_done ? "primary" : null, d.all_done ? "All set - dismiss" : "Dismiss");
  dismiss.addEventListener("click", async () => { await post("/api/radio/change/dismiss", {}); box.hidden = true; bannerSig = ""; });
  bar.append(dismiss); box.append(bar);
}

// Pause/resume toggle in the header: flips the state, then refreshes the header.
$("pauseBtn").addEventListener("click", async () => {
  const paused = !$("pauseBtn").dataset.paused;
  await fetch("/api/pause", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paused }) });
  refreshTop();
});
// AI log controls: Load older, a debounced (250 ms) text search, and the status filter.
$("moreBtn").addEventListener("click", loadOlder);
let debounce;
$("q").addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(() => refreshRows(true), 250); });
$("status").addEventListener("change", () => refreshRows(true));
