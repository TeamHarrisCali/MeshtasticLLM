// channel: part of the dashboard script (loaded in order by index.html; all files share one global scope)
// ---- public channel: read and post by hand (the AI is not involved) -----------------------------------------
let chanReady = false, chanSig = "", chanMax = 200;
function chanNote(msg, isErr) { const n = $("chanNote"); n.textContent = msg || ""; n.className = "sendnote" + (isErr ? " err" : ""); }
const byteLen = s => new TextEncoder().encode(s).length;
const CHAN_STATUS = { queued: "waiting for the radio", sent: "sent, not yet heard being repeated", heard: "sent and repeated by a neighbour", failed: "⚠ not sent" };

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
