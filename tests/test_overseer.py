"""
Unit tests for Overseer Agent Static Invariant Auditor
Tests: tests/test_overseer.py
"""

import subprocess
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.overseer import StaticInvariantAuditor, AuditResult


def test_overseer_static_audit_passes_on_current_codebase():
    """
    Verifies that running StaticInvariantAuditor on current codebase passes all checks:
    1. MAX_PERMITTED_SPREAD_RISK_INR <= 1500.0
    2. BUY leg precedes SELL leg (Broker margin safety)
    3. SQLite WAL mode in bus.py
    """
    auditor = StaticInvariantAuditor()
    results = auditor.run_full_audit()

    assert len(results) == 3
    for r in results:
        assert r.passed is True, f"Invariant check failed: {r.name} - {r.message}"


def test_overseer_cli_audit_command_exit_code():
    """
    Verifies that running `python agents/overseer.py --audit` outputs clean scorecard and returns exit code 0.
    """
    repo_root = Path(__file__).parent.parent
    proc = subprocess.run(
        [sys.executable, "agents/overseer.py", "--audit"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0
    stdout = proc.stdout
    assert "OVERSEER AGENT: STATIC INVARIANT AUDIT SCORECARD" in stdout
    assert "TOTAL INVARIANTS: 3 | PASSED: 3 | FAILED: 0" in stdout
    assert "ALL INVARIANTS SATISFIED [PASS]" in stdout


def test_overseer_detects_risk_ceiling_breach(tmp_path: Path):
    """
    Verifies auditor flags violation if MAX_PERMITTED_SPREAD_RISK_INR > 1500.0.
    """
    fake_agents = tmp_path / "agents"
    fake_agents.mkdir(parents=True)
    fake_coder = fake_agents / "coder.py"
    fake_coder.write_text("MAX_PERMITTED_SPREAD_RISK_INR = 2500.0\n", encoding="utf-8")

    auditor = StaticInvariantAuditor(root_dir=tmp_path)
    res = auditor.audit_coder_max_risk()
    assert res.passed is False
    assert "exceeds maximum ceiling of 1500.0 INR" in res.message


def test_overseer_detects_inverted_leg_ordering(tmp_path: Path):
    """
    Verifies auditor flags violation if SELL leg precedes BUY leg.
    """
    fake_agents = tmp_path / "agents"
    fake_agents.mkdir(parents=True)
    fake_coder = fake_agents / "coder.py"
    fake_code = """
legs = [
    {"action": "SELL", "strike": 25000},
    {"action": "BUY", "strike": 24950},
]
"""
    fake_coder.write_text(fake_code, encoding="utf-8")

    auditor = StaticInvariantAuditor(root_dir=tmp_path)
    res = auditor.audit_coder_leg_ordering()
    assert res.passed is False
    assert "violates margin order" in res.message


def test_overseer_detects_missing_wal_mode(tmp_path: Path):
    """
    Verifies auditor flags violation if bus.py lacks PRAGMA journal_mode = WAL.
    """
    fake_bus = tmp_path / "bus.py"
    fake_bus.write_text("conn.execute('PRAGMA journal_mode = DELETE;')\n", encoding="utf-8")

    auditor = StaticInvariantAuditor(root_dir=tmp_path)
    res = auditor.audit_bus_wal_mode()
    assert res.passed is False
    assert "not found in bus.py" in res.message
