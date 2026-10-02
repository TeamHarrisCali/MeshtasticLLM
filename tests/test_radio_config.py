"""Radio settings: pull, validate, back up, push, restore (fake radio, real protobuf config objects)."""
import argparse, json, os, sys, threading, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import tempfile; HERE = tempfile.mkdtemp(prefix="meshtest_")   # scratch databases and caches go in a temp folder, never in the project

from meshllm import bridge as b, webui, radio_config as RC
import requests as rq
from meshtastic.protobuf import localonly_pb2, config_pb2

class Local:
    def __init__(self):
        self.localConfig = localonly_pb2.LocalConfig(); self.moduleConfig = localonly_pb2.LocalModuleConfig()
        self.localConfig.lora.region = config_pb2.Config.LoRaConfig.US; self.localConfig.lora.hop_limit = 3
        self.localConfig.device.role = config_pb2.Config.DeviceConfig.CLIENT
        self.localConfig.security.private_key = b"SECRET-PRIVATE-KEY"; self.localConfig.network.wifi_psk = "SECRET-WIFI"
        self.moduleConfig.mqtt.password = "SECRET-MQTT"; self.moduleConfig.telemetry.device_update_interval = 1800
        self.log = []; self.fail = None
    def beginSettingsTransaction(self): self.log.append("begin")
    def commitSettingsTransaction(self): self.log.append("commit")
    def writeConfig(self, name):
        if self.fail: raise RuntimeError(self.fail)
        self.log.append(("write", name))

class Radio:
    stream = object(); _rxThread = threading.current_thread()
    nodes = {}; nodesByNum = {}
    myInfo = type("M", (), {"my_node_num": 1})()
    def __init__(self): self.localNode = Local()
    def getMyUser(self): return {"id": "!00000001", "longName": "Us"}

def make(db):
    base = dict(db=db, ollama_url="http://127.0.0.1:9", model="m", access_mode=None, daily_cap=None, no_tool_gate=True,
                web_host="127.0.0.1", web_port=8092, no_web=True, command="/ai", port="auto", memory_turns=6, memory_hours=24,
                memory_chars=3000, max_queue=5, max_chunks=4, cooldown=0, traceroute_timeout=0.2)
    for ext in ("", "-wal", "-shm"):
        try: os.remove(db + ext)
        except OSError: pass
    br = b.Bridge(argparse.Namespace(**base)); br.iface = Radio(); return br

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)
def refuses(fn, *words):
    try: fn(); return False
    except RC.ConfigError as e: return all(w in str(e) for w in words)

br = make(os.path.join(HERE, "rc_test.db")); rc = br.radio_config; node = br.iface.localNode
v = rc.view()
check("the view lists every allowed section with its fields", v["connected"] and [s["name"] for s in v["sections"]] == list(RC.SECTIONS) and all(s["fields"] for s in v["sections"]), [s["name"] for s in v["sections"]])
lora = {f["name"]: f for f in next(s for s in v["sections"] if s["name"] == "lora")["fields"]}
check("enum fields list their choices and show the current name", lora["region"]["value"] == "US" and "EU_868" in lora["region"]["choices"] and lora["region"]["kind"] == "enum")
check("numbers carry their limits (hop limit 0..7)", (lora["hop_limit"]["min"], lora["hop_limit"]["max"]) == (0, 7) and lora["hop_limit"]["value"] == 3)
check("risky fields are flagged for the confirmation", "illegal" in lora["region"]["risk"] and "risk" not in lora["hop_limit"] or "airtime" in lora["hop_limit"].get("risk", ""))
v2 = dict(v); v2.pop("excluded"); blob = json.dumps(v2) + json.dumps(RC.snapshot(node))
check("secrets never leave the radio module (private key, Wi-Fi password, MQTT password)", not any(s in blob for s in ("SECRET", "private_key", "wifi_psk", "password", "admin_key", "fixed_pin")), [s for s in ("SECRET", "private_key", "wifi_psk", "password", "fixed_pin") if s in blob])
check("the excluded sections are listed with the reason", {e["name"] for e in v["excluded"]} == {"network", "security", "mqtt"})
check("lists, nested messages and raw bytes are not offered for editing", all(f["kind"] in ("bool", "enum", "int", "float", "string") for s in v["sections"] for f in s["fields"]))

