# What the bridge does

- **Per-user memory.** The model is given each node's own recent `/ai` conversation, looked up by
  node ID, so users never see each other's history. Defaults: last 6 exchanges, 3000 characters,
  forgotten after 24 h idle. A user can send `/ai reset` to wipe it; you can clear it per node in
  the web UI. Clearing memory never deletes the audit log.
- **AI log** (was the Audit tab). Every message, with status, model time, signal and per-message delivery
  (acked / relayed / failed). Search, filter, CSV export, and a pause switch.
- **Direct messages and AI conversations.** Two pages with the same layout. *Direct messages* is for talking to a person yourself: a chat
  per node, a box to DM them (sent as you, not the AI; it is added to that node's AI context, labelled as coming from the operator), a
  "new chat" box for any node name the radio has heard or an ID like `!1a2b3c4d`, and your saved snippets. *AI conversations* shows what
  each node asked the AI and what it answered, with the node's AI access, daily use and a button to clear the AI's memory of it.
- **Public channel page.** Read what people post on the default public channel (the radio's primary channel, LongFast with
  the default key) and post there yourself. Posts are typed by you only and go out as one broadcast of up to 200 bytes
  (never split), at least 5 s apart and at most 30 an hour, because everyone in range hears them. Each of your posts shows
  whether the radio sent it and whether a neighbour was heard repeating it. **The AI is kept out entirely:** it never reads
  the channel, a `/ai ...` typed on it gets no answer, nothing on the channel is put into the model's prompt, and no AI tool
  can post. Only the primary channel is recorded (private channels never are); messages are kept on this PC for 30 days,
  shown on the Data page, and can be cleared on the page. If the primary channel isn't using the default key, the page says so.
- **Queue.** Questions are answered one at a time. Someone who has to wait is told their position and a
  rough time ("Queued (#2, about 40s)"); a node can have only one question pending; when the queue is
  full (`--max-queue 5`) new questions get "Busy". The AI log shows the live queue with a Cancel button
  for waiting questions. Questions still waiting when the bridge stops are picked back up on restart if
  they're under 10 minutes old (`--queue-ttl`), otherwise marked expired. Replies already waiting to be
  sent over the radio are not saved across a restart.
- **Access control (Access tab).** Two modes: *open* (anyone except nodes you block) or *allowlist* (only
  nodes you mark Allowed). Each node can also have its own daily limit (rolling 24 h; blank = follow the
  default, 0 = unlimited). Blocked and not-allowed nodes get **no reply at all**; the attempt is logged at
  most once a minute per node. A node that hits its limit is told once an hour. Your own messages from
  the Direct messages page ignore these rules. Settings are saved in the database; `--access-mode` and
  `--daily-cap` override the saved values at startup.
- **Model tab.** Shows every model installed in Ollama with its size, parameters, whether it is loaded in
  memory, and whether it supports **tools** (needed for AI tools) or is embedding-only (can't chat).
  "Use" switches the model immediately (remembered across restarts; the new one is preloaded so the first
  answer isn't slow). You can also **download** any model from ollama.com by name, with a live progress bar,
  cancel, and Ollama's own error message if the name is wrong; only one download runs at a time. A model
  without tool support still chats normally - AI tools just aren't offered with it (the bridge falls back
  instead of erroring). Reasoning ("thinking") models such as qwen3.5 have thinking switched off, for chat
  and for the tool gate: radio replies are short, and with a small token budget a thinking model used to
  spend it all reasoning and return an empty answer. If a model still returns nothing, the bridge retries
  once and then tells the sender "The model gave no answer" and records an error instead of transmitting an
  empty message. Tool-choice reliability was measured on llama3.2:3b only, so the tab says so and
  points to `python -m meshllm.tools.eval_tools --model <name>`. Models are never deleted from the UI.
- **Layout.** A sidebar in four groups. **Messages:** Public channel, Direct messages, AI conversations. **Network:** Home, Nodes, Map,
  Coverage, Activity, Trends. **Tools:** Traceroute, Telemetry, Radio settings, Data, Report, Diagnostics, Settings. **AI:** Overview,
  Model, Access & actions, AI log, Evaluation. Every page has its own address (`#/nodes/!1a2b3c4d`), so refresh, back and links work;
  on a phone the sidebar becomes a scrolling bar. Search (press `/` or Ctrl+K) looks across everything. See `docs/dashboard.md` for
  what each page does. Nothing on the read-only pages transmits.
- **Home.** Tiles with sparklines (radio, nodes known, heard in the last hour, direct neighbours, channel use,
  our battery, AI questions, telemetry heard), alerts (radio unplugged, AI model unavailable, low batteries, a
  watched node gone quiet, a radio with a wrong clock), a **map of every node with a captured position**, new
  nodes, the closest nodes, **packets heard per hour by type** with the busiest senders, **mesh health over
  time**, mesh makeup (roles, hardware), sensors around you and an AI summary. The activity feed lives on its
  own page.
- **Nodes.** A searchable, sortable, filterable list on the left; everything about the chosen node on the
  right: live values, a **telemetry history chart** (pick battery, voltage, channel use, temperature, ...),
  a small map with distance from us, recent readings, hops/signal/key details, the last traceroute, and
  buttons to trace, message, see all telemetry or show it on the map. Nodes the radio has *forgotten* are kept
  in our database (`mesh_nodes`: name, hardware, last position, first/last seen, 90 days) and listed as
  "remembered".
- **Map.** Every node whose position we ever captured, whether the radio still lists it or not. Positions come
  from the radio's node list **and** from position broadcasts this bridge hears and stores itself. Filters
  (heard within, include remembered, names, fit every node, OpenStreetMap background), a selected-node card,
  and the list of nodes with no position. **Setting our radio's position:** "Set the position on a map" opens
  a pan/zoom OpenStreetMap view; click to choose the spot (or type coordinates and an altitude), confirm, and
  the bridge writes a *fixed position* to the radio (`Node.setFixedPosition`); "Remove the fixed position"
  undoes it. The radio then shares that position with the mesh on its normal schedule, so the dialog says so.
  It is an operator action only: the AI has no tool that changes the radio.
- **Radio settings** (Mesh group). Edit the settings stored on the radio itself. **Pull** reads what the radio
  reported when the bridge connected and saves it as a backup; the form (Device, Position, Power, Display, LoRa,
  Bluetooth, and the Telemetry, Neighbour info, Store & forward, Range test and External notification modules)
  is generated from the radio's own protobuf definitions, so every field is type- and range-checked (whole
  numbers, enum names, on/off, text of at most 200 printable characters; hop limit 0-7, tx power 0-30) and a
  request is all-or-nothing. **Save** writes only the sections that changed, inside one settings transaction so
  the radio restarts once, after first saving a backup of the settings being replaced; if the radio refuses,
  the bridge's copy is rolled back. **Restore** re-applies an earlier backup the same way (its values are
  re-validated; a backup can be downloaded as JSON). Fields that can cut you off the mesh or break the law
  (region, modem preset, tx power, role, tx enabled, ...) are flagged and the confirmation repeats the warning.
  **Never shown, saved or written:** Wi-Fi (password), security (private key, admin keys), MQTT (credentials),
  the Bluetooth PIN, and channels. Operator-only: the AI cannot reach it. Backups live in `radio_config_backups`
  (newest 40 kept). Code: `radio_config.py`; tests: `test_radio_config.py` (real protobuf objects, fake radio).
