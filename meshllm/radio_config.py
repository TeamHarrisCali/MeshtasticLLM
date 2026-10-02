"""Read, back up and change the settings stored on our own radio (the "Radio" page).

Operator-only: nothing the AI says can reach this module. The form is generated from the radio's own protobuf
definitions, so every field is type-checked and range-checked before anything is sent, and a change is all-or-nothing.

  pull     -> the settings the radio reported when we connected, plus a saved backup
  save     -> validate every change, back up the current settings, then write only the sections that changed
  restore  -> save, using the values from an earlier backup

Sections that hold secrets are never shown, saved or written: Wi-Fi (network: the password), security (the radio's
private key and admin keys) and MQTT (credentials); nor is the Bluetooth pairing PIN. Channels are not handled here.
"""
import json
import math
import struct
import time

from google.protobuf.descriptor import FieldDescriptor as FD
from google.protobuf.json_format import MessageToDict

# ---- which sections and fields are allowed -------------------------------------------------------------------
# Only the sections listed here can be viewed or changed; everything else is refused by _message(). This is an allow-list,
# so a section added to a newer firmware stays invisible until someone adds it deliberately.
CONFIG_SECTIONS = {   # name -> label
    "device": "Device", "position": "Position", "power": "Power", "display": "Display", "lora": "LoRa radio", "bluetooth": "Bluetooth",
}
MODULE_SECTIONS = {
    "telemetry": "Telemetry", "neighbor_info": "Neighbour info", "store_forward": "Store & forward", "range_test": "Range test",
    "external_notification": "External notification",
}
# section name -> (where it lives on the node: "config" = localConfig, "module" = moduleConfig, display label)
SECTIONS = {**{k: ("config", v) for k, v in CONFIG_SECTIONS.items()}, **{k: ("module", v) for k, v in MODULE_SECTIONS.items()}}
HIDDEN_FIELDS = {("bluetooth", "fixed_pin")}          # the Bluetooth pairing PIN is a credential: never shown, saved or changed here
# Sections left out on purpose because they contain secrets. Listed only so the page can say why they are missing.
EXCLUDED = {"network": "holds the Wi-Fi password", "security": "holds the radio's private key", "mqtt": "holds server credentials"}
# Extra numeric bounds for settings where the protobuf type (e.g. uint32) allows nonsense values.
LIMITS = {("lora", "hop_limit"): (0, 7), ("lora", "tx_power"): (0, 30), ("lora", "channel_num"): (0, 255)}   # tighter than the field's type
MAX_STRING = 200       # longest accepted text value
BACKUP_KEEP = 40       # settings backups kept in the database; older ones are deleted

# Changes that can cut the radio off from the mesh, break the law, or lock you out: shown in the confirmation.
RISKS = {
    ("lora", "region"): "A wrong region can be illegal where you are and stops the radio talking to your mesh.",
    ("lora", "modem_preset"): "Radios with a different preset cannot hear this one.",
    ("lora", "use_preset"): "Radios with different LoRa settings cannot hear this one.",
    ("lora", "bandwidth"): "Radios with different LoRa settings cannot hear this one.",
    ("lora", "spread_factor"): "Radios with different LoRa settings cannot hear this one.",
    ("lora", "coding_rate"): "Radios with different LoRa settings cannot hear this one.",
    ("lora", "channel_num"): "Radios on a different channel number cannot hear this one.",
    ("lora", "frequency_offset"): "Shifts the frequency; radios will not hear this one.",
    ("lora", "tx_enabled"): "With this off the radio stops transmitting: no replies, no relaying.",
    ("lora", "tx_power"): "Transmit power above the legal limit for your region is illegal.",
    ("lora", "hop_limit"): "More hops means more airtime used by every node on the mesh.",
    ("device", "role"): "The role changes how the radio relays and broadcasts; ROUTER-type roles should only be used on well-placed nodes.",
    ("device", "rebroadcast_mode"): "Changes which packets this radio relays.",
    ("bluetooth", "enabled"): "With Bluetooth off the phone app cannot connect, and neither can this bridge if it is attached over Bluetooth (USB and Wi-Fi are not affected).",
    ("position", "gps_mode"): "Changing GPS mode affects whether this radio shares a position.",
}
# Short hints shown beside a field in the form, keyed like RISKS.
HELP = {
    ("lora", "region"): "Frequency plan for where you are (US, EU_868, ...). Must match your country.",
    ("lora", "hop_limit"): "How many times a packet may be relayed (3 is the default; 7 is the maximum).",
    ("position", "position_broadcast_secs"): "How often the radio shares its position. 0 = the default.",
    ("position", "fixed_position"): "Use the fixed position set on the Map page instead of a GPS fix.",
    ("device", "node_info_broadcast_secs"): "How often the radio announces itself (minimum 3600 s = 1 hour).",
    ("telemetry", "device_update_interval"): "Seconds between battery/airtime broadcasts. 0 = the default (30 min).",
    ("telemetry", "environment_update_interval"): "Seconds between sensor broadcasts. 0 = the default.",
}