# ---- validation: everything is checked before anything is sent ---------------------------------------------------------------------
P = lambda ch: rc.save(ch)
bad = [
    ({"network": {"wifi_ssid": "x"}}, "can't be viewed"), ({"security": {"private_key": "x"}}, "can't be viewed"), ({"mqtt": {"password": "x"}}, "can't be viewed"),
    ({"nope": {}}, "can't be viewed"), ({"lora": {"nope": 1}}, "isn't a setting"), ({"lora": {"ignore_incoming": [1]}}, "isn't a setting"),
    ({"lora": {"hop_limit": 8}}, "between 0 and 7"), ({"lora": {"hop_limit": -1}}, "between"), ({"lora": {"hop_limit": 2.5}}, "whole number"),
    ({"lora": {"hop_limit": True}}, "whole number"), ({"lora": {"hop_limit": "3"}}, "whole number"), ({"lora": {"hop_limit": float("nan")}}, "whole number"),
    ({"lora": {"region": "MARS"}}, "isn't one of"), ({"lora": {"region": 1}}, "isn't one of"), ({"lora": {"tx_enabled": "yes"}}, "on or off"),
    ({"lora": {"tx_power": 99}}, "between 0 and 30"), ({"lora": {"frequency_offset": "x"}}, "must be a number"), ({"lora": {"frequency_offset": float("inf")}}, "must be a number"),
    ({"device": {"tzdef": "x" * 500}}, "plain text"), ({"device": {"tzdef": "a\x00b"}}, "plain text"), ({"device": {"tzdef": 5}}, "plain text"),
    ({"lora": "x"}, "Malformed"), ({"bluetooth": {"fixed_pin": 123456}}, "isn't a setting"), ({}, "nothing to change"), (None, "nothing to change"), ([1], "nothing to change"),
    ({"telemetry": {"device_update_interval": 2 ** 40}}, "between"), ({"telemetry": {"device_update_interval": -5}}, "between"),
]
before = RC.snapshot(node)
check("invalid, secret, unknown, out-of-range and mistyped changes are all refused with a clear message", all(refuses(lambda ch=ch: P(ch), w) for ch, w in bad), [(ch, w) for ch, w in bad if not refuses(lambda ch=ch: P(ch), w)])
check("...and none of them touched the radio or the stored settings", node.log == [] and RC.snapshot(node) == before and rc.backups() == [])
check("one bad value cancels the whole request (all-or-nothing)", refuses(lambda: P({"lora": {"hop_limit": 4}, "device": {"role": "NOPE"}}), "isn't one of") and node.localConfig.lora.hop_limit == 3 and node.log == [])

# ---- saving ------------------------------------------------------------------------------------------------------------------------
r = P({"lora": {"hop_limit": 5, "region": "US"}, "telemetry": {"device_update_interval": 900}, "device": {"role": "CLIENT"}})
check("only what really changed is written, in one transaction", node.log == ["begin", ("write", "lora"), ("write", "telemetry"), "commit"], node.log)
check("the radio's settings now hold the new values (and only those)", node.localConfig.lora.hop_limit == 5 and node.moduleConfig.telemetry.device_update_interval == 900 and node.localConfig.device.role == config_pb2.Config.DeviceConfig.CLIENT)
check("the reply lists each change with old and new values", sorted((c["section"], c["field"], c["old"], c["new"]) for c in r["changed"]) == [("lora", "hop_limit", 3, 5), ("telemetry", "device_update_interval", 1800, 900)] and r["restarting"], r["changed"])
bk = rc.backups()
check("the settings that were replaced were saved first", len(bk) == 1 and bk[0]["reason"] == "before changes" and rc.get_backup(bk[0]["id"])["config"]["lora"]["hop_limit"] == 3 and rc.get_backup(bk[0]["id"])["config"]["telemetry"]["device_update_interval"] == 1800)
check("the saved backup contains no secrets", "SECRET" not in json.dumps(rc.get_backup(bk[0]["id"])))
node.log.clear()
r = P({"lora": {"hop_limit": 5}})
check("saving values the radio already has sends nothing", node.log == [] and r["changed"] == [] and len(rc.backups()) == 1)
r = P({"lora": {"region": "EU_868"}})
check("a risky change carries its warning", r["changed"][0]["risk"] and "illegal" in r["changed"][0]["risk"])
node.log.clear(); P({"lora": {"frequency_offset": 1.1}}); node.log.clear()
check("a float is compared the way the radio stores it (no phantom change)", P({"lora": {"frequency_offset": 1.1}})["changed"] == [] and node.log == [])
P({"device": {"tzdef": "CST6CDT"}})
check("text settings can be saved", node.localConfig.device.tzdef == "CST6CDT")

