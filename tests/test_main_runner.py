"""
Unit Tests for Multi-Agent System Runner (main_runner.py)
Tests: tests/test_main_runner.py
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(repo_root))

from main_runner import CandleAggregator

IST = ZoneInfo("Asia/Kolkata")


def test_main_runner_cli_dry_run_flag():
    """
    Verifies that running `python main_runner.py --dry-run`:
    1. Starts all 4 agents (Architect, Coder, Auditor, DevOps) concurrently using threads.
    2. Publishes dummy test signal to bus.py.
    3. Verifies all 4 stages: Architect emits signal -> Coder creates spread -> Auditor approves -> DevOps receives order.
    4. Prints '[SUCCESS] All 4 agents communicated cleanly via bus.py' and exits with code 0.
    """
    proc = subprocess.run(
        [sys.executable, "main_runner.py", "--dry-run"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, f"Process failed with stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}"
    stdout = proc.stdout

    # Verify all 4 stages logged as verified
    assert "[VERIFIED] Architect emits signal" in stdout
    assert "[VERIFIED] Coder creates spread" in stdout
    assert "[VERIFIED] Auditor approves" in stdout
    assert "[VERIFIED] DevOps receives order" in stdout

    # Verify required success message
    assert "[SUCCESS] All 4 agents communicated cleanly via bus.py" in stdout


def test_main_runner_cli_check_broker_flag():
    """
    Verifies that running `python main_runner.py --check-broker`:
    1. Validates Angel One API credentials and dynamic TOTP generation.
    2. Successfully receives JWT session token.
    3. Fetches live NIFTY 50 quote from NSE.
    4. Exits with code 0 and logs pre-flight success.
    """
    proc = subprocess.run(
        [sys.executable, "main_runner.py", "--check-broker"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, f"Process failed with stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}"
    stdout = proc.stdout

    assert "ANGEL ONE SMARTAPI PRE-FLIGHT CONNECTIVITY & QUOTE CHECK" in stdout
    assert "JWT Session Token Generated" in stdout
    assert "Nifty 50" in stdout
    assert "LTP:" in stdout
    assert "[PRE-FLIGHT SUCCESS] Angel One Broker & Live Feed Connection VERIFIED READY" in stdout


def test_candle_aggregator_rollover():
    """
    Verifies 5-minute candle aggregation and rollover mechanics.
    """
    aggregator = CandleAggregator(interval_minutes=5)
    t1 = datetime(2026, 10, 5, 9, 16, 0, tzinfo=IST)
    c1 = aggregator.on_tick(25000.0, t1)
    assert c1 is None  # Initial tick starts candle

    t2 = datetime(2026, 10, 5, 9, 18, 0, tzinfo=IST)
    c2 = aggregator.on_tick(25050.0, t2)
    assert c2 is None
    assert aggregator.high == 25050.0

    # Cross 09:20 slot boundary
    t3 = datetime(2026, 10, 5, 9, 20, 5, tzinfo=IST)
    closed = aggregator.on_tick(25030.0, t3)
    assert closed is not None
    assert closed.timestamp == datetime(2026, 10, 5, 9, 15, 0, tzinfo=IST)
    assert closed.open == 25000.0
    assert closed.high == 25050.0
    assert closed.close == 25050.0
