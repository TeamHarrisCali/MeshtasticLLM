# TODO: what to build next

This file is the hand-off for whoever (person or AI agent) picks the project up next. Work top to bottom. Each item is its own
branch and pull request, reviewed by someone who did not write it, and merged only when CI is green; see
[CONTRIBUTING.md](CONTRIBUTING.md). Items 2 to 4 depend on each other as noted under each.

## Scope

This is a **hobby project for one person's own radios**. It is not a fleet, crew or jobsite tool: no check-ins for other people's
radios, no crew groups, no job calculators. The AI stays read-only on a fixed tool menu and never transmits on its own
([docs/roadmap.md](docs/roadmap.md) has the design rules).

## Queue

- [x] **1. Demo mode** (`python -m meshllm --demo`): done
  - [x] A simulated mesh (a fake radio with a few dozen nodes, positions, batteries, sensors, links, traffic and a couple of scripted
    `/ai` conversations) so anyone can try the dashboard with no hardware, and a built-in scripted model so it also works without
    Ollama (use the real Ollama model instead when one is available).
  - [x] The radio fake in `tests/fixture.py` is thin (four nodes, almost no batteries, sensors, links or traffic) and lives outside the
    package. Demo mode needs its own richer fake inside `meshllm/`. Use obviously fake names and ids. Demo mode uses a temporary
    database and never touches the real `audit.db`.
  - [x] Then take README screenshots of the demo dashboard (Home, Nodes, Map, AI log) into `docs/screenshots/` and reference them from the README.
  - Done when: `--demo` starts with nothing plugged in, every dashboard page renders, tests cover it.
