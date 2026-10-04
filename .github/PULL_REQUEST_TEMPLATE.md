## What this changes

<!-- One or two sentences: what and why. -->

## Review

<!-- Who reviewed this and what they found. For an agent-authored change: the independent reviewer's summary, and what was done about each finding. -->

## Checklist

- [ ] One topic on this branch (feature/fix/docs), branched from an up-to-date `main`
- [ ] `python scripts/run_tests.py` passes and CI is green
- [ ] New behaviour has tests (the radio and Ollama are faked in `tests/`); a bug fix has a test that fails without the fix
- [ ] Reviewed independently of the author; findings addressed or answered above
- [ ] Docs updated if behaviour changed (`docs/`, and `README.md` if it is user-facing)
- [ ] No personal data: node IDs, node names, callsigns, coordinates, usernames or paths from a real mesh
- [ ] If this touches what the AI may do, it is still read-only, still a fixed menu, and still never transmits on its own
