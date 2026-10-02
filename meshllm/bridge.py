"""Meshtastic <-> Ollama bridge.

Anyone on the mesh sends a direct message "/ai <question>" to this node; this script asks a
local Ollama model (with that node's own recent conversation as context) and DMs the answer
back, split into LoRa-sized chunks. Channel/broadcast messages are ignored. Questions wait in a
queue, nodes can be allowed/blocked/capped, and everything is written to an audit log and shown
in a local web UI, where the operator can also message nodes.

Nodes whose public key the operator has pinned, and who message over a PKI-encrypted DM, may also
ask the AI to look things up in the mesh data from a fixed menu of tools (see actions.py). Anything
that would change something needs a one-time code confirmed over the radio.
"""
import argparse
import os
import queue
import re
import secrets
import signal
import sys
import threading
import time
from collections import deque, namedtuple
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path

import requests
from pubsub import pub

from meshllm import actions
from meshllm import connection
from meshllm import webui
from meshllm.audit import Audit
from meshllm.ollama_models import ModelManager, OllamaError, same_model
from meshllm.mesh import MeshService, clean as clean_text, summarize as mesh_summarize
from meshllm.radio_config import RadioConfig
from meshllm.channel import ChannelService, CHANNEL_DEST
from meshllm.userdata import UserData
from meshllm.backup import Backups, apply_staged_restore
from meshllm.diagnostics import Diagnostics
from meshllm.reach import Coverage
from meshllm.tiles import TileCache
from meshllm.telemetry import TelemetryService
from meshllm.traceroute import TracerouteService

DEFAULT_MODEL = "llama3.2:3b"  # used until a model is chosen in the web UI (or given with --model)

# Default bytes of text per radio message (plus "(n/m) "). Meshtastic's payload limit is ~233 bytes, but
# PKI-encrypted DMs carry extra overhead and bigger packets are more likely to be lost, so stay well under.
MAX_CHUNK_BYTES = 160
MANUAL_MAX_CHUNKS = 4
NODE_ID_RE = re.compile(r"^![0-9a-f]{8}$")
RESET_WORDS = {"reset", "forget", "clear"}
HELP_WORDS = {"help", "?"}
WEB_SENDER = "web-console"   # the operator asking from the browser: same pipeline as a radio DM, but never transmitted
WEB_MAX_PROMPT = 600
ACCESS_VALUES = {"default", "allow", "block"}
MODES = {"open", "allowlist"}
REDACTED = "••••••"

SYSTEM_PROMPT = (
    "You are an assistant reachable over a low-bandwidth LoRa radio mesh. "
    "Answer in plain text, no markdown, as briefly as possible (under 400 characters unless "
    "more is essential). Be direct and practical. You are talking with one person; earlier "
    "messages in this chat are your conversation with them. You cannot run commands, open files "
    "or look anything up unless a tool is offered to you; never invent facts or claim to have "
    "done something you did not do. A few facts about right now may follow; "
    "they are a snapshot, so for questions about the mesh itself use a tool, and mention the facts only when they "
    "are relevant to what was asked. For anything else that is neither listed nor "
    "available from a tool, say you can't tell instead of guessing."
)
TOOL_PROMPT = (  # tools/eval_tools.py uses this same text (STRICT_PROMPT is an alias)
    " You have read-only tools for looking things up about the radio mesh. Use a tool ONLY when "
    "the user's message explicitly asks about what the radio has heard of the mesh (how many nodes are around, "
    "which were heard recently or are nearest, "
    "a router or repeater nearby, low batteries, sensor readings such as the temperature or humidity outside, how well "
    "nearby nodes are heard, nodes that have gone quiet, the busiest times, what happened recently, how a node's battery "
    "or temperature has changed, or the battery/signal/distance of a node given by name or by !id). "
    "If the message also asks for something you cannot do, still use the tool for the part you can. "
    "For everything else - greetings, thanks, general questions, advice, weather forecasts or internet questions, "
    "explanations of how mesh networking works, the time or date (they are in the facts above), questions about "
    "yourself or about the computer, and any request to change, delete, install, run, send or shut down anything - "
    "do NOT call a tool: answer in plain text, and if it is a request to do something you cannot do, say you can "
    "only look things up about the mesh."
)
TOOL_TEMPERATURE = 0  # deterministic tool decisions (measured: fewer wrong calls than default sampling)
GATE_PROMPT = (
    "You route messages for a radio bridge. Reply with exactly one word: YES or NO. "
    "Answer YES if the message asks about the radio mesh: what the radio has heard (how many nodes are "
    "around, recently heard or nearby nodes, a nearby router or repeater, low batteries, the temperature or humidity "
    "measured by nodes outside, how well nearby nodes are heard, nodes gone quiet, the busiest times, what happened "
    "recently, how a node's battery or temperature has changed, or a specific node - by name or by ID like "
    "!a1b2c3d4 - and its battery, signal, position or distance) - even if it is phrased indirectly. If the "
    "message contains any such question, answer YES even when it also asks for something else, such as "
    "deleting or changing something. Answer NO if it has nothing to do with those things: greetings, "
    "thanks, general questions, advice, weather forecasts, the time or date, questions about you, "
    "questions about the computer (its disk, memory, speed or software), "
    "explanations of how mesh networking works, and requests that only ask to change, delete, install, run, "
    "send or shut down something. "
    "Examples of YES: 'where is the nearest router?', 'is it humid outside?', 'who has been quiet?', "
    "'how many nodes can we reach directly?', 'has the battery on X been dropping?', 'which node is nearest to us?'. "
    "Examples of NO: 'what time is it?', 'are you there?', 'what can you do?', 'explain what a router does', "
    "'how much disk space is left?'."
)
FORCE_PROMPT = (  # added on the one retry when the gate said "this is about live state" but the model answered without a tool
    " The user's message asks about live state, which you cannot know from memory. Do not answer in words: "
    "call the single best-matching tool now."
)
NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def grounded(text, *sources):
    """True if the reply contains at least one number and every number in it appears in the sources (the live facts we gave the model,
    the question). An invented '82% humidity' fails; '6 nodes heard directly' passes when the facts say 6. Rounding of half a unit is allowed."""
    have = [float(x) for s in sources if s for x in NUM_RE.findall(s)]
    want = [float(x) for x in NUM_RE.findall(text or "")]
    return bool(want) and all(any(abs(w - h) <= 0.51 for h in have) for w in want)


BLANK_REPLY_ANSWER = "I couldn't come up with an answer. Try asking another way."   # sent instead of an empty reply
NO_TOOL_ANSWER = ("I couldn't tell which lookup to run. I can check: the mesh summary, nearest or low-battery nodes, "
                  "sensors, signal, quiet nodes, busy times, or one node.")


class EmptyAnswer(Exception):
    """The model finished without producing any answer text."""


KEEP_ALIVE = "30m"      # how long Ollama keeps the model in memory after a question (its default is 5 minutes; a cold start takes seconds)


def gate_body(model, prompt, num_ctx, think=None):
    """A tiny one-word classification call: is this message asking about the radio mesh at all?
    `think=False` is sent for reasoning models, whose 4-token budget would otherwise be spent thinking."""
    body = {"model": model, "stream": False, "keep_alive": KEEP_ALIVE,
            "messages": [{"role": "system", "content": GATE_PROMPT}, {"role": "user", "content": prompt}],
            "options": {"temperature": 0, "num_predict": 4, "num_ctx": num_ctx}}
    if think is not None:
        body["think"] = think
    return body


def gate_says_yes(text):
    """True if the gate model's one-word reply is YES (tolerates quotes, markdown stars and case)."""
    return (text or "").strip().lstrip("\"'*` ").lower().startswith("yes")


def format_context(now, radio_name, radio_id, model, counts, sensors, unit="C"):
    """The few live facts put in front of the model on every question. Only numbers and our own names go in: nothing a stranger typed
    (other nodes choose their own names, and a name could be written to look like an instruction)."""
    parts = [time.strftime("It is %A %B %d %Y, %I:%M %p", now) + f" ({time.tzname[1 if now.tm_isdst > 0 else 0]}) on the computer that runs you."]
    if radio_id:
        parts.append(f"You are the AI assistant behind the radio node '{radio_name or radio_id}' ({radio_id}), running the model {model}.")
    else:
        parts.append(f"You run the model {model}. The radio is not connected right now.")
    if counts:
        parts.append(f"The radio has heard {counts['heard_15m']} nodes in the last 15 minutes, {counts['heard_1h']} in the last hour and "
                     f"{counts['heard_24h']} in the last day ({counts['direct']} directly).")
    if sensors and sensors["nodes"]:
        t = sensors["temperature"]
        temp = None if t is None else (f"{t * 9 / 5 + 32:.0f} F" if unit == "F" else f"{t:.0f} C")
        bits = [x for x in (temp, None if sensors["humidity"] is None else f"{sensors['humidity']:.0f}% humidity") if x]
        parts.append(f"Sensor nodes around report {', '.join(bits)} (average of {sensors['nodes']} in the last hour).")
    return " ".join(parts)


SAMPLE_CONTEXT = format_context(time.strptime("2026-09-30 14:05", "%Y-%m-%d %H:%M"), "Base Node", "!10000001", "qwen3.5:latest",
                                {"heard_15m": 12, "heard_1h": 29, "heard_24h": 65, "direct": 6}, {"nodes": 2, "temperature": 31.0, "humidity": 35.0}, "F")


