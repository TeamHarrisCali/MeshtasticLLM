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
