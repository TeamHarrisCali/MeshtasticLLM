"""Demo mode (`python -m meshllm --demo`): the whole bridge and dashboard with no radio and no Ollama.

Anyone can try the dashboard this way. Nothing here is real, and nothing is ever transmitted:

  * DemoRadio    stands in for the serial radio. It has the same surface the bridge uses (node list, own identity, protobuf
                 settings, sendText / sendData) but never opens a serial port or a network connection.
  * DemoTraffic  publishes the same pubsub events the meshtastic library does (meshtastic.receive.*), so the real code paths fill
                 the dashboard: counts, telemetry, trails, the public channel and, about once a minute, an `/ai` question from a
                 fake node that goes through the normal receive path, the queue, the model and an acked reply.
  * ScriptedOllama  a tiny stand-in for Ollama on a free localhost port, so the AI answers without a model. If a real Ollama with a
                 tool-capable model is running, that is used instead.
  * seed_history  a day of plausible history written straight to the temporary database, so Trends, Activity and the map trails
                 are not empty on the first page load.

The database, tile cache, backups and logs all live in one temporary folder that is deleted when the demo stops; the real audit.db is
never opened. Every name and id below is obviously fake (!d3000001...) and the places are scattered around a public park.
"""
import base64
import errno
import hashlib
import json
import math
import os
import random
import re
import shutil
import signal
import tempfile
import threading
import time
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from meshtastic.protobuf import channel_pb2, config_pb2, localonly_pb2, mesh_pb2, portnums_pb2
from pubsub import pub

DEMO_MODEL = "demo-scripted"            # the name shown everywhere for the built-in model
PREFERRED_MODEL = "llama3.2:3b"         # a real Ollama's model of this name is picked first (same as the bridge's default)
CENTER = (40.7829, -73.9654)            # a neutral public landmark (a big city park); every fake node is placed around it
FIRST_NUM = 0xD3000001                  # the demo radio is !d3000001, the other fake nodes count up from there
BROADCAST = 0xFFFFFFFF
HOP_START = 3                           # hops a fake node allows its packets (the firmware's default)
MAX_SPEED = 50.0

# ======================================================================================================================
# the fake mesh
# ======================================================================================================================
# One row per fake node: name, short name, hardware, role, battery % (None = no battery data, 101 = external power), hops away,
# bearing (degrees) and distance (km) from the park centre, seconds since it was last heard, flags, battery drain in % per hour.
# Flags: mover (walks a loop, leaves a trail), sensor (temperature/humidity/pressure), solar (battery rises and falls with the day),
#        mqtt (reached through the internet), nopos (shares no position), asker (may ask the AI, tools on), visitor (may ask, tools off).
Spec = namedtuple("Spec", "name short hw role battery hops bearing km age flags drain")
SPECS = [
    Spec("Demo Ridge Repeater", "RDGE", "RAK4631", "REPEATER", 101, 0, 15, 1.6, 40, "", 0),
    Spec("Demo Hilltop Router", "HILL", "HELTEC_V3", "ROUTER", 101, 0, 60, 2.3, 95, "", 0),
    Spec("Demo Lakeside Router", "LAKE", "STATION_G2", "ROUTER_LATE", 101, 1, 200, 2.8, 300, "", 0),
    Spec("Demo Pavilion Repeater", "PAVL", "TBEAM", "REPEATER", 88, 1, 140, 1.9, 420, "solar", 0),
    Spec("Demo Reservoir Router", "RSVR", "HELTEC_V3", "ROUTER", 100, 2, 330, 3.4, 600, "", 0),
    Spec("Demo Meadow Client", "MDOW", "HELTEC_V3", "CLIENT", 76, 0, 95, 0.7, 25, "asker", 0.15),
    Spec("Demo Boathouse Client", "BOAT", "TLORA_T3_S3", "CLIENT", 54, 0, 185, 1.1, 150, "", 0.2),
    Spec("Demo Cabin Client", "CABN", "HELTEC_V3", "CLIENT", 91, 0, 250, 0.9, 70, "asker", 0.1),
    Spec("Demo Picnic Handheld", "PICN", "T_DECK", "CLIENT", 9, 0, 40, 0.5, 210, "asker", 0.5),
    Spec("Demo Orchard Client", "ORCH", "HELTEC_V2_1", "CLIENT", 19, 1, 300, 2.2, 900, "", 0.3),
    Spec("Demo Fountain Client", "FNTN", "TBEAM", "CLIENT_MUTE", 67, 1, 120, 1.4, 1300, "", 0.1),
    Spec("Demo Bridge Client", "BRDG", "RAK4631", "CLIENT", 83, 1, 170, 1.7, 2500, "", 0.1),
    Spec("Demo Bandstand Client", "BAND", "HELTEC_V3", "CLIENT", 45, 2, 10, 3.0, 3400, "", 0.2),
    Spec("Demo Playground Client", "PLAY", "TLORA_T3_S3", "CLIENT", 72, 2, 75, 2.6, 5200, "", 0.1),
    Spec("Demo Observatory Client", "OBSV", "TBEAM", "CLIENT", 38, 3, 350, 3.7, 7000, "", 0.2),
    Spec("Demo Rooftop Client", "ROOF", "HELTEC_V3", "CLIENT", 95, 0, 275, 1.3, 180, "", 0.05),
    Spec("Demo Kiosk Client", "KSK", "RAK4631", "CLIENT", 61, 1, 155, 2.0, 700, "", 0.15),
    Spec("Demo Gazebo Client", "GAZB", "HELTEC_V2_1", "CLIENT", 58, 1, 215, 2.4, 1900, "", 0.15),
    Spec("Demo Stables Client", "STBL", "TLORA_T3_S3", "CLIENT", 49, 2, 105, 3.1, 4100, "", 0.1),
    Spec("Demo Trail Tracker 1", "TRK1", "T_ECHO", "TRACKER", 81, 0, 30, 0.9, 20, "mover asker", 0.25),
    Spec("Demo Trail Tracker 2", "TRK2", "HELTEC_WIRELESS_TRACKER", "TRACKER", 63, 1, 130, 1.2, 55, "mover", 0.3),
    Spec("Demo Trail Tracker 3", "TRK3", "T_ECHO", "TRACKER", 14, 0, 220, 1.0, 35, "mover", 1.3),
    Spec("Demo Trail Tracker 4", "TRK4", "RAK4631", "TRACKER", 57, 2, 310, 1.8, 400, "mover", 0.2),
    Spec("Demo Weather Station", "WX", "RAK4631", "SENSOR", 100, 0, 80, 1.5, 120, "sensor solar", 0),
    Spec("Demo Garden Sensor", "GRDN", "HELTEC_V3", "SENSOR", 92, 1, 235, 1.9, 360, "sensor solar", 0),
    Spec("Demo Greenhouse Sensor", "GRNH", "TLORA_T3_S3", "SENSOR", 78, 1, 160, 2.5, 500, "sensor", 0.1),
    Spec("Demo Creek Sensor", "CRK", "RAK4631", "SENSOR", 64, 2, 345, 2.9, 1500, "sensor solar", 0),
    Spec("Demo Visitor Phone", "VSTR", "TLORA_V2_1_1P6", "CLIENT", 88, 0, 110, 0.4, 60, "visitor", 0.2),
    Spec("Demo MQTT Guest", "MQTT", "HELTEC_V3", "CLIENT", None, 3, 0, 0, 800, "mqtt nopos", 0),
    # nodes that have gone quiet: heard hours or days ago
    Spec("Demo Storage Shed", "SHED", "HELTEC_V2_1", "CLIENT", 33, 1, 190, 3.3, 9 * 3600, "", 0),
    Spec("Demo Old Barn", "BARN", "TBEAM", "CLIENT", 42, 2, 260, 3.6, int(2.1 * 86400), "", 0),
    Spec("Demo Winter Cabin", "WNTR", "RAK4631", "CLIENT", None, 3, 20, 3.8, 4 * 86400, "", 0),
    Spec("Demo Spare Router", "SPAR", "HELTEC_V3", "ROUTER_LATE", 100, 2, 295, 3.5, 20 * 3600, "", 0),
    Spec("Demo Lost Tracker", "LOST", "T_ECHO", "TRACKER", 22, 3, 55, 3.9, 30 * 3600, "nopos", 0),
]
# nodes the radio has forgotten but the database still remembers (they show as "remembered" on the map and Nodes page)
FORGOTTEN = [("Demo Old Relay", "ORLY", "HELTEC_V2_1", "ROUTER", 4.1, 140, 3), ("Demo Camp Client", "CAMP", "TBEAM", "CLIENT", 4.4, 250, 5),
             ("Demo Loaner Tracker", "LOAN", "T_ECHO", "TRACKER", 3.0, 70, 8)]
