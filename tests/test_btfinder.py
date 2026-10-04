"""The Connection page's Bluetooth finder: name matching, strict address validation, the one-at-a-time scan, the saved fallback and how start-up uses it.

Nothing real is touched: the Bluetooth scan is a fake function, the radio is a fake with real protobuf settings, Docker detection is injected, and
every database lives in a temp folder. Addresses, node ids and names are made-up ones (AA:BB:CC:DD:EE:FF, !00000a01, Meshtastic_0a01). The pairing
PIN in the fake radio is a throwaway number that must never appear in anything the finder returns, prints or stores."""
import contextlib, io, json, os, re, sys, tempfile, threading, time
from types import SimpleNamespace
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
from fixture import make, Checker
from meshllm import bridge as bridgemod, btfinder as BF, connection as conn, webroutes
from meshllm.audit import Audit
from meshtastic.protobuf import config_pb2, localonly_pb2

check = Checker()
HERE = tempfile.mkdtemp(prefix="meshbt_")
MAC, MAC2 = "AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE:02"
PIN = 482915           # the fake radio's Bluetooth PIN: it must never show up anywhere
out = io.StringIO()    # everything the code under test prints is collected here, to search it for the PIN


def until(cond, t=8.0):
    end = time.time() + t
    while time.time() < end:
        if cond(): return True
        time.sleep(0.02)
    return cond()


def dev(name, address): return SimpleNamespace(name=name, address=address)


def finder(node_id="!00000a01", devices=None, **kw):
    """(bridge, finder) with a fake radio whose node id is `node_id`, a Bluetooth section holding a PIN, and a fake scan returning `devices`."""
    br, radio, _ = make(tmp=tempfile.mkdtemp(dir=HERE), **kw)
    radio.getMyUser = lambda: {"id": node_id, "longName": "Test"}
    lc = localonly_pb2.LocalConfig()
    lc.bluetooth.enabled, lc.bluetooth.mode, lc.bluetooth.fixed_pin = True, config_pb2.Config.BluetoothConfig.FIXED_PIN, PIN
    radio.localNode.localConfig = lc
    f = br.btfinder
    f.scan_fn = lambda: list(devices or [])
    return br, f


# ---- the address: strict --------------------------------------------------------------------------------------------------
N = conn.normalise_mac
check("an address is normalised to upper case with colons (lower case, dashes and spaces around it are accepted)", N("aa:bb:cc:dd:ee:ff") == MAC and N("AA-BB-CC-DD-EE-FF") == MAC and N("  aa-bb-cc-dd-ee-ff ") == MAC and N(MAC) == MAC)
junk = ["", None, 123, "AA:BB:CC:DD:EE", "AA:BB:CC:DD:EE:FF:00", "AA:BB:CC:DD:EE:GG", "AABBCCDDEEFF", "AA:BB-CC:DD:EE:FF", "AA.BB.CC.DD.EE.FF", "ble:" + MAC, MAC + ";x",
        MAC + " extra", "Meshtastic_0a01", "AA:BB:CC:DD:EE:F", "A:B:C:D:E:F", "\x00" + MAC, "../etc/passwd", "AA:BB:CC:DD:EE:FF\nAA:BB:CC:DD:EE:01"]
def refuses(x):
    try: N(x); return False
    except ValueError: return True
check("anything that is not six hex pairs is refused (empty, wrong length, no separators, mixed separators, a prefix, a name, extra text)", all(refuses(x) for x in junk), [x for x in junk if not refuses(x)])

