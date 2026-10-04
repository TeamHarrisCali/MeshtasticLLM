# Files in the project

```text
.
├── meshllm/                  the Python package: all of the bridge's code
│   ├── bridge.py             the bridge: radio <-> Ollama, queue, access rules, acked replies, memory (entry point)
│   ├── __main__.py           `python -m meshllm` starts the bridge
│   ├── connection.py         how the radio is reached: USB serial, Wi-Fi (TCP) or Bluetooth (BLE) endpoints, their health checks and retry wording, `--tcp`/`--ble`/`--fallback` parsing, and the `FailoverChain` that strings them into one prioritised connection
│   ├── demo.py               `--demo`: a simulated radio and mesh, a traffic generator and a scripted fake Ollama, so the dashboard works with no hardware
│   ├── actions.py            the AI's fixed menu of read-only mesh lookups, with parameter validation
│   ├── audit.py              the SQLite database (audit.db, next to the project): requests, settings, access rules
│   ├── mesh.py               the mesh pages' data: nodes, positions, packet counts, health history, alerts, activity feed
│   ├── telemetry.py          recording the telemetry the radio hears, retention and pruning
│   ├── traceroute.py         traceroute requests, path decoding and storage
│   ├── reach.py              the Coverage page: reach by direction, signal against distance, the walk test
│   ├── channel.py            the public channel page: hearing the primary channel, posting to it (operator only)
│   ├── radio_config.py       reading, backing up, validating and writing the radio's settings
│   ├── btfinder.py           the Connection page: the radio's Bluetooth state, the one-at-a-time scan for its Bluetooth address, the saved Bluetooth fallback
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
│   ├── websecurity.py        who may talk to the dashboard: Host/Origin/CSRF checks, login, sessions, throttling, roles, TLS
│   ├── passwords.py          scrypt password hashes, the hash file and `--set-password`
│   ├── static/               the dashboard: index.html, login.html + login.js (the sign-in page), style.css, and js/*.js (one file per page, joined in js/order.txt)
│   └── tools/                developer tools, run with `python -m`
│       ├── eval_tools.py         measures a model's tool-choice accuracy (dev and held-out question sets)
│       └── usefulness_audit.py   asks the running bridge a fixed set of questions and saves the answers
├── tests/                    the test suite (radio and Ollama are faked); run it with `python scripts/run_tests.py`
├── docs/                     these pages (the dashboard also shows them, on Evaluation under Write-ups), plus TODO.md (the hand-off queue, not shown there)
│   ├── eval_results/         saved evaluation runs (CSV for tool choice, JSON for the usefulness audit)
│   └── screenshots/          the pictures in the README (made in demo mode)
├── scripts/                  the installer and the helpers around it (they find the project folder as their parent)
│   ├── setup_env.py          the one-step setup for a new computer (also sets up start at login; `--docker` hands over to setup_docker.py)
│   ├── setup_docker.py       `setup_env.py --docker`: checks Docker, finds the radio, offers a dashboard password, writes .env and starts the containers
│   ├── setup.ps1             the Windows launcher for setup_env.py (setup.bat starts it)
│   ├── start_bridge.sh, stop_bridge.sh     run the bridge in the background / stop it (Linux / macOS)
│   ├── start_bridge.ps1, stop_bridge.ps1   the same on Windows
│   └── run_tests.py          runs every test file and prints one line each
├── setup.sh, setup.bat       the two "run this first" launchers (Linux / macOS, Windows): find Python, then run scripts/setup_env.py
├── Dockerfile                the container image: Python slim base pinned by tag and digest, numeric non-root user, healthcheck
├── docker-compose.yml        the bridge + Ollama + a `demo` profile; dashboard published on 127.0.0.1 unless you opt in to the LAN (which needs the login), hardened container
├── .env.example              the settings docker-compose.yml reads (copy to .env; every line is commented out)
├── docker/                   what the container image and Compose use besides the files above
│   ├── entrypoint.sh         container start-up: copies the login secret (as root, then drops to 10001), enforces the LAN rule, builds the bridge's command line from MESHLLM_* variables, then `exec`s it
│   ├── docker-compose.usb.yml    override that passes a USB serial radio into the container (Linux hosts only)
│   └── docker-compose.login.yml  override that adds the dashboard login: the password hash as a Compose secret file, copied to a 0400 tmpfs file for uid 10001
├── .dockerignore             keeps the database, backups, logs, tile cache, tests and git history out of the image
├── requirements.txt          the Python dependencies (meshtastic, requests)
├── LICENSE
├── .github/                  CONTRIBUTING.md (contribution guide), SECURITY.md (security policy), CI workflow, issue and pull request templates
└── .gitignore, .gitattributes
```

Made while running, and never committed (see `.gitignore`): `.venv/` (the private Python environment), `audit.db` (every setting, message
and bit of mesh data), `logs/`, `backups/`, `tile_cache/`, and a `private/` folder for anything you want to keep out of version control.

The two launchers, `setup.sh` and `setup.bat`, stay at the top level on purpose: they are the first thing a new user types. The
installer itself (`scripts/setup_env.py`) runs with the system Python before any dependency exists. `docker-compose.yml`, the `Dockerfile`
and `.env.example` stay at the top too, because Compose reads `.env` from the folder of the first compose file.
