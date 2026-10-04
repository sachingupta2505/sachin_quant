"""
Unit tests for agents/master_executive.py (Master Executive Agent)
Tests:
1. Telemetry aggregation & system_state.json persistence across all bus topics.
2. Multi-agent supervision and automatic dead-worker resurrection.
3. Strict 5-gated Telegram alert routing.
4. Post-market expectancy calculation, EOD evolution diagnostic generation, and regression audit.
"""

import json
import os
import threading
import time
from pathlib import Path
import pytest

from bus import SystemBus, EventStatus
from agents.master_executive import MasterExecutiveAgent


@pytest.fixture
def temp_env(tmp_path):
    db_file = tmp_path / "test_bus.db"
    state_file = tmp_path / "test_system_state.json"
    journal_file = tmp_path / "test_journal.db"
    reports_dir = tmp_path / "test_reports"

    agent = MasterExecutiveAgent(
        db_path=db_file,
        state_file=state_file,
        journal_db=journal_file,
        reports_dir=reports_dir,
        dry_run=True,
    )
    # Mock notifier network calls
    agent.notifier.send_message = lambda msg, parse_mode="HTML": None
    agent.notifier.send_message_sync = lambda msg, parse_mode="HTML": {"ok": True}
    return agent, db_file, state_file, reports_dir


def test_telemetry_aggregation_and_state_persistence(temp_env):
    """Verify that MasterExecutiveAgent observes all bus events and persists system_state.json."""
    agent, db_file, state_file, _ = temp_env
    bus = agent.bus

    # Publish events across diverse topics
    bus.publish("SIGNAL_DETECTED", "Architect", {"description": "Hammer at Support"}, target="Coder")
    bus.publish("ORDER_PROPOSED", "Coder", {"trade_id": "T1", "spread_type": "BULL_PUT_SPREAD", "max_risk_inr": 800.0}, target="Auditor")
    bus.publish("ORDER_APPROVED", "Auditor", {"trade_id": "T1"}, target="DevOps")
    bus.publish("ORDER_EXECUTED", "DevOps", {"trade_id": "T1", "spread_type": "BULL_PUT_SPREAD", "max_risk_inr": 800.0}, target="Auditor")
    bus.publish("ORDER_BLOCKED", "Auditor", {"trade_id": "T2", "reason": "Loss limit breached"}, target="Notifier")
    bus.publish("INITIAL_BALANCE_LOCKED", "LiveFeed", {"ib_high": 25200.0, "ib_low": 25000.0}, target="ALL")

    # Ingest telemetry
    records = agent.observe_bus()
    assert len(records) == 6
    assert agent.metrics["signals_detected"] == 1
    assert agent.metrics["orders_proposed"] == 1
    assert agent.metrics["orders_approved"] == 1
    assert agent.metrics["orders_executed"] == 1
    assert agent.metrics["orders_blocked"] == 1
    assert agent.metrics["ib_locked"] is True
    assert agent.metrics["ib_high"] == 25200.0

    # Verify system_state.json
    assert state_file.exists()
    state_data = json.loads(state_file.read_text(encoding="utf-8"))
    assert state_data["metrics"]["orders_executed"] == 1
    assert len(state_data["recent_events"]) == 6


def test_multi_agent_supervision_and_auto_restart(temp_env):
    """Verify supervisor detects dead worker threads and automatically resurrects them."""
    agent, _, _, _ = temp_env

    # Define controllable workers
    stop_flags = {"WorkerA": False, "WorkerB": False}

    def make_worker(name):
        def worker_fn():
            while not stop_flags[name]:
                time.sleep(0.01)
        return threading.Thread(target=worker_fn, name=name)

    factories = {
        "WorkerA": lambda: make_worker("WorkerA"),
        "WorkerB": lambda: make_worker("WorkerB"),
    }

    # Spawn workers
    agent.spawn_workers(factories)
    assert agent.threads["WorkerA"].is_alive()
    assert agent.threads["WorkerB"].is_alive()

    # Kill WorkerA
    stop_flags["WorkerA"] = True
    agent.threads["WorkerA"].join(timeout=1.0)
    assert not agent.threads["WorkerA"].is_alive()

    # Supervise - should detect dead WorkerA and resurrect it
    stop_flags["WorkerA"] = False  # Reset flag for new thread
    agent.supervise_workers(factories)

    assert agent.threads["WorkerA"].is_alive()
    assert agent.threads["WorkerB"].is_alive()

    # Cleanup
    stop_flags["WorkerA"] = True
    stop_flags["WorkerB"] = True
    agent.stop_event.set()


