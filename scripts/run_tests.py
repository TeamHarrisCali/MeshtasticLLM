"""Runs every test in tests/ and prints one line per file and a total.

    python scripts/run_tests.py                 # all of them (about a minute; no radio or Ollama needed, both are faked)
    python scripts/run_tests.py channel setup   # only the files whose names contain one of these words
    python scripts/run_tests.py -v              # also print every failing check's detail

Each test file is an ordinary script that prints PASS / FAIL lines and exits non-zero on any failure. They use a
throwaway temp folder for databases and caches, so your real audit.db, tile_cache/ and logs/ are never touched.
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))      # the project folder (this file is scripts/run_tests.py)


def main(argv):
    """Run the selected test files one by one and print a summary; returns the process exit code (1 if anything failed)."""
    verbose = "-v" in argv
    words = [a for a in argv if not a.startswith("-")]
    files = sorted(glob.glob(os.path.join(ROOT, "tests", "test_*.py")))
    if words:
        files = [f for f in files if any(w in os.path.basename(f) for w in words)]
    if not files:
        print("No matching tests.")
        return 1
    # Point every temp-dir variable (Windows uses TEMP/TMP, POSIX uses TMPDIR) at a throwaway folder so the tests
    # never write into the real data folders; PYTHONIOENCODING keeps non-ASCII output from crashing on Windows consoles.
    scratch = tempfile.mkdtemp(prefix="meshtests_")
    env = dict(os.environ, TEMP=scratch, TMP=scratch, TMPDIR=scratch, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    total_pass = total_fail = 0
    broken = []
    t0 = time.time()
    try:
        for f in files:
            name = os.path.basename(f)[5:-3]
            t1 = time.time()
            try:
                p = subprocess.run([sys.executable, f], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900)
                out = p.stdout.decode("utf-8", "replace")
                code = p.returncode
            except subprocess.TimeoutExpired as e:
                out, code = (e.stdout or b"").decode("utf-8", "replace") + "\nFAIL timed out after 15 minutes", 1
            lines = out.splitlines()
            ok = sum(1 for l in lines if l.startswith("PASS "))
            bad = [l for l in lines if l.startswith("FAIL ")]
            total_pass += ok
            total_fail += len(bad)
            crashed = code != 0 and not bad   # non-zero exit without any FAIL line: the script died before reporting
            status = "ok  " if code == 0 and not bad else "FAIL"
            print(f"{status} {name:<16} {ok:>4} passed{'' if not bad else f', {len(bad)} failed'}{'  (crashed)' if crashed else ''}   {time.time() - t1:5.1f}s")
            if bad or crashed:
                broken.append(name)
                for l in (bad if not crashed else lines[-12:]):
                    print("       " + l[:240 if verbose else 160])
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    print(f"\n{total_pass} passed, {total_fail} failed in {time.time() - t0:.0f}s" + (f"   (check: {', '.join(broken)})" if broken else ""))
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
