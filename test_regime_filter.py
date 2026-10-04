"""
Comprehensive Unit Tests for Module 2: RegimeFilter
Validates Initial Balance (IBH, IBL, IBR) tracking (09:15 - 09:45 AM IST),
pure price action volatility/chop compression detection, and breakout regimes.
"""

from __future__ import annotations

from datetime import datetime
import pytest

from regime_filter import (
    IST,
    Candle,
    InitialBalanceTracker,
    MarketRegime,
    RegimeFilter,
)


def make_candle(
    hour: int,
    minute: int,
    open_p: float,
    high_p: float,
    low_p: float,
    close_p: float,
    volume: float = 1000.0,
    day: int = 5,
) -> Candle:
    return Candle(
        timestamp=datetime(2026, 10, day, hour, minute, 0, tzinfo=IST),
        open=open_p,
        high=high_p,
        low=low_p,
        close=close_p,
        volume=volume,
    )


def test_candle_geometry():
    c = make_candle(9, 15, open_p=25000.0, high_p=25050.0, low_p=24980.0, close_p=25030.0)
    assert c.range == 70.0
    assert c.body == 30.0
    assert c.is_bullish is True
    assert c.is_bearish is False
    assert c.upper_wick == 20.0  # 25050 - 25030
    assert c.lower_wick == 20.0  # 25000 - 24980


def test_initial_balance_tracking_and_locking():
    ib_tracker = InitialBalanceTracker()

    # Pre-market candle (09:08 AM) should be rejected
    pre_candle = make_candle(9, 8, 25000, 25010, 24990, 25005)
    assert ib_tracker.add_candle(pre_candle) is False
    assert ib_tracker.candle_count == 0

    # 6 five-minute candles between 09:15 and 09:44
    candles = [
        make_candle(9, 15, 25000, 25060, 24980, 25040),  # H: 25060, L: 24980
        make_candle(9, 20, 25040, 25080, 25020, 25070),  # H: 25080
        make_candle(9, 25, 25070, 25075, 25010, 25015),
        make_candle(9, 30, 25015, 25030, 24950, 24960),  # L: 24950
        make_candle(9, 35, 24960, 25000, 24955, 24995),
        make_candle(9, 40, 24995, 25020, 24980, 25010),
    ]

    for c in candles:
        assert ib_tracker.add_candle(c) is True

    assert ib_tracker.candle_count == 6
    assert ib_tracker.is_locked is False

    ib = ib_tracker.get_ib()
    assert ib is not None
    assert ib.high == 25080.0
    assert ib.low == 24950.0
    assert ib.range == 130.0
    assert ib.mid == 25015.0

    # 09:45 candle should trigger lock and not be added to IB
    post_ib_candle = make_candle(9, 45, 25010, 25100, 25000, 25090)
    assert ib_tracker.add_candle(post_ib_candle) is False
    assert ib_tracker.is_locked is True
    assert ib_tracker.candle_count == 6


def test_regime_forming_ib_prior_to_0945():
    rf = RegimeFilter()
    c1 = make_candle(9, 20, 25000, 25050, 24990, 25020)
    rf.ingest_ib_candle(c1)

    analysis = rf.evaluate_regime([c1], current_time=datetime(2026, 10, 5, 9, 25, tzinfo=IST))
    assert analysis.regime == MarketRegime.FORMING_IB
    assert analysis.is_favorable_for_spread is False
    assert "forming" in analysis.description.lower()


def test_regime_breakout_bullish_and_bearish():
    rf = RegimeFilter()
    # Populate IB range: [25000 - 25100] (Range: 100 pts)
    rf.ingest_ib_candle(make_candle(9, 15, 25020, 25100, 25000, 25050))
    rf.ib_tracker.lock_manual()

    # Case 1: Post 09:45 candle closes above IBH (25100) -> 25130
    bullish_candle = make_candle(9, 50, 25090, 25140, 25080, 25130)
    analysis_bull = rf.evaluate_regime([bullish_candle], current_time=datetime(2026, 10, 5, 9, 50, tzinfo=IST))
    assert analysis_bull.regime == MarketRegime.BREAKOUT_BULLISH
    assert analysis_bull.is_favorable_for_spread is True
    assert "Bullish Breakout" in analysis_bull.description

    # Case 2: Post 09:45 candle closes below IBL (25000) -> 24970
    bearish_candle = make_candle(10, 0, 25010, 25020, 24960, 24970)
    analysis_bear = rf.evaluate_regime([bearish_candle], current_time=datetime(2026, 10, 5, 10, 0, tzinfo=IST))
    assert analysis_bear.regime == MarketRegime.BREAKOUT_BEARISH
    assert analysis_bear.is_favorable_for_spread is True
    assert "Bearish Breakout" in analysis_bear.description


def test_volatility_compression_detection():
    rf = RegimeFilter(compression_threshold=0.35)
    # IB range: [25000 - 25100] (Range: 100 pts)
    rf.ingest_ib_candle(make_candle(9, 15, 25020, 25100, 25000, 25050))
    rf.ib_tracker.lock_manual()

    # 3 narrow-range compressed candles oscillating tightly between 25040 and 25060 (20 pts span <= 35 pts)
    compressed_candles = [
        make_candle(9, 50, 25045, 25055, 25042, 25050),
        make_candle(9, 55, 25050, 25060, 25048, 25052),
        make_candle(10, 0, 25052, 25058, 25045, 25048),
    ]

    analysis = rf.evaluate_regime(compressed_candles, current_time=datetime(2026, 10, 5, 10, 0, tzinfo=IST))
    assert analysis.regime == MarketRegime.COMPRESSED
    assert analysis.is_compressed is True
    assert analysis.compression_ratio <= 0.35
    assert analysis.is_favorable_for_spread is True


def test_rotational_chop_detection():
    rf = RegimeFilter(overlap_chop_threshold=0.60)
    # IB range: [24900 - 25100] (Range: 200 pts)
    rf.ingest_ib_candle(make_candle(9, 15, 25000, 25100, 24900, 25000))
    rf.ib_tracker.lock_manual()

    # Highly overlapping, oscillating bars (50-60 pt range each, almost identical price levels)
    chop_candles = [
        make_candle(10, 0, 25010, 25060, 24990, 25040),
        make_candle(10, 5, 25040, 25065, 24985, 25015),
        make_candle(10, 10, 25015, 25055, 24995, 25035),
    ]

    analysis = rf.evaluate_regime(chop_candles, current_time=datetime(2026, 10, 5, 10, 10, tzinfo=IST))
    assert analysis.regime == MarketRegime.CHOP_CONFINED
    assert analysis.overlap_ratio >= 0.60
    assert analysis.is_favorable_for_spread is False


def test_candle_overlap_calculation():
    # Complete overlap: c1 [100 - 150], c2 [110 - 140]
    c1 = make_candle(10, 0, 105, 150, 100, 120)
    c2 = make_candle(10, 5, 115, 140, 110, 130)
    assert RegimeFilter.calculate_candle_overlap(c1, c2) == 1.0

    # No overlap: c_no_overlap [100 - 120], c3 [130 - 150]
    c_no_overlap = make_candle(10, 0, 105, 120, 100, 115)
    c3 = make_candle(10, 10, 135, 150, 130, 140)
    assert RegimeFilter.calculate_candle_overlap(c_no_overlap, c3) == 0.0


def test_empty_candles_raises_value_error():
    rf = RegimeFilter()
    with pytest.raises(ValueError):
        rf.evaluate_regime([])
