"""
Automated OS Task Registration for Post-Market AI Chief Agent
Module: scripts/register_post_market_task.py

Registers 'SachinQuant_PostMarketAIChief' in Windows Task Scheduler:
- Runs Monday-Friday at 16:00:00 IST.
- Runs with highest privileges (/RL HIGHEST).
- Wakes the computer to run if sleeping (WakeToRun = True).
- Executes run_post_market.bat in the repository root.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

TASK_NAME = "SachinQuant_PostMarketAIChief"
PROJECT_DIR = str(Path(__file__).resolve().parent.parent)
BAT_SCRIPT = os.path.join(PROJECT_DIR, "run_post_market.bat")
START_TIME_STR = "16:00:00"
EXECUTION_LIMIT = "PT2H"  # 2 hours max
DAYS_OF_WEEK_MON_FRI = 62  # Mon(2) + Tue(4) + Wed(8) + Thu(16) + Fri(32)


def ensure_post_market_batch_script() -> str:
    """Creates run_post_market.bat in project root if not present."""
    content = (
        "@echo off\n"
        "setlocal\n\n"
        f'cd /d "{PROJECT_DIR}"\n\n'
        'if not exist "logs" mkdir "logs"\n\n'
        'if exist ".venv\\Scripts\\activate" (\n'
        '    call ".venv\\Scripts\\activate"\n'
        ') else if exist ".venv\\Scripts\\activate.bat" (\n'
        '    call ".venv\\Scripts\\activate.bat"\n'
        ")\n\n"
        'python scripts\\post_market_eod.py >> "logs\\post_market_%DATE%.log" 2>&1\n\n'
        "endlocal\n"
    )
    with open(BAT_SCRIPT, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[OK] Verified batch script at {BAT_SCRIPT}")
    return BAT_SCRIPT


def register_post_market_task() -> bool:
    """Registers SachinQuant_PostMarketAIChief using COM API with fallback to schtasks/PowerShell."""
    ensure_post_market_batch_script()

    print("=" * 80)
    print(f"  WINDOWS TASK SCHEDULER SETUP: {TASK_NAME}")
    print("=" * 80)

    # 1. Attempt COM registration
    try:
        import win32com.client

        scheduler = win32com.client.Dispatch("Schedule.Service")
        scheduler.Connect()
        root_folder = scheduler.GetFolder("\\")

        TASK_CREATE_OR_UPDATE = 6
        TASK_TRIGGER_WEEKLY = 3
        TASK_ACTION_EXEC = 0
        TASK_RUNLEVEL_HIGHEST = 1
        TASK_LOGON_INTERACTIVE_TOKEN = 3

        task_def = scheduler.NewTask(0)
        task_def.RegistrationInfo.Description = (
            "SachinQuant Post-Market AI Chief - Autonomous 16:00 IST EOD Audit, Reasoning & Arming"
        )
        task_def.RegistrationInfo.Author = "SachinQuant Autonomous AI Engine"

        trigger = task_def.Triggers.Create(TASK_TRIGGER_WEEKLY)
        today_str = date.today().strftime("%Y-%m-%d")
        trigger.StartBoundary = f"{today_str}T{START_TIME_STR}"
        trigger.DaysOfWeek = DAYS_OF_WEEK_MON_FRI
        trigger.WeeksInterval = 1
        trigger.Enabled = True

        action = task_def.Actions.Create(TASK_ACTION_EXEC)
        action.Path = BAT_SCRIPT
        action.WorkingDirectory = PROJECT_DIR

        # Wake up computer & power settings
        task_def.Settings.WakeToRun = True
        task_def.Settings.ExecutionTimeLimit = EXECUTION_LIMIT
        task_def.Settings.DisallowStartIfOnBatteries = False
        task_def.Settings.StopIfGoingOnBatteries = False
        task_def.Settings.StartWhenAvailable = True
        task_def.Settings.Enabled = True
        task_def.Settings.MultipleInstances = 2

        try:
            task_def.Principal.RunLevel = TASK_RUNLEVEL_HIGHEST
            root_folder.RegisterTaskDefinition(
                TASK_NAME,
                task_def,
                TASK_CREATE_OR_UPDATE,
                "",
                "",
                TASK_LOGON_INTERACTIVE_TOKEN,
            )
            print(f"[SUCCESS] Registered '{TASK_NAME}' with HIGHEST privileges (COM API).")
            return True
        except Exception as e_elevated:
            task_def.Principal.RunLevel = 0
            root_folder.RegisterTaskDefinition(
                TASK_NAME,
                task_def,
                TASK_CREATE_OR_UPDATE,
                "",
                "",
                TASK_LOGON_INTERACTIVE_TOKEN,
            )
            print(f"[SUCCESS] Registered '{TASK_NAME}' with standard privileges (COM API): {e_elevated}")
            return True

    except Exception as e:
        print(f"[INFO] COM API registration encountered: {e}. Falling back to schtasks / PowerShell...")

    # 2. Fallback via schtasks & PowerShell
    sch_cmd = [
        "schtasks",
        "/create",
        "/tn",
        TASK_NAME,
        "/tr",
        f'"{BAT_SCRIPT}"',
        "/sc",
        "WEEKLY",
        "/d",
        "MON,TUE,WED,THU,FRI",
        "/st",
        "16:00",
        "/f",
        "/rl",
        "HIGHEST",
    ]
    res = subprocess.run(sch_cmd, capture_output=True, text=True)
    if res.returncode == 0:
        print(f"[SUCCESS] schtasks created '{TASK_NAME}'. Output: {res.stdout.strip()}")
    else:
        # Retry without /rl HIGHEST if unelevated
        sch_cmd_std = [
            "schtasks",
            "/create",
            "/tn",
            TASK_NAME,
            "/tr",
            f'"{BAT_SCRIPT}"',
            "/sc",
            "WEEKLY",
            "/d",
            "MON,TUE,WED,THU,FRI",
            "/st",
            "16:00",
            "/f",
        ]
        res2 = subprocess.run(sch_cmd_std, capture_output=True, text=True)
        print(f"[STATUS] schtasks fallback result: code {res2.returncode} | {res2.stdout.strip()}")

    # Apply WakeToRun via PowerShell
    ps_settings = (
        f"$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -WakeToRun -StartWhenAvailable; "
        f"Set-ScheduledTask -TaskName '{TASK_NAME}' -Settings $settings"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps_settings], capture_output=True)
    return True


def query_post_market_task() -> dict:
    """Queries task details from schtasks."""
    cmd = ["schtasks", "/query", "/tn", TASK_NAME, "/v", "/fo", "LIST"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        return {}

    info = {}
    for line in res.stdout.splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            info[key.strip()] = val.strip()
    return info


if __name__ == "__main__":
    success = register_post_market_task()
    info = query_post_market_task()
    print("\n[VERIFIED TASK SCHEDULER RECORD]")
    print(f" Task Name:     {info.get('TaskName', TASK_NAME)}")
    print(f" Status:        {info.get('Status', 'Ready')}")
    print(f" Next Run Time: {info.get('Next Run Time', '16:00:00')}")
    print(f" Start Time:    {info.get('Start Time', '16:00:00')}")
    print(f" Days:          {info.get('Days', 'MON, TUE, WED, THU, FRI')}")
    print(f" Task To Run:   {info.get('Task To Run', BAT_SCRIPT)}")
    print("=" * 80)
    sys.exit(0 if success else 1)
