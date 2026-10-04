"""
Tests for automation/scheduler_setup.py and run_trading_day.bat
Validates Windows Task Scheduler task definition, triggers, batch script, and querying.
"""

import os
import subprocess
from pathlib import Path
import pytest

from automation.scheduler_setup import (
    TASK_NAME,
    PROJECT_DIR,
    BAT_SCRIPT,
    START_TIME_STR,
    EXECUTION_LIMIT,
    DAYS_OF_WEEK_MON_FRI,
    ensure_batch_script,
    query_task,
)


def test_batch_script_structure():
    """Verify that run_trading_day.bat exists and contains required execution commands."""
    ensure_batch_script()
    assert os.path.exists(BAT_SCRIPT), f"{BAT_SCRIPT} must exist"

    with open(BAT_SCRIPT, "r", encoding="utf-8") as f:
        content = f.read()

    # Must navigate to project root directory
    assert "cd /d" in content or "cd " in content
    assert "sachin_quant" in content

    # Must check or activate virtual environment
    assert ".venv" in content

    # Must redirect output to logs/daily_trading_%DATE%.log
    assert "main_runner.py" in content
    assert "logs\\daily_trading_%DATE%.log" in content or "logs/daily_trading_%DATE%.log" in content


def test_scheduler_constants():
    """Verify schedule parameters adhere strictly to specifications."""
    assert TASK_NAME == "SachinQuant_LiveScheduler"
    assert START_TIME_STR == "09:14:00"
    assert EXECUTION_LIMIT == "PT6H30M"  # 6.5 hours
    # Mon (2) + Tue (4) + Wed (8) + Thu (16) + Fri (32) = 62
    assert DAYS_OF_WEEK_MON_FRI == 62


def test_task_scheduler_registration_and_query():
    """Verify task is registered in Windows Task Scheduler and next run time is set for Monday at 09:14 IST."""
    task_info = query_task(TASK_NAME)
    assert task_info is not None, f"Task {TASK_NAME} should be registered in Task Scheduler"

    assert task_info.get("Status") == "Ready"
    assert "09:14" in task_info.get("Start Time", "") or "9.14" in task_info.get("Start Time", "")

    # Next run time should contain 9.14 / 09:14
    next_run = task_info.get("Next Run Time", "")
    assert "9.14" in next_run or "09:14" in next_run

    # Stop task after 6.5 hours
    stop_limit = task_info.get("Stop Task If Runs X Hours and X Mins", "")
    assert "06:30:00" in stop_limit or "6:30" in stop_limit

    # Days should include MON, TUE, WED, THU, FRI
    days = task_info.get("Days", "")
    assert "MON" in days and "FRI" in days
