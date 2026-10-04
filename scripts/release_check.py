"""The release workflow's checks, as a script so that the tests run the same code the workflow does. Standard library only.

    python scripts/release_check.py check v0.1.0     # the tag is `v` + __version__, the version is valid semver, CHANGELOG.md has a section for it
    python scripts/release_check.py notes 0.1.0      # print that CHANGELOG section (the release notes)
    python scripts/release_check.py version          # print __version__

Exit code 0 means the check passed; a problem is printed to stderr and the exit code is 1. See docs/releasing.md.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # the project folder (this file is scripts/release_check.py)
# The official semantic-versioning pattern (semver.org), so "0.1", "v0.1.0" and "01.2.3" are refused.
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$")


def read_version(root=ROOT):
    """__version__ from meshllm/__init__.py, read as text (the package is not imported); None if it is not there."""
    with open(os.path.join(root, "meshllm", "__init__.py"), encoding="utf-8") as f:
        m = re.search(r'^__version__\s*=\s*"([^"]+)"\s*$', f.read(), re.M)
    return m.group(1) if m else None


def is_semver(version):
    """True for a valid semantic version such as 0.1.0 or 1.2.3-rc.1."""
    return bool(version) and bool(SEMVER.match(version))


def tag_for(version):
    """The git tag for a version: `v` and the version."""
    return "v" + version


def tag_matches(tag, version):
    """True when `tag` is exactly `v` + `version` (nothing else is accepted: no refs/tags/ prefix, no missing v)."""
    return tag == tag_for(version)


def changelog_section(text, version):
    """The body of the `## [version]` section of a Keep-a-Changelog file (up to the next `## [` heading or the link references at the end),
    stripped; None if there is no such section or it is empty."""
    heading = re.compile(r"^##\s+\[" + re.escape(version) + r"\](?:\s+-\s+\S.*)?\s*$", re.M)
    m = heading.search(text)
    if not m:
        return None
    rest = text[m.end():]
    end = re.search(r"^## \[|^\[[^\]]+\]:\s*\S", rest, re.M)
    body = (rest[:end.start()] if end else rest).strip()
    return body or None


def read_changelog(root=ROOT):
    with open(os.path.join(root, "docs", "CHANGELOG.md"), encoding="utf-8") as f:
        return f.read()


def check_tag(tag, root=ROOT):
    """A list of problems with releasing `tag` from this checkout (empty = fine)."""
    version = read_version(root)
    if version is None:
        return ["meshllm/__init__.py has no __version__"]
    problems = []
    if not is_semver(version):
        problems.append(f"__version__ {version!r} is not a valid semantic version (X.Y.Z)")
    if not tag_matches(tag, version):
        problems.append(f"the tag {tag!r} does not match the version in meshllm/__init__.py (expected {tag_for(version)!r})")
    if changelog_section(read_changelog(root), version) is None:
        problems.append(f"docs/CHANGELOG.md has no non-empty '## [{version}]' section")
    return problems


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 2 and argv[0] == "check":
        problems = check_tag(argv[1])
        for p in problems:
            print("error: " + p, file=sys.stderr)
        if not problems:
            print(f"ok: {argv[1]} matches version {read_version()} and has a CHANGELOG section")
        return 1 if problems else 0
    if len(argv) == 2 and argv[0] == "notes":
        body = changelog_section(read_changelog(), argv[1])
        if body is None:
            print(f"error: docs/CHANGELOG.md has no non-empty '## [{argv[1]}]' section", file=sys.stderr)
            return 1
        print(body)
        return 0
    if argv == ["version"]:
        print(read_version())
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
