// snippets: part of the dashboard script (loaded in order by index.html; all files share one global scope)
// ---- saved snippets: one-click text for the public channel and direct messages. A click only fills the box; you still press Send. ----
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

async function saveSnippet(target, input, say) {
  const text = input.value.trim();
  if (!text) { say("Type the message first, then press + Save.", true); return; }
  const { ok, data } = await post("/api/snippets/add", { target, text });
  say(ok ? "Saved as a snippet." : (data.error || "Could not save."), !ok);
  if (ok) renderSnippets(target);
}

$("dmSaveSnip").addEventListener("click", () => saveSnippet("dm", $("msg"), note));
