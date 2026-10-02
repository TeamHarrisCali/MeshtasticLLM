# TODO: what to build next

This file is the hand-off for whoever (person or AI agent) picks the project up next. Work top to bottom. Each item is its own
branch and pull request, reviewed by someone who did not write it, and merged only when CI is green; see
[CONTRIBUTING.md](CONTRIBUTING.md). Items 2 to 4 depend on each other as noted under each.

## Scope

This is a **hobby project for one person's own radios**. It is not a fleet, crew or jobsite tool: no check-ins for other people's
radios, no crew groups, no job calculators. The AI stays read-only on a fixed tool menu and never transmits on its own
([docs/roadmap.md](docs/roadmap.md) has the design rules).

## Queue

- [ ] **1. Demo mode** (`python -m meshllm --demo`): implemented in PR #12; only the screenshots remain
  - [x] A simulated mesh (a fake radio with a few dozen nodes, positions, batteries, sensors, links, traffic and a couple of scripted
    `/ai` conversations) so anyone can try the dashboard with no hardware, and a built-in scripted model so it also works without
    Ollama (use the real Ollama model instead when one is available).
  - [x] The radio fake in `tests/fixture.py` is thin (four nodes, almost no batteries, sensors, links or traffic) and lives outside the
    package. Demo mode needs its own richer fake inside `meshllm/`. Use obviously fake names and ids. Demo mode uses a temporary
    database and never touches the real `audit.db`.
  - [ ] Then take README screenshots of the demo dashboard (Home, Nodes, Map, AI log) into `docs/screenshots/` and reference them from the README.
  - Done when: `--demo` starts with nothing plugged in, every dashboard page renders, tests cover it.
- [ ] **2. Connect over Wi-Fi and Bluetooth, not only USB**
  - The `meshtastic` library has `TCPInterface` (a radio on the LAN, by host name or IP) and `BLEInterface` (by address). `bleak` is installed
    only because `meshtastic` depends on it; it is not listed in `requirements.txt`.
  - Today the bridge opens a serial port (search for `SerialInterface` in `meshllm/bridge.py`; `--port auto|COMx`). Add a connection choice
    (`--tcp HOST`, `--ble ADDRESS`, still defaulting to USB auto-detect) and keep the reconnect behaviour.
  - Choosing or scanning from the dashboard adds a web route that changes which radio the bridge talks to. That is an `admin` route (see
    item 3) and scanning is a host-side action, so build the command-line options first and the dashboard part together with or after item 3.
  - Bluetooth is host-only: on Linux it needs BlueZ and D-Bus and will not work inside a container (item 4).
  - Done when: each mode connects, drops and reconnects in tests with the interface classes faked; the docs explain each mode. A
    real-hardware check needs a radio with Wi-Fi or Bluetooth enabled; ask the owner first.
