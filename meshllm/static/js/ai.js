// ai: the AI overview page (/api/ai/overview, /api/status: counts, recent questions, top askers, available AI tools) and a chat box
// that asks the AI from the browser: questions go to /api/ai/ask and answers are read from /api/conversation for a fixed web node;
// nothing is sent over the radio. Uses bubble from chat.js, tilesInto from mesh-common.js and lsGet/lsSet from traceroute.js.
// ---- the AI chat in the browser: the radio pipeline (queue, gate, model, read-only tools) without the radio --------------------
// Node id the server uses for conversations started from the browser.
const WEB_NODE = "web-console";
// aiChatSig skips redundant redraws, aiChatBusy blocks a second question while one is pending, aiChatPoll is the answer poll timer.
let aiChatReady = false, aiChatSig = "", aiChatBusy = false, aiChatPoll = null;
// Time (Unix seconds) of the last 'new chat'; earlier messages are hidden. Kept in localStorage, so it is per browser.
const aiChatSince = () => Number(lsGet("aiChatSince")) || 0;

// One-time setup: the ask form and the New chat button, which clears the AI's memory for the web node and hides earlier messages here.
function initAiChat() {
  if (aiChatReady) return; aiChatReady = true;
  $("aiChatForm").addEventListener("submit", async e => {
    e.preventDefault();
    const q = $("aiInput").value.trim(); if (!q || aiChatBusy) return;
    aiChatBusy = true; $("aiSend").disabled = true; $("aiChatNote").textContent = ""; $("aiChatNote").className = "sendnote";
    const { ok, data } = await post("/api/ai/ask", { prompt: q });
    if (!ok) { aiChatBusy = false; $("aiSend").disabled = false; $("aiChatNote").textContent = data.error || "Could not ask."; $("aiChatNote").className = "sendnote err"; return; }
    $("aiInput").value = ""; waitForAnswer(data.id);
  });
  $("aiChatReset").addEventListener("click", async () => {
    if (!confirm("Start a new chat? The AI forgets this conversation (it stays in the AI log).")) return;
    await post("/api/memory/clear", { node: WEB_NODE }); lsSet("aiChatSince", Date.now() / 1000); aiChatSig = ""; refreshAiChat(true);
  });
}

// Gives up after 120 polls (about two minutes) and re-enables the form.
function waitForAnswer(rid) {       // poll quickly until this question has an answer
  clearInterval(aiChatPoll); let tries = 0;
  const tick = async () => {
    const rows = await refreshAiChat(true);
    const row = rows && rows.find(r => r.id === rid);
    if ((row && row.status !== "queued") || ++tries > 120) { clearInterval(aiChatPoll); aiChatBusy = false; $("aiSend").disabled = false; $("aiInput").focus(); }
  };
  tick(); aiChatPoll = setInterval(tick, 1000);
}

// Redraws the chat from the stored conversation (messages newer than the last new chat, at most 40). Returns the messages, or null on failure.
async function refreshAiChat(force) {
  let d; try { d = await api("/api/conversation?node=" + WEB_NODE); } catch { return null; }
  const since = aiChatSince(), rows = d.messages.filter(r => r.ts > since).slice(-40);
  const sig = JSON.stringify(rows.map(r => [r.id, r.status, r.response])) + d.memory;
  if (sig !== aiChatSig || force) {
    aiChatSig = sig;
    const t = $("aiThread"), near = t.scrollHeight - t.scrollTop - t.clientHeight < 80; t.replaceChildren();
    if (!rows.length) t.append(el("div", "empty", "Ask a question below, or pick one of the suggestions."));
    for (const r of rows) {
      t.append(bubble("out", r.prompt || "", fmtTime(r.ts)));
      if (r.status === "queued") t.append(bubble("in pending", "thinking…"));
      else {
        const label = (STATUS[r.status] || [r.status])[0], ms = r.llm_ms ? " · " + fmtMs(r.llm_ms) : "";
        const b = bubble("in" + (r.status === "llm_error" || r.status === "action_denied" ? " lost" : ""), r.response || "(no answer)", `AI${ms}${r.status !== "answered" ? " · " + label : ""}`);
        if (r.action) b.querySelector(".meta2").append(el("span", "badge info tool", "ran " + r.action));
        t.append(b);
      }
    }
    if (near || force) t.scrollTop = t.scrollHeight;
    if (!aiChatBusy) $("aiChatNote").textContent = d.memory ? `The AI remembers the last ${d.memory} message${d.memory === 1 ? "" : "s"} of this chat.` : "";
  }
  return d.messages;
}