# ---- the name ----------------------------------------------------------------------------------------------------------------
check("the Bluetooth name is Meshtastic_ plus the last four hex digits of the node id, lower case", conn.expected_ble_name("!00000a01") == "Meshtastic_0a01" and conn.expected_ble_name("!0000ABCD") == "Meshtastic_abcd" and conn.expected_ble_name("!ffff0001") == "Meshtastic_0001")
check("something that is not a node id gives no name", all(conn.expected_ble_name(x) is None for x in (None, "", "00000a01", "!a01", "!0000000g", "!00000a011", 5)))
found = conn.matching_ble_devices([dev("Meshtastic_0a01", MAC), dev("Meshtastic_0b02", MAC2), dev(None, "AA:BB:CC:DD:EE:03"), dev("Other", "AA:BB:CC:DD:EE:04")], "Meshtastic_0a01")
check("only the device with the expected name is kept; other radios and nameless devices are dropped", found == [{"name": "Meshtastic_0a01", "address": MAC}], found)
check("the name is compared without regard to case", conn.matching_ble_devices([dev("MESHTASTIC_0A01", MAC)], "Meshtastic_0a01") == [{"name": "MESHTASTIC_0A01", "address": MAC}])
check("a name that only contains or extends the expected one is not a match", conn.matching_ble_devices([dev("Meshtastic_0a01x", MAC), dev("xMeshtastic_0a01", MAC), dev("Meshtastic_a01", MAC)], "Meshtastic_0a01") == [])
two = conn.matching_ble_devices([dev("Meshtastic_0a01", MAC), dev("Meshtastic_0a01", MAC2), dev("Meshtastic_0a01", MAC)], "Meshtastic_0a01")
check("several radios with the same name are all returned (one entry per address) so the owner can choose", [d["address"] for d in two] == [MAC, MAC2], two)
check("with no expected name nothing matches (never everything)", conn.matching_ble_devices([dev("Meshtastic_0a01", MAC)], None) == [] and conn.matching_ble_devices(None, "x") == [])

# ---- the scan, with a fake adapter ---------------------------------------------------------------------------------------------
br, f = finder(devices=[dev("Meshtastic_0a01", MAC), dev("Meshtastic_0b02", MAC2), dev("Meshtastic_0c03", "AA:BB:CC:DD:EE:05")])
with contextlib.redirect_stdout(out):
    started = f.start_scan()
    check("a scan starts at once and says which name it looks for", started == {"state": "scanning", "expected": "Meshtastic_0a01"}, started)
    check("it finishes with only the matching radio, whose address can be saved", until(lambda: f.scan_state()["state"] == "done"))
s = f.scan_state()
check("...the other radios in range are not in the answer at all (not their names, not their addresses)", s["candidates"] == [{"name": "Meshtastic_0a01", "address": MAC, "can_save": True}] and "0b02" not in json.dumps(s) and MAC2 not in json.dumps(s) and "0c03" not in json.dumps(s), s)
view = f.view(admin=True)
check("the admin's page data carries the expected name and the scan result", view["expected_name"] == "Meshtastic_0a01" and view["scan"]["state"] == "done" and view["scan_blocked"] is None)

br, f = finder(devices=[dev("Meshtastic_0b02", MAC2)])
with contextlib.redirect_stdout(out):
    f.start_scan(); until(lambda: f.scan_state()["state"] == "done")
s = f.scan_state()
check("no radio with that name: a clear 'not found' state with advice, and nothing listed", s["state"] == "done" and s["candidates"] == [] and "No radio advertising as Meshtastic_0a01" in s["message"] and "connected" in s["message"], s)

# the same address as a macOS UUID cannot be saved, and the page is told
br, f = finder(devices=[dev("Meshtastic_0a01", "6F1D3A52-0000-4000-8000-0123456789AB")])
with contextlib.redirect_stdout(out):
    f.start_scan(); until(lambda: f.scan_state()["state"] == "done")
check("an address that is not a MAC (as macOS reports) is listed as not saveable", f.scan_state()["candidates"][0]["can_save"] is False)

# single flight: a second scan while one runs is refused as busy and never starts a second discovery
gate, calls = threading.Event(), []
def slow():
    calls.append(1); gate.wait(10); return [dev("Meshtastic_0a01", MAC)]
br, f = finder(); f.scan_fn = slow
with contextlib.redirect_stdout(out):
    f.start_scan(); until(lambda: calls)
    try: f.start_scan(); err = None
    except BF.FinderError as e: err = e
    check("a scan already running: the second request is refused as busy (409) with a message", err is not None and err.state == "busy" and err.code == 409 and "already running" in str(err), err)
    check("...and only one discovery was ever started", len(calls) == 1)
    check("...the page data says it is scanning and blocks nothing it should not", f.view(admin=True)["scan"]["state"] == "scanning")
    gate.set(); until(lambda: f.scan_state()["state"] == "done")
    f.start_scan(); until(lambda: len(calls) == 2 and f.scan_state()["state"] == "done")
check("once it has finished a new scan can be started", len(calls) == 2)
racers, results = [], []
gate2, calls2 = threading.Event(), []
def slow2():
    calls2.append(1); gate2.wait(10); return []
br, f = finder(); f.scan_fn = slow2
def go():
    try: f.start_scan(); results.append("started")
    except BF.FinderError as e: results.append(e.state)
