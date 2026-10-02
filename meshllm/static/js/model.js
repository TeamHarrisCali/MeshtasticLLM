// model: the Model page. Lists Ollama models (/api/models), switches the active model (/api/model) and downloads new ones
// (/api/models/pull, /api/models/pull/cancel) with progress bars. Uses post from nav.js and refreshTop from core.js.
// ---- model tab ----------------------------------------------------------------------------
// modelSig: signature of the last drawn data; pullTimer: the pending re-poll while a download is running.
let modelSig = "", pullTimer = null;
// Bytes as GB (one decimal) or whole MB.
const fmtSize = n => n >= 1e9 ? (n / 1e9).toFixed(1) + " GB" : Math.max(1, Math.round(n / 1e6)) + " MB";

// Confirms, then switches the model. The warning text covers models without tool support and models whose AI tool reliability was not measured.
async function useModel(m, d) {
  const extra = !m.tools ? "\n\nThis model can't use tools, so AI tools won't work with it (normal chat still does)."
    : m.name !== d.default_model ? `\n\nPC-action reliability was only measured on ${d.default_model}. Run python -m meshllm.tools.eval_tools --model ${m.name} to check this one.` : "";
  if (!confirm(`Switch to ${m.name}?${extra}\n\nThe first answer will be slow while it loads into memory.`)) return;
  const { ok, data } = await post("/api/model", { model: m.name });
  if (!ok) alert(data.error || "Could not switch model");
  refreshModels(true); refreshTop();
}

// Draws the current model, the installed list and the download progress bars from the /api/models data d.
function renderModels(d) {
  const warn = $("modelWarn");
  warn.hidden = !(d.error || !d.current_installed);
  warn.textContent = d.error ? d.error : `The selected model "${d.current}" isn't installed on this Ollama. Pick one below, or download it.`;

  const cur = d.installed.find(m => m.name === d.current);
  const now = $("modelNow"); now.replaceChildren(d.current);
  if (cur) {
    now.append(el("span", "badge " + (cur.tools ? "ok" : "warn"), cur.tools ? "tools ✓ (AI tools available)" : "no tool support (AI tools off)"));
    if (d.loaded.includes(cur.name)) now.append(el("span", "badge info", "loaded in memory"));
  }
  $("modelNote").textContent = `Chosen here and remembered, so the bridge starts with no arguments. Tool-choice reliability for AI tools was measured on ${d.default_model} only (python -m meshllm.tools.eval_tools). Models without tool support still chat normally. Reasoning models have their "thinking" switched off, because radio replies are short and thinking would use up the answer budget.`;

  const rows = $("modelRows"); rows.replaceChildren();
  if (!d.installed.length) rows.append(el("div", "empty", d.error ? "Ollama isn't reachable." : "No models installed yet. Download one below."));
  for (const m of d.installed) {
    const r = el("div", "mrow");
    const isCur = m.name === d.current;
    const badges = el("div", "badges");
    if (isCur) badges.append(el("span", "badge ok", "In use"));
    if (d.loaded.includes(m.name)) badges.append(el("span", "badge info", "In memory"));
    badges.append(el("span", "badge" + (m.chat ? "" : " warn"), m.chat ? "Chat" : "Embedding only"));
    if (m.chat) badges.append(el("span", "badge " + (m.tools ? "ok" : "warn"), m.tools ? "Tools ✓" : "No tools"));
    if ((m.capabilities || []).includes("thinking")) badges.append(el("span", "badge", "Reasoning (thinking off)"));
    const btn = el("button", isCur ? "" : "primary", isCur ? "In use" : "Use");
    btn.disabled = isCur || !m.chat;
    btn.addEventListener("click", () => useModel(m, d));
    const act = el("div"); act.append(btn);
    r.append(el("div", "mname", m.name), el("div", "num", fmtSize(m.size)),
             el("div", null, [m.params, m.quant].filter(Boolean).join(" · ") || "–"), badges, act);
    rows.append(r);
  }

  const box = $("pullBox"); box.replaceChildren();
  for (const p of d.pulls) {
    const row = el("div", "pullrow"), top = el("div", "ptop");
    const pct = p.total ? Math.min(100, Math.round(100 * p.completed / p.total)) : 0;
    const label = p.done ? "Downloaded" : p.active ? (p.total ? `${pct}% · ${fmtSize(p.completed)} of ${fmtSize(p.total)}` : p.status) : p.status === "cancelled" ? "Cancelled" : "Failed";
    top.append(el("span", null, p.name + " — " + label));
    if (p.active) {
      const c = el("button", null, "Cancel");
      c.addEventListener("click", async () => { c.disabled = true; await post("/api/models/pull/cancel", { name: p.name }); refreshModels(true); });
      top.append(c);
    } else if (p.done) {
      const m = d.installed.find(x => x.name === p.name);
      if (m && m.name !== d.current) { const u = el("button", "primary", "Use it"); u.addEventListener("click", () => useModel(m, d)); top.append(u); }
    }
    row.append(top);
    if (p.active) { const bar = el("div", "pbar"); const fill = el("span"); fill.style.width = pct + "%"; bar.append(fill); row.append(bar); }
    if (p.error) row.append(el("div", "perr", p.error));
    box.append(row);
  }
}

// Fetches /api/models and redraws when changed (or force). While a download is active it re-polls every second, faster than the 3 s page tick, so progress looks smooth.
async function refreshModels(force) {
  let d; try { d = await api("/api/models"); } catch { return; }
  clearTimeout(pullTimer);
  if (d.pulls.some(p => p.active) && view === "model") pullTimer = setTimeout(() => refreshModels(false), 1000);
  const sig = JSON.stringify(d);
  if (sig === modelSig && !force) return;
  modelSig = sig;
  renderModels(d);
}

// Starts downloading a model by name.
$("pullForm").addEventListener("submit", async e => {
  e.preventDefault();
  const name = $("pullName").value.trim(); if (!name) return;
  const { ok, data } = await post("/api/models/pull", { name });
  const n = $("pullNote"); n.textContent = ok ? "Download started." : (data.error || "Could not start the download"); n.className = "sendnote" + (ok ? "" : " err");
  if (ok) $("pullName").value = "";
  refreshModels(true);
});
