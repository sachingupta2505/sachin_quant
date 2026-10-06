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
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from agents.base import AgentMessage, BaseAgent, MessageType
from regime_filter import Candle, InitialBalance, MarketRegime, RegimeFilter
from execution_engine import (
    RejectionDetector,
    SignalType,
    SRZone,
    fetch_historical_reference_levels,
)

IST = ZoneInfo("Asia/Kolkata")
EXPIRY_ENTRY_CUTOFF_TIME: time = time(12, 30)  # 12:30 PM IST: Freeze fresh trade entries on expiry days to eliminate 0DTE gamma spikes
EXPIRY_CUTOFF_TIME: time = time(13, 30)        # 01:30 PM IST: Mandatory gamma cut-off & square-off on expiry days
EXPIRY_SQUARE_OFF_TIME: time = time(13, 30)    # 01:30 PM IST: Position square-off limit
MAX_INDIA_VIX: float = 24.0                   # Volatility expansion limit: halt fresh entries if India VIX / IV expands violently (> 24.0)


def is_expiry_day(dt: datetime) -> bool:
    """Evaluates whether dt is an active expiry session using dynamic contract evaluation."""
    from risk_guard import is_expiry_session
    return is_expiry_session(dt)


class ArchitectAgent(BaseAgent):
    def __init__(
        self,
        dispatch_fn: Optional[Callable[[AgentMessage], None]] = None,
        tz: ZoneInfo = IST,
        auth: Optional[Any] = None,
        daily_levels: Optional[dict[str, float]] = None,
        weekly_levels: Optional[dict[str, float]] = None,
        require_confirmation: bool = True,
    ):
        super().__init__(name="Architect")
        self.dispatch_fn = dispatch_fn
        self.tz = tz
        self.require_confirmation = require_confirmation
        self.regime_filter = RegimeFilter(tz=tz)
        self.rejection_detector = RejectionDetector()
        self._recent_candles: list[Candle] = []
        self.pending_rejection: Optional[RejectionSignal] = None
        self.auth = auth
        self.daily_levels = daily_levels or {}
        self.weekly_levels = weekly_levels or {}
        if not self.daily_levels or not self.weekly_levels:
            ref = fetch_historical_reference_levels(auth=self.auth)
            if not self.daily_levels:
                self.daily_levels = ref.get("daily", {"pdh": ref.get("pdh"), "pdl": ref.get("pdl"), "pdc": ref.get("pdc")})
            if not self.weekly_levels:
                self.weekly_levels = ref.get("weekly", {"pwh": ref.get("pwh"), "pwl": ref.get("pwl")})

    def set_reference_levels(
        self,
        daily_levels: Optional[dict[str, float]] = None,
        weekly_levels: Optional[dict[str, float]] = None,
    ) -> None:
        """Update daily and weekly reference levels at runtime."""
        if daily_levels:
            self.daily_levels.update(daily_levels)
        if weekly_levels:
            self.weekly_levels.update(weekly_levels)

    def is_expiry_day(self, dt: datetime) -> bool:
        return is_expiry_day(dt)

    def handle_message(self, message: AgentMessage) -> None:
        if message.msg_type == MessageType.MARKET_CANDLE:
            candle: Candle = message.payload["candle"]
            self.on_candle(candle)

    def on_candle(self, candle: Candle) -> None:
        # Invariant: Freeze fresh trade entries after 12:30 IST on expiry days to eliminate 0DTE gamma spikes (EXPIRY_CUTOFF_TIME)
        if is_expiry_day(candle.timestamp) and (candle.timestamp.time() >= EXPIRY_ENTRY_CUTOFF_TIME or candle.timestamp.time() >= EXPIRY_CUTOFF_TIME):
            self.pending_rejection = None
            self.logger.info(
                f"[EXPIRY GAMMA CUTOFF] Signals blocked after {EXPIRY_ENTRY_CUTOFF_TIME.strftime('%H:%M')} IST on expiry days."
            )
            return

        # Invariant: Volatility safety - halt entries if India VIX / IV expands violently (> 24.0)
        current_vix = getattr(candle, "india_vix", 15.0)
        if current_vix > MAX_INDIA_VIX:
            self.logger.warning(
                f"[VOLATILITY EXPANSION GATED] India VIX ({current_vix}) exceeds ceiling of {MAX_INDIA_VIX}. Halting fresh entries."
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

        # 3. Check Pending Rejection Confirmation (Candle Confirmation Filter)
        if self.pending_rejection is not None:
            confirmed, reason = self.rejection_detector.validate_confirmation(
                self.pending_rejection, candle
            )
            if confirmed:
                sig = self.pending_rejection
                self.pending_rejection = None
                self.logger.info(
                    f"[CONFIRMATION CONFIRMED] {sig.description} confirmed by candle {candle.timestamp.strftime('%H:%M')} ({reason})."
                )
                if self.dispatch_fn:
                    msg = AgentMessage(
                        msg_id=f"SIG-{uuid.uuid4().hex[:6].upper()}",
                        sender=self.name,
                        recipient="Coder",
                        msg_type=MessageType.STRATEGY_SIGNAL,
                        payload={
                            "signal_type": sig.signal_type.value,
                            "spot_price": candle.close,
                            "candle": candle,
                            "rejection_candle": sig.candle,
                            "zone_name": sig.zone.name,
                            "zone_level": sig.zone.level,
                            "wick_ratio": sig.wick_ratio,
                            "confidence": sig.confidence,
                            "description": f"{sig.description} [Confirmed: {reason}]",
                            "timestamp": candle.timestamp,
                        },
                        timestamp=candle.timestamp,
                    )
                    self.dispatch_fn(msg)
                return
            else:
                self.logger.info(
                    f"[CONFIRMATION FILTER] Rejection invalidated: {reason}. Signal cancelled."
                )
                self.pending_rejection = None

        # 4. Identify S/R Zones (including Daily & Weekly higher timeframe levels)
        ib = self.regime_filter.ib_tracker.get_ib()
        zones = self.rejection_detector.identify_sr_zones(
            ib=ib,
            spot_price=candle.close,
            daily_levels=self.daily_levels,
            weekly_levels=self.weekly_levels,
        )

        # 5. Check for 5-min Rejection Candle (wick ratio >= 50%)
        rejection_signal = self.rejection_detector.detect_rejection(candle, zones)
        if rejection_signal.signal_type == SignalType.NONE:
            return

        if self.require_confirmation:
            self.pending_rejection = rejection_signal
            target_direction = "cross above high" if rejection_signal.signal_type == SignalType.BULLISH_REJECTION else "cross below low"
            self.logger.info(
                f"[REJECTION DETECTED - AWAITING CONFIRMATION] {rejection_signal.description}. "
                f"Waiting for subsequent candle to {target_direction}..."
            )
            return

        # Immediate dispatch when confirmation filter is disabled
        self.logger.info(
            f"[EDGE CONFIRMED] {rejection_signal.description} (Confidence: {rejection_signal.confidence})"
        )

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