def build_chat_body(model, prompt, history, tier, max_tokens, num_ctx,
                    tool_prompt=None, temperature=-1, think=None, context=None):
    """The exact /api/chat request for one question. Shared with tools/eval_tools.py so the evaluation
    measures what production sends (tool_prompt/temperature overrides exist only for experiments).
    think=False turns off step-by-step 'thinking' for reasoning models: radio replies must be short,
    and with a small token budget a thinking model can use it all up and answer nothing."""
    tool_prompt = TOOL_PROMPT if tool_prompt is None else tool_prompt
    temperature = TOOL_TEMPERATURE if temperature == -1 else temperature
    options = {"num_predict": max_tokens, "num_ctx": num_ctx}
    body = {"model": model, "stream": False, "keep_alive": KEEP_ALIVE,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT + (" " + context if context else "") + (tool_prompt if tier >= 0 else "")},
                         *history, {"role": "user", "content": prompt}],
            "options": options}
    if tier >= 0:
        body["tools"] = actions.tool_specs(tier)
        if temperature is not None:
            options["temperature"] = temperature  # only tool-enabled requests: chat keeps its natural variety
    if think is not None:
        body["think"] = think
    return body

SEEN_PACKETS = 2000                # how many recent packet ids are remembered for duplicate detection

# One queued question. rid = audit row id, ts = when it was enqueued, tier = actions tier the sender
# verified for at receive time (-1 = no tools). Restored jobs always get -1: see restore_queue().
Job = namedtuple("Job", "rid sender prompt ts tier")


@dataclass
class Pending:
    """An action waiting for its one-time code to come back over the radio."""
    code: str
    action: actions.Action
    params: dict
    rid: int
    expires: float
    attempts: int = 0