// ---- AI overview ----------------------------------------------------------------------------------------------------------
// Signature of the last overview drawn.
let aiSig = "";
// AI tool names that only read the mesh database; used to group the tool list.
const MESH_ACTIONS = new Set(["mesh_summary", "list_nodes", "node_info"]);

// Draws the overview: alerts, summary tiles, recent questions, top askers, example question chips and the AI tools grouped by kind. Also starts the browser chat. Skips the redraw if nothing changed.
async function refreshAi(force) {
  initAiChat(); refreshAiChat(force);
  let d, st; try { [d, st] = await Promise.all([api("/api/ai/overview"), api("/api/status")]); } catch { return; }
  const sig = JSON.stringify([d, st.command]); if (sig === aiSig && !force) return; aiSig = sig;
  const al = $("aiAlerts"); al.replaceChildren();
  if (!d.ollama_ok) al.append(el("div", "alert warn", `The AI model '${d.model}' isn't available from Ollama right now.`));
  if (d.paused) al.append(el("div", "alert info", "The bot is paused: questions get no answer until you resume it (top right)."));
  const fails = Object.entries(d.counts_24h).filter(([k]) => !["answered", "action_ok"].includes(k)).sort((a, b) => b[1] - a[1]);
  tilesInto($("aiTiles"), [
    ["Model", d.model, d.ollama_ok ? "ready" : "unavailable", null, "model"],
    ["Questions · 24 h", d.total_24h, `${d.ok_24h} answered`, null, "audit"],
    ["Answered", d.total_24h ? Math.round(100 * d.ok_24h / d.total_24h) + "%" : "–", fails.length ? fails.slice(0, 2).map(([k, n]) => `${n} ${(STATUS[k] || [k])[0].toLowerCase()}`).join(" · ") : "nothing went wrong"],
    ["Average model time", d.avg_ms == null ? "–" : fmtMs(d.avg_ms), "per answer, last 24 h"],
    ["Queue", d.queue_depth, d.paused ? "paused" : "questions waiting"],
    ["Who can ask", d.mode === "allowlist" ? "Allowed only" : "Anyone", "except blocked nodes", null, "access"],
  ]);
  const rec = $("aiRecent"); rec.replaceChildren();
  if (!d.recent.length) rec.append(el("div", "hint", "No questions yet. Send “" + st.command + " hello” from another node."));
  for (const r of d.recent) {
    const [label] = STATUS[r.status] || [r.status], row = el("div", "irow");
    row.append(el("span", null, `${r.node_name || r.node_id}: ${(r.prompt || "").slice(0, 70)}`), el("small", null, `${label} · ${agoStr(Math.max(0, Date.now() / 1000 - r.ts))}`));
    row.addEventListener("click", () => go("chat", r.node_id)); rec.append(row);
  }
  const ask = $("aiAskers"); ask.replaceChildren();
  if (!d.top_askers.length) ask.append(el("div", "hint", "Nobody has asked in the last 24 h."));
  for (const a of d.top_askers) { const row = el("div", "irow"); row.append(el("span", null, a.node_name || a.node_id), el("small", null, `${a.n} question${a.n === 1 ? "" : "s"}`)); row.addEventListener("click", () => go("chat", a.node_id)); ask.append(row); }
  const ex = $("aiExamples"); ex.replaceChildren();
  for (const q of ["how's the mesh doing?", "which nodes have the lowest battery?", "who is the closest node to us?", "what is the temperature outside?"]) {
    const c = el("button", "chip ask", q); c.type = "button"; c.addEventListener("click", () => { $("aiInput").value = q; $("aiInput").focus(); }); ex.append(c);
  }
  const acts = $("aiActions"); acts.replaceChildren();
  for (const [title, test, note] of [["Mesh questions", a => MESH_ACTIONS.has(a.name), "Read from the database only. They never transmit."], ["This computer", a => !MESH_ACTIONS.has(a.name) && a.tier === 0, "Read-only."], ["Needs a code confirmed over the radio", a => a.tier >= 1, ""]]) {
    const list = d.actions.filter(test); if (!list.length) continue;
    acts.append(el("h4", "subh", title + (note ? " · " + note : "")));
    for (const a of list) { const r = el("div", "actrow"); r.append(el("code", null, a.name), el("span", "badge " + (a.tier ? "warn" : "ok"), a.tier ? "needs radio code" : "read-only"), el("span", null, a.description)); acts.append(r); }
  }
}
