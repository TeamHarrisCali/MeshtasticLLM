# Setting up on a new computer

Copy the project folder over, then run the one-step setup. It works out what kind of computer it is on, builds a private
Python environment (`.venv`), installs the dependencies, and checks Ollama and the radio. It is safe to run again any time.

    setup.bat               # Windows: double-click it, or run it in a terminal
    ./setup.sh              # Linux / macOS
    python scripts/setup_env.py     # anywhere, if the two above don't suit (needs Python 3.9 or newer)

What it does, in order: detects the OS (including WSL); finds a Python 3.9+ (and, if there is none, prints the exact install
command for this computer - winget, brew, apt, dnf, pacman... - and offers to run it); creates `.venv`, **or rebuilds it if it was
copied from another computer** (a Windows `.venv` is useless on Linux, and vice versa); installs `requirements.txt` and proves
every package imports; checks that Ollama is installed and running and that the configured model is downloaded; looks for the
radio (on Linux it also checks the `dialout` group, ModemManager and brltty, which commonly block it); creates `logs/`; and
finishes with a checklist that says what, if anything, still needs you.

Options: `--check` (report only, change nothing), `--recreate` (throw the `.venv` away and rebuild), `--force` (reinstall
packages), `--pull-model` (download the AI model if missing, asks first), `--install-ollama` (asks first), `--start` (start the
bridge when ready), `--yes` (answer yes to questions), `--python PATH`, `--dir PATH`.

To keep your history, copy `audit.db` with the folder (it holds the settings, chat memory and mesh data; the map cache
`tile_cache/` is optional and just refills). Leave `.venv` behind if you like - setup rebuilds it. Start the bridge afterwards
with `scripts/start_bridge.ps1` (Windows) or `./scripts/start_bridge.sh` (Linux / macOS); both use `.venv` automatically.

If `./setup.sh` says `bad interpreter` or similar, the file picked up Windows line endings on the way over; run
`python3 scripts/setup_env.py` instead - it repairs the `.sh` files itself.

**Linux and the serial port.** Your user needs permission to open the radio's serial port. Setup checks this and tells you which group to
join (`dialout` on Debian and Ubuntu, `uucp` on Arch). Close any other program that uses the radio's serial port first; only one program can hold it.

## Starting and stopping the bridge

The bridge runs in the background with no window. It finds the radio by itself, and no flags are needed: the model and every setting you change
in the dashboard are remembered in the database, and the command-line flags in [flags.md](flags.md) are optional overrides. Logs are overwritten on
each start and go to `logs/bridge.log` and `logs/bridge.err.log`.

    ./scripts/start_bridge.sh           # Linux / macOS: start (or restart) the bridge in the background
    ./scripts/stop_bridge.sh            # stop it
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_bridge.ps1     # Windows (scripts\stop_bridge.ps1 stops it)
    .venv/bin/python -m meshllm         # or run it in the foreground (Windows: .venv\Scripts\python.exe -m meshllm)

The scripts find the project folder (the parent of `scripts/`) themselves, so they work from any folder, and they pass flags on to the bridge:
`./scripts/start_bridge.sh --model qwen2.5:7b`. Then open <http://127.0.0.1:8080/> and, from another node, send your radio a direct message
such as `/ai help`, `/ai how's the mesh doing?`, `/ai what's the temperature outside?` or `/ai where is the nearest router?`.

## Try it without hardware (demo mode)

No radio and no Ollama? Run the whole bridge and dashboard against a simulated mesh:

    .venv/bin/python -m meshllm --demo          # then open http://127.0.0.1:8080/

(In Docker the same thing is `./setup.sh --docker --demo`, see [Run with Docker](#run-with-docker).) You get about three dozen obviously fake nodes
(`Demo Ridge Repeater`, `Demo Trail Tracker 3`, ...) scattered around a public park, with batteries, sensors, trails, a day of history and live
traffic, and once a minute or so a fake node DMs the bridge an `/ai` question that goes through the real queue, tools and an acked reply. Without
Ollama a small scripted model (shown as `demo-scripted`) answers; if Ollama is running with a tool-capable model, that model is used instead. A
"Demo mode" badge shows in the dashboard header. Nothing is transmitted, nothing connects to a serial port, and everything is kept in a temporary
folder that is deleted when you stop it (Ctrl+C, `kill`, or closing the terminal): your real `audit.db` is never opened. Demo mode listens on this
computer only (`--web-host` must stay `127.0.0.1`), and if port 8080 is taken, for example by your real bridge, use `--web-port 8081`. (The optional
map background still downloads OpenStreetMap tiles if you look at the Map page.) The flags are in [flags.md](flags.md); the screenshots in the README
were taken in demo mode.

## Connecting over Wi-Fi or Bluetooth

USB serial is the default and needs nothing. The bridge can also reach a radio without a cable. Choose one way; they cannot be combined.
All three keep the same behaviour: a lost link is noticed, the bridge keeps running (the dashboard says it is searching), messages
queued for the radio wait up to `--reconnect-hold` seconds, and it reconnects by itself when the radio is back, including after a power
cycle. If a different radio answers at the same address the "different radio" banner appears, as for USB. The dashboard shows the connection as
`tcp://host:4403` or `ble:ADDRESS`.

    python -m meshllm --tcp 192.168.1.50                  # Wi-Fi: the radio's address or host name (port 4403 unless you add :PORT)
    python -m meshllm --ble-scan                          # Bluetooth: list nearby radios (name and address), then exit
    python -m meshllm --ble AA:BB:CC:DD:EE:FF             # Bluetooth: connect to one of them (address or name, as the scan printed it)

Wi-Fi needs the radio's Wi-Fi switched on and joined to your network; Bluetooth needs a Bluetooth adapter on the computer running the bridge, so it works
on the host only and not inside a container. Both are covered in detail below.

**Wi-Fi (TCP).** On the radio, switch Wi-Fi on and give it your network's name and password (Meshtastic app or web client, Network
settings). The radio's API then listens on port 4403. Find its address on your router, then:

    python -m meshllm --tcp 192.168.1.50        # or a host name, or HOST:PORT for another port

Give the radio a fixed address (a DHCP reservation in the router) so it does not move. If the connection is refused, another program may already hold the
radio's connection (a phone app or web client attached over Wi-Fi): close it. When a radio vanishes from Wi-Fi without closing the
connection, the bridge asks the operating system to probe the idle connection, so it notices after a minute or so on Linux (longer on
other systems) and reconnects. That probe only runs while the connection is idle: if the radio vanishes while a reply is still unsent, the operating
system's retransmission timeout (about 15 minutes on Linux) applies. On Windows, Ctrl+C may lag by up to the OS connect timeout during a connection attempt to an unreachable address (untested).

**Bluetooth (BLE).** **Set a PIN first.** Before pairing, set the radio's Bluetooth mode to *Fixed PIN* (or *Random PIN*) in the Meshtastic app. In
"No PIN" mode the radio pairs with anyone in range, and the unauthenticated pairing goes stale when the radio reboots (seen on BlueZ 5.87): after
that the computer aborts every connect and the radio's settings never arrive. Pair with the PIN as shown below. (The radio also reboots when
a USB serial connection to it is opened or closed, which is normal.) Then get the radio's address and pass it:

    python -m meshllm --ble-scan                  # nearby radios that are advertising (name and address)
    bluetoothctl devices Paired                   # Linux: radios the computer already knows
    python -m meshllm --ble AA:BB:CC:DD:EE:FF     # connect by ADDRESS (a name from the scan also works)

