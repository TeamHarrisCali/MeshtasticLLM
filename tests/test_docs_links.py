"""Markdown links: every relative link and #anchor in README.md, docs/*.md and .github/*.md points at a file or heading that exists.

The documentation moves around (the README links into docs/, the pages link to each other and to .github/CONTRIBUTING.md), so this keeps a
rename or a reshuffle from leaving a dead link behind. Offline: web addresses (http, https, mailto) are not fetched. Anchors follow GitHub's
rules: the heading is lower-cased, anything but letters, digits, spaces, hyphens and underscores is dropped, spaces become hyphens, and a
repeated heading gets -1, -2, ... appended. Links and headings inside code blocks and code spans are ignored, as GitHub does."""
import glob, os, re, sys
from urllib.parse import unquote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)

FENCE = re.compile(r"^\s*(```|~~~)")
LINK = re.compile(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*(<[^>]*>|[^)\s]*)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
REF_DEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(\S+)")
HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
SPAN = re.compile(r"(`+)(.+?)\1")


def prose_lines(text):
    """(line number, line) for every line outside a fenced code block and an indented one."""
    inside = False
    for n, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            inside = not inside
            continue
        if not inside and not line.startswith("    "):
            yield n, line


def slug(heading):
    """GitHub's anchor for a heading's text (inline Markdown stripped first)."""
    t = SPAN.sub(lambda m: m.group(2), heading)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)            # [text](url) -> text
    t = re.sub(r"<[^>]+>", "", t)                               # inline html
    t = t.replace("*", "").replace("~", "")                     # emphasis markers
    t = t.strip().lower()
    t = re.sub(r"[^\w\s-]", "", t)                              # letters, digits, _, space and - survive
    return t.replace(" ", "-")


_anchor_cache = {}
def anchors(path):
    """The set of heading anchors a Markdown file offers."""
    if path not in _anchor_cache:
        seen, out = {}, set()
        with open(path, encoding="utf-8") as f:
            for _, line in prose_lines(f.read()):
                m = HEADING.match(line)
                if m:
                    s = slug(m.group(2))
                    k = seen.get(s, 0)
                    seen[s] = k + 1
                    out.add(s if k == 0 else f"{s}-{k}")
        _anchor_cache[path] = out
    return _anchor_cache[path]


def links(path):
    """(line number, target) for every relative link or image in a Markdown file (web and mail addresses left out)."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    found = []
    for n, line in prose_lines(text):
        line = SPAN.sub(lambda m: " " * len(m.group(0)), line)          # a link inside a code span is just text
        targets = [m.group(1) for m in LINK.finditer(line)]
        m = REF_DEF.match(line)
        if m:
            targets.append(m.group(1))
        for t in targets:
            t = t.strip("<>")
            if t and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", t) and not t.startswith("//"):      # not http:, https:, mailto:, ...
                found.append((n, t))
    return found


def broken(path):
    """The problems in one file: a list of 'line N: target (what is wrong)'."""
    bad = []
    here = os.path.dirname(path)
    for n, t in links(path):
        target, _, frag = t.partition("#")
        dest = path if target == "" else os.path.normpath(os.path.join(here, unquote(target.split("?")[0])))
        if target and not os.path.exists(dest):
            bad.append(f"line {n}: {t} (no such file)")
        elif frag and os.path.isfile(dest) and dest.lower().endswith(".md") and unquote(frag).lower() not in anchors(dest):
            bad.append(f"line {n}: {t} (no heading with that anchor in {os.path.relpath(dest, ROOT)})")
        elif frag and os.path.isdir(dest):
            bad.append(f"line {n}: {t} (an anchor on a folder)")
    return bad


# ---- the checker itself, on a small made-up tree (so a regression in it cannot hide a broken link)
import tempfile, shutil
tmp = tempfile.mkdtemp(prefix="doclinks_")
try:
    os.makedirs(os.path.join(tmp, "sub"))
    open(os.path.join(tmp, "a.md"), "w", encoding="utf-8").write(
        "# Title\n\n## Use it (LAN login)\n\n## What can't it do?\n\n## Same\n\n## Same\n\n## `code` in *a* heading\n\n"
        "[ok](#title) [ok2](#use-it-lan-login) [ok3](#what-cant-it-do) [ok4](#same-1) [ok5](sub/b.md#deep) [ok6](sub/) [ok7](https://example.invalid/x#y)\n"
        "[bad1](#nope) [bad2](missing.md) [bad3](sub/b.md#nothing) ![img](sub/none.png) [ok8](#code-in-a-heading)\n"
        "`[in code](missing.md)`\n\n```\n[fenced](missing.md)\n# not a heading\n```\n")
    open(os.path.join(tmp, "sub", "b.md"), "w", encoding="utf-8").write("# Deep\n")
    got = broken(os.path.join(tmp, "a.md"))
    check("the checker finds the four broken links and nothing else", len(got) == 4 and all(any(w in g for g in got) for w in ("#nope", "missing.md", "sub/b.md#nothing", "none.png")), got)
    check("GitHub-style anchors: punctuation dropped, spaces to hyphens, repeats numbered", {"use-it-lan-login", "what-cant-it-do", "same", "same-1", "code-in-a-heading"} <= anchors(os.path.join(tmp, "a.md")))
    check("code blocks and code spans are not read as links or headings", "not-a-heading" not in anchors(os.path.join(tmp, "a.md")) and not any("fenced" in g or "in code" in g for g in got))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---- the real documentation
files = [os.path.join(ROOT, "README.md")] + sorted(glob.glob(os.path.join(ROOT, "docs", "*.md"))) + sorted(glob.glob(os.path.join(ROOT, ".github", "*.md")))
check("the files to check are there (README, docs and .github pages)", len(files) >= 12 and all(os.path.isfile(f) for f in files), files)
total = 0
for f in files:
    total += len(links(f))
    problems = broken(f)
    check(f"{os.path.relpath(f, ROOT).replace(os.sep, '/')}: every relative link and anchor resolves", not problems, "; ".join(problems))
check("a fair number of links were actually checked", total >= 40, total)

print(f"\n{len(fails)} failed" if fails else "\nall passed")
sys.exit(1 if fails else 0)
