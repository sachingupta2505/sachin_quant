"""
Automated Windows Task Scheduler Elevation Script
Module: scripts/elevate_task.py

Responsibilities:
1. Elevates SachinQuant_LiveScheduler task to run under SYSTEM with HIGHEST run level:
   `schtasks /change /tn "SachinQuant_LiveScheduler" /ru "SYSTEM" /rl HIGHEST`
2. Configures flags so the task runs whether user is logged on or not, with wake timer enabled.
3. Automatically triggers UAC elevation (Verb: 'runas') if executed in an unelevated session.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

TASK_NAME = "SachinQuant_LiveScheduler"


def is_admin() -> bool:
    """Checks if the current process is running with administrative privileges."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_command(cmd: list[str]) -> tuple[int, str, str]:
    """Runs a shell command and returns returncode, stdout, stderr."""
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.returncode, res.stdout.strip(), res.stderr.strip()


def elevate_and_configure_task() -> bool:
    """Configures SachinQuant_LiveScheduler with SYSTEM and HIGHEST privileges."""
    print("=" * 75)
    print(f"  SACHIN QUANT: TASK SCHEDULER ELEVATION ({TASK_NAME})")
    print("=" * 75)

    if not is_admin():
        print("[INFO] Process is not elevated. Requesting Administrator elevation via ShellExecute 'runas'...")
        script_path = str(Path(__file__).resolve())
        # Use pythonw or python to rerun with runas
        params = f'"{script_path}" --elevated'
        ret = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            sys.executable,
            params,
            None,
            1,  # SW_SHOWNORMAL
        )
        if ret > 32:
            print("[SUCCESS] Elevation request dispatched successfully to Windows UAC.")
            time.sleep(2.0)
            return True
        else:
            print(f"[WARNING] ShellExecute returned error code: {ret}. Trying PowerShell Start-Process elevation...")
            ps_cmd = (
                f'Start-Process python -ArgumentList \'"{script_path}" --elevated\' -Verb RunAs -Wait'
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd])
            return True

    # Running with Administrator privileges
    print("[INFO] Confirmed Administrator privileges.")

    # 1. Run schtasks /change /tn "SachinQuant_LiveScheduler" /ru "SYSTEM" /rl HIGHEST
    print(f"[EXECUTING] schtasks /change /tn \"{TASK_NAME}\" /ru \"SYSTEM\" /rl HIGHEST")
    code, out, err = run_command(
        ["schtasks", "/change", "/tn", TASK_NAME, "/ru", "SYSTEM", "/rl", "HIGHEST"]
    )
    if code == 0:
        print(f"[OK] schtasks succeeded: {out}")
    else:
        print(f"[WARNING] schtasks output: {out} | Error: {err}")

    # 2. Configure WakeToRun and battery settings via PowerShell Set-ScheduledTask
    ps_settings = (
        f"$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -WakeToRun -StartWhenAvailable; "
        f"Set-ScheduledTask -TaskName '{TASK_NAME}' -Settings $settings"
    )
    print("[EXECUTING] Configuring WakeToRun & Battery flags via PowerShell...")
    p_code, p_out, p_err = run_command(["powershell", "-NoProfile", "-Command", ps_settings])
    if p_code == 0:
        print("[OK] Successfully enabled WakeToRun and power conditions.")
    else:
        print(f"[WARNING] PowerShell settings: {p_out} | {p_err}")

    # 3. Verify task properties
    q_code, q_out, _ = run_command(["schtasks", "/query", "/tn", TASK_NAME, "/fo", "LIST", "/v"])
    if q_code == 0:
        print("\n[VERIFIED TASK CONFIGURATION]")
        for line in q_out.splitlines():
            if any(k in line for k in ("TaskName", "Run Level", "Run As User", "Status", "Schedule Type", "Start Time")):
                print(f"  {line}")

    print("\n[COMPLETE] Task elevation and configuration completed.")
    return True


if __name__ == "__main__":
    elevate_and_configure_task()
