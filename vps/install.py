"""Windows VPS helper: token (User env only, never written to a file), Task Scheduler, power, start/stop/status.
Portable on Windows 10/11 and Windows Server 2022. Does not print the GitHub token."""
from __future__ import annotations

import ctypes
import getpass
import json
import os
import subprocess
import sys
import time
from pathlib import Path

TASK = "TogetherWeThriveBot"
TOKEN_NAME = "GITHUB_TOKEN_TOGETHER"
ROOT = Path(__file__).resolve().parent

# Hide the console window when this script is used by the scheduled task? start.bat runs the bot directly.


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _user_env(name: str) -> str | None:
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment")
        try:
            val, _ = winreg.QueryValueEx(key, name)
            return val if val else None
        finally:
            winreg.CloseKey(key)
    except OSError:
        return os.environ.get(name)


def _set_user_env(name: str, value: str) -> None:
    import winreg
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_SET_VALUE)
    try:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    finally:
        winreg.CloseKey(key)
    os.environ[name] = value
    _broadcast_env()


def _delete_user_env(name: str) -> bool:
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(key, name)
        finally:
            winreg.CloseKey(key)
        os.environ.pop(name, None)
        _broadcast_env()
        return True
    except OSError:
        return False


def _broadcast_env() -> None:
    try:
        HWND_BROADCAST, WM_SETTINGCHANGE = 0xFFFF, 0x001A
        ctypes.windll.user32.SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment", 0, 2000, None)
    except Exception:
        pass


def _prompt_token() -> str:
    existing = _user_env(TOKEN_NAME) or os.environ.get(TOKEN_NAME)
    if existing:
        print(f"{TOKEN_NAME} da co (User env, length={len(existing)}). Bo qua nhap lai.")
        return existing
    print("Nhap GitHub token (quyen contents:write repo lechinh911/together-trading-bot).")
    print("Ky tu se bi an. Token chi luu vao User environment variable, KHONG ghi ra file.")
    tok = getpass.getpass(f"{TOKEN_NAME}: ").strip()
    if not tok:
        sys.exit("Khong co token — dung lai.")
    _set_user_env(TOKEN_NAME, tok)
    print(f"Da luu {TOKEN_NAME} vao User env (length={len(tok)}).")
    return tok


def _task_xml() -> str:
    start = ROOT / "start.bat"
    # Paths in XML should be escaped.
    bat = str(start).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Run at startup and at logon; restart on failure; no time limit.
    # LogonType InteractiveToken: runs as the installing user when they are logged on.
    # Boot trigger + S4U would need admin / stored password; we also add a Boot trigger
    # that starts the same user task if Windows allows it.
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Together We Thrive signal bot (virtual trades only)</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
    <BootTrigger>
      <Enabled>true</Enabled>
      <Delay>PT30S</Delay>
    </BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{bat}</Command>
    </Exec>
  </Actions>