- **Sensors and the temperature unit.** Home's "Sensors around you" is built from the environment telemetry the bridge
  recorded (this PC's timestamps, so it doesn't depend on the radio's clock). It shows the **average** temperature,
  humidity and pressure over a window you pick (15 min, 1 h - the default, 6 h, 24 h; remembered in the browser),
  then each node's own average with how many readings and when it last reported. Across nodes it averages each
  node's own average, so a chatty node doesn't outweigh a quiet one; impossible values (e.g. humidity over 100%)
  are ignored. **°C / °F** is a button in the top bar; the choice is saved on the server and also used in the
  activity feed and in the AI's replies (`mesh_summary` now includes the 1 h sensor average, `node_info` the node's
  reading). Temperatures are stored in Celsius; only the display changes.
- **Map background and the local tile cache.** Ticking "OpenStreetMap background" (Map, Traceroute, the position
  picker; off by default) makes the maps ask *this bridge* for tiles (`/tiles/z/x/y.png`). The bridge fetches a tile
  from openstreetmap.org the first time somebody looks at it and keeps it in `tile_cache/` next to the database
  (capped at 500 MB, oldest deleted first; refreshed after 30 days; deleting the folder is always safe, or use
  "Delete them" on the Map page). So panning is smooth, places you have looked at load from disk and work offline,
  and **the browser itself never contacts openstreetmap.org** (the page's Content-Security-Policy now allows images
  from this bridge only). Only tiles actually on screen (plus one ring around it) are fetched: nothing is downloaded
  in bulk (the OpenStreetMap tile policy forbids that), at most four fetches run at once, a failing tile isn't
  retried for a minute, and the requests identify the app in their User-Agent. The maps also no longer flicker
  while you drag: each map keeps one tile layer and moves the tiles instead of recreating them, and the tiles of
  the previous zoom stay underneath until the new ones have arrived.
