"""setup_env.py: OS detection, environment state, line-ending repair, the --check run."""
import io, os, shutil, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import setup_env as S
from meshllm import actions

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)

CR, LF = bytes([13]), bytes([10])

# ---- OS detection (inputs supplied, so every OS is testable from here)
ubuntu = 'NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 24.04.3 LTS"\nID=ubuntu\nID_LIKE=debian\n'
i = S.detect_os("Linux", "6.6.0", "x86_64", ubuntu, "Linux version 6.6 generic")
check("Ubuntu recognised", i["distro"] == "ubuntu" and "Ubuntu 24.04" in i["pretty"] and not i["wsl"], i)
i = S.detect_os("Linux", "5.15.0-microsoft-standard-WSL2", "x86_64", ubuntu, "Linux version 5.15 microsoft-standard-WSL2")
check("WSL recognised", i["wsl"], i)
i = S.detect_os("Darwin", "23.1.0", "arm64", None, None, "14.2")
check("macOS recognised", i["system"] == "Darwin" and i["pretty"] == "macOS 14.2" and i["arch"] == "arm64", i)
i = S.detect_os("Windows", "11", "AMD64")
check("Windows recognised", i["system"] == "Windows" and i["pretty"] == "Windows 11", i)
i = S.detect_os("Linux", "6.0", "x86_64", "", "")
check("a Linux with no os-release still works", i["system"] == "Linux" and i["pretty"] == "Linux" and i["distro"] == "", i)
check("os-release parsing ignores comments and quotes", S.parse_os_release('# c\nID="fedora"\nX=\'y\'\n') == {"ID": "fedora", "X": "y"})

# ---- package managers and install hints
win, mac, lin = S.detect_os("Windows", "11", "AMD64"), S.detect_os("Darwin", "1", "arm64", None, None, "14"), S.detect_os("Linux", "6", "x86_64", ubuntu, "")
has = lambda *names: (lambda n: "/bin/" + n if n in names else None)
check("winget is preferred on Windows", S.package_manager(win, has("winget", "choco")) == "winget")
check("no package manager is reported as None", S.package_manager(lin, has()) is None and S.package_manager(mac, has()) is None)
check("apt is found on Debian-likes, dnf on Fedora", S.package_manager(lin, has("apt-get")) == "apt-get" and S.package_manager(lin, has("dnf")) == "dnf")
check("Python hint uses the right command", "apt-get install" in S.python_install_hint(lin, has("apt-get")) and "brew install python" in S.python_install_hint(mac, has("brew")))
check("Python hint falls back to the download page", "python.org" in S.python_install_hint(lin, has()))
cmd, note = S.ollama_install_command(lin, has("curl"))
check("Ollama on Linux: the official script, with a note", cmd and "ollama.com/install.sh" in cmd and "sudo" in note, (cmd, note))
check("Ollama on Linux with no curl says what to do", S.ollama_install_command(lin, has())[0] is None)
check("Ollama on macOS via brew, else the download page", S.ollama_install_command(mac, has("brew"))[0] == "brew install ollama" and "ollama.com/download" in S.ollama_install_command(mac, has())[1])
check("Ollama on Windows via winget", S.ollama_install_command(win, has("winget"))[0] == "winget install -e --id Ollama.Ollama")

