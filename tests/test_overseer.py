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
    Verifies that running StaticInvariantAuditor on current codebase passes all 9 checks:
    1. MAX_PERMITTED_SPREAD_RISK_INR <= 1500.0 (Coder)
    2. BUY leg precedes SELL leg (Coder - Broker margin safety)
    3. Hard daily loss limit <= -1500.0 INR (Auditor)
    4. Maximum daily trades <= 2 (Auditor)
    5. time_gates.entry_end == '15:05' & square_off == '15:10' (Config)
    6. Expiry day gamma cutoff 13:30 IST (Architect)
    7. Orders strictly routed as LIMIT (DevOps)
    8. Bid-Ask spread guard <= 10% (DevOps)
    9. SQLite WAL mode in bus.py (Bus)
    """
    auditor = StaticInvariantAuditor()
    results = auditor.run_full_audit()

    assert len(results) == 9
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
    assert "TOTAL INVARIANTS: 9 | PASSED: 9 | FAILED: 0" in stdout
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


def test_overseer_detects_daily_loss_limit_breach(tmp_path: Path):
    """
    Verifies auditor flags violation if MAX_DAILY_LOSS_INR in auditor.py breaches -1500.0 limit.
    """
    fake_agents = tmp_path / "agents"
    fake_agents.mkdir(parents=True)
    fake_auditor = fake_agents / "auditor.py"
    fake_auditor.write_text("MAX_DAILY_LOSS_INR: float = -3000.0\nif self.risk_guard.total_pnl <= MAX_DAILY_LOSS_INR: pass\n", encoding="utf-8")

    auditor = StaticInvariantAuditor(root_dir=tmp_path)
    res = auditor.audit_auditor_daily_loss_limit()
    assert res.passed is False
    assert "breaches hard invariant of -1500.0 INR" in res.message


def test_overseer_detects_max_trades_breach(tmp_path: Path):
    """
    Verifies auditor flags violation if MAX_DAILY_TRADES in auditor.py exceeds 2 trades/day.
    """
    fake_agents = tmp_path / "agents"
    fake_agents.mkdir(parents=True)
    fake_auditor = fake_agents / "auditor.py"
    fake_auditor.write_text("MAX_DAILY_TRADES: int = 5\nif self.risk_guard.trade_count >= MAX_DAILY_TRADES: pass\n", encoding="utf-8")

    auditor = StaticInvariantAuditor(root_dir=tmp_path)
    res = auditor.audit_auditor_max_daily_trades()
    assert res.passed is False
    assert "exceeds hard cap of 2 trades/day" in res.message


def test_overseer_detects_time_gate_breach(tmp_path: Path):
    """
    Verifies auditor flags violation if time_gates in config.json breach 15:05 or 15:10.
    """
    fake_config = tmp_path / "config.json"
    fake_config.write_text('{"time_gates": {"entry_end": "15:20", "square_off": "15:30"}}', encoding="utf-8")

    auditor = StaticInvariantAuditor(root_dir=tmp_path)
    res = auditor.audit_config_time_gates()
    assert res.passed is False
    assert "expected strictly '15:05'" in res.message


def test_overseer_detects_missing_expiry_gamma_cutoff(tmp_path: Path):
    """
    Verifies auditor flags violation if agents/architect.py lacks EXPIRY_CUTOFF_TIME = time(13, 30).
    """
    fake_agents = tmp_path / "agents"
    fake_agents.mkdir(parents=True)
    fake_arch = fake_agents / "architect.py"
    fake_arch.write_text("class ArchitectAgent: pass\n", encoding="utf-8")

    auditor = StaticInvariantAuditor(root_dir=tmp_path)
    res = auditor.audit_architect_expiry_gamma_cutoff()
    assert res.passed is False
    assert "EXPIRY_CUTOFF_TIME = time(13, 30) not defined" in res.message


def test_overseer_detects_market_order_violation(tmp_path: Path):
    """
    Verifies auditor flags violation if agents/devops.py routes unconstrained MARKET orders.
    """
    fake_agents = tmp_path / "agents"
    fake_agents.mkdir(parents=True)
    fake_devops = fake_agents / "devops.py"
    fake_devops.write_text('DEFAULT_ORDER_TYPE = "MARKET"\norder_params = {"ordertype": "MARKET"}\n', encoding="utf-8")

    auditor = StaticInvariantAuditor(root_dir=tmp_path)
    res = auditor.audit_devops_limit_order_safety()
    assert res.passed is False
    assert "MARKET order routing detected" in res.message or "LIMIT order type invariant not found" in res.message


def test_overseer_detects_excessive_spread_ratio(tmp_path: Path):
    """
    Verifies auditor flags violation if MAX_BID_ASK_SPREAD_RATIO > 10% in devops.py.
    """
    fake_agents = tmp_path / "agents"
    fake_agents.mkdir(parents=True)
    fake_devops = fake_agents / "devops.py"
    fake_devops.write_text('MAX_BID_ASK_SPREAD_RATIO: float = 0.25\ndef check_hedge_liquidity_guard(): pass\n', encoding="utf-8")

    auditor = StaticInvariantAuditor(root_dir=tmp_path)
    res = auditor.audit_devops_bid_ask_spread_guard()
    assert res.passed is False
    assert "must be <= 0.10" in res.message


def test_overseer_cli_next_command_returns_actionable_prompt():
    """
    Verifies that running `python agents/overseer.py --next` exits with code 0
    and returns an actionable development prompt for the next roadmap milestone.
    """
    repo_root = Path(__file__).parent.parent
    proc = subprocess.run(
        [sys.executable, "agents/overseer.py", "--next"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0
    stdout = proc.stdout
    assert "AUTONOMOUS TASK GENERATOR: NEXT DEVELOPMENT ROADMAP TASK" in stdout
    assert "ALL SYSTEM INVARIANTS SATISFIED [PASS]" in stdout
    assert "[X] Milestone 1: End-to-end integration test" in stdout
    assert "[ ] Milestone 2: Paper trading replay engine with historical 1-minute tick data" in stdout
    assert "READY-TO-RUN PROMPT FOR NEXT MILESTONE (Milestone 2):" in stdout
    assert "replay_engine.py" in stdout
    assert "ReplayEngine" in stdout


def test_overseer_cli_next_command_emits_prioritized_remediation_on_failure(tmp_path: Path):
    """
    Verifies that when any invariant fails, generate_next_task returns exit code 1
    and prints a prioritized remediation prompt specifying the exact file and lines to fix.
    """
    fake_agents = tmp_path / "agents"
    fake_agents.mkdir(parents=True)
    fake_coder = fake_agents / "coder.py"
    fake_coder.write_text(
        "# Header\n# Comment\nMAX_PERMITTED_SPREAD_RISK_INR = 3500.0\n",
        encoding="utf-8",
    )

    auditor = StaticInvariantAuditor(root_dir=tmp_path)
    prompt, code = auditor.generate_next_task()

    assert code == 1
    assert "OVERSEER AGENT: INVARIANT AUDIT FAILED - REMEDIATION REQUIRED" in prompt
    assert "PRIORITIZED REMEDIATION PROMPT:" in prompt
    assert "Target File:" in prompt
    assert str(fake_coder) in prompt or "coder.py" in prompt
    assert "Line 3" in prompt  # Exact line number verified!
    assert "exceeds maximum ceiling of 1500.0 INR" in prompt


def test_overseer_milestone_evaluation_progression(tmp_path: Path):
    """
    Verifies evaluation of milestones 1, 2, and 3 based on codebase files.
    """
    auditor = StaticInvariantAuditor(root_dir=tmp_path)
    milestones = auditor.evaluate_roadmap_milestones()
    assert len(milestones) == 3
    assert milestones[0].completed is False  # No integration test yet in tmp_path

    # Complete Milestone 1 in tmp_path
    m1_file = tmp_path / "test_blackboard_integration.py"
    m1_file.write_text(
        "from bus import SystemBus\ndef test_blackboard_end_to_end_flow(): pass\n",
        encoding="utf-8",
    )
    milestones = auditor.evaluate_roadmap_milestones()
    assert milestones[0].completed is True
    assert milestones[1].completed is False

    next_m = auditor.get_next_uncompleted_milestone()
    assert next_m is not None
    assert next_m.id == 2

    # Complete Milestone 2 in tmp_path
    m2_file = tmp_path / "replay_engine.py"
    m2_file.write_text("class ReplayEngine: pass\n", encoding="utf-8")
    milestones = auditor.evaluate_roadmap_milestones()
    assert milestones[1].completed is True
    assert milestones[2].completed is False

    next_m = auditor.get_next_uncompleted_milestone()
    assert next_m is not None
    assert next_m.id == 3
