# Contributing

Thanks for helping. Bug reports, hardware reports (radio model, OS, what happened) and pull requests are all welcome.

## Set up

```bash
./setup.sh            # Linux / macOS  (Windows: setup.bat)
python run_tests.py   # about a minute; no radio or Ollama needed, both are faked
python -m meshllm     # run the bridge in the foreground
```

`python run_tests.py channel backup` runs only the test files whose names contain those words. `-v` prints more detail for failures.

## How changes are made

From now on, work happens on branches and lands through pull requests rather than direct commits to `main` (the early history predates this; it is a
convention, not a GitHub-enforced rule).

1. **Branch** from an up-to-date `main`, one branch per task: `feat/<topic>`, `fix/<topic>` or `docs/<topic>`. When several people or agents work at
   once, each takes their own branch and their own topic, like team members on a small team.
2. **Commit** in small steps with messages that say *why*. Run `python run_tests.py` first. New behaviour needs tests, and a bug fix needs a test that fails without the fix.
3. **Publish the branch and open a pull request** using the template. Say what changed, why, and how it was checked.
4. **Get an independent review.** A reviewer who did not write the change (another contributor, or a separate reviewer agent working from the diff alone)
   reads it and records findings on the PR. A reviewer agent is the same model family as the author, so it is a second pair of eyes, not a guarantee.
   Fix what they find on the same branch.
5. **Merge** once CI is green and the review is clean (squash merge keeps `main` readable), then delete the branch.

AI agents follow the same steps. Agent-authored commits carry a `Co-Authored-By` trailer and pull requests a "Generated with Claude Code" footer (the first
commits predate that habit).

## Ground rules

- **Keep the AI read-only and boxed in.** Tools are entries in `ACTIONS` in `actions.py`: a name, a validated parameter
  schema and a hand-written handler. Do not add a tool that transmits, changes the radio, runs commands or reads files.
  Anything that would change something belongs behind the tier-1 confirmation code. See `docs/ai_tools.md`.
- **Treat everything from the mesh as untrusted**, including node names.
- **Add a test.** Tests are ordinary scripts in `tests/` that print `PASS` / `FAIL` lines (see `tests/fixture.py`). They fake the
  radio and Ollama and use a temporary folder, so they never touch a real `audit.db`.
- **Match the surrounding code.** The Python lives in the `meshllm/` package, one module per concern, each with a docstring saying
  what it is for ([docs/files.md](docs/files.md) is the map). The dashboard is plain HTML, CSS and JavaScript in `meshllm/static/`
  with no build step; the script files are joined in the order listed in `meshllm/static/js/order.txt`.
- **Document as you go.** Give new functions and classes a docstring, and comment the *why* of anything non-obvious.
- **Update the docs** in `docs/` (and the README if it is user-facing) when behaviour changes.
- **Do not measure against the held-out set to tune a prompt.** `eval_tools.py` keeps a development set and a held-out set
  apart on purpose.

## Keep real mesh data out of the repository

Issues, pull requests and committed files are public. Do not include node IDs, node names, callsigns, coordinates, message text,
usernames or file paths from a real mesh or computer. Test fixtures use obvious fakes (`!1a2b3c4d`, `!0000aaaa`, "Hilltop Base").
`audit.db`, `logs/`, `backups/` and `tile_cache/` are git-ignored for this reason.

## Pull requests

Keep them focused, say what changed and why, and make sure `python run_tests.py` passes. CI runs the same command.
