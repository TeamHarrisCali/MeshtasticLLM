"""setup_env.py start-at-login: the per-OS plans (as pure data), quoting and XML escaping, --check reporting, and that declining or a missing
systemd executes nothing. Nothing here creates, enables or deletes a real scheduled task, service or launch agent: commands go to a fake
runner and files go to a throwaway folder."""
import io, os, plistlib, shutil, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import setup_env as S

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(name)

WIN = S.detect_os("Windows", "11", "AMD64")
MAC = S.detect_os("Darwin", "23.1.0", "arm64", None, None, "14.2")
LIN = S.detect_os("Linux", "6.6.0", "x86_64", 'ID=ubuntu\n', "Linux version 6.6 generic")
HOME_L, HOME_M = "/home/sam", "/Users/sam"
import posixpath
posixpath_join = posixpath.join

def all_cmds(plan):
    return plan["pre_cmds"] + plan["install_cmds"] + plan["remove_cmds"] + plan["after_remove_cmds"] + [plan["query_cmd"]]

class FakeRunner:
    """Stands in for setup_env.run: records every command, answers from a table (default: success)."""
    def __init__(self, answers=None, default=(0, "")):
        self.calls, self.answers, self.default = [], answers or {}, default
    def __call__(self, cmd, cwd=None, timeout=None, shell=False):
        self.calls.append(list(cmd))
        for key, ans in self.answers.items():
            if key in " ".join(cmd):
                return ans
        return self.default