- **Distance unit.** A **km / mi** button in the top bar (next to °C/°F) switches every distance - node pages,
  closest nodes, the map scale bar (miles, or feet when zoomed in) - and the distances in the AI's replies. Saved
  on the server. Code: `tiles.py`, `test_tiles.py`.
- **Moving the maps.** Every map (Home, Map, a node's page, Traceroute) can be dragged to move it and zoomed with the
  scroll wheel, centred on the pointer; double-click zooms in, and +, - and a reset arrow (back to the automatic
  framing) are drawn on the map for touch screens. Your view is kept when the data refreshes, and a drag never
  counts as a click on a node. On a phone a sideways drag moves the map while an up/down swipe still scrolls the
  page. The position picker zooms at the pointer too.
- **Home order.** Temperature and humidity (averages, with each sensor node) are the first thing under the alerts,
  then the radio/mesh status tiles, the map, charts and the rest.
- **Ask the AI from the browser** (AI overview). A chat box that runs a question through exactly what a radio DM
  gets: the same queue, YES/NO gate, model, system prompt and read-only (tier 0) tools, including the mesh and PC
  tools, with per-chat memory and a "New chat" button. The answer is stored, not transmitted. Answers are as brief as
  a radio reply on purpose, so what you see is what a node would get. It is recorded in the AI log as "Web console"
  (a separate kind, so it never appears as a node in the conversation lists or Access and isn't counted in the radio
  statistics); the radio's daily cap and cooldown don't apply to you; confirmed (tier 1) actions are not offered.
- **Live facts in every prompt.** Each question reaches the model with a few real facts in front of it: the date and time on
  the PC, our radio's name and ID, the model name, how many nodes were heard in the last 15 minutes / hour / day (and how many
  directly), and the average temperature and humidity the sensor nodes report (in your chosen unit). So "what time is it?",
  "are you there?" and "is it humid out there?" are answered truthfully without a tool call or extra delay. Only numbers and
  our own radio's name go in: never the name of another node, which a stranger chooses and could word like an instruction.
  The system prompt also tells the model to say it can't tell rather than guess about anything not listed. Code:
  `format_context` / `Bridge.live_context`.
- **More tools over data we already hold** (all read-only, database only, nothing transmitted; offered to the same nodes as the
  other AI tools). `mesh_report` takes a topic: **sensors** (what the nodes report, averaged over 1, 6 or 24 h; in F or C),
  **signal** (best and weakest direct links, with distances), **quiet** (nodes gone silent for 1/6/24+ hours), **busiest** (the
  busiest and quietest hour of the day) and **activity** (packets, senders, telemetry, questions and new nodes in the last 1/6/24 h).
  `node_history` shows how one node's battery, voltage, temperature, humidity or channel use changed over 24 h, with the rate and,
  for a draining battery, roughly how long is left. `list_nodes` gained a role filter ("where is the nearest router / repeater").
  The tool and gate prompts, the tool descriptions and both evaluation sets (`eval_tools.py`, which now also checks a tool's
  *arguments*, e.g. `mesh_report|topic=sensors`) were extended to match.
