# Command-line flags

`--cooldown 20` seconds per node between requests, `--max-chunks 4` messages per AI reply,
`--max-tokens 150` model output cap, `--command /ai` trigger word, `--web-port 8080`,
`--memory-turns 6` (0 turns memory off), `--memory-hours 24` (0 = never expire),
`--memory-chars 3000`, `--num-ctx 4096`, `--no-log-inbound`, `--no-web`, `--max-queue 5`,
`--queue-ttl 600`, `--no-queue-notice`, `--access-mode open|allowlist`, `--daily-cap N`,
`--confirm-seconds 60`, `--chunk-bytes 160`, `--send-retries 2`, `--retry-delay 10`,
`--port auto|COMx`, `--probe-unknown`, `--scan-interval 2`, `--reconnect-hold 60`,
`--model NAME` (optional; overrides and saves the model chosen in the UI), `--no-telemetry`,
`--telemetry-retention-days 30`, `--telemetry-passive-gap 60`, `--no-mesh-stats`, `--mesh-sample-interval 300`,
`--traceroute-timeout 60`, `--traceroute-cooldown 30`.
`--web-host 0.0.0.0` exposes the dashboard - including the send box - to your LAN. There is no login,
so only do that on a network you trust.
`--no-warm-up` (don't load the model into Ollama's memory at start-up; by default it is loaded and kept ready for 30 minutes after each question).
