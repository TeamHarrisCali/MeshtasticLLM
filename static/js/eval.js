// eval: part of the dashboard script (loaded in order by index.html; all files share one global scope)
// ---- evaluation: saved results for the AI (tool choice, usefulness audits) and the project's write-ups -----------------------------------------
let evalData = null, evalOpen = null, docOpen = null;
const pctCell = (r, cls) => { const c = el("td", cls || ""); if (!r) { c.textContent = "–"; return c; } const bar = el("span", "pbar2"), fill = el("i"); fill.style.width = r.pct + "%"; fill.className = r.pct >= 95 ? "g" : r.pct >= 80 ? "a" : "r"; bar.append(fill); c.append(el("b", null, r.pct.toFixed(1) + "%"), el("small", null, ` ${r.good}/${r.n}`), bar); return c; };
const modelLabel = (m, v) => el("td", null, m + (v && v !== "?" ? " · " + v.replace(/_/g, " ") : ""));

async function refreshEval(force) {
  if (!force && evalData) return;
  try { evalData = await api("/api/evals"); } catch { return; }
  const d = evalData;
  // the newest run of each model and prompt variant, development and held-out side by side
  const cmp = $("evalCompare"); cmp.replaceChildren();
  if (!d.comparison.length) cmp.append(el("p", "hint", "No saved tool-choice runs found in eval_results/."));
  else {
    const t = el("table", "evtable"), h = el("tr");
    for (const c of ["Model · prompt variant", "Development questions", "Held-out questions", "Average answer time"]) h.append(el("th", null, c));
    t.append(h);
    for (const r of d.comparison) { const tr = el("tr"); tr.append(modelLabel(r.model, r.variant), pctCell(r.dev), pctCell(r.heldout), el("td", null, [r.dev, r.heldout].filter(Boolean).map(x => x.avg_ms).filter(Boolean).map(ms => (ms / 1000).toFixed(1) + " s")[0] || "–")); t.append(tr); }
    const wrap = el("div", "mdscroll"); wrap.append(t); cmp.append(wrap);
  }
  const runs = $("evalRuns"); runs.replaceChildren();
  for (const r of d.runs) {
    const row = el("div", "evrun" + (evalOpen === r.file ? " open" : "")), head = el("div", "evhead");
    head.append(el("b", null, r.label), el("span", "evset " + (r.set.startsWith("held") ? "held" : "dev"), r.set), el("span", null, `${r.good}/${r.n} · ${r.pct.toFixed(1)}%`), el("span", "hide-sm", r.avg_ms ? (r.avg_ms / 1000).toFixed(1) + " s avg" : ""), el("span", "when hide-sm", fmtTime(r.ts)));
    head.tabIndex = 0; const toggle = () => { evalOpen = evalOpen === r.file ? null : r.file; refreshEval(true); };
    head.addEventListener("click", toggle); head.addEventListener("keydown", e => { if (e.key === "Enter") toggle(); });
    row.append(head);
    if (evalOpen === r.file) {
      const body = el("div", "evbody");
      body.append(el("div", "hint", `${r.model} · ${r.variant} · file ${r.file}`));
      for (const [k, v] of Object.entries(r.categories)) { const line = el("div", "hbar"), track = el("div", "track"), fill = el("i"); fill.style.width = (100 * v.good / v.n) + "%"; track.append(fill); line.append(el("span", null, k.replace(/_/g, " ")), track, el("b", null, `${v.good}/${v.n}`)); body.append(line); }
      const miss = Object.entries(r.misses); if (miss.length) body.append(el("div", "hint", "Misses: " + miss.map(([k, n]) => `${k.replace(/_/g, " ")} ${n}`).join(", ")));
      row.append(body);
    }
    runs.append(row);
  }
  if (!d.runs.length) runs.append(el("p", "hint", "Nothing saved yet."));
  const use = $("evalUse"); use.replaceChildren();
  if (!d.usefulness.length) use.append(el("p", "hint", "No usefulness audits saved. Run python usefulness_audit.py results.json with the bridge running."));
  else {
    const t = el("table", "evtable"), h = el("tr"); for (const c of ["Audit", "Questions", "Answered from a tool", "Average time"]) h.append(el("th", null, c)); t.append(h);
    for (const u of d.usefulness) { const tr = el("tr"); tr.append(el("td", null, u.label), el("td", null, String(u.n)), el("td", null, `${u.tool_answers} of ${u.n}`), el("td", null, u.avg_ms ? (u.avg_ms / 1000).toFixed(1) + " s" : "–")); t.append(tr); }
    const wrap = el("div", "mdscroll"); wrap.append(t); use.append(wrap);
    use.append(el("p", "hint", "A tool answer comes from real data. Answers without a tool were written by the model from the live facts it was handed; read docs/ai_usefulness_audit.md (below) for how each question was judged."));
  }
  const chips = $("docChips"); chips.replaceChildren();
  if (docOpen == null && d.docs.length) docOpen = d.docs[0];
  for (const name of d.docs) { const c = el("button", "chip", name.replace(/_/g, " ")); c.setAttribute("aria-pressed", String(name === docOpen)); c.addEventListener("click", () => { docOpen = name; refreshEval(true); }); chips.append(c); }
  if (docOpen) { try { renderMd((await api("/api/docs?name=" + encodeURIComponent(docOpen))).text, $("docBody")); } catch { $("docBody").textContent = "Could not load that document."; } }
}
