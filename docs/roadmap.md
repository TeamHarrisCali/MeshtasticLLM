# Roadmap and design notes

## Design rules that should not change

- **The AI never transmits on its own.** It can look things up in the bridge's database, and it can answer the node
  that asked. It has no tool that sends a message to another node, changes the radio, or touches the computer.
- **Anything from the mesh is untrusted input.** Node names are chosen by strangers, so they are stripped of control
  characters and never fed back to the model.
- **Tools are a fixed menu.** A new capability is a new entry in `ACTIONS` with a validated schema. If one ever changes
  something, it goes behind the tier-1 confirmation code, needs a verified (pinned-key, PKI) sender, and gets hard rate
  limits on airtime. The operator must always be able to pause everything.

## Tested only against fakes

These work in the test suite, which fakes the radio and Ollama, but have had little or no time on real hardware.
Reports and fixes are welcome.

- Setup on macOS and on a native Linux box with a radio attached (Windows and Ubuntu under WSL were exercised; start at
  login is covered as generated commands and files only).
- Telemetry requests (the wire format) and traceroute against a real node.
- Writing a position, the radio's clock, or a settings change back to a real radio and confirming it reconnects.
- Posting to the public channel from a real radio.

## Evaluation to do

- The tool menu is now mesh-only and has been measured once, on `llama3.2:3b` (see `evaluation.md`: dev 85%, held-out 70%).
  Repeat with a larger model (for example `qwen3.5`) and `--runs 3`, and record accuracy *and* latency. The weak spot is choosing the
  right `mesh_report` topic, so that is where better tool descriptions or a bigger model would show up.
- Measure how often a model invents radio or device status when asked a casual question (some models do).
- Repeat the tool-choice results for models other than `qwen3.5` before quoting them.

## Ideas

- A read-only tool for a node's stored telemetry history in a form people ask for ("what's the battery on X?").
- Telemetry retention controls in the UI (the setting exists as a flag).
- A mesh-wide link-quality map (colour links by SNR), comparing two traceroutes over time, GeoJSON/GPX export of the map.
- A "new node heard" entry on Home, per-node battery history on the node panel, "reboots seen" from uptime drops.
- A web panel password, if the dashboard is ever meant to be exposed beyond localhost (today `--web-host` defaults to
  loopback and the docs warn against exposing it).
- Replying to one person's post on the public channel in place; more than channel 0.
- Document Q&A with cited sources; scripts-folder actions with fixed arguments (carefully, behind tier 1).
- Notifications that work without the tab open.

## Deliberately not done

- Letting the AI send messages to other nodes, or change the radio's settings, channels, Wi-Fi, MQTT or keys.
- Tuning prompts to the held-out questions (that would turn the measurement into a memorised answer). One known
  miss, "Find me the closest repeater.", is left in the held-out set for that reason.
- A jobsite calculator of exact functions. It was considered after the usefulness audit and set aside.