- [ ] **2. Connect over Wi-Fi and Bluetooth, not only USB**
  - [x] The `meshtastic` library has `TCPInterface` (a radio on the LAN, by host name or IP) and `BLEInterface` (by address). `bleak` is installed
    only because `meshtastic` depends on it; it is not listed in `requirements.txt`. (BLE is imported lazily; see `meshllm/connection.py`.)
  - [x] Today the bridge opens a serial port (search for `SerialInterface` in `meshllm/bridge.py`; `--port auto|COMx`). Add a connection choice
    (`--tcp HOST`, `--ble ADDRESS`, still defaulting to USB auto-detect) and keep the reconnect behaviour. (Done, plus `--ble-scan`.)
  - Choosing or scanning from the dashboard adds a web route that changes which radio the bridge talks to. That is an `admin` route (see
    item 3) and scanning is a host-side action, so build the command-line options first and the dashboard part together with or after item 3.
  - [x] Bluetooth hardening from the first hardware runs: unfiltered scan (the library's filtered one crashed bluetoothd), direct connect by MAC
    address (a radio the OS already holds does not advertise), a log line per phase and a 90 s limit on the whole connect.
  - [x] Bluetooth is host-only: on Linux it needs BlueZ and D-Bus and will not work inside a container (item 4). (Documented in `docs/setup.md`.)
  - [x] Done when: each mode connects, drops and reconnects in tests with the interface classes faked; the docs explain each mode.
  - [x] Real-hardware check, USB and Bluetooth: USB hot-plug, Bluetooth connect (with and without the OS holding the radio) and recovery after
    a radio reboot were verified on a Heltec V3. Bluetooth needs a PIN and a Low Energy bond (see docs/setup.md).
  - [ ] Real-hardware check, Wi-Fi/TCP: still unverified; needs a radio with Wi-Fi enabled (ask the owner first).
  - [x] Automatic failover between connections (`--fallback KIND:VALUE`, repeatable, in priority order after the primary): carry on over
    Bluetooth or Wi-Fi when USB is unplugged and switch back to USB after it has been listed for 10 s; one transport held at a time; docs in
    `docs/setup.md` (Failover). Done in `FailoverChain` (`meshllm/connection.py`), tests in `tests/test_failover.py`.
  - [x] Real-hardware check, failover (USB with a Bluetooth fallback, Heltec V3): unplugging USB switched to Bluetooth, replugging switched back after the
    10 s window, and the same radio was not reported as a different one. Wi-Fi as a fallback is still untested.
- [x] **3. Headless / LAN mode with a login** (so a phone or another PC can use the dashboard). **Needs an independent security review before merge.** Built on
  branch `feat/lan-login`; every box below is ticked only where the code and a test exist. Code: `meshllm/websecurity.py`, `meshllm/passwords.py`, `meshllm/webui.py`,
  `meshllm/webroutes.py`; tests: `tests/test_websecurity.py`. The box for the review itself stays open until a reviewer has signed it off.
  - [x] **Behaviour today preserved:** a loopback bind with no password configured has no login and behaves as before (the existing tests pass unchanged apart from the Host-name
    rule below). A password is mandatory on any non-loopback bind (`0.0.0.0`, `::`, any address outside `127.0.0.0/8` and `::1`, or a host name other than `localhost`); the bridge refuses to start without one.
    The one exception is the Docker image (`MESHLLM_CONTAINER=1`, `0.0.0.0`, no password configured), which keeps its loopback-published, no-login behaviour. Demo mode ignores passwords.
  - [x] **Host and Origin:** `_host_ok` is replaced by an allowlist (`--allowed-host NAME`, repeatable, required with a clear error on a non-loopback bind; `localhost`, `127.0.0.1`, `::1`
    always accepted); bracketed IPv6 is parsed and normalised. The Origin must be exactly this page's scheme, host and port (an absent Origin is refused once a login exists; `Sec-Fetch-Site`
    other than same-origin is refused when sent). *Behaviour change:* a named `--web-host` is no longer accepted as a Host by itself; it has to be in `--allowed-host`.
  - [x] **Passwords:** `hashlib.scrypt` n = 2**15, r = 8, p = 1, 16-byte salt, 32-byte key, `maxmem` raised, parameters stored in the hash line, `hmac.compare_digest`. `python -m meshllm --set-password [--role viewer]`
    prompts with `getpass` (twice, no echo, at least 12 characters, a terminal is required) and writes a mode-600 file atomically; `--password-hash-file` / `MESHLLM_PASSWORD_HASH_FILE` (a path) points at it.
    Not done: the "or the settings database" alternative (a file only), and automatic re-hashing at login when the defaults are raised (old hashes verify; `--set-password` again upgrades).
  - [x] **Sessions:** 256-bit `secrets` token, only its SHA-256 kept in memory, 2 h idle and 12 h absolute expiry (flags), a new token at every login (an old cookie presented at login is destroyed), invalidated at logout and
    whenever the hash file changes (a running bridge re-reads it), at most 64 live per account (a viewer cannot sign the admin out). Cookie `HttpOnly`, `SameSite=Strict`, `Path=/`, no `Domain`, `Secure` (and the `__Host-` name) over TLS or behind a trusted proxy that says https.
  - [x] **Throttling:** per-source (IPv4 address or IPv6 /64) exponential backoff (1, 2, 4 ... capped at 300 s) plus a gentler global one (10 free failures, then up to 60 s) that a browser holding the admin's 30-day device cookie skips (the per-source wait always applies); no permanent lockout; while throttled the password is not even tried;
    wrong password, unknown account and an unconfigured viewer give an identical 401 (a password hash is computed in every case); at most two hashes in flight. `X-Forwarded-For` / `-Proto` are believed only from a `--trusted-proxy`.
  - [x] **CSRF:** with a login, every POST needs a matching Origin **and** the session's `X-CSRF-Token` (an HMAC of the session id, read from `GET /api/session`); `Content-Type` is parsed as a media type
    (`application/json`; the raw upload needs `application/octet-stream`); the login POST passes the same Host, Origin and JSON checks; the body is read only after all of that.
  - [x] **Roles, default-deny:** every route carries `PUBLIC`, `VIEWER` or `ADMIN` (decorator argument, default `ADMIN`); an untagged function put straight into a table is still treated as admin at run time. `tests/test_websecurity.py`
    enumerates `GET`, `POST`, `GET_RE` and `POST_RAW` against a reviewed table (an untagged, mistagged or new route fails it) and checks every route as anonymous, viewer and admin. Admin-only: everything that transmits, writes to the
    radio, access rules, backups (including the raw restore upload), clears, prunes, notes, snippets, units, model pull or switch, pause. Viewer-readable but closed unless `--viewer-exports`: CSV exports, logs, diagnostics, reports.
    Backup downloads are admin-only even with `--viewer-exports`. `/tiles/...` is a viewer route (a size-capped cache; the owner can say otherwise by changing one tag).
    Not done: the viewer's page still shows admin buttons (they answer with an error); only the pause button is disabled.
  - [x] **The AI:** a viewer may use `/api/ai/ask`; it is queued at tier 0 as before. Tests: no tier-0 tool handler mentions anything that transmits; a jailbroken fake model that asks for `sendText`, a made-up send tool and the confirmed tool,
    with orders planted in a node name and a stored message, gets "denied" and nothing reaches the radio or the outbox; the admin's send box in the same harness does transmit (so the test can see a transmission). The Content-Security-Policy
    is kept with `frame-ancestors 'none'`, `base-uri`, `form-action`, plus `Referrer-Policy: no-referrer`, `X-Frame-Options`, and cross-origin opener/resource policies.
  - [x] **TLS:** optional `--tls-cert` / `--tls-key` (TLS 1.2+, the handshake runs in the connection's own thread under a time limit), a documented reverse-proxy setup (`docs/setup.md`), and a warning on the login page whenever the
    connection is not TLS and the peer is not loopback. No HSTS is sent (a self-signed certificate plus HSTS would lock a browser out). Without TLS the clear-text password is documented as an owner-accepted risk.
  - [x] **Docs:** README, `docs/flags.md`, `SECURITY.md`, `docs/roadmap.md`, `docs/setup.md`, `docs/dashboard.md`, `docs/files.md` updated.
  - Done when: every rule above has a test, an independent security review is clean, and the docs match.
  - [x] **Independent security review** done (REQUEST CHANGES; every finding was then fixed and covered by tests and mutation checks by the builder's side, but the fixes were not re-reviewed by the reviewer: a viewer could evict the admin's session; a stranger could hold the global wait on the owner; plus a loopback Host edge case, viewer status redaction, a 64-connection cap, `__Host-` cookies and test gaps). Left as follow-ups: the tile cache's per-tile lock table grows without bound and a viewer can make the owner's address fetch many tiles (`meshllm/tiles.py`; needs a per-session fetch budget); a slow client can still hold one of the 64 connections for a long time; a viewer shares the admin's web-console AI conversation.
  - Things the builder could not check: a real browser through a real reverse proxy; real hardware; long-run memory use of the in-memory tables; Python 3.9 on this machine (CI runs it).
  - Known gaps, honestly: two shared accounts only (no per-person logins, no 2FA, no browser password change); a stranger with several addresses can keep NEW browsers at the global wait (the owner's usual browser is exempt); sessions are in memory (a restart signs everyone out);
    the login page only shows the clear-text warning, it does not refuse; `Secure` cookies and the Origin scheme behind a proxy rely on `X-Forwarded-Proto` from a `--trusted-proxy`.
- [ ] **4. Docker and Docker Compose.** Depends on items 2 and 3. Everything below is ticked only where the files and a test exist; what could not be tried is listed under "Done when".
  - [x] An image for the bridge (`Dockerfile`), plus a Compose file with an Ollama service and a volume for the database. Runs as a numeric non-root
    user (10001) and the `/data` volume is writable by it. The base image is pinned by tag **and digest**, and Dependabot's Docker ecosystem keeps it current.
    There is also a `demo` profile (`docker compose --profile demo up demo`) that needs no radio and no Ollama.
  - [x] The password hash comes from a Compose `secrets:` file (`docker-compose.login.yml`, path in `MESHLLM_ADMIN_HASH_FILE`), not from an environment variable (those show up in `docker inspect`; checked with a real
    `docker compose up`). The unreadable-secret problem (a Compose file secret keeps the host file's owner and mode, so the `600` file from `--set-password` is unreadable by uid 10001; Compose ignores `uid`/`gid`/`mode` for file secrets) is solved by
    the entrypoint: the container starts as root with only `DAC_OVERRIDE`, `SETUID` and `SETGID`, copies the hash into a 0400 file owned by 10001 on a memory-only tmpfs (`/run/meshllm`), drops to 10001 and re-runs itself;
    the bridge then runs as 10001 with no capabilities, the root file system stays read-only. Trade-off and the rejected host-side `chown 10001` alternative: `docs/setup.md`. Only the admin account is wired (no viewer file in Docker); changing the password needs `docker compose restart bridge`.
  - [x] The healthcheck works with a login: it now asks `/api/session` (public and data-free) instead of `/api/status` (401 under a login), so no new route was added. It is plain HTTP, so it does not fit `--tls-cert` inside the container.
  - [x] Dashboard port published on the host loopback only (`127.0.0.1:8080`) and Ollama's port not published, with a warning in the compose file and the docs.
  - [x] The LAN as an explicit opt-in for the published address: `MESHLLM_WEB_BIND` plus `MESHLLM_ALLOWED_HOSTS` in `.env`, only together with the login. The default publish stays `127.0.0.1`. The container refuses to start (exit 78, clear message) on a non-loopback
    publish address without the login or without allowed hosts, and never for `--demo`; the entrypoint also sets `MESHLLM_PUBLISH_LAN=1` so the app's own check refuses a login-less wildcard bind. Not covered by design: a hand-edited `ports:` line or `docker run -p 0.0.0.0:...`.
  - [x] USB serial passthrough (`docker-compose.usb.yml`, Linux hosts only); on Windows and macOS use the Wi-Fi/TCP radio path (item 2); Bluetooth does not work in a container. Documented in `docs/setup.md`.
    The container, the image and the demo were tried on Linux; USB and Wi-Fi from inside a container have **not** been tried with a real radio, and Docker Desktop not at all.
  - [x] CI builds the image, smoke-tests the demo container and the login override (healthy with a login, sign-in through the published port, no hash in `docker inspect`, PID 1 uid 10001 with no capabilities, LAN publish refused without a login); nothing is pushed to a registry.
  - [x] Independent security review of the login-in-Docker change (the root-then-10001 copy and the LAN rule): APPROVE WITH NITS; the one real finding (`--demo` through `MESHLLM_EXTRA_ARGS` on a LAN publish) and the wording nits were fixed and tested, not re-reviewed.
  - [ ] Done when: `docker compose up` gives a working dashboard behind the login with a faked or real radio, the docs explain each platform. Open: the login was proven with no radio attached (the demo profile ignores passwords, so there is no faked-radio login);
    a real radio from inside a container (USB, Wi-Fi), Docker Desktop on Windows/macOS, rootless Docker, and a real peer on a LAN (how the client address looks through Docker's proxy) have not been tried.
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
