# Setting up on a new computer

Copy the project folder over, then run the one-step setup. It works out what kind of computer it is on, builds a private
Python environment (`.venv`), installs the dependencies, and checks Ollama and the radio. It is safe to run again any time.

    setup.bat               # Windows: double-click it, or run it in a terminal
    ./setup.sh              # Linux / macOS
    python setup_env.py     # anywhere, if the two above don't suit (needs Python 3.9 or newer)

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
with `start_bridge.ps1` (Windows) or `./start_bridge.sh` (Linux / macOS); both use `.venv` automatically.

If `./setup.sh` says `bad interpreter` or similar, the file picked up Windows line endings on the way over; run
`python3 setup_env.py` instead - it repairs the `.sh` files itself.

## Connecting over Wi-Fi or Bluetooth

USB serial is the default and needs nothing. The bridge can also reach a radio without a cable. Choose one way; they cannot be combined.
All three keep the same behaviour: a lost link is noticed, the bridge keeps running (the dashboard says it is searching), messages
queued for the radio wait up to `--reconnect-hold` seconds, and it reconnects by itself when the radio is back, including after a power
cycle. If a different radio answers at the same address the "different radio" banner appears, as for USB.

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

## Start at login (optional)

    python setup_env.py --autostart       # start the bridge every time you log in (asks first; --yes skips the question)
    python setup_env.py --no-autostart    # take it out again
    python setup_env.py --check           # includes a "Start at login" line: installed / not installed

It is per user: no administrator or sudo, and nothing outside your own account is changed. It uses the `.venv` when there is one
(run plain `python setup_env.py` first on a new computer). What it creates: on **Windows** a Task Scheduler task named
`MeshLLMBridge` (runs `start_bridge.ps1` hidden at logon; if Windows says access is denied, run it from an administrator terminal
or put a shortcut to `start_bridge.ps1` in `shell:startup`); on **Linux** a systemd user service
`~/.config/systemd/user/mesh-llm-bridge.service` (restarts on failure; `loginctl enable-linger $USER` makes it start at boot instead
of at login; without systemd, e.g. plain WSL or a container, add `./start_bridge.sh` to the desktop's startup applications); on
**macOS** a launchd agent `~/Library/LaunchAgents/com.meshllm.bridge.plist` (logs in `logs/`). `--no-autostart` removes exactly
what `--autostart` made. A plain `python setup_env.py` never turns this on. Not yet run on a real Linux or Mac: the unit and plist
text and the commands are unit-tested only.

Tested: Windows 11 natively, and Linux (Ubuntu 24.04) under WSL (fresh build, a stale `.venv`, repairing line endings,
starting and stopping the bridge). macOS is covered by unit tests of the detection and install-command logic only, not run on a Mac.
The radio is not visible inside WSL; run the bridge on the Windows side, or attach the radio with usbipd-win.

## Run with Docker

The project can run as containers instead of through `setup.sh`: one for the bridge and dashboard, one for Ollama (the AI model server).
The files are `Dockerfile`, `docker-compose.yml`, `docker-compose.usb.yml`, `.env.example` and `docker/entrypoint.sh`.
It was built and tried on Linux with Docker 29.8.2 and Compose 5.5.1, running the demo and the bridge without a radio. **Not yet tried:**
Docker Desktop on Windows or macOS, a real radio from inside a container (USB or Wi-Fi), and the Ollama container (the model server was
not started in testing).

> **The dashboard has no login.** It shows message text and has a box that sends on your radio. The compose file therefore publishes
> it on **this computer's loopback only** (`127.0.0.1:8080`) and does not publish Ollama's port at all. **Do not change that address to
> `0.0.0.0` or to a LAN or public IP** until the login work (TODO.md item 3) is done. Docker adds its own firewall rules, so a port published on all
> interfaces is reachable from the network even when a host firewall such as `ufw` says it is blocked. To use the dashboard from another
> computer, forward the port over SSH (`ssh -L 8080:127.0.0.1:8080 host`) rather than publishing it. If you run the image without Compose, publish with `-p 127.0.0.1:8080:8080`, never `-p 8080:8080` (that would expose a login-less dashboard on your network).
>
> If the container says `/data is not writable by uid 10001`, the volume or bind-mounted folder has the wrong owner: make it writable for user id 10001 (a new named volume already is).

**What you need:** Docker Engine with the Compose plugin (`docker compose version` works), and disk space: about 250 MB for the bridge image, about 4 GB for the Ollama image (the demo does not need it), plus a few GB for
each model.

**Try it first, with no radio and no Ollama.** One command builds the image (about half a minute the first time) and starts the demo, a
simulated mesh with a scripted model, nothing transmitted and nothing kept:

    docker compose --profile demo up demo        # then open http://127.0.0.1:8080/ ; Ctrl+C stops it

**The real thing.**

    cp .env.example .env                          # optional: every line in it is a comment; edit what you need
    docker compose up -d --build                  # starts the bridge and Ollama
    docker compose exec ollama ollama pull llama3.2:3b     # download the AI model once (about 2 GB); it is stored in a volume
    docker compose logs -f bridge                 # what the bridge prints (the dashboard's log viewer reads log files, which this setup does not make)

Open <http://127.0.0.1:8080/>. With no radio connected the dashboard is up and says it is waiting for one. If port 8080 is taken on your computer
(for example by a bridge running outside Docker), set `MESHLLM_WEB_PORT=8081` in `.env`; only the number can change, not the address.
To use another model, pull it the same way and set `MESHLLM_MODEL` in `.env` (or choose it on the dashboard's Model page, which is remembered in the database).

**Choose how the radio is reached** (pick one; Bluetooth is not an option):

- **USB, Linux computers only.** Docker Desktop on Windows and macOS cannot pass a USB serial device into a container. On Linux, find the group
  that owns the device with `stat -c %g /dev/ttyUSB0`, put that number in `.env` as `MESHLLM_SERIAL_GID=<number>` (it is 20 on Debian and Ubuntu, but
  other distributions differ), and start with the override file, which gives the container that one device and that one group and nothing else:

      docker compose -f docker-compose.yml -f docker-compose.usb.yml up -d

  If your radio is not `/dev/ttyUSB0` (for instance `/dev/ttyACM0`), edit the two lines marked `device` in `docker-compose.usb.yml`. Only one program can hold
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
root filesystem (only `/data` and a memory-backed `/tmp` are writable), and no host networking. It starts with `restart: unless-stopped`. `docker stop`
ends the bridge in about a second with exit code 0 (the bridge turns SIGTERM into the same clean stop as Ctrl+C). Docker marks it *healthy* when
`/api/status` answers, which says nothing about whether a radio is connected.

**Updating.** Pull the new code, then rebuild and restart; the volume is kept:

    git pull
    docker compose up -d --build

The base image is pinned by tag and digest in the `Dockerfile` (Dependabot proposes new digests monthly), and the Ollama image by version tag in
`docker-compose.yml`. Both are updated by changing those lines in a normal pull request.

**Stopping.** `docker compose stop` pauses everything (`start` resumes), `docker compose down` removes the containers but keeps the data, and
`docker compose down -v` also deletes the volumes, which means your database and downloaded models.

**GPU for Ollama (optional).** With an NVIDIA card and the NVIDIA Container Toolkit installed on the host, remove the `#` signs from the `deploy:`
block under the `ollama` service in `docker-compose.yml`. It is left commented out and was not tested.

CI builds the image on every pull request and smoke-tests the demo container (it checks the dashboard answers, the user id is not 0 and the healthcheck passes);
nothing is pushed to any registry.
