"""Builds the packaged program (no Python needed to run it) for the operating system this script runs on.

    pip install -r scripts/requirements-build.txt -r requirements.txt     # PyInstaller, then the program's own libraries
    python scripts/build_binary.py                    # dist/meshllm-<version>-<os>-<arch>/  and  dist/meshllm-<version>-<os>-<arch>.tar.gz (.zip on Windows)
    python scripts/build_binary.py --print-name       # only print that name (the release workflow uses it); builds nothing
    python scripts/build_binary.py --out DIR          # put the folder and archive in DIR instead of dist/

PyInstaller cannot cross-compile: the Windows program is built on Windows, the macOS one on macOS, the Linux one on Linux (CI does all three, see
.github/workflows/package.yml). The recipe is scripts/meshllm.spec, the entry point scripts/launcher.py. This script only adds the naming, the
archive and a size report; it uses the standard library only apart from PyInstaller itself.
"""
import argparse
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # the project folder (this file is scripts/build_binary.py)
SPEC = os.path.join(ROOT, "scripts", "meshllm.spec")
OS_NAMES = {"linux": "linux", "win32": "windows", "darwin": "macos"}
ARCH_NAMES = {"x86_64": "x86_64", "amd64": "x86_64", "x64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}


def read_version(root=ROOT):
    """The version from meshllm/__init__.py, read as text so that nothing has to be installed to build."""
    with open(os.path.join(root, "meshllm", "__init__.py"), encoding="utf-8") as f:
        m = re.search(r'^__version__\s*=\s*"([^"]+)"', f.read(), re.M)
    if not m:
        raise SystemExit("meshllm/__init__.py has no __version__")
    return m.group(1)


def os_name(platform_name=None):
    """linux, windows or macos for sys.platform (SystemExit for anything this project does not package)."""
    p = sys.platform if platform_name is None else platform_name
    for key, name in OS_NAMES.items():
        if p.startswith(key):
            return name
    raise SystemExit(f"no packaged program is built for {p}")


def arch_name(machine=None):
    """x86_64 or arm64 for platform.machine() (anything else is kept, lower case)."""
    m = (platform.machine() if machine is None else machine).lower()
    return ARCH_NAMES.get(m, m)


def build_name(version=None, platform_name=None, machine=None):
    """meshllm-<version>-<os>-<arch>: the folder inside the archive and the archive's name without its extension."""
    return f"meshllm-{version or read_version()}-{os_name(platform_name)}-{arch_name(machine)}"


def archive_extension(platform_name=None):
    """.zip on Windows (what Explorer opens), .tar.gz elsewhere (keeps the executable bit)."""
    return ".zip" if os_name(platform_name) == "windows" else ".tar.gz"


def folder_size(path):
    """Total bytes of the files under `path`."""
    return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(path) for f in fs)


def _anonymous(info):
    """tarfile filter: do not record who built it (user and group ids and names) in the archive."""
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def make_archive(folder, archive):
    """Pack `folder` (keeping its name as the top-level entry) into `archive` (.zip or .tar.gz by the extension); returns the archive path."""
    top = os.path.basename(folder.rstrip("/\\"))
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for d, _, files in os.walk(folder):
                for f in sorted(files):
                    full = os.path.join(d, f)
                    z.write(full, os.path.join(top, os.path.relpath(full, folder)))
    else:
        with tarfile.open(archive, "w:gz", compresslevel=9) as t:
            t.add(folder, arcname=top, filter=_anonymous)
    return archive


def sha256(path):
    """Hex SHA-256 of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the packaged program for this operating system.")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"), help="where the folder and the archive go (default: dist/)")
    ap.add_argument("--print-name", action="store_true", help="print meshllm-<version>-<os>-<arch> and exit")
    ap.add_argument("--no-archive", action="store_true", help="build the folder only")
    opts = ap.parse_args(argv)
    name = build_name()
    if opts.print_name:
        print(name)
        return 0
    try:
        import PyInstaller      # noqa: F401
    except ImportError:
        print("PyInstaller is not installed:  pip install -r scripts/requirements-build.txt", file=sys.stderr)
        return 1
    out = os.path.abspath(opts.out)
    work = os.path.join(out, "_work")
    folder = os.path.join(out, name)
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(out, exist_ok=True)
    env = dict(os.environ, MESHLLM_BUILD_NAME=name)
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", out, "--workpath", work, SPEC]
    print("building", name, flush=True)
    code = subprocess.call(cmd, cwd=ROOT, env=env)
    if code:
        print(f"PyInstaller failed (exit code {code})", file=sys.stderr)
        return code
    exe = os.path.join(folder, "meshllm.exe" if os_name() == "windows" else "meshllm")
    if not os.path.isfile(exe):
        print(f"the build did not produce {exe}", file=sys.stderr)
        return 1
    shutil.rmtree(work, ignore_errors=True)
    print(f"folder:  {folder}  ({folder_size(folder) / 1e6:.1f} MB)")
    if not opts.no_archive:
        archive = make_archive(folder, folder + archive_extension())
        print(f"archive: {archive}  ({os.path.getsize(archive) / 1e6:.1f} MB)  sha256 {sha256(archive)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
