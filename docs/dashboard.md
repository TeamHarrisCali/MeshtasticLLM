# The dashboard, page by page

Open <http://127.0.0.1:8080/> (with a password configured you sign in first; a `viewer` account can look and ask the AI but not send or change anything, see [setup.md](setup.md#use-the-dashboard-from-a-phone-or-another-computer-lan-login)). The sidebar groups the pages; every page has its own address (`#/nodes/!1a2b3c4d`), so refresh, back and
links work. Press `/` or Ctrl+K to search everything. Only a few things on these pages can make the radio transmit, and each one is
something you press yourself: sending a direct message, posting on the public channel, a traceroute, setting the radio's position or
clock, and pushing radio settings. Nothing else transmits, and the AI can never do any of them.

With `python -m meshllm --demo` every page below is filled with a simulated mesh, and a **Demo mode** badge shows in the header: nothing is
transmitted and nothing is saved ([setup.md](setup.md#try-it-without-hardware-demo-mode)).

The sidebar has four groups. **Messages:** the public channel (read and post by hand, the AI is kept out), direct messages, and what people asked the
AI. **Network:** Home (alerts, sensors, map), Nodes, Map, Coverage (how far the radio reaches, walk test), Activity, Trends. **Tools:** Traceroute,
Telemetry, Radio settings (pull, edit and push the radio's config), Data, Report, Diagnostics, Settings. **AI:** Overview, Model, Access and tools,
the AI log, and Evaluation. The pages are described below.

## Messages

- **Public channel.** What people post on the default public channel, and a box to post yourself. One short broadcast per post (200
  bytes), spaced out. Your posts show whether the radio sent them and whether a neighbour repeated them. The AI is kept out entirely.
  Saved **snippets** above the box fill it with one click (you still press Post).
- **Direct messages.** Chats with people, one per node. Start a new one with any node name the radio has heard. Snippets work here too.
- **AI conversations.** What each node asked the AI and what it answered, with that node's AI access and daily use.

A number beside a page in the sidebar (and in the browser tab's title) means something new: posts on the channel (`@` means someone
used your radio's name), direct messages, and alerts on Home. **Settings** chooses what you are told about, and whether to play a sound
or show a desktop notification while the tab is in the background.

## Network

- **Home.** Headline numbers, alerts (radio unplugged, AI model unavailable, low batteries, a watched node gone quiet), the sensors
  around you (temperature and humidity averaged over a window you choose), your **starred nodes**, a map, and recent activity.
- **Nodes.** Every node the radio knows or has ever heard, with its signal, battery, position and history. On a node's page, **Your
  label and notes** lets you name it ("Lodge repeater"), add notes, and star it. Your label replaces its own name throughout the
  dashboard. These stay on this PC: never sent over the radio and never shown to the AI.
- **Map.** Every node with a position; pan and zoom; link quality coloured by signal; an optional OpenStreetMap background that this PC
  downloads as you look and keeps; set the radio's own position by picking a spot.
- **Coverage.** How far and how well this radio reaches, from signal and position data already collected. A map with the nodes heard
  directly and the furthest reach in each compass direction, signal against distance, and the **walk test**: pick a node that someone
  will carry around and every packet heard from it is logged with its signal and distance until you press Stop. It only listens.
- **Activity.** A feed of what happened: AI questions, messages, telemetry, traceroutes, new nodes.
- **Trends.** When the mesh is busiest by hour and day, and how many hops packets travel.

## Tools

- **Traceroute.** Trace the route to a node, on request only and rate limited, and draw it on a map.
- **Telemetry.** Battery, voltage, channel use and sensor readings every node broadcast, recorded passively; a watch list; CSV export.
- **Radio settings.** Pull the radio's configuration, edit it in a form, and push only what changed. Backups are taken first and can be
  restored. Keys, passwords and Wi-Fi/MQTT secrets are never shown.
- **Data.** Everything this bridge collects, how much, how long it is kept, and downloads.
- **Report.** A written summary of the last day, week or month: nodes, traffic, signal and range, sensors, the AI, the public channel
  (counts only) and things worth a look. Download it as Markdown, copy it, or print it. It never contains message text.
- **Diagnostics.** A health check that says what is wrong and what to do (radio, Ollama, disk, database, backups, errors, start at
  login), and the bridge's own log, which follows the end as it grows.
- **Settings.** Units, notifications, backups, storage and start at login.

## AI

- **Overview.** The model, what people asked, how it went, and a box to ask the AI from the browser (the same pipeline as over the radio,
  but the answer stays on screen).
- **Model.** Every model installed in Ollama, switching, and downloading new ones.
- **Access & actions.** Who may use the AI, daily limits, blocking, and the opt-in AI tools.
- **AI log.** Every message to and from the AI, with status, model time, signal, hops and delivery. Search, filter, CSV export.
- **Evaluation.** The saved results of measuring how well the model picks tools (development questions, and held-out questions that are
  never tuned to), models side by side, the usefulness audits, and the project's write-ups, including the demo script.

## Backups

**Settings > Backups** makes a copy of everything this bridge stores (settings, AI log, messages, node data). A daily copy is made
automatically and the newest seven are kept. You can download one, and restore from a saved or uploaded file. A restore never replaces
the running database: the file is checked and set aside, and it is swapped in the next time the bridge starts, with the old data kept
as a backup first. Backups hold message text, so keep them private. The map cache isn't included (it refills by itself).
