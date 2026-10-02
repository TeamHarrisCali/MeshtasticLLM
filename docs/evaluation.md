# Evaluating the model

Runs a fixed set of prompts (indirect, plain chat, out-of-scope, questions about the computer, prompt-injection, mesh lookups) through
Ollama with the same system prompt and tool list the bridge uses, and scores each result as
correct / missed / wrong tool / hallucinated tool / false positive. Writes `eval_results.csv`.

    python eval_tools.py --model llama3.2:3b --model qwen2.5:7b --runs 3

Two prompt sets: `dev` (used while tuning) and `heldout` (fresh prompts written beforehand and never
used for tuning; `--set heldout`).

> **The tool menu changed.** The AI used to have five checks of the computer it runs on (disk space, uptime, CPU/RAM, Ollama,
> bridge status) as well as the mesh lookups. Those were removed; it now only looks things up about the mesh, and the
> questions about the computer in both sets became "should call no tool" cases. The CSVs in `eval_results/` and the table
> below were measured on the earlier menu. Re-run `eval_tools.py` (both sets) on your model before quoting any number for the current one.

Results on llama3.2:3b with the earlier menu, 3 runs per prompt (CSVs in `eval_results/`):

| Configuration | dev | held-out | Main weakness |
|---|---|---|---|
| Original prompt | 73% | 70% | ~15 false positives per set: calls a tool on chit-chat |
| temperature 0 only | 70% | - | deterministic *wrong* calls (no-tool 0/12) |
| stricter prompt | 80% | - | starts missing real requests |
| strict prompt + temp 0 | 80% | - | still 12 false positives |
| **+ YES/NO gate (current default)** | **90%** | **85%** | misses some indirect and "check, then delete" requests |

The gate is a one-word pre-check (no tools, temperature 0) that decides whether a message is about the mesh
at all; tools are only offered on YES (`--no-tool-gate` turns it off). It costs one extra short model call
for verified nodes. Known limits: the gate sees only the current message, so a follow-up like "and the one
before that?" is treated as ordinary chat, and requests that also ask to delete something are declined
(arguably the safe behaviour). A single run is noisy - the first single-run test said 90% for the original
prompt. Everything here is one model, small prompt sets and temperature sampling, so treat it as indicative.

Wrong calls are bounded by design: the menu has only read-only entries plus a confirmation-gated demo.