# questions the fake nodes DM the bridge: (who: "asker" has AI tools, "visitor" does not, text)
QUESTIONS = [
    ("asker", "/ai how's the mesh doing?"),
    ("asker", "/ai which nodes have the lowest battery?"),
    ("asker", "/ai what's the temperature outside?"),
    ("visitor", "/ai help"),
    ("asker", "/ai where is the nearest router?"),
    ("asker", "/ai has the battery on Demo Trail Tracker 3 been dropping?"),
    ("visitor", "/ai what is a mesh router?"),
    ("asker", "/ai which nodes have gone quiet?"),
    ("asker", "/ai how well can we hear nearby nodes?"),
    ("visitor", "/ai ping"),
    ("asker", "/ai what happened in the last hour?"),
    ("asker", "/ai status"),
]
CHANNEL_CHATTER = [
    "Radio check, how do I sound?", "Heading up the north trail now", "Anyone near the boathouse?", "Coffee cart is open by the fountain",
    "Loud and clear from the meadow", "Weather is clearing up", "Test, test, one two", "Picnic spot by the lake is free",
    "Anyone hear the band tuning up?", "Back at the cabin, signal is good", "Trail looks muddy past the bridge", "Good morning, mesh!",
]


def node_id(num):
    """The '!d3000001' style id of a node number."""
    return "!%08x" % num


def fake_key(num):
    """A fake public key (base64, 32 bytes) derived from the node number: obviously not a real key, and the same on every run."""
    return base64.b64encode(hashlib.sha256(b"meshllm-demo-key-%d" % num).digest()).decode("ascii")


def offset(lat, lon, bearing, km):
    """The point `km` kilometres from (lat, lon) towards `bearing` degrees (flat-earth maths is plenty at park scale)."""
    b = math.radians(bearing)
    return lat + km * math.cos(b) / 111.0, lon + km * math.sin(b) / (111.0 * math.cos(math.radians(lat)))


def daylight(t):
    """0..1 through the local day, peaking mid-afternoon: shapes temperature, solar charging and how busy the mesh is."""
    lt = time.localtime(t)
    return 0.5 + 0.5 * math.sin(2 * math.pi * ((lt.tm_hour + lt.tm_min / 60.0) - 9.0) / 24.0)


def activity(t):
    """How busy the mesh is at time t, 0.35 (night) to 1.0 (afternoon); the shape of the demo's history."""
    return 0.35 + 0.65 * daylight(t)


class DemoNode:
    """One fake node: its fixed description plus the library-style entry the radio's node list holds (updated as it 'transmits')."""

    def __init__(self, num, spec, start, origin):
        self.num, self.spec, self.start, self.id = num, spec, start, node_id(num)
        self.flags = set(spec.flags.split())
        self.hops = spec.hops
        self.lat, self.lon = offset(origin[0], origin[1], spec.bearing, spec.km)
        self.phase = (num * 0.37) % (2 * math.pi)                                   # where on its loop a mover starts
        self.radius_km = 0.5 + (num % 5) * 0.12                                      # size of a mover's loop
        self.base_snr = max(-14.0, 11.0 - 2.4 * spec.km - 1.5 * spec.hops)          # what a direct neighbour of this distance measures
        self.age = spec.age
        self.entry = {}

    def position_at(self, t):
        """(lat, lon) at time t. Fixed nodes stay put; movers walk a slow loop (about 40 minutes round) that passes through their start."""
        if "mover" not in self.flags:
            return self.lat, self.lon
        theta = self.phase + 2 * math.pi * (t - self.start) / 2400.0
        r = self.radius_km * 0.6
        return (self.lat + r / 111.0 * (math.cos(theta) - math.cos(self.phase)),
                self.lon + r / (111.0 * math.cos(math.radians(self.lat))) * (math.sin(theta) - math.sin(self.phase)))

    def battery_at(self, t):
        """Battery % at time t (None if the node reports none; 101 = on external power). Drains steadily, solar nodes also follow the sun."""
        b = self.spec.battery
        if b is None or b > 100:
            return b
        b = b - self.spec.drain * (t - self.start) / 3600.0
        if "solar" in self.flags:
            b += 6.0 * (daylight(t) - 0.5)
        return max(1.0, min(100.0, b))

    def device_metrics(self, t, rng):
        """The deviceMetrics dict a node of this kind would broadcast at time t (None for a node that reports no battery)."""
        bat = self.battery_at(t)
        if bat is None:
            return None
        volts = 5.0 if bat > 100 else 3.3 + bat / 100.0 * 0.9
        return {"batteryLevel": int(round(bat)), "voltage": round(volts + rng.uniform(-0.02, 0.02), 3),
                "channelUtilization": round(2.0 + 9.0 * activity(t) + rng.uniform(-1.0, 1.0), 2),
                "airUtilTx": round(0.2 + 1.6 * activity(t) * rng.uniform(0.5, 1.0), 2), "uptimeSeconds": int(86400 * (1 + self.num % 6) + (t - self.start))}

    def environment_metrics(self, t, rng):
        """temperature / humidity / pressure at time t for a sensor node (None for the others). Warmer afternoons, a hot greenhouse, a cool creek."""
        if "sensor" not in self.flags:
            return None
        local = {"GRNH": 6.0, "CRK": -2.5, "GRDN": 0.8}.get(self.spec.short, 0.0)
        temp = 13.0 + 8.0 * daylight(t) + local + rng.uniform(-0.3, 0.3)
        return {"temperature": round(temp, 1), "relativeHumidity": round(max(20.0, min(95.0, 85.0 - 2.2 * (temp - 10) + rng.uniform(-2, 2))), 1),
                "barometricPressure": round(1016.0 + 4.0 * math.sin(t / 40000.0) + rng.uniform(-0.4, 0.4), 1)}

    def signal(self, rng):
        """(snr, rssi, hop_limit) for one packet from this node as heard by the demo radio. Only a direct neighbour's numbers describe its own link."""
        if self.hops == 0:
            snr = max(-16.0, min(12.0, self.base_snr + rng.gauss(0, 1.4)))
        else:
            snr = rng.uniform(-3.0, 8.0)
        return round(snr, 2), int(max(-125, min(-45, -112 + 2.4 * (snr + 5)))), HOP_START - self.hops


