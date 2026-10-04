# Demo script (about 8 minutes)

A walk through the project in the order that tells the story: a radio mesh with no internet, a local AI reachable from any
node, and a dashboard that makes it understandable and safe. Times are rough. Have the radio plugged in and Ollama running
before you start (`python scripts/setup_env.py --check` tells you if anything is missing).

## Before you begin (1 minute, off stage)

1. `python scripts/setup_env.py --check`. Everything should be `[ok]` (an Ollama or radio warning means fix that first).
2. Start the bridge: `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_bridge.ps1` (Windows) or `./scripts/start_bridge.sh`.
3. Open <http://127.0.0.1:8080/>. Another person with a Meshtastic node ready to DM this radio is ideal; if not, the browser
   chat on the **AI conversations** side and the **AI overview** page show the same pipeline.

## 1. The idea (1 minute)

Say: "Meshtastic is a long-range radio mesh. There is no phone signal or internet needed. I connected one radio to this PC, and
a local language model answers questions sent over the mesh. Nothing leaves this computer."

Show **Home**: the radio name, how many nodes it has heard, the sensor strip (temperature and humidity averaged from nearby
sensor nodes), and the alert list.

## 2. Ask it something from a node (2 minutes)

From the other node, send a direct message:

- `/ai help` - answered instantly with no model (shows what it can do).
- `/ai ping` - replies with the signal of the message it just received: a free range test.
- `What is the temperature outside?` - the model picks a tool and answers with real numbers.
- `Which node is nearest to us?` - a live-data question: answered from the node list, not guessed.

Point at the **AI log** as each arrives: status, model time, signal, hops, and delivery (acked, relayed or failed).

Say: "The model never invents mesh facts. It is given the live numbers and a fixed menu of read-only tools, and if it tries to
answer a live question without one, the reply is replaced by the tool's real answer or an honest 'I can't check that'."

## 3. Safety and control (1.5 minutes)

- **Access & actions**: access mode (everyone or allow-list), a daily cap per node, block a node. Replies to blocked or unknown
  nodes are silent so nobody can make the radio transmit by spamming it.
- AI tools are opt-in per node, need a pinned key and a PKI-encrypted DM, and can need a one-time code.
- **Public channel**: post something yourself. Point at the "AI not involved" label. Say: "The AI never reads or answers the public
  channel, and it has no tool that can post. Only the operator transmits."

## 4. The network (1.5 minutes)

- **Map**: pan and zoom, link quality coloured by signal, optional OpenStreetMap background cached on this PC.
- **Coverage**: how far the radio reaches, by compass direction, and signal against distance. Start a **walk test** if someone is
  carrying a node: it logs signal and distance as they move.
- **Trends**: busiest hours, how many hops packets travel.
- **Traceroute**: trace the route to a node (operator only, rate limited).

## 5. Looking after it (1 minute)

- **Diagnostics**: one list that says what is wrong and what to do; the bridge's own log is below it.
- **Settings**: units, notifications, backups (daily automatic, plus download and restore).
- **Report**: a written summary of the week, downloadable as Markdown. It contains counts only, never message text.

## 6. How we know it works (1 minute)

Open **Evaluation**. Say: "We measure tool choice on two question sets. The development set is used to improve the prompts;
the held-out set is only ever used to measure, never tuned to."

Numbers to quote (from the saved runs on the page):

- Tool choice with `qwen3.5:latest`: 74 of 74 on the development questions, 66 of 68 on the held-out ones. (Measured on the earlier tool
  menu, which also had computer checks; re-run `eval_tools.py` for the current mesh-only menu before quoting these.)
- The usefulness audit of 25 realistic questions: at the start 8 were correct, 10 were confidently wrong and 7 gave no help; every
  wrong one was the model speaking without a tool. After adding live context, more tools and instant commands, 13 of the 25
  answers came from a tool (up from 8). Read `docs/ai_usefulness_audit.md` for the full list.
- One known miss is left in the held-out set ("Find me the closest repeater."). It was deliberately not fixed, because fixing a
  question you measure with turns the measurement into a memorised answer.

## If something goes wrong

| Symptom | What to do |
|---|---|
| The radio pill is red | Replug the radio; close any other program using its serial port. The bridge reconnects by itself. |
| "AI model isn't available" | Start Ollama (`ollama serve`) or pick an installed model on the **Model** page. |
| Answers take 10+ seconds | First question after a quiet spell loads the model; the bridge keeps it loaded for 30 minutes afterwards. |
| A node times out | Check **Diagnostics** and the **AI log** status; direct messages show delivery per part. |
| Nothing else works | `python scripts/setup_env.py --check` and the **Diagnostics** page say what is missing. |
