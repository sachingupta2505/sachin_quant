"""
Architect Agent
Role: System Architect & Quantitative Strategy Validator
Responsibilities:
1. Validates market regime conditions (Initial Balance calculation 09:15 - 09:45 AM IST).
2. Pure price action compression & chop detection (zero lagging indicators).
3. Identifies Support & Resistance zones.
4. Detects high-probability 5-min rejection candles (wick ratio >= 50%).
5. Enforces strategy structural invariants before passing trade signals to Coder Agent.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from agents.base import AgentMessage, BaseAgent, MessageType
from regime_filter import Candle, InitialBalance, MarketRegime, RegimeFilter
from execution_engine import RejectionDetector, SignalType, SRZone

IST = ZoneInfo("Asia/Kolkata")
EXPIRY_CUTOFF_TIME: time = time(13, 30)  # 01:30 PM IST: Cut off new signals on expiry days to eliminate high-gamma risk


def is_expiry_day(dt: datetime) -> bool:
    """NSE Nifty weekly options contracts expire on Thursdays (weekday 3)."""
    return dt.weekday() == 3


class ArchitectAgent(BaseAgent):
    def __init__(
        self,
        dispatch_fn: Optional[Callable[[AgentMessage], None]] = None,
        tz: ZoneInfo = IST,
    ):
        super().__init__(name="Architect")
        self.dispatch_fn = dispatch_fn
        self.tz = tz
        self.regime_filter = RegimeFilter(tz=tz)
        self.rejection_detector = RejectionDetector()
        self._recent_candles: list[Candle] = []

    def is_expiry_day(self, dt: datetime) -> bool:
        return is_expiry_day(dt)

    def handle_message(self, message: AgentMessage) -> None:
        if message.msg_type == MessageType.MARKET_CANDLE:
            candle: Candle = message.payload["candle"]
            self.on_candle(candle)

    def on_candle(self, candle: Candle) -> None:
        # Invariant: Restrict new strategy signals after 13:30 IST on expiry days to eliminate high-gamma risk
        if is_expiry_day(candle.timestamp) and candle.timestamp.time() >= EXPIRY_CUTOFF_TIME:
            self.logger.info(
                f"[EXPIRY GAMMA CUTOFF] Signals blocked after {EXPIRY_CUTOFF_TIME.strftime('%H:%M')} IST on expiry days."
            )
            return

        self._recent_candles.append(candle)
        if len(self._recent_candles) > 50:
            self._recent_candles.pop(0)

        # 1. Update Initial Balance if in 09:15-09:45 AM window
        is_ib = self.regime_filter.ingest_ib_candle(candle)
        if is_ib:
            self.logger.debug(f"[IB Ingestion] Candle {candle.timestamp.strftime('%H:%M')} recorded into IB.")

        # 2. Evaluate Pure Price Action Regime
        regime_analysis = self.regime_filter.evaluate_regime(
            self._recent_candles, current_time=candle.timestamp
        )

        if not regime_analysis.is_favorable_for_spread:
            self.logger.info(
                f"[Regime Gated] {regime_analysis.regime.value}: {regime_analysis.description}"
            )
            return

        self.logger.info(
            f"[Regime Validated] {regime_analysis.regime.value} - Favorable price action structure."
        )

        # 3. Identify S/R Zones
        ib = self.regime_filter.ib_tracker.get_ib()
        zones = self.rejection_detector.identify_sr_zones(ib=ib, spot_price=candle.close)

        # 4. Check for 5-min Rejection Candle (wick ratio >= 50%)
        rejection_signal = self.rejection_detector.detect_rejection(candle, zones)
        if rejection_signal.signal_type == SignalType.NONE:
            return

        self.logger.info(
            f"[EDGE CONFIRMED] {rejection_signal.description} (Confidence: {rejection_signal.confidence})"
        )

        # 5. Dispatch Strategy Signal to Coder Agent
        if self.dispatch_fn:
            msg = AgentMessage(
                msg_id=f"SIG-{uuid.uuid4().hex[:6].upper()}",
                sender=self.name,
                recipient="Coder",
                msg_type=MessageType.STRATEGY_SIGNAL,
                payload={
                    "signal_type": rejection_signal.signal_type.value,
                    "spot_price": candle.close,
                    "candle": candle,
                    "zone_name": rejection_signal.zone.name,
                    "zone_level": rejection_signal.zone.level,
                    "wick_ratio": rejection_signal.wick_ratio,
                    "confidence": rejection_signal.confidence,
                    "description": rejection_signal.description,
                    "timestamp": candle.timestamp,
                },
                timestamp=candle.timestamp,
            )
            self.dispatch_fn(msg)