- [ ] **3. Headless / LAN mode with a login** (so a phone or another PC can use the dashboard). **Needs an independent security review before merge.**
  - **Behaviour today to preserve:** on a loopback bind with no password configured, nothing changes (no login). A password becomes mandatory
    on any non-loopback bind (`0.0.0.0`, `::`, or any address outside `127.0.0.0/8` and `::1`); the bridge refuses to start without one.
  - **Host and Origin:** today `_host_ok` in `meshllm/webui.py` accepts only loopback names or a Host equal to `--web-host`, so binding to
    `0.0.0.0` and browsing by LAN IP gets a 403. Replace it with an explicit allowlist (`--allowed-host NAME`, repeatable; required, with a
    clear error, on a non-loopback bind). Compare the full Origin (scheme, host and port) with the request's own. Parse bracketed IPv6 hosts correctly.
  - **Passwords:** `hashlib.scrypt` with n >= 2**15, r = 8, p = 1, a 16-byte random salt, a 32-byte key and `maxmem` raised enough to allow it
    (the default limit rejects those parameters); store the parameters with the hash so they can be raised later; compare the derived key with
    `hmac.compare_digest`. Never read a password from the command line or an environment variable. `python -m meshllm --set-password`
    prompts without echo and writes the hash to a mode-600 file (or the settings database); non-interactive setups pass a hash through
    `--password-hash-file PATH` (or `MESHLLM_PASSWORD_HASH_FILE` pointing at a file).
  - **Sessions:** a random 256-bit token from `secrets`; keep only a hash of it server side (in memory is fine: a restart logs everyone out); idle
    and absolute expiry; new token on login (no fixation); invalidate on logout and on every password change. The cookie is `HttpOnly`,
    `SameSite=Strict`, `Path=/`, no `Domain`, and `Secure` when TLS is on.
  - **Throttling:** per-source and global exponential backoff on failed logins, never a hard lockout a stranger could use against the owner,
    no difference in responses that reveals anything. Behind a reverse proxy every client shares the proxy's address: do not trust
    `X-Forwarded-For` unless a `--trusted-proxy` is configured.
  - **CSRF:** once a cookie session exists, a POST needs a matching Origin (an absent Origin is refused) **and** a CSRF header or token;
    parse `Content-Type` properly (the media type must be `application/json`, not a substring match). The login POST passes the same Host,
    Origin and JSON checks.
  - **Roles, default-deny:** every route is tagged `public`, `viewer` or `admin` in `meshllm/webroutes.py` (the decorator takes the role; the
    default is `admin`). A test enumerates `GET`, `POST`, `GET_RE` and `POST_RAW` and fails on any untagged route. A viewer gets 403 on every
    `admin` route. Admin-only: everything that transmits (`/api/send`, `/api/channel/post`, `/api/traceroute/request`, telemetry requests),
    everything that writes to the radio (position, time, settings), access rules, backups (including the raw restore upload), clears and
    prunes, and model pull or switch. Viewers do not get backup downloads, CSV exports, logs, diagnostics or reports (they hold message text)
    unless the owner opts in; test each. Note `/tiles/...` makes the bridge fetch from an outside tile server and fill a size-capped cache.
  - **The AI:** a viewer may use `/api/ai/ask` only while it stays log-only (tier 0, no tool that transmits). Add a test that a viewer
    session cannot cause any `sendText` or `sendData` on the radio, directly or through the AI, including with mesh text that contains
    instructions. Keep the Content-Security-Policy and add `frame-ancestors` and a `Referrer-Policy`.
  - **TLS:** optional `--tls-cert` and `--tls-key`, plus a documented reverse-proxy setup. Without TLS a LAN login sends the password in
    clear and the `Secure` flag cannot be used: document it as an owner-accepted risk and show a warning on the login page whenever the
    connection is not TLS and the peer is not loopback.
  - **Docs that become false and must change in the same PR:** the "no login" lines in `README.md`, `docs/flags.md` (the `--web-host 0.0.0.0`
    paragraph) and `SECURITY.md`, and the "web panel password" idea in `docs/roadmap.md`.
  - Done when: every rule above has a test, an independent security review is clean, and the docs match.
- [ ] **4. Docker and Docker Compose.** Depends on items 2 and 3.
  - An image for the bridge, plus a Compose file with an Ollama service and a volume for the database. Run as a numeric non-root user and make
    sure the database volume is writable by it. Pin the base image by tag **and digest**, and add Dependabot's Docker ecosystem to keep it current.
  - The password hash comes from a Compose `secrets:` file (item 3), not from an environment variable (those show up in `docker inspect`).
    Publish the dashboard port on one interface only (loopback by default, the LAN as an explicit opt-in) and do not publish Ollama's port.
  - USB serial passthrough (`devices:`) works on Linux hosts only; on Windows and macOS use the Wi-Fi/TCP radio path (item 2). Bluetooth does not work in a container.
  - CI builds the image (build only; nothing is pushed to a registry until the owner decides).
  - Done when: `docker compose up` gives a working dashboard behind the login with a faked or real radio, the docs explain each platform.
- [ ] **5. Afterwards**
  - A whole-project security and bug pass: static analysis, a dependency scan, a secret scan over the full history, a review of every web route and of the installer scripts.
  - Install `qwen3.5` and re-run the tool-choice evaluation next to `llama3.2:3b` (the weak spot is choosing the right `mesh_report` topic).
  - Run the full over-the-air loop again after each change that touches the radio path (the owner has a test radio).

## Not planned

Crew or jobsite features (see Scope), anything that lets the AI transmit or change the radio on its own, and cloud services.

## Status

As of this commit everything else is merged and green: the `meshllm` package, the mesh-only AI tools, the dashboard, the test suite (run
`python run_tests.py` for the current count; CI runs it on Python 3.9, 3.10, 3.12 and 3.13), and the review workflow. Known smaller gaps are in
[docs/roadmap.md](docs/roadmap.md).
