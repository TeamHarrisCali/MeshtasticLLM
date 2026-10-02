// report: the Report page (a written summary for the chosen number of days from /api/report, download from /api/report.md, copy, print)
// and a small Markdown renderer (mdInline, renderMd) that eval.js also uses. The renderer builds only DOM elements and text nodes,
// so nothing in a report or document is ever parsed as HTML.
// ---- the written report, and a small Markdown reader used for it and for the project's write-ups (no HTML is ever inserted: only text nodes) ----
// Appends text to `into`, turning **bold** and `code` spans into strong and code elements and everything else into plain text nodes. Returns `into`.
function mdInline(text, into) {
  for (const part of text.split(/(\*\*[^*]+\*\*|`[^`]+`)/)) {
    if (!part) continue;
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) into.append(el("strong", null, part.slice(2, -2)));
    else if (part.startsWith("`") && part.endsWith("`") && part.length > 2) into.append(el("code", null, part.slice(1, -1)));
    else into.append(document.createTextNode(part));
  }
  return into;
}

// Renders a small Markdown subset into box: headings (# to ####, shown as h2 to h5), fenced code, tables, bullet and numbered lists, block quotes and paragraphs.
function renderMd(text, box) {
  box.replaceChildren();
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  let i = 0, list = null, para = null;
  // Ends the current list or paragraph.
  const flush = () => { list = null; para = null; };
  const cells = l => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(c => c.trim());
  while (i < lines.length) {
    const l = lines[i];
    let m;
    if (!l.trim()) { flush(); i++; continue; }
    if (l.startsWith("```")) {                                   // a code block
      flush(); const pre = el("pre", "mdcode"), buf = []; i++;
      while (i < lines.length && !lines[i].startsWith("```")) buf.push(lines[i++]);
      i++; pre.textContent = buf.join("\n"); box.append(pre); continue;
    }
    if ((m = /^(#{1,4})\s+(.*)$/.exec(l))) { flush(); box.append(mdInline(m[2], el("h" + (m[1].length + 1)))); i++; continue; }
    if (l.trim().startsWith("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {      // a table
      flush(); const t = el("table", "mdtable"), head = el("tr"), body = el("tbody");
      for (const c of cells(l)) head.append(mdInline(c, el("th")));
      const thead = el("thead"); thead.append(head); t.append(thead); i += 2;
      while (i < lines.length && lines[i].trim().startsWith("|")) { const tr = el("tr"); for (const c of cells(lines[i])) tr.append(mdInline(c, el("td"))); body.append(tr); i++; }
      t.append(body); const wrap = el("div", "mdscroll"); wrap.append(t); box.append(wrap); continue;
    }
    if ((m = /^\s*(?:[-*]|\d+\.)\s+(.*)$/.exec(l))) {
      const ordered = /^\s*\d+\./.test(l);
      if (!list || list.dataset.ordered !== String(ordered)) { para = null; list = el(ordered ? "ol" : "ul"); list.dataset.ordered = String(ordered); box.append(list); }
      list.append(mdInline(m[1], el("li"))); i++; continue;
    }
    if (l.startsWith(">")) { flush(); box.append(mdInline(l.replace(/^>\s?/, ""), el("blockquote"))); i++; continue; }
    if (!para) { list = null; para = el("p"); box.append(para); } else para.append(document.createTextNode(" "));
    mdInline(l.trim(), para); i++;
  }
}

// reportText keeps the raw Markdown for the Copy button.
let reportReady = false, reportText = "";
// One-time setup: restores the chosen number of days from localStorage and wires the download, copy and print buttons.
function initReport() {
  if (reportReady) return; reportReady = true;
  $("repDays").value = lsGet("reportDays") || "7"; if (!$("repDays").value) $("repDays").value = "7";
  $("repDays").addEventListener("change", () => { lsSet("reportDays", $("repDays").value); refreshReport(true); });
  $("repDownload").addEventListener("click", () => { const a = document.createElement("a"); a.href = "/api/report.md?days=" + $("repDays").value; a.download = ""; document.body.append(a); a.click(); a.remove(); });
  $("repCopy").addEventListener("click", async () => { try { await navigator.clipboard.writeText(reportText); $("repNote").textContent = "Copied."; } catch { $("repNote").textContent = "Your browser wouldn't allow copying; use Download instead."; } });
  $("repPrint").addEventListener("click", () => window.print());
}

// Generates the report for the chosen days. Only runs when forced (page opened or days changed); the poll does not regenerate it.
async function refreshReport(force) {
  if (!force) return;
  $("repNote").textContent = "Writing the report…";
  let d; try { d = await api("/api/report?days=" + $("repDays").value); } catch { $("repNote").textContent = "Could not make the report."; return; }
  reportText = d.markdown; renderMd(d.markdown, $("repBody")); $("repNote").textContent = "";
}
