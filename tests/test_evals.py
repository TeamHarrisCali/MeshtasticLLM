"""The Evaluation page's data: saved runs, the models-side-by-side table, usefulness audits, and the docs (read-only, no path escapes)."""
import csv, json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixture import make, Checker
import evals as E
import requests as rq

check = Checker()
tmp = tempfile.mkdtemp(prefix="evals_")
res, docs = os.path.join(tmp, "eval_results"), os.path.join(tmp, "docs")
os.makedirs(res); os.makedirs(docs)

def write_run(name, model, variant, set_, verdicts, latency=1000):
    with open(os.path.join(res, name), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["model", "variant", "set", "category", "prompt", "run", "expected", "called", "verdict", "args_valid", "latency_ms", "reply"])
        for i, v in enumerate(verdicts):
            w.writerow([model, variant, set_, "direct" if i % 2 else "indirect", f"question {i}", 1, "x", "x", v, "yes", latency + i, ""])

write_run("dev_a.csv", "m1", "gated", "dev", ["correct"] * 8 + ["wrong_tool", "missed"])
write_run("held_a.csv", "m1", "gated", "heldout", ["correct"] * 9 + ["missed"])
write_run("dev_b.csv", "m2", "gated_retry", "dev", ["correct"] * 9 + ["grounded_text"])
write_run("held_old.csv", "m1", "gated", "heldout", ["correct"] * 5 + ["missed"] * 5)
os.utime(os.path.join(res, "held_old.csv"), (1000, 1000))                     # older than held_a
open(os.path.join(res, "notes.csv"), "w").write("a,b\n1,2\n")                 # some other CSV: ignored
open(os.path.join(res, "broken.csv"), "wb").write(b"\xff\xfe\x00garbage\x00")  # not even text: ignored
open(os.path.join(res, "empty.csv"), "w").write("")
json.dump([{"status": "action_ok", "action": "mesh_report", "ms": 2000, "group": "env", "question": "q1"}, {"status": "answered", "action": None, "ms": 4000, "group": "env", "question": "q2"}],
          open(os.path.join(res, "usefulness_one.json"), "w"))
open(os.path.join(res, "usefulness_bad.json"), "w").write("{not json")
json.dump({"not": "a list"}, open(os.path.join(res, "usefulness_dict.json"), "w"))

runs = E.tool_runs(res)
by = {r["file"]: r for r in runs}
check("only real tool-choice CSVs are read", set(by) == {"dev_a.csv", "held_a.csv", "dev_b.csv", "held_old.csv"}, set(by))
check("score is right answers over all, and grounded answers count", by["dev_a.csv"]["pct"] == 80.0 and by["dev_b.csv"]["pct"] == 100.0 and by["held_a.csv"]["good"] == 9, by)
check("misses are counted by kind", by["dev_a.csv"]["misses"] == {"wrong_tool": 1, "missed": 1})
check("categories are scored separately", by["dev_a.csv"]["categories"]["direct"]["n"] == 5 and sum(c["good"] for c in by["dev_a.csv"]["categories"].values()) == 8)
check("speed is the average of the latencies", by["dev_a.csv"]["avg_ms"] in (1004, 1005), by["dev_a.csv"]["avg_ms"])
check("runs are newest first", runs[-1]["file"] == "held_old.csv")
cmp = E.comparison(runs)
row = {(r["model"], r["variant"]): r for r in cmp}
check("one row per model and variant, with dev and held-out together", set(row) == {("m1", "gated"), ("m2", "gated_retry")} and row[("m1", "gated")]["dev"]["pct"] == 80.0 and row[("m1", "gated")]["heldout"]["pct"] == 90.0, row)
check("the newest held-out run wins over an older one", row[("m1", "gated")]["heldout"]["file"] == "held_a.csv")
check("a model with only one set has the other empty", row[("m2", "gated_retry")]["heldout"] is None)
check("rows are ordered best first (held-out score, else development)", cmp[0]["model"] == "m2", [(r["model"], r["variant"]) for r in cmp])
u = E.usefulness_runs(res)
check("usefulness audits are read, junk files skipped", len(u) == 1 and u[0]["n"] == 2 and u[0]["tool_answers"] == 1 and u[0]["avg_ms"] == 3000 and u[0]["groups"]["env"] == {"tool": 1, "n": 2}, u)
check("an empty folder is fine", E.overview(os.path.join(tmp, "none"), os.path.join(tmp, "none"))["runs"] == [])

# ---- docs
open(os.path.join(docs, "demo_script.md"), "w", encoding="utf-8").write("# Demo\n\nhello")
open(os.path.join(docs, "other.txt"), "w").write("not markdown")
open(os.path.join(tmp, "secret.md"), "w").write("outside the docs folder")
check("only .md files in docs are listed", E.doc_list(docs) == ["demo_script"])
check("a document can be read", E.read_doc("demo_script", docs) == "# Demo\n\nhello")
for bad in ["../secret", "..\\secret", "demo_script.md", "demo script", "", None, 5, "a" * 100, "/etc/passwd", "C:\\Windows\\win.ini", "demo_script\x00"]:
    check(f"read_doc refuses {bad!r}", E.read_doc(bad, docs) is None)
check("a missing document is None", E.read_doc("nope", docs) is None)

# ---- over HTTP, against the real project files
br, radio, _ = make(web_port=8115)
base = "http://127.0.0.1:8115"
d = rq.get(base + "/api/evals", timeout=10).json()
check("the real results load", len(d["runs"]) >= 10 and d["comparison"] and d["usefulness"] and "demo_script" in d["docs"], {k: len(v) if hasattr(v, "__len__") else v for k, v in d.items()})
best = d["comparison"][0]
check("the best model shown has a held-out score (the honest one)", best["heldout"] is not None and best["heldout"]["pct"] >= 90, best)
r = rq.get(base + "/api/docs", params={"name": "demo_script"}, timeout=5)
check("a document is served", r.status_code == 200 and r.json()["text"].startswith("# Demo script"))
for bad in ["../audit", "..%2f..%2faudit", "README", "setup_env", "x/y", "audit.db", ""]:
    r = rq.get(base + "/api/docs?name=" + bad, timeout=5)
    check(f"the docs route refuses {bad!r}", r.status_code == 404, r.status_code)
check("no name at all is a clean 404", rq.get(base + "/api/docs", timeout=5).status_code == 404)
check("a foreign Host is refused", rq.get(base + "/api/evals", headers={"Host": "evil.example"}, timeout=5).status_code == 403)
check("every docs page can be read through the route", all(rq.get(base + "/api/docs", params={"name": n}, timeout=5).status_code == 200 for n in d["docs"]), d["docs"])

check.done()