# ---- the environment's state
tmp = tempfile.mkdtemp(prefix="setup_test_")
try:
    st, why = S.venv_state(tmp, "Linux")
    check("no .venv is 'missing'", st == "missing", (st, why))
    os.makedirs(os.path.join(tmp, ".venv", "Scripts"))
    st, why = S.venv_state(tmp, "Linux")
    check("a Windows .venv on Linux is 'stale' and says why", st == "stale" and "different kind" in why, (st, why))
    shutil.rmtree(os.path.join(tmp, ".venv"))
    os.makedirs(os.path.join(tmp, ".venv", "bin"))
    st, why = S.venv_state(tmp, "Windows")
    check("a Linux .venv on Windows is 'stale'", st == "stale" and "different kind" in why, (st, why))
    shutil.rmtree(os.path.join(tmp, ".venv"))
    os.makedirs(os.path.join(tmp, ".venv"))
    st, why = S.venv_state(tmp, sys_system := ("Windows" if os.name == "nt" else "Linux"))
    check("an empty .venv folder is 'stale' (its Python is missing)", st == "stale" and "missing" in why, (st, why))
    check("venv python path per OS", S.venv_python("/p", "Windows").endswith(os.path.join("Scripts", "python.exe")) and S.venv_python("/p", "Linux").endswith(os.path.join("bin", "python")))

    # a real environment made from the running Python, then its home pointed somewhere that doesn't exist
    shutil.rmtree(os.path.join(tmp, ".venv"))
    code, out = S.run([sys.executable, "-m", "venv", "--without-pip", os.path.join(tmp, ".venv")], timeout=120)
    if code == 0:
        check("a fresh environment is 'ok'", S.venv_state(tmp)[0] == "ok", S.venv_state(tmp))
        cfg = os.path.join(tmp, ".venv", "pyvenv.cfg")
        lines = open(cfg, encoding="utf-8").read().splitlines()
        lines = ["home = " + os.path.join(tmp, "nowhere") if l.lower().startswith("home") else l for l in lines]
        open(cfg, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        st, why = S.venv_state(tmp)
        check("an environment copied from another computer is 'stale'", st == "stale" and "isn't on this computer" in why, (st, why))
    else:
        check("could build a scratch environment", False, out[-200:])

    # ---- the dependency stamp changes when it should
    open(os.path.join(tmp, "requirements.txt"), "w").write("requests\n")
    h1 = S.deps_hash(tmp, (3, 12), "Linux")
    check("same inputs, same stamp", h1 == S.deps_hash(tmp, (3, 12), "Linux"))
    check("different Python or OS, different stamp", h1 != S.deps_hash(tmp, (3, 13), "Linux") and h1 != S.deps_hash(tmp, (3, 12), "Windows"))
    open(os.path.join(tmp, "requirements.txt"), "w").write("requests\nmeshtastic\n")
    check("changed requirements, different stamp", h1 != S.deps_hash(tmp, (3, 12), "Linux"))
    check("missing requirements.txt doesn't crash", isinstance(S.deps_hash(os.path.join(tmp, "nope"), (3, 12), "Linux"), str))

    # ---- line endings
    p = os.path.join(tmp, "x.sh")
    open(p, "wb").write(b"#!/usr/bin/env sh" + CR + LF + b"echo hi" + CR + LF)
    check("CRLF script is repaired", S.fix_line_endings(p) is True and open(p, "rb").read() == b"#!/usr/bin/env sh" + LF + b"echo hi" + LF)
    check("an LF script is left alone", S.fix_line_endings(p) is False)
    open(p, "wb").write(b"a" + CR + LF + b"b" + LF)
    S.fix_line_endings(p)
    check("only the CR before an LF goes", open(p, "rb").read() == b"a" + LF + b"b" + LF)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---- serial permission advice, model naming
check("Linux serial: not in the owning group -> advice names the group", "usermod -aG dialout" in (S.linux_serial_advice("/dev/ttyUSB0", ["sam"], "dialout") or ""))
check("Linux serial: in the group -> no advice", S.linux_serial_advice("/dev/ttyUSB0", ["dialout"], "dialout") is None)
check("Linux serial: unknown group -> no advice", S.linux_serial_advice("/dev/ttyUSB0", [], None) is None)
check("model names compare with the implicit :latest", S.same_model("qwen3.5", "qwen3.5:latest") and not S.same_model("llama3.2:3b", "llama3.2:1b"))

# ---- the real --check run on this machine changes nothing
root = S.ROOT
before = sorted(os.listdir(root))
buf = io.StringIO()
rc = S.main(["--check", "--dir", root], out=buf)
text = buf.getvalue()
check("--check runs and prints every section", all(s in text for s in ("This computer", "Python", "Private environment", "Ollama", "Folder", "Result")), text[-300:])
check("--check is ASCII only", all(ord(c) < 128 for c in text))
check("--check changes nothing in the folder", sorted(os.listdir(root)) == before)
check("--check exits 0 when nothing failed", rc == (1 if "[xx]" in text else 0), (rc, text[-200:]))
buf = io.StringIO()
rc = S.main(["--check", "--dir", tempfile.gettempdir()], out=buf)
check("a wrong folder is refused politely", rc == 1 and "isn't the project folder" in buf.getvalue(), buf.getvalue())

# ---- the launch scripts and bootstrappers exist and have the right line endings
for n in ("setup.sh", "start_bridge.sh", "stop_bridge.sh"):
    b = open(os.path.join(root, n), "rb").read()
    check(n + " uses Unix line endings and a shebang", CR not in b and b.startswith(b"#!/usr/bin/env sh"))
check("setup.bat uses Windows line endings", open(os.path.join(root, "setup.bat"), "rb").read().count(LF) == open(os.path.join(root, "setup.bat"), "rb").read().count(CR + LF))
ga = open(os.path.join(root, ".gitattributes")).read()
check(".gitattributes pins .sh to LF and .bat to CRLF", "*.sh text eol=lf" in ga and "*.bat text eol=crlf" in ga)
check("start_bridge.ps1 prefers the project's environment", ".venv" in open(os.path.join(root, "start_bridge.ps1")).read())

print(f"\n{len(fails)} failed" if fails else "\nall passed")
sys.exit(1 if fails else 0)
