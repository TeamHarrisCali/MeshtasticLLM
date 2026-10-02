// snippets: saved one-click text for the public channel and direct messages (/api/snippets, /api/snippets/add, /api/snippets/delete).
// Clicking a snippet only fills the message box. renderSnippets and saveSnippet are called from chat.js and channel.js.
// ---- saved snippets: one-click text for the public channel and direct messages. A click only fills the box; you still press Send. ----
// Draws the snippet chips for the 'dm' or 'channel' target. A chip fills the message box and fires an input event so the byte counter updates.
async function renderSnippets(target) {
  const box = $(target === "dm" ? "dmSnips" : "chanSnips"); if (!box) return;
  let d; try { d = await api("/api/snippets?target=" + target); } catch { return; }
  box.replaceChildren();
  if (!d.snippets.length) { box.append(el("span", "hint", "Type a message and press + Save to keep it here for next time.")); return; }
  const input = $(target === "dm" ? "msg" : "chanMsg");
  for (const s of d.snippets) {
    const chip = el("span", "snip"), use = el("button", "snipuse", s.label), del = el("button", "snipdel", "×");
    use.type = del.type = "button";
    use.title = s.text + "\n\nClick to put this in the message box. Nothing is sent until you press " + (target === "dm" ? "Send" : "Post") + ".";
    del.title = "Delete this snippet"; del.setAttribute("aria-label", "Delete the snippet " + s.label);
    use.addEventListener("click", () => { input.value = s.text; input.dispatchEvent(new Event("input")); input.focus(); });
    del.addEventListener("click", async () => { await post("/api/snippets/delete", { id: s.id }); renderSnippets(target); });
    chip.append(use, del); box.append(chip);
  }
}

// Saves the text in input as a snippet for the target; say(message, isError) reports the result.
async function saveSnippet(target, input, say) {
  const text = input.value.trim();
  if (!text) { say("Type the message first, then press + Save.", true); return; }
  const { ok, data } = await post("/api/snippets/add", { target, text });
  say(ok ? "Saved as a snippet." : (data.error || "Could not save."), !ok);
  if (ok) renderSnippets(target);
}

// Save button for the Direct messages box; the channel page wires its own in initChannel.
$("dmSaveSnip").addEventListener("click", () => saveSnippet("dm", $("msg"), note));
