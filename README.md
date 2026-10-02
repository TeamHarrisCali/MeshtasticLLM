# Meshtastic LLM Bridge

**Ask a local AI questions over a LoRa radio mesh. No internet, no cloud, no account.**

Plug a [Meshtastic](https://meshtastic.org) radio into your computer, run the bridge, and anyone on the mesh can send it a
direct message like `/ai which nodes have low batteries?`. A model running on your own machine through
[Ollama](https://ollama.com) answers, and the reply comes back over the air as short messages. A local web dashboard shows what
is happening on the mesh and lets you control who may ask what.

[![tests](https://github.com/TeamHarrisCali/MeshtasticLLM/actions/workflows/tests.yml/badge.svg)](https://github.com/TeamHarrisCali/MeshtasticLLM/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

```text
  phone / node ──LoRa──▶  your radio  ──USB──▶  bridge  ──▶  Ollama (local model)
       ▲                                          │  │
       └──────────── short DM replies ◀───────────┘  └──▶  dashboard at http://127.0.0.1:8080
```

## What it does

- **Answers questions over the radio.** DM a node with `/ai <question>`. Replies are split into balanced, numbered parts that
  fit a LoRa packet, sent with acknowledgements, and re-sent if the radio gives up on one.
- **Knows about your mesh.** The AI can look things up in the data your radio has heard: how many nodes are around, who is
  nearest, low batteries, the temperature outside from sensor nodes, signal quality, nodes gone quiet, the busiest times, one
  node's battery history. It answers from real numbers and says "I can't tell" instead of inventing them.
- **Is safe by construction.** The AI picks from a fixed menu of read-only lookups. It has no shell, cannot touch your files,
  and cannot transmit on its own. See [Safety model](#safety-model).
- **Finds the radio by itself.** Plug it in before or after starting; unplug it, swap it, move it to another USB port, and the
  bridge notices and reconnects.
- **Comes with a full dashboard.** Map, node list with your own labels and notes, coverage and walk test, traceroute, telemetry,
  trends, a daily report, radio settings backup and restore, diagnostics, and an AI log of every question and answer.
- **Runs unattended.** Background start and stop scripts, start at login, daily backups, logs.
- **Is measured.** An evaluation harness scores how reliably a model picks the right tool, with separate development and
  held-out question sets.

Everything runs on your computer. Once set up, the only outbound connections are ones you ask for: model downloads through
Ollama, and the optional map background, whose OpenStreetMap tiles are fetched only for the area you are looking at.

## Requirements

- A Meshtastic radio connected over USB (firmware 2.5 or newer for the encrypted-DM features).
- [Ollama](https://ollama.com/download) with at least one model that supports tool calling (for example `qwen3.5` or `llama3.2:3b`).
- Python 3.9 or newer. Windows, Linux and macOS are supported. Setup builds its own virtual environment.

## Quick start

```bash
git clone https://github.com/TeamHarrisCali/MeshtasticLLM.git
cd MeshtasticLLM
./setup.sh                 # Linux / macOS     (Windows: double-click setup.bat)
./start_bridge.sh          # runs in the background; ./stop_bridge.sh stops it
```

Setup finds Python, builds `.venv`, installs the dependencies, and checks Ollama and the radio. It is safe to run again at any
time. If something is missing it prints the exact command to fix it, and `./setup.sh --check` reports without changing anything.

Then open <http://127.0.0.1:8080/> and, from another node, send a direct message to your radio:

```text
/ai help
/ai how's the mesh doing?
/ai what's the temperature outside?
/ai where is the nearest router?
/ai has the battery on Hilltop Base been dropping today?
```

**No flags are needed.** The radio is found automatically, and the model and every setting you change in the dashboard are
remembered in the database. Command-line flags exist as optional overrides ([docs/flags.md](docs/flags.md)). To run in the
foreground instead: `.venv/bin/python mesh_llm_bridge.py`.

**Linux:** your user needs permission to open the serial port. Setup checks this and tells you which group to join (`dialout` on
Debian and Ubuntu, `uucp` on Arch). Close any other program using the radio's serial port first; only one program can hold it.

## Safety model

A radio mesh is an open channel and any text on it can be hostile, so the AI is deliberately boxed in.

| Layer | What it does |
|---|---|
| **DM only** | The AI answers direct messages that start with `/ai`. It ignores the public channel and never reads it. |
| **Access control** | Open or allow-list mode, a daily cap per node, block list. Blocked nodes get no reply at all. |
| **Tools are opt-in** | A node gets lookups only if you enable it, **pin its public key**, and it messages over a **PKI-encrypted DM** whose key still matches. |
| **Fixed menu** | The model can only *ask* for a tool by name. The code validates the name, the node's level and every parameter, then runs a hand-written handler. Nothing the model says can create a capability. |
| **Read-only** | Every tool reads the bridge's own database. None transmits, and none touches the computer. |
| **Confirmation codes** | Anything that would change something needs a one-time code confirmed over the radio. (Only a demo uses this today.) |
| **Untrusted names** | Node names are chosen by strangers, so they are stripped of control characters and never fed back to the model. |
| **Local dashboard** | The web UI binds to `127.0.0.1` by default. It has no login, so do not expose it to a network you do not trust. |

Details, and what this does *not* protect against (a stolen radio carries a valid key), are in
[docs/ai_tools.md](docs/ai_tools.md). To report a vulnerability, see [SECURITY.md](SECURITY.md).

## The dashboard

Open <http://127.0.0.1:8080/>. Four groups in the sidebar:

- **Messages:** the public channel (read and post by hand, the AI is kept out), direct messages, and what people asked the AI.
- **Network:** Home (alerts, sensors, map), Nodes, Map, Coverage (how far the radio reaches, walk test), Activity, Trends.
- **Tools:** Traceroute, Telemetry, Radio settings (pull, edit and push the radio's config), Data, Report, Diagnostics, Settings.
- **AI:** Overview, Model, Access and tools, the AI log, and Evaluation.

[docs/dashboard.md](docs/dashboard.md) explains every page.

## Documentation

| Page | What is in it |
|---|---|
| [docs/setup.md](docs/setup.md) | Setting up on a new computer, options, start at login |
| [docs/dashboard.md](docs/dashboard.md) | What each dashboard page does |
| [docs/features.md](docs/features.md) | How the bridge behaves: memory, queue, access control, models, telemetry, traceroute, and more |
| [docs/ai_tools.md](docs/ai_tools.md) | The fixed menu of lookups the AI may run, and how it is protected |
| [docs/evaluation.md](docs/evaluation.md) | Measuring how well a model picks tools |
| [docs/ai_usefulness_audit.md](docs/ai_usefulness_audit.md) | An audit of how useful the AI was, and what was done about it |
| [docs/flags.md](docs/flags.md) | Command-line flags (none are needed) |
| [docs/files.md](docs/files.md) | What each file in the project is for |
| [docs/roadmap.md](docs/roadmap.md), [docs/notes.md](docs/notes.md) | Design rules, known gaps, ideas |
| [docs/demo_script.md](docs/demo_script.md) | An 8-minute walk-through for presenting the project |

The same pages can be read inside the dashboard, on **Evaluation** under Write-ups.

## Tests

```bash
python run_tests.py                 # everything, about a minute; no radio or Ollama needed (both are faked)
python run_tests.py channel backup  # only the test files whose names contain these words
```

The tests use a throwaway temporary folder, so your real `audit.db`, `tile_cache/` and `logs/` are never touched.

## Project layout

The code is a flat set of modules, each with a docstring that says what it is for; [docs/files.md](docs/files.md) lists them all.
The entry point is `mesh_llm_bridge.py`. `actions.py` is the AI's tool menu, `webroutes.py` is the dashboard's JSON API, `static/`
is the dashboard itself (plain HTML, CSS and JavaScript, no build step), and `tests/` holds the suite.

## Contributing

Bug reports, hardware reports (what radio, what OS, what happened) and pull requests are welcome. Please read
[CONTRIBUTING.md](CONTRIBUTING.md) first. Known gaps and ideas are in [docs/roadmap.md](docs/roadmap.md).

## License and notices

[MIT](LICENSE). This is an independent project and is not affiliated with or endorsed by the Meshtastic project or Ollama.
"Meshtastic" is a registered trademark of Meshtastic LLC. You are responsible for operating your radio within the radio rules of
your country, and for deciding who on your mesh may talk to your computer.

Map tiles are © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), fetched on demand and cached locally in
line with the [tile usage policy](https://operations.osmfoundation.org/policies/tiles/).
