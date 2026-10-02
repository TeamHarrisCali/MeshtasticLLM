# Files in the project

```text
.
├── meshllm/                  the Python package: all of the bridge's code
│   ├── bridge.py             the bridge: radio <-> Ollama, queue, access rules, acked replies, memory (entry point)
│   ├── __main__.py           `python -m meshllm` starts the bridge
│   ├── connection.py         how the radio is reached: USB serial, Wi-Fi (TCP) or Bluetooth (BLE) endpoints, their health checks and retry wording, `--tcp`/`--ble` parsing
│   ├── demo.py               `--demo`: a simulated radio and mesh, a traffic generator and a scripted fake Ollama, so the dashboard works with no hardware
│   ├── actions.py            the AI's fixed menu of read-only mesh lookups, with parameter validation
│   ├── audit.py              the SQLite database (audit.db, next to the project): requests, settings, access rules
│   ├── mesh.py               the mesh pages' data: nodes, positions, packet counts, health history, alerts, activity feed
│   ├── telemetry.py          recording the telemetry the radio hears, retention and pruning
│   ├── traceroute.py         traceroute requests, path decoding and storage
│   ├── reach.py              the Coverage page: reach by direction, signal against distance, the walk test
│   ├── channel.py            the public channel page: hearing the primary channel, posting to it (operator only)
│   ├── radio_config.py       reading, backing up, validating and writing the radio's settings
│   ├── ollama_models.py      listing and downloading Ollama models for the Model page
│   ├── tiles.py              the on-disk cache behind the OpenStreetMap map background (tile_cache/)
│   ├── userdata.py           your node labels, notes and stars, and saved snippets
│   ├── inbox.py              what is new since you last looked (sidebar badges, alerts) and search
│   ├── backup.py             backups, the daily copy, and restoring at the next start
│   ├── diagnostics.py        the health check and the log viewer
│   ├── report.py             the written report (counts only, never message text)
│   ├── evals.py              reads the saved evaluation results and the docs for the Evaluation page
│   ├── webui.py              the dashboard's web server: security checks, static files, JSON replies
│   ├── webroutes.py          the dashboard's JSON API as a table of small functions (one per route)
│   ├── static/               the dashboard: index.html, style.css, and js/*.js (one file per page, joined in js/order.txt)
│   └── tools/                developer tools, run with `python -m`
│       ├── eval_tools.py         measures a model's tool-choice accuracy (dev and held-out question sets)
│       └── usefulness_audit.py   asks the running bridge a fixed set of questions and saves the answers
├── tests/                    the test suite (radio and Ollama are faked); run it with `python run_tests.py`
├── docs/                     these pages (the dashboard also shows them, on Evaluation under Write-ups)
├── eval_results/             saved evaluation runs (CSV for tool choice, JSON for the usefulness audit)
├── setup_env.py              the one-step setup for a new computer (also sets up start at login)
├── setup.sh, setup.ps1, setup.bat          launchers for setup_env.py (Linux / macOS, Windows)
├── start_bridge.sh, stop_bridge.sh         run the bridge in the background / stop it (Linux / macOS)
├── start_bridge.ps1, stop_bridge.ps1       the same on Windows
├── run_tests.py              runs every test file and prints one line each
├── requirements.txt          the Python dependencies (meshtastic, requests)
├── LICENSE, CONTRIBUTING.md, SECURITY.md, .github/    licence, contribution guide, security policy, CI and issue templates
└── .gitignore, .gitattributes
```

Made while running, and never committed (see `.gitignore`): `.venv/` (the private Python environment), `audit.db` (every setting, message
and bit of mesh data), `logs/`, `backups/`, `tile_cache/`, and a `private/` folder for anything you want to keep out of version control.

The installer stays at the top level on purpose: it runs with the system Python before any dependency exists, and it is the first
thing a new user types.