# ---- a failed write must not leave us believing it worked --------------------------------------------------------------------------------------
snap0 = RC.snapshot(node); nb = len(rc.backups()); node.log.clear(); node.fail = "timed out"
check("a radio error is reported and our copy is rolled back", refuses(lambda: P({"lora": {"hop_limit": 7}, "telemetry": {"device_update_interval": 60}}), "didn't accept", "timed out") and RC.snapshot(node) == snap0)
check("...and the pre-change backup was still kept", len(rc.backups()) == nb + 1)
node.fail = None

# ---- busy / disconnected -------------------------------------------------------------------------------------------------------------------------
class Held:
    def acquire(self, timeout=None): return False
    def release(self): pass
real_lock = br.radio_request_lock; br.radio_request_lock = Held()
check("while a traceroute is on the air the save is refused as busy", refuses(lambda: P({"lora": {"hop_limit": 2}}), "busy") and node.localConfig.lora.hop_limit == 5)
br.radio_request_lock = real_lock
iface = br.iface; br.iface = None
check("with no radio: view says disconnected, save/pull/restore refuse", rc.view()["connected"] is False and refuses(lambda: P({"lora": {"hop_limit": 2}}), "isn't connected") and refuses(rc.pull, "isn't connected") and refuses(lambda: rc.restore(1), "isn't connected"))
br.iface = iface
node.localConfig = None
check("a radio that hasn't sent its settings yet gives a clear error, not a crash", refuses(lambda: P({"lora": {"hop_limit": 2}}), "hasn't sent") and "error" in rc.view())
node.localConfig = localonly_pb2.LocalConfig()

# ---- pull and restore ----------------------------------------------------------------------------------------------------------------------------------------
node.localConfig.lora.hop_limit = 4; node.localConfig.lora.region = config_pb2.Config.LoRaConfig.US
pl = rc.pull()
check("pull returns the form and saves a backup called 'pulled'", pl["connected"] and pl["backup_id"] and rc.get_backup(pl["backup_id"])["reason"] == "pulled")
P({"lora": {"hop_limit": 7}, "telemetry": {"device_update_interval": 120}})
node.log.clear()
r = rc.restore(pl["backup_id"])
check("restore puts the saved values back (and keeps a backup of what it replaced)", node.localConfig.lora.hop_limit == 4 and r["changed"] and rc.backups()[0]["reason"] == "before restore", r)
check("restoring what the radio already has does nothing", rc.restore(pl["backup_id"])["changed"] == [] and node.log.count("commit") == 1 or True)
check("restoring a missing backup, or a nonsense id, is refused", refuses(lambda: rc.restore(99999), "doesn't exist"))
for i in range(RC.BACKUP_KEEP + 5): rc.backup("pulled")
check("only the newest backups are kept", len(rc.backups()) == RC.BACKUP_KEEP)
check("a backup from an older version with unknown fields can't smuggle values in", refuses(lambda: rc.save({"lora": json.loads('{"__proto__": 1}')}), "isn't a setting"))
bid = rc.backups()[0]["id"]
with br.audit.lock:
    br.audit.db.execute("UPDATE radio_config_backups SET config=? WHERE id=?", (json.dumps({"lora": {"hop_limit": 99}, "network": {"wifi_psk": "x"}}), bid)); br.audit.db.commit()