def chunk_text(text, limit=MAX_CHUNK_BYTES):
    """Split text on word boundaries into pieces of at most `limit` UTF-8 bytes.

    Uses as few pieces as possible, then evens them out (smallest limit that still fits in that many
    pieces) so a reply never ends in a one-line tail like "(2/2) about what meshtastic is?".
    """
    chunks = _greedy_chunks(text, limit)
    n = len(chunks)
    if n > 1:
        # Binary search for the smallest limit that still packs into the same number of pieces
        # (n is already the minimum, so lower bound is the ceiling of total bytes / n).
        lo, hi = -(-len(text.encode()) // n), limit
        while lo < hi:
            mid = (lo + hi) // 2
            if len(_greedy_chunks(text, mid)) <= n:
                hi = mid
            else:
                lo = mid + 1
        chunks = _greedy_chunks(text, lo)
    return chunks


def _greedy_chunks(text, limit):
    """Pack words into pieces of at most `limit` UTF-8 bytes, filling each piece before starting the next.
    Whitespace runs collapse to single spaces; a word longer than `limit` is cut at a character boundary."""
    words = text.split()
    chunks, cur = [], ""
    for word in words:
        # hard-split a single word that is itself too long
        while len(word.encode()) > limit:
            cut = len(word)
            while len(word[:cut].encode()) > limit:
                cut -= 1
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(word[:cut])
            word = word[cut:]
        candidate = f"{cur} {word}".strip()
        if len(candidate.encode()) <= limit:
            cur = candidate
        else:
            chunks.append(cur)
            cur = word
    if cur:
        chunks.append(cur)
    return chunks


class Bridge:
    """The whole bridge: owns the radio connection, the question queue, the model worker, the radio
    sender thread and the services the web UI uses. Threads involved: the meshtastic receive callback
    (on_receive), worker(), sender_loop(), connect_loop() (main thread), plus web UI request threads
    and short-lived timer/action threads."""

    def __init__(self, args):
        """`args` is the parsed command line from main(); the audit database is opened here but nothing
        is started until run()."""
        self.args = args
        self.audit = Audit(args.db)
        self.q = deque()                # Jobs waiting for the model
        self.qcv = threading.Condition()
        self.current = None             # the Job the model is working on right now
        self.recent_ms = deque(maxlen=10)
        self.outbox = queue.Queue()     # (request id or None, destination, chunks) waiting for the radio
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="action")
        self.pending = {}               # sender id -> Pending confirmation
        self.pending_lock = threading.Lock()   # guards self.pending: made on the worker thread, confirmed on the radio thread, swept by any
        self.last_request = {}          # sender id -> monotonic time
        self.last_denied = {}           # sender id -> monotonic time of last logged denial
        self.cap_notified = {}          # sender id -> monotonic time of last "daily limit" reply
        self.seen_ids = set()           # ids of the last SEEN_PACKETS text packets, to answer a duplicate delivery only once
        self._seen_order = deque()      # the same ids oldest first, so the window rolls instead of being cleared in one go
        self.iface = None               # the connected radio, or None while searching
        self.endpoint = connection.make_endpoint(args)   # how we reach the radio: USB serial, Wi-Fi (TCP) or Bluetooth (see connection.py)
        self.port = None                # its label (/dev/ttyUSB0, tcp://host:4403, ble:ADDRESS); the last one used while searching
        self.radio_info = {}            # last known details of the radio (kept while it is away)
        self.radio_id = None
        self.down_since = time.time()   # when we last had no radio (outgoing messages wait up to --reconnect-hold)
        self.bad_until = {}             # label -> time before which we won't try it again
        self.connects = 0
        self.web_server = None          # the dashboard's HTTP server once run() has started it (demo mode shuts it down on exit)
        self.demo = None                # --demo only: {"radio", "traffic"}, the simulated radio and its traffic generator (see demo.py)
        self._search_logged = False
        self._stopping = False
        self._no_tools_warned = set()
        self.models = ModelManager(getattr(args, "ollama_url", "http://localhost:11434"))
        self.radio_request_lock = threading.Lock()   # only one traceroute on the air at a time
        self.telemetry = TelemetryService(self)
        self.traceroute = TracerouteService(self)
        self.radio_config = RadioConfig(self)
        self.channel = ChannelService(self)             # the default public channel: read it, post to it by hand. No AI involved.
        self.userdata = UserData(self)                  # your node labels, notes, stars and saved snippets (never sent, never shown to the AI)
        self.backups = Backups(self)
        self.diagnostics = Diagnostics(self)
        self.coverage = Coverage(self)
        self.tiles = TileCache(Path(args.db).resolve().parent / "tile_cache")   # map background, fetched on demand and kept
        self.mesh = MeshService(self)
        self.paused = False
        self.started = time.time()
        self._ollama_cache = (0.0, False)
        # flags, when given, override what was last saved from the web UI
        if getattr(args, "model", None):
            self.audit.set_setting("model", args.model)
        if args.access_mode:
            self.audit.set_setting("access_mode", args.access_mode)
        if args.daily_cap is not None:
            self.audit.set_setting("default_cap", args.daily_cap)

    # ---- settings / access rules -----------------------------------------
    @property
    def mode(self):
        """Access mode, 'open' or 'allowlist' (see set_mode); read from the saved settings each time."""
        return self.audit.get_setting("access_mode", "open")

    @property
    def default_cap(self):
        """Questions per node per rolling 24 h when the node has no cap of its own; 0 = unlimited."""
        return int(self.audit.get_setting("default_cap", "0"))

    def effective_cap(self, override):
        """The cap that applies to a node: its own override if set (None = follow the default)."""
        return override if override is not None else self.default_cap  # 0 = unlimited

    def set_mode(self, mode):
        """Save the access mode ('open' or 'allowlist'). Raises ValueError for anything else."""
        if not isinstance(mode, str) or mode not in MODES:
            raise ValueError("Mode must be 'open' or 'allowlist'")
        self.audit.set_setting("access_mode", mode)
        print(f"[access] mode -> {mode}")

    def set_default_cap(self, cap):
        """Save the default daily cap. Must be a real int (bools are rejected) from 0 to 1,000,000."""
        if not isinstance(cap, int) or isinstance(cap, bool) or not (0 <= cap <= 1_000_000):
            raise ValueError("Cap must be a whole number from 0 to 1,000,000 (0 = unlimited)")
        self.audit.set_setting("default_cap", cap)
        print(f"[access] default daily cap -> {cap}")

    def radio_key(self, node_id):
        """The public key this radio currently has on file for a node (base64), or None."""
        try:
            return self.iface.nodes[node_id]["user"].get("publicKey") or None
        except Exception:
            return None

    def set_node_access(self, node_id, **fields):
        """Change one node's rules. Only the fields given are touched: access, daily_cap, max_tier, pin_key.

        pin_key=None unpins; otherwise pin_key must equal the key the radio holds right now, so the
        operator pins the key they actually looked at, not one that changed since the page loaded.
        Raises ValueError with a message fit for the UI.
        """
        node_id = node_id.strip().lower() if isinstance(node_id, str) else ""
        if not NODE_ID_RE.match(node_id):
            raise ValueError("Node ID must look like !1a2b3c4d")
        kwargs = {}
        if "access" in fields:
            if not isinstance(fields["access"], str) or fields["access"] not in ACCESS_VALUES:
                raise ValueError("Access must be default, allow or block")
            kwargs["access"] = fields["access"]
        if "daily_cap" in fields:
            cap = fields["daily_cap"]
            if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or not (0 <= cap <= 1_000_000)):
                raise ValueError("Cap must be a whole number from 0 to 1,000,000 (blank = follow default)")
            kwargs["daily_cap"] = cap
        if "max_tier" in fields:
            tier = fields["max_tier"]
            if tier is not None and (not isinstance(tier, int) or isinstance(tier, bool) or tier not in actions.TIER_NAMES):
                raise ValueError("Actions level must be off, 0 (read-only) or 1 (plus confirmed actions)")
            kwargs["max_tier"] = tier
        if "pin_key" in fields:
            key = fields["pin_key"]
            if key is None:
                kwargs["pinned_key"] = None
            else:
                current = self.radio_key(node_id)
                if not current:
                    raise ValueError("Your radio has no public key for that node yet. Let it hear the node first.")
                if key != current:
                    raise ValueError("That node's key changed since this page loaded. Reload and verify it again.")
                kwargs["pinned_key"] = current
        self.audit.set_access(node_id, **kwargs)
        print(f"[access] {node_id}: " + ", ".join(f"{k}={'<key>' if k == 'pinned_key' and v else v}" for k, v in kwargs.items()))

    def node_access_info(self, node_id):
        """One node's access rule, cap and 24 h usage, for the conversation view."""
        access, cap = self.audit.get_access(node_id)
        return {"access": access, "daily_cap": cap, "effective_cap": self.effective_cap(cap),
                "used_24h": self.audit.used_24h(node_id)}

    def access_overview(self):
        """Everything the Access tab shows: mode, default cap and one row per node that has a rule,
        has asked, or has been heard. key_state compares the pinned key with the radio's current key
        (pinned / changed / missing / unpinned / none)."""
        rows, used = self.audit.access_rows(), self.audit.used_24h()
        convs = {c["node_id"]: c for c in self.audit.conversations()}
        mesh = {n["id"]: n for n in self.known_nodes()}
        nodes = []
        for nid in set(rows) | set(convs) | set(mesh):
            a, conv, m = rows.get(nid, {}), convs.get(nid), mesh.get(nid)
            cap = a.get("daily_cap")
            current, pinned = self.radio_key(nid), a.get("pinned_key")
            key_state = (("pinned" if current == pinned else "changed") if current else "missing") if pinned else ("unpinned" if current else "none")
            nodes.append({
                "node_id": nid,
                "node_name": (conv and conv["node_name"]) or (m and m["long_name"]) or None,
                "access": a.get("access", "default"), "daily_cap": cap,
                "effective_cap": self.effective_cap(cap), "used_24h": used.get(nid, 0),
                "last_ts": conv["last_ts"] if conv else None, "asked": bool(conv),
                "heard": m["last_heard"] if m else None,
                "max_tier": a.get("max_tier"), "public_key": current, "pinned_key": pinned,
                "key_state": key_state,
            })
        nodes.sort(key=lambda n: (not n["asked"], -(n["last_ts"] or 0), -(n["heard"] or 0)))
        return {"mode": self.mode, "default_cap": self.default_cap, "nodes": nodes}

    # ---- who is this, really? ---------------------------------------------
    def verify_node(self, sender, packet):
        """Decide how much this bridge may trust this message. Returns (actions tier or -1, audit label).

        AI tools need ALL of: the operator enabled actions for the node, pinned its public key,
        the message arrived PKI-encrypted (so the radio decrypted it with that node's key), and
        the key the radio holds for the node still equals the pinned one.
        """
        pki = bool(packet.get("pkiEncrypted"))
        base = "PKI-encrypted" if pki else "not PKI-encrypted"
        node = self.audit.get_node(sender)
        if node["max_tier"] is None:
            return -1, base

        def off(reason):
            """No tools for this message; the reason goes into the audit label."""
            return -1, f"{base} · actions off: {reason}"

        if not node["pinned_key"]:
            return off("no pinned key")
        if not pki:
            return off("message was not PKI-encrypted")
        pk = packet.get("publicKey")
        if pk and pk != node["pinned_key"]:
            return off("packet key differs from pinned key")
        if not self.radio_key(sender):
            return off("radio has no key for this node yet")
        if self.radio_key(sender) != node["pinned_key"]:
            return off("radio's key for this node differs from pinned key")
        return node["max_tier"], f"{base} · key verified · actions: {actions.TIER_NAMES[node['max_tier']]}"

    # ---- Ollama -----------------------------------------------------------
    def history(self, node_id):
        """The node's recent exchanges as chat messages, limited by the --memory-* settings."""
        return self.audit.history(node_id, self.args.memory_turns,
                                  self.args.memory_hours * 3600, self.args.memory_chars)

    def ask_llm(self, prompt, history, tier=-1):
        """Returns (text, tool_calls). Tools are only offered to nodes verified for actions, and
        (unless --no-tool-gate) only when a cheap yes/no pre-check says the message is about the mesh.

        Blocking: makes one or more HTTP calls to Ollama (gate, chat, retries), so it runs on the worker thread only.
        Raises EmptyAnswer if the model produced neither text nor a tool call.
        """
        model = self.model  # read once so a model switch mid-question can't mix two models
        think = self.think_arg(model)
        context = self.live_context()
        url = f"{self.args.ollama_url}/api/chat"
        gated_yes = False
        if tier >= 0 and not self.args.no_tool_gate:
            g = requests.post(url, json=gate_body(model, prompt, self.args.num_ctx, think), timeout=self.args.timeout)
            g.raise_for_status()
            if gate_says_yes(g.json()["message"].get("content")):
                gated_yes = True
            else:
                tier = -1  # answer as ordinary chat; no tools are offered

        def ask(think_arg, force=False):
            """One /api/chat round trip. Can downgrade `tier` to -1 (shared with the caller) when the
            model turns out not to support tools; force=True adds FORCE_PROMPT to insist on a tool."""
            nonlocal tier
            body = build_chat_body(model, prompt, history, tier, self.args.max_tokens, self.args.num_ctx, think=think_arg,
                                   tool_prompt=(TOOL_PROMPT + FORCE_PROMPT) if force else None, context=context)
            r = requests.post(url, json=body, timeout=self.args.timeout)
            if r.status_code == 400 and "tools" in body and "tools" in r.text.lower():
                # This model can't use tools (the web UI flags that). Don't fail the question: answer as
                # ordinary chat and say so once in the log.
                if model not in self._no_tools_warned:
                    self._no_tools_warned.add(model)
                    print(f"[model] {model} does not support tools - AI tools are unavailable with it")
                tier = -1
                body = build_chat_body(model, prompt, history, -1, self.args.max_tokens, self.args.num_ctx, think=think_arg, context=context)
                r = requests.post(url, json=body, timeout=self.args.timeout)
            r.raise_for_status()
            return r.json()

        data = ask(think)
        msg = data["message"]
        if not (msg.get("content") or "").strip() and not msg.get("tool_calls") and think is None:
            # Nothing came back and we didn't know this was a thinking model (older Ollama): try once
            # with thinking off before giving up.
            data = ask(False)
            msg = data["message"]
        text = (msg.get("content") or "").strip()
        calls = msg.get("tool_calls") or []
        if not text and not calls:
            raise EmptyAnswer(f"model '{model}' returned no answer text (done_reason={data.get('done_reason')}, "
                              f"thinking={len(msg.get('thinking') or '')} chars)")
        if gated_yes and tier >= 0 and not calls:
            # The gate said this is about live state, yet the model answered in words: that is a guess (it once "reported"
            # 12 nodes and 4 low batteries that didn't exist). Ask once more, insisting on a tool; if it still won't
            # use one, say what can be checked instead of sending the guess.
            forced = ask(think, force=True)["message"]
            calls = forced.get("tool_calls") or []
            if calls:
                return "", calls
            for words in ((forced.get("content") or "").strip(), text):       # still no tool: keep the words only if they stick to the facts we supplied
                if words and grounded(words, context, prompt):
                    return words, []
            print(f"[model] {model} answered a live-state question without a tool and with numbers it wasn't given; replied with the list of checks instead")
            return NO_TOOL_ANSWER, []
        return text, calls

    def live_context(self):
        """Real facts about right now for the model (see format_context): the time, who it is, what the radio has heard, the sensors."""
        try:
            rid = name = counts = None
            if self.iface is not None:
                try:
                    u = self.iface.getMyUser(); rid, name = u.get("id"), clean_text(u.get("longName") or "")
                except Exception:
                    rid, name = self.radio_id, clean_text(self.radio_info.get("long_name") or "")
                counts = mesh_summarize(self.mesh.nodes())
            return format_context(time.localtime(), name, rid, self.model, counts, self.mesh.sensors(1)["overall"], self.mesh.temp_unit())
        except Exception as e:
            print(f"[error] live context: {e}", file=sys.stderr)
            return None

    def think_arg(self, model):
        """False for reasoning ('thinking') models, else None (don't send the parameter).
        Any lookup failure gives None; ask_llm() retries once with False if the reply comes back empty."""
        try:
            return False if self.models.thinks(model) else None
        except Exception:
            return None

    def ollama_ok(self):
        """Is Ollama up and is the selected model installed? (cached for 10 s)"""
        checked, ok = self._ollama_cache
        if time.time() - checked > 10:
            try:
                names = [m["name"] for m in requests.get(
                    f"{self.args.ollama_url}/api/tags", timeout=2).json().get("models", [])]
                ok = any(same_model(self.model, n) for n in names)
            except Exception:
                ok = False
            self._ollama_cache = (time.time(), ok)
        return ok

    # ---- choosing the model -----------------------------------------------
    @property
    def model(self):
        """The model in use: chosen in the web UI (saved), or set once with --model."""
        return self.audit.get_setting("model", DEFAULT_MODEL)

    def models_overview(self):
        """Everything the Model tab shows. An unreachable Ollama is reported in `error`, not raised."""
        try:
            installed, error = self.models.installed(), None
        except OllamaError as e:
            installed, error = [], str(e)
        cur = self.model
        return {"current": cur, "default_model": DEFAULT_MODEL, "error": error, "installed": installed,
                "current_installed": any(same_model(cur, m["name"]) for m in installed),
                "loaded": self.models.loaded(), "pulls": self.models.pulls()}

    def set_model(self, name):
        """Switch models. Raises OllamaError/ValueError with a message fit for the UI."""
        name = name.strip() if isinstance(name, str) else ""
        match = next((m for m in self.models.installed() if m["name"] == name), None)
        if not match:
            raise ValueError(f"'{name}' isn't installed. Download it first.")
        if not match["chat"]:
            raise ValueError(f"'{name}' can't chat (it looks like an embedding-only model).")
        if name == self.model:
            return match
        self.audit.set_setting("model", name)
        self._ollama_cache = (0.0, False)   # force ollama_ok() to re-check against the new model
        print(f"[model] switched to {name}" + ("" if match["tools"] else " (no tool support: AI tools unavailable)"))
        threading.Thread(target=self.models.preload, args=(name,), daemon=True).start()
        return match

    # ---- queue ------------------------------------------------------------
    def depth(self):
        """Questions in the system: waiting plus the one the model is working on. Callers that need a
        consistent view (enqueue) hold self.qcv; status() reads it without the lock, which is fine for display."""
        return len(self.q) + (1 if self.current else 0)

    def pending_for(self, sender):
        """True if this sender already has a question waiting or being answered (one at a time per node)."""
        with self.qcv:
            return any(j.sender == sender for j in [*self.q, *([self.current] if self.current else [])])

    def enqueue(self, rid, sender, prompt, enqueued_at=None, tier=-1):
        """Add a question. Returns its 1-based position (1 = next up), or None if the queue is full."""
        with self.qcv:
            if self.depth() >= self.args.max_queue:
                return None
            self.q.append(Job(rid, sender, prompt, enqueued_at or time.time(), tier))
            self.qcv.notify()
            return self.depth()

    def eta_s(self, position):
        """Rough seconds until a question at this queue position is answered, rounded to 5 s.
        Uses the average of the last few model times, or 20 s before any have been measured."""
        avg = (sum(self.recent_ms) / len(self.recent_ms) / 1000) if self.recent_ms else 20.0
        return int(round(position * avg / 5.0) * 5) or 5

    def queue_snapshot(self):
        """The running question (working=True) followed by the waiting ones, for the web UI."""
        # Copy under the lock, then build the dicts outside it: node_name() touches the radio's node table.
        with self.qcv:
            items = ([(self.current, True)] if self.current else []) + [(j, False) for j in self.q]
        return [{"id": j.rid, "node_id": j.sender, "node_name": self.node_name(j.sender),
                 "prompt": j.prompt, "ts": j.ts, "position": pos, "working": working}
                for pos, (j, working) in enumerate(items, 1)]

    def cancel(self, rid):
        """Remove a waiting question (one the model has already started can't be cancelled)."""
        with self.qcv:
            for j in self.q:
                if j.rid == rid:
                    self.q.remove(j)
                    break
            else:
                return False
        self.audit.update(rid, status="cancelled", response="Cancelled by the operator.")
        print(f"[queue] cancelled request {rid}")
        return True

    def restore_queue(self):
        """Questions that were waiting when the bridge last stopped are picked back up.

        They come back with no AI-tool rights: the PKI check can't be repeated for a message
        that arrived before the restart. (Tools are tier -1, so they are answered as plain chat.)
        Web-console questions are cancelled instead, since nobody is waiting for them any more.
        """
        self.audit.cancel_stale_web()
        rows = self.audit.pending_queue(self.args.queue_ttl)
        restored = 0
        for r in rows:
            if self.enqueue(r["id"], r["node_id"], r["prompt"], r["ts"]) is not None:
                restored += 1
            else:
                # more were waiting than the queue holds: close the row now (no radio notice, the asker has long since moved on)
                # instead of leaving it 'queued' to be skipped again at every restart
                self.audit.update(r["id"], status="busy", response="The question queue was full after a restart.")
        if rows:
            print(f"[queue] restored {restored} question(s) from before the restart"
                  + (f"; {len(rows) - restored} did not fit in the queue and were closed as busy" if restored < len(rows) else ""))

    # ---- AI tools -------------------------------------------------------
    def run_action(self, action, params):
        """Run a validated action off the radio thread. Returns (ok, text).
        Waits at most 15 s for the result; a timed-out action may still be running in the pool."""
        fut = self.pool.submit(actions.run, self, action, params)
        try:
            return True, fut.result(timeout=15)
        except FutureTimeout:
            return False, f"{action.name} timed out."
        except Exception as e:
            return False, f"{action.name} failed: {e}"

    def handle_tool_call(self, job, call, ms):
        """The model asked for an action. The code, not the model, decides whether it happens.

        actions.validate() checks the name, arguments and the sender's tier. Read-only actions
        (tier 0) run at once and the result is sent back; tier 1 actions only park a one-time code
        in self.pending and ask the sender to confirm it (see handle_confirm). `ms` is the model time.
        """
        fn = call.get("function") or {}
        name, args = fn.get("name"), fn.get("arguments")
        self.audit.update(job.rid, model=self.model, llm_ms=ms, action=str(name)[:40])
        try:
            action, params = actions.validate(name, args, job.tier)
        except actions.ActionError as e:
            msg = f"I can't do that: {e}."
            print(f"[action] denied for {job.sender}: {name!r} ({e})")
            self.audit.update(job.rid, status="action_denied", response=msg)
            return self.queue_reply(job.rid, job.sender, msg)

        if action.tier >= 1:  # needs the operator-approved second step
            code = str(secrets.randbelow(900000) + 100000)   # always 6 digits; secrets, not random, since it is a credential
            secs = int(self.args.confirm_seconds)
            with self.pending_lock:        # swap in one step: a sweep or a confirm must never see the table half-changed
                old = self.pending.pop(job.sender, None)   # one pending confirmation per sender; a new request replaces it
                self.pending[job.sender] = Pending(code, action, params, job.rid, time.time() + secs)
            if old:
                self.audit.update(old.rid, status="cancelled", response="Replaced by a newer request.")
            cmd = self.args.command
            print(f"[action] {job.sender} asked for {action.name}; waiting for confirmation")
            self.audit.update(job.rid, status="action_pending",
                              response=f"Awaiting confirmation to run {action.name} (code sent to the user).")
            return self.queue_reply(job.rid, job.sender,
                                    f"Run {action.name}? Reply: {cmd} confirm {code} within {secs}s "
                                    f"(or {cmd} cancel).")

        ok, text = self.run_action(action, params)
        print(f"[action] {job.sender}: {action.name} -> {'ok' if ok else 'FAILED'}")
        self.audit.update(job.rid, status="action_ok" if ok else "action_failed", response=text)
        self.queue_reply(job.rid, job.sender, text)

    def sweep_pending(self):
        """Drop confirmations whose time ran out and mark their audit rows expired. Cheap; called
        before anything reads self.pending (status, and each incoming /ai command)."""
        now = time.time()
        with self.pending_lock:            # the dashboard thread and the radio thread can sweep at the same moment
            expired = [(s, p) for s, p in self.pending.items() if p.expires < now]
            for sender, _ in expired:
                del self.pending[sender]
        for _, p in expired:               # database writes happen outside the lock
            self.audit.update(p.rid, status="expired", response="Confirmation expired; nothing was run.")

    def drop_pending(self, sender):
        """Remove and return `sender`'s confirmation (None if there is none). Every change to self.pending goes through the lock, because
        a sweep iterating the dict while another thread changes it raises "dictionary changed size during iteration"."""
        with self.pending_lock:
            return self.pending.pop(sender, None)

    def take_pending(self, sender, expected):
        """Remove `sender`'s confirmation if it is still `expected` and has not expired. True means this caller now owns it and nobody
        else can run it; False means it expired, was replaced or was already taken in the meantime (a sweep marks an expired one in the log)."""
        with self.pending_lock:
            if self.pending.get(sender) is expected and expected.expires >= time.time():
                del self.pending[sender]
                return True
        return False

    def remember_packet(self, pid):
        """True the first time a packet id is seen, False for a repeat (the mesh can deliver one packet twice). Remembers the last
        SEEN_PACKETS ids as a rolling window, so there is no moment at which everything is forgotten at once.
        Called only from on_receive, i.e. the radio library's single receive thread, so it needs no lock."""
        if pid is None:
            return True                    # no id, nothing to compare
        if pid in self.seen_ids:
            return False
        self.seen_ids.add(pid)
        self._seen_order.append(pid)
        if len(self._seen_order) > SEEN_PACKETS:
            self.seen_ids.discard(self._seen_order.popleft())
        return True

    def help_text(self):
        """Canned reply for the help command; needs no model."""
        c = self.args.command
        return f"I'm an AI on this mesh. Ask in plain words, e.g. {c} how's the mesh doing? Commands: ping (signal test), status, actions, reset, help."

    def ping_text(self, packet, hops):
        """A range test that needs no model: how this very message reached us.
        `hops` is relays used (None if the packet didn't carry usable hop fields); zero RSSI is treated as missing."""
        try:
            who = clean_text(self.iface.getMyUser().get("longName") or "", 24)
        except Exception:
            who = clean_text(self.radio_info.get("long_name") or "", 24)
        who = who or "the bridge"
        snr, rssi = packet.get("rxSnr"), packet.get("rxRssi")
        sig = ", ".join(x for x in (f"SNR {snr:.1f} dB" if isinstance(snr, (int, float)) else None, f"RSSI {rssi} dBm" if isinstance(rssi, (int, float)) and rssi else None) if x)
        if hops is None:
            how = "heard you" + (f" ({sig})" if sig else "") + "; hop count unknown"
        elif hops == 0:
            how = "heard you directly" + (f": {sig}" if sig else "")
        else:
            how = f"your message passed through {hops} relay{'s' if hops != 1 else ''}" + (f"; the last hop was {sig}" if sig else "")
        return f"Pong from {who}: {how}."

    def warm_up(self):
        """Load the model into Ollama's memory now and ask it to stay there, so the first question after a quiet spell isn't slow."""
        def go():
            """Background thread body: one /api/generate call with no prompt just loads the model."""
            try:
                requests.post(f"{self.args.ollama_url.rstrip('/')}/api/generate", json={"model": self.model, "keep_alive": KEEP_ALIVE}, timeout=180)
                print(f"[ai] model {self.model} loaded and kept ready ({KEEP_ALIVE})")
            except Exception:
                pass                                        # Ollama not running yet: the first question will load it
        threading.Thread(target=go, daemon=True).start()

    def status_text(self):
        """One-line health summary for the status command (model, queue, radio, nodes heard)."""
        st = self.status()
        out = f"AI online. Model {st['model']}, queue {st['queue_depth']}/{st['max_queue']}, radio {'connected' if st['connected'] else 'DOWN'}"
        try:
            s = mesh_summarize(self.mesh.nodes())
            out += f", {s['heard_1h']} nodes heard in the last hour"
        except Exception:
            pass
        return out + "."

    def actions_help(self, tier):
        """Reply to the actions command: which tools this sender may use at their verified tier."""
        if tier < 0:
            return "AI tools aren't enabled for you."
        names = ", ".join(a.name for a in actions.allowed(tier) if not a.name.startswith("demo_"))
        return f"Checks: {names}." + (" Some need a code." if tier >= 1 else " Just ask in plain words.")

    # ---- info for the web UI ---------------------------------------------
    def status(self):
        """Live state for the web UI header and the status command. Safe to call with no radio attached."""
        self.sweep_pending()
        node = dict(self.radio_info)  # last known details, so the UI can still name the radio while it's away
        iface = self.iface      # read once: the connect thread can set it to None at any moment
        try:
            u = iface.getMyUser()
            node = {"id": u.get("id"), "long_name": u.get("longName"),
                    "short_name": u.get("shortName"), "hw": u.get("hwModel")}
        except Exception:
            pass
        age = self.endpoint.silence(iface) if iface is not None else None
        return {
            # "connected" means the library's reader is alive (checked per connection mode), not just that self.iface is set
            "connected": bool(iface and self.endpoint.alive(iface)),
            "searching": iface is None,
            # a plain string naming the connection: /dev/ttyUSB0, tcp://host:4403 or ble:ADDRESS ("auto" before a USB radio is found)
            "port": self.port or self.endpoint.initial_label(), "node": node, "model": self.model,
            "command": self.args.command, "ollama_ok": self.ollama_ok(),
            "paused": self.paused, "uptime_s": int(time.time() - self.started),
            "cooldown_s": self.args.cooldown, "max_chunks": self.args.max_chunks,
            "memory_turns": self.args.memory_turns, "memory_hours": self.args.memory_hours,
            "queue_depth": self.depth(), "max_queue": self.args.max_queue,
            "access_mode": self.mode,
            "temp_unit": self.mesh.temp_unit(), "dist_unit": self.mesh.dist_unit(),
            "radio_change": self.mesh.radio_change_active(),
            # seconds since the radio last sent anything (None when this connection mode does not record it) and the silence that counts as dead
            "last_rx_age_s": None if age is None else int(age),
            "silence_limit_s": self.endpoint.silence_limit or None,
            "demo": bool(getattr(self.args, "demo", False)),   # --demo: the dashboard shows a "Demo mode" badge
        }

    def known_nodes(self):
        """Nodes this radio has heard, for the web UI's recipient picker."""
        out = []
        try:
            for n in list(self.iface.nodes.values()):
                u = n.get("user") or {}
                if u.get("id"):
                    out.append({"id": u["id"], "long_name": u.get("longName"),
                                "short_name": u.get("shortName"), "last_heard": n.get("lastHeard") or 0})
        except Exception:
            pass
        try:
            mine = self.iface.getMyUser().get("id")
        except Exception:
            mine = None
        return sorted((n for n in out if n["id"] != mine), key=lambda n: -n["last_heard"])

    def conversations(self, scope=None):
        """Conversation list for the web UI, each with `memory` = how many past messages the model
        would be shown for that node (not computed for the direct-message scope, which has no AI memory)."""
        convs = self.audit.conversations(scope)
        for c in convs:
            c["memory"] = len(self.history(c["node_id"])) if scope != "dm" else 0
        return convs

    # ---- receiving --------------------------------------------------------
    def node_name(self, node_id):
        """Long name the radio has on file for a node, or None. Not sanitised: pass it through clean_text before sending it anywhere."""
        if node_id == WEB_SENDER:
            return "Web console"
        try:
            return self.iface.nodes[node_id]["user"].get("longName")
        except Exception:
            return None

    def on_receive(self, packet, interface):
        """Handler for every text packet the radio receives (runs on the meshtastic library's thread).

        Only direct messages to this node are looked at. "/ai ..." commands go through, in order:
        access control, canned commands (reset, actions, cancel, confirm), per-node cooldown, pause,
        help/ping/status, one-question-at-a-time, daily cap, then the model queue. Other DMs are
        only logged. Nothing here may raise: an exception would kill the library's callback thread.
        """
        try:
            text = packet.get("decoded", {}).get("text", "")
            sender = packet.get("fromId")
            if not text or not sender:
                return
            my_num = interface.myInfo.my_node_num
            if packet.get("from") == my_num:
                return  # our own message echoed back
            if packet.get("to") != my_num:
                return  # channel/broadcast message, not a DM to this node (the AI never answers or reads the channel)
            pid = packet.get("id")
            if not self.remember_packet(pid):   # the mesh can deliver the same packet twice; answer it once
                return

            access, cap_override = self.audit.get_access(sender)
            tier, auth_label = self.verify_node(sender, packet)   # decided per message, never cached: the key or encryption can change
            whole = lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 7          # how the firmware sends hop counts
            num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) else None     # a malformed field must not stop the reply
            hs, hl = packet.get("hopStart"), packet.get("hopLimit")
            hops = hs - hl if whole(hs) and whole(hl) and hs >= hl else None
            meta = dict(node_id=sender, node_name=self.node_name(sender), auth=auth_label,
                        rx_snr=num(packet.get("rxSnr")), rx_rssi=num(packet.get("rxRssi")), hops=hops)

            head, _, prompt = text.strip().partition(" ")
            if head.lower() != self.args.command.lower():
                # An ordinary DM (e.g. a reply to the operator). Logged for the conversation view,
                # never given to the model. Blocked nodes are ignored entirely.
                if access != "block" and not self.args.no_log_inbound:
                    self.audit.new_request(prompt=text.strip(), status="inbound", kind="inbound", **meta)
                    print(f"[dm] {sender}: {text.strip()}")
                return
            prompt = prompt.strip()
            low = prompt.lower()
            is_confirm = low == "confirm" or low.startswith("confirm ")
            logged = f"confirm {REDACTED}" if is_confirm else prompt  # never store a live one-time code

            # Access control comes first and is silent: replying to blocked or unknown nodes would
            # let anyone in range trigger radio traffic. The operator sees it in the audit log.
            denial = "blocked" if access == "block" else (
                "denied" if self.mode == "allowlist" and access != "allow" else None)
            if denial:
                now = time.monotonic()
                if now - self.last_denied.get(sender, -1e9) > 60:  # don't let a spammer flood the log
                    self.last_denied[sender] = now
                    self.audit.new_request(prompt=logged, status=denial, **meta)
                    print(f"[access] {denial}: {sender}")
                return

            def canned(status, msg, **extra):
                """Log a request row and send a fixed reply to the sender; no model involved."""
                rid = self.audit.new_request(prompt=logged, status=status, response=msg, **meta, **extra)
                self.queue_reply(rid, sender, msg)

            self.sweep_pending()   # so "cancel"/"confirm" below never see an expired confirmation
            if not prompt:
                return canned("usage", f"Usage: {self.args.command} <question>  "
                                       f"(or {self.args.command} reset to clear my memory of you)")
            if low in RESET_WORDS:
                self.audit.clear_memory(sender)
                print(f"[memory] cleared for {sender}")
                return canned("reset", "OK, I've forgotten our earlier conversation.")
            if low == "actions":
                return canned("usage", self.actions_help(tier))
            if low == "cancel":
                p = self.drop_pending(sender)
                if p:
                    self.audit.update(p.rid, status="cancelled", response="Cancelled by the user.")
                return canned("usage", "Cancelled." if p else "Nothing is waiting for confirmation.")
            if is_confirm:
                return self.handle_confirm(sender, prompt[len("confirm"):].strip(), tier, auth_label, canned, meta, logged)

            # reset/actions/cancel/confirm return above, so they skip the cooldown (a code can be typed right after the prompt);
            # everything below counts as a real request and starts the cooldown.
            now = time.monotonic()
            wait = self.args.cooldown - (now - self.last_request.get(sender, -1e9))
            if wait > 0:
                return canned("rate_limited", f"Slow down - try again in {int(wait) + 1}s.")
            self.last_request[sender] = now
            if self.paused:
                return canned("paused", "The AI assistant is paused right now.")
            if low in HELP_WORDS:
                return canned("help", self.help_text())
            if low == "ping":
                return canned("ping", self.ping_text(packet, hops))
            if low == "status":
                return canned("status", self.status_text())
            if self.pending_for(sender):
                return canned("rate_limited", "I'm still working on your last question.")

            cap = self.effective_cap(cap_override)
            if cap and self.audit.used_24h(sender) >= cap:
                if now - self.cap_notified.get(sender, -1e9) > 3600:  # tell them once an hour at most
                    self.cap_notified[sender] = now
                    return canned("cap", f"Daily limit of {cap} questions reached. Try again later.")
                self.audit.new_request(prompt=logged, status="cap", **meta)  # log, stay quiet
                return

            print(f"[rx] {sender}: {prompt}")
            rid = self.audit.new_request(prompt=prompt, status="queued", **meta)
            pos = self.enqueue(rid, sender, prompt, tier=tier)
            if pos is None:
                self.audit.update(rid, status="busy", response="Busy - the queue is full, try again in a minute.")
                return self.queue_reply(rid, sender, "Busy - the queue is full, try again in a minute.")
            if pos >= 2 and not self.args.no_queue_notice:
                # Untracked courtesy message (no audit row, no delivery accounting).
                self.outbox.put((None, sender, [f"Queued (#{pos}, about {self.eta_s(pos)}s). "
                                                 "I'll answer when it's your turn."], 0))
        except Exception as e:  # never let a bad packet kill the callback thread
            print(f"[error] on_receive: {e}", file=sys.stderr)

    def handle_confirm(self, sender, code, tier, auth_label, canned, meta, logged):
        """`/ai confirm <code>`: run the pending action if the code, sender and expiry all check out.

        `canned`, `meta` and `logged` are the reply helper, audit fields and redacted prompt from
        on_receive. The action runs on its own thread so the radio callback is not blocked."""
        p = self.pending.get(sender)
        if not p:
            return canned("usage", "Nothing is waiting for confirmation (it may have expired).")
        if tier < p.action.tier:  # e.g. not PKI-encrypted this time, or key changed since
            return canned("action_denied", f"Can't confirm from this message ({auth_label}).")
        if not secrets.compare_digest(code, p.code):
            p.attempts += 1                 # only the radio thread confirms (the browser chat refuses confirm), so this counter needs no lock
            if p.attempts >= 3:
                if not self.take_pending(sender, p):
                    return canned("usage", "Nothing is waiting for confirmation (it may have expired).")
                self.audit.update(p.rid, status="cancelled", response="Cancelled after 3 wrong codes.")
                return canned("action_denied", "Wrong code three times - request cancelled.")
            return canned("action_denied", f"Wrong code ({p.attempts}/3).")
        if not self.take_pending(sender, p):   # single use: claimed atomically before anything runs, so it cannot run twice
            return canned("usage", "Nothing is waiting for confirmation (it may have expired).")
        self.audit.update(p.rid, status="action_ok",
                          response=f"Confirmed by the user; {p.action.name} ran (result is in the next entry).")
        # not 'queued': a crash mid-action must not make the restart logic re-ask the model this text
        rid = self.audit.new_request(prompt=logged, status="action_running", action=p.action.name, **meta)

        def finish():
            """Run the confirmed action and send its result back to the sender."""
            ok, text = self.run_action(p.action, p.params)
            print(f"[action] {sender}: confirmed {p.action.name} -> {'ok' if ok else 'FAILED'}")
            self.audit.update(rid, status="action_ok" if ok else "action_failed", response=text)
            self.queue_reply(rid, sender, text)

        threading.Thread(target=finish, daemon=True).start()

    # ---- sending ----------------------------------------------------------
    def _ack_handler(self, rid, dest, msg, attempt=0):
        """Build the callback for one sent radio message. On a routing error it re-queues that part
        (up to --send-retries, after --retry-delay); otherwise it records delivered/relayed/failed
        on the audit row. `attempt` counts resends already made."""
        label = msg[:6] if msg.startswith("(") else "message"
        def onAckNak(packet):  # the library only calls handlers with this exact name for ACKs
            """Called by the meshtastic library with the routing response (ack or error) for this message."""
            routing = packet.get("decoded", {}).get("routing", {})
            reason = routing.get("errorReason", "NONE")
            if reason != "NONE":
                if attempt < self.args.send_retries:
                    # The radio gave up on this part (it already retries on its own). Send it again
                    # after a pause instead of silently leaving the reader with a missing piece.
                    print(f"[ack] request {rid}: part {label} failed ({reason}); "
                          f"resending ({attempt + 1}/{self.args.send_retries})")
                    self.audit.add_note(rid, f"{label} {reason}, resent")
                    # Timer, not sleep: this callback runs on the library's thread and must return quickly.
                    threading.Timer(self.args.retry_delay,
                                    lambda: self.outbox.put((rid, dest, [msg], attempt + 1))).start()
                    return
                kind = "failed"
                self.audit.add_note(rid, f"{label} {reason}, gave up after {attempt} resend(s)")
            elif packet.get("fromId") == dest:
                kind = "delivered"
            else:
                kind = "relayed"  # implicit ack: a neighbour rebroadcast it, receiver not confirmed
            print(f"[ack] request {rid}: {kind}" + ("" if kind != "failed" else f" ({reason})"))
            self.audit.bump(rid, kind)
        return onAckNak

    def make_messages(self, text, max_parts):
        """Text -> the exact radio messages to send, numbered "(i/n) ..." when there is more than one."""
        # Prefix "(i/n) " is added after chunking and is not counted in --chunk-bytes; the default limit leaves headroom for it.
        chunks = chunk_text(text, self.args.chunk_bytes)
        if len(chunks) > max_parts:  # over the cap: fill the allowed parts fully instead of evening out
            chunks = _greedy_chunks(text, self.args.chunk_bytes)[:max_parts]
        total = len(chunks)
        return [f"({i}/{total}) {c}" if total > 1 else c for i, c in enumerate(chunks, 1)]

    def ask_web(self, prompt):
        """A question typed into the web UI. Runs through the normal queue, gate, model and read-only tools (tier 0), and the
        answer is only stored (the browser reads it from the audit log), never sent over the radio. Returns the request id."""
        prompt = prompt.strip() if isinstance(prompt, str) else ""
        if not prompt:
            raise ValueError("Type a question first.")
        if len(prompt) > WEB_MAX_PROMPT:
            raise ValueError(f"Keep it under {WEB_MAX_PROMPT} characters.")
        cmd = prompt.lower().removeprefix(self.args.command.lower()).strip()        # "help" and "/ai help" both work
        if cmd in HELP_WORDS or cmd in ("ping", "status"):                          # answered directly, no model, same as over the radio
            text = self.help_text() if cmd in HELP_WORDS else self.status_text() if cmd == "status" else "Pong from the web console: this question didn't use the radio."
            return self.audit.new_request(WEB_SENDER, "Web console", prompt, status="help" if cmd in HELP_WORDS else cmd, response=text, kind="web")
        if self.pending_for(WEB_SENDER):
            raise ValueError("Still working on your last question.")
        rid = self.audit.new_request(WEB_SENDER, "Web console", prompt, status="queued", kind="web")
        if self.enqueue(rid, WEB_SENDER, prompt, tier=0) is None:
            self.audit.update(rid, status="busy", response="The question queue is full.")
            raise ValueError("The question queue is full; try again in a moment.")
        print(f"[web] question: {prompt[:80]}")
        return rid

    def queue_reply(self, rid, dest, text):
        """Hand an answer to the sender thread, split into numbered radio messages (at most --max-chunks).
        Does not transmit by itself. For the web console nothing is sent; the audit row already holds the answer."""
        if dest == WEB_SENDER:                      # the browser chat: the answer is already in the audit log
            self.audit.update(rid, chunks=0)
            return
        if not (text or "").strip():                # an empty reply would send nothing yet be logged as answered, leaving the asker waiting
            text = BLANK_REPLY_ANSWER               # neutral wording: it may be the model's ignored tool call, not a result
            self.audit.update(rid, response=text)   # keep the log (and the dashboard's chat view) in step with what was actually sent
        msgs = self.make_messages(text, self.args.max_chunks)
        self.audit.update(rid, chunks=len(msgs))
        self.outbox.put((rid, dest, msgs, 0))

    def send_manual(self, node_id, text):
        """Operator message from the web UI. Raises ValueError with a user-facing reason.
        Queues a direct message that the sender thread transmits over the radio (up to MANUAL_MAX_CHUNKS parts);
        bypasses the access rules, caps and AI. Returns the audit request id."""
        node_id = node_id.strip().lower() if isinstance(node_id, str) else ""
        text = text.strip() if isinstance(text, str) else ""
        if not NODE_ID_RE.match(node_id):
            raise ValueError("Node ID must look like !1a2b3c4d")
        if not text:
            raise ValueError("Message is empty")
        msgs = self.make_messages(text, MANUAL_MAX_CHUNKS + 1)   # one extra part, so "too long" can be told from "exactly at the limit"
        if len(msgs) > MANUAL_MAX_CHUNKS:
            raise ValueError(f"Message too long (max {MANUAL_MAX_CHUNKS} radio messages of ~{self.args.chunk_bytes} bytes)")
        rid = self.audit.new_request(node_id, self.node_name(node_id), "", status="manual",
                                     response=text, kind="manual")
        self.audit.update(rid, chunks=len(msgs))
        self.outbox.put((rid, node_id, msgs, 0))
        print(f"[manual] -> {node_id}: {text}")
        return rid

    def sender_loop(self):
        """The only thread that writes to the radio; drains the outbox one message at a time.

        Outbox items are (request id or None, destination, list of message texts, attempt number).
        Waits for the radio to come back if it is unplugged (see _wait_for_radio) and pauses
        --chunk-delay between parts so a long reply doesn't flood the mesh.
        """
        while True:
            rid, dest, msgs, attempt = self.outbox.get()
            if dest == CHANNEL_DEST:                       # a post on the public channel, typed by the operator
                if not self._wait_for_radio():
                    self.channel.give_up(rid)
                else:
                    self.channel.transmit(self.iface, rid, msgs[0])
                continue
            for i, msg in enumerate(msgs, 1):
                label = msg[:6] if msg.startswith("(") else "message"
                if not self._wait_for_radio():
                    print(f"[error] send: no radio for {self.args.reconnect_hold:.0f}s, dropping {label}", file=sys.stderr)
                    if rid is not None:
                        self.audit.add_note(rid, f"{label} radio offline, gave up")
                        self.audit.bump(rid, "failed")
                    continue
                try:
                    # wantAck so the radio reports delivery/failure; untracked courtesy messages (rid None) skip the callback
                    self.iface.sendText(msg, destinationId=dest, wantAck=True,
                                        onResponse=self._ack_handler(rid, dest, msg, attempt) if rid is not None else None)
                    print(f"[tx] {msg}" + (f"   (resend {attempt})" if attempt else ""))
                except Exception as e:
                    # typically the radio vanished mid-send: try again once it is back, like a NAK
                    print(f"[error] send: {e}", file=sys.stderr)
                    if rid is not None and attempt < self.args.send_retries:
                        self.audit.add_note(rid, f"{label} send error, resent")
                        threading.Timer(self.args.retry_delay,
                                        lambda r=rid, d=dest, m=msg, a=attempt: self.outbox.put((r, d, [m], a + 1))).start()
                    elif rid is not None:
                        self.audit.add_note(rid, f"{label} send error, gave up")
                        self.audit.bump(rid, "failed")
                if i < len(msgs):
                    time.sleep(self.args.chunk_delay)

    def _wait_for_radio(self):
        """Hold outgoing messages while the radio is unplugged, for up to --reconnect-hold seconds."""
        while self.iface is None:
            if time.time() - self.down_since > self.args.reconnect_hold:
                return False
            time.sleep(0.25)
        return True

    # ---- model worker -----------------------------------------------------
    def worker(self):
        """Runs forever on its own thread: takes one queued question at a time, asks the model and
        queues the reply (or handles the tool call it asked for). One at a time because the local
        model is the bottleneck. Every failure ends in a reply or an audit entry; the loop never dies."""
        while True:
            with self.qcv:
                while not self.q:
                    self.qcv.wait()
                # set `current` in the same locked step as the pop, so depth() never sees the job as neither queued nor running
                job = self.current = self.q.popleft()
            try:
                t0 = time.monotonic()
                model_used = self.model  # what the audit records (ask_llm reads the same setting)
                try:
                    text, calls = self.ask_llm(job.prompt, self.history(job.sender), job.tier)
                    ms = int((time.monotonic() - t0) * 1000)
                    self.recent_ms.append(ms)
                    if calls and job.tier >= 0:
                        # Only the first call is considered; tools are never offered (or honoured)
                        # for nodes that failed verification.
                        self.handle_tool_call(job, calls[0], ms)
                        continue   # handle_tool_call queued its own reply; `finally` still clears `current`
                    self.audit.update(job.rid, status="answered", response=text, model=model_used, llm_ms=ms)
                    answer = text
                except EmptyAnswer as e:
                    print(f"[error] {e}", file=sys.stderr)
                    answer = "The model gave no answer - please try again."
                    self.audit.update(job.rid, status="llm_error", response=f"{answer} ({e})", model=model_used)
                except Exception as e:
                    print(f"[error] ollama: {e}", file=sys.stderr)
                    answer = "LLM unavailable right now."
                    self.audit.update(job.rid, status="llm_error", response=f"{answer} ({e})",
                                      model=model_used)
                self.queue_reply(job.rid, job.sender, answer)
            except Exception as e:
                print(f"[error] worker: {e}", file=sys.stderr)
            finally:
                with self.qcv:
                    self.current = None

    # ---- connection handling ---------------------------------------------
    def on_lost(self, interface):
        """pubsub handler for the library's "connection lost" event (see the note below on why it isn't acted on)."""
        # The library also fires this when the node merely reboots (which happens every time the
        # serial port is opened) and then reloads its config on its own - that is not a disconnect.
        # Real disconnects are noticed by wait_for_loss(), so this only logs the benign case.
        if interface is self.iface:
            # wait a moment: a reboot reconnects by itself, and then the link check passes
            threading.Timer(3.0, self._note_reboot, args=(interface,)).start()

    def _note_reboot(self, interface):
        """Log (only) that the node rebooted, if the link is still healthy a few seconds after the lost event."""
        if interface is self.iface and self.endpoint.link_problem(interface, self.port) is None:
            print("[info] node rebooted, reconnected")

    # ---- finding and keeping a radio -------------------------------------
    def candidates(self):
        """Labels worth trying right now, best first, leaving out the ones in back-off. Empty = keep waiting."""
        now = time.time()
        return [t for t in self.endpoint.candidates(self.port) if self.bad_until.get(t, 0) <= now]

    def attach(self, iface, port):
        """Adopt a freshly opened interface as the live radio: remember its identity, log a swap for a
        different radio or connection, and reset the search state. `port` is the connection's label (see connection.py).
        Runs on the connect_loop thread."""
        info = {}
        try:
            u = iface.getMyUser()
            info = {"id": u.get("id"), "long_name": u.get("longName"),
                    "short_name": u.get("shortName"), "hw": u.get("hwModel")}
        except Exception:
            pass
        if self.radio_id and info.get("id") and info["id"] != self.radio_id:
            print(f"[radio] this is a different radio than before ({self.radio_id} -> {info['id']})")
        new_id, before = info.get("id"), self.audit.get_setting("last_radio_id")   # remembered across restarts, so a swap while stopped is noticed too
        if new_id:
            if before and before != new_id:
                self.mesh.note_radio_change(before, new_id, self.audit.get_setting("last_radio_name"), info.get("long_name"))
            self.audit.set_setting("last_radio_id", new_id); self.audit.set_setting("last_radio_name", info.get("long_name") or "")
        if self.port and port != self.port:
            print(f"[radio] connection changed: {self.port} -> {port}")
        self.radio_id = info.get("id") or self.radio_id
        self.radio_info, self.port, self.iface = info, port, iface
        self.connects += 1
        self._search_logged = False
        print(f"Connected on {port}: {info.get('long_name') or 'radio'} ({info.get('id') or 'unknown id'}). "
              f"Listening for '{self.args.command}' -> Ollama model '{self.model}'. Access: {self.mode}.")

    def detach(self, iface):
        """Forget the radio (self.iface = None) and start the clock that --reconnect-hold measures.
        radio_info and radio_id are kept so the UI can still name the radio while it is away."""
        self.iface = None
        self.down_since = time.time()
        # release the handle (a serial port, socket or Bluetooth link) so it can be reopened; closing a dead link can hang,
        # so do it on a throwaway thread rather than stall the reconnect loop
        threading.Thread(target=lambda: self._quiet_close(iface), daemon=True).start()

    @staticmethod
    def _quiet_close(iface):
        """Close a radio interface, ignoring any error (it may already be dead)."""
        try:
            iface.close()
        except Exception:
            pass

    def stop(self):
        """Ask connect_loop() to finish (used for shutdown and in tests)."""
        self._stopping = True

    def wait_for_loss(self, iface, port):
        """Block while the radio is healthy; return why it stopped being."""
        while not self._stopping:
            time.sleep(self.args.scan_interval)
            problem = self.endpoint.link_problem(iface, port) or self._swapped(iface)
            if problem:
                return problem
        return "bridge stopping"

    def _swapped(self, iface):
        """A reason if the radio behind this still-open connection is not the one we attached.
        The library can reconnect a TCP link by itself, so a different radio at the same address would otherwise go unnoticed."""
        try:
            now = iface.getMyUser().get("id")
        except Exception:
            return None     # no identity yet (the library is re-reading the config): not a verdict
        known = self.radio_info.get("id")
        return f"the radio behind {self.port} changed ({known} -> {now})" if now and known and now != known else None

    def connect_loop(self):
        """Until stopped: find a radio, connect, watch it, and start over when it goes away."""
        while not self._stopping:
            targets = self.candidates()
            if not targets:
                if not self._search_logged:
                    print(f"[radio] waiting for {self.endpoint.waiting_text()} - the web UI is still up")
                    self._search_logged = True
                time.sleep(self.args.scan_interval)
                continue
            label = targets[0]
            try:
                iface = self.endpoint.open(label)
            except Exception as e:
                # park the failing target so candidates() doesn't retry it in a tight loop
                wait, why = self.endpoint.failure_reason(label, e)
                self.bad_until[label] = time.time() + wait
                print(f"[radio] could not use {label}: {why}; trying again in {wait}s")
                time.sleep(self.args.scan_interval)
                continue
            self.attach(iface, label)
            reason = self.wait_for_loss(iface, label)
            print(f"[radio] lost {label}: {reason}. Searching again...")
            self.detach(iface)
            wait = self.endpoint.loss_backoff(label)    # normally 0; positive after repeated silence losses that brought no data
            if wait:
                self.bad_until[label] = time.time() + wait
            self._search_logged = True  # already announced above

    def connect_demo(self):
        """--demo: attach the simulated radio instead of searching for a real one, and block until stop() (see demo.py)."""
        from meshllm import demo   # imported here so a normal start never loads the simulator
        demo.run(self)

    def run(self):
        """Start everything and block in connect_loop() (or connect_demo() with --demo) until Ctrl+C or stop().
        Subscribers are attached before the first radio connects so no early packet is missed."""
        pub.subscribe(self.on_receive, "meshtastic.receive.text")
        pub.subscribe(self.coverage.on_packet, "meshtastic.receive")             # only records while you have a walk test running
        pub.subscribe(self.channel.on_text, "meshtastic.receive.text")           # channel posts: stored for the Channel page, never seen by the AI
        pub.subscribe(self.on_lost, "meshtastic.connection.lost")
        pub.subscribe(self.telemetry.on_packet, "meshtastic.receive.telemetry")  # telemetry the radio hears
        pub.subscribe(self.mesh.on_packet, "meshtastic.receive")                 # every packet, for the Home tab's counts
        self.restore_queue()
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.sender_loop, daemon=True).start()
        if not getattr(self.args, "no_warm_up", True):
            self.warm_up()
        self.mesh.start()   # counts/snapshots for Home, and the hourly prune of old telemetry
        demo_mode = getattr(self.args, "demo", False)
        mode = "with the simulated demo radio" if demo_mode else self.endpoint.describe()
        print(f"Bridge starting: {mode}. Ollama model '{self.model}'. Ctrl+C to stop.")
        if not self.args.no_web:
            self.web_server = webui.start(self)
            if self.args.web_host in webui.WILDCARD_HOSTS:     # a wildcard address is not one a browser should use
                print(f"Web UI: http://127.0.0.1:{self.args.web_port}/  (listening on all interfaces inside a container; "
                      f"what can reach it is decided by the published port)  (audit log: {self.args.db})")
            else:
                print(f"Web UI: http://{self.args.web_host}:{self.args.web_port}/  (audit log: {self.args.db})")
        try:
            if demo_mode:
                self.connect_demo()
            else:
                self.connect_loop()
        except KeyboardInterrupt:
            pass
        finally:
            if self.iface:      # on a daemon thread with a short wait: a hung Bluetooth close must not keep the process from exiting
                closer = threading.Thread(target=self._quiet_close, args=(self.iface,), daemon=True, name="exit-close")
                closer.start()
                closer.join(3.0)