class ConfigError(ValueError):
    """A settings change the code refuses; the message is safe to show the operator."""


class RadioMismatch(ConfigError):
    """The backup was taken from a different radio than the one connected now."""


def pretty(name):
    """'hop_limit' -> 'Hop limit' (protobuf field name as a form label)."""
    return name.replace("_", " ").capitalize()


def _message(node, section):
    """The live protobuf message for an allowed section. Refuses any section not in SECTIONS, which is what keeps the
    secret-bearing ones unreachable from every caller."""
    if section not in SECTIONS:
        raise ConfigError(f"'{section}' can't be viewed or changed here.")
    if getattr(node, "localConfig", None) is None or getattr(node, "moduleConfig", None) is None:
        raise ConfigError("The radio hasn't sent its settings yet.")
    return getattr(node.localConfig if SECTIONS[section][0] == "config" else node.moduleConfig, section)


def _int_range(fd):
    """(min, max) a protobuf integer field can hold, from its bit width and signedness."""
    bits = 64 if fd.cpp_type in (FD.CPPTYPE_INT64, FD.CPPTYPE_UINT64) else 32
    signed = fd.cpp_type in (FD.CPPTYPE_INT32, FD.CPPTYPE_INT64)
    return (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1) if signed else (0, 2 ** bits - 1)


def _is_repeated(fd):
    """protobuf 5.x+ has fd.is_repeated; newer releases dropped fd.label, older ones have no is_repeated."""
    flag = getattr(fd, "is_repeated", None)
    return flag if flag is not None else fd.label == FD.LABEL_REPEATED


def _kind(fd):
    """Editor type of a field: bool, enum, string, float or int; None if it can't be edited here."""
    if _is_repeated(fd) or fd.cpp_type == FD.CPPTYPE_MESSAGE or fd.type == FD.TYPE_BYTES:
        return None                                   # lists, nested messages and raw bytes (keys) are not editable here
    return {FD.CPPTYPE_BOOL: "bool", FD.CPPTYPE_ENUM: "enum", FD.CPPTYPE_STRING: "string",
            FD.CPPTYPE_FLOAT: "float", FD.CPPTYPE_DOUBLE: "float"}.get(fd.cpp_type, "int")


def describe(msg, section):
    """The fields of one section, for the form. Non-editable kinds and HIDDEN_FIELDS are left out, so they are also
    absent from snapshot() and therefore from backups."""
    out = []
    for fd in msg.DESCRIPTOR.fields:
        kind = _kind(fd)
        if kind is None or (section, fd.name) in HIDDEN_FIELDS:
            continue
        f = {"name": fd.name, "label": pretty(fd.name), "kind": kind, "value": getattr(msg, fd.name)}
        if kind == "enum":
            f["choices"] = [v.name for v in fd.enum_type.values]
            f["value"] = fd.enum_type.values_by_number[f["value"]].name if f["value"] in fd.enum_type.values_by_number else f["value"]
        elif kind == "int":
            f["min"], f["max"] = LIMITS.get((section, fd.name)) or _int_range(fd)
        elif kind == "string":
            f["max"] = MAX_STRING
        if (section, fd.name) in HELP:
            f["help"] = HELP[(section, fd.name)]
        if (section, fd.name) in RISKS:
            f["risk"] = RISKS[(section, fd.name)]
        out.append(f)
    return out


def snapshot(node):
    """Every editable value of every allowed section: {section: {field: value}}."""
    snap = {}
    for section in SECTIONS:
        msg = _message(node, section)
        snap[section] = {f["name"]: f["value"] for f in describe(msg, section)}
    return snap


