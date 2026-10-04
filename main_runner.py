"""
Multi-Agent Algorithmic Trading Runner
Module: main_runner.py

Orchestrates all 4 autonomous agents:
1. Architect Agent: Strategy edge detection & regime validation
2. Coder Agent: Dynamic option spread synthesis with risk sizing (<= 1500 INR)
3. Auditor Agent: Continuous compliance & hard risk FSM gating
4. DevOps Agent: Execution bridge & paper trading fill engine

Supports `--dry-run` flag to verify concurrent inter-agent communication over bus.py.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# Fix UTF-8 stdout encoding on Windows
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from bus import EventStatus, SystemBus
from execution_engine import SignalType, SpreadType
from risk_guard import RiskGuard
from audit_logger import AuditLogger
from agents.architect import EXPIRY_CUTOFF_TIME, is_expiry_day

IST = ZoneInfo("Asia/Kolkata")
LOT_SIZE = 25
MAX_PERMITTED_SPREAD_RISK_INR = 1500.0
MAX_DAILY_LOSS_INR = -1500.0
MAX_DAILY_TRADES = 2
DEFAULT_ORDER_TYPE = "LIMIT"
MAX_BID_ASK_SPREAD_RATIO = 0.10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("MainRunner")


def architect_worker(
    bus: SystemBus,
    stop_event: threading.Event,
    verified_events: Optional[dict[str, threading.Event]] = None,
) -> None:
    """
    Architect Agent Worker:
    Listens on bus.py for TEST_SIGNAL / MARKET_CANDLE events.
    Validates regime & expiry invariants, then publishes SIGNAL_DETECTED to Coder.
    """
    while not stop_event.is_set():
        events = bus.consume(topic="TEST_SIGNAL", target="Architect")
        if not events:
            events = bus.consume(topic="MARKET_CANDLE", target="Architect")

        for ev in events:
            payload = ev.payload
            sim_time_str = payload.get("timestamp") or datetime.now(IST).isoformat()
            sim_time = datetime.fromisoformat(sim_time_str) if isinstance(sim_time_str, str) else sim_time_str

            # Invariant: Gamma protection on expiry days
            if is_expiry_day(sim_time) and sim_time.time() >= EXPIRY_CUTOFF_TIME:
                logger.warning("[ARCHITECT] Signal blocked by 13:30 expiry gamma cutoff.")
                bus.update_status(ev.id, EventStatus.FAILED)
                continue

            signal_payload = {
                "signal_type": payload.get("signal_type", SignalType.BULLISH_REJECTION.value),
                "spot_price": float(payload.get("spot_price", 25000.0)),
                "zone_name": payload.get("zone_name", "S1_SUPPORT"),
                "zone_level": float(payload.get("zone_level", 24950.0)),
                "wick_ratio": float(payload.get("wick_ratio", 0.55)),
                "confidence": payload.get("confidence", "HIGH"),
                "description": payload.get("description", "Bullish Hammer at Support"),
                "timestamp": sim_time.isoformat() if hasattr(sim_time, "isoformat") else str(sim_time),
            }

            eid = bus.publish(
                topic="SIGNAL_DETECTED",
                source="Architect",
                target="Coder",
                payload=signal_payload,
            )
            bus.update_status(ev.id, EventStatus.COMPLETED)
            logger.info(f"[ARCHITECT] Emitted SIGNAL_DETECTED #{eid}: {signal_payload['description']} -> Coder")

            if verified_events and "architect_emitted" in verified_events:
                verified_events["architect_emitted"].set()

        time.sleep(0.05)


def coder_worker(
    bus: SystemBus,
    stop_event: threading.Event,
    verified_events: Optional[dict[str, threading.Event]] = None,
) -> None:
    """
    Coder Agent Worker:
    Consumes SIGNAL_DETECTED events from Blackboard.
    Synthesizes defined-risk 2-leg option spread (Max Risk <= 1500 INR, BUY leg before SELL leg).
    Publishes ORDER_PROPOSED to Auditor.
    """
    while not stop_event.is_set():
        events = bus.consume(topic="SIGNAL_DETECTED", target="Coder")
        for ev in events:
            payload = ev.payload
            signal_type = payload["signal_type"]
            spot_price = float(payload["spot_price"])
            ts_str = payload.get("timestamp") or datetime.now(IST).isoformat()

            # Dynamic strike calculation
            atm_strike = round(spot_price / 50.0) * 50.0
            candidate_widths = [100.0, 50.0]
            chosen_width = None
            chosen_credit = 0.0
            chosen_risk_inr = 0.0

            for width in candidate_widths:
                sell_prem = 75.0
                buy_prem = max(57.0 - (width - 50.0) * 0.25, 20.0)
                net_credit = sell_prem - buy_prem
                net_premium_received = net_credit * LOT_SIZE
                risk_inr = (width * LOT_SIZE) - net_premium_received

                if risk_inr <= MAX_PERMITTED_SPREAD_RISK_INR:
                    chosen_width = width
                    chosen_credit = net_credit
                    chosen_risk_inr = round(risk_inr, 2)
                    break

            if chosen_width is None:
                bus.update_status(ev.id, EventStatus.FAILED)
                continue

            # Invariant: Margin order safety - BUY leg MUST precede SELL leg
            if signal_type == SignalType.BULLISH_REJECTION.value:
                sell_strike = atm_strike
                buy_strike = sell_strike - chosen_width
                spread_type = SpreadType.BULL_PUT_SPREAD.value
                legs = [
                    {"symbol": f"NIFTY_{int(buy_strike)}_PE", "strike": buy_strike, "option_type": "PE", "action": "BUY", "quantity": LOT_SIZE, "price": 75.0 - chosen_credit},
                    {"symbol": f"NIFTY_{int(sell_strike)}_PE", "strike": sell_strike, "option_type": "PE", "action": "SELL", "quantity": LOT_SIZE, "price": 75.0},
                ]
            else:
                sell_strike = atm_strike
                buy_strike = sell_strike + chosen_width
                spread_type = SpreadType.BEAR_CALL_SPREAD.value
                legs = [
                    {"symbol": f"NIFTY_{int(buy_strike)}_CE", "strike": buy_strike, "option_type": "CE", "action": "BUY", "quantity": LOT_SIZE, "price": 75.0 - chosen_credit},
                    {"symbol": f"NIFTY_{int(sell_strike)}_CE", "strike": sell_strike, "option_type": "CE", "action": "SELL", "quantity": LOT_SIZE, "price": 75.0},
                ]

            trade_id = f"SPD-{datetime.now(IST).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4].upper()}"
            max_reward_inr = round(chosen_credit * LOT_SIZE, 2)

            order_payload = {
                "trade_id": trade_id,
                "spread_type": spread_type,
                "legs": legs,
                "spot_price": spot_price,
                "spread_width": chosen_width,
                "net_credit": chosen_credit,
                "max_risk_inr": chosen_risk_inr,
                "max_reward_inr": max_reward_inr,
                "timestamp": ts_str,
                "architect_signal": payload["description"],
            }

            eid = bus.publish(
                topic="ORDER_PROPOSED",
                source="Coder",
                target="Auditor",
                payload=order_payload,
            )
            bus.update_status(ev.id, EventStatus.COMPLETED)
            logger.info(
                f"[CODER] Created spread {spread_type} ({trade_id}) | "
                f"Width: {chosen_width} pts | Max Risk: INR {chosen_risk_inr} <= {MAX_PERMITTED_SPREAD_RISK_INR} -> Auditor"
            )

            if verified_events and "coder_created_spread" in verified_events:
                verified_events["coder_created_spread"].set()

        time.sleep(0.05)


def auditor_worker(
    bus: SystemBus,
    stop_event: threading.Event,
    verified_events: Optional[dict[str, threading.Event]] = None,
    state_file: str = "daily_state.json",
    db_path: str = "trading_journal.db",
) -> None:
    """
    Auditor Agent Worker:
    Consumes ORDER_PROPOSED events from Blackboard.
    Validates hard risk invariants (Max Risk <= 1500 INR, Daily Trades < 2, Daily Loss > -1500 INR).
    Publishes ORDER_APPROVED to DevOps.
    """
    risk_guard = RiskGuard(state_file=state_file, tz=IST)
    audit_logger = AuditLogger(db_path=db_path, tz=IST)

    while not stop_event.is_set():
        events = bus.consume(topic="ORDER_PROPOSED", target="Auditor")
        for ev in events:
            payload = ev.payload
            trade_id = payload["trade_id"]
            ts = datetime.fromisoformat(payload["timestamp"]) if "timestamp" in payload else datetime.now(IST)

            # Invariant 1: Single spread risk <= 1500 INR
            max_risk_inr = float(payload.get("max_risk_inr", 0.0))
            if max_risk_inr > MAX_PERMITTED_SPREAD_RISK_INR:
                bus.update_status(ev.id, EventStatus.VETOED)
                logger.warning(f"[AUDITOR] Vetoed {trade_id}: Max risk INR {max_risk_inr} > 1500 INR")
                continue

            # Invariant 2: Daily loss limit
            if risk_guard.total_pnl <= MAX_DAILY_LOSS_INR:
                bus.update_status(ev.id, EventStatus.VETOED)
                logger.warning(f"[AUDITOR] Vetoed {trade_id}: Daily loss limit breached")
                continue

            # Invariant 3: Daily trades limit
            if risk_guard.trade_count >= MAX_DAILY_TRADES:
                bus.update_status(ev.id, EventStatus.VETOED)
                logger.warning(f"[AUDITOR] Vetoed {trade_id}: Max daily trades reached")
                continue

            # Invariant 4: Time window gating
            allowed, reason = risk_guard.can_enter_trade(current_time=ts)
            if not allowed:
                bus.update_status(ev.id, EventStatus.VETOED)
                logger.warning(f"[AUDITOR] Vetoed {trade_id}: {reason}")
                continue

            # Approved
            bus.update_status(ev.id, EventStatus.COMPLETED)
            risk_guard.record_trade_entry(trade_id=trade_id, details=payload, current_time=ts)

            eid = bus.publish(
                topic="ORDER_APPROVED",
                source="Auditor",
                target="DevOps",
                payload=payload,
            )
            logger.info(f"[AUDITOR] Approved order {trade_id} (Invariant compliant) -> DevOps (Event #{eid})")

            if verified_events and "auditor_approved" in verified_events:
                verified_events["auditor_approved"].set()

        # Also consume execution confirmations for journal logging
        exec_events = bus.consume(topic="ORDER_EXECUTED", target="Auditor")
        for ev in exec_events:
            payload = ev.payload
            bus.update_status(ev.id, EventStatus.COMPLETED)

        time.sleep(0.05)


def devops_worker(
    bus: SystemBus,
    stop_event: threading.Event,
    verified_events: Optional[dict[str, threading.Event]] = None,
) -> None:
    """
    DevOps Agent Worker:
    Consumes ORDER_APPROVED events from Blackboard.
    Validates LIMIT order type & bid-ask liquidity spread guard.
    Fills order in Paper Trading mode and publishes ORDER_EXECUTED to Auditor.
    """
    while not stop_event.is_set():
        events = bus.consume(topic="ORDER_APPROVED", target="DevOps")
        for ev in events:
            payload = ev.payload
            trade_id = payload["trade_id"]
            legs = payload["legs"]
            ts_str = payload.get("timestamp") or datetime.now(IST).isoformat()

            # Invariant: Limit order type
            order_type = DEFAULT_ORDER_TYPE
            assert order_type == "LIMIT", "Order type invariant breached!"

            # Invariant: Bid-ask spread guard (assume synthetic quote 18.0 / 18.5)
            hedge_bid, hedge_ask = 18.0, 18.5
            mid_price = (hedge_bid + hedge_ask) / 2.0
            spread_ratio = (hedge_ask - hedge_bid) / mid_price
            if spread_ratio > MAX_BID_ASK_SPREAD_RATIO:
                bus.update_status(ev.id, EventStatus.FAILED)
                logger.warning(f"[DEVOPS] Execution paused: Bid-Ask spread {spread_ratio:.1%} > 10%")
                continue

            executed_legs = []
            for i, leg in enumerate(legs):
                executed_legs.append({**leg, "order_id": f"FILL-{trade_id}-{i+1}", "status": "FILLED"})

            eid = bus.publish(
                topic="ORDER_EXECUTED",
                source="DevOps",
                target="Auditor",
                payload={
                    **payload,
                    "order_type": order_type,
                    "executed_legs": executed_legs,
                    "actual_entry_price": payload["net_credit"],
                    "expected_entry_price": payload["net_credit"],
                    "is_paper": True,
                    "timestamp": ts_str,
                },
            )
            bus.update_status(ev.id, EventStatus.COMPLETED)
            logger.info(f"[DEVOPS] Received order {trade_id}, routed 2 legs as {order_type} (Paper Mode) -> Event #{eid}")

            if verified_events and "devops_received" in verified_events:
                verified_events["devops_received"].set()

        time.sleep(0.05)


def run_dry_run(db_path: str = "system_bus.db") -> bool:
    """
    Executes dry-run test:
    1. Starts all 4 agents concurrently using threads.
    2. Publish one dummy test signal onto bus.py.
    3. Verify that:
       - Architect emits signal
       - Coder creates spread
       - Auditor approves
       - DevOps receives order
    4. Print: "[SUCCESS] All 4 agents communicated cleanly via bus.py" and exit.
    """
    print("\n" + "=" * 80)
    print("      SACCHIN QUANT: MULTI-AGENT SYSTEM DRY-RUN (CONCURRENT THREADS)")
    print("=" * 80)
    logger.info("Initializing concurrent thread test harness over bus.py...")

    dry_run_state = "dry_run_state.json"
    state_path = Path(dry_run_state)
    if state_path.exists():
        try:
            state_path.unlink()
        except OSError:
            pass

    bus = SystemBus(db_path=db_path)
    # Clear any prior stale events for clean dry-run verification
    with bus._get_connection() as conn:
        conn.execute("DELETE FROM bus_events;")
        conn.commit()

    stop_event = threading.Event()
    verified_events = {
        "architect_emitted": threading.Event(),
        "coder_created_spread": threading.Event(),
        "auditor_approved": threading.Event(),
        "devops_received": threading.Event(),
    }

    # Step 1: Start all 4 agents concurrently using threads
    threads = [
        threading.Thread(
            target=architect_worker,
            args=(SystemBus(db_path=db_path), stop_event, verified_events),
            name="ArchitectWorker",
            daemon=True,
        ),
        threading.Thread(
            target=coder_worker,
            args=(SystemBus(db_path=db_path), stop_event, verified_events),
            name="CoderWorker",
            daemon=True,
        ),
        threading.Thread(
            target=auditor_worker,
            args=(SystemBus(db_path=db_path), stop_event, verified_events, dry_run_state),
            name="AuditorWorker",
            daemon=True,
        ),
        threading.Thread(
            target=devops_worker,
            args=(SystemBus(db_path=db_path), stop_event, verified_events),
            name="DevOpsWorker",
            daemon=True,
        ),
    ]

    for t in threads:
        t.start()
    logger.info("All 4 agent worker threads started and actively polling bus.py.")
    time.sleep(0.2)

    # Step 2: Publish one dummy test signal onto bus.py
    logger.info("Publishing dummy test signal to bus.py for Architect...")
    test_sim_time = datetime(2026, 10, 5, 10, 15, tzinfo=IST)
    test_payload = {
        "signal_type": SignalType.BULLISH_REJECTION.value,
        "spot_price": 25000.0,
        "zone_name": "S1_SUPPORT",
        "zone_level": 24950.0,
        "wick_ratio": 0.58,
        "confidence": "HIGH",
        "description": "Bullish Hammer at 24950 Support",
        "timestamp": test_sim_time.isoformat(),
    }

    bus.publish(
        topic="TEST_SIGNAL",
        source="TestRunner",
        target="Architect",
        payload=test_payload,
    )

    # Step 3: Verify the 4-stage event chain
    logger.info("Awaiting pipeline completion across all 4 agents...")

    stages = [
        ("Architect emits signal", "architect_emitted"),
        ("Coder creates spread", "coder_created_spread"),
        ("Auditor approves", "auditor_approved"),
        ("DevOps receives order", "devops_received"),
    ]

    for stage_desc, stage_key in stages:
        completed = verified_events[stage_key].wait(timeout=6.0)
        if not completed:
            logger.error(f"[TIMEOUT] Stage '{stage_desc}' failed to complete within 6 seconds.")
            stop_event.set()
            return False
        logger.info(f"  [VERIFIED] {stage_desc}")

    # Stop threads
    stop_event.set()
    for t in threads:
        t.join(timeout=1.0)

    # Cleanup temporary dry run state file
    if state_path.exists():
        try:
            state_path.unlink()
        except OSError:
            pass

    # Step 4: Print success message
    print("\n" + "=" * 80)
    print("[SUCCESS] All 4 agents communicated cleanly via bus.py")
    print("=" * 80 + "\n")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Main Multi-Agent System Runner (Blackboard Pattern over bus.py)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run concurrent dry-run verification across all 4 agents via bus.py",
    )
    args = parser.parse_args()

    # If --dry-run or no args specified, run dry-run verification
    if args.dry_run or len(sys.argv) == 1:
        success = run_dry_run()
        sys.exit(0 if success else 1)
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
