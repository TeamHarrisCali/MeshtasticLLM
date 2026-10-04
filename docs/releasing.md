# Releasing

A release is a git tag such as `v0.1.0`. Pushing it makes GitHub Actions test the code, build a downloadable program for Linux, Windows and macOS, and
publish a GitHub Release with those archives, a `SHA256SUMS` file and the notes from [CHANGELOG.md](CHANGELOG.md). **Nothing is published until a tag is pushed**, and
only the maintainer should push one. As of this page no release exists yet.

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
   - merge it when CI is green. `tests/test_release.py` fails if the version is not valid semver or the changelog has no section for it.
2. **Check what you are about to publish.** On a clean checkout of `main`: `python scripts/release_check.py check vX.Y.Z` (the workflow runs the same code) and `python scripts/release_check.py notes X.Y.Z` (the text that becomes the release notes).
   The package workflow already built and smoke-tested all three programs on the pull request.
3. **Tag the merge commit and push the tag**:

   ```bash
   git switch main && git pull
   git tag -a vX.Y.Z -m "Meshtastic LLM Bridge X.Y.Z"
   git push origin vX.Y.Z
   ```

4. **Watch the Actions tab.** The [release workflow](../.github/workflows/release.yml) runs these jobs, and a failure in any of them stops the release before anything is published:
   - `verify`: the tag is exactly `v` plus `meshllm.__version__`, the changelog has a section for it, and the tagged commit is an ancestor of `main` (a tag on an unmerged branch is refused);
   - `test`: the whole test suite;
   - `build`: the package workflow, i.e. the program built and smoke-tested on Linux, Windows and macOS;
   - `checksums`: `SHA256SUMS` for the three archives and a signed build-provenance attestation for each (this job alone may request the signing identity);
   - `publish`: `gh release create` with the changelog section as notes and the archives plus `SHA256SUMS` attached (this job alone may write to the repository). A version with a hyphen (`1.0.0-rc.1`) is marked as a pre-release.
5. **If it fails**, fix it with a normal pull request. A tag whose release failed can be deleted (`git push origin :refs/tags/vX.Y.Z` and `git tag -d vX.Y.Z`) and made again on the fixed commit; a published release should be
   superseded by a new version instead of rewritten.

The workflows use no secret except the run's own short-lived token, and the tag name reaches a shell only through an environment variable.

## Checking a download

Put the archive and `SHA256SUMS` from the release page in one folder.

```bash
sha256sum --check --ignore-missing SHA256SUMS        # Linux;  macOS: shasum -a 256 --check --ignore-missing SHA256SUMS
```

On Windows PowerShell: `(Get-FileHash .\meshllm-X.Y.Z-windows-x86_64.zip).Hash` and compare it with the line in `SHA256SUMS`.

That proves the file matches the list, which sits next to it. The **attestation** proves where the file came from: with the [GitHub CLI](https://cli.github.com) (`gh`), run

```bash
gh attestation verify meshllm-X.Y.Z-linux-x86_64.tar.gz --repo TeamHarrisCali/MeshtasticLLM
```

It succeeds only if the file was built by a workflow of this repository, and prints which one and the commit; read that run if you want to see what was built.

## Building one yourself

```bash
pip install -r requirements.txt -r scripts/requirements-build.txt
python scripts/build_binary.py            # dist/meshllm-<version>-<os>-<arch>/ and its archive
python scripts/smoke_binary.py dist/meshllm-<version>-<os>-<arch>
```

The recipe is [`scripts/meshllm.spec`](../scripts/meshllm.spec) (a one-folder build; the dashboard files and docs are bundled, the screenshots and `TODO.md` are not), the entry point is
`scripts/launcher.py` (the same `main()` as `python -m meshllm`, plus `--self-check`), and the build tool is pinned to a range in `scripts/requirements-build.txt`. The result is not byte-for-byte reproducible.
