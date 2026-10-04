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

## The safety layers at a glance

A radio mesh is an open channel and any text on it can be hostile, so the AI is deliberately boxed in.

| Layer | What it does |
|---|---|
| **DM only** | The AI answers direct messages that start with `/ai`. It ignores the public channel and never reads it. |
| **Access control** | Open or allow-list mode, a daily cap per node, block list. Blocked nodes get no reply at all. |
| **Tools are opt-in** | A node gets lookups only if you enable it, **pin its public key**, and it messages over a **PKI-encrypted DM** whose key still matches (the four conditions above). |
| **Fixed menu** | The model can only *ask* for a tool by name. The code validates the name, the node's level and every parameter, then runs a hand-written handler. Nothing the model says can create a capability. |
| **Read-only** | Every tool reads the bridge's own database. None transmits, and none touches the computer. |
| **Confirmation codes** | Anything that would change something needs a one-time code confirmed over the radio. (Only a demo uses this today.) |
| **Untrusted names** | Node names are chosen by strangers, so they are stripped of control characters and never fed back to the model. |
| **Local dashboard** | The web UI binds to `127.0.0.1` by default, with no login. On any other address it refuses to start without a password (scrypt hash in a file only you can read) and a host allow-list; sessions, throttled logins, CSRF checks, an `admin`/`viewer` split on every route and optional HTTPS are described in [setup.md](setup.md#use-the-dashboard-from-a-phone-or-another-computer-lan-login). Without HTTPS the password crosses your network in clear text. |

The sections below give the details, and what this does *not* protect against (a stolen radio carries a valid key). To report a vulnerability,
see [SECURITY.md](../.github/SECURITY.md).

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

Measure how reliably a model picks the right tool with `python -m meshllm.tools.eval_tools` (see `evaluation.md`).