with contextlib.redirect_stdout(out):
    racers = [threading.Thread(target=go) for _ in range(8)]
    for t in racers: t.start()
    for t in racers: t.join()
    gate2.set(); until(lambda: f.scan_state()["state"] == "done")
check("eight scans requested at the same moment: exactly one starts, the other seven are busy", sorted(results) == ["busy"] * 7 + ["started"] and len(calls2) == 1, results)

# failures
def fails_with(exc):
    br, f = finder()
    def boom(): raise exc
    f.scan_fn = boom
    with contextlib.redirect_stdout(out):
        f.start_scan(); until(lambda: f.scan_state()["state"] != "scanning")
    return f.scan_state()
s = fails_with(conn.BleUnavailable("Bluetooth is not available here (ImportError: x)"))
check("bleak/BlueZ missing during the scan: state 'unavailable' with the library's own sentence", s["state"] == "unavailable" and "not available" in s["message"], s)
s = fails_with(conn.BleTimeout("Bluetooth scan timed out after 25 s while scanning"))
check("a scan timeout is an 'error' state with a message, not a crash or a hang", s["state"] == "error" and "timed out" in s["message"], s)
s = fails_with(OSError("org.bluez.Error.NotReady"))
check("an adapter that is off or missing is an 'error' state with a message", s["state"] == "error" and "scan failed" in s["message"] and "NotReady" in s["message"], s)
br, f = finder()
f.ble_check = lambda: (_ for _ in ()).throw(conn.BleUnavailable("Bluetooth is not available here (ModuleNotFoundError: bleak)"))
try: f.start_scan(); err = None
except BF.FinderError as e: err = e
check("no Bluetooth support at all is reported at once (400, 'unavailable') and no thread is started", err is not None and err.state == "unavailable" and err.code == 400 and f.scan_state()["state"] == "idle", err)

# ---- when a scan must not run -------------------------------------------------------------------------------------------------
br, f = finder(); f.container = lambda: True
try: f.start_scan(); err = None
except BF.FinderError as e: err = e
check("in Docker the scan is refused and says why in plain words, and nothing is scanned", err is not None and err.state == "unavailable" and "Docker" in str(err) and "--ble-scan" in str(err), err)
check("...the page data reports it too (the button is disabled with that sentence)", f.view(admin=True)["scan_blocked"] == BF.DOCKER_MESSAGE and f.view(admin=True)["container"] is True)
try: f.save(MAC); err = None
except BF.FinderError as e: err = e
check("...and a fallback cannot be saved from inside Docker (it could never be used there)", err is not None and br.audit.get_setting(conn.SAVED_FALLBACK_KEY) is None)
check("Docker detection: the image's variable, or Docker's own marker file", BF.in_container({"MESHLLM_CONTAINER": "1"}, dockerenv=os.path.join(HERE, "nope")) and BF.in_container({}, dockerenv=__file__) and not BF.in_container({}, dockerenv=os.path.join(HERE, "nope")) and not BF.in_container({"MESHLLM_CONTAINER": "0"}, dockerenv=os.path.join(HERE, "nope")))
br, f = finder(); br.iface = None
try: f.start_scan(); err = None
except BF.FinderError as e: err = e
check("no radio connected: refused, because its node id (and so its name) is unknown", err is not None and err.state == "refused" and "USB" in str(err))
br, f = finder(node_id="not-an-id")
check("a radio that gives no usable node id: refused, not scanning for 'everything'", f.blocked() is not None and "node id" in f.blocked())
br, f = finder(); br.endpoint = conn.BleEndpoint(MAC)
try: f.start_scan(); err = None
except BF.FinderError as e: err = e
check("while the bridge is connected over Bluetooth the scan is refused with the reason (a connected radio does not advertise)", err is not None and "advertising" in str(err) and f.active_kind() == "ble", err)
br, f = finder(); br.endpoint = conn.FailoverChain([conn.SerialEndpoint("STUB"), conn.BleEndpoint(MAC)])
check("a USB-first chain that is connected over USB may scan", f.blocked() is None)
br, f = finder(demo=True)
check("demo mode has no real Bluetooth: refused", f.blocked() is not None and "Demo" in f.blocked())

