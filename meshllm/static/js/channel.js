// channel: the Public channel page. Shows what the radio heard on the primary channel and posts by hand (the AI is not involved).
// Uses /api/channel (messages and channel info), /api/channel/post and /api/channel/clear. Uses bubble from chat.js and
// saveSnippet/renderSnippets from snippets.js.
// ---- public channel: read and post by hand (the AI is not involved) -----------------------------------------
// chanMax: maximum message size in bytes, taken from the server.
let chanReady = false, chanSig = "", chanMax = 200;
// Shows a message on the channel page, in error style when isErr is set.
function chanNote(msg, isErr) { const n = $("chanNote"); n.textContent = msg || ""; n.className = "sendnote" + (isErr ? " err" : ""); }
// Length in UTF-8 bytes; the radio's message limit is in bytes, not characters.
const byteLen = s => new TextEncoder().encode(s).length;
// Delivery status of a message we posted -> text shown under it.
const CHAN_STATUS = { queued: "waiting for the radio", sent: "sent, not yet heard being repeated", heard: "sent and repeated by a neighbour", failed: "⚠ not sent" };

// One-time setup: live byte counter, save-as-snippet, post and clear handlers.
function initChannel() {
  if (chanReady) return; chanReady = true;
  const count = () => { const n = byteLen($("chanMsg").value); $("chanCount").textContent = n ? `${n}/${chanMax}` : ""; $("chanCount").className = "chancount" + (n > chanMax ? " over" : ""); };
  $("chanMsg").addEventListener("input", count);
  $("chanSaveSnip").addEventListener("click", () => saveSnippet("channel", $("chanMsg"), chanNote));
  renderSnippets("channel");
  $("chanForm").addEventListener("submit", async e => {
    e.preventDefault();
    const text = $("chanMsg").value.trim(); if (!text) return;
    $("chanSend").disabled = true;
    const { ok, data } = await post("/api/channel/post", { text });
    $("chanSend").disabled = false;
    if (!ok) { chanNote(data.error || "Could not post.", true); return; }
    $("chanMsg").value = ""; count(); chanNote("Posted. The status shows under your message.");
    refreshChannel(true);
  });
  $("chanClear").addEventListener("click", async () => {
    if (!confirm("Delete every message saved from the public channel on this PC? This cannot be undone.")) return;
    const { ok, data } = await post("/api/channel/clear", {});
    chanNote(ok ? `Cleared ${data.deleted} message${data.deleted === 1 ? "" : "s"}.` : (data.error || "Could not clear."), !ok);
    refreshChannel(true);
  });
}

// Loads the latest 150 messages and the channel info, shows warnings (no radio, or a non-default key) and redraws the thread when it changed. Scrolls to the bottom only on first load, force, or if the reader was already near it.
async function refreshChannel(force) {
  let d; try { d = await api("/api/channel?limit=150"); } catch { return; }
  const info = d.info; chanMax = info.max_bytes || 200;
  $("chanName").textContent = info.known ? `Public channel · ${info.name}` : "Public channel";
  const banner = $("chanBanner"), warn = [];
  if (!info.connected) warn.push("No radio is connected, so posting is paused. Messages already heard are shown below.");
  else if (info.known && info.default_key === false) warn.push("This radio's primary channel is not using the default public key, so this is not the open public channel: posts reach whoever shares your key.");
  banner.hidden = !warn.length; banner.textContent = warn.join(" ");
  $("chanSend").disabled = !info.connected;
  const sig = JSON.stringify(d.messages);
  if (sig === chanSig && !force) return;
  chanSig = sig;
  const t = $("chanThread"), nearBottom = t.scrollHeight - t.scrollTop - t.clientHeight < 80, first = !t.querySelector(".bubble");
  t.replaceChildren();
  if (!d.messages.length) t.append(el("div", "empty", "Nothing heard on the public channel yet. Posts from people in range appear here as they arrive."));
  for (const m of d.messages) {
    if (m.direction === "out") t.append(bubble("out op" + (m.status === "failed" ? " lost" : ""), m.text, `You · ${fmtTime(m.ts)} · ${CHAN_STATUS[m.status] || m.status}`));
    else {
      const sig2 = [m.rx_snr != null ? `SNR ${Math.round(m.rx_snr * 10) / 10} dB` : null, m.hops != null ? (m.hops === 0 ? "direct" : `${m.hops} hop${m.hops === 1 ? "" : "s"}`) : null].filter(Boolean).join(" · ");
      t.append(bubble("in", m.text, `${m.node_name || m.node_id} · ${fmtTime(m.ts)}${sig2 ? " · " + sig2 : ""}`));
    }
  }
  if (first || nearBottom || force) t.scrollTop = t.scrollHeight;
}
