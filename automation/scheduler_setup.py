"""
SachinQuant Windows Task Scheduler Setup
Registers "SachinQuant_LiveScheduler" to automate daily market execution from 09:14 IST Mon-Fri.
"""

import sys
import os
import subprocess
from datetime import datetime, date
from pathlib import Path

TASK_NAME = "SachinQuant_LiveScheduler"
PROJECT_DIR = str(Path(__file__).resolve().parent.parent)
BAT_SCRIPT = os.path.join(PROJECT_DIR, "run_trading_day.bat")
START_TIME_STR = "09:14:00"
EXECUTION_LIMIT = "PT6H30M"  # 6.5 hours (terminates at 15:45 IST)
DAYS_OF_WEEK_MON_FRI = 62  # Monday (2) + Tuesday (4) + Wednesday (8) + Thursday (16) + Friday (32)


def ensure_batch_script():
    """Ensure run_trading_day.bat exists in the project root."""
    if not os.path.exists(BAT_SCRIPT):
        print(f"Creating {BAT_SCRIPT}...")
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
            'python main_runner.py >> "logs\\daily_trading_%DATE%.log" 2>&1\n\n'
            "endlocal\n"
        )
        with open(BAT_SCRIPT, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[OK] Created {BAT_SCRIPT}")
    else:
        print(f"[OK] Found existing {BAT_SCRIPT}")


def register_task_com():
    """Register scheduled task using Windows Task Scheduler COM API (win32com)."""
    import win32com.client

    scheduler = win32com.client.Dispatch("Schedule.Service")
    scheduler.Connect()
    root_folder = scheduler.GetFolder("\\")

    # TASK_CREATE_OR_UPDATE = 6
    TASK_CREATE_OR_UPDATE = 6
    TASK_TRIGGER_WEEKLY = 3
    TASK_ACTION_EXEC = 0
    TASK_RUNLEVEL_HIGHEST = 1
    TASK_RUNLEVEL_LUA = 0
    TASK_LOGON_INTERACTIVE_TOKEN = 3

    task_def = scheduler.NewTask(0)
    task_def.RegistrationInfo.Description = "SachinQuant Live Scheduler - Automated 09:14 IST Market Lifecycle"
    task_def.RegistrationInfo.Author = "SachinQuant Autonomous Engine"

    # Triggers: Mon-Fri at 09:14:00 IST
    trigger = task_def.Triggers.Create(TASK_TRIGGER_WEEKLY)
    today_str = date.today().strftime("%Y-%m-%d")
    trigger.StartBoundary = f"{today_str}T{START_TIME_STR}"
    trigger.DaysOfWeek = DAYS_OF_WEEK_MON_FRI
    trigger.WeeksInterval = 1
    trigger.Enabled = True

    # Action: Execute run_trading_day.bat in PROJECT_DIR
    action = task_def.Actions.Create(TASK_ACTION_EXEC)
    action.Path = BAT_SCRIPT
    action.WorkingDirectory = PROJECT_DIR

    # Power conditions & Execution policy
    task_def.Settings.WakeToRun = True  # Allow waking the computer
    task_def.Settings.ExecutionTimeLimit = EXECUTION_LIMIT  # Auto-terminate after 6.5 hours
    task_def.Settings.DisallowStartIfOnBatteries = False
    task_def.Settings.StopIfGoingOnBatteries = False
    task_def.Settings.StartWhenAvailable = True
    task_def.Settings.Enabled = True
    task_def.Settings.MultipleInstances = 2  # TASK_INSTANCES_IGNORE_NEW

    # Privilege handling: Try highest privileges first, fallback to standard interactive token
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
        print(f"[SUCCESS] Registered '{TASK_NAME}' with HIGHEST privileges.")
        return True
    except Exception as e_elevated:
        # Fallback to standard user privileges if unelevated shell
        try:
            task_def.Principal.RunLevel = TASK_RUNLEVEL_LUA
            root_folder.RegisterTaskDefinition(
                TASK_NAME,
                task_def,
                TASK_CREATE_OR_UPDATE,
                "",
                "",
                TASK_LOGON_INTERACTIVE_TOKEN,
            )
            print(f"[SUCCESS] Registered '{TASK_NAME}' with standard privileges ({e_elevated!r} handled).")
            return True
        except Exception as e_std:
            print(f"[ERROR] Failed to register task via COM: {e_std}")
            return False


def query_task(task_name=TASK_NAME):
    """Query Task Scheduler using schtasks CLI and display key properties."""
    cmd = ["schtasks", "/query", "/tn", task_name, "/fo", "LIST", "/v"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"[ERROR] Failed to query task {task_name}: {res.stderr.strip()}")
        return None

    output = res.stdout
    parsed = {}
    for line in output.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if key and val:
                parsed[key] = val

    print("\n" + "=" * 65)
    print(f" Windows Task Scheduler Verification: {task_name}")
    print("=" * 65)
    fields = [
        "TaskName",
        "Next Run Time",
        "Status",
        "Days",
        "Start Time",
        "Stop Task If Runs X Hours and X Mins",
        "Task To Run",
        "Start In",
        "Run As User",
    ]
    for f in fields:
        if f in parsed:
            print(f" {f:36}: {parsed[f]}")
    print("=" * 65 + "\n")
    return parsed


def main():
    import argparse

    parser = argparse.ArgumentParser(description="SachinQuant Windows Live Scheduler Setup")
    parser.add_argument("--query", action="store_true", help="Query status of the live scheduled task")
    parser.add_argument("--delete", action="store_true", help="Delete the live scheduled task")
    args = parser.parse_args()

    if args.delete:
        cmd = ["schtasks", "/delete", "/tn", TASK_NAME, "/f"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            print(f"[OK] Task '{TASK_NAME}' deleted successfully.")
        else:
            print(f"[ERROR] Delete failed: {res.stderr.strip()}")
        return

    if args.query:
        query_task()
        return

    # Standard registration workflow
    ensure_batch_script()
    success = register_task_com()
    if not success:
        sys.exit(1)

    # Verify task immediately
    info = query_task()
    if not info:
        sys.exit(1)

    next_run = info.get("Next Run Time", "")
    print(f"[VERIFIED] Next scheduled execution: {next_run}")


if __name__ == "__main__":
    main()
