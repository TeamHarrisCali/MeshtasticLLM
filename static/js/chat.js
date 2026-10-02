// chat: part of the dashboard script (loaded in order by index.html; all files share one global scope)
async function loadNodes() {
  try { knownNodes = await api("/api/nodes"); } catch { return; }
  const dl = $("nodeOptions"); dl.replaceChildren();
  for (const n of knownNodes.slice(0, 200)) {
    const o = document.createElement("option");
    o.value = n.id; o.label = [n.long_name, n.short_name && `(${n.short_name})`].filter(Boolean).join(" ");
    dl.append(o);
  }
}

// Direct messages (you and another person, no AI) and AI conversations (what people asked the AI) use the same page; chatScope says which.
function applyChatScope() {
  const dm = chatScope === "dm";
  $("newForm").hidden = !dm; $("sendForm").hidden = !dm || !selected; $("dmSnips").hidden = !dm || !selected;
  $("clearMem").hidden = dm || !selected; $("convAccess").hidden = dm || !selected;
  $("memInfo").hidden = dm; $("convUsage").hidden = dm;
  if (!selected) {
    $("convName").textContent = "Select a conversation"; $("convId").textContent = "";
    $("thread").replaceChildren(el("div", "empty", dm ? "Pick a person on the left, or open a new chat to message someone directly." : "Pick a node on the left to see what it asked the AI and what the AI answered."));
  }
  if (dm) renderSnippets("dm");
}

async function refreshConvs() {
  let convs; try { convs = await api("/api/conversations?scope=" + chatScope); } catch { return; }
  const sig = JSON.stringify(convs) + selected + chatScope;
  if (sig === convSig) return;
  convSig = sig;
  const box = $("convs"); box.replaceChildren();
  if (!convs.length) box.append(el("div", "empty", chatScope === "dm" ? "No direct messages yet. Open a new chat above to message someone." : "Nobody has asked the AI anything yet."));
  for (const c of convs) {
    const d = el("div", "conv" + (c.node_id === selected ? " sel" : ""));
    const top = el("div", "top");
    top.append(el("b", null, c.node_name || c.node_id), el("span", "when", fmtTime(c.last_ts)));
    d.append(top, el("div", "last", (c.last_kind === "manual" ? "You: " : "") + (c.last_text || "")));
    d.addEventListener("click", () => selectNode(c.node_id, c.node_name));
    box.append(d);
  }
}

function selectNode(id, name) {
  selected = id; selectedName = name || (knownNodes.find(n => n.id === id) || {}).long_name || null;
  threadSig = ""; convSig = ""; note("");
  applyChatScope();
  $("convName").textContent = selectedName || "Unnamed node"; $("convId").textContent = id;
  refreshConvs(); refreshThread(true);
  if (chatScope === "dm") $("msg").focus();
}

function bubble(cls, text, meta) {
  const b = el("div", "bubble " + cls, text);
  if (meta) b.append(el("span", "meta2", meta));
  return b;
}

function bubblesFor(r) {
  const [dlabel] = delivery(r);
  const dpart = r.chunks ? ` · ${dlabel}` : "";
  // A part the radio gave up on must be obvious: the reader is missing a piece of the message.
  const warn = r.failed ? ` · ⚠ ${r.failed} part${r.failed === 1 ? "" : "s"} NOT delivered` : "";
  const noteTip = r.delivery_note ? ` (${r.delivery_note})` : "";
  if (r.kind === "manual") return [bubble("out op" + (r.failed ? " lost" : ""), r.response, `You · ${fmtTime(r.ts)}${dpart}${warn}${noteTip}`)];
  if (r.kind === "inbound") return [bubble("in", r.prompt, fmtTime(r.ts))];
  const out = [bubble("in", r.prompt || "(empty)", fmtTime(r.ts))];
  if (r.status === "queued") out.push(bubble("out pending", "thinking…"));
  else if (r.response) {
    const [label] = STATUS[r.status] || [r.status];
    out.push(bubble("out" + (r.failed ? " lost" : ""), r.response,
      `AI${r.llm_ms ? " · " + fmtMs(r.llm_ms) : ""}${r.status !== "answered" ? " · " + label : ""}${dpart}${warn}${noteTip}`));
  }
  return out;
}

async function refreshThread(force) {
  if (!selected) return;
  const scope = chatScope;
  let data; try { data = await api(`/api/conversation?scope=${scope}&node=` + encodeURIComponent(selected)); } catch { return; }
  if (scope !== chatScope) return;                 // the page changed while we were asking
  if (scope === "ai") {
    $("memInfo").textContent = data.memory ? `AI remembers ${data.memory} message${data.memory === 1 ? "" : "s"}` : "AI has no memory of this node";
    const acc = data.access;
    if (document.activeElement !== $("convAccess")) $("convAccess").value = acc.access;
    $("convUsage").textContent = `${acc.used_24h}${acc.effective_cap ? " / " + acc.effective_cap : ""} today`;
  }
  const sig = JSON.stringify(data.messages);
  if (sig === threadSig && !force) return;
  const first = threadSig === "";
  threadSig = sig;
  const t = $("thread");
  const nearBottom = t.scrollHeight - t.scrollTop - t.clientHeight < 80;
  t.replaceChildren();
  if (!data.messages.length) t.append(el("div", "empty", scope === "dm" ? "No messages yet. Write below to start." : "This node has not asked the AI anything."));
  for (const r of data.messages) for (const b of bubblesFor(r)) t.append(b);
  if (first || nearBottom) t.scrollTop = t.scrollHeight;
}

$("convAccess").addEventListener("change", async e => {
  if (!selected) return;
  const { ok, data } = await post("/api/access/node", { node: selected, access: e.target.value });
  note(ok ? "Access updated." : data.error || "Could not update access", !ok);
  refreshThread(true);
});

$("newForm").addEventListener("submit", e => {
  e.preventDefault();
  const v = $("newTo").value.trim(); if (!v) return;
  let id = null, name = null;
  if (/^![0-9a-f]{8}$/i.test(v)) id = v.toLowerCase();
  else {
    const m = knownNodes.find(n => [n.long_name, n.short_name].some(s => s && s.trim().toLowerCase() === v.toLowerCase()));
    if (m) { id = m.id; name = m.long_name; }
  }
  if (!id) { note("Unknown node. Use a name this radio has heard, or an ID like !1a2b3c4d.", true); return; }
  $("newTo").value = "";
  selectNode(id, name);
});

$("sendForm").addEventListener("submit", async e => {
  e.preventDefault();
  const text = $("msg").value.trim(); if (!text || !selected) return;
  const btn = e.submitter || e.target.querySelector("button"); btn.disabled = true;
  const { ok, data } = await post("/api/send", { node: selected, text });
  btn.disabled = false;
  if (!ok) { note(data.error || "Could not send", true); return; }
  $("msg").value = ""; note("Queued for the radio. Delivery status will appear on the message.");
  await refreshThread(true); refreshConvs();
});

$("clearMem").addEventListener("click", async () => {
  if (!selected || !confirm(`Make the AI forget its earlier conversation with ${selectedName || selected}? The messages stay in the audit log.`)) return;
  await post("/api/memory/clear", { node: selected });
  note("AI memory cleared for this node."); refreshThread(true);
});
