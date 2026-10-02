# TODO: what to build next

This file is the hand-off for whoever (person or AI agent) picks the project up next. Work top to bottom. Each item is its own
branch and pull request, reviewed by someone who did not write it, and merged only when CI is green; see
[CONTRIBUTING.md](CONTRIBUTING.md).

## Scope

This is a **hobby project for one person's own radios**. It is not a fleet, crew or jobsite tool: no check-ins for other people's
radios, no crew groups, no job calculators. The AI stays read-only on a fixed tool menu and never transmits on its own
([docs/roadmap.md](docs/roadmap.md) has the design rules).

## Queue

- [ ] **1. Demo mode** (`python -m meshllm --demo`)
  - A simulated mesh (a fake radio with a few dozen nodes, positions, batteries, sensors, links, traffic and a couple of scripted
    `/ai` conversations) so anyone can try the dashboard with no hardware, and a built-in scripted model so it also works without
    Ollama (use the real Ollama model instead when one is available).
  - Reuse what `tests/fixture.py` already fakes. Use obviously fake names and ids. Never touch the real `audit.db`: demo mode uses a
    temporary database.
  - Then take README screenshots of the demo dashboard (Home, Nodes, Map, AI log) into `docs/screenshots/` and reference them from the README.
  - Done when: `--demo` starts with nothing plugged in, every dashboard page renders, tests cover it.
- [ ] **2. Connect over Wi-Fi and Bluetooth, not only USB**
  - The `meshtastic` library has `TCPInterface` (a radio on the LAN, by host name or IP) and `BLEInterface` (by address). `bleak` is already installed.
  - Today the bridge opens a serial port (search for `SerialInterface` in `meshllm/bridge.py`; `--port auto|COMx`). Add a connection choice (`--tcp HOST`, `--ble ADDRESS`, still defaulting to USB auto-detect), keep the reconnect behaviour, and let the dashboard choose and scan (Settings or a new Connection page).
  - Tests: fake the interface classes. Real-hardware test needs a radio with Wi-Fi or Bluetooth enabled; ask the owner first.
- [ ] **3. Headless / LAN mode with a login**
  - Goal: serve the dashboard on the LAN instead of loopback so a phone or another PC can use it, **only with proper security**.
  - Refuse to start on a non-loopback address unless a password is configured. Password stored as a salted `hashlib.scrypt` hash; set it with a CLI prompt, never on the command line or in logs.
  - Login: `HttpOnly`, `SameSite=Strict` session cookie (and `Secure` when TLS is on), constant-time comparison, throttling of failed attempts, logout, and session expiry. Keep the existing Host and Origin checks working on a LAN name, plus a Host allowlist.
  - A **read-only viewer role** (no send box, no radio settings, no access changes) next to the admin role.
  - Optional TLS (`--tls-cert`, `--tls-key`) and a documented reverse-proxy setup. Document clearly that plain HTTP on a LAN is readable by anyone on it.
  - **Needs an independent security review before merge**, with tests for each rule above (including that every POST route needs the session).
- [ ] **4. Docker and Docker Compose**
  - Image for the bridge, plus a Compose file with an Ollama service and a volume for the database. Password from a secret or environment variable (item 3), listening on the LAN.
  - USB serial passthrough (`devices:`) works on Linux hosts only; on Windows and macOS use the Wi-Fi/TCP radio path (item 2). Say so in the docs.
  - CI builds the image. Do not run the container as root; pin the base image by tag.
- [ ] **5. Afterwards**
  - A whole-project security and bug pass: static analysis, a dependency scan, a secret scan over the full history, a review of every web route and of the installer scripts.
  - Install `qwen3.5` and re-run the tool-choice evaluation next to `llama3.2:3b` (the weak spot is choosing the right `mesh_report` topic).
  - Run the full over-the-air loop again after each change that touches the radio path (the owner has a test radio).

## Not planned

Crew or jobsite features (see Scope), anything that lets the AI transmit or change the radio on its own, and cloud services.

## Status of the rest

Everything else is merged and green: the `meshllm` package, mesh-only AI tools, the dashboard, the test suite (about 1300 checks, run in
CI on Python 3.9 to 3.13), and the review workflow. Known smaller gaps are in [docs/roadmap.md](docs/roadmap.md).