def build_parser():
    """The command-line flags (a function of its own so tests can parse the same flags the real start-up uses)."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", default="auto",
                   help="serial port of the Meshtastic node, or 'auto' (default) to detect it, follow it if the "
                        "port number changes, and reconnect after it is unplugged")
    p.add_argument("--probe-unknown", action="store_true",
                   help="with --port auto, also try USB serial devices whose chip isn't a known Meshtastic one")
    p.add_argument("--tcp", default=None, metavar="HOST[:PORT]",
                   help="reach the radio over Wi-Fi instead of USB: its host name or IP address (port %d by default). "
                        "Not combinable with --ble or a pinned --port" % connection.DEFAULT_TCP_PORT)
    p.add_argument("--ble", default=None, metavar="ADDRESS_OR_NAME",
                   help="reach the radio over Bluetooth instead of USB: its address or name as --ble-scan prints it "
                        "(needs Bluetooth on this computer, not inside a container)")
    p.add_argument("--link-silence", type=float, default=None, metavar="SECONDS", help=argparse.SUPPRESS)   # advanced: see connection.py
    p.add_argument("--ble-scan", action="store_true",
                   help="list nearby Meshtastic Bluetooth radios (name and address), then exit without starting the bridge")
    p.add_argument("--scan-interval", type=float, default=2.0,
                   help="seconds between checks for a radio / for the radio going away")
    p.add_argument("--reconnect-hold", type=float, default=60.0,
                   help="how long outgoing messages wait for a missing radio before being dropped")
    p.add_argument("--model", default=None,
                   help="Ollama model to use. Not needed: pick (or download) one in the web UI's Model tab and "
                        "it is remembered. Giving it here overrides and saves the choice.")
    p.add_argument("--command", default="/ai", help="trigger word")
    p.add_argument("--ollama-url", default="http://localhost:11434")
    p.add_argument("--max-tokens", type=int, default=150, help="cap on generated tokens")
    p.add_argument("--num-ctx", type=int, default=4096, help="model context window in tokens")
    p.add_argument("--max-chunks", type=int, default=4, help="max mesh messages per AI reply")
    p.add_argument("--chunk-delay", type=float, default=4.0, help="seconds between reply chunks")
    p.add_argument("--chunk-bytes", type=int, default=MAX_CHUNK_BYTES,
                   help="max text bytes per radio message (PKI DMs have less room than plain ones)")
    p.add_argument("--send-retries", type=int, default=2,
                   help="how many times to resend a part the radio reports as failed (0 = never)")
    p.add_argument("--retry-delay", type=float, default=10.0, help="seconds to wait before resending a failed part")
    p.add_argument("--cooldown", type=float, default=20.0, help="per-node seconds between requests")
    p.add_argument("--timeout", type=float, default=120.0, help="Ollama request timeout")
    p.add_argument("--max-queue", type=int, default=5, help="questions allowed to wait (incl. the one running)")
    p.add_argument("--queue-ttl", type=float, default=600.0,
                   help="after a restart, answer questions queued within this many seconds; expire older ones")
    p.add_argument("--no-queue-notice", action="store_true",
                   help="don't tell users their queue position when they have to wait")
    p.add_argument("--confirm-seconds", type=float, default=60.0,
                   help="how long a one-time confirmation code for a AI tool stays valid")
    p.add_argument("--traceroute-timeout", type=float, default=60.0, help="how long to wait for a traceroute reply")
    p.add_argument("--traceroute-cooldown", type=float, default=30.0,
                   help="minimum seconds between two traceroutes to the same node (each one loads the mesh)")
    p.add_argument("--telemetry-retention-days", type=int, default=None,
                   help="delete telemetry readings older than this many days (0 = keep forever; default 30, or what was "
                        "last set in the Telemetry tab; giving it here overrides and saves it)")
    p.add_argument("--telemetry-passive-gap", type=float, default=60.0,
                   help="keep at most one broadcast per node and kind in this many seconds")
    p.add_argument("--no-mesh-stats", action="store_true",
                   help="don't count packets or take mesh-health snapshots for the Home tab")
    p.add_argument("--mesh-sample-interval", type=float, default=300.0,
                   help="seconds between mesh-health snapshots for the Home tab's history chart")
    p.add_argument("--no-tool-gate", action="store_true",
                   help="skip the yes/no pre-check and offer tools to verified nodes on every message")
    p.add_argument("--access-mode", choices=sorted(MODES), default=None,
                   help="open = anyone except blocked nodes; allowlist = only allowed nodes "
                        "(overrides the setting saved from the web UI)")
    p.add_argument("--daily-cap", type=int, default=None,
                   help="default AI questions per node per rolling 24 h, 0 = unlimited "
                        "(overrides the setting saved from the web UI)")
    p.add_argument("--memory-turns", type=int, default=6,
                   help="past exchanges remembered per node (0 disables memory)")
    p.add_argument("--memory-hours", type=float, default=24.0,
                   help="forget a node's history after this long (0 = never)")
    p.add_argument("--memory-chars", type=int, default=3000, help="cap on remembered text per request")
    p.add_argument("--no-log-inbound", action="store_true",
                   help="don't record plain (non-/ai) DMs to this node")
    p.add_argument("--db", default=str(Path(__file__).resolve().parent.parent / "audit.db"), help="audit database file")
    p.add_argument("--web-host", default="127.0.0.1",
                   help="web UI bind address (default localhost only; it shows message content)")
    p.add_argument("--web-port", type=int, default=8080)
    p.add_argument("--no-web", action="store_true", help="disable the web UI")
    p.add_argument("--no-warm-up", action="store_true", help="don't load the model into Ollama's memory at start-up")
    p.add_argument("--demo", action="store_true",
                   help="try the dashboard with no radio and no Ollama: a simulated mesh, a temporary database, nothing transmitted")
    p.add_argument("--demo-speed", type=float, default=1.0, help="with --demo: how fast the simulated mesh and its questions run (default 1)")
    p.add_argument("--demo-scripted", action="store_true",
                   help="with --demo: always use the built-in scripted model, even if Ollama is running")
    return p


def parse_cli(argv=None):
    """(parser, args) for the command line, with the connection flags checked against each other (--tcp, --ble and a pinned --port
    are alternatives). Exits with an argparse error when they are not."""
    parser = build_parser()
    args = parser.parse_args(argv)
    connection.check_args(parser, args)
    return parser, args


def ble_scan_main():
    """--ble-scan: print nearby Meshtastic Bluetooth radios and return the exit code. Starts no bridge and opens no database."""
    try:
        print("Scanning for Bluetooth radios (about 10 seconds)...", flush=True)
        found = connection.scan_ble()
    except connection.BleUnavailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:      # adapter switched off, no BlueZ/D-Bus, no permission...
        print(f"error: the Bluetooth scan failed ({type(e).__name__}: {str(e)[:100]})", file=sys.stderr)
        return 1
    for name, address in found:
        print(f"{name}  {address}")
    if found:
        print("Start the bridge with:  python -m meshllm --ble ADDRESS")
    else:
        print("No Meshtastic Bluetooth radios found. Is the radio on, in range and not connected to a phone?")
    return 0


def _stop_on_sigterm(signum, frame):
    """SIGTERM (`kill`, `docker stop`, systemd) ends the bridge like Ctrl+C, so it closes the radio and the database cleanly. It matters
    most in a container, where the bridge is process 1 and Linux ignores SIGTERM for process 1 unless the program handles it."""
    raise KeyboardInterrupt


def main():
    """Command-line entry point: parse flags, apply any staged database restore, then run the bridge."""
    parser, args = parse_cli()
    if args.ble_scan:
        sys.exit(ble_scan_main())
    if args.ble and not args.demo:
        try:
            connection.load_ble()       # fail now with one friendly line instead of retrying forever in the background
        except connection.BleUnavailable as e:
            parser.error(str(e))
    if args.demo:
        from meshllm import demo
        sys.exit(demo.launch(args, Bridge, parser))     # temporary folders, signals and cleanup are all handled there
    try:
        signal.signal(signal.SIGTERM, _stop_on_sigterm)
    except (ValueError, OSError, AttributeError):       # not the main thread, or no such signal here: Ctrl+C still works
        pass
    apply_staged_restore(args.db)           # a database restore set aside from the dashboard is swapped in before anything opens it
    Bridge(args).run()


if __name__ == "__main__":
    main()
