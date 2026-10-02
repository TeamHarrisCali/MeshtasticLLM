## What this changes

<!-- One or two sentences: what and why. -->

## Review

<!-- Who reviewed this and what they found. For an agent-authored change: the independent reviewer's summary, and what was done about each finding. -->

## Checklist

- [ ] One topic on this branch (feature/fix/docs), branched from an up-to-date `main`
- [ ] Reviewed independently of the author; findings addressed or answered above

- [ ] `python run_tests.py` passes
- [ ] New behaviour has a test (the radio and Ollama are faked in `tests/`)
- [ ] Docs updated if behaviour changed (`docs/`, and `README.md` if it is user-facing)
- [ ] No personal data: node IDs, node names, callsigns, coordinates, usernames or paths from a real mesh
- [ ] If this touches what the AI may do, it is still read-only, still a fixed menu, and still never transmits on its own