- **Commands that need no model.** `/ai help` (or `?`) lists what to try; `/ai ping` is a range test that replies with the
  signal of the very message it received ("heard you directly: SNR 5.5 dB, RSSI -42 dBm", or "passed through 2 relays; the
  last hop was ..."); `/ai status` gives the model, queue, radio state and nodes heard. They answer instantly, work with the
  model down, don't count towards a node's daily limit, obey blocking, the allowlist and the pause switch, and work in the
  browser chat too. A packet with a malformed signal field is now handled instead of dropping the whole message.
- **Tool choice: a guess is never sent as a status report.** If the gate says a message is about live state (PC,
  bridge or mesh) but the model answers in words instead of calling a tool, the bridge asks once more telling it to
  use a tool; if it still won't, the reply lists what can be checked (145 bytes, one radio message) rather than the
  guess. This replaced an observed failure: qwen3.5 answered "how's the mesh doing?" with "12 nodes, 4 low on
  battery", numbers it invented. The gate and tool prompts and the tool descriptions were sharpened for the cases
  it missed (a node given by `!id`, a status question mixed with a request to change something). Evaluation:
  `python -m meshllm.tools.eval_tools --variant gated_retry` matches what the bridge now does; results in `eval_results/`.
- **Link-quality map** (Map page). Tick "Link quality" and a line is drawn from our radio to every node heard *directly*,
  coloured by its average SNR (green 5 dB or better, then yellow-green, amber, orange, red below -10 dB) and thicker the
  more packets it contributed; the map reframes on you and your neighbours. Below the map, **Direct neighbours** shows a
  signal-against-distance plot (one dot per neighbour, a bar from its worst to best SNR, the LongFast decode floor as a
  dashed line: the real range picture), and a table with SNR, RSSI, packets, distance and when each was last heard,
  over the last 24 h, 7 days or 30 days, with the farthest direct link named. Only direct packets count: for a relayed
  packet the radio measures the relay's signal, not the sender's. Endpoint: `/api/mesh/linkmap`.
- **Hops away is measured, not just reported.** The radio's "hops away" for a node is only as fresh as its last node-info
  push, so a neighbour could show as "2 hops" while packets were arriving directly. A node that delivered a packet with no
  relay in between (a real radio reception whose hop counters show nothing used) within the last 3 hours now counts as 0
  hops (direct) on the map, the Home counts and the node pages; after that it falls back to the radio's value. Links to
  nodes that share a spot are drawn side by side so one can't hide another.
- **Trends page.** *When is the mesh busiest?* - average packets heard per hour of the day (in your local time, the
  busiest hour highlighted, filterable by kind of packet) and an hour-by-hour heat map of the last 7 days (grey = nothing
  recorded); it sharpens as days accumulate (14 days of packet counts are kept). *How far packets travel* - packets per
  hour stacked by how many relays they had passed through (direct, 1, 2, 3+), over 24 h, 7 days or 30 days, with the
  average relay count, the share heard directly and the furthest seen. Endpoint: `/api/mesh/hops`.
- **More data, and a page that shows it.** Besides packet counts, telemetry and positions, the bridge now keeps, per
  hour: the **signal (SNR/RSSI) of each node heard directly** (average, best, worst; for a relayed packet the radio
  measures the relay, so those are left out) and **how many relays packets had passed through**; and a **position
  trail** per node (a point each time it is seen 15 m or more from the last). They appear as "Signal history" and
  the position trail on a node's page, a "Movement trails" option on the Map, and on the new **Data** page, which
  lists every dataset with its row count, date range, retention and a CSV export, plus the database and tile-cache
  sizes. Kept 30 days (signal, hops) and 90 days (trails); `--no-mesh-stats` switches the counting off.
- **Activity.** The feed that used to crowd the Home page: AI questions, direct messages, your messages,
  telemetry heard, traceroutes and **new nodes heard**, with kind filters, search and "load older".
- **AI overview.** Model status, questions in 24 h, success rate, average model time, queue, who can ask;
  recent questions, busiest askers, example questions to send, and the full action menu grouped as mesh
  questions / this computer / needs a code. Links to AI conversations, Model, Access & actions and the AI log.
- **Asking the AI about the mesh.** Read-only tools, answered from the database and never
  transmitting: `mesh_summary` ("how's the mesh?"), `list_nodes` (most recent, lowest battery, nearest,
  farthest) and `node_info` ("what's the battery on Ridge Repeater?"). They are offered to the same nodes as
  the other AI tools (pinned key + PKI DM + tier), and their answers go straight to the sender; node names
  (chosen by strangers) are stripped of control characters and never fed back to the model. **The tool
  prompt, gate prompt and evaluation sets changed with them, so the earlier accuracy numbers describe the old
  earlier menu, which also had five checks of the computer (disk, uptime, load, Ollama, bridge status). Those were removed so the AI only deals with the mesh; re-run `eval_tools.py` (dev and held-out now include mesh prompts) before quoting them.**
- **Swapping the radio.** Unplug it and plug in another and the bridge finds the new one by itself. If it is a
  different radio from last time (remembered across restarts, so a swap while the bridge was off is noticed too), a
  **"A different radio is connected"** banner appears on every page with a live checklist: give it a position, check
  its clock, let it learn the keys of nodes with AI tools (their pinned keys are kept; a pinned key the new radio
  hasn't seen yet reads "Key not seen yet" rather than "KEY CHANGED", and AI tools stay off until it has), and
  back up its settings. Items tick themselves as they're done; dismiss the banner when you like. The old radio's
  "wrong clock" verdict is thrown away, and **restoring a settings backup taken from a different radio asks for
  explicit confirmation** (the API answers 409 until told `force: true`), since it would copy that radio's LoRa and
  role settings onto this one. The old radio stays in your node list as a remembered node.
- **The radio's clock.** The clock is judged by the time the radio itself stamps on every packet it receives
  (`rxTime`), compared with when the packet reached this PC: the median of the last ten decides, within 10 minutes is
  fine, and a radio that has never been told the time sends packets with no stamp at all (detected after three).
  Only real radio receptions count (a packet relayed in over MQTT carries someone else's stamp), and absurd stamps
  are ignored. The node list's "last heard" values are **not** used as evidence: the library only refreshes them
  when the radio pushes a node-info update, so they are often old for a node you have just heard (an earlier version
  trusted them and raised false alarms; `/api/radio/clock` shows both signals side by side for diagnosis). When the
  clock really is wrong, only nodes this PC has heard itself show a last-heard time, the rest say "unknown", and Home
  offers **Set the radio's clock** (`Node.setTime`), which fixes it at the source.
- **Telemetry tab.** Nodes with the telemetry module on broadcast their battery, voltage, channel use and
  sensor readings every so often, and the radio hears them for free. **Every broadcast the radio hears is
  saved** in table `telemetry` with a timestamp, the node, what it reported (real columns for the common
  metrics, everything it sent as JSON in `raw`), and the signal and hop count it arrived with. Nothing is
  transmitted and nobody is asked (an earlier "ask a node" feature was removed: it cost airtime for data the
  radio gets anyway). The tab shows the latest values per node, a chart of any metric over time, the readings
  table, CSV export, and **storage controls**: "keep readings for N days" (default 30, 0 = forever) with a
  background job that deletes older rows once an hour, plus "Prune now". At most one reading per node and
  kind is kept per minute (`--telemetry-passive-gap`), duplicates heard via relays are dropped, and replies to
  requests aren't counted twice. "Record every node's broadcasts" is on by default; turn it off to keep only
  the nodes on the *watch list*. Flag: `--telemetry-retention-days N` (overrides and saves the setting).
- **Traceroute tab.** Pick a node (and a hop limit, default 7) and the bridge asks the mesh which way a packet
  takes to it **and back**. The reply lists every relay in order with the signal strength (dB) each hop
  received with; unreported relays show as "Unknown node". The route is drawn on a map: a green marker for
  your radio, blue for relays, red for the target, a solid line there and a dashed orange one back, and a
  dotted line where a stretch crosses a node whose position isn't known (only nodes that share their position
  can be placed; the hop list always shows the full route). The map is an offline drawing by default (Web
  Mercator, with a scale bar). Tick "Show an OpenStreetMap background" and this PC fetches the map
  tiles you look at (off by default; the choice is remembered in the browser; see "Map background" above). Every traceroute is
  saved - both paths, the signal strengths and the node positions *as they were then* - in table
  `traceroutes`, so past routes can be replayed on the map from the history list. Positions are location
  data: they're stored only for nodes on a traced route, and only in your local database. Guardrails: sent
  only when you click (never automatically), one traceroute at a time, the same
  node at most once per 30 s (`--traceroute-cooldown`), 60 s timeout (`--traceroute-timeout`).
- **Plain DMs are logged too.** A DM to the node that isn't `/ai ...` (e.g. someone replying to your
  message) appears in that node's conversation so you can see it. It is *never* sent to the model or
  remembered by it. Turn this off with `--no-log-inbound`.
