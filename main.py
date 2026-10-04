"""
Nifty Derivatives Algorithmic Trading Engine - Orchestration Entry Point
Ties together:
1. RiskGuard (FSM, kill-switch, limits, window gating)
2. RegimeFilter (Initial Balance, pure price action compression & chop)
3. ExecutionEngine (Angel One SmartAPI, rejection signals, defined-risk spreads)
4. AuditLogger (SQLite trade journal, MAE, MFE, slippage)
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from audit_logger import AuditLogger
from execution_engine import AngelAuth, ExecutionEngine, RejectionDetector, SignalType
from regime_filter import Candle, MarketRegime, RegimeFilter
from risk_guard import RiskGuard, RiskState

IST = ZoneInfo("Asia/Kolkata")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("algo_engine.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("nifty_engine")


class TradingEngine:
    """
    Main autonomous trading controller.
    """

    def __init__(
        self,
        paper_trading: bool = True,
        state_file: str = "daily_state.json",
        db_path: str = "trading_journal.db",
    ):
        self.paper_trading = paper_trading
        self.risk_guard = RiskGuard(state_file=state_file)
        self.regime_filter = RegimeFilter()
        self.rejection_detector = RejectionDetector()
        self.audit_logger = AuditLogger(db_path=db_path)

        # Angel One Authentication
        api_key = os.getenv("SMARTAPI_API_KEY")
        client_code = os.getenv("SMARTAPI_CLIENT_CODE")
        pin = os.getenv("SMARTAPI_PIN")
        totp_secret = os.getenv("SMARTAPI_TOTP_SECRET")

        self.auth = AngelAuth(
            api_key=api_key,
            client_code=client_code,
            pin=pin,
            totp_secret=totp_secret,
        )

        if not self.paper_trading:
            logger.info("Live mode selected: Attempting Angel One SmartAPI login...")
            if not self.auth.login():
                logger.error("SmartAPI login failed! Falling back to Paper Trading for capital protection.")
                self.paper_trading = True

        self.execution_engine = ExecutionEngine(
            risk_guard=self.risk_guard,
            auth=self.auth,
            paper_trading=self.paper_trading,
        )

        logger.info(
            f"TradingEngine initialized. Mode: {'[PAPER TRADING]' if self.paper_trading else '[LIVE CAPITAL]'}"
        )

    def process_5min_candle(self, candle: Candle, recent_candles: list[Candle]) -> None:
        """
        Main pipeline callback called on every completed 5-min candle.
        """
        now = candle.timestamp

        # 1. Update Initial Balance if within 09:15 - 09:45 AM
        self.regime_filter.ingest_ib_candle(candle)

        # 2. Check if mandatory 03:10 PM square-off or kill switch is active
        if self.risk_guard.is_square_off_time(now):
            if self.execution_engine.active_spread:
                logger.warning("Mandatory Square-off triggered! Liquidating active position.")
                # Liquidate active position
                self.execution_engine.close_active_spread(realized_pnl=0.0, exit_time=now)
            return

        # 3. Check if RiskGuard permits trading
        allowed, reason = self.risk_guard.can_enter_trade(now)
        if not allowed:
            logger.debug(f"Trading gated by RiskGuard: {reason}")
            return

        # 4. Pure Price Action Regime Analysis (zero lagging indicators)
        analysis = self.regime_filter.evaluate_regime(recent_candles, current_time=now)
        if not analysis.is_favorable_for_spread:
            logger.info(f"Regime filter: {analysis.regime.value} - {analysis.description}")
            return

        # 5. Identify S/R Zones from IB and Psychological Strikes
        ib = self.regime_filter.ib_tracker.get_ib()
        zones = self.rejection_detector.identify_sr_zones(ib=ib, spot_price=candle.close)

        # 6. Detect 5-min Rejection Candle (wick ratio >= 50%)
        signal = self.rejection_detector.detect_rejection(candle, zones)
        if signal.signal_type == SignalType.NONE:
            return

        logger.info(f"High-Probability Signal Detected: {signal.description}")

        # 7. Construct Defined-Risk Option Spread
        spread = self.execution_engine.build_spread(signal, spot_price=candle.close, timestamp=now)
        if spread is None:
            return

        # 8. Execute Trade & Log to ACID Audit Journal
        if self.execution_engine.execute_spread(spread):
            self.audit_logger.log_trade_entry(
                trade_id=spread.trade_id,
                symbol="NIFTY",
                spread_type=spread.spread_type.value,
                expected_entry_price=spread.net_credit,
                actual_entry_price=spread.net_credit,
                is_paper=self.paper_trading,
                timestamp=now,
                notes=signal.description,
            )
            logger.info(f"Trade opened: {spread.trade_id} (Max Risk: ₹{spread.max_risk_inr:.2f})")


def main():
    engine = TradingEngine(paper_trading=True)
    summary = engine.risk_guard.get_summary()
    logger.info(f"Engine ready. Initial Risk State: {summary}")


if __name__ == "__main__":
    main()
