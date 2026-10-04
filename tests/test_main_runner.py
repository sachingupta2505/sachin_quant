"""
Unit Tests for Multi-Agent System Runner (main_runner.py)
Tests: tests/test_main_runner.py
"""

import subprocess
import sys
from pathlib import Path

repo_root = Path(__file__).parent.parent


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
