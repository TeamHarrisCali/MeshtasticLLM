// search: the search box and results page. /api/search?q= looks through node names and labels, direct messages, AI conversations and
// public-channel posts; each result links to its page with go(). Ctrl/Cmd+K or / focuses the box.
// ---- search everything: nodes and your labels, direct messages, AI conversations, public-channel posts ----------------------------------
// One-time setup: submitting goes to the search page address. Ctrl/Cmd+K, or / when not typing in a field, focuses the box; Escape leaves it.
function initSearch() {
  $("searchForm").addEventListener("submit", e => { e.preventDefault(); const q = $("searchBox").value.trim(); if (q) go("search", q); });
  document.addEventListener("keydown", e => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement || {}).tagName || "") || (document.activeElement || {}).isContentEditable;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); $("searchBox").focus(); $("searchBox").select(); }
    else if (e.key === "/" && !typing && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); $("searchBox").focus(); $("searchBox").select(); }
    else if (e.key === "Escape" && document.activeElement === $("searchBox")) $("searchBox").blur();
  });
}

// Sequence number so an older, slower search cannot overwrite a newer one.
let searchSeq = 0;
// Runs a search and draws the grouped results. showView calls this with the query from the address, so a search can be reloaded or bookmarked. Result text is added with textContent.
async function runSearch(q) {
  const seq = ++searchSeq;
  $("searchBox").value = q; $("searchFor").textContent = q ? `for “${q}”` : "";
  const box = $("searchBody"); box.replaceChildren();
  if (!q) { box.append(el("div", "empty", "Type something in the search box at the top (or press / to get there).")); return; }
  let d; try { d = await api("/api/search?q=" + encodeURIComponent(q)); } catch { box.append(el("div", "empty", "The search failed. Is the bridge running?")); return; }
  if (seq !== searchSeq) return;           // a newer search has started
  if (d.error) { box.append(el("div", "empty", d.error)); return; }
  if (!d.groups.length) { box.append(el("div", "empty", `Nothing found for “${d.q}”. Search looks at node names, IDs, your labels and notes, direct messages, AI questions and answers, and public-channel posts.`)); return; }
  for (const g of d.groups) {
    box.append(el("h3", "subh", g.label));
    for (const it of g.items) {
      const row = el("div", "sres"), head = el("div", "sh");
      head.append(el("b", null, it.title)); if (it.sub) head.append(el("small", null, it.sub)); if (it.ts) head.append(el("span", "when", fmtTime(it.ts)));
      row.append(head); if (it.text) row.append(el("div", "st", it.text));
      row.tabIndex = 0; row.setAttribute("role", "link");
      const open = () => go(it.go[0], it.go[1] || undefined);
      row.addEventListener("click", open); row.addEventListener("keydown", e => { if (e.key === "Enter") open(); });
      box.append(row);
    }
  }
}
