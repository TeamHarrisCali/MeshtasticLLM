# -*- mode: python ; coding: utf-8 -*-
# PyInstaller recipe for the packaged program: a ONE-FOLDER build (a folder with a `meshllm` program and its libraries beside it).
# Run it with scripts/build_binary.py, which names the folder meshllm-<version>-<os>-<arch>, runs this and makes the archive.
#
# Why one folder and not one file: a one-file program unpacks itself into a temporary folder at every start (slow, and some virus
# scanners dislike it); a folder starts at once, can be unpacked anywhere, and its files can be inspected.
#
# This file is Python that PyInstaller runs; SPECPATH is the folder of this file (scripts/), and Analysis, PYZ, EXE and COLLECT are
# provided by PyInstaller.
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent                       # the project folder
NAME = os.environ.get("MESHLLM_BUILD_NAME", "meshllm")      # the folder's name (build_binary.py sets it to meshllm-<version>-<os>-<arch>)

# What the program reads while it runs (read-only; the data it WRITES goes to the user's data folder, see meshllm/paths.py).
# Same selection as the Docker image: no screenshots (the README's pictures) and no TODO.md (the developers' hand-off queue).
datas = [(str(ROOT / "meshllm" / "static"), "meshllm/static")]
for p in sorted((ROOT / "docs").rglob("*")):
    rel = p.relative_to(ROOT / "docs")
    if p.is_file() and rel.parts[0] != "screenshots" and rel.as_posix() != "TODO.md":
        datas.append((str(p), str(Path("docs") / rel.parent)))

# Imports PyInstaller's scan cannot see: the radio libraries load their parts by name, and the Bluetooth backend is picked by
# operating system at run time. `meshllm --self-check` (scripts/launcher.py) proves they are there.
hidden = collect_submodules("meshtastic") + collect_submodules("pubsub") + [
    "serial", "serial.tools.list_ports", "serial.tools.list_ports_common",
]
if sys.platform.startswith("linux"):
    hidden += collect_submodules("bleak.backends.bluezdbus") + collect_submodules("dbus_fast")
elif sys.platform == "win32":
    hidden += collect_submodules("bleak.backends.winrt") + collect_submodules("winrt")
    hidden += ["serial.tools.list_ports_windows"]
elif sys.platform == "darwin":
    hidden += collect_submodules("bleak.backends.corebluetooth") + ["objc", "Foundation", "CoreBluetooth", "libdispatch"]
    hidden += ["serial.tools.list_ports_osx"]
if sys.platform.startswith("linux"):
    hidden += ["serial.tools.list_ports_linux", "serial.tools.list_ports_posix"]

# Things the program never uses that PyInstaller would otherwise pull in through optional imports (smaller download).
excludes = ["tkinter", "matplotlib", "numpy", "pandas", "pytest", "IPython"]

a = Analysis(
    [str(ROOT / "scripts" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,       # the one-folder layout: libraries stay beside the program instead of inside it
    name="meshllm",
    debug=False,
    strip=False,
    upx=False,                   # UPX-packed programs are flagged by many virus scanners
    console=True,                # a command-line program: it prints the dashboard address and logs to the terminal
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=NAME)
