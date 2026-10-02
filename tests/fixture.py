"""Shared set-up for the newer tests: a bridge with a fake radio (no hardware, no Ollama) and, optionally, its web server."""
import argparse, os, sys, tempfile, threading, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import mesh_llm_bridge as b
import webui

NOW = time.time()


class Ch:
    def __init__(self, index, name, psk):
        self.index = index
        self.settings = type("S", (), {"name": name, "psk": psk})()


class Radio:
    """Stands in for the serial radio: a node list with us (!00000001) and a few neighbours, and a record of what was sent."""
    stream = object()
    _rxThread = threading.current_thread()

    class myInfo:
        my_node_num = 1

    def __init__(self, nodes=None):
        self.sent = []
        self.handlers = []
        self.nodes = nodes if nodes is not None else {
            "!00000001": {"num": 1, "user": {"id": "!00000001", "longName": "Base Station", "shortName": "BASE", "hwModel": "STUB"},
                          "position": {"latitude": 37.80, "longitude": -122.27}},
            "!0000aaaa": {"num": 0xaaaa, "user": {"id": "!0000aaaa", "longName": "Lodge", "shortName": "LDG"}, "lastHeard": int(NOW - 60), "hopsAway": 0,
                          "position": {"latitude": 37.81, "longitude": -122.27}, "deviceMetrics": {"batteryLevel": 15}},
            "!0000bbbb": {"num": 0xbbbb, "user": {"id": "!0000bbbb", "longName": "Ridge Repeater", "shortName": "RDG"}, "lastHeard": int(NOW - 300), "hopsAway": 1,
                          "position": {"latitude": 37.85, "longitude": -122.20}},
            "!0000cccc": {"num": 0xcccc, "user": {"id": "!0000cccc", "longName": "Far Farm", "shortName": "FRM"}, "lastHeard": int(NOW - 900), "hopsAway": 0},
        }
        self.nodesByNum = {n["num"]: n for n in self.nodes.values()}
        self.localNode = type("N", (), {"channels": [Ch(0, "", b"\x01")]})()

    def getMyUser(self):
        return {"id": "!00000001", "longName": "Base Station", "shortName": "BASE", "hwModel": "STUB"}

    def sendText(self, text, destinationId="^all", wantAck=False, wantResponse=False, onResponse=None, channelIndex=0, **kw):
        self.sent.append(dict(text=text, dest=destinationId, ack=wantAck, ch=channelIndex))
        self.handlers.append(onResponse)


def make(tmp=None, web_port=None, **over):
    """(bridge, radio, tmp_folder). With web_port the dashboard is served on it."""
    tmp = tmp or tempfile.mkdtemp(prefix="meshfx_")
    base = dict(db=os.path.join(tmp, "t.db"), port="STUB", model="fake", command="/ai", ollama_url="http://127.0.0.1:9", max_tokens=50, num_ctx=4096, max_chunks=4,
                chunk_delay=0, cooldown=0, timeout=2, memory_turns=6, memory_hours=24, memory_chars=3000, no_log_inbound=False, web_host="127.0.0.1",
                web_port=web_port or 8090, no_web=web_port is None, max_queue=3, queue_ttl=600, no_queue_notice=False, access_mode=None, daily_cap=None,
                confirm_seconds=60, no_tool_gate=True, chunk_bytes=160, send_retries=2, retry_delay=0.05, traceroute_timeout=0.2, reconnect_hold=0.4,
                channel_gap=0.0, channel_per_hour=100)
    base.update(over)
    br = b.Bridge(argparse.Namespace(**base))
    radio = Radio()
    br.iface = radio
    br.models.thinks = lambda name: False
    if web_port:
        webui.start(br)
        time.sleep(0.3)
    return br, radio, tmp


class Checker:
    def __init__(self):
        self.fails = []

    def __call__(self, name, cond, detail=""):
        print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
        if not cond:
            self.fails.append(name)

    def done(self):
        print(f"\n{len(self.fails)} failed" if self.fails else "\nall passed")
        sys.stdout.flush()
        os._exit(1 if self.fails else 0)