def build_nodes(now, rng):
    """(us, others): the demo radio's own DemoNode and the fake neighbours, with ready-made library-style node-list entries."""
    us_spec = Spec("Demo Bridge Base", "DEMO", "HELTEC_V3", "CLIENT", 101, 0, 0, 0.0, 0, "", 0)
    nodes = [DemoNode(FIRST_NUM + i, s, now, CENTER) for i, s in enumerate([us_spec] + SPECS)]
    for n in nodes:
        n.entry = {"num": n.num, "user": {"id": n.id, "longName": n.spec.name, "shortName": n.spec.short, "hwModel": n.spec.hw,
                                          "role": n.spec.role, "publicKey": fake_key(n.num)}}
        heard = now - n.age
        if n.num != FIRST_NUM:
            n.entry["lastHeard"] = int(heard)
            n.entry["hopsAway"] = n.hops
            n.entry["snr"] = n.signal(rng)[0]
        else:
            n.entry["lastHeard"] = int(now)
        if "nopos" not in n.flags:
            lat, lon = n.position_at(heard)
            n.entry["position"] = {"latitude": round(lat, 6), "longitude": round(lon, 6), "altitude": 20 + n.num % 40, "time": int(heard)}
        dm = n.device_metrics(heard, rng)
        if dm:
            n.entry["deviceMetrics"] = dm
        em = n.environment_metrics(heard, rng)
        if em:
            n.entry["environmentMetrics"] = em
        if "mqtt" in n.flags:
            n.entry["viaMqtt"] = True
    nodes[1].entry["isFavorite"] = True
    return nodes[0], nodes[1:]


def _set(msg, name, value):
    """Set a protobuf field if this version of the meshtastic library has it (field names move between releases); never raises."""
    try:
        setattr(msg, name, value)
    except (AttributeError, ValueError, TypeError):
        pass


class DemoLocalNode:
    """The radio's 'local node': real protobuf settings so the Radio settings page renders, and admin calls that change nothing real."""

    def __init__(self, radio):
        self.radio = radio
        self.localConfig = localonly_pb2.LocalConfig()
        self.moduleConfig = localonly_pb2.LocalModuleConfig()
        lora, pos = self.localConfig.lora, self.localConfig.position
        _set(lora, "region", config_pb2.Config.LoRaConfig.US)
        _set(lora, "use_preset", True)
        _set(lora, "modem_preset", config_pb2.Config.LoRaConfig.LONG_FAST)
        _set(lora, "hop_limit", HOP_START)
        _set(lora, "tx_enabled", True)
        _set(self.localConfig.device, "role", config_pb2.Config.DeviceConfig.CLIENT)
        _set(self.localConfig.bluetooth, "enabled", True)
        _set(pos, "fixed_position", True)
        _set(pos, "position_broadcast_secs", 900)
        _set(self.moduleConfig.telemetry, "device_update_interval", 1800)
        _set(self.moduleConfig.telemetry, "environment_update_interval", 1800)
        self.channels = [channel_pb2.Channel(index=0, role=channel_pb2.Channel.PRIMARY,
                                             settings=channel_pb2.ChannelSettings(psk=b"\x01"))]   # the default public channel
        self.calls = []            # admin calls made, for tests and curiosity: nothing is ever sent

    def beginSettingsTransaction(self):
        """Admin no-op (a real radio would open a settings transaction)."""
        self.calls.append("begin")

    def commitSettingsTransaction(self):
        """Admin no-op (a real radio would apply and restart)."""
        self.calls.append("commit")

    def writeConfig(self, name):
        """Admin no-op: the edited protobuf is already the 'radio's' state, there is no device to write to."""
        self.calls.append(("write", name))

    def setTime(self, timeSec=0):
        """Admin no-op (the demo radio's clock is always right)."""
        self.calls.append("setTime")

    def setFixedPosition(self, lat, lon, alt=0):
        """Move the demo radio's own node and mark its position as fixed, like the real call does."""
        self.calls.append(("setFixedPosition", lat, lon, alt))
        _set(self.localConfig.position, "fixed_position", True)
        self.radio.us.entry["position"] = {"latitude": lat, "longitude": lon, "altitude": alt, "time": int(time.time())}

    def removeFixedPosition(self):
        """Clear the fixed-position flag."""
        self.calls.append("removeFixedPosition")
        _set(self.localConfig.position, "fixed_position", False)


