# Releasing

A release is a git tag such as `v0.1.0`. Pushing it makes GitHub Actions test the code, build a downloadable program for Linux, Windows and macOS, and
publish a GitHub Release with those archives, a `SHA256SUMS` file and the notes from [CHANGELOG.md](CHANGELOG.md). **Nothing is published until a tag is pushed**, and
only the maintainer should push one. The first release is `v0.1.0`.

## What the downloads are, and are not

- One archive per system, named `meshllm-<version>-<os>-<arch>`: `linux-x86_64` (`.tar.gz`), `macos-arm64` (`.tar.gz`, Apple silicon only) and `windows-x86_64` (`.zip`). Each holds a folder with a
  `meshllm` program (`meshllm.exe` on Windows) and its libraries. How to run it, and where it keeps its data: [setup.md](setup.md#where-the-data-lives-and-the-downloadable-program).
- They are built with [PyInstaller](https://pyinstaller.org) from the same code the tests run on. PyInstaller cannot cross-compile, so each one is built on its own system by the
  [package workflow](../.github/workflows/package.yml). The Linux program needs a glibc at least as new as the build machine's (Ubuntu 24.04 at the time of writing).
- **They are not signed.** Windows SmartScreen ("Windows protected your PC": More info, Run anyway) and macOS Gatekeeper (the program is from an unidentified developer; allow it in System Settings, Privacy &
  Security, or run `xattr -dr com.apple.quarantine <folder>` after you have checked the download) will warn the first time. That is the cost of not paying for certificates; the checksum and the attestation below are how you check
  the file is the one CI built.
- **Ollama is not included.** Install it separately; the program talks to it the same way the Python version does.
- **USB and Bluetooth behave as with the installer**: the program runs on your computer with your drivers and permissions.
- **How far they were tested:** every build is started in CI and must serve the dashboard in demo mode, answer `--version`, load its Bluetooth backend (`meshllm --self-check`), find its bundled
  pages and docs, write its database and backups to the data folder and nothing inside its own folder. That is a smoke test. **No packaged program (Linux, Windows or macOS) has been run against a real
  radio**; the real-hardware results in the README are for the Python installation.

## Cutting a release

1. **Pick the version** ([semver](https://semver.org): while it is `0.x`, anything may change). In one pull request:
   - set `__version__` in `meshllm/__init__.py` (the only place the number is written);
   - in [CHANGELOG.md](CHANGELOG.md) move what is under `## [Unreleased]` into a new `## [X.Y.Z] - YYYY-MM-DD` section (Added, Changed, Fixed, Removed; plain words, and say what is not tried on real hardware),
     leave an empty `## [Unreleased]` above it, and update the two link lines at the bottom of the file;
   - change the date on the new changelog heading to the day you really release (it is a guess until then);
   - the sentences that say no release exists yet ("none exists yet", "As of this page no release exists yet") are in `README.md`, `docs/setup.md`, `docs/releasing.md` and `docs/TODO.md`: update them in the same pull request, and tick the open box in the TODO item;
   - merge it when CI is green. `tests/test_release.py` fails if the version is not valid semver or the changelog has no section for it.
2. **Check what you are about to publish.** On a clean checkout of `main`: `python scripts/release_check.py check vX.Y.Z` (the workflow runs the same code) and `python scripts/release_check.py notes X.Y.Z` (the text that becomes the release notes).
   The package workflow already built and smoke-tested all three programs on the pull request.
3. **Once, before the first release: restrict who can create release tags.** Settings > Rules > Rulesets > New ruleset > New tag ruleset, target `v*`, and restrict creations, updates and deletions to the owner (no bypass list other than the owner). Without it the checks below only stop a *mistaken* tag (see [What protects a release, and what does not](#what-protects-a-release-and-what-does-not)). Optionally also create a protected `release` environment with required reviewers and turn on immutable releases (Settings > General), so a published release and its tag cannot be edited afterwards.
4. **Tag the merge commit and push the tag.** Main requires a linear history, so a release pull request is squash- or rebase-merged: the commit to tag is the one at the tip of `main` after the merge, never a commit on the pull request's branch (those commits are not on `main`, and the `verify` job refuses them). So the only workable order is: merge, pull `main`, tag, push the tag:

   ```bash
   git switch main && git pull
   git tag -a vX.Y.Z -m "Meshtastic LLM Bridge X.Y.Z"
   git push origin vX.Y.Z
   ```

5. **Watch the Actions tab.** The [release workflow](../.github/workflows/release.yml) runs these jobs, and a failure in any of them stops the release before anything is published:
   - `verify`: the tag is exactly `v` plus `meshllm.__version__`, the changelog has a section for it, and the tagged commit is an ancestor of `main` (a tag on an unmerged branch is refused); it also writes the release notes, so the job that publishes runs none of our code;
   - `test`: the whole test suite;
   - `build`: the package workflow, i.e. the program built and smoke-tested on Linux, Windows and macOS;
   - `checksums`: `SHA256SUMS` for the three archives and a signed build-provenance attestation for each (this job alone may request the signing identity);
   - `publish`: `gh release create` with the changelog section as notes and the archives plus `SHA256SUMS` attached (this job alone may write to the repository). A version with a hyphen (`1.0.0-rc.1`) is marked as a pre-release.
6. **If it fails**, fix it with a normal pull request. A tag whose release failed can be deleted (`git push origin :refs/tags/vX.Y.Z` and `git tag -d vX.Y.Z`; delete any leftover draft release with that tag on the Releases page first) and made again on the fixed commit; a published release should be
   superseded by a new version instead of rewritten.

## What protects a release, and what does not

The workflow's checks (the tag is `v` + the version, the changelog has a section, the tagged commit is on `main`, the tests pass, the programs smoke-test) protect against a **mistaken** tag: the wrong number, a forgotten
changelog, a branch that was never merged. They do **not** protect against a malicious person who can push to the repository. Anyone with write access can push a branch containing an edited `release.yml` and tag
it; a workflow run from a tag uses the workflow file *at that tag*, so the edited file decides what runs, and it can skip every check above. The real control is the tag ruleset in step 3: only the owner can create `v*` tags,
so only the owner can start a release run. Treat write access to this repository as the ability to publish, and keep it to people you would let do that.

What the workflows do on their own: they use no secret except the run's own short-lived token, the tag name reaches a shell only through an environment variable, only `checksums` can request the signing identity,
only `publish` can write to the repository, and `publish` checks nothing out and runs none of the project's code.

## Checking a download

Put the archive and `SHA256SUMS` from the release page in one folder.

```bash
sha256sum --check --ignore-missing SHA256SUMS        # Linux;  macOS: shasum -a 256 --check --ignore-missing SHA256SUMS
```

On Windows PowerShell: `(Get-FileHash .\meshllm-X.Y.Z-windows-x86_64.zip).Hash` and compare it with the line in `SHA256SUMS`.

That proves the file matches the list, which sits next to it. The **attestation** proves where the file came from: with the [GitHub CLI](https://cli.github.com) (`gh`), run

```bash
gh attestation verify meshllm-X.Y.Z-linux-x86_64.tar.gz --repo TeamHarrisCali/MeshtasticLLM \
  --signer-workflow TeamHarrisCali/MeshtasticLLM/.github/workflows/release.yml --source-ref refs/tags/vX.Y.Z
```

It succeeds only if the file was built by this repository's release workflow from that tag, and prints the commit; read that run if you want to see what was built. (Without `--signer-workflow`, any workflow of the repository,
for example the pull-request packaging one, would also pass.)

## Building one yourself

```bash
pip install -r requirements.txt -r scripts/requirements-build.txt
python scripts/build_binary.py            # dist/meshllm-<version>-<os>-<arch>/ and its archive
python scripts/smoke_binary.py dist/meshllm-<version>-<os>-<arch>
```

The recipe is [`scripts/meshllm.spec`](../scripts/meshllm.spec) (a one-folder build; the dashboard files and docs are bundled, the screenshots and `TODO.md` are not), the entry point is
`scripts/launcher.py` (the same `main()` as `python -m meshllm`, plus `--self-check`), and the build tool is pinned to a range in `scripts/requirements-build.txt`. The result is not byte-for-byte reproducible.