**If the computer's own Bluetooth already holds the radio** (a desktop Bluetooth manager connected it after pairing), the radio stops
advertising and will not appear in a scan. Pass the **address** then: a Bluetooth address (six hex pairs) is connected to directly, with no scan,
and the bridge logs `Bluetooth: connecting to ...`. On Linux it reuses BlueZ's existing entry for the radio, including a connection the system already holds, so the radio need not be advertising. A **name** always needs a scan. If the address is not known to the system yet, the bridge says
to pair the radio once in the system Bluetooth settings.

Only one Bluetooth client can hold the radio at a time: disconnect the phone app first. Bluetooth is host-only: it uses the computer's own Bluetooth adapter and does not work inside
a container or a virtual machine without the adapter passed through. On **Linux** it needs BlueZ (the `bluetooth` service running, D-Bus available)
and your user allowed to use it; if the scan fails, check `systemctl status bluetooth` and that `bluetoothctl show` lists a powered adapter.

*Pairing as Low Energy with a PIN (BlueZ).* A desktop pairing can be recorded as classic Bluetooth (BR/EDR) instead of Low Energy, or can go stale,
and then connects fail with `br-connection-canceled` or `le-connection-abort-by-local`, or the radio connects but never sends its settings. The bridge
prints one line naming this as the likely cause. The fix is to remove the pairing and pair again as Low Energy, with the PIN:

    bluetoothctl remove AA:BB:CC:DD:EE:FF
    bluetoothctl agent KeyboardOnly
    bluetoothctl default-agent
    bluetoothctl --timeout 22 scan le             # leave this running ...
    bluetoothctl pair AA:BB:CC:DD:EE:FF           # ... and in a second terminal run this; type the radio's PIN when asked
    bluetoothctl trust AA:BB:CC:DD:EE:FF
    bluetoothctl disconnect AA:BB:CC:DD:EE:FF     # let go, so the bridge can connect

(If no PIN prompt appears, run the agent, scan and pair commands inside one interactive `bluetoothctl` session: a one-shot `agent` command may not outlive the command.)

