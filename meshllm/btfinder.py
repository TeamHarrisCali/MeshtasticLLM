"""The "Connection" page: how the bridge is connected, what the radio says about its Bluetooth, and finding its Bluetooth address.

What a radio tells you over USB, and what it does not:

* It DOES report its Bluetooth settings: whether Bluetooth is on and the pairing mode (random PIN, fixed PIN or no PIN). Those two are read
  here from the settings the radio sent when we connected.
* It does NOT report its Bluetooth ADDRESS. The only hints are its name, which is Meshtastic_ plus the last 4 hex digits of its node id, and a
  scan of what is advertising nearby. So the address is found by scanning the PC's own Bluetooth adapter and keeping only the device with that
  name. That is the one thing this module scans for; every other radio in range is dropped and never shown.
* The pairing PIN is a credential. Nothing here reads, shows, logs or stores it: only the two fields above are ever looked at.

The scan reuses `connection.safe_ble_scan` (an unfiltered discovery, because the library's filtered one crashed bluetoothd on one real
set-up) on a background thread, one at a time. It cannot work inside Docker (the container has no Bluetooth adapter) or without bleak and BlueZ,
and says so plainly. A chosen address is saved in the settings database and used as the Bluetooth fallback at the next start (see
`connection.saved_fallback`); command-line flags always win. Nothing here transmits anything or changes the radio."""
import os
import threading
import time

from meshllm import connection

# what the radio's Bluetooth pairing mode means, in the words the page shows (the PIN itself is never read)
PAIRING = {"RANDOM_PIN": "random PIN (shown on the radio's screen)", "FIXED_PIN": "fixed PIN", "NO_PIN": "no PIN (anyone nearby can pair)"}
SCAN_KEEP = 300             # seconds a finished scan's answer stays on the page before it is forgotten
SCAN_STALE = 60             # seconds after which a scan that never reported back no longer blocks a new one (the scan itself times out at 25)
ERROR_COOLDOWN = 30         # seconds after a failed (for example timed-out) scan before another may start: BlueZ may still be finishing the old discovery
DOCKER_MESSAGE = ("Bluetooth is not available in Docker: the container cannot see this computer's Bluetooth adapter. "
                  "Run the bridge directly on the PC (not in Docker) to find the address, or run  python -m meshllm --ble-scan  there.")


class FinderError(Exception):
    """The request cannot be done now. `state` says why in one word (unavailable, refused, busy); the message is for the operator."""

    def __init__(self, state, message, code=409):
        super().__init__(message)
        self.state, self.code = state, code


def in_container(environ=None, dockerenv="/.dockerenv"):
    """True inside a Docker container: the image sets MESHLLM_CONTAINER=1, and Docker creates /.dockerenv."""
    environ = os.environ if environ is None else environ
    return environ.get("MESHLLM_CONTAINER") == "1" or os.path.exists(dockerenv)


