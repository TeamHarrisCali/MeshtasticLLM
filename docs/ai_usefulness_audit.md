# How useful is the AI today? An audit (2026-09-30)

> Historical record. It was run when the AI also had checks of the computer it runs on (PC load, Ollama, disk); those tools
> were later removed, and the audit script now expects an honest "I can't check that" for such questions. The mesh findings stand.

**Method.** 25 realistic questions (mesh, environment, history, PC, questions about itself, jobsite maths) were sent through the
live bridge with the browser chat, which uses exactly the radio pipeline (queue, YES/NO gate, `qwen3.5:latest`, read-only
tools). Memory was cleared before each one. Every answer was then checked against what the system actually knows
or against the maths. Script: `usefulness_audit.py`; raw answers: `eval_results/usefulness_baseline_2026-09-30.json`.
A second look at the 25 real questions already in the audit log (mostly tests from the two radios) agreed with it.

## Result

| | count | share |
|---|---|---|
| Correct and grounded in real data | 8 | 32% |
| Presented as fact but **invented or wrong** | 10 | 40% |
| Honest but **no help** ("check your phone", prompt echo) | 7 | 28% |

The 8 correct answers are all tool answers (disk, PC load, Ollama, bridge, mesh summary, node lookups, farthest node) plus a
unit conversion the model happens to know. **Every tool answer was right. Every wrong answer was the model speaking without a tool.**

## What went wrong, question by question

| Question | What the AI said | Truth / why |
|---|---|---|
| is it humid out there? | "Humidity readings show 82% right now" | Invented. The sensors read about 39%. |
| which nodes report temperature? | "Nodes 2, 5 and 8 ... IDs ending in TMP" | Invented. Two real sensor nodes exist. |
| what's the temperature outside? | "I have no sensor access" | False: two sensor nodes report; average about 91 F. No tool exposes them. |
| what's the weather like near us? | "I have no access to location data" | Same: the mesh sensors could answer. |
| who has been quiet for a while? | "No one else is in this chat yet" | Invented; 65 nodes were heard in 24 h. |
| what's the busiest time on the mesh? | "09:00-17:00, solar harvest spikes at noon" | Invented. The data exists (Trends page) but no tool reads it. |
| what happened on the mesh in the last hour? | "No activity logged ... a heartbeat ping" | Invented. 40+ packets and several new nodes. |
| has the battery on Trail Mobile been dropping today? | current value only | Answered a different question: no history tool. |
| which node has the best signal to us? | list of the *nearest* nodes | Wrong tool: link-quality data exists but no tool offers it. |
| where is the nearest router? | "I have no access to maps" | `list_nodes` has no role filter. |
| are you there? | "Signal is weak but steady. Battery low" | Invented status about itself. |
| what can you do? | recited its own system prompt | Leaks the prompt; no real capability list. |
| help | "What's broken? Tell me specifically" | `/ai help` isn't a command. |
| what time is it? / today's date? | "I don't know" | Honest, but the PC knows. |
| how many gallons per foot in 2 inch pipe? | "0.31 gal/ft" | Wrong: 2 in sch-40 is about 0.17 gal/ft. |
| travel on a 45 degree offset with 12 in of set | "8.49 inches (sine 45 x 12)" | **Wrong: travel = set / sin(45) = 16.97 in.** A dangerous error on a jobsite. |

Correct: how many nodes reachable directly, is Downtown Router online, when Tracker Node was last heard, farthest node, how is the PC,
is Ollama running, what model are you (lucky), 3/4 in to mm (19.05, lucky).

## Why it happens (three separate causes)

1. **Data we already hold has no tool.** Sensors, link quality, hop counts, who has gone quiet, busiest times, traceroutes, trends,
   role filters: the database has them, the AI cannot reach them, so it guesses. (Of the 8 datasets, only the node list and a handful of headline numbers reach the AI.)
2. **When no tool fires, the model is free to state "facts".** The safety net added today only covers questions the YES/NO gate already
   classed as live state. Questions about itself, the time, or arithmetic go straight to free text.
3. **Language models are unreliable at arithmetic and look-ups.** Two of the three wrong jobsite answers were confident and wrong.

## What would make it more useful (ordered by value for effort)

1. **Ground every answer with a tiny live context.** Put a few hundred characters of real facts in the system prompt each turn: date
   and time, our radio's name, how many nodes were heard, the average temperature and humidity, the model name. "Are you there?",
   "what time is it?", "is it humid?" become correct with no tool call and no extra latency. Only numbers and our own names go in (no
   remote node names, which strangers choose).
2. **Tools for the data we already have:** `sensor_readings` (who reports what, averages), `link_quality` (best and worst links),
   `quiet_nodes`, `busiest_times`, `node_history` (battery/temperature trend), `recent_activity` (what changed in the last hour),
   a role filter on `list_nodes`, `current_time`.
3. **A jobsite calculator of exact functions** (unit conversion, offset travel and run, gallons per foot by pipe size, round and
   rectangular duct areas, gauge thicknesses). The model only chooses the function and fills in the numbers; the answer is computed,
   so it cannot be wrong. Skipped on purpose for now.
4. **Commands that need no model:** `/ai help` (what I can do), `/ai ping` (reply with the SNR, RSSI and hops of the packet you just
   sent: a range test for free), `/ai status`. Instant and always correct.
5. **Stop free-text facts:** widen the gate into a three-way router (live state / calculation / general chat), and for the first two
   require a tool or say what can be checked. Re-run this audit and `eval_tools.py` after each change.
6. **Speed:** median answer was about 5.5 s (gate about 2.4 s plus tool call about 3 s; up to 14 s). Keep the model loaded, try a
   smaller gate model, and skip the gate for obvious requests.
7. **Web chat only:** hand the tool result back to the model for a friendly summary. Not for the radio, where raw results are
   shorter and safer (node names are chosen by strangers).

**Target:** from 8/25 useful and 10/25 invented to at least 21/25 useful and none invented, measured by re-running `usefulness_audit.py`.
