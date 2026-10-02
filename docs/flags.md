# Command-line flags

`--cooldown 20` seconds per node between requests, `--max-chunks 4` messages per AI reply,
`--max-tokens 150` model output cap, `--command /ai` trigger word, `--web-port 8080`,
`--memory-turns 6` (0 turns memory off), `--memory-hours 24` (0 = never expire),
`--memory-chars 3000`, `--num-ctx 4096`, `--no-log-inbound`, `--no-web`, `--max-queue 5`,
`--queue-ttl 600`, `--no-queue-notice`, `--access-mode open|allowlist`, `--daily-cap N`,
`--confirm-seconds 60`, `--chunk-bytes 160`, `--send-retries 2`, `--retry-delay 10`,
`--port auto|COMx`, `--probe-unknown`, `--tcp HOST[:PORT]`, `--ble ADDRESS_OR_NAME`, `--ble-scan`, `--scan-interval 2`, `--reconnect-hold 60`,
`--model NAME` (optional; overrides and saves the model chosen in the UI), `--no-telemetry`,
`--telemetry-retention-days 30`, `--telemetry-passive-gap 60`, `--no-mesh-stats`, `--mesh-sample-interval 300`,
`--traceroute-timeout 60`, `--traceroute-cooldown 30`.
`--web-host 0.0.0.0` exposes the dashboard - including the send box - to your LAN. There is no login,
so only do that on a network you trust.
**How the radio is reached** (pick one; the default is USB serial with auto-detect):
`--port auto|COMx|/dev/ttyUSB0` (USB serial; `auto` finds the radio by its chip and follows it if the port number changes),
`--tcp HOST[:PORT]` (Wi-Fi: a radio on your network, by IP address or host name; the port is 4403 unless you give one; IPv6 as `[addr]:port`; **not yet tested on real hardware**),
`--ble ADDRESS_OR_NAME` (Bluetooth LE, host only). `--tcp`, `--ble` and a pinned `--port` cannot be combined (`--port auto` is the
default and may be written out). `--ble-scan` prints the nearby Meshtastic Bluetooth radios (name and address) and exits without starting the
bridge; it takes about ten seconds. `--probe-unknown` applies to USB auto-detect only. `--scan-interval` is how often the link is checked in
every mode, and a failing Wi-Fi or Bluetooth connection is retried only every 15 to 60 seconds. `--reconnect-hold` applies to all three.
`--tcp`, `--ble` and `--port` are ignored in `--demo`.
`--no-warm-up` (don't load the model into Ollama's memory at start-up; by default it is loaded and kept ready for 30 minutes after each question).

`--demo` runs the whole bridge and dashboard with a simulated radio and mesh: no radio and no Ollama needed, nothing transmitted, and a
temporary database, map-tile cache, backups and log folder (deleted when you stop it with Ctrl+C, `kill` or by closing the terminal) instead of
the real `audit.db`. `--port`, `--tcp`, `--ble`, `--db` and `--model` are ignored in demo mode (a one-line note says so if you give them), and a `--web-host`
other than `127.0.0.1`, `localhost` or `::1` is refused: the demo has a send box and settings pages and must not face a network. The banner
says where the temporary folder is. It implies `--no-warm-up`, uses the scripted `demo-scripted` model unless a real Ollama with a
tool-capable model is reachable at `--ollama-url`, and the web UI stays on `--web-port`. With it, `--demo-speed 5` makes the simulated
traffic and the fake nodes' `/ai` questions run five times as fast (useful for trying things out), and `--demo-scripted` always uses the
built-in scripted model even if Ollama is running. See [the README](../README.md#try-it-without-hardware).
