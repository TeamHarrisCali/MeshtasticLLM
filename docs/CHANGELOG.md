# Changelog

All notable changes to this project are written here, newest first, in the style of [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The version number follows [Semantic Versioning](https://semver.org/): while it is `0.x`, anything may change between minor versions.
The version itself lives in one place, `meshllm/__init__.py`; the release workflow refuses a tag that does not match it and publishes
the section below that matches the tag as the release notes ([releasing.md](releasing.md)).

## [Unreleased]

### Added

- **An update check and an in-app update** (Settings > Updates, admin only). The check looks at this repository's published GitHub Releases, is **off by default** (turn it on in Settings; "Check now"
  always works) and sends nothing but one HTTPS request. A git checkout can then update itself from the dashboard: a database backup first, a fast-forward to the release tag (only if it is on `main`,
  the working tree is clean and the checkout is on a branch), `pip install -r requirements.txt` if that file changed, a check that the new version starts, a rollback if either fails, and a restart
  of the bridge where that is safe. Docker, the downloadable program and other installs are told that a release exists and how to update by hand; they never update themselves
  ([setup.md](setup.md#updating)). Only tried against a local fake of GitHub: no real release has been through it yet.

## [0.1.0] - 2026-10-04

The first version: everything the project does today. It is a hobby project written by AI under a human maintainer's direction
([README](../README.md#how-this-is-built)); read [Not tried on real hardware](#not-tried-on-real-hardware) below before relying on it.

### Added

- **The bridge.** A direct message `/ai <question>` to your Meshtastic radio is answered by a local Ollama model; replies are split into
  numbered parts that fit a LoRa packet, sent with acknowledgements and re-sent when the radio reports a failure. Per-node memory, a queue,
  access control (open or allowlist, a daily cap per node, a block list) and a pause switch.
- **A fixed menu of read-only mesh lookups for the AI** (nodes, batteries, sensors, signal, quiet nodes, history, traceroutes), enabled per
  node and only for a verified sender; the AI cannot transmit, change the radio or touch files ([ai_tools.md](ai_tools.md)).
- **The web dashboard** on `127.0.0.1:8080`: Home, Nodes, Map (optional OpenStreetMap background through a local tile cache), Coverage and
  walk test, Activity, Trends, Traceroute, Telemetry, Radio settings (view, back up, validated write and restore), Data, Report, Diagnostics,
  Settings, the public channel page, direct messages, AI conversations, AI log and an Evaluation page ([dashboard.md](dashboard.md)).
- **Connections:** USB (found and re-found automatically), Wi-Fi (`--tcp`), Bluetooth (`--ble`, `--ble-scan`) and automatic failover between
  them (`--fallback`), with a watchdog for a link that went silent ([setup.md](setup.md)).
- **Demo mode** (`--demo`): a simulated radio and mesh with a built-in scripted model, so the dashboard works with no hardware and no Ollama.
- **A login for the dashboard** on any non-loopback address: two accounts (admin, viewer), scrypt password hashes, sessions, throttling,
  CSRF protection, optional TLS, an allow-list of host names ([setup.md](setup.md#use-the-dashboard-from-a-phone-or-another-computer-lan-login)).
- **Docker:** a `Dockerfile` and `docker-compose.yml` with its own Ollama, `./setup.sh --docker` as the one-command start, a Compose-secret
  login override and USB passthrough on Linux hosts.
- **Backups and upkeep:** a daily database copy, manual backups, restore at the next start, an audit log of every question and answer.
- **The installer:** `setup.sh` / `setup.bat` build a private Python environment, check Ollama and the radio, and can set up start at login.
- **An evaluation harness** that scores how reliably a model picks the right tool, with separate development and held-out question sets.
- **Releases and downloadable programs** (this version): `python -m meshllm --version`, the version in the dashboard (sidebar, `/api/status`
  and Diagnostics), and `--data-dir` / `MESHLLM_DATA_DIR` to choose where `audit.db`, backups, the map cache and logs live. A packaged program
  for Linux, Windows and macOS is built by CI (`scripts/build_binary.py`, `.github/workflows/package.yml`) and the release workflow publishes
  it with checksums ([releasing.md](releasing.md)). A packaged program keeps its data in a per-user folder instead of next to the program.

### Not tried on real hardware

The automated tests fake the radio and Ollama. What was tried for real is listed in the [README status](../README.md#status) and
[roadmap.md](roadmap.md#tested-only-against-fakes); in short:

- USB and Bluetooth were tried on one radio (a Heltec V3), including USB-to-Bluetooth failover and recovery after a reboot.
- Wi-Fi/TCP, setup on macOS, Docker Desktop, a radio from inside a container and the LAN login through a real reverse proxy were not.
- The packaged Windows and macOS programs are only smoke-tested in CI (they start, serve the dashboard in demo mode and find their bundled
  files); no packaged program has been run against a real radio. The programs are not signed, so Windows and macOS will warn before running them.

[Unreleased]: https://github.com/TeamHarrisCali/MeshtasticLLM/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/TeamHarrisCali/MeshtasticLLM/releases/tag/v0.1.0
