"""
Master Executive Agent for SachinQuant Autonomous Trading Engine
Module: agents/master_executive.py

Responsibilities:
1. Central Event Hub & Observer:
   - Subscribes to ALL topics on bus.py (SIGNAL, ORDER_PROPOSED, ORDER_APPROVED, ORDER_EXECUTED, VETO, EOD_METRICS).
   - Ingests telemetry into an in-memory ring buffer and persists system_state.json.
2. Multi-Agent Lifecycle Supervision (09:14 - 15:30 IST):
   - Spawns and supervises worker threads: Architect, Coder, Auditor, DevOps, Notifier.
   - Health monitoring & automated worker thread resurrection.
   - Programmatically resolves Telegram numeric chat ID for @shishilalapoopoo (BOT_TOKEN 8983716841:AAFr0G5IYyeq7eJaYzUwh5NZ0197G7c6YLI)
     and dispatches strictly 5 gated alerts (Boot, IB Range, Fills, Square-Off, EOD Summary).
3. Post-Market Continuous Analysis & Self-Evolution (15:30+ IST):
   - Ingests trade executions, slippages, and Auditor rejections.
   - Computes system expectancy: Win Rate, Profit Factor, Max Drawdown vs Invariant ceiling (INR 1500).
   - Generates automated EOD Evolution Diagnostic report (flagged weaknesses & queued code improvements).
   - Executes static invariant verification (`StaticInvariantAuditor().run_full_audit()`) for zero regression.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Ensure Windows console UTF-8 protection
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger("agent.master_executive")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from audit_logger import AuditLogger
from bus import EventStatus, SystemBus
from agents.notifier import TelegramNotifier, resolve_chat_id_for_user
from agents.overseer import StaticInvariantAuditor

TELEGRAM_BOT_TOKEN = "8983716841:AAFr0G5IYyeq7eJaYzUwh5NZ0197G7c6YLI"
TARGET_TELEGRAM_USER = "shishilalapoopoo"
MAX_PERMITTED_SPREAD_RISK_INR = 1500.0


class MasterExecutiveAgent:
    """
    Central autonomous brain and self-evolving executive orchestrator.
    Supervises multi-agent workflows, telemetry recording, gated alerts,
    and post-market self-evolution analysis.
    """

    def __init__(
        self,
        db_path: str | Path = "system_bus.db",
        state_file: str | Path = "system_state.json",
        journal_db: str | Path = "trading_journal.db",
        reports_dir: str | Path = "reports",
        dry_run: bool = False,
    ):
        self.db_path = Path(db_path)
        self.state_file = Path(state_file)
        self.journal_db = Path(journal_db)
        self.reports_dir = Path(reports_dir)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.dry_run = dry_run

        self.bus = SystemBus(db_path=self.db_path)
        self.audit_logger = AuditLogger(db_path=self.journal_db, tz=IST)
        self.overseer = StaticInvariantAuditor()

        # Telemetry & State Buffer
        self.telemetry_buffer: list[dict[str, Any]] = []
        self.last_observed_id: int = 0
        self.stop_event = threading.Event()
        self.threads: dict[str, threading.Thread] = {}

        # Gated Alerts Tracking (Strictly 5 Gated Alerts)
        self.gated_alerts_sent: set[str] = set()

        # Resolve Telegram Chat ID
        self.chat_id = resolve_chat_id_for_user(
            target_username=TARGET_TELEGRAM_USER,
            bot_token=TELEGRAM_BOT_TOKEN,
        ) or os.getenv("TELEGRAM_CHAT_ID", "6711295622")

        self.notifier = TelegramNotifier(
            bot_token=TELEGRAM_BOT_TOKEN,
            chat_id=self.chat_id,
        )

        # Telemetry counters
        self.metrics = {
            "total_events": 0,
            "signals_detected": 0,
            "orders_proposed": 0,
            "orders_approved": 0,
            "orders_executed": 0,
            "orders_blocked": 0,
            "daily_pnl": 0.0,
            "ib_high": 0.0,
            "ib_low": 0.0,
            "ib_locked": False,
        }
        self.rejection_events: list[dict[str, Any]] = []
        self.execution_events: list[dict[str, Any]] = []

    # -------------------------------------------------------------------------
    # 1. Central Event Hub & Observer
    # -------------------------------------------------------------------------
    def observe_bus(self) -> list[dict[str, Any]]:
        """
        Polls SQLite system_bus.db for ALL new events regardless of target.
        Appends to in-memory telemetry buffer and updates system_state.json.
        """
        new_records: list[dict[str, Any]] = []
        try:
            with self.bus._get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT id, timestamp, source_agent, target_agent, topic, payload, status
                    FROM bus_events
                    WHERE id > ?
                    ORDER BY id ASC
                    """,
                    (self.last_observed_id,),
                ).fetchall()

                for r in rows:
                    ev_id = r["id"]
                    self.last_observed_id = max(self.last_observed_id, ev_id)
                    try:
                        payload = json.loads(r["payload"])
                    except Exception:
                        payload = {"raw": r["payload"]}

                    event_entry = {
                        "id": ev_id,
                        "timestamp": r["timestamp"],
                        "source": r["source_agent"],
                        "target": r["target_agent"],
                        "topic": r["topic"],
                        "payload": payload,
                        "status": r["status"],
                    }
                    self.telemetry_buffer.append(event_entry)
                    new_records.append(event_entry)
                    self._process_telemetry_event(event_entry)

        except Exception as e:
            logger.warning(f"[OBSERVER] Error reading bus events: {e}")

        if new_records:
            self.persist_system_state()

        return new_records

    def _process_telemetry_event(self, ev: dict[str, Any]) -> None:
        """Updates internal metrics and routes gated lifecycle alerts."""
        topic = ev["topic"]
        payload = ev["payload"]
        self.metrics["total_events"] += 1

        if topic in ("SIGNAL_DETECTED", "TEST_SIGNAL"):
            self.metrics["signals_detected"] += 1

        elif topic == "ORDER_PROPOSED":
            self.metrics["orders_proposed"] += 1

        elif topic == "ORDER_APPROVED":
            self.metrics["orders_approved"] += 1

        elif topic in ("ORDER_EXECUTED", "ORDER_EXECUTED_ALERT"):
            self.metrics["orders_executed"] += 1
            self.execution_events.append(ev)
            # Route Gated Alert #3: Order Execution Fill
            trade_id = payload.get("trade_id", f"TRADE-{self.metrics['orders_executed']}")
            spread_type = payload.get("spread_type", "SPREAD")
            max_risk = float(payload.get("max_risk_inr", payload.get("max_risk", 0.0)))
            self.send_gated_alert(
                gate_key=f"FILL_{trade_id}",
                message=f"🎯 Order Executed: {spread_type} | Risk: INR {max_risk:.1f}",
            )

        elif topic in ("ORDER_BLOCKED", "ORDER_REJECTED", "VETO") or ev["status"] == "VETOED":
            self.metrics["orders_blocked"] += 1
            self.rejection_events.append(ev)

        elif topic in ("INITIAL_BALANCE_LOCKED", "IB_LOCKED"):
            self.metrics["ib_locked"] = True
            self.metrics["ib_high"] = float(payload.get("ib_high", self.metrics["ib_high"]))
            self.metrics["ib_low"] = float(payload.get("ib_low", self.metrics["ib_low"]))
            # Route Gated Alert #2: IB Range
            self.send_gated_alert(
                gate_key="IB_RANGE",
                message=f"📊 Initial Balance Set: High {self.metrics['ib_high']:.1f} | Low {self.metrics['ib_low']:.1f}",
            )

        elif topic in ("SYSTEM_LIVE", "SYSTEM_START"):
            # Route Gated Alert #1: Boot
            self.send_gated_alert(
                gate_key="BOOT",
                message="🚀 System Live & Broker Connected",
            )

        elif topic in ("POSITIONS_SQUARED_OFF", "SQUARE_OFF_ALERT"):
            # Route Gated Alert #4: Square-Off
            self.send_gated_alert(
                gate_key="SQUARE_OFF",
                message="🔒 All Positions Auto Squared-Off",
            )

        elif topic in ("EOD_SUMMARY", "DAILY_RECONCILIATION"):
            count = int(payload.get("count", payload.get("total_trades", self.metrics["orders_executed"])))
            pnl = float(payload.get("pnl", payload.get("daily_pnl", self.metrics["daily_pnl"])))
            # Route Gated Alert #5: EOD Summary
            self.send_gated_alert(
                gate_key="EOD_SUMMARY",
                message=f"🏁 EOD Summary: Total Trades: {count} | Daily PnL: INR {pnl:.1f}",
            )

    def persist_system_state(self, market_phase: str = "ACTIVE") -> None:
        """Atomically saves full telemetry state to system_state.json."""
        state_data = {
            "timestamp": datetime.now(IST).isoformat(),
            "market_phase": market_phase,
            "metrics": self.metrics,
            "agents_health": {
                name: "HEALTHY" if t.is_alive() else "STOPPED"
                for name, t in self.threads.items()
            },
            "gated_alerts_sent": list(self.gated_alerts_sent),
            "rejections_count": len(self.rejection_events),
            "executions_count": len(self.execution_events),
            "recent_events": self.telemetry_buffer[-10:],
        }
        try:
            temp_file = self.state_file.with_suffix(".tmp")
            temp_file.write_text(json.dumps(state_data, indent=2, default=str), encoding="utf-8")
            temp_file.replace(self.state_file)
        except Exception as e:
            logger.warning(f"[STATE] Error writing {self.state_file}: {e}")

    # -------------------------------------------------------------------------
    # 2. Multi-Agent Lifecycle Supervision (09:14 - 15:30 IST)
    # -------------------------------------------------------------------------
    def send_gated_alert(self, gate_key: str, message: str) -> bool:
        """
        Enforces strictly 5 gated lifecycle alert categories:
        1. Boot (09:14 IST)
        2. IB Range (09:45 IST)
        3. Fills (Order Execution)
        4. Square-Off (15:10 IST)
        5. EOD Summary (15:30 IST)
        """
        if gate_key in self.gated_alerts_sent:
            return False

        self.gated_alerts_sent.add(gate_key)
        logger.info(f"[GATED ALERT: {gate_key}] {message}")
        try:
            self.notifier.send_message(message)
            return True
        except Exception as e:
            logger.error(f"[GATED ALERT ERROR] Failed delivering '{message}': {e}")
            return False

    def spawn_workers(self, worker_factories: dict[str, Callable[[], threading.Thread]]) -> None:
        """Spawns worker threads and tracks them in supervision registry."""
        for name, factory in worker_factories.items():
            if name not in self.threads or not self.threads[name].is_alive():
                t = factory()
                t.daemon = True
                t.start()
                self.threads[name] = t
                logger.info(f"[SUPERVISOR] Spawned agent worker thread: {name}")

    def supervise_workers(self, worker_factories: dict[str, Callable[[], threading.Thread]]) -> None:
        """Monitors worker threads; auto-restarts failed agent workers."""
        if self.stop_event.is_set():
            return

        for name, factory in worker_factories.items():
            thread = self.threads.get(name)
            if thread is None or not thread.is_alive():
                logger.warning(f"[SUPERVISOR HEALTH ALERT] Thread '{name}' died unexpectedly! Auto-restarting...")
                new_thread = factory()
                new_thread.daemon = True
                new_thread.start()
                self.threads[name] = new_thread
                logger.info(f"[SUPERVISOR] Resurrected worker thread: {name}")

    # -------------------------------------------------------------------------
    # 3. Post-Market Continuous Analysis & Self-Evolution (15:30+ IST)
    # -------------------------------------------------------------------------
    def run_post_market_analysis(self, target_date: Optional[str] = None) -> dict[str, Any]:
        """
        Post-Market Analysis & Self-Evolution Engine:
        - Ingests day's trades, slippages, and Auditor rejections.
        - Evaluates system expectancy: Win Rate, Profit Factor, Max Drawdown vs Invariant ceiling (1500 INR).
        - Generates automated EOD Evolution Diagnostic report with flagged weaknesses.
        - Automatically drafts and queues prioritized code enhancement prompt.
        - Runs full invariant checks (agents/overseer.py --audit) for zero regression.
        """
        today_str = target_date or datetime.now(IST).strftime("%Y-%m-%d")
        daily_perf = self.audit_logger.get_daily_performance(today_str)

        # 1. Expectancy & Invariant Calculations
        trade_count = daily_perf.get("trade_count", 0)
        wins = daily_perf.get("wins", 0)
        losses = daily_perf.get("losses", 0)
        total_pnl = float(daily_perf.get("total_pnl", 0.0))
        win_rate = float(daily_perf.get("win_rate", 0.0))
        avg_slippage = float(daily_perf.get("avg_slippage", 0.0))
        avg_mae = float(daily_perf.get("avg_mae", 0.0))

        # Profit factor calculation
        gross_profit = total_pnl if total_pnl > 0 else 0.0
        gross_loss = abs(total_pnl) if total_pnl < 0 else 0.0
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (round(gross_profit, 2) if gross_profit > 0 else 1.0)

        # Max drawdown vs Invariant ceiling
        drawdown_ceiling = MAX_PERMITTED_SPREAD_RISK_INR
        drawdown_respected = abs(min(avg_mae, total_pnl)) <= drawdown_ceiling

        # 2. Flagged Weaknesses Identification
        flagged_weaknesses: list[str] = []
        if avg_slippage > 0.05:
            flagged_weaknesses.append(
                f"Elevated execution slippage: average {avg_slippage:.4f} pts exceeds 0.05 threshold."
            )
        if len(self.rejection_events) > 0:
            reasons = [r["payload"].get("reason", "Unknown") for r in self.rejection_events]
            flagged_weaknesses.append(
                f"Auditor rejections detected ({len(self.rejection_events)} vetoes): {', '.join(set(reasons))}."
            )
        if trade_count > 0 and win_rate < 50.0:
            flagged_weaknesses.append(
                f"Sub-optimal win rate ({win_rate:.1f}%): possible false breakout near Initial Balance boundaries."
            )
        if not drawdown_respected:
            flagged_weaknesses.append(
                f"Drawdown breach warning: loss of {abs(total_pnl)} INR exceeded ceiling {drawdown_ceiling} INR."
            )
        if not flagged_weaknesses:
            flagged_weaknesses.append("No critical weaknesses detected. All runtime metrics within baseline tolerances.")

        # 3. Automated Code Enhancement / Remediation Prompt Draft
        enhancement_prompt = self._draft_enhancement_prompt(
            flagged_weaknesses=flagged_weaknesses,
            win_rate=win_rate,
            avg_slippage=avg_slippage,
            rejections_count=len(self.rejection_events),
        )

        # 4. Zero Regression Audit Check
        audit_results = self.overseer.run_full_audit()
        failed_audits = [r for r in audit_results if not r.passed]
        audit_status = "PASS" if not failed_audits else "FAIL"

        diagnostic_report = {
            "date": today_str,
            "timestamp": datetime.now(IST).isoformat(),
            "expectancy_metrics": {
                "trade_count": trade_count,
                "wins": wins,
                "losses": losses,
                "win_rate_pct": win_rate,
                "profit_factor": profit_factor,
                "total_pnl_inr": total_pnl,
                "avg_slippage_pts": avg_slippage,
                "avg_mae_inr": avg_mae,
                "invariant_ceiling_inr": drawdown_ceiling,
                "drawdown_ceiling_respected": drawdown_respected,
            },
            "flagged_weaknesses": flagged_weaknesses,
            "prioritized_enhancement_prompt": enhancement_prompt,
            "regression_audit": {
                "status": audit_status,
                "total_checks": len(audit_results),
                "passed_checks": len(audit_results) - len(failed_audits),
                "failures": [r.name for r in failed_audits],
            },
        }

        # Save Report Artifact
        report_file = self.reports_dir / f"eod_evolution_{today_str}.json"
        report_file.write_text(json.dumps(diagnostic_report, indent=2), encoding="utf-8")
        logger.info(f"[EOD EVOLUTION] Diagnostic report saved to {report_file}")

        # Update system_state with EOD Diagnostic
        self.persist_system_state(market_phase="POST_MARKET_EVOLUTION")
        return diagnostic_report

    def _draft_enhancement_prompt(
        self,
        flagged_weaknesses: list[str],
        win_rate: float,
        avg_slippage: float,
        rejections_count: int,
    ) -> str:
        """Drafts prioritized code enhancement/remediation prompt for next development cycle."""
        tasks = []
        if avg_slippage > 0.05:
            tasks.append(
                "Optimize DevOps limit-order placement: tune bid-ask offset to prevent fill slippage on hedge legs."
            )
        if rejections_count > 0:
            tasks.append(
                "Refine Coder option-spread width: ensure proposed max risk is strictly padded below 1500 INR threshold."
            )
        if win_rate < 50.0:
            tasks.append(
                "Enhance Architect zone filter: introduce 15-minute volume confirmation before emitting breakout signals."
            )
        if not tasks:
            tasks.append("Conduct historical tick replay calibration for dynamic ATM strike selection.")

        formatted_tasks = "\n".join(f"{i+1}. {t}" for i, t in enumerate(tasks))
        return (
            "================================================================================\n"
            "PRIORITIZED AUTONOMOUS EVOLUTION PROMPT (NEXT DEVELOPMENT CYCLE):\n"
            "================================================================================\n"
            f"Observed Diagnostics:\n"
            f"- Win Rate: {win_rate:.1f}%\n"
            f"- Average Slippage: {avg_slippage:.4f} pts\n"
            f"- Auditor Rejections: {rejections_count}\n\n"
            f"Action Items:\n{formatted_tasks}\n"
            "================================================================================"
        )

    # -------------------------------------------------------------------------
    # 4. Main Autonomous Execution Lifecycle
    # -------------------------------------------------------------------------
    def run(self) -> bool:
        """
        Executes MasterExecutiveAgent lifecycle:
        - Boot & Broker pre-flight check
        - Concurrent agent supervision & observer loop
        - EOD analysis & self-evolution
        """
        from main_runner import (
            architect_worker,
            auditor_worker,
            check_broker_connectivity,
            coder_worker,
            devops_worker,
            notifier_worker,
            run_dry_run,
        )

        logger.info("=" * 70)
        logger.info("   SACHIN QUANT: MASTER EXECUTIVE AUTONOMOUS ORCHESTRATOR")
        logger.info("=" * 70)

        # In Dry-Run mode, delegate to pipeline verification
        if self.dry_run:
            logger.info("[MODE: DRY-RUN] Executing multi-agent dry-run pipeline...")
            success = run_dry_run(db_path=str(self.db_path))
            self.observe_bus()
            self.run_post_market_analysis()
            return success

        # 1. Pre-flight broker connection check
        logger.info("[PRE-FLIGHT] Verifying Angel One SmartAPI connection & live quote...")
        if not check_broker_connectivity():
            logger.error("[FATAL] Broker connectivity check failed! Aborting lifecycle.")
            return False

        # 2. Boot Gated Alert #1
        self.send_gated_alert("BOOT", "🚀 System Live & Broker Connected")

        # 3. Setup multi-agent worker factories for supervision
        worker_factories = {
            "Architect": lambda: threading.Thread(
                target=architect_worker,
                args=(SystemBus(db_path=self.db_path), self.stop_event),
                name="ArchitectWorker",
            ),
            "Coder": lambda: threading.Thread(
                target=coder_worker,
                args=(SystemBus(db_path=self.db_path), self.stop_event),
                name="CoderWorker",
            ),
            "Auditor": lambda: threading.Thread(
                target=auditor_worker,
                args=(SystemBus(db_path=self.db_path), self.stop_event, None, "daily_state.json", str(self.journal_db)),
                name="AuditorWorker",
            ),
            "DevOps": lambda: threading.Thread(
                target=devops_worker,
                args=(SystemBus(db_path=self.db_path), self.stop_event, None, True),
                name="DevOpsWorker",
            ),
            "Notifier": lambda: threading.Thread(
                target=notifier_worker,
                args=(SystemBus(db_path=self.db_path), self.stop_event, self.notifier),
                name="NotifierWorker",
            ),
        }

        self.spawn_workers(worker_factories)
        logger.info("[SUPERVISOR] All 5 multi-agent workers active under MasterExecutive supervision.")

        # 4. Main Market Polling & Supervision Loop
        try:
            while not self.stop_event.is_set():
                now = datetime.now(IST)
                current_time = now.time()

                # Ingest telemetry from bus.py
                self.observe_bus()

                # Health check & supervision
                self.supervise_workers(worker_factories)

                # Time Gate: 15:10 IST Square-Off
                if current_time >= dtime(15, 10):
                    self.send_gated_alert("SQUARE_OFF", "🔒 All Positions Auto Squared-Off")

                # Time Gate: 15:30 IST Post-Market Close & Evolution Trigger
                if current_time >= dtime(15, 30):
                    logger.info("[MARKET CLOSE] 15:30 IST reached. Triggering EOD Analysis & Self-Evolution...")
                    break

                time.sleep(1.0)

        except KeyboardInterrupt:
            logger.info("[INTERRUPT] Received user interrupt. Shutting down gracefully...")

        finally:
            self.stop_event.set()
            for name, t in self.threads.items():
                t.join(timeout=1.0)
            logger.info("[SHUTDOWN] All agent threads terminated.")

        # 5. Run Post-Market Self-Evolution Analysis
        diag = self.run_post_market_analysis()
        logger.info("=" * 70)
        logger.info(f" EOD EVOLUTION COMPLETE: Audit Status = {diag['regression_audit']['status']}")
        logger.info("=" * 70)
        return True


def main():
    parser = argparse.ArgumentParser(description="SachinQuant Master Executive Agent")
    parser.add_argument("--dry-run", action="store_true", help="Execute dry-run multi-agent pipeline")
    parser.add_argument("--post-market", action="store_true", help="Execute post-market analysis & self-evolution only")
    args = parser.parse_args()

    agent = MasterExecutiveAgent(dry_run=args.dry_run)

    if args.post_market:
        diag = agent.run_post_market_analysis()
        print(json.dumps(diag, indent=2))
        sys.exit(0)

    success = agent.run()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