# ---- the radio's own Bluetooth settings, and the PIN ---------------------------------------------------------------------------------
br, f = finder()
bt = f.radio_bluetooth()
check("the radio's Bluetooth state: on, and the pairing mode in words", bt == {"known": True, "enabled": True, "mode": "FIXED_PIN", "mode_text": "fixed PIN"}, bt)
br.iface = None
check("not connected: 'not known' with a reason", f.radio_bluetooth()["known"] is False and "not connected" in f.radio_bluetooth()["reason"])
src = open(os.path.join(ROOT, "meshllm", "btfinder.py"), encoding="utf-8").read()
js = open(os.path.join(ROOT, "meshllm", "static", "js", "connection.js"), encoding="utf-8").read()
check("the code never touches the PIN field (it reads 'enabled' and 'mode' only), and the page script never mentions it", not re.search(r"\.fixed_pin|[\"']fixed_pin[\"']", src) and "fixed_pin" not in js and "localConfig.bluetooth" in src)

# ---- the saved fallback ----------------------------------------------------------------------------------------------------------
br, f = finder()
with contextlib.redirect_stdout(out):
    r = f.save("aa-bb-cc-dd-ee-ff")
check("saving stores the normalised address in the settings database and says a restart is needed", br.audit.get_setting(conn.SAVED_FALLBACK_KEY) == MAC and r["saved"] == MAC and r["restart_needed"] is True and "Restart" in r["message"] and r["ok"], r)
for bad in ("", "junk", "ble:" + MAC, "AA:BB:CC", None, 5, MAC + "00"):
    try: f.save(bad); ok = False
    except ValueError: ok = True
    check(f"saving {bad!r} is refused and changes nothing", ok and br.audit.get_setting(conn.SAVED_FALLBACK_KEY) == MAC)
v = f.view(admin=True)
check("the page data shows the saved address and that the running bridge does not use it yet", v["saved"] == MAC and v["restart_needed"] is True and v["in_use"] is None)
with contextlib.redirect_stdout(out):
    r = f.clear()
check("clearing removes it; with nothing running from it no restart is needed", br.audit.get_setting(conn.SAVED_FALLBACK_KEY) is None and r["saved"] is None and r["restart_needed"] is False and f.view(admin=True)["saved"] is None, r)
br, f = finder(fallback=[("usb", "auto")])
with contextlib.redirect_stdout(out):
    r = f.save(MAC)
check("when a --fallback flag is in force the reply says the flag wins and no restart changes that", r["flags_override"] is True and r["restart_needed"] is False and "always wins" in r["message"] and f.view(admin=True)["flags_override"] is True, r)
check("saving prints no address (the log line says only that one was saved)", MAC not in out.getvalue())

# ---- start-up: the saved fallback only when no flags were given, and flags win -----------------------------------------------------------------
def started(saved, **flags):
    tmp = tempfile.mkdtemp(dir=HERE)
    if saved is not None: Audit(os.path.join(tmp, "t.db")).set_setting(conn.SAVED_FALLBACK_KEY, saved)
    with contextlib.redirect_stdout(out):
        br, _, _ = make(tmp=tmp, **flags)
    return br
def chain(br): return [(e.kind, getattr(e, "target", None) or getattr(e, "port", None) or getattr(e, "host", None)) for e in getattr(br.endpoint, "endpoints", [br.endpoint])]
br = started(MAC)
check("a saved fallback and no flags: USB first, then that Bluetooth address (a failover chain)", isinstance(br.endpoint, conn.FailoverChain) and [k for k, _ in chain(br)] == ["usb", "ble"] and chain(br)[1] == ("ble", MAC), chain(br))
check("...and the bridge remembers it started from the saved one", br.saved_fallback == ("ble", MAC) and br.btfinder.view(admin=True)["restart_needed"] is False and br.btfinder.view(admin=True)["in_use"] == MAC)
br = started(MAC, port="/dev/ttyUSB9")
check("a pinned --port is a primary, not a fallback flag: the saved fallback still applies behind it", chain(br)[1:] == [("ble", MAC)] and chain(br)[0][1] == "/dev/ttyUSB9", chain(br))
br = started(MAC, fallback=[("tcp", "radio.test:4403")])
check("a --fallback flag wins: only the flag's entries, the saved address is ignored", [k for k, _ in chain(br)] == ["usb", "tcp"] and br.saved_fallback is None, chain(br))
br = started(MAC, tcp="radio.test")
check("--tcp wins: a Wi-Fi primary with no fallback from the saved address", not isinstance(br.endpoint, conn.FailoverChain) and br.endpoint.kind == "tcp" and br.saved_fallback is None)
br = started(MAC, ble="Meshtastic_0a01")
check("--ble wins: a Bluetooth primary with no fallback from the saved address", not isinstance(br.endpoint, conn.FailoverChain) and br.endpoint.kind == "ble" and br.saved_fallback is None)
br = started(MAC, demo=True)
check("demo mode ignores the saved address", br.saved_fallback is None and not isinstance(br.endpoint, conn.FailoverChain))
for bad in ("not an address", "", "AA:BB"):
    br = started(bad)
    check(f"a damaged saved value ({bad!r}) is ignored at start-up and does not stop the bridge", br.saved_fallback is None and not isinstance(br.endpoint, conn.FailoverChain))