def _coerce(fd, value, section):
    """Check `value` against the field's type and range and return it in protobuf form; raises ConfigError otherwise.
    Types are checked strictly (a JSON true is not accepted as the number 1, and floats must be whole for int fields)."""
    kind = _kind(fd)
    name = fd.name
    if kind == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"{pretty(name)} must be on or off.")
        return value
    if kind == "enum":
        names = {v.name: v.number for v in fd.enum_type.values}
        if isinstance(value, str) and value in names:
            return names[value]
        raise ConfigError(f"{pretty(name)}: '{str(value)[:30]}' isn't one of the choices.")
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value != int(value):
            raise ConfigError(f"{pretty(name)} must be a whole number.")
        lo, hi = LIMITS.get((section, name)) or _int_range(fd)
        if not lo <= value <= hi:
            raise ConfigError(f"{pretty(name)} must be between {lo} and {hi}.")
        return int(value)
    if kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ConfigError(f"{pretty(name)} must be a number.")
        return struct.unpack("f", struct.pack("f", value))[0] if fd.cpp_type == FD.CPPTYPE_FLOAT else float(value)   # the radio stores 32-bit floats
    if not isinstance(value, str) or len(value) > MAX_STRING or any(not ch.isprintable() for ch in value):
        raise ConfigError(f"{pretty(name)} must be plain text of at most {MAX_STRING} characters.")
    return value


def plan(node, changes):
    """Validate {section: {field: value}} against the radio's current settings.
    Returns [(section, candidate message, [change dicts])] for the sections that really change; raises ConfigError
    before anything is touched if any part is invalid."""
    if not isinstance(changes, dict) or not changes:
        raise ConfigError("There is nothing to change.")
    out = []
    for section, fields in changes.items():
        if not isinstance(section, str) or not isinstance(fields, dict):
            raise ConfigError("Malformed request.")
        current = _message(node, section)
        cand = type(current)(); cand.CopyFrom(current)   # work on a copy; the live message is only touched in save()
        diffs = []
        for name, value in fields.items():
            fd = current.DESCRIPTOR.fields_by_name.get(name) if isinstance(name, str) else None
            if fd is None or _kind(fd) is None or (section, name) in HIDDEN_FIELDS:
                raise ConfigError(f"'{str(name)[:40]}' isn't a setting you can change in {SECTIONS[section][1]}.")
            new = _coerce(fd, value, section)
            old = getattr(current, name)
            if new == old:
                continue
            setattr(cand, name, new)
            view = lambda v: fd.enum_type.values_by_number[v].name if fd.cpp_type == FD.CPPTYPE_ENUM else v
            diffs.append({"section": section, "field": name, "label": pretty(name), "old": view(old), "new": view(new), "risk": RISKS.get((section, name))})
        if diffs:
            out.append((section, cand, diffs))
    return out


# Stored in the same SQLite file as the audit log. The `config` JSON never contains hidden or excluded settings.
SCHEMA = """
CREATE TABLE IF NOT EXISTS radio_config_backups (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    reason TEXT NOT NULL,           -- pulled | before changes | before restore
    radio  TEXT,
    config TEXT NOT NULL            -- JSON: {section: {field: value}}
);
"""


