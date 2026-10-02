// queue: part of the dashboard script (loaded in order by index.html; all files share one global scope)
// ---- queue card (Audit tab) ---------------------------------------------------------------
let queueSig = "";
async function refreshQueue() {
  let items; try { items = await api("/api/queue"); } catch { return; }
  const card = $("queueCard");
  card.hidden = !items.length;
  if (!items.length) { queueSig = ""; return; }
  $("queueCount").textContent = `· ${items.length} waiting`;
  const sig = items.map(i => i.id + (i.working ? "w" : "")).join(",");
  const box = $("queueRows");
  if (sig !== queueSig) {                       // rebuild only when the queue itself changed
    queueSig = sig; box.replaceChildren();
    for (const i of items) {
      const row = el("div", "qrow");
      const badgeWrap = el("div"); badgeWrap.append(el("span", "badge " + (i.working ? "info" : ""), i.working ? "Answering…" : "#" + i.position));
      row.append(badgeWrap, el("div", "qwho", i.node_name || i.node_id), el("div", "qprompt", i.prompt),
                 Object.assign(el("div", "num waited"), { id: "wait-" + i.id }));
      const act = el("div");
      if (!i.working) {
        const b = el("button", null, "Cancel");
        b.addEventListener("click", async () => {
          b.disabled = true;
          const { ok, data } = await post("/api/queue/cancel", { id: i.id });
          if (!ok) alert(data.error || "Could not cancel");
          refreshQueue(); refreshRows(false);
        });
        act.append(b);
      }
      row.append(act); box.append(row);
    }
  }
  const now = Date.now() / 1000;
  for (const i of items) { const w = $("wait-" + i.id); if (w) w.textContent = Math.max(0, Math.round(now - i.ts)) + " s"; }
}
