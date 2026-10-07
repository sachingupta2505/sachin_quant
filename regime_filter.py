"""
Regime Filter Module for Algorithmic Trading Engine
Pure price action market regime & volatility compression detection with zero lagging indicators.
Features:
1. Initial Balance (IB) Tracker: Tracks 09:15 AM - 09:45 AM IST range (IBH, IBL, IBR).
2. Pure Price Action Compression & Chop Detector:
   - Evaluates candle range compression, body overlaps, and IB relative positioning.
   - Detects COMPRESSED (coiled spring / squeeze), TRENDING (Bullish/Bearish expansion),
     and CHOPPY (rotational noise inside IB).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

IB_START_TIME = time(9, 15)
IB_END_TIME = time(9, 45)


class MarketRegime(str, Enum):
    FORMING_IB = "FORMING_IB"              # Before 09:45 AM IST; IB still developing
    COMPRESSED = "COMPRESSED"              # High-probability squeeze: narrow range coiling
    BREAKOUT_BULLISH = "BREAKOUT_BULLISH"  # Sustained expansion above IB High
    BREAKOUT_BEARISH = "BREAKOUT_BEARISH"  # Sustained expansion below IB Low
    CHOP_CONFINED = "CHOP_CONFINED"        # Range-bound, high candle overlap inside IB


@dataclass(frozen=True)
class Candle:
    """Standard 5-minute (or 1-minute) price bar."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def range(self) -> float:
        return max(self.high - self.low, 0.0)

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low


@dataclass
class InitialBalance:
    """Initial Balance levels computed from 09:15 to 09:45 AM IST."""
    high: float
    low: float
    range: float
    mid: float
    is_locked: bool
    candle_count: int


@dataclass
class RegimeAnalysis:
    """Outcome of pure price action regime evaluation."""
    timestamp: datetime
    regime: MarketRegime
    current_price: float
    initial_balance: Optional[InitialBalance]
    is_compressed: bool
    compression_ratio: float
    overlap_ratio: float
    is_favorable_for_spread: bool
    description: str


class InitialBalanceTracker:
    """
    Collects price data strictly within the 09:15 - 09:45 AM IST window
    and locks IB values upon 09:45:00 IST completion.
    """

    def __init__(self, tz: ZoneInfo = IST):
        self.tz = tz
        self._candles: list[Candle] = []
        self._high: float = float("-inf")
        self._low: float = float("inf")
        self._is_locked: bool = False

    @property
    def is_locked(self) -> bool:
        return self._is_locked

    @property
    def candle_count(self) -> int:
        return len(self._candles)

    def add_candle(self, candle: Candle) -> bool:
        """
        Ingests a candle. If candle falls within 09:15 - 09:45 AM IST,
        it updates IB. If timestamp >= 09:45, locks IB.
        Returns True if candle was ingested into IB calculation.
        """
        ts = candle.timestamp.astimezone(self.tz) if candle.timestamp.tzinfo else candle.timestamp.replace(tzinfo=self.tz)
        t = ts.time()

        if t < IB_START_TIME:
            return False

        if t >= IB_END_TIME:
            # Reached or passed 09:45 AM IST: Lock IB
            self._is_locked = True
            return False

        # Within IB window (09:15 to 09:44:59)
        self._candles.append(candle)
        if candle.high > self._high:
            self._high = candle.high
        if candle.low < self._low:
            self._low = candle.low
        return True

    def ingest_ib_candle(self, candle: Candle) -> bool:
        """Alias for add_candle for compatibility with live feed worker."""
        return self.add_candle(candle)

    def lock_manual(self) -> None:
        """Explicitly lock IB (e.g. at 09:45:00 mark)."""
        self._is_locked = True

    def get_ib(self) -> Optional[InitialBalance]:
        """Returns InitialBalance snapshot if at least one candle was recorded."""
        if not self._candles:
            return None

        ib_range = self._high - self._low
        return InitialBalance(
            high=round(self._high, 2),
            low=round(self._low, 2),
            range=round(ib_range, 2),
            mid=round((self._high + self._low) / 2.0, 2),
            is_locked=self._is_locked,
            candle_count=len(self._candles),
        )

    def reset(self) -> None:
        """Reset for a new trading day."""
        self._candles.clear()
        self._high = float("-inf")
        self._low = float("inf")
        self._is_locked = False