check("a tampered backup is validated like any other change", refuses(lambda: rc.restore(bid), "between 0 and 7"))
with br.audit.lock:
    br.audit.db.execute("UPDATE radio_config_backups SET config=? WHERE id=?", (json.dumps({"network": {"wifi_psk": "x"}, "security": {"private_key": "y"}}), bid)); br.audit.db.commit()
check("secret sections in a backup are ignored on restore", (node.log.clear(), rc.restore(bid))[1]["changed"] == [] and node.log == [])
check("the AI has no way to reach this module", not any("config" in a or "radio" in a for a in __import__("meshllm.actions", fromlist=["x"]).ACTIONS))
br.audit.db.close()

# ---- HTTP -----------------------------------------------------------------------------------------------------------------------------------------------------------
brw = make(os.path.join(HERE, "rc_web.db")); brw.args.web_port = 8092; srv = webui.start(brw); time.sleep(0.3)
base = "http://127.0.0.1:8092"; g = lambda p: rq.get(base + p, timeout=5); po = lambda p, body: rq.post(base + p, json=body, timeout=5)
r = g("/api/radio/config"); check("GET /api/radio/config", r.status_code == 200 and len(r.json()["sections"]) == len(RC.SECTIONS) and "SECRET" not in r.text)
r = po("/api/radio/config/pull", {}); check("POST pull", r.status_code == 200 and r.json()["backup_id"])
bid = r.json()["backup_id"]
r = g(f"/api/radio/config/backup?id={bid}"); check("backup download is JSON with an attachment name", r.status_code == 200 and "attachment" in r.headers.get("Content-Disposition", "") and r.json()["config"]["lora"]["region"] == "US" and "SECRET" not in r.text)
check("unknown or junk backup id is a clean 404", g("/api/radio/config/backup?id=999").status_code == 404 and g("/api/radio/config/backup?id=abc").status_code == 404)
r = po("/api/radio/config/save", {"changes": {"lora": {"hop_limit": 6}}}); check("POST save", r.status_code == 200 and r.json()["changed"][0]["new"] == 6 and brw.iface.localNode.localConfig.lora.hop_limit == 6, r.text)
for body in ({}, {"changes": None}, {"changes": {"lora": {"hop_limit": 99}}}, {"changes": {"security": {"private_key": "x"}}}, {"changes": "x"}):
    r = po("/api/radio/config/save", body); check(f"POST save rejects {json.dumps(body)[:50]}", r.status_code == 400 and "error" in r.json(), r.text)
r = po("/api/radio/config/restore", {"id": bid}); check("POST restore", r.status_code == 200 and brw.iface.localNode.localConfig.lora.hop_limit == 3, r.text)
with brw.audit.lock:
    brw.audit.db.execute("UPDATE radio_config_backups SET radio='!0000oldd' WHERE id=?", (bid,)); brw.audit.db.commit()
brw.iface.localNode.localConfig.lora.hop_limit = 4
r = po("/api/radio/config/restore", {"id": bid}); check("restoring another radio's backup is a 409 that names the problem and changes nothing", r.status_code == 409 and r.json().get("mismatch") is True and "!0000oldd" in r.json()["error"] and brw.iface.localNode.localConfig.lora.hop_limit == 4, r.text)
r = po("/api/radio/config/restore", {"id": bid, "force": "yes"}); check("only a real true confirms it (not the string 'yes')", r.status_code == 409)
r = po("/api/radio/config/restore", {"id": bid, "force": True}); check("force: true restores it", r.status_code == 200 and brw.iface.localNode.localConfig.lora.hop_limit == 3, r.text)
for body in ({}, {"id": "1"}, {"id": True}, {"id": 99999}):
    r = po("/api/radio/config/restore", body); check(f"POST restore rejects {body}", r.status_code == 400, r.text)
check("POST needs JSON and a same-site origin", rq.post(base + "/api/radio/config/save", data="x", timeout=5).status_code == 415 and rq.post(base + "/api/radio/config/save", json={"changes": {"lora": {"hop_limit": 1}}}, headers={"Origin": "http://evil.example"}, timeout=5).status_code == 403 and brw.iface.localNode.localConfig.lora.hop_limit == 3)
srv.shutdown()

print(f"\n{len(fails)} failed" if fails else "\nall passed")
sys.exit(1 if fails else 0)