class DemoRadio:
    """A fake Meshtastic interface with the surface the bridge uses. It never opens a serial port or any network connection.

    Sending is recorded in `sent` / `data_sent` and acknowledged shortly afterwards the way a real radio reports delivery."""

    def __init__(self, now=None, rng=None, speed=1.0):
        self.rng = rng or random.Random(7)
        self.speed = speed
        self.lock = threading.Lock()   # guards the lists of what was sent
        self.now = now or time.time()
        self.us, self.neighbours = build_nodes(self.now, self.rng)
        self.all = [self.us] + self.neighbours
        self.nodes = {n.id: n.entry for n in self.all}
        self.nodesByNum = {n.num: n.entry for n in self.all}
        self.myInfo = mesh_pb2.MyNodeInfo(my_node_num=self.us.num)
        self.localNode = DemoLocalNode(self)
        self.sent = []             # (destination, text) for every sendText
        self.data_sent = []        # (destination, port number) for every sendData
        self.stream = object()     # the bridge only checks that it is not None
        self._stop = threading.Event()
        self._rxThread = threading.Thread(target=self._stop.wait, name="demo-radio", daemon=True)   # alive until close(), like the reader thread
        self._rxThread.start()

    def getMyUser(self):
        """Our own identity, shaped like the library's."""
        return dict(self.us.entry["user"])

    def _later(self, delay, fn, *args):
        """Run fn(*args) on a daemon timer thread after `delay` seconds (scaled by the demo speed), unless the radio was closed meanwhile."""
        def go():
            """Timer body: do nothing once the radio is closed."""
            if not self._stop.is_set():
                fn(*args)
        t = threading.Timer(max(0.01, delay / self.speed), go)
        t.daemon = True
        t.start()

    def sendText(self, text, destinationId="^all", wantAck=False, wantResponse=False, onResponse=None, channelIndex=0, **kw):
        """Record the message and, if the caller wants to know, report it delivered a moment later (or heard being relayed)."""
        with self.lock:
            self.sent.append((destinationId, text))
            n = len(self.sent)
        if onResponse is None:
            return
        # most replies are confirmed by the destination itself; every sixth is only heard being repeated by a neighbour, which the
        # dashboard counts as 'relayed' - never a failure, because a failure would make the bridge resend and the demo should stay calm
        sender = destinationId if n % 6 else node_id(self.neighbours[1].num)
        self._later(0.4 + 0.1 * (n % 4), onResponse, {"fromId": sender, "decoded": {"routing": {"errorReason": "NONE"}}})

    def sendData(self, data, destinationId=None, portNum=None, wantAck=False, wantResponse=False, onResponse=None, channelIndex=0, hopLimit=3, **kw):
        """Record the request. Only traceroutes get an answer: from a node that is awake, along a plausible path of relays."""
        with self.lock:
            self.data_sent.append((destinationId, portNum))
        target = self.nodes.get(destinationId)
        if onResponse is None or portNum != portnums_pb2.PortNum.TRACEROUTE_APP or target is None:
            return
        if time.time() - (target.get("lastHeard") or 0) > 3 * 3600:
            return                                       # a node that has been quiet for hours does not answer: the trace times out
        hops = min(int(target.get("hopsAway") or 0), max(1, int(hopLimit)))
        routers = [n.num for n in self.neighbours if n.spec.role.startswith(("ROUTER", "REPEATER")) and n.hops < max(hops, 1) and n.num != target["num"]]
        route = routers[:hops] if hops else []
        snr = lambda: [int(4 * self.rng.uniform(-2, 9)) for _ in range(len(route) + 1)]
        reply = {"from": target["num"], "to": self.us.num, "hopStart": HOP_START, "hopLimit": HOP_START - hops,
                 "decoded": {"portnum": "TRACEROUTE_APP", "requestId": 1,
                             "traceroute": {"route": route, "snrTowards": snr(), "routeBack": route[::-1], "snrBack": snr()}}}
        self._later(0.8, onResponse, reply)

    def close(self):
        """Shut the fake radio: its reader thread ends and `stream` goes away, so the bridge sees it as disconnected."""
        self._stop.set()
        self.stream = None