*How it connects.* Names are looked up with an unfiltered scan of about ten seconds (the bridge picks out Meshtastic radios itself: with
BlueZ 5.87 and bleak 3.0.2 the library's own scan, which asks BlueZ to filter by service, crashed the Bluetooth daemon). Every phase is logged
(`Bluetooth: scanning`, `connecting`, `waiting for the radio to send its settings`, `radio ready`), and the whole connect gives up after 90 seconds
with a message naming the phase it was stuck in, then retries after a back-off (30 to 60 seconds) instead of hanging silently. The bridge also watches the link itself: it asks the Bluetooth stack whether the radio is still connected, and it
counts the messages the radio sends. If nothing at all has arrived for 15 minutes (30 minutes over Wi-Fi; the library's own heartbeat goes out every 5) it treats the link as dead, closes it and
reconnects, because after a radio reboot the system can reconnect underneath while the radio's data stream is gone and the connection still
looks fine. A reconnect interrupts anything in flight, so it is a last resort: on a very quiet mesh the radio may genuinely send nothing for that long, and if a silence reconnect is followed by another with no data in between, the bridge logs a warning, waits a minute before reopening and allows twice as much silence next time (up to four times; any data resets this). Raise the limit with `--link-silence` if your mesh is that quiet. The dashboard shows "heard N s ago" next to the radio,
Diagnostics warns when the silence passes the limit, and the hidden `--link-silence SECONDS` flag changes the limit (0 turns it off). USB is not
affected. The bridge depends on a few internals of the `meshtastic` library, so `requirements.txt` limits it to `meshtastic>=2.7.11,<2.8`; with another version the bridge stops with a one-line message instead of misbehaving. Bluetooth support
comes with the `meshtastic` package (it installs `bleak`); if it is missing the bridge says so in one line and you can reinstall with
`pip install -r requirements.txt`. Not yet tried on Windows or macOS.

**Wi-Fi (`--tcp`) has not been tested on real hardware yet.** It is covered by automated tests with a simulated connection only, so treat
the first Wi-Fi run as a trial and report what happens. USB and Bluetooth (`--ble`) have been tested on a real Heltec V3 radio:
hot-plug reconnect, connecting with and without the computer already holding the radio, and recovery after the radio rebooted.

### Failover between connections

Add `--fallback KIND:VALUE` (repeatable) to name other ways to reach the same radio. KIND is `usb`, `tcp` or `ble`; only the first colon separates
the kind from the value, so values may contain colons:

    python -m meshllm --fallback ble:AA:BB:CC:DD:EE:FF                       # USB first, Bluetooth when the radio is unplugged
    python -m meshllm --tcp 192.168.1.50 --fallback ble:AA:BB:CC:DD:EE:FF --fallback usb:auto
    # other forms: tcp:HOST[:PORT]  tcp:[fe80::1]:4403  usb:/dev/ttyUSB1  usb:COM4  ble:NAME

*The order.* Priority is the primary connection (`--port`, `--tcp`, `--ble`, or USB auto-detect when none is given), then each `--fallback` in the order
written. Whenever the bridge needs a connection it takes the highest entry that has something to try: a USB entry when a radio port is listed, a
Wi-Fi or Bluetooth entry always. An entry that fails to open is left alone for its normal back-off (10 to 60 seconds for USB, 15 to 60 for Wi-Fi and Bluetooth, longer for a radio that needs fixing first; it doubles with each failure, up to five minutes, for entries that have
something below them), so the bridge moves on to the next one instead of retrying a dead entry forever. A fallback may not repeat another entry
(same Bluetooth address, same host and port, same serial port).

*Failback.* While a lower entry is in use, the bridge checks (every `--scan-interval`) whether a **higher USB entry** is listed again. If it has stayed
listed for 10 seconds without a break, the bridge logs `a preferred connection is available again: switching from ... to ...`, closes the current link
and opens USB. A port that appears and disappears inside the 10 seconds starts the count over, so a loose cable does not make it switch back and forth. If the USB link then dies within a minute of opening, the next wait doubles (up to five minutes), and a port that fails to open three times in a row is left alone until it is unplugged and plugged in again. While a lower entry is live, a USB open that gets no answer from the radio gives up after 45 seconds instead of the library's five minutes.
Wi-Fi and Bluetooth entries cannot be checked without opening them, so they are **not probed**: they are used when everything above them is
unavailable, and a bridge running on a lower entry never goes back up to one of them by itself. If the radio is unplugged or its link drops, the
next connection attempt starts again from the top of the list.

*One transport at a time.* The bridge waits for the old link to finish closing (up to 10 seconds) before it opens the next, so it does not normally hold two connections to the radio. (What the
firmware does with USB and Bluetooth open together is not known, so this is deliberate.) The radio reboots whenever USB serial is opened or closed, so
expect a short gap when switching to or from USB. The radio's identity is compared across transports: the same radio behind USB and Bluetooth is not
"a different radio" (no banner, history kept). Messages queued meanwhile wait up to `--reconnect-hold` seconds, as for any reconnect.

*Where you see it.* The dashboard's radio status says `via Bluetooth (USB not connected)` while a fallback is in use (the connection label itself stays a plain
string), Diagnostics has a line listing the chain and each entry's state, and the log names the connection in use.

*Limits.* Bluetooth entries work on the host only, so they make no sense in a container (the Docker setting `MESHLLM_FALLBACK` takes a comma-separated list,
for example `usb:/dev/ttyUSB1,tcp:192.168.1.50`; see [Run with Docker](#run-with-docker)). Wi-Fi has not been tested on real hardware. USB with a Bluetooth fallback **has** been: on one
Heltec V3, unplugging USB switched to Bluetooth and replugging switched back to USB after the 10 s window, and the bridge recognised the same radio each
time. Wi-Fi as a fallback, and failover with several radios, are untested. `--demo` ignores `--fallback`.

### Finding the radio's Bluetooth address from the dashboard

If the radio is connected by USB, can this PC see its Bluetooth and use it? Partly. Over USB the radio reports its **Bluetooth settings**
(whether Bluetooth is on, and the pairing mode: random PIN, fixed PIN or no PIN), but **not its Bluetooth address**; the firmware never sends
it. The only hints are its name, which is `Meshtastic_` plus the last four hex digits of its node id (node id `!00000a01` advertises as
`Meshtastic_0a01`), and what this PC's own Bluetooth hears nearby. The pairing PIN is a credential and is never read, shown or stored by
this feature.

The dashboard's **Connection** page (Tools) puts that together, for the admin account only:

1. *How the bridge is connected*: the failover chain and which entry is live, as on Home and Diagnostics.
2. *The radio's Bluetooth*: on or off and (admin only) the pairing mode, read from the settings the radio sent when the bridge connected.
3. **Scan for it**: a scan of this PC's Bluetooth (up to 25 seconds, one at a time, in the background) that keeps only a device advertising the
   expected name. Other radios in range are dropped, never listed. It says plainly when nothing was found (the radio is off, out of range, or
   held by a phone or another computer, which stops it advertising) or when Bluetooth is unavailable.
4. **Use as Bluetooth fallback**: saves that address in the bridge's settings database. **Restart the bridge to apply it** (the running chain is
   not rebuilt). From then on, when the bridge is started with no `--fallback`, `--tcp` or `--ble` flag, it builds the chain USB first and that
   Bluetooth address second, exactly as `--fallback ble:ADDRESS` would. Any of those flags on the command line always wins, and the page says
   so. **Clear** removes the saved address (also applied at the next start). The address must be six hex pairs (`AA:BB:CC:DD:EE:FF`, colons or
   dashes, any case; it is saved upper case with colons) and nothing else is accepted.

*Limits.* The scan cannot run **in Docker**: a container has no Bluetooth adapter, so the page and the API say so and refuse (run the bridge
directly on the PC, or run `python -m meshllm --ble-scan` there, to find the address). It needs `bleak` and, on Linux, BlueZ; without them
it says so. It is refused while the bridge is itself connected over Bluetooth, because a connected radio does not advertise and a scan could
disturb the live link. Radios whose Bluetooth name was changed may not match the expected name. macOS reports a Bluetooth device as a long
identifier instead of an address; those can be found but not saved here (use `--fallback ble:NAME` instead). A viewer (read-only) account sees the kinds of
connection and whether the radio's Bluetooth is on, never an address, the radio's Bluetooth name or pairing mode, the saved fallback, or the scan.
Only one scan runs at a time, and a scan that never reports back stops blocking a new one after 60 seconds. After a failed or timed-out scan a new one
is refused for about 30 seconds, because BlueZ can keep a discovery running for a short while after the bridge gives up on it. A scan started just
as USB drops may overlap one Bluetooth connect attempt of the failover chain; if the link then misbehaves, wait a minute and it settles.
Real Bluetooth scanning from the dashboard has not been tried on a real radio yet; the tests use a fake scan.

## Use the dashboard from a phone or another computer (LAN login)

By default the dashboard only listens on this computer (`127.0.0.1`) and has no login. To open it from a phone or another PC it needs a **password**, a list of the
**names you will browse to**, and ideally **HTTPS**. The bridge refuses to start on any other address without the first two.

**1. Set a password** (the bridge never takes one on the command line or from the environment):

```bash
python -m meshllm --set-password                  # the admin account: everything. Asks twice, no echo, at least 12 characters
python -m meshllm --set-password --role viewer    # optional: a read-only account to hand to someone else
```

Each writes one file that holds only a scrypt hash, readable by you alone (`~/.config/meshllm/admin.hash` and `viewer.hash`, or the path you give with
`--password-hash-file` / `--viewer-password-hash-file`). Running it again changes the password, and a bridge that is already running signs everyone out of that account within a few seconds.
Keep the files out of git (`*.hash` is ignored) and out of backups you share.

**2. Start the bridge with the address, the names and the files:**

```bash
python -m meshllm --web-host 0.0.0.0 --allowed-host 192.0.2.10 --allowed-host radio.test \
    --password-hash-file ~/.config/meshllm/admin.hash --viewer-password-hash-file ~/.config/meshllm/viewer.hash
```

`--allowed-host` is every host name or address you will type in the browser (192.0.2.10 and radio.test are made-up examples); a request that names anything else is refused, which
is what stops another web page from borrowing your browser to reach the bridge (DNS rebinding). Then browse to `http://192.0.2.10:8080/` and sign in. The same flags can be set
as `MESHLLM_PASSWORD_HASH_FILE` and `MESHLLM_VIEWER_PASSWORD_HASH_FILE` (paths, never passwords). A password on `127.0.0.1` also turns the login on. `./scripts/start_bridge.sh` passes the same flags on.

| | admin | viewer |
|---|---|---|
| Read the dashboard (nodes, map, messages, AI log, telemetry, ...) | yes | yes |
| Ask the AI a question (the answer goes to the log only; the AI is still read-only and tier 0) | yes | yes |
| Send a message, post to the channel, traceroute, change the radio, access rules, settings, model | yes | **no** |
| Backups (download, restore), clearing and deleting data | yes | **no** |
| CSV exports, reports, diagnostics, logs (they hold message text) | yes | only with `--viewer-exports` |

The viewer's page still shows the buttons it cannot use; they answer with an error. Pressing Sign out ends the session on the server too.

**3. Encrypt it.** Without HTTPS the password and everything on the dashboard cross your network in clear text; on a network you do not trust that is not acceptable, on a
home network it is a risk you may decide to accept. The login page shows a warning to anyone who is not on the bridge's own computer and not using HTTPS. Two ways:

- *Directly:* `--tls-cert cert.pem --tls-key key.pem`. For a certificate only you trust, for example (a made-up name; change it):
  `openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem -days 365 -subj "/CN=radio.test" -addext "subjectAltName=DNS:radio.test,IP:192.0.2.10"`
  (your browser will ask you to trust it once). The session cookie then gets the `Secure` flag.
- *Behind a reverse proxy* that does HTTPS (Caddy, nginx, Traefik) on the same computer, which then talks to the bridge on loopback. Caddy needs one line,
  `radio.test { reverse_proxy 127.0.0.1:8080 }`; with nginx use `proxy_pass http://127.0.0.1:8080;` and pass `Host`, `X-Forwarded-For` and `X-Forwarded-Proto`
  (`proxy_set_header Host $host; proxy_set_header X-Forwarded-For $remote_addr; proxy_set_header X-Forwarded-Proto $scheme;`). Start the bridge with
  `--web-host 127.0.0.1 --allowed-host radio.test --trusted-proxy 127.0.0.1 --password-hash-file ...`. **Only name a proxy you control in `--trusted-proxy`:** its headers are believed.
  Without `--trusted-proxy` every visitor looks like the proxy to the login throttle and the bridge thinks the connection is plain HTTP.

**What it does.** Passwords are hashed with scrypt (n = 2^15, r = 8, p = 1, random salt) and compared in constant time. A session is 256 random bits in an `HttpOnly`, `SameSite=Strict`
cookie; the server keeps only its hash, issues a new one at every login, and ends it after 2 hours idle or 12 hours in total (`--session-idle-minutes`, `--session-hours`), at Sign out,
when that account's password changes, and when the bridge restarts. Failed logins back off exponentially per source address (and, more gently, across all sources); it never locks the
owner out, the wait just ends, and while it lasts even the right password is not tried. Every change from the browser needs the right `Origin` and a per-session token. Each route is tagged
public, viewer or admin in `meshllm/webroutes.py`, and an untagged one is admin-only.

**What it does not do.** There are only two shared accounts (no per-person logins, no two-factor, no password change from the browser; use `--set-password`). Someone on your network can still slow
your sign-in down by guessing: a new browser can be made to wait up to a minute (from another address) or up to five minutes (if they share yours, for example everyone behind a proxy you did not list in `--trusted-proxy`), and a determined guesser can renew that for as long as they keep guessing. A browser you have signed in to as admin before (it holds a 30-day device cookie) skips the one-minute wait and only ever waits for its own failures, so you can still get in. Existing sessions are never affected. They can see that the bridge exists. Without HTTPS they can read and replay everything. A viewer sees message text. Non-browser
clients (curl) must send an `Origin` header and, after signing in, the `X-CSRF-Token` from `GET /api/session`. See [.github/SECURITY.md](../.github/SECURITY.md).

## Start at login (optional)

    python scripts/setup_env.py --autostart       # start the bridge every time you log in (asks first; --yes skips the question)
    python scripts/setup_env.py --no-autostart    # take it out again
    python scripts/setup_env.py --check           # includes a "Start at login" line: installed / not installed

It is per user: no administrator or sudo, and nothing outside your own account is changed. It uses the `.venv` when there is one
(run plain `python scripts/setup_env.py` first on a new computer). What it creates: on **Windows** a Task Scheduler task named
`MeshLLMBridge` (runs `scripts/start_bridge.ps1` hidden at logon; if Windows says access is denied, run it from an administrator terminal
or put a shortcut to `scripts/start_bridge.ps1` in `shell:startup`); on **Linux** a systemd user service
`~/.config/systemd/user/mesh-llm-bridge.service` (restarts on failure; `loginctl enable-linger $USER` makes it start at boot instead
of at login; without systemd, e.g. plain WSL or a container, add `./scripts/start_bridge.sh` to the desktop's startup applications); on
**macOS** a launchd agent `~/Library/LaunchAgents/com.meshllm.bridge.plist` (logs in `logs/`). `--no-autostart` removes exactly
what `--autostart` made. A plain `python scripts/setup_env.py` never turns this on. Not yet run on a real Linux or Mac: the unit and plist
text and the commands are unit-tested only.

Tested: Windows 11 natively, and Linux (Ubuntu 24.04) under WSL (fresh build, a stale `.venv`, repairing line endings,
starting and stopping the bridge). macOS is covered by unit tests of the detection and install-command logic only, not run on a Mac.
The radio is not visible inside WSL; run the bridge on the Windows side, or attach the radio with usbipd-win.

## Run with Docker

The project can run as containers instead of through `setup.sh`. **This is the second option**: the installer above supports everything, Docker only needs Docker but cannot do some things (the list is
just below). The containers are: one for the bridge and dashboard, one for Ollama (the AI model server), and a small one-off
container (`model-pull`) that downloads the AI model the first time. The files are `Dockerfile`, `docker-compose.yml`, `docker/docker-compose.usb.yml`,
`docker/docker-compose.login.yml`, `.env.example`, `docker/entrypoint.sh` and the helper `scripts/setup_docker.py`.

### What Docker can't do

**What Docker can't do** (use the installer above if you need any of these):

- **Bluetooth.** A container cannot reach the host's Bluetooth service, so a Bluetooth radio, `--ble-scan`, the dashboard's [Bluetooth address finder](#finding-the-radios-bluetooth-address-from-the-dashboard), and the **USB-with-Bluetooth failover** (the combination tested on real hardware) work only with the installer. Inside Docker failover can only be between USB and Wi-Fi, which is untested.
- **USB on Windows and macOS.** Docker Desktop cannot hand a USB serial device to a container, so USB radios work in Docker on Linux only (elsewhere use a Wi-Fi radio, also untested).
- **USB hot-plug.** The device is passed to the container when it is created: after unplugging and replugging the radio, run the setup command (or `docker compose up -d`) again. The installer's bridge reconnects by itself.
- **Your existing data.** Docker keeps its own database and its own copy of the AI model, separate from a bridge and an Ollama you already run (restore an old database from the dashboard's Backups page).
- **The dashboard's log viewer.** The container writes no log files, so read them with `docker compose logs -f bridge`.
- **Not yet tried:** Docker Desktop on Windows and macOS, and Wi-Fi/TCP radios, with or without Docker.

### The easy way: one command

With Docker running, from the project folder:

    ./setup.sh --docker                          # Linux / macOS            (Windows: setup.bat --docker)

(The same thing as `python scripts/setup_env.py --docker`; the script only needs Python itself, not the project's packages.) It does, in order, and tells you at each step what it found:

1. **Checks Docker**: the program, Compose version 2, and that the engine is running and you may use it. If not, it prints the exact install or start command for your system.
2. **Finds the radio.** On Linux it uses the USB serial device it sees (`/dev/ttyUSB*` or `/dev/ttyACM*`) and the group that owns it. Docker Desktop on Windows and macOS cannot pass USB through,
   so there you give a Wi-Fi radio's address: `--tcp ADDRESS` (`--tcp off` goes back to USB). With no radio found the dashboard still starts and waits for one.
3. **Offers a dashboard password** (recommended; asked in your terminal, typed twice, nothing shown, stored only as a scrypt hash in a mode-600 file). If you already made one with `--set-password` it offers to reuse it.
   `--no-login` skips it (the dashboard then stays on this computer only). Run it without `--yes`, in a terminal, to be asked.
4. **Checks the port** (8080 unless `--web-port N`) so you get a clear message, not Docker's, when a bridge outside Docker is already using it.
5. **Writes `.env`** (git ignores it; no secret goes in it: only which compose files to use, the hash file's *path*, the USB device, a Wi-Fi address, the port), then runs `docker compose up -d --build`
   and waits until the bridge reports healthy. The first start also downloads the AI model in the background (about 2 GB): `docker compose logs -f model-pull`.

Afterwards the plain Docker commands do the same thing because `.env` carries the choices: `docker compose up -d`, `docker compose logs -f bridge`, `docker compose down`. Re-running the script is safe (it changes `.env` only when
something changed). Other options: `--demo` (a simulated mesh, no radio, no Ollama, no password), `--lan ADDRESS [--allowed-host NAME]` (the dashboard on your network; needs the password, see
[below](#reaching-it-from-another-computer-opt-in)), `--no-start` (write `.env` only), `--check` (look and report, change nothing) and `--docker-stop` (stop everything; the database stays).
Stop the bridge you run outside Docker first (`./scripts/stop_bridge.sh`): it holds the same port and the same radio. The model downloaded here lives in Docker's volume and is separate from any Ollama you run on the host.
It was built and tried on Linux with Docker 29.8.2 and Compose 5.5.1, running the demo and the bridge without a radio. **Not yet tried:**
Docker Desktop on Windows or macOS, a real radio from inside a container (USB or Wi-Fi), and the Ollama container (the model server was
not started in testing).

> **By default the container's dashboard has no login.** It shows message text and has a box that sends on your radio, so the compose file publishes
> it on **this computer's loopback only** (`127.0.0.1:8080`) and does not publish Ollama's port at all. For a password, use
> [the login override](#the-dashboard-login-in-docker); for another computer or a phone, [the LAN opt-in](#reaching-it-from-another-computer-opt-in), which is refused
> without that login. **Do not hand-edit the `ports:` address to `0.0.0.0` or to a LAN or public IP:** the container cannot see that edit, so none of its checks run. Docker adds its own firewall rules, so a port published on all
> interfaces is reachable from the network even when a host firewall such as `ufw` says it is blocked. To use the no-login dashboard from another
> computer, forward the port over SSH (`ssh -L 8080:127.0.0.1:8080 host`) rather than publishing it. If you run the image without Compose, publish with `-p 127.0.0.1:8080:8080`, never `-p 8080:8080` (that would expose a login-less dashboard on your network).
>
> If the container says `/data is not writable by uid 10001`, the volume or bind-mounted folder has the wrong owner: make it writable for user id 10001 (a new named volume already is).

**What you need:** Docker Engine with the Compose plugin (`docker compose version` works), and disk space: about 250 MB for the bridge image, about 4 GB for the Ollama image (the demo does not need it), plus a few GB for
each model.

**Try it first, with no radio and no Ollama.** One command builds the image (about half a minute the first time) and starts the demo, a
simulated mesh with a scripted model, nothing transmitted and nothing kept:

    docker compose --profile demo up demo        # then open http://127.0.0.1:8080/ ; Ctrl+C stops it

**The real thing, by hand** (what the script above automates).

    cp .env.example .env                          # optional: every line in it is a comment; edit what you need
    docker compose up -d --build                  # starts the bridge and Ollama, and downloads the AI model the first time (about 2 GB)
    docker compose logs -f model-pull             # the download's progress (it exits when done; MESHLLM_PULL_MODEL=0 turns it off)
    docker compose logs -f bridge                 # what the bridge prints (the dashboard's log viewer reads log files, which this setup does not make)

Open <http://127.0.0.1:8080/>. With no radio connected the dashboard is up and says it is waiting for one. If port 8080 is taken on your computer
(for example by a bridge running outside Docker), set `MESHLLM_WEB_PORT=8081` in `.env`. That changes the number only; the address stays `127.0.0.1` unless you opt in to the LAN below.
To use another model, choose it on the dashboard's Model page (remembered in the database), or set `MESHLLM_MODEL` in `.env`, which makes `model-pull` download that one. With no `MESHLLM_MODEL`, `model-pull` downloads
`llama3.2:3b` only when no model is installed yet.

**Choose how the radio is reached** (pick one; Bluetooth is not an option):

- **USB, Linux computers only.** Docker Desktop on Windows and macOS cannot pass a USB serial device into a container. `./setup.sh --docker` does the next two steps for you. On Linux, find the group
  that owns the device with `stat -c %g /dev/ttyUSB0`, put that number in `.env` as `MESHLLM_SERIAL_GID=<number>` (it is 20 on Debian and Ubuntu, but
  other distributions differ) and, if the radio is not `/dev/ttyUSB0` (for instance `/dev/ttyACM0`), the device as `MESHLLM_SERIAL_DEVICE=/dev/ttyACM0`. Then start with the override file, which gives the container that one device and that one group and nothing else:

      docker compose -f docker-compose.yml -f docker/docker-compose.usb.yml up -d

  The device is handed to the container when it is created, so after unplugging and replugging the radio run `docker compose up -d` again (or `./setup.sh --docker`). Only one program can hold
  the serial port, so stop any bridge running outside Docker first. This path has not been tried with a real radio yet.
- **Wi-Fi (TCP), any computer.** Switch on the radio's Wi-Fi and give it a fixed address as described under *Connecting over Wi-Fi* above, then
  put `MESHLLM_TCP=192.168.1.50` (its address, or `HOST:PORT`) in `.env` and run `docker compose up -d`. This is the way to use Docker on Windows
  or macOS. **Wi-Fi/TCP has not been tested on real hardware, with or without Docker**, and Docker Desktop has not been tried at all.
- **Failover.** `MESHLLM_FALLBACK` is a comma-separated list of `KIND:VALUE` entries tried after the primary, in order (for example `usb:/dev/ttyUSB1,tcp:192.168.1.50`; see
  [Failover](#failover-between-connections)). Leave `ble:` entries out: they cannot work here.
- **Bluetooth does not work in a container.** It needs the host's BlueZ service and D-Bus, which the container does not have. Run the bridge on the
  host for a Bluetooth radio (the setup above).

Any other bridge flag (see [flags.md](flags.md)) can go in `MESHLLM_EXTRA_ARGS` in `.env`, split the way a shell would, for example
`MESHLLM_EXTRA_ARGS=--access-mode allowlist --daily-cap 20`. An unclosed quote stops the container with a message instead of starting with half the flags.

**Your data.** Everything the bridge keeps is in the Docker volume `meshllm-data`, mounted at `/data`: the database `audit.db` (settings, chat memory, mesh
data), its `backups/` folder (the automatic daily copy and your manual backups, as on the dashboard's Backups page) and the map-tile cache. It survives
`docker compose stop`, `down`, restarts and image updates, and is deleted only by `docker compose down -v` or `docker volume rm`. The Ollama models are in
the volume `ollama-models`. A backup copy lives in the same volume as the database, so it does not protect against losing the volume: copy the whole
folder out now and then (the bridge can be running; stop it first for a perfectly consistent copy):

    docker compose run --rm -T --no-deps --entrypoint tar bridge cf - -C /data . > meshllm-data.tar
    # restore into an empty volume, with the bridge stopped:
    docker compose run --rm -T --no-deps --entrypoint tar bridge xf - -C /data < meshllm-data.tar

To move an existing `audit.db` from a non-Docker install into Docker, restore it from the dashboard's Backups page after the first start (or extract a tar of it
into `/data` as above), rather than bind-mounting your project folder. If you do bind-mount a host folder over `/data`, make it writable by user id 10001.

**How it is locked down.** The container runs as user id 10001 (not root), with every Linux capability dropped, `no-new-privileges`, a read-only
root filesystem (only `/data` and a memory-backed `/tmp` are writable), and no host networking. It starts with `restart: unless-stopped`, so a configuration the container refuses (exit 78 for an unsafe LAN publish, exit 1 or 2 for a missing file or a bad flag) shows up as a restart loop in `docker ps`: read `docker compose logs bridge` for the one-line reason. `docker stop`
ends the bridge in about a second with exit code 0 (the bridge turns SIGTERM into the same clean stop as Ctrl+C). Docker marks it *healthy* when
`/api/session` answers (the one data route that stays open under a login, so the check keeps working with one); it says nothing about whether a radio is connected.

### The dashboard login in Docker

(`./setup.sh --docker` does these steps for you, asking for the password in your terminal.)

1. On the host, make the password hash (once; it asks twice and shows nothing you type): `python -m meshllm --set-password`. It writes a mode-600 file, by default `~/.config/meshllm/admin.hash`, that holds
   only a scrypt hash. Treat it as a secret anyway: keep it out of git (`*.hash` is ignored) and out of backups you share.
2. Put the file's **absolute** path in `.env` (Compose does not expand `~`): `MESHLLM_ADMIN_HASH_FILE=/absolute/path/to/admin.hash`.
3. Start with the override file added: `docker compose -f docker-compose.yml -f docker/docker-compose.login.yml up -d` (add `-f docker/docker-compose.usb.yml` for a USB radio). To avoid typing it, put
   `COMPOSE_FILE=docker-compose.yml:docker/docker-compose.login.yml` in `.env`. The dashboard (still on `127.0.0.1:8080`) now shows the sign-in page; see [the LAN login section](#use-the-dashboard-from-a-phone-or-another-computer-lan-login)
   for the accounts, throttling and what the login does not do. Only the admin account is wired in Docker, not the optional read-only viewer.
4. After running `--set-password` again, run `docker compose restart bridge`: the container reads the hash when it starts (an already-running container does not see a replaced file).

**Why a secret file and not an environment variable.** `docker inspect` shows every environment variable of a container to anyone who can use Docker. A Compose `secrets:` file is mounted at `/run/secrets/meshllm_admin_hash` and never appears there.
The same goes for the image: `.dockerignore` keeps `*.hash` out of it.

**The unreadable-secret problem, and the trade-off.** Compose mounts a file secret with the *host file's* owner and mode (it ignores `uid`, `gid` and `mode` for file secrets; Compose 5.5.1 prints a warning saying so). The file `--set-password` writes
is mode 600 and belongs to you, so the container's user (uid 10001) cannot read it. What the override does about it:

- The bridge container starts as **root for a moment**, with exactly three capabilities added back: `DAC_OVERRIDE` (to read your 600 file), `SETUID` and `SETGID` (to give up root). Everything else stays dropped, the root file system stays read-only and `no-new-privileges` stays on.
- `docker/entrypoint.sh` copies the file into `/run/meshllm`, a **memory-only tmpfs** (mode 0700, 1 MB, `noexec`), as a file owned by 10001 with mode **0400**. The copy is written by the unprivileged user, so no `CHOWN` capability is needed. It disappears when the container stops; nothing is written into the image or a volume.
- It then re-runs itself as 10001 (keeping only the groups Docker added with `group_add`, such as the USB serial group), and everything else (reading the environment, building the command line, Python, the bridge) runs as 10001 with **no capabilities at all**. Root never runs the bridge or parses `MESHLLM_EXTRA_ARGS`.
- A container started as root **without** the secret refuses to run (so root is never left running the bridge).

Seen from outside: PID 1 is uid 10001 with empty effective and permitted capability sets (the three capabilities remain only in the process's bounding set, which grants nothing without file capabilities, and `no-new-privileges` forbids those).
Two things still run as the configured user, root: `docker compose exec bridge ...` and the healthcheck (a plain Python GET of `/api/session`). Anyone who can run `docker exec` already controls the Docker daemon, which is root on the host, so that adds no power for them.

*The alternative, not used:* on the host `sudo chown 10001 admin.hash && chmod 400 admin.hash`, which needs no extra capabilities at all. It hands your own password file to a numeric user id that may not exist on your computer, you can no longer read or edit it without `sudo`,
and it does not work under rootless Docker or user-namespace remapping (there, container uid 10001 is a different host user). If you prefer it anyway, mount your own `secrets:` entry on the base compose file: the entrypoint uses a secret it can read directly (and says what to do when it cannot). That route was only tested with the unreadable-file error, not with a real chown.

### Reaching it from another computer (opt-in)

By default the port is published on `127.0.0.1` only. To publish it on an address of this computer that other devices can reach, **all three** of these are required, and the bridge container **refuses to start** (exit code 78, with a message that says what is missing) if any is missing:

1. the login above (`docker/docker-compose.login.yml` and a valid `MESHLLM_ADMIN_HASH_FILE`);
2. `MESHLLM_WEB_BIND=<an address of this computer>` in `.env`, for example `192.0.2.10` (the only way to change the published address; the demo ignores it and stays on `127.0.0.1`). Any address that is not `127.x.x.x` or `::1` counts as the network;
3. `MESHLLM_ALLOWED_HOSTS=<every name or address you will type in the browser>` in `.env`, comma-separated, for example `192.0.2.10,radio.test` (one `--allowed-host` each; this is what stops DNS rebinding).

`docker compose -f docker-compose.yml -f docker/docker-compose.login.yml up -d` then publishes `192.0.2.10:8080`. Ollama's port is never published, and `--demo` is refused on a non-loopback address. Inside the container the app still listens on `0.0.0.0` (that is how a published port reaches it); the rule is enforced twice:
the entrypoint refuses, and it sets `MESHLLM_PUBLISH_LAN=1` so the app's own start-up check refuses a wildcard bind without a login even if the entrypoint were bypassed. What neither can see is a hand-edited `ports:` line or a `docker run -p 0.0.0.0:...`: do not do that.
`0.0.0.0` as `MESHLLM_WEB_BIND` follows the same rules (the login is then mandatory), but it publishes on every interface of the computer, including internet-facing ones, and Docker bypasses host firewalls such as `ufw`; name one address instead.

Limits to know: the traffic is plain HTTP, so the password crosses your network in clear text (see [the LAN login section](#use-the-dashboard-from-a-phone-or-another-computer-lan-login)); TLS is not wired through Compose, and the container's healthcheck is plain HTTP too, so `--tls-cert` in `MESHLLM_EXTRA_ARGS` would make the container unhealthy (use a reverse proxy on the host instead).
A connection to the published port from this same computer reaches the app through Docker's proxy, so the app sees Docker's gateway address as the client: the sign-in page then shows its clear-text-password warning even on this computer, and the login throttle counts all such connections as one source.
How a peer on the real network appears to the app (its own address, or the gateway's) depends on Docker's networking settings and was **not** verified with a real LAN peer.

**Updating.** Pull the new code, then rebuild and restart; the volume is kept:

    git pull
    docker compose up -d --build

The base image is pinned by tag and digest in the `Dockerfile` (Dependabot proposes new digests monthly), and the Ollama image by version tag in
`docker-compose.yml`. Both are updated by changing those lines in a normal pull request.

**Stopping.** `docker compose stop` pauses everything (`start` resumes), `docker compose down` removes the containers but keeps the data, and
`docker compose down -v` also deletes the volumes, which means your database and downloaded models.

**GPU for Ollama (optional).** With an NVIDIA card and the NVIDIA Container Toolkit installed on the host, remove the `#` signs from the `deploy:`
block under the `ollama` service in `docker-compose.yml`. It is left commented out and was not tested.

CI builds the image on every pull request and smoke-tests the demo container (it checks the dashboard answers, the user id is not 0 and the healthcheck passes), then starts the bridge with the login override
(a hash file made by the project's own helper): the healthcheck goes healthy, the sign-in works through the published port, the hash is not in `docker inspect`, PID 1 is uid 10001 with no capabilities, and the image refuses a LAN publish without a login. Nothing is pushed to any registry.