class RegimeFilter:
    """
    Pure price action market regime detector.
    Does NOT use any moving averages, RSI, or lagging indicators.
    Relies purely on:
    - Initial Balance (IBH, IBL, IBR)
    - Consecutive candle range compression ratio (NR compression)
    - Body-to-range ratios
    - Inter-candle overlap (rotational chop vs directional expansion)
    """

    def __init__(
        self,
        compression_threshold: float = 0.35,  # Recent 3-candle range <= 35% of IB range = compression
        overlap_chop_threshold: float = 0.65, # Overlap >= 65% indicates rotational chop
        tz: ZoneInfo = IST,
    ):
        self.compression_threshold = compression_threshold
        self.overlap_chop_threshold = overlap_chop_threshold
        self.tz = tz
        self.ib_tracker = InitialBalanceTracker(tz=tz)

    def ingest_ib_candle(self, candle: Candle) -> bool:
        return self.ib_tracker.add_candle(candle)

    @staticmethod
    def calculate_candle_overlap(c1: Candle, c2: Candle) -> float:
        """
        Calculates overlap percentage between two consecutive candle ranges.
        A high overlap (> 60-70%) is indicative of non-trending chop.
        """
        overlap_high = min(c1.high, c2.high)
        overlap_low = max(c1.low, c2.low)
        if overlap_high <= overlap_low:
            return 0.0

        overlap_span = overlap_high - overlap_low
        min_span = min(c1.range, c2.range)
        if min_span <= 0:
            return 0.0
        return min(overlap_span / min_span, 1.0)

    def evaluate_regime(
        self,
        recent_candles: Sequence[Candle],
        current_time: Optional[datetime] = None,
    ) -> RegimeAnalysis:
        """
        Evaluates current market regime from recent price action and Initial Balance.
        Requires at least 1 candle.
        """
        if not recent_candles:
            raise ValueError("recent_candles cannot be empty for regime evaluation")

        last_candle = recent_candles[-1]
        eval_dt = current_time or last_candle.timestamp
        eval_dt = eval_dt.astimezone(self.tz) if eval_dt.tzinfo else eval_dt.replace(tzinfo=self.tz)
        current_price = last_candle.close

        # Before 09:45 AM IST: Market is still forming Initial Balance
        if eval_dt.time() < IB_END_TIME and not self.ib_tracker.is_locked:
            ib_snapshot = self.ib_tracker.get_ib()
            return RegimeAnalysis(
                timestamp=eval_dt,
                regime=MarketRegime.FORMING_IB,
                current_price=current_price,
                initial_balance=ib_snapshot,
                is_compressed=False,
                compression_ratio=0.0,
                overlap_ratio=0.0,
                is_favorable_for_spread=False,
                description="Pre-09:45 AM: Initial Balance is currently forming.",
            )

        ib = self.ib_tracker.get_ib()
        if ib is None or ib.range <= 0:
            # Fallback if no IB was recorded
            return RegimeAnalysis(
                timestamp=eval_dt,
                regime=MarketRegime.CHOP_CONFINED,
                current_price=current_price,
                initial_balance=None,
                is_compressed=False,
                compression_ratio=0.0,
                overlap_ratio=0.0,
                is_favorable_for_spread=False,
                description="Insufficient IB data recorded.",
            )

        # 1. Calculate Recent Range Compression (last 3 candles vs IB Range)
        eval_window = recent_candles[-3:] if len(recent_candles) >= 3 else recent_candles
        recent_high = max(c.high for c in eval_window)
        recent_low = min(c.low for c in eval_window)
        recent_range = recent_high - recent_low

        compression_ratio = recent_range / ib.range if ib.range > 0 else 1.0
        is_compressed = compression_ratio <= self.compression_threshold

        # 2. Calculate Overlap Ratio (measuring chop / rotation)
        overlaps: list[float] = []
        for i in range(1, len(eval_window)):
            overlaps.append(self.calculate_candle_overlap(eval_window[i - 1], eval_window[i]))
        avg_overlap = sum(overlaps) / len(overlaps) if overlaps else 0.0

        # 3. Pure Price Action Regime Classification
        # Check Breakout expansion beyond IB
        if current_price > ib.high:
            # Above IBH: Bullish expansion
            regime = MarketRegime.BREAKOUT_BULLISH
            description = (
                f"Bullish Breakout: price {current_price:.2f} trading above IB High {ib.high:.2f} "
                f"(IBR: {ib.range:.2f} pts)."
            )
            is_favorable = True

        elif current_price < ib.low:
            # Below IBL: Bearish expansion
            regime = MarketRegime.BREAKOUT_BEARISH
            description = (
                f"Bearish Breakout: price {current_price:.2f} trading below IB Low {ib.low:.2f} "
                f"(IBR: {ib.range:.2f} pts)."
            )
            is_favorable = True

        elif is_compressed:
            # Inside IB and range is coiling tightly (NR compression)
            regime = MarketRegime.COMPRESSED
            description = (
                f"Volatility Compression: 3-bar range ({recent_range:.2f} pts) is only "
                f"{compression_ratio * 100:.1f}% of IB range. Coiling for impending breakout/spread entry."
            )
            is_favorable = True

        elif (abs(current_price - ib.low) <= 20.0 or (last_candle.low <= ib.low and current_price <= ib.low + 35.0)):
            # Testing IB Support Zone: Prime location for Bull Put Spread
            regime = MarketRegime.COMPRESSED
            description = f"Testing Key Support Zone around IB Low {ib.low:.2f}. Potential rejection reversal setup."
            is_favorable = True

        elif (abs(current_price - ib.high) <= 20.0 or (last_candle.high >= ib.high and current_price >= ib.high - 35.0)):
            # Testing IB Resistance Zone: Prime location for Bear Call Spread
            regime = MarketRegime.COMPRESSED
            description = f"Testing Key Resistance Zone around IB High {ib.high:.2f}. Potential rejection reversal setup."
            is_favorable = True

        elif avg_overlap >= self.overlap_chop_threshold:
            # Inside IB with high bar-to-bar overlap: pure chop
            regime = MarketRegime.CHOP_CONFINED
            description = (
                f"Rotational Chop: {avg_overlap * 100:.1f}% candle overlap inside IB range "
                f"[{ib.low:.2f} - {ib.high:.2f}]. Unfavorable for directional continuation."
            )
            is_favorable = False

        else:
            # Inside IB, normal oscillation
            regime = MarketRegime.CHOP_CONFINED
            description = f"Oscillating within Initial Balance range [{ib.low:.2f} - {ib.high:.2f}]."
            is_favorable = False

        return RegimeAnalysis(
            timestamp=eval_dt,
            regime=regime,
            current_price=current_price,
            initial_balance=ib,
            is_compressed=is_compressed,
            compression_ratio=round(compression_ratio, 3),
            overlap_ratio=round(avg_overlap, 3),
            is_favorable_for_spread=is_favorable,
            description=description,
        )
