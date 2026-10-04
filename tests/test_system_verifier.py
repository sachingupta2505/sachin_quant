"""
Unit and Integration Tests for SystemVerifier
Tests: tests/test_system_verifier.py
"""

import json
import subprocess
import sys
from pathlib import Path
import pytest

repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(repo_root))

from agents.system_verifier import SystemVerifier, DiagnosticCheck


def test_system_verifier_all_suites_pass_on_current_codebase():
    """
    Verifies that running SystemVerifier on the codebase passes all 12 checks across the 4 suites:
    Suite A (Greeks & Volatility): VIX limit, Gamma cutoff, Theta credit capture
    Suite B (Trader Failure Traps): Hard daily loss limit, max trade cap, Initial Balance freeze
    Suite C (Microstructure Traps): Margin protection sequencing, LIMIT orders only, Bid-Ask liquidity guard
    Suite D (End-to-End Readiness): SQLite WAL mode, Angel One credentials in .env, Mandatory 15:10 square-off
    """
    verifier = SystemVerifier()
    checks = verifier.run_all_diagnostics()

    assert len(checks) == 12

    suite_a = [c for c in checks if c.suite.startswith("A")]
    suite_b = [c for c in checks if c.suite.startswith("B")]
    suite_c = [c for c in checks if c.suite.startswith("C")]
    suite_d = [c for c in checks if c.suite.startswith("D")]

    assert len(suite_a) == 3
    assert len(suite_b) == 3
    assert len(suite_c) == 3
    assert len(suite_d) == 3

    for c in checks:
        assert c.passed is True, f"Diagnostic check failed: {c.suite} - {c.name}: {c.details}"


def test_system_verifier_cli_execution():
    """
    Verifies that running `python agents/system_verifier.py` exits with code 0
    and outputs the clean Readiness Matrix and 'READY FOR 09:15 LIVE PAPER EXECUTION'.
    """
    proc = subprocess.run(
        [sys.executable, "agents/system_verifier.py"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, f"Process failed with stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}"
    stdout = proc.stdout

    assert "SACCHIN QUANT: PRE-FLIGHT SYSTEM VERIFIER & READINESS MATRIX" in stdout
    assert "A. Greeks & Volatility" in stdout
    assert "B. Trader Failure Traps" in stdout
    assert "C. Microstructure Traps" in stdout
    assert "D. End-to-End Readiness" in stdout
    assert "TOTAL DIAGNOSTIC CHECKS: 12 | PASSED: 12 | FAILED: 0" in stdout
    assert "VERDICT: READY FOR 09:15 LIVE PAPER EXECUTION" in stdout


def test_system_verifier_detects_volatility_violation(tmp_path: Path):
    """
    Verifies that missing or excessive MAX_INDIA_VIX trips Suite A check.
    """
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True)
    fake_arch = agents_dir / "architect.py"
    fake_arch.write_text("MAX_INDIA_VIX = 45.0\n", encoding="utf-8")

    verifier = SystemVerifier(root_dir=tmp_path)
    checks_a = verifier.check_suite_a()
    vix_check = next(c for c in checks_a if c.name == "India VIX / IV Expansion Limit")
    assert vix_check.passed is False


def test_system_verifier_detects_risk_trap_violation(tmp_path: Path):
    """
    Verifies that breaching daily loss limit or trade cap in auditor/config trips Suite B checks.
    """
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True)
    fake_auditor = agents_dir / "auditor.py"
    fake_auditor.write_text("MAX_DAILY_LOSS_INR = -5000.0\nMAX_DAILY_TRADES = 10\n", encoding="utf-8")

    fake_cfg = tmp_path / "config.json"
    fake_cfg.write_text(json.dumps({
        "time_gates": {"ib_end": "09:30", "entry_start": "09:30"},
        "risk_guardrails": {"hard_daily_loss_limit_inr": -5000.0, "max_daily_trades": 10},
    }), encoding="utf-8")

    verifier = SystemVerifier(root_dir=tmp_path)
    checks_b = verifier.check_suite_b()

    loss_check = next(c for c in checks_b if "Daily Loss" in c.name)
    trades_check = next(c for c in checks_b if "Trade Cap" in c.name)
    ib_check = next(c for c in checks_b if "Initial Balance" in c.name)

    assert loss_check.passed is False
    assert trades_check.passed is False
    assert ib_check.passed is False


def test_system_verifier_detects_microstructure_trap_violation(tmp_path: Path):
    """
    Verifies that missing margin sequencing or MARKET order usage trips Suite C checks.
    """
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True)
    fake_devops = agents_dir / "devops.py"
    fake_devops.write_text('DEFAULT_ORDER_TYPE = "MARKET"\nMAX_BID_ASK_SPREAD_RATIO = 0.25\n', encoding="utf-8")

    verifier = SystemVerifier(root_dir=tmp_path)
    checks_c = verifier.check_suite_c()

    margin_check = next(c for c in checks_c if "Margin Protection" in c.name)
    order_check = next(c for c in checks_c if "Execution Order Type" in c.name)
    spread_check = next(c for c in checks_c if "Bid-Ask Liquidity" in c.name)

    assert margin_check.passed is False
    assert order_check.passed is False
    assert spread_check.passed is False


def test_system_verifier_blocks_deployment_verdict(tmp_path: Path):
    """
    Verifies that print_readiness_matrix outputs 'VERDICT: BLOCKED' when any check fails.
    """
    verifier = SystemVerifier(root_dir=tmp_path)  # Empty directory will fail checks
    checks = verifier.run_all_diagnostics()
    ready = verifier.print_readiness_matrix(checks)

    assert ready is False
    assert any(not c.passed for c in checks)
