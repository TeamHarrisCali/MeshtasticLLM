"""The entry point of the packaged program: the same as `python -m meshllm`, so every flag works (`meshllm --demo`, `meshllm --ble ADDRESS`, ...).

PyInstaller needs a plain script to start from, and meshllm/__main__.py is a package entry point, so this is the tiny script that calls
the same `main()`. One extra, only here: `meshllm --self-check` loads every part a packaged program can silently lack (the Bluetooth
backend of this operating system, the USB serial and Wi-Fi interfaces, the dashboard's files, the docs) and exits 0 when they are all
there. The packaging smoke tests run it, because a missing Bluetooth backend would otherwise only show up when someone connects a radio.
It never opens a radio, a port or a database.
"""
import importlib
import sys

BLE_BACKENDS = {"linux": "bluezdbus", "win32": "winrt", "darwin": "corebluetooth"}     # bleak's backend package for each operating system


def self_check():
    """Print one line per part and return the exit code (0 = everything this program ships with was found and imports)."""
    from meshllm import __version__, paths
    problems = []

    def step(name, fn):
        try:
            detail = fn()
            print(f"ok    {name}" + (f": {detail}" if detail else ""))
        except Exception as e:      # the point is to report every missing part, not to stop at the first
            problems.append(name)
            print(f"FAIL  {name}: {type(e).__name__}: {str(e)[:160]}")

    def bluetooth():
        """The library's own check, then the Bluetooth backend it will pick on this operating system (the part PyInstaller cannot see by itself)."""
        from meshllm import connection
        connection.load_ble()
        name = BLE_BACKENDS.get("linux" if sys.platform.startswith("linux") else sys.platform)
        if name is None:
            raise RuntimeError(f"no Bluetooth backend known for {sys.platform}")
        importlib.import_module(f"bleak.backends.{name}.client")
        importlib.import_module(f"bleak.backends.{name}.scanner")
        return f"bleak backend {name}"

    def files():
        root = paths.resource_root()
        need = [root / "meshllm" / "static" / "index.html", root / "meshllm" / "static" / "js" / "order.txt", root / "docs" / "setup.md",
                root / "docs" / "eval_results"]
        gone = [str(p.relative_to(root)) for p in need if not p.exists()]
        if gone:
            raise FileNotFoundError("missing from the bundle: " + ", ".join(gone))
        return "dashboard files and docs found"

    def usb_and_tcp():
        importlib.import_module("meshtastic.serial_interface")
        importlib.import_module("meshtastic.tcp_interface")
        importlib.import_module("serial.tools.list_ports")
        return "serial and tcp interfaces import"

    print(f"meshllm {__version__} ({'packaged' if paths.is_frozen() else 'from source'}), Python {sys.version.split()[0]}, {sys.platform}")
    step("bundled files", files)
    step("USB and Wi-Fi", usb_and_tcp)
    step("Bluetooth", bluetooth)
    print(f"data folder (not created here): {paths.data_dir()}")
    return 1 if problems else 0


def keep_printing():
    """A Windows console (or a pipe) may not be able to show every character in a node name or a log line; show a ? instead of stopping."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


if __name__ == "__main__":
    keep_printing()
    if sys.argv[1:] == ["--self-check"]:
        sys.exit(self_check())
    from meshllm.bridge import main
    main()