# ======================================================================================================================
# traffic
# ======================================================================================================================
class DemoTraffic:
    """Feeds the bridge the packets a busy little mesh would: calls pub.sendMessage on the meshtastic topics, exactly as the library
    does for a real radio, so every dashboard page is filled by the real code. Rate is low (about one packet a second at speed 1)."""

    EVENTS = [("telemetry", 28), ("position", 22), ("user", 8), ("text", 5), ("encrypted", 15), ("other", 12)]

    def __init__(self, bridge, radio, speed=1.0, rng=None):
        self.bridge, self.radio, self.speed = bridge, radio, speed
        self.rng = rng or random.Random(11)
        self.active = [n for n in radio.neighbours if n.age < 3600]            # quiet nodes stay quiet
        self.sensors = [n for n in self.active if "sensor" in n.flags]
        self.askers = [n for n in radio.neighbours if "asker" in n.flags]
        self.visitors = [n for n in radio.neighbours if "visitor" in n.flags]
        self.asked = 0                                                         # questions sent so far (also picks the next one)
        self.packets = 0
        self._ids = iter(range(0x5000, 1 << 31))
        self._stop = threading.Event()
        self._threads = []
        # The library publishes every received packet from ONE reader thread, so the bridge's handlers never run at the same time.
        # Here packets come from several threads (traffic, questions, ack timers); one lock keeps that guarantee.
        self._publish_lock = threading.Lock()

    # ---- building packets --------------------------------------------------------------------------------------
    def packet(self, node, decoded=None, to=BROADCAST, **extra):
        """A library-style packet dict from `node` (with the signal fields a real reception carries) and mark the node as just heard."""
        snr, rssi, hop_limit = node.signal(self.rng)
        now = time.time()
        node.entry["lastHeard"] = int(now)             # the library refreshes these as packets arrive
        node.entry["snr"] = snr
        p = {"id": next(self._ids), "from": node.num, "to": to, "fromId": node.id, "toId": "^all" if to == BROADCAST else node_id(to),
             "rxTime": int(now), "rxSnr": snr, "rxRssi": rssi, "hopStart": HOP_START, "hopLimit": hop_limit, "channel": 0}
        if decoded is not None:
            p["decoded"] = decoded
        p.update(extra)
        return p

    def publish(self, topic, packet):
        """Hand a packet to every subscriber of the topic, as the library's receive thread would. A failing listener never stops the demo."""
        with self._publish_lock:
            self.packets += 1
            try:
                pub.sendMessage(topic, packet=packet, interface=self.radio)
            except Exception as e:                      # the real handlers catch their own errors; this is a second net
                print(f"[demo] a listener failed on {topic}: {e}")

    # ---- one thing happening --------------------------------------------------------------------------------------
    def telemetry(self, node=None):
        """A device-metrics broadcast (and, from a sensor node, usually an environment one)."""
        node = node or (self.rng.choice(self.sensors) if self.sensors and self.rng.random() < 0.35 else self.rng.choice(self.active))
        now = time.time()
        for key, metrics in (("deviceMetrics", node.device_metrics(now, self.rng)), ("environmentMetrics", node.environment_metrics(now, self.rng))):
            if not metrics:
                continue
            node.entry[key] = metrics                   # the radio's node list keeps the latest values
            self.publish("meshtastic.receive.telemetry", self.packet(
                node, {"portnum": "TELEMETRY_APP", "telemetry": {"time": int(now), key: metrics}}))

    def position(self, node=None):
        """A position broadcast; movers have moved a little since the last one, which draws their trail."""
        node = node or self.rng.choice([n for n in self.active if "nopos" not in n.flags])
        lat, lon = node.position_at(time.time())
        pos = {"latitude": round(lat, 6), "longitude": round(lon, 6), "latitudeI": int(lat * 1e7), "longitudeI": int(lon * 1e7),
               "altitude": 20 + node.num % 40, "time": int(time.time())}
        node.entry["position"] = pos
        self.publish("meshtastic.receive.position", self.packet(node, {"portnum": "POSITION_APP", "position": pos}))

    def user(self, node=None):
        """A node announcing itself."""
        node = node or self.rng.choice(self.active)
        self.publish("meshtastic.receive.user", self.packet(node, {"portnum": "NODEINFO_APP", "user": dict(node.entry["user"])}))

    def text(self, node=None, message=None):
        """A message on the public channel, to everyone (the AI never reads these; the Channel page does)."""
        node = node or self.rng.choice([n for n in self.active if n.spec.role.startswith("CLIENT")])
        message = message or self.rng.choice(CHANNEL_CHATTER)
        self.publish("meshtastic.receive.text", self.packet(node, {"portnum": "TEXT_MESSAGE_APP", "text": message, "payload": message.encode()}))

    def encrypted(self, node=None):
        """A packet from a channel we have no key for: counted, but nothing in it can be read."""
        node = node or self.rng.choice(self.active)
        self.publish("meshtastic.receive", self.packet(node, None, encrypted=b"\x00" * 16))

    def other(self, node=None):
        """Routine mesh housekeeping (neighbour lists, routing) shown as extra packet types on the traffic chart."""
        node = node or self.rng.choice(self.active)
        port = self.rng.choice(["NEIGHBORINFO_APP", "ROUTING_APP", "STORE_FORWARD_APP"])
        self.publish("meshtastic.receive.data." + port, self.packet(node, {"portnum": port, "payload": b""}))

    def tick(self):
        """Do one randomly chosen thing (the weights are in EVENTS). Safe to call directly, e.g. from a test."""
        names, weights = zip(*self.EVENTS)
        getattr(self, self.rng.choices(names, weights)[0])()

    def dm(self, node, message):
        """A direct message to the bridge from `node` through the normal receive path. `/ai ...` is a question for the AI; anything
        else is an ordinary DM. Verified nodes arrive PKI-encrypted, with the key the operator pinned, so their AI tools work."""
        encrypted = "visitor" not in node.flags
        extra = {"pkiEncrypted": True, "publicKey": fake_key(node.num)} if encrypted else {}
        packet = self.packet(node, {"portnum": "TEXT_MESSAGE_APP", "text": message, "payload": message.encode()}, to=self.radio.us.num, **extra)
        self.publish("meshtastic.receive.text", packet)
        return packet

    def ask(self, node=None, text=None):
        """Send the next question of the rotation (or the given text, or from the given node) as a DM to the bridge.
        Returns the (node, text) used."""
        kind, line = QUESTIONS[self.asked % len(QUESTIONS)]
        if node is None:
            pool = self.visitors if kind == "visitor" else self.askers
            node = pool[self.asked % len(pool)]
        text = text or line
        self.asked += 1
        self.dm(node, text)
        if self.asked % 3 == 0:                         # now and then the asker answers back, which fills the direct-messages page
            self.radio._later(15.0, self.dm, node, "Thanks, that helps!")
        return node, text

    # ---- the background threads -----------------------------------------------------------------------------
    def _traffic_loop(self):
        """Thread body: one event every ~1.2 s / speed, forever (until stop)."""
        while not self._stop.wait(self.rng.uniform(0.7, 1.7) / self.speed):
            try:
                self.tick()
            except Exception as e:
                print(f"[demo] traffic error: {e}")

    def _question_loop(self):
        """Thread body: the first question after a couple of seconds, the second soon after, then one a minute."""
        delays = iter([2.0, 20.0])
        while True:
            if self._stop.wait(next(delays, 60.0) / self.speed):
                return
            try:
                self.ask()
            except Exception as e:
                print(f"[demo] question error: {e}")

    def start(self):
        """Start the traffic and question threads."""
        for fn, name in ((self._traffic_loop, "demo-traffic"), (self._question_loop, "demo-questions")):
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        """Stop both threads and wait briefly for them."""
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)


# ======================================================================================================================
# a day of history
# ======================================================================================================================
PORT_RATES = {"TELEMETRY_APP": 60, "POSITION_APP": 45, "NODEINFO_APP": 12, "TEXT_MESSAGE_APP": 8, "NEIGHBORINFO_APP": 6,
              "ROUTING_APP": 9, "ENCRYPTED": 25}


