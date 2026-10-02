"""Reads the saved evaluation results (eval_results/) and the project's write-ups (docs/*.md) for the Evaluation page. Read-only.

Tool-choice runs are the CSV files `eval_tools.py` writes (one row per question and repeat); a question counts as right when the
model picked the right tool, or rightly none, or answered from the live facts it was given. Usefulness runs are the JSON files
`usefulness_audit.py` writes. Development and held-out questions are kept apart: the held-out ones are only ever used to measure.
"""
import csv
import json
import re
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "eval_results"
DOCS = ROOT / "docs"
MAX_FILE = 2 * 1024 * 1024
GOOD = ("correct", "grounded_text")
DOC_NAME = re.compile(r"^[A-Za-z0-9_-]{1,60}$")


def _label(name):
    return name.rsplit(".", 1)[0].replace("_", " ")


def tool_runs(folder=RESULTS):
    """One entry per CSV of tool-choice results, newest first: model, variant, question set, score, speed, per-category scores."""
    out = []
    for f in Path(folder).glob("*.csv"):
        try:
            if f.stat().st_size > MAX_FILE:
                continue
            with open(f, newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
        except (OSError, UnicodeDecodeError, csv.Error):
            continue
        if not rows or "verdict" not in rows[0] or "prompt" not in rows[0]:
            continue
        good = sum(1 for r in rows if r["verdict"] in GOOD)
        lat = [int(r["latency_ms"]) for r in rows if (r.get("latency_ms") or "").isdigit()]
        cats = defaultdict(lambda: [0, 0])
        for r in rows:
            c = cats[r.get("category") or "?"]
            c[1] += 1
            c[0] += r["verdict"] in GOOD
        bad = defaultdict(int)
        for r in rows:
            if r["verdict"] not in GOOD:
                bad[r["verdict"]] += 1
        out.append({"file": f.name, "label": _label(f.name), "ts": f.stat().st_mtime, "model": rows[0].get("model", "?"), "variant": rows[0].get("variant", "?"),
                    "set": rows[0].get("set", "?"), "n": len(rows), "good": good, "pct": round(100 * good / len(rows), 1),
                    "avg_ms": round(sum(lat) / len(lat)) if lat else None, "categories": {k: {"good": v[0], "n": v[1]} for k, v in sorted(cats.items())},
                    "misses": dict(bad)})
    return sorted(out, key=lambda r: -r["ts"])


def usefulness_runs(folder=RESULTS):
    """One entry per usefulness-audit JSON: how many questions, how many used a tool, speed, and the mix by topic."""
    out = []
    for f in Path(folder).glob("usefulness_*.json"):
        try:
            if f.stat().st_size > MAX_FILE:
                continue
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, list) or not data or not all(isinstance(x, dict) for x in data):
            continue
        ms = [x["ms"] for x in data if isinstance(x.get("ms"), (int, float))]
        groups = defaultdict(lambda: [0, 0])
        for x in data:
            g = groups[str(x.get("group") or "?")]
            g[1] += 1
            g[0] += 1 if x.get("action") else 0
        out.append({"file": f.name, "label": _label(f.name).replace("usefulness ", ""), "ts": f.stat().st_mtime, "n": len(data),
                    "tool_answers": sum(1 for x in data if x.get("action")), "avg_ms": round(sum(ms) / len(ms)) if ms else None,
                    "groups": {k: {"tool": v[0], "n": v[1]} for k, v in sorted(groups.items())}})
    return sorted(out, key=lambda r: r["ts"])


def comparison(runs):
    """The newest run for each model + variant, with its development and held-out scores side by side."""
    by = {}
    for r in runs:                                             # runs are newest first
        key = (r["model"], r["variant"])
        slot = by.setdefault(key, {"model": r["model"], "variant": r["variant"], "dev": None, "heldout": None})
        which = "heldout" if r["set"].startswith("held") else "dev" if r["set"].startswith("dev") else None
        if which and slot[which] is None:
            slot[which] = {"pct": r["pct"], "good": r["good"], "n": r["n"], "avg_ms": r["avg_ms"], "file": r["file"]}
    rows = [v for v in by.values() if v["dev"] or v["heldout"]]
    return sorted(rows, key=lambda v: (-(v["heldout"] or v["dev"])["pct"], v["model"], v["variant"]))


def doc_list(folder=DOCS):
    return sorted(f.stem for f in Path(folder).glob("*.md") if DOC_NAME.match(f.stem))


def read_doc(name, folder=DOCS):
    """The text of docs/<name>.md, or None (only plain names: nothing else on disk can be reached)."""
    if not isinstance(name, str) or not DOC_NAME.match(name):
        return None
    f = Path(folder) / (name + ".md")
    try:
        if not f.is_file() or f.stat().st_size > MAX_FILE:
            return None
        return f.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def overview(results=RESULTS, docs=DOCS):
    runs = tool_runs(results)
    return {"generated": time.time(), "runs": runs, "comparison": comparison(runs), "usefulness": usefulness_runs(results), "docs": doc_list(docs)}
