# AI tools

The AI can look things up about the radio mesh, but only for nodes you have explicitly allowed. It is off for
everyone until you enable it in the Access tab, and it takes **all** of these at once:

1. You set the node's *AI tools* level (Read-only, or Read-only + confirmed actions).
2. You **pin the node's public key** (Access tab -> "Verify & pin key"). Check the key against the one
   shown in that node's own Meshtastic app, in person or by phone. Trusting whatever key the radio first
   heard would defeat the point.
3. The message arrives as a **PKI-encrypted DM** (Meshtastic 2.5+), which means the radio decrypted it
   with that node's key.
4. The key the radio holds for the node still equals the pinned one. If it changes you get a red
   "KEY CHANGED" badge and the tools stay off until you review and re-pin.

## What the AI can look up

`actions.py` holds a fixed menu. Every entry reads the bridge's own database; none of them transmits, and none touches
the computer the bridge runs on (no files, disks, processes or shell).

| Tool | Answers questions like |
|---|---|
| `mesh_summary` | "How's the mesh doing?", "How many nodes are around?" |
| `list_nodes` | "Which nodes have the lowest battery?", "Where is the nearest router?" (sort and role filters) |
| `mesh_report` | Topics: sensors ("what's the temperature outside?"), signal, quiet nodes, busiest times, recent activity |
| `node_history` | "Has the battery on X been dropping today?" |
| `node_info` | "Tell me about node !a1b2c3d4", "What's the battery on Ridge Repeater?" |
| `demo_confirm` | Does nothing except prove the confirmation flow works |

The model can only *ask* for one by name. The code checks the name, the node's level and every parameter against a
declared schema, then runs a hand-written handler and sends its result back verbatim. Tools are only offered to
verified nodes, and a tool call from anyone else is ignored. Adding a capability means adding an entry to `ACTIONS`;
nothing the model says can create one. Node names come from strangers, so they are stripped of control characters and
are never fed back to the model.

Actions that change something (tier 1) additionally need a one-time 6-digit code: the bridge DMs it, the
user replies `/ai confirm <code>` within 60 s (`--confirm-seconds`). The reply must itself be a verified
PKI DM from the same node. Codes are single-use, wrong guesses are limited to 3, `/ai cancel` aborts,
and the code is never written to the audit log. `/ai actions` lists what a node may run. There is currently
no tier-1 tool besides the demo; the mechanism is there for when one is added.

Every request records how the sender was verified ("Sender check" in the audit detail).

**What this does not protect against:** a key proves a *device*, not a person. A lost or stolen radio or
phone carries a valid key; unpin it (or set its level to Off) in the Access tab. Keep the menu to things
you'd be comfortable a stolen key could trigger, and put anything more sensitive behind the tier-1 code.

Measure how reliably a model picks the right tool with `python eval_tools.py` (see `evaluation.md`).
