"""
Process 3: Auditor Agent Worker
Blackboard Pattern: Uses bus.py (system_bus.db)
Responsibilities:
1. Consumes ORDER_PROPOSED events from Blackboard.
2. Checks hard FSM constraints:
   - Max 2 trades per calendar day limit.
   - Hard daily loss kill-switch (-1500 INR).
   - Single-spread risk <= 1500 INR ceiling.
   - 09:45 - 15:05 IST active trading window.
3. Publishes ORDER_APPROVED or ORDER_VETOED to Blackboard.
4. Consumes ORDER_EXECUTED and logs into SQLite journal (trading_journal.db).
5. Completes trade exits, tracking slippage, MAE, and MFE.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Fix UTF-8 stdout on Windows
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).parent.parent))

from bus import EventStatus, SystemBus
from audit_logger import AuditLogger
from risk_guard import RiskGuard, RiskState

IST = ZoneInfo("Asia/Kolkata")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [AUDITOR] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("AuditorWorker")


def run_auditor():
    bus = SystemBus()
    risk_guard = RiskGuard(state_file="daily_state.json", tz=IST)
    audit_logger = AuditLogger(db_path="trading_journal.db", tz=IST)

    print("\n" + "=" * 65)
    print(" [PROCESS 3] AUDITOR AGENT - HARD RISK FSM & COMPLIANCE")
    print("  Pattern: Event-Driven Blackboard (system_bus.db)")
    print("  Enforcing: Max 2 Trades/Day | -INR 1500 Loss Limit | Max Risk <= 1500")
    print("  Input Topic: ORDER_PROPOSED | Output: ORDER_APPROVED / ORDER_VETOED")
    print("=" * 65 + "\n")

    logger.info(
        f"Auditor active. Initial FSM State: {risk_guard.current_state.value} | "
        f"Day Trades: {risk_guard.trade_count}/2 | Cum PnL: INR {risk_guard.total_pnl:.2f}"
    )

    last_report_time = time.time()

    while True:
        # 1. Audit proposed orders (atomically transitions PENDING -> PROCESSING)
        orders = bus.consume(topic="ORDER_PROPOSED", target="Auditor")
        for ev in orders:
            payload = ev.payload
            trade_id = payload["trade_id"]
            ts = datetime.fromisoformat(payload["timestamp"]) if "timestamp" in payload else datetime.now(IST)

            logger.info("=" * 50)
            logger.info(f"Auditing ORDER_PROPOSED #{ev.id} ({trade_id}) from {ev.source_agent}...")

            # Check 1: Enforce hard single-spread risk ceiling <= INR 1500
            max_risk_inr = float(payload.get("max_risk_inr", 0.0))
            if max_risk_inr > 1500.0:
                reason = f"Proposed max risk INR {max_risk_inr:.2f} exceeds hard limit of INR 1500.0"
                logger.warning(f"[AUDIT VETO] Order {trade_id} VETOED! {reason}")
                bus.update_status(ev.id, EventStatus.VETOED)
                bus.publish(
                    topic="ORDER_VETOED",
                    source="Auditor",
                    target="Coder",
                    payload={"trade_id": trade_id, "reason": reason, "timestamp": ts.isoformat()},
                )
                continue

            # Check 2: Evaluate hard FSM limits (trades < 2, loss > -1500, time window)
            allowed, reason = risk_guard.can_enter_trade(current_time=ts)
            if not allowed:
                logger.warning(f"[AUDIT VETO] Order {trade_id} VETOED by RiskGuard! Reason: {reason}")
                bus.update_status(ev.id, EventStatus.VETOED)
                bus.publish(
                    topic="ORDER_VETOED",
                    source="Auditor",
                    target="Coder",
                    payload={"trade_id": trade_id, "reason": reason, "timestamp": ts.isoformat()},
                )
                continue

            # Approved: Record planned entry in FSM and mark event COMPLETED
            bus.update_status(ev.id, EventStatus.COMPLETED)
            risk_guard.record_trade_entry(trade_id=trade_id, details=payload, current_time=ts)

            logger.info(
                f"[AUDIT APPROVED] Order {trade_id} COMPLIES with all invariants. "
                f"Day Trades: {risk_guard.trade_count}/2 | FSM State: {risk_guard.current_state.value}"
            )

            # Publish ORDER_APPROVED to Blackboard for DevOps
            eid = bus.publish(
                topic="ORDER_APPROVED",
                source="Auditor",
                target="DevOps",
                payload=payload,
            )
            logger.info(f"Published ORDER_APPROVED (Event #{eid}) to system_bus.db for DevOps.")

        # 2. Consume execution confirmations from DevOps
        executed_events = bus.consume(topic="ORDER_EXECUTED", target="Auditor")
        for ev in executed_events:
            payload = ev.payload
            trade_id = payload["trade_id"]
            entry_price = float(payload.get("actual_entry_price", payload.get("net_credit", 0.0)))
            expected_price = float(payload.get("expected_entry_price", payload.get("net_credit", 0.0)))
            ts = datetime.fromisoformat(payload["timestamp"]) if "timestamp" in payload else datetime.now(IST)

            entry = audit_logger.log_trade_entry(
                trade_id=trade_id,
                symbol="NIFTY",
                spread_type=payload["spread_type"],
                expected_entry_price=expected_price,
                actual_entry_price=entry_price,
                is_paper=payload.get("is_paper", True),
                timestamp=ts,
                notes=payload.get("architect_signal", ""),
            )
            bus.update_status(ev.id, EventStatus.COMPLETED)
            logger.info(f"[JOURNAL COMMITTED] Trade {trade_id} logged to SQLite. Slippage: {entry.entry_slippage} pts.")

        # 3. Consume position exits from DevOps
        closed_events = bus.consume(topic="POSITION_CLOSED", target="Auditor")
        for ev in closed_events:
            payload = ev.payload
            trade_id = payload["trade_id"]
            realized_pnl = float(payload["realized_pnl"])
            ts = datetime.fromisoformat(payload["timestamp"]) if "timestamp" in payload else datetime.now(IST)

            risk_guard.record_trade_exit(trade_id, realized_pnl=realized_pnl, current_time=ts)
            closed = audit_logger.log_trade_exit(
                trade_id=trade_id,
                expected_exit_price=float(payload.get("expected_exit_price", 0.0)),
                actual_exit_price=float(payload.get("actual_exit_price", 0.0)),
                realized_pnl=realized_pnl,
                timestamp=ts,
                notes=payload.get("exit_reason", ""),
            )
            bus.update_status(ev.id, EventStatus.COMPLETED)
            logger.info(
                f"[TRADE CLOSED & AUDITED] {trade_id} | Realized PnL: INR {realized_pnl:.2f} | "
                f"MAE: INR {closed.mae_inr:.2f} | MFE: INR {closed.mfe_inr:.2f} | Day Total: INR {risk_guard.total_pnl:.2f}"
            )

        # Heartbeat report every 15 seconds
        if time.time() - last_report_time > 15.0:
            last_report_time = time.time()
            perf = audit_logger.get_daily_performance(datetime.now(IST).strftime("%Y-%m-%d"))
            logger.info(
                f"[AUDIT HEARTBEAT] Trades: {perf['trade_count']}/2 | PnL: INR {perf['total_pnl']:.2f} | "
                f"Win Rate: {perf['win_rate']}% | State: {risk_guard.current_state.value}"
            )

        time.sleep(0.5)


if __name__ == "__main__":
    run_auditor()