</Task>
"""


def _create_task() -> None:
    xml_path = ROOT / "_task.xml"
    # Write UTF-16 as declared. This file has no token.
    xml_path.write_text(_task_xml(), encoding="utf-16")
    try:
        r = subprocess.run(
            ["schtasks", "/Create", "/TN", TASK, "/XML", str(xml_path), "/F"],
            capture_output=True, text=True)
        if r.returncode != 0:
            print("Tao task that bai:")
            print((r.stdout or "") + (r.stderr or ""))
            sys.exit(r.returncode)
        print(f"Da tao Task Scheduler '{TASK}' (logon + startup, restart 3 lan / 1 phut neu chet).")
        print("Luu y: task chay voi tai khoan cai dat khi user da dang nhap.")
        print("Tren VPS, bat tu dang nhap (auto-logon) de bot chay sau moi lan reboot.")
        print("Chay khi chua logon (S4U/SYSTEM) can quyen admin va mat khau Windows — bo qua neu khong cap.")
    finally:
        try:
            xml_path.unlink()
        except OSError:
            pass


def _power() -> None:
    if not _is_admin():
        print("Khong chay voi quyen admin — bo qua powercfg (khong doi cau hinh ngu).")
        print("Neu VPS di ngu, mo cmd Admin va chay: powercfg /change standby-timeout-ac 0")
        return
    cmds = [
        ["powercfg", "/change", "standby-timeout-ac", "0"],
        ["powercfg", "/change", "standby-timeout-dc", "0"],
        ["powercfg", "/change", "hibernate-timeout-ac", "0"],
        ["powercfg", "/change", "hibernate-timeout-dc", "0"],
    ]
    for c in cmds:
        subprocess.run(c, capture_output=True)
    print("Da dat powercfg: khong sleep / hibernate (AC va pin).")


def _bot_pids() -> list[int]:
    marker = str(ROOT).replace("/", "\\")
    out = []
    try:
        r = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "ProcessId,CommandLine", "/FORMAT:LIST"],
            capture_output=True, text=True, errors="replace")
        block_cmd, block_pid = "", None
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("CommandLine="):
                block_cmd = line[12:]
            elif line.startswith("ProcessId="):
                try:
                    block_pid = int(line.split("=", 1)[1])
                except ValueError:
                    block_pid = None
                if block_pid and ("-m tbot" in block_cmd or " -m tbot" in block_cmd) and (
                    marker.lower() in block_cmd.lower() or "tbot" in block_cmd
                ):
                    # Prefer path match; fall back to -m tbot started from this package via start.bat cwd
                    if marker.lower() in block_cmd.lower() or str(ROOT / "runtime").lower() in block_cmd.lower():
                        out.append(block_pid)
                block_cmd, block_pid = "", None
    except OSError:
        pass
    return out


def cmd_stop() -> int:
    subprocess.run(["schtasks", "/End", "/TN", TASK], capture_output=True)
    pids = _bot_pids()
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    print("Da dung bot." if pids else "Khong thay process bot (da dung hoac chua chay).")
    if pids:
        print("PID da stop: " + ", ".join(str(p) for p in pids))
    return 0


def cmd_status() -> int:
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK, "/FO", "LIST", "/V"],
                       capture_output=True, text=True, errors="replace")
    if r.returncode == 0:
        # Print only safe lines (no env/token would be here)
        for line in (r.stdout or "").splitlines():
            if any(k in line for k in ("TaskName", "Status", "Last Run", "Next Run", "Last Result", "Run As")):
                print(line)
    else:
        print(f"Task '{TASK}': chua tao.")
    pids = _bot_pids()
    print("Process: " + (", ".join("PID " + str(p) for p in pids) if pids else "khong chay"))
    bot_json = ROOT / "app" / "data" / "bot.json"
    st_json = ROOT / "app" / "data" / "status.json"
    if bot_json.is_file():
        try:
            data = json.loads(bot_json.read_text(encoding="utf-8"))
            print("bot.json generated_t7:", data.get("generated_t7"))
            print("publisher_host:", data.get("publisher_host"))
        except (OSError, ValueError):
            print("bot.json: khong doc duoc")
    if st_json.is_file():
        try:
            st = json.loads(st_json.read_text(encoding="utf-8"))
            print("WS connected:", st.get("connected"), "/", st.get("connections"),
                  "universe:", st.get("universe"), "uptime_s:", st.get("uptime_s"))
            gh = (st.get("github") or {})
            err = gh.get("last_error")
            if err:
                print("push last_error:", err)
        except (OSError, ValueError):
            pass
    tok = _user_env(TOKEN_NAME)
    print(f"{TOKEN_NAME}:", "SET len=" + str(len(tok)) if tok else "CHUA SET")
    return 0


def cmd_install() -> int:
    print("Together We Thrive — cai dat tren Windows VPS")
    print("TRUOC KHI START: dung bot tren box/cloud de tranh hai may cung push bot.json.")
    print("Neu bot kia van moi (< 8 phut), may nay se TU CHOI push cho den khi bên kia dung.")
    py = ROOT / "runtime" / "python" / "python.exe"
    if not py.is_file():
        sys.exit("Thieu runtime\\python\\python.exe — goi khong hop le.")
    _prompt_token()
    _create_task()
    _power()
    # Start via the task so restart-on-failure applies
    subprocess.run(["schtasks", "/Run", "/TN", TASK], capture_output=True)
    print("Da goi start bot. Doi 1-2 phut roi chay status.bat.")
    print("Kiem tra generated_t7 tren dashboard (raw bot.json) sau khi box da dung.")
    return 0


def cmd_uninstall() -> int:
    cmd_stop()
    subprocess.run(["schtasks", "/Delete", "/TN", TASK, "/F"], capture_output=True)
    print(f"Da xoa task '{TASK}'.")
    if _delete_user_env(TOKEN_NAME):
        print(f"Da xoa {TOKEN_NAME} khoi User env.")
    else:
        print(f"{TOKEN_NAME} khong co trong User env (bo qua).")
    print("Thu muc goi (app, runtime, data) GIU NGUYEN — xoa tay neu muon.")
    return 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("install", "uninstall", "start", "stop", "status"):
        print("Usage: install.py install|uninstall|start|stop|status")
        return 2
    cmd = sys.argv[1]
    if cmd == "install":
        return cmd_install()
    if cmd == "uninstall":
        return cmd_uninstall()
    if cmd == "stop":
        return cmd_stop()
    if cmd == "status":
        return cmd_status()
    if cmd == "start":
        # Interactive / task start is start.bat; this path just runs the task.
        r = subprocess.run(["schtasks", "/Run", "/TN", TASK], capture_output=True, text=True)
        if r.returncode != 0:
            print("Task chua co — chay start.bat hoac install.bat truoc.")
            return r.returncode
        print("Da goi task start.")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
