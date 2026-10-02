# Contributing

Thanks for helping. Bug reports, hardware reports (radio model, OS, what happened) and pull requests are all welcome.

## Set up

```bash
./setup.sh            # Linux / macOS  (Windows: setup.bat)
python run_tests.py   # about a minute; no radio or Ollama needed, both are faked
```

`python run_tests.py channel backup` runs only the test files whose names contain those words. `-v` prints more detail for failures.

## Ground rules

- **Keep the AI read-only and boxed in.** Tools are entries in `ACTIONS` in `actions.py`: a name, a validated parameter
  schema and a hand-written handler. Do not add a tool that transmits, changes the radio, runs commands or reads files.
  Anything that would change something belongs behind the tier-1 confirmation code. See `docs/ai_tools.md`.
- **Treat everything from the mesh as untrusted**, including node names.
- **Add a test.** Tests are ordinary scripts in `tests/` that print `PASS` / `FAIL` lines (see `tests/fixture.py`). They fake the
  radio and Ollama and use a temporary folder, so they never touch a real `audit.db`.
- **Match the surrounding code.** The modules are flat, each with a docstring saying what it is for. The dashboard is plain
  HTML, CSS and JavaScript in `static/`, with no build step.
- **Update the docs** in `docs/` (and the README if it is user-facing) when behaviour changes.
- **Do not measure against the held-out set to tune a prompt.** `eval_tools.py` keeps a development set and a held-out set
  apart on purpose.

## Keep real mesh data out of the repository

Issues, pull requests and committed files are public. Do not include node IDs, node names, callsigns, coordinates, message text,
usernames or file paths from a real mesh or computer. Test fixtures use obvious fakes (`!1a2b3c4d`, `!0000aaaa`, "Hilltop Base").
`audit.db`, `logs/`, `backups/` and `tile_cache/` are git-ignored for this reason.

## Pull requests

Keep them focused, say what changed and why, and make sure `python run_tests.py` passes. CI runs the same command.