class BluetoothFinder:
    """Reads the connection and the radio's Bluetooth settings, runs the one-at-a-time scan, and saves or clears the fallback address."""

    def __init__(self, bridge, scan=None, container=in_container, clock=time.time, ble_check=None):
        self.bridge = bridge
        self.scan_fn = scan or connection.safe_ble_scan       # tests replace it: no real Bluetooth is touched
        self.ble_check = ble_check or connection.load_ble     # raises BleUnavailable when bleak/BlueZ support cannot be loaded
        self.container = container
        self.clock = clock
        self.lock = threading.Lock()                          # guards `scan` and makes starting a scan atomic: one at a time
        self.scan = {"state": "idle"}                         # idle | scanning | done | error | unavailable

    # ---- what we know ----------------------------------------------------------------------------------------------
    def _iface(self):
        """The connected radio's interface, or None."""
        return self.bridge.iface

    def node_id(self):
        """The connected radio's node id ('!00000a01'), or None when it is not connected or cannot say."""
        iface = self._iface()
        try:
            return iface.getMyUser().get("id") if iface is not None else None
        except Exception:
            return None

    def active_kind(self):
        """'usb', 'tcp' or 'ble' for the link that is live now, or None when nothing is connected."""
        b = self.bridge
        if b.iface is None:
            return None
        info = b.endpoint.connection_info(True)
        if info.get("failover"):
            return next((e["kind"] for e in info["entries"] if e.get("state") == "active"), None)
        return getattr(b.endpoint, "kind", None) or None

    def radio_bluetooth(self):
        """{"known", "enabled", "mode", "mode_text"} from the radio's own settings, or {"known": False, "reason"}. Reads ONLY the enabled and
        mode fields of the Bluetooth section, so the pairing PIN next to them is never touched."""
        iface = self._iface()
        if iface is None:
            return {"known": False, "reason": "The radio is not connected."}
        try:
            bt = iface.localNode.localConfig.bluetooth
            mode = bt.DESCRIPTOR.fields_by_name["mode"].enum_type.values_by_number[bt.mode].name
            return {"known": True, "enabled": bool(bt.enabled), "mode": mode, "mode_text": PAIRING.get(mode, mode.lower().replace("_", " "))}
        except Exception:
            return {"known": False, "reason": "The radio has not sent its Bluetooth settings."}

    def blocked(self, node_id=False):
        """Why a scan cannot start right now (a sentence), or None when it can. Checked again when the scan is requested.
        `node_id` is the id already read by the caller (so one request reads it once); by default it is read here."""
        if node_id is False:
            node_id = self.node_id()
        if getattr(self.bridge.args, "demo", False):
            return "Demo mode has no real radio or Bluetooth."
        if self.container():
            return DOCKER_MESSAGE
        if self._iface() is None:
            return "Connect the radio by USB first: the bridge needs its node id to know which Bluetooth name to look for."
        if self.active_kind() == "ble":
            return ("The bridge is connected over Bluetooth right now. A radio that is connected stops advertising, so a scan cannot find it, "
                    "and scanning could disturb the live link. Its address is the one in the connection list above.")
        if connection.expected_ble_name(node_id) is None:
            return "The radio did not report its node id, so its Bluetooth name cannot be worked out."
        return None

    def saved(self):
        """The saved fallback address (normalised), or None."""
        value = self.bridge.audit.get_setting(connection.SAVED_FALLBACK_KEY)
        try:
            return connection.normalise_mac(value) if value else None
        except ValueError:
            return None

    def flags_override(self):
        """True when a command-line flag (--fallback, --tcp or --ble) is set, so a saved fallback is not used."""
        a = self.bridge.args
        return bool(getattr(a, "fallback", None) or getattr(a, "tcp", None) is not None or getattr(a, "ble", None) is not None)

    # ---- the page's data ---------------------------------------------------------------------------------------------
    def view(self, admin):
        """What the Connection page shows. A read-only account gets the chain's kinds and whether the radio's Bluetooth is on, and nothing that
        names an address, the radio's Bluetooth name or pairing mode, the saved fallback or the scan."""
        b = self.bridge
        info = b.endpoint.connection_info(b.iface is not None)
        entries = [dict(e) for e in info.get("entries") or []]
        connected = b.iface is not None
        if not admin:
            entries = [dict(e, label=connection.KIND_NAMES.get(e.get("kind"), "radio")) for e in entries]
        out = {"connected": connected, "kind": self.active_kind(), "failover": bool(info.get("failover")), "entries": entries,
               "text": info.get("text") or "", "admin": bool(admin), "bluetooth": self.radio_bluetooth()}
        if admin:
            out["port"] = b.port or b.endpoint.initial_label()
        else:
            out["port"] = connection.KIND_NAMES.get(out["kind"], "radio") if connected else None
        if not admin:
            bt = out["bluetooth"]       # the pairing mode (it can read "no PIN") is for the admin; a viewer is told only whether Bluetooth is on
            out["bluetooth"] = {k: v for k, v in bt.items() if k not in ("mode", "mode_text")}
            return out
        saved, running = self.saved(), b.saved_fallback[1] if b.saved_fallback else None
        flags = self.flags_override()
        why = self.blocked()
        out.update(expected_name=connection.expected_ble_name(self.node_id()), saved=saved, flags_override=flags,
                   restart_needed=(saved != running) and not flags, in_use=running, container=bool(self.container()),
                   scan_blocked=why, scan=self.scan_state())
        return out

    def scan_state(self):
        """The current scan's state for the page: {"state", and when finished "candidates", "message", "expected"}. A finished scan is forgotten
        after SCAN_KEEP seconds."""
        with self.lock:
            s = dict(self.scan)
            if s["state"] in ("done", "error", "unavailable") and self.clock() - s.get("finished", 0) > SCAN_KEEP:
                self.scan = {"state": "idle", "gen": s.get("gen", 0)}
                return {"state": "idle"}
            if s["state"] == "scanning" and self.clock() - s.get("started", 0) > SCAN_STALE:
                return {"state": "error", "expected": s.get("expected"), "candidates": [],
                        "message": "The last scan never finished. You can start a new one."}
        for k in ("finished", "started", "gen"):
            s.pop(k, None)
        return s

    # ---- the scan -------------------------------------------------------------------------------------------------
    def start_scan(self):
        """Begin one scan on a background thread and return at once ({"state": "scanning"}). Raises FinderError when it cannot run (Docker, no
        Bluetooth support, nothing to look for, a Bluetooth link already live) or when a scan is already running (state 'busy')."""
        node_id = self.node_id()                   # read once: the radio may drop between two reads, and the check and the name must agree
        why = self.blocked(node_id)
        if why:
            raise FinderError("unavailable" if self.container() else "refused", why)
        expected = connection.expected_ble_name(node_id)
        if expected is None:
            raise FinderError("refused", "The radio did not report its node id, so its Bluetooth name cannot be worked out.")
        try:
            self.ble_check()                       # fail now, in one line, when bleak or BlueZ support is missing
        except connection.BleUnavailable as e:
            raise FinderError("unavailable", str(e), 400) from None
        except Exception as e:
            raise FinderError("unavailable", f"Bluetooth support could not be loaded ({type(e).__name__}: {str(e)[:80]}).", 400) from None
        with self.lock:
            now, cur = self.clock(), self.scan
            if cur["state"] == "scanning" and now - cur.get("started", 0) <= SCAN_STALE:
                raise FinderError("busy", "A Bluetooth scan is already running. Wait for it to finish (up to about 25 seconds).")
            if cur["state"] == "error" and now - cur.get("finished", 0) < ERROR_COOLDOWN:
                raise FinderError("busy", "The last scan failed a moment ago and Bluetooth may still be settling. Try again in about half a minute.")
            gen = cur.get("gen", 0) + 1            # a scan that was given up on as stale must not write its late result over this one
            self.scan = {"state": "scanning", "expected": expected, "started": now, "gen": gen}
        try:
            threading.Thread(target=self._run, args=(expected, gen), daemon=True, name="ble-find").start()
        except BaseException:                      # no thread could be started: do not leave the page 'scanning' for good
            with self.lock:
                if self.scan.get("gen") == gen:
                    self.scan = {"state": "idle", "gen": gen}
            raise
        return {"state": "scanning", "expected": expected}

    def _run(self, expected, gen):
        """The scan thread: run the scan, keep only the device with the expected name, and record the outcome."""
        try:
            devices = self.scan_fn()
            found = connection.matching_ble_devices(devices, expected)
            for d in found:
                d["can_save"] = _is_mac(d["address"])
            result = {"state": "done", "expected": expected, "candidates": found,
                      "message": (f"Found {len(found)} radio{'s' if len(found) != 1 else ''} advertising as {expected}." if found else
                                  f"No radio advertising as {expected} was found. Is it powered, in range and not connected to a phone or another computer? "
                                  "(A connected radio stops advertising.)")}
        except connection.BleUnavailable as e:
            result = {"state": "unavailable", "expected": expected, "candidates": [], "message": str(e)}
        except Exception as e:                       # a timeout, the adapter switched off, no BlueZ/D-Bus, no permission
            result = {"state": "error", "expected": expected, "candidates": [],
                      "message": f"The Bluetooth scan failed ({type(e).__name__}: {str(e)[:100]})."}
        with self.lock:
            if self.scan.get("gen") != gen:          # a newer scan took over after this one went stale: its answer is not wanted
                return
            self.scan = dict(result, finished=self.clock(), gen=gen)
        print(f"[connection] Bluetooth scan finished: {result['state']}, {len(result['candidates'])} match(es)", flush=True)

    # ---- the saved fallback -----------------------------------------------------------------------------------------------
    def save(self, address):
        """Save `address` as the Bluetooth fallback. Strict: only a MAC address is accepted. Takes effect at the next start of the bridge."""
        if getattr(self.bridge.args, "demo", False):
            raise FinderError("refused", "Demo mode does not save settings.")
        if self.container():
            raise FinderError("unavailable", DOCKER_MESSAGE)
        mac = connection.normalise_mac(address)       # ValueError -> a 400 with its message
        self.bridge.audit.set_setting(connection.SAVED_FALLBACK_KEY, mac)
        print("[connection] a Bluetooth fallback address was saved from the dashboard (applies at the next start)", flush=True)
        return self._saved_reply("Saved. Restart the bridge to use it: USB stays first and Bluetooth takes over when the radio is unplugged.")

    def clear(self):
        """Forget the saved Bluetooth fallback. Takes effect at the next start of the bridge."""
        self.bridge.audit.delete_setting(connection.SAVED_FALLBACK_KEY)
        print("[connection] the saved Bluetooth fallback was cleared from the dashboard (applies at the next start)", flush=True)
        return self._saved_reply("Cleared. Restart the bridge for it to stop using Bluetooth as a fallback.")

    def _saved_reply(self, message):
        """The reply to a save or clear: the new saved value, whether a restart is needed, and a note when a command-line flag overrides it."""
        saved, running = self.saved(), self.bridge.saved_fallback[1] if self.bridge.saved_fallback else None
        flags = self.flags_override()
        if flags:
            message += " Note: this bridge was started with --fallback, --tcp or --ble, which always wins; the saved address is used only when you start it without them."
        return {"ok": True, "saved": saved, "flags_override": flags, "restart_needed": saved != running and not flags, "message": message}


def _is_mac(address):
    """True when `address` is a MAC address that save() would accept (macOS reports a UUID instead, which cannot be saved)."""
    try:
        connection.normalise_mac(address)
        return True
    except ValueError:
        return False