def seed_history(bridge, radio, now, rng):
    """Write about a day of plausible history straight into the temporary database: traffic counts, signal and hop statistics,
    health snapshots, telemetry, trails, remembered nodes and a few public-channel posts. Only the demo's own database is touched."""
    a = bridge.audit
    nodes = radio.neighbours
    seen = [n for n in nodes if n.age < 24 * 3600]
    hour_now = int(now // 3600)
    with a.lock:
        db = a.db
        for k in range(0, 25):                                     # this hour so far, and the 24 hours before it
            h, t = hour_now - k, (hour_now - k) * 3600 + 1800
            f = activity(t) * (((now % 3600) / 3600.0) if k == 0 else 1.0)       # the hour in progress is only partly over
            for port, rate in PORT_RATES.items():
                db.execute("INSERT OR REPLACE INTO mesh_packets (hour, portnum, n) VALUES (?,?,?)", (h, port, max(1, int(rate * f * rng.uniform(0.8, 1.2)))))
            for n in seen:
                share = (3.0 if n.spec.role.startswith(("ROUTER", "REPEATER")) else 1.0) * (2.0 if "mover" in n.flags else 1.0)
                db.execute("INSERT OR REPLACE INTO mesh_talkers (hour, node_id, n) VALUES (?,?,?)", (h, n.id, max(1, int(share * 4 * f * rng.uniform(0.5, 1.5)))))
            for hops, share in ((0, 0.45), (1, 0.30), (2, 0.17), (3, 0.08)):
                db.execute("INSERT OR REPLACE INTO mesh_hops (hour, hops, n) VALUES (?,?,?)", (h, hops, max(1, int(160 * f * share * rng.uniform(0.8, 1.2)))))
            for n in seen:
                if n.hops == 0:
                    c = max(1, int(8 * f * rng.uniform(0.6, 1.4)))
                    snr = n.base_snr + rng.uniform(-1, 1)
                    rssi = max(-125, min(-45, -112 + 2.4 * (snr + 5)))
                    db.execute("INSERT OR REPLACE INTO mesh_links VALUES (?,?,?,?,?,?,?,?,?)",
                               (h, n.id, c, snr * c, snr - 2.5, snr + 2.5, rssi * c, rssi - 6, rssi + 6))
        for i in range(96, 0, -1):                                 # a health snapshot every 15 minutes
            t = now - i * 900
            f = activity(t)
            total = len(nodes) + 1
            db.execute("INSERT OR REPLACE INTO mesh_samples VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (t, total, int(8 + 12 * f + rng.uniform(-1, 1)), int(14 + 12 * f), len([n for n in nodes if n.age < 86400]),
                        len([n for n in nodes if n.hops == 0 and n.age < 86400]), round(3 + 8 * f + rng.uniform(-0.5, 0.5), 2),
                        101, round(2 + 8 * f, 2), round(0.3 + 1.2 * f, 2), int(120 * f)))
        for n in nodes:                                            # trails: movers walked their loop for the last 6 hours
            if "nopos" in n.flags:
                continue
            steps = range(72, -1, -1) if "mover" in n.flags else [rng.randint(1, 20)]
            for i in steps:
                t = now - i * 300 - (n.age if "mover" not in n.flags else 0)
                lat, lon = n.position_at(t)
                db.execute("INSERT INTO node_positions (node_id, ts, lat, lon, alt) VALUES (?,?,?,?,?)", (n.id, t, lat, lon, 20 + n.num % 40))
        for i, n in enumerate(nodes):                              # every node we know, first heard some days ago (a few only lately)
            first = now - (rng.uniform(1.5, 9.0) * 86400 if i % 11 else rng.uniform(600, 7000))
            lat, lon = n.position_at(now - n.age)
            db.execute("INSERT OR REPLACE INTO mesh_nodes (node_id, num, name, short, hw, role, first_seen, last_seen, lat, lon, alt, pos_ts, pos_source) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (n.id, n.num, n.spec.name, n.spec.short, n.spec.hw, n.spec.role, first, now - n.age,
                        None if "nopos" in n.flags else lat, None if "nopos" in n.flags else lon, 20 + n.num % 40,
                        None if "nopos" in n.flags else now - n.age, None if "nopos" in n.flags else "radio"))
        for j, (name, short, hw, role, km, bearing, days) in enumerate(FORGOTTEN):
            num = FIRST_NUM + 0x40 + j
            lat, lon = offset(CENTER[0], CENTER[1], bearing, km)
            db.execute("INSERT OR REPLACE INTO mesh_nodes (node_id, num, name, short, hw, role, first_seen, last_seen, lat, lon, alt, pos_ts, pos_source) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (node_id(num), num, name, short, hw, role, now - (days + 20) * 86400, now - days * 86400, lat, lon, 25, now - days * 86400, "packet"))
        posts = [(26000, 0, "Good morning, mesh!"), (19000, 1, "Radio check, how do I sound?"), (12500, 2, "Loud and clear from the meadow"),
                 (7200, 5, "Coffee cart is open by the fountain"), (3400, 7, "Anyone hear the band tuning up?")]
        for ago, i, msg in posts:
            n = nodes[i]
            db.execute("INSERT INTO channel_messages (ts, direction, node_id, node_name, text, rx_snr, rx_rssi, hops, packet_id) VALUES (?,?,?,?,?,?,?,?,?)",
                       (now - ago, "in", n.id, n.spec.name, msg, round(n.base_snr, 1), -90, n.hops, 0x4000 + i))
        db.commit()
    a.set_setting("mesh_nodes_since", now - 10 * 86400)           # 'new nodes' then means the ones first heard in the last hours
    # telemetry: one device reading per node every ~40 minutes (until it went quiet), sensors a little more often
    rows = []
    for n in nodes:
        step = 2400 if "sensor" not in n.flags else 1800
        t = now - n.age - rng.uniform(0, step)
        while t > now - 24 * 3600:
            for kind, metrics in (("device", n.device_metrics(t, rng)), ("environment", n.environment_metrics(t, rng))):
                if metrics:
                    rows.append((t, n.id, n.spec.name, kind, "broadcast", "ok", json.dumps(metrics, sort_keys=True), metrics))
            t -= step * rng.uniform(0.9, 1.1)
    cols = {"batteryLevel": "battery_level", "voltage": "voltage", "channelUtilization": "channel_utilization", "airUtilTx": "air_util_tx",
            "uptimeSeconds": "uptime_seconds", "temperature": "temperature", "relativeHumidity": "relative_humidity", "barometricPressure": "barometric_pressure"}
    with a.lock:
        db = a.db
        for ts, nid, name, kind, source, status, raw, metrics in rows:
            names = ["ts", "node_id", "node_name", "kind", "source", "status", "raw"] + [cols[k] for k in metrics]
            db.execute(f"INSERT INTO telemetry ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                       [ts, nid, name, kind, source, status, raw] + list(metrics.values()))
        db.commit()


# ======================================================================================================================
# a scripted stand-in for Ollama
# ======================================================================================================================
NODE_AFTER_RE = re.compile(r"\b(battery|voltage|temperature|humidity|channel use)\s+(?:on|of|for|at)\s+(.+?)(?:\s+(?:been|dropping|draining|changed|lately|today|over|doing)\b|[?.!]|$)", re.I)
ABOUT_RE = re.compile(r"(?:tell me about|info(?:rmation)? (?:on|about)|details (?:on|for|about)|about node)\s+(?:node\s+)?(.+?)(?:[?.!]|$)", re.I)
ID_RE = re.compile(r"![0-9a-f]{8}", re.I)
ROLE_WORDS = {"router": "router", "routers": "router", "repeater": "repeater", "repeaters": "repeater", "tracker": "tracker", "trackers": "tracker",
              "sensor": "sensor", "sensors": "sensor", "client": "client", "clients": "client"}
NOT_MESH_RE = re.compile(r"\b(explain|why|forecast|disk|joke|poem|what time|what day|what is a|what's a|what does|how does)\b", re.I)


def plan_tool(prompt):
    """The one tool call the scripted model makes for this message as (name, arguments), or None when it is not about the mesh.

    Plain keyword rules over the real menu in actions.py; no learning, so the demo's answers are the same every time."""
    low = prompt.lower()
    if NOT_MESH_RE.search(low):
        return None
    m = NODE_AFTER_RE.search(prompt)
    if m:
        metric = {"channel use": "channel_use"}.get(m.group(1).lower(), m.group(1).lower())
        return "node_history", {"node": m.group(2).strip(), "metric": metric}
    m = ID_RE.search(prompt) or ABOUT_RE.search(prompt)
    if m:
        return "node_info", {"node": (m.group(1) if m.groups() else m.group(0)).strip()}
    role = next((ROLE_WORDS[w] for w in re.findall(r"[a-z]+", low) if w in ROLE_WORDS), None)
    hours = "24" if re.search(r"24 hours|today|all day|yesterday", low) else "6" if re.search(r"6 hours|six hours", low) else None
    if re.search(r"\b(temperature|humid\w*|pressure|hot|cold|warm|outside)\b", low):
        return "mesh_report", {"topic": "sensors", **({"hours": hours} if hours else {})}
    if re.search(r"\b(quiet|silent|gone|offline|missing)\b", low):
        return "mesh_report", {"topic": "quiet"}
    if re.search(r"\b(busiest|busy|peak|quietest)\b", low):
        return "mesh_report", {"topic": "busiest"}
    if re.search(r"\b(signal|snr|hear|reception|links?|reach)\b", low) and not re.search(r"heard (?:recently|last)", low):
        return "mesh_report", {"topic": "signal", **({"hours": hours} if hours else {})}
    if re.search(r"\b(battery|batteries|power|dying)\b", low):
        return "list_nodes", {"sort": "low_battery"}
    if re.search(r"\b(happened|activity|lately|news)\b|what's new", low):
        return "mesh_report", {"topic": "activity", **({"hours": hours} if hours else {})}
    if re.search(r"heard (?:recently|last)|last heard|most recent", low):
        return "list_nodes", {"sort": "recent"}
    if re.search(r"farthest|furthest|most hops", low):
        return "list_nodes", {"sort": "farthest", **({"role": role} if role else {})}
    if re.search(r"\b(nearest|closest|near me|nearby)\b", low):
        return "list_nodes", {"sort": "nearest", **({"role": role} if role else {})}
    if role:
        return "list_nodes", {"role": role}
    if re.search(r"\b(how many|mesh|nodes|network|around|status)\b", low):
        return "mesh_summary", {}
    return None


def chat_answer(prompt, tools_offered):
    """What the scripted model says when it does not call a tool. Always signs itself as the demo model so nobody mistakes it for a real one."""
    low = prompt.lower()
    tag = " (scripted demo model)"
    if plan_tool(prompt) is not None and not tools_offered:
        return "I can't look at the mesh data for this node: the operator hasn't turned AI tools on for it." + tag
    if re.search(r"\b(hello|hi|hey|thanks|thank you)\b", low):
        return "Hello! I'm a scripted demo model. Ask me about the mesh, like how many nodes are around." + tag
    if re.search(r"what can you do|who are you|what are you|help", low):
        return "I can look up the mesh: node counts, low batteries, sensors, signal, quiet nodes, busy hours. I answer from a fixed script." + tag
    if re.search(r"router|repeater", low):
        return "A router or repeater node rebroadcasts other nodes' messages so they reach farther. It is usually placed high up." + tag
    if re.search(r"\bmesh\b|lora", low):
        return "A mesh network passes messages node to node, so a message can travel past the range of a single radio." + tag
    if re.search(r"what time|what day|date", low):
        return "It is " + time.strftime("%A %B %d, %I:%M %p") + " on the computer that runs me." + tag
    if re.search(r"forecast|internet|news", low):
        return "I have no internet. I can only look things up about the mesh." + tag
    if re.search(r"delete|install|shut ?down|reboot|change|run |send", low):
        return "I can only look things up about the mesh; I can't change or send anything." + tag
    return "I'm only a scripted demo, so I don't have an answer for that. Try asking about the mesh." + tag


class ScriptedOllama:
    """Answers exactly the endpoints the bridge uses (/api/tags, /api/ps, /api/show, /api/chat, /api/generate, /api/pull) on a free
    localhost port. /api/chat handles the YES/NO tool gate and picks a tool call from the real menu with plan_tool()."""

    def __init__(self, model=DEMO_MODEL):
        self.model = model
        outer = self

        class Handler(BaseHTTPRequestHandler):
            """One request: JSON in, JSON out."""

            def log_message(self, *a):
                """Keep the console for mesh traffic."""

            def _send(self, obj, code=200):
                """Write a JSON reply."""
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                """/api/tags (installed models) and /api/ps (loaded models)."""
                if self.path.startswith("/api/tags"):
                    self._send({"models": [{"name": outer.model, "model": outer.model, "size": 1, "digest": "demo-scripted-0001",
                                            "modified_at": "2026-01-01T00:00:00Z",
                                            "details": {"family": "scripted demo model", "parameter_size": "scripted", "quantization_level": "none"}}]})
                elif self.path.startswith("/api/ps"):
                    self._send({"models": [{"name": outer.model}]})
                else:
                    self._send({"error": "not found"}, 404)

            def do_POST(self):
                """/api/show, /api/chat, /api/generate; downloads are refused."""
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                except ValueError:
                    return self._send({"error": "bad json"}, 400)
                if self.path.startswith("/api/show"):
                    self._send({"capabilities": ["completion", "tools"], "details": {"family": "scripted demo model"}})
                elif self.path.startswith("/api/generate"):
                    self._send({"model": outer.model, "response": "", "done": True})
                elif self.path.startswith("/api/chat"):
                    self._send(outer.chat(body))
                elif self.path.startswith("/api/pull"):
                    self._send({"error": "downloads are disabled in demo mode"}, 400)
                else:
                    self._send({"error": "not found"}, 404)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = None
        self._stopped = False

    @property
    def url(self):
        """Base address to give the bridge as --ollama-url."""
        return "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def chat(self, body):
        """Reply to one /api/chat request: the YES/NO gate, a tool call, or plain text."""
        messages = body.get("messages") or []
        prompt = next((m.get("content") or "" for m in reversed(messages) if m.get("role") == "user"), "")
        system = (messages[0].get("content") or "") if messages else ""
        reply = {"model": self.model, "done": True, "done_reason": "stop"}
        if system.startswith("You route messages"):                       # the gate: is this about the mesh?
            reply["message"] = {"role": "assistant", "content": "YES" if plan_tool(prompt) else "NO"}
            return reply
        offered = {t.get("function", {}).get("name") for t in body.get("tools") or []}
        plan = plan_tool(prompt)
        if plan and plan[0] in offered:
            reply["message"] = {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": plan[0], "arguments": plan[1]}}]}
        else:
            reply["message"] = {"role": "assistant", "content": chat_answer(prompt, bool(offered))}
        return reply

    def start(self):
        """Serve in a background thread."""
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="demo-ollama")
        self.thread.start()

    def stop(self):
        """Stop serving and release the port (safe to call twice)."""
        if self._stopped:
            return
        self._stopped = True
        if self.thread is not None:
            self.httpd.shutdown()               # only valid once serve_forever is running
        self.httpd.server_close()


def pick_real_model(url):
    """The name of a model on a reachable Ollama that can chat AND use tools (the bridge's default if installed, else the smallest), or None."""
    from meshllm.ollama_models import ModelManager
    try:
        usable = [m for m in ModelManager(url).installed() if m["chat"] and m["tools"]]
    except Exception:
        return None
    if not usable:
        return None
    best = next((m for m in usable if m["name"] == PREFERRED_MODEL), None) or min(usable, key=lambda m: (m.get("size") or 0, m["name"]))
    return best["name"]


# ======================================================================================================================
# starting and running demo mode
# ======================================================================================================================
def configure(args):
    """Prepare the parsed command line for --demo, before the Bridge exists: a temporary folder for the database, tiles, backups and
    logs, no warm-up, and the model to use (a real tool-capable Ollama model if one is reachable, else the scripted one). Prints the banner."""
    speed = max(0.1, min(float(getattr(args, "demo_speed", 1.0) or 1.0), MAX_SPEED))
    args.demo_speed = speed
    args.demo_dir = tempfile.mkdtemp(prefix="meshllm_demo_")
    args.db = os.path.join(args.demo_dir, "demo.db")
    args.no_warm_up = True
    args.mesh_sample_interval = min(args.mesh_sample_interval, max(10.0, 60.0 / speed))     # so the Trends chart moves while you watch
    args.chunk_delay = min(args.chunk_delay, 1.0)                                          # short replies, no need to space them out
    args.retry_delay = min(args.retry_delay, 2.0)
    args.cooldown = min(args.cooldown, 20.0 / speed)                                       # the fake nodes ask faster when the demo runs faster
    args.demo_ollama = None
    real = None if args.demo_scripted else pick_real_model(args.ollama_url)
    if real:
        args.model = real
        model_line = f"AI model: {real} (the real Ollama at {args.ollama_url})"
    else:
        fake = ScriptedOllama()
        fake.start()
        args.demo_ollama, args.ollama_url, args.model = fake, fake.url, DEMO_MODEL
        model_line = f"AI model: {DEMO_MODEL} (scripted demo model on {fake.url}; no Ollama needed)"
    bar = "=" * 74
    print(f"{bar}\n DEMO MODE: simulated radio and mesh; nothing is transmitted.\n"
          f" Database, map tiles, backups and logs: {args.demo_dir}\n"
          f" This folder is temporary and deleted when you stop; your real audit.db is not used.\n {model_line}\n{bar}", flush=True)
    return args


def pin_askers(bridge, radio):
    """Turn on read-only AI tools for the fake askers the way an operator would: pin the key their radio holds and enable level 0."""
    for n in radio.neighbours:
        if "asker" in n.flags:
            bridge.set_node_access(n.id, max_tier=0, pin_key=bridge.radio_key(n.id))


def run(bridge):
    """Bridge.connect_demo(): attach the simulated radio, fill the dashboard, and block until bridge.stop() or Ctrl+C. Cleans up after,
    including when it is interrupted half way through starting."""
    args = bridge.args
    folder = getattr(args, "demo_dir", None)
    speed = getattr(args, "demo_speed", 1.0)
    now = time.time()
    rng = random.Random(2024)
    radio = DemoRadio(now, rng, speed)
    traffic = DemoTraffic(bridge, radio, speed, random.Random(11))
    bridge.demo = {"radio": radio, "traffic": traffic}                  # so tests (and the curious) can reach them
    try:                                                                  # everything below may be interrupted, so all of it is inside the try
        if folder:
            log_dir = Path(folder) / "logs"                                # the log viewer must not show the real logs/ folder
            log_dir.mkdir(exist_ok=True)
            bridge.diagnostics.log_dir = log_dir
        seed_history(bridge, radio, now, rng)
        bridge.attach(radio, "demo")
        pin_askers(bridge, radio)
        bridge.mesh.remember()
        bridge.mesh.sample()
        traffic.start()
        print("Demo mode is running: simulated traffic, and a question to the AI about once a minute"
              + ("" if speed == 1 else f" (speed x{speed:g})") + ". Press Ctrl+C to stop.", flush=True)
        while not bridge._stopping:
            time.sleep(0.2)
    finally:
        traffic.stop()
        bridge.detach(radio)
        radio.close()
        bridge.mesh.stop()
        if bridge.web_server is not None:
            bridge.web_server.shutdown()
            bridge.web_server.server_close()
        cleanup(args)


def cleanup(args):
    """Stop the scripted Ollama and delete the temporary folder. Safe to call more than once and when configure() never finished."""
    fake = getattr(args, "demo_ollama", None)
    if fake is not None:
        fake.stop()
    folder = getattr(args, "demo_dir", None)
    if folder:
        shutil.rmtree(folder, ignore_errors=True)


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def web_host_allowed(host, environ=None):
    """True if --demo may listen on `host`: this computer only, except that the Docker image (which sets MESHLLM_CONTAINER=1) binds
    0.0.0.0 inside its own network namespace. What can reach the container is then decided by the published port, and the compose
    file publishes it on the host's loopback only."""
    environ = os.environ if environ is None else environ
    return host in LOOPBACK or (host == "0.0.0.0" and environ.get("MESHLLM_CONTAINER") == "1")


def _address_in_use(e):
    """True if an OSError means the port is already taken (the error number differs per operating system)."""
    return e.errno in (errno.EADDRINUSE, 98, 48, 10048) or "in use" in str(e).lower()


def launch(args, bridge_class, parser):
    """The whole demo lifecycle, called by main() for --demo; returns the process exit code.

    Owns the temporary folder from the moment it exists, so Ctrl+C, kill, closing the terminal, a busy port or any other failure
    still deletes it. Refuses to listen beyond this machine: the demo has a send box and settings pages that must not face a network."""
    if not web_host_allowed(args.web_host):
        print(f"Demo mode only listens on this computer (127.0.0.1); --web-host {args.web_host} is not allowed with --demo.", flush=True)
        return 2
    ignored = [flag for flag, given in (("--port", args.port != parser.get_default("port")), ("--tcp", args.tcp is not None), ("--ble", args.ble is not None), ("--db", args.db != parser.get_default("db")),
                                        ("--model", args.model is not None)) if given]
    if ignored:
        print(f"Note: {', '.join(ignored)} {'is' if len(ignored) == 1 else 'are'} ignored in demo mode "
              "(a simulated radio, a temporary database and the demo model are used).", flush=True)
    stopping = []

    def on_signal(signum, frame):
        """kill / closing the terminal: turn it into the same clean stop as Ctrl+C (the first one only; later ones are ignored)."""
        if not stopping:
            stopping.append(signum)
            raise KeyboardInterrupt

    saved = {}
    for name in ("SIGTERM", "SIGHUP"):                    # SIGHUP does not exist on Windows
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                saved[sig] = signal.signal(sig, on_signal)
            except ValueError:                            # not the main thread: leave the signals alone
                pass
    try:
        configure(args)
        bridge_class(args).run()
        return 0
    except KeyboardInterrupt:
        return 0
    except OSError as e:
        if _address_in_use(e):
            print(f"Port {args.web_port} is already in use (is the real bridge running?). "
                  f"Try: python -m meshllm --demo --web-port {args.web_port + 1}", flush=True)
        else:
            print(f"Demo mode could not start: {e}", flush=True)
        return 1
    finally:
        cleanup(args)
        for sig, old in saved.items():
            signal.signal(sig, old)
