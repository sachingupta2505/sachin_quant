"""
Multi-Agent Algorithmic Trading Orchestrator
Coordinates message passing and autonomous execution between:
1. ArchitectAgent (Regime, S/R, Rejection Signals)
2. CoderAgent (Dynamic Order Payload Generation & Option Spreads)
3. AuditorAgent (Hard Risk FSM, Limits, ACID Trade Journal)
4. DevOpsAgent (Broker Connectivity, TOTP, Health & Execution)
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Force UTF-8 stdout on Windows to prevent charmap encoding errors
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from agents.architect import ArchitectAgent
from agents.auditor import AuditorAgent
from agents.base import AgentMessage, MessageType
from agents.coder import CoderAgent
from agents.devops import DevOpsAgent
from regime_filter import Candle

IST = ZoneInfo("Asia/Kolkata")

# Configure rich logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("Orchestrator")


class MultiAgentOrchestrator:
    """
    Message broker and lifecycle coordinator for all 4 trading agents.
    """

    def __init__(
        self,
        paper_trading: bool = True,
        state_file: str = "daily_state.json",
        db_path: str = "trading_journal.db",
        reset_state: bool = False,
    ):
        self.logger = logger
        self.paper_trading = paper_trading
        state_path = Path(state_file)

        if reset_state and state_path.exists():
            try:
                state_path.unlink()
            except OSError:
                pass

        # Message Router Registry
        self.agents: dict[str, object] = {}

        # 1. Instantiate Autonomous Agents
        self.architect = ArchitectAgent(dispatch_fn=self.route_message)
        self.coder = CoderAgent(dispatch_fn=self.route_message)
        self.auditor = AuditorAgent(dispatch_fn=self.route_message, state_file=state_file, db_path=db_path)
        self.devops = DevOpsAgent(dispatch_fn=self.route_message, paper_trading=paper_trading)

        # Register agents in router
        self.agents["Architect"] = self.architect
        self.agents["Coder"] = self.coder
        self.agents["Auditor"] = self.auditor
        self.agents["DevOps"] = self.devops

        self.logger.info("=================================================================")
        self.logger.info("[INIT] Multi-Agent Trading System Initialized with 4 Autonomous Agents")
        self.logger.info("   1. ArchitectAgent: Strategy & Regime Invariants")
        self.logger.info("   2. CoderAgent: Dynamic Option Spread Synthesizer")
        self.logger.info("   3. AuditorAgent: Hard FSM Guardian & SQLite Journal")
        self.logger.info("   4. DevOpsAgent: Angel One Broker & Execution Bridge")
        self.logger.info("=================================================================")

    def route_message(self, message: AgentMessage) -> None:
        """Central message passing bus routing messages between agents."""
        target = self.agents.get(message.recipient)
        if target:
            self.logger.info(
                f"[BUS] {message.sender} ---> [{message.msg_type.value}] ---> {message.recipient}"
            )
            target.receive_message(message)
        else:
            self.logger.error(f"[BUS Error] Unknown message recipient: {message.recipient}")

    def run_simulation(self) -> None:
        """
        Executes an end-to-end multi-agent trading day simulation:
        1. DevOps connectivity and health check.
        2. Initial Balance formation (09:15 - 09:44 AM IST).
        3. Market entry window opens (09:45 AM IST).
        4. Rejection signal -> Payload synthesis -> Audit approval -> Broker dispatch.
        5. Live M2M tick tracking (MAE & MFE).
        6. Profit target exit.
        7. Max trades compliance test (Audit Veto).
        8. Daily performance report.
        """
        sim_day = datetime(2026, 10, 5, 9, 15, tzinfo=IST)

        # -------------------------------------------------------------
        # STEP 1: DevOps Connectivity Check
        # -------------------------------------------------------------
        self.logger.info("\n--- STEP 1: DEVOPS HEALTH & BROKER CONNECTIVITY ---")
        self.devops.initialize_connectivity()
        health = self.devops.health_check()
        self.logger.info(f"Health Telemetry: {health}")

        # -------------------------------------------------------------
        # STEP 2: Initial Balance Formation (09:15 - 09:40 AM IST)
        # -------------------------------------------------------------
        self.logger.info("\n--- STEP 2: INITIAL BALANCE FORMATION (09:15 - 09:44 AM IST) ---")
        ib_candles = [
            Candle(sim_day + timedelta(minutes=0), 25000, 25060, 24980, 25040),
            Candle(sim_day + timedelta(minutes=5), 25040, 25080, 25020, 25070),
            Candle(sim_day + timedelta(minutes=10), 25070, 25075, 25010, 25015),
            Candle(sim_day + timedelta(minutes=15), 25015, 25030, 24950, 24960), # Low: 24950
            Candle(sim_day + timedelta(minutes=20), 24960, 25000, 24955, 24995),
            Candle(sim_day + timedelta(minutes=25), 24995, 25080, 24980, 25020), # High: 25080
        ]
        for c in ib_candles:
            self.architect.on_candle(c)

        # 09:45:00 AM IST mark reached: Lock Initial Balance
        self.architect.regime_filter.ib_tracker.lock_manual()
        ib = self.architect.regime_filter.ib_tracker.get_ib()
        self.logger.info(f"Initial Balance Computed & Locked: High=INR {ib.high}, Low=INR {ib.low}, Range={ib.range} pts")

        # -------------------------------------------------------------
        # STEP 3: Trading Window Opens (10:15 AM IST) - Signal & Trade 1
        # -------------------------------------------------------------
        self.logger.info("\n--- STEP 3: TRADE 1 - SIGNAL, AUDIT & EXECUTION PIPELINE ---")
        # Bullish Hammer rejecting 24950 Support:
        # Open: 24985, High: 24990, Low: 24945 (spikes into 24950 support), Close: 24985
        # Range: 45 pts, Lower Wick: 40 pts (88.9% >= 50%)
        t1_candle = Candle(
            timestamp=datetime(2026, 10, 5, 10, 15, tzinfo=IST),
            open=24985.0,
            high=24990.0,
            low=24945.0,
            close=24985.0,
            volume=5000.0,
        )

        # Send candle event into multi-agent bus
        self.route_message(
            AgentMessage(
                msg_id="MKT-001",
                sender="MarketFeed",
                recipient="Architect",
                msg_type=MessageType.MARKET_CANDLE,
                payload={"candle": t1_candle},
                timestamp=t1_candle.timestamp,
            )
        )

        active_trade_id = self.devops.active_order["trade_id"] if self.devops.active_order else "TRADE-1"

        # -------------------------------------------------------------
        # STEP 4: Auditor Real-time M2M & MAE/MFE Tracking
        # -------------------------------------------------------------
        self.logger.info("\n--- STEP 4: AUDITOR LIVE M2M, MAE & MFE MONITORING ---")
        # Adverse excursion: -250 INR
        self.auditor.audit_tick_m2m(active_trade_id, -250.0, current_time=datetime(2026, 10, 5, 10, 25, tzinfo=IST))
        # Favorable excursion: +750 INR
        self.auditor.audit_tick_m2m(active_trade_id, 750.0, current_time=datetime(2026, 10, 5, 10, 35, tzinfo=IST))

        # -------------------------------------------------------------
        # STEP 5: Position Close / Profit Take
        # -------------------------------------------------------------
        self.logger.info("\n--- STEP 5: POSITION EXIT & JOURNAL PERSISTENCE ---")
        self.route_message(
            AgentMessage(
                msg_id="EVT-CLS-1",
                sender="DevOps",
                recipient="Auditor",
                msg_type=MessageType.POSITION_CLOSED,
                payload={
                    "trade_id": active_trade_id,
                    "realized_pnl": 650.0,
                    "expected_exit_price": 5.0,
                    "actual_exit_price": 5.1,
                    "exit_reason": "PROFIT_TARGET_HIT",
                    "timestamp": datetime(2026, 10, 5, 10, 45, tzinfo=IST),
                },
                timestamp=datetime(2026, 10, 5, 10, 45, tzinfo=IST),
            )
        )

        # -------------------------------------------------------------
        # STEP 6: Trade 2 Execution (Reaching 2 Trades/Day Limit)
        # -------------------------------------------------------------
        self.logger.info("\n--- STEP 6: TRADE 2 EXECUTION (LIMIT REACHED) ---")
        t2_candle = Candle(
            timestamp=datetime(2026, 10, 5, 11, 30, tzinfo=IST),
            open=24980.0,
            high=24985.0,
            low=24945.0,
            close=24980.0,
            volume=4500.0,
        )
        self.route_message(
            AgentMessage(
                msg_id="MKT-002",
                sender="MarketFeed",
                recipient="Architect",
                msg_type=MessageType.MARKET_CANDLE,
                payload={"candle": t2_candle},
                timestamp=t2_candle.timestamp,
            )
        )
        t2_id = self.devops.active_order["trade_id"] if self.devops.active_order else "TRADE-2"
        # Close Trade 2 with small loss (-200 INR)
        self.route_message(
            AgentMessage(
                msg_id="EVT-CLS-2",
                sender="DevOps",
                recipient="Auditor",
                msg_type=MessageType.POSITION_CLOSED,
                payload={
                    "trade_id": t2_id,
                    "realized_pnl": -200.0,
                    "expected_exit_price": 20.0,
                    "actual_exit_price": 20.0,
                    "exit_reason": "STOP_LOSS_HIT",
                    "timestamp": datetime(2026, 10, 5, 12, 10, tzinfo=IST),
                },
                timestamp=datetime(2026, 10, 5, 12, 10, tzinfo=IST),
            )
        )

        # -------------------------------------------------------------
        # STEP 7: Trade 3 Attempt -> AUDITOR VETO (Hard Limit Enforced)
        # -------------------------------------------------------------
        self.logger.info("\n--- STEP 7: TRADE 3 ATTEMPT -> AUDITOR VETO TEST ---")
        t3_candle = Candle(
            timestamp=datetime(2026, 10, 5, 13, 0, tzinfo=IST),
            open=24982.0,
            high=24985.0,
            low=24945.0,
            close=24982.0,
            volume=4000.0,
        )
        self.route_message(
            AgentMessage(
                msg_id="MKT-003",
                sender="MarketFeed",
                recipient="Architect",
                msg_type=MessageType.MARKET_CANDLE,
                payload={"candle": t3_candle},
                timestamp=t3_candle.timestamp,
            )
        )

        # -------------------------------------------------------------
        # STEP 8: Daily Audit Performance Report
        # -------------------------------------------------------------
        self.logger.info("\n--- STEP 8: AUDITOR DAILY PERFORMANCE SUMMARY ---")
        perf = self.auditor.audit_logger.get_daily_performance("2026-10-05")
        self.logger.info("=======================================================")
        self.logger.info(f"[REPORT] Daily Session: {perf['date']}")
        self.logger.info(f"   Completed Trades: {perf['trade_count']}/2 Limit")
        self.logger.info(f"   Net Realized PnL: INR {perf['total_pnl']:.2f}")
        self.logger.info(f"   Win Rate: {perf['win_rate']}% ({perf['wins']} Wins / {perf['losses']} Losses)")
        self.logger.info(f"   Average MAE: INR {perf['avg_mae']:.2f}")
        self.logger.info(f"   Average MFE: INR {perf['avg_mfe']:.2f}")
        self.logger.info(f"   Average Slippage: {perf['avg_slippage']} pts")
        self.logger.info(f"   FSM Final State: {self.auditor.risk_guard.current_state.value}")
        self.logger.info("=======================================================\n")


if __name__ == "__main__":
    orchestrator = MultiAgentOrchestrator(paper_trading=True, reset_state=True)
    orchestrator.run_simulation()