br = started(None)
check("nothing saved: the plain USB endpoint, exactly as before", br.saved_fallback is None and isinstance(br.endpoint, conn.SerialEndpoint))
br = started(MAC, link_silence=None)
saved_after = br.btfinder
br.audit.set_setting(conn.SAVED_FALLBACK_KEY, MAC2)
check("a change saved while running asks for a restart (the running chain is not rebuilt)", saved_after.view(admin=True)["restart_needed"] is True and chain(br)[1:] == [("ble", MAC)])
# through the real command line parser
_, ns = bridgemod.parse_cli([])
check("the real command line with no flags leaves the saved fallback to apply; any of the three flags switches it off", conn.saved_fallback(ns, MAC) == ("ble", MAC) and all(conn.saved_fallback(bridgemod.parse_cli(a)[1], MAC) is None for a in (["--fallback", "usb:/dev/ttyUSB1"], ["--tcp", "radio.test"], ["--ble", "Meshtastic_0a01"])))
check("make_endpoint with the saved entry builds the same chain a '--fallback ble:ADDRESS' flag would", [e.kind for e in conn.make_endpoint(ns, saved=("ble", MAC)).endpoints] == [e.kind for e in conn.make_endpoint(bridgemod.parse_cli(["--fallback", "ble:" + MAC])[1]).endpoints])

# ---- the routes, called directly (the roles, CSRF and Origin are checked for every route in test_websecurity) ------------------------------------
br, f = finder(devices=[dev("Meshtastic_0a01", MAC)])
f.container = lambda: True
try: webroutes.POST["/api/connection/ble/scan"](br, {}); err = None
except webroutes.HttpError as e: err = e
check("the scan route in Docker: 409 with state 'unavailable' and the message", err is not None and err.code == 409 and err.extra == {"state": "unavailable"} and "Docker" in err.message, err)
f.container = lambda: False
with contextlib.redirect_stdout(out):
    reply = webroutes.POST["/api/connection/ble/scan"](br, {}); until(lambda: f.scan_state()["state"] == "done")
check("the scan route starts the scan and returns at once", reply["state"] == "scanning" and f.scan_state()["candidates"][0]["address"] == MAC, reply)
with contextlib.redirect_stdout(out):
    check("the save route stores the address", webroutes.POST["/api/connection/ble/save"](br, {"address": MAC})["saved"] == MAC and br.audit.get_setting(conn.SAVED_FALLBACK_KEY) == MAC)
    try: webroutes.POST["/api/connection/ble/save"](br, {"address": "junk"}); err = None
    except ValueError as e: err = e
check("the save route refuses junk with a ValueError (which the server turns into a 400 with that message)", err is not None and isinstance(err, webroutes.USER_ERRORS) and "Bluetooth address" in str(err))
with contextlib.redirect_stdout(out):
    check("the clear route forgets it", webroutes.POST["/api/connection/ble/clear"](br, {})["saved"] is None and br.audit.get_setting(conn.SAVED_FALLBACK_KEY) is None)
viewer = br.btfinder.view(admin=False)
check("the viewer's view has no address, name, saved value or scan: kinds only", set(viewer) == {"connected", "kind", "failover", "entries", "text", "admin", "bluetooth", "port"} and viewer["port"] == "USB" and "Meshtastic_" not in json.dumps(viewer))

# ---- the PIN is nowhere ----------------------------------------------------------------------------------------------------------------------
everything = json.dumps([f.view(admin=True), f.view(admin=False), f.scan_state(), f.radio_bluetooth()]) + out.getvalue()
check("the PIN does not appear in any page data, scan state, reply or anything printed", str(PIN) not in everything and "fixed_pin" not in everything)

check.done()