class RadioConfig:
    """Operator-facing radio settings: view the form, keep backups, and write validated changes to the connected radio."""

    def __init__(self, bridge):
        self.bridge = bridge
        self.audit = bridge.audit
        with self.audit.lock:
            self.audit.db.executescript(SCHEMA)
            self.audit.db.commit()

    # ---- plumbing ---------------------------------------------------------------------------------------------
    def _node(self):
        """The connected radio's local node object; raises ConfigError if there is no connection."""
        iface = self.bridge.iface
        if iface is None:
            raise ConfigError("The radio isn't connected.")
        return iface.localNode

    def _radio_id(self):
        """The connected radio's node id, or None if it can't be read. Used to spot a backup from a different radio."""
        try:
            return self.bridge.iface.getMyUser().get("id")
        except Exception:
            return None

    # ---- backups -------------------------------------------------------------------------------------------------
    def backup(self, reason):
        """Save the radio's current settings as a backup row, trim to BACKUP_KEEP, and return the new row id."""
        snap = snapshot(self._node())
        with self.audit.lock:
            cur = self.audit.db.execute("INSERT INTO radio_config_backups (ts, reason, radio, config) VALUES (?,?,?,?)",
                                        (time.time(), reason, self._radio_id(), json.dumps(snap)))
            self.audit.db.execute("DELETE FROM radio_config_backups WHERE id NOT IN "
                                  "(SELECT id FROM radio_config_backups ORDER BY id DESC LIMIT ?)", (BACKUP_KEEP,))
            self.audit.db.commit()
            return cur.lastrowid

    def backups(self):
        """Backup list without the settings JSON (id, ts, reason, radio), newest first."""
        with self.audit.lock:
            return [dict(r) for r in self.audit.db.execute("SELECT id, ts, reason, radio FROM radio_config_backups ORDER BY id DESC")]

    def get_backup(self, bid):
        """One backup with its settings decoded, or None if the id doesn't exist."""
        with self.audit.lock:
            r = self.audit.db.execute("SELECT * FROM radio_config_backups WHERE id=?", (bid,)).fetchone()
        if not r:
            return None
        return {"id": r["id"], "ts": r["ts"], "reason": r["reason"], "radio": r["radio"], "config": json.loads(r["config"])}

    # ---- reading -----------------------------------------------------------------------------------------------------
    def view(self):
        """Everything the page needs: the form for each section and the saved backups."""
        out = {"connected": self.bridge.iface is not None, "sections": [], "excluded": [{"name": k, "why": v} for k, v in EXCLUDED.items()],
               "backups": self.backups(), "radio": self._radio_id()}
        if not out["connected"]:
            return out
        try:
            node = self._node()
            for section, (kind, label) in SECTIONS.items():
                out["sections"].append({"name": section, "kind": kind, "label": label, "fields": describe(_message(node, section), section)})
        except ConfigError as e:
            out["error"] = str(e)
            out["sections"] = []
        return out

    def pull(self):
        """Read the radio's settings and save them as a backup."""
        bid = self.backup("pulled")
        return {**self.view(), "backup_id": bid}

    # ---- writing ---------------------------------------------------------------------------------------------------------
    def save(self, changes, reason="before changes"):
        """Validate `changes` ({section: {field: value}}), back up the current settings, and write the changed
        sections to the radio in one settings transaction. On failure the in-memory copy is rolled back and
        ConfigError is raised. The radio restarts afterwards. Holds radio_request_lock so it can't overlap a traceroute."""
        b = self.bridge
        node = self._node()
        steps = plan(node, changes)                  # raises before anything is touched
        if not steps:
            return {"changed": [], "message": "Nothing changed: those are the radio's current values."}
        if not b.radio_request_lock.acquire(timeout=5):
            raise ConfigError("The radio is busy with another request (a traceroute or a settings change); try again in a moment.")
        old = {}
        try:
            backup_id = self.backup(reason)           # always keep the settings we are about to replace
            for section, cand, _ in steps:
                live = _message(node, section)
                old[section] = type(live)(); old[section].CopyFrom(live)
                live.CopyFrom(cand)
            try:
                node.beginSettingsTransaction()
                for section, _, _ in steps:
                    node.writeConfig(section)
                node.commitSettingsTransaction()
            except Exception as e:
                for section, prev in old.items():     # the radio didn't take it: don't pretend we did
                    _message(node, section).CopyFrom(prev)
                raise ConfigError(f"The radio didn't accept the change: {e}")
        finally:
            b.radio_request_lock.release()
        changed = [d for _, _, diffs in steps for d in diffs]
        print(f"[radio] settings written ({', '.join(s for s, _, _ in steps)}): " + "; ".join(f"{d['section']}.{d['field']} {d['old']} -> {d['new']}" for d in changed))
        return {"changed": changed, "backup_id": backup_id, "restarting": True,
                "message": "Saved to the radio. It restarts to apply the change; the bridge reconnects by itself in a few seconds."}

    def restore(self, bid, force=False):
        """Apply an earlier backup through save(). Refuses (RadioMismatch) a backup from a different radio unless `force`."""
        r = self.get_backup(bid)
        if not r:
            raise ConfigError("That backup doesn't exist.")
        here = self._radio_id()
        if not force and r["radio"] and here and r["radio"] != here:
            raise RadioMismatch(f"This backup was taken from a different radio ({r['radio']}); the one connected now is {here}. "
                                "Restoring it would copy that radio's LoRa, role and other settings onto this one.")
        # ignore anything in the stored JSON that isn't an allowed section, in case the file was edited or SECTIONS changed
        changes = {s: f for s, f in r["config"].items() if s in SECTIONS and isinstance(f, dict)}
        steps = plan(self._node(), changes) if changes else []
        if not steps:
            return {"changed": [], "message": "The radio already has those settings."}
        return self.save(changes, reason="before restore")