def test_strictly_5_gated_alerts(temp_env):
    """Verify that MasterExecutiveAgent strictly gates alerts and avoids duplicate transmissions."""
    agent, _, _, _ = temp_env

    # Test each of the 5 gates
    assert agent.send_gated_alert("BOOT", "🚀 System Live & Broker Connected") is True
    assert agent.send_gated_alert("BOOT", "Duplicate Boot") is False  # Gate locked

    assert agent.send_gated_alert("IB_RANGE", "📊 Initial Balance Set") is True
    assert agent.send_gated_alert("IB_RANGE", "Duplicate IB") is False

    assert agent.send_gated_alert("FILL_T1", "🎯 Order Executed: T1") is True
    assert agent.send_gated_alert("FILL_T1", "Duplicate Fill") is False

    assert agent.send_gated_alert("SQUARE_OFF", "🔒 All Positions Auto Squared-Off") is True
    assert agent.send_gated_alert("SQUARE_OFF", "Duplicate Square Off") is False

    assert agent.send_gated_alert("EOD_SUMMARY", "🏁 EOD Summary") is True
    assert agent.send_gated_alert("EOD_SUMMARY", "Duplicate EOD") is False

    assert len(agent.gated_alerts_sent) == 5


def test_post_market_analysis_and_eod_evolution(temp_env):
    """Verify EOD post-market analysis calculates expectancy, drafts remediation, and audits regression."""
    agent, _, state_file, reports_dir = temp_env

    # Add mock rejection to agent
    agent.rejection_events.append({"payload": {"reason": "Max risk INR 1600.0 > 1500 INR"}})

    report = agent.run_post_market_analysis()

    assert "expectancy_metrics" in report
    metrics = report["expectancy_metrics"]
    assert "win_rate_pct" in metrics
    assert "profit_factor" in metrics
    assert metrics["invariant_ceiling_inr"] == 1500.0
    assert metrics["drawdown_ceiling_respected"] is True

    # Weaknesses identified
    assert len(report["flagged_weaknesses"]) >= 1
    assert any("Auditor rejections detected" in w for w in report["flagged_weaknesses"])

    # Code enhancement prompt drafted
    prompt = report["prioritized_enhancement_prompt"]
    assert "PRIORITIZED AUTONOMOUS EVOLUTION PROMPT" in prompt
    assert "Refine Coder option-spread width" in prompt

    # Regression audit
    assert report["regression_audit"]["status"] == "PASS"
    assert report["regression_audit"]["total_checks"] == 9
    assert report["regression_audit"]["passed_checks"] == 9

    # Verify JSON file written to reports_dir
    files = list(reports_dir.glob("eod_evolution_*.json"))
    assert len(files) == 1


def test_chat_id_resolution_no_dummy_fallback(tmp_path, monkeypatch):
    """Verify that if resolve_chat_id_for_user fails and TELEGRAM_CHAT_ID is dummy or empty, chat_id is None."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "6711295622")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agents.master_executive.resolve_chat_id_for_user", lambda **kwargs: None)
        agent = MasterExecutiveAgent(
            db_path=tmp_path / "bus.db",
            state_file=tmp_path / "state.json",
            journal_db=tmp_path / "journal.db",
            reports_dir=tmp_path / "reports",
            dry_run=True,
        )
        assert agent.chat_id is None


def test_supervision_with_six_workers(temp_env):
    """Verify that MasterExecutiveAgent supervises all 6 agent worker threads."""
    agent, _, _, _ = temp_env

    stop_flags = {
        "Architect": False,
        "Coder": False,
        "Auditor": False,
        "DevOps": False,
        "Notifier": False,
        "LiveMarketFeed": False,
    }

    factories = {
        name: (lambda n=name: threading.Thread(
            target=lambda: [time.sleep(0.01) for _ in iter(lambda: stop_flags[n], True)],
            name=f"{n}Worker"
        ))
        for name in stop_flags
    }

    agent.spawn_workers(factories)
    assert len(agent.threads) == 6
    for name in stop_flags:
        assert agent.threads[name].is_alive()

    # Kill LiveMarketFeed worker
    stop_flags["LiveMarketFeed"] = True
    agent.threads["LiveMarketFeed"].join(timeout=1.0)
    assert not agent.threads["LiveMarketFeed"].is_alive()

    # Resurrect
    stop_flags["LiveMarketFeed"] = False
    agent.supervise_workers(factories)
    assert agent.threads["LiveMarketFeed"].is_alive()

    # Cleanup
    for n in stop_flags:
        stop_flags[n] = True
    agent.stop_event.set()