# ---- Windows: Task Scheduler
winroot = r"C:\Users\Sam Smith & Co\Meshtastic LLM"
p = S.autostart_plan(WIN, winroot, winroot + r"\.venv\Scripts\python.exe", r"C:\Users\Sam", 0)
check("Windows uses Task Scheduler and writes no files", p["kind"] == "schtasks" and p["files"] == {} and p["dirs"] == [])
c = p["install_cmds"][0]
check("schtasks create: task name, ONLOGON, limited, replace existing", c[:2] == ["schtasks", "/Create"] and c[c.index("/TN") + 1] == "MeshLLMBridge" and c[c.index("/SC") + 1] == "ONLOGON" and c[c.index("/RL") + 1] == "LIMITED" and "/F" in c, c)
tr = c[c.index("/TR") + 1]
check("schtasks runs start_bridge.ps1 hidden, script path quoted (spaces and &)", tr == r'powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + winroot + r'\start_bridge.ps1"', tr)
check("schtasks query and delete name the same task", p["query_cmd"] == ["schtasks", "/Query", "/TN", "MeshLLMBridge"] and p["remove_cmds"] == [["schtasks", "/Delete", "/TN", "MeshLLMBridge", "/F"]], p)
check("schtasks: no /RU /RP /SC SYSTEM (nothing that needs admin or a password)", not any(x in c for x in ("/RU", "/RP", "SYSTEM", "HIGHEST", "/S")), c)
long_root = "C:\\" + "x" * 300
check("a path too long for Task Scheduler is reported as a problem", S.autostart_plan(WIN, long_root, "p", "h", 0)["problem"] != "", "")
check("a normal path has no problem", p["problem"] == "")
uni = "C:\\Users\\S\u00e9bastien\\\u00fcber proj"
check("schtasks keeps unicode paths intact", uni + "\\start_bridge.ps1" in S.schtasks_create_cmd(uni)[7])

# ---- Linux: systemd user unit
linroot = "/home/sam/Mesh Project & Co/mesh llm"
lpy = linroot + "/.venv/bin/python"
p = S.autostart_plan(LIN, linroot, lpy, HOME_L, 1000)
unit = "/home/sam/.config/systemd/user/mesh-llm-bridge.service"
check("Linux: one unit file under ~/.config/systemd/user", p["kind"] == "systemd" and list(p["files"]) == [unit], list(p["files"]))
text = p["files"][unit]
check("unit has the required settings", all(s in text for s in ("[Unit]", "[Service]", "[Install]", "Type=simple", "Restart=on-failure", "RestartSec=10", "WantedBy=default.target")), text)
check("unit quotes paths with spaces in WorkingDirectory and ExecStart", 'WorkingDirectory="%s"' % linroot in text and 'ExecStart="%s" -u mesh_llm_bridge.py' % lpy in text, text)
plain = S.systemd_unit_text("/opt/mesh", "/opt/mesh/.venv/bin/python")
check("unit leaves simple paths unquoted", "WorkingDirectory=/opt/mesh\n" in plain and "ExecStart=/opt/mesh/.venv/bin/python -u mesh_llm_bridge.py\n" in plain, plain)
check("systemd quoting escapes %, $, quotes and backslashes", S.systemd_arg("/a%b$c") == "/a%%b$$c" and S.systemd_arg('/a "b"') == '"/a \\"b\\""' and S.systemd_arg("/a b\\c") == '"/a b\\\\c"' and S.systemd_arg("/a$b", False) == "/a$b")
check("unit has no CR and ends with a newline", "\r" not in text and text.endswith("\n"))
uni_l = S.systemd_unit_text("/home/s\u00e9/\u00fcber", "/home/s\u00e9/\u00fcber/.venv/bin/python")
check("unit keeps unicode paths", "/home/s\u00e9/\u00fcber" in uni_l)
check("Linux install: daemon-reload then enable --now", p["install_cmds"] == [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", "mesh-llm-bridge.service"]], p["install_cmds"])
check("Linux removal mirrors it: disable --now, then reload after the file is gone", p["remove_cmds"] == [["systemctl", "--user", "disable", "--now", "mesh-llm-bridge.service"]] and p["after_remove_cmds"] == [["systemctl", "--user", "daemon-reload"]])
check("Linux note mentions enable-linger as an option", "loginctl enable-linger $USER" in p["note"])

# ---- macOS: launchd agent
macroot = "/Users/sam/Meshtastic & <LLM> 'x'/proj dir"
mpy = macroot + "/.venv/bin/python"
p = S.autostart_plan(MAC, macroot, mpy, HOME_M, 501)
plist = "/Users/sam/Library/LaunchAgents/com.meshllm.bridge.plist"
check("macOS: one plist under ~/Library/LaunchAgents", p["kind"] == "launchd" and list(p["files"]) == [plist], list(p["files"]))
xml = p["files"][plist]
check("plist XML-escapes &, < and >", "&amp;" in xml and "&lt;LLM&gt;" in xml and "& <" not in xml, xml)
d = plistlib.loads(xml.encode("utf-8"))
check("plist parses back to the exact paths (spaces, &, <>, quotes)", d["WorkingDirectory"] == macroot and d["ProgramArguments"] == [mpy, "-u", "mesh_llm_bridge.py"], d)
check("plist: RunAtLoad, KeepAlive only after failure, label, logs under <root>/logs", d["RunAtLoad"] is True and d["KeepAlive"] == {"SuccessfulExit": False} and d["Label"] == "com.meshllm.bridge" and d["StandardOutPath"] == macroot + "/logs/bridge.log" and d["StandardErrorPath"] == macroot + "/logs/bridge.err.log", d)
check("macOS: the logs folder is created before loading", p["dirs"] == [macroot + "/logs"])
check("macOS install: bootstrap gui/<uid> <plist>", p["install_cmds"] == [["launchctl", "bootstrap", "gui/501", plist]], p["install_cmds"])
check("macOS removal mirrors it: bootout gui/<uid>/<label>", p["remove_cmds"] == [["launchctl", "bootout", "gui/501/com.meshllm.bridge"]] and p["pre_cmds"] == p["remove_cmds"])
uni_m = plistlib.loads(S.launchd_plist_text("/Users/s\u00e9/\u00fcber", "/Users/s\u00e9/\u00fcber/.venv/bin/python").encode("utf-8"))
check("plist keeps unicode paths", uni_m["WorkingDirectory"] == "/Users/s\u00e9/\u00fcber")

# ---- all plans
plans = {"win": S.autostart_plan(WIN, winroot, "p", r"C:\Users\Sam", 0), "lin": S.autostart_plan(LIN, linroot, lpy, HOME_L, 1000), "mac": S.autostart_plan(MAC, macroot, mpy, HOME_M, 501)}
check("no plan uses sudo, su, runas or an elevated flag", not any(w in [x.lower() for x in cmd] for p_ in plans.values() for cmd in all_cmds(p_) for w in ("sudo", "su", "runas", "doas", "pkexec", "--system", "/ru", "/rp")), "")
check("linux/mac plans put every file under the user's home", all(f.startswith(h + "/") for k, h in (("lin", HOME_L), ("mac", HOME_M)) for f in plans[k]["files"]))
check("systemd commands are all --user", all("--user" in c for c in all_cmds(plans["lin"])))
check("every plan has a query command and a summary", all(p_["query_cmd"] and p_["summary"] for p_ in plans.values()))
check("an unknown OS gets a 'none' plan with a reason, and nothing to run", S.autostart_plan({"system": "Plan9"}, "/r", "p", "/h", 1)["kind"] == "none" and S.autostart_plan({"system": "Plan9"}, "/r", "p", "/h", 1)["problem"])

# ---- bridge python: .venv when it works, else the running interpreter
tmp = tempfile.mkdtemp(prefix="autostart_test_")
try:
    py, st, why = S.bridge_python(tmp, LIN, lambda r, s: ("ok", "Python 3.12"))
    check("bridge uses the .venv python when the environment is ok", py == S.venv_python(tmp, "Linux") and st == "ok")
    py, st, why = S.bridge_python(tmp, LIN)
    check("bridge uses the running interpreter when there is no .venv", py == sys.executable and st == "missing", (py, st))
    py, st, why = S.bridge_python(tmp, LIN, lambda r, s: ("stale", "copied"))
    check("bridge uses the running interpreter when the .venv is stale", py == sys.executable and st == "stale")

    # ---- executing a plan: confirmation, declined = nothing happens
    home = os.path.join(tmp, "home")
    lp = S.autostart_plan(LIN, "/srv/mesh", "/srv/mesh/.venv/bin/python", home, 1000)
    unitfile = next(iter(lp["files"]))
    check("the plan's unit path is under the (temporary) home given to it", unitfile == posixpath_join(home, ".config/systemd/user/" + S.UNIT_NAME), unitfile)
    systemd_here = dict(which=lambda n: "/bin/" + n, isdir=lambda d: True)

    asked = []
    def no(q, yes=False): asked.append((q, yes)); return False
    def yes_(q, yes=False): asked.append((q, yes)); return True
    fr, buf = FakeRunner(), io.StringIO()
    ok = S.autostart_install(S.Report(buf), lp, fr, no, False, **systemd_here)
    check("declining the confirmation executes nothing and writes nothing", ok is True and fr.calls == [] and not os.path.exists(home) and "nothing was changed" in buf.getvalue(), (fr.calls, buf.getvalue()))
    check("the confirmation question was asked, with yes=False", len(asked) == 1 and asked[0][1] is False and "start at login" in asked[0][0])
    check("ask_yes with no keyboard and no --yes says no", S.ask_yes("x", False, interactive=False) is False and S.ask_yes("x", True, interactive=False) is True)

    fr, buf = FakeRunner(), io.StringIO()
    ok = S.autostart_install(S.Report(buf), lp, fr, yes_, False, **systemd_here)
    check("confirming writes the unit and runs the install commands in order", ok and os.path.isfile(unitfile) and fr.calls[:2] == lp["install_cmds"], (fr.calls, buf.getvalue()))
    data = open(unitfile, "rb").read()
    check("the unit file is LF-only UTF-8", bytes([13]) not in data and data.decode("utf-8") == lp["files"][unitfile])
    check("the success report shows the linger hint", "enable-linger" in buf.getvalue() and "[ok]" in buf.getvalue(), buf.getvalue())

    # a failing command is reported, not raised
    fr, buf = FakeRunner({"enable": (1, "Failed: boom")}), io.StringIO()
    ok = S.autostart_install(S.Report(buf), lp, fr, yes_, False, **systemd_here)
    check("a failing systemctl is reported as [xx] with its output", ok is False and "[xx]" in buf.getvalue() and "boom" in buf.getvalue(), buf.getvalue())

    # removal
    fr, buf = FakeRunner({"is-enabled": (0, "enabled")}), io.StringIO()
    calls_state = {"n": 0}
    def state_runner(cmd, cwd=None, timeout=None, shell=False):
        fr.calls.append(list(cmd))
        if "is-enabled" in cmd:
            calls_state["n"] += 1
            return (0, "enabled") if not calls_state.get("gone") else (1, "disabled")
        if "disable" in cmd:
            calls_state["gone"] = True
        return 0, ""
    ok = S.autostart_remove(S.Report(buf), lp, state_runner, **systemd_here)
    check("removal runs disable --now, deletes the unit file, reloads, and confirms", ok and not os.path.exists(unitfile) and lp["remove_cmds"][0] in fr.calls and lp["after_remove_cmds"][0] in fr.calls and "removed" in buf.getvalue(), (fr.calls, buf.getvalue()))
    check("removal runs disable before the final reload", fr.calls.index(lp["remove_cmds"][0]) < fr.calls.index(lp["after_remove_cmds"][0]), fr.calls)
    fr, buf = FakeRunner({"is-enabled": (1, "not-found")}), io.StringIO()
    ok = S.autostart_remove(S.Report(buf), lp, fr, **systemd_here)
    check("removing when nothing is installed changes nothing", ok and fr.calls == [lp["query_cmd"]] and "nothing to remove" in buf.getvalue(), (fr.calls, buf.getvalue()))

    # ---- no systemd: clear message and the fallback, nothing executed
    for label, kw in (("no systemctl", dict(which=lambda n: None, isdir=lambda d: True)), ("no /run/systemd/system", dict(which=lambda n: "/bin/" + n, isdir=lambda d: False))):
        fr, buf = FakeRunner(), io.StringIO()
        ok = S.autostart_install(S.Report(buf), lp, fr, yes_, True, **kw)
        out = buf.getvalue()
        check("%s: says systemd isn't running, names the fallback, runs nothing" % label, ok is True and fr.calls == [] and "systemd isn't running" in out and "start_bridge.sh" in out and "startup applications" in out and "WSL" in out, out)
    os.remove(unitfile) if os.path.exists(unitfile) else None

    # ---- macOS and Windows: executing via the fake runner
    mhome = os.path.join(tmp, "machome")
    mp = S.autostart_plan(MAC, os.path.join(tmp, "proj"), "/usr/bin/python3", mhome, 501)
    fr, buf = FakeRunner(), io.StringIO()
    mac_kw = dict(which=lambda n: "/bin/" + n, isdir=lambda d: True)
    ok = S.autostart_install(S.Report(buf), mp, fr, no, False, **mac_kw)
    check("macOS declined: nothing executed or written", ok and fr.calls == [] and not os.path.exists(mhome) and not os.path.exists(os.path.join(tmp, "proj")))
    ok = S.autostart_install(S.Report(io.StringIO()), mp, fr, yes_, False, **mac_kw)
    check("macOS confirmed: plist and logs folder written, old copy booted out, then bootstrap", ok and os.path.isfile(next(iter(mp["files"]))) and os.path.isdir(os.path.join(tmp, "proj", "logs")) and fr.calls[0] == mp["pre_cmds"][0] and fr.calls[1] == mp["install_cmds"][0], fr.calls)
    wp = S.autostart_plan(WIN, r"C:\proj", "p", "h", 0)
    fr, buf = FakeRunner(), io.StringIO()
    ok = S.autostart_install(S.Report(buf), wp, fr, no, False, which=lambda n: "x")
    check("Windows declined: nothing executed", ok and fr.calls == [])
    fr, buf = FakeRunner({"/Create": (1, "ERROR: Access is denied.")}), io.StringIO()
    ok = S.autostart_install(S.Report(buf), wp, fr, yes_, False, which=lambda n: "x")
    check("Windows 'access denied' explains the administrator / Startup-folder options", ok is False and "shell:startup" in buf.getvalue() and "Access is denied" in buf.getvalue(), buf.getvalue())
    fr, buf = FakeRunner(), io.StringIO()
    ok = S.autostart_install(S.Report(buf), wp, fr, yes_, False, which=lambda n: None)
    check("Windows without schtasks: explained, nothing executed", ok and fr.calls == [] and "schtasks" in buf.getvalue())

    # ---- state
    check("state: query ok -> installed", S.autostart_state(wp, FakeRunner())[0] == "installed")
    check("state: query fails -> not installed", S.autostart_state(wp, FakeRunner(default=(1, "ERROR: cannot find")))[0] == "not installed")
    check("state: program missing -> unknown", S.autostart_state(wp, FakeRunner(default=(127, "nope")))[0] == "unknown")
    check("state: systemd without a bus -> unknown", S.autostart_state(lp, FakeRunner(default=(1, "Failed to connect to bus")))[0] == "unknown")
    check("state: unit file present but disabled is explained", "exists" in S.autostart_state(lp, FakeRunner(default=(1, "disabled")), exists=lambda p_: True)[1])

    # ---- main(): --check, flags, nothing runs on a plain invocation
    fake_root = os.path.join(tmp, "project")
    os.makedirs(fake_root)
    open(os.path.join(fake_root, "mesh_llm_bridge.py"), "w").write("")
    fr, buf = FakeRunner(default=(1, "not found")), io.StringIO()
    rc = S.main(["--autostart", "--dir", fake_root], out=buf, runner=fr, ask=no, home=os.path.join(tmp, "h2"), uid=7, info=LIN, which=lambda n: "/bin/" + n, isdir=lambda d: True)
    text = buf.getvalue()
    check("main --autostart: shows the section, asks, and declining runs nothing", rc == 0 and "Start at login" in text and "nothing was changed" in text and len(asked) and [c for c in fr.calls if c[0] == "systemctl" and "--user" in c and "is-enabled" not in c] == [] and not os.path.exists(os.path.join(tmp, "h2")), (rc, text, fr.calls))
    check("main --autostart does not run the rest of the setup", "Private environment" not in text and "Dependencies" not in text)
    fr, buf = FakeRunner(default=(1, "not found")), io.StringIO()
    rc = S.main(["--no-autostart", "--dir", fake_root], out=buf, runner=fr, home=os.path.join(tmp, "h3"), uid=7, info=LIN, which=lambda n: "/bin/" + n, isdir=lambda d: True)
    check("main --no-autostart with nothing installed only looks", rc == 0 and "nothing to remove" in buf.getvalue() and fr.calls == [plans["lin"]["query_cmd"]], (fr.calls, buf.getvalue()))
    import contextlib
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            S.main(["--autostart", "--no-autostart", "--dir", fake_root], out=io.StringIO())
        both = False
    except SystemExit as e:
        both = e.code != 0
    check("--autostart together with --no-autostart is refused", both)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---- the real --check on this machine: has the section, changes nothing (it may run a read-only query such as schtasks /Query)
before = sorted(os.listdir(ROOT))
buf = io.StringIO()
rc = S.main(["--check", "--dir", ROOT], out=buf)
text = buf.getvalue()
check("--check prints a 'Start at login' section with one installed / not installed line", "Start at login" in text and any(("start at login: " + s) in text for s in ("installed", "not installed", "unknown")), text[-400:])
check("--check output is ASCII only", all(ord(c) < 128 for c in text))
check("--check changes nothing in the folder", sorted(os.listdir(ROOT)) == before)

# --check with a fake runner: read-only commands only, and --autostart alongside it changes nothing
fr, buf = FakeRunner(default=(1, "x")), io.StringIO()
rc = S.main(["--check", "--autostart", "--yes", "--dir", ROOT], out=buf, runner=fr, info=WIN, home="/nonexistent-home", uid=0)
check("--check --autostart --yes only queries (no create / delete)", all(c[:2] == ["schtasks", "/Query"] for c in fr.calls) and len(fr.calls) == 1 and "nothing was changed" in buf.getvalue() and "not installed" in buf.getvalue(), (fr.calls, buf.getvalue()[-300:]))
fr = FakeRunner(default=(0, ""))
buf = io.StringIO()
S.main(["--check", "--dir", ROOT], out=buf, runner=fr, info=WIN)
check("--check reports 'installed' when the query succeeds", "start at login: installed" in buf.getvalue())

# ---- plain `python setup_env.py` never touches start-at-login; the options exist; ASCII-only text
src = open(os.path.join(ROOT, "setup_env.py"), encoding="utf-8").read()
check("the autostart code is reachable only via the flags", src.count("run_autostart(") == 2 and "if (opts.autostart or opts.no_autostart) and not opts.check:" in src)
check("--autostart and --no-autostart are documented in the docstring", "--autostart" in S.__doc__ and "--no-autostart" in S.__doc__)
check("setup_env.py has no non-ASCII characters", all(ord(c) < 128 for c in src))
check("setup_env.py is LF-only", open(os.path.join(ROOT, "setup_env.py"), "rb").read().count(bytes([13])) == 0)
for label, pl in plans.items():
    txt = "\n".join([pl["summary"], pl["note"]])
    check("%s plan messages are ASCII only" % label, all(ord(c) < 128 for c in txt), txt)

print(f"\n{len(fails)} failed" if fails else "\nall passed")
sys.exit(1 if fails else 0)
