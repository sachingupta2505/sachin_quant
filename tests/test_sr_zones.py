"""
Unit Tests for Daily and Weekly S/R Zones & Historical Reference Levels
Validates:
1. Computation and naming of DAILY_HIGH_RES, DAILY_LOW_SUP, DAILY_CLOSE_PIVOT,
   WEEKLY_HIGH_RES, and WEEKLY_LOW_SUP.
2. Rejection detection against Daily and Weekly zones.
3. fetch_historical_reference_levels helper with SmartAPI and cache fallbacks.
4. ArchitectAgent end-to-end integration with daily/weekly zones.
"""

from __future__ import annotations

import json
from datetime import datetime, time
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from agents.architect import ArchitectAgent
from agents.base import AgentMessage, MessageType
from execution_engine import (
    RejectionDetector,
    SignalType,
    SRZone,
    fetch_historical_reference_levels,
)
from regime_filter import Candle, InitialBalance

IST = ZoneInfo("Asia/Kolkata")


def make_candle(
    hour: int,
    minute: int,
    open_p: float,
    high_p: float,
    low_p: float,
    close_p: float,
    volume: int = 1000,
    day: int = 5,
    month: int = 10,
    year: int = 2026,
) -> Candle:
    dt = datetime(year, month, day, hour, minute, tzinfo=IST)
    return Candle(
        timestamp=dt,
        open=open_p,
        high=high_p,
        low=low_p,
        close=close_p,
        volume=volume,
    )


def test_identify_sr_zones_daily_and_weekly_dicts():
    detector = RejectionDetector()
    ib = InitialBalance(high=25150.0, low=25000.0, range=150.0, mid=25075.0, is_locked=True, candle_count=6)

    daily_levels = {"pdh": 25220.0, "pdl": 24920.0, "pdc": 25080.0}
    weekly_levels = {"pwh": 25350.0, "pwl": 24800.0}

    zones = detector.identify_sr_zones(
        ib=ib,
        spot_price=25070.0,
        daily_levels=daily_levels,
        weekly_levels=weekly_levels,
    )

    zone_map = {z.name: z.level for z in zones}

    assert zone_map["DAILY_HIGH_RES"] == 25220.0
    assert zone_map["DAILY_LOW_SUP"] == 24920.0
    assert zone_map["DAILY_CLOSE_PIVOT"] == 25080.0
    assert zone_map["WEEKLY_HIGH_RES"] == 25350.0
    assert zone_map["WEEKLY_LOW_SUP"] == 24800.0
    assert zone_map["IB_HIGH_RESISTANCE"] == 25150.0
    assert zone_map["IB_LOW_SUPPORT"] == 25000.0


def test_identify_sr_zones_direct_kwargs():
    detector = RejectionDetector()
    zones = detector.identify_sr_zones(
        ib=None,
        spot_price=25000.0,
        pdh=25200.0,
        pdl=24850.0,
        pdc=25010.0,
        pwh=25400.0,
        pwl=24700.0,
    )

    names = {z.name for z in zones}
    assert "DAILY_HIGH_RES" in names
    assert "DAILY_LOW_SUP" in names
    assert "DAILY_CLOSE_PIVOT" in names
    assert "WEEKLY_HIGH_RES" in names
    assert "WEEKLY_LOW_SUP" in names


def test_rejection_detection_at_daily_high_resistance():
    detector = RejectionDetector(min_wick_ratio=0.50, min_candle_range=12.0)
    zones = [
        SRZone(name="DAILY_HIGH_RES", level=25200.0, band_pts=12.0),
        SRZone(name="DAILY_LOW_SUP", level=24900.0, band_pts=12.0),
    ]

    # Shooting Star testing 25200 Resistance:
    # Open: 25160, High: 25208 (tests 25200 zone [25188, 25212]), Low: 25155, Close: 25158
    # Range: 25208 - 25155 = 53 pts.
    # Upper Wick: 25208 - max(25160, 25158) = 48 pts.
    # Wick Ratio: 48 / 53 = 90.5% >= 50%
    candle = make_candle(10, 30, open_p=25160.0, high_p=25208.0, low_p=25155.0, close_p=25158.0)

    signal = detector.detect_rejection(candle, zones)
    assert signal.signal_type == SignalType.BEARISH_REJECTION
    assert signal.zone.name == "DAILY_HIGH_RES"
    assert signal.wick_ratio >= 0.50
    assert "Bearish Rejection" in signal.description


def test_rejection_detection_at_daily_low_support():
    detector = RejectionDetector(min_wick_ratio=0.50, min_candle_range=12.0)
    zones = [
        SRZone(name="DAILY_LOW_SUP", level=24900.0, band_pts=12.0),
    ]

    # Hammer testing 24900 Support:
    # Open: 24930, High: 24935, Low: 24892 (tests 24900 zone [24888, 24912]), Close: 24928
    # Range: 24935 - 24892 = 43 pts.
    # Lower Wick: min(24930, 24928) - 24892 = 36 pts.
    # Wick Ratio: 36 / 43 = 83.7% >= 50%
    candle = make_candle(10, 45, open_p=24930.0, high_p=24935.0, low_p=24892.0, close_p=24928.0)

    signal = detector.detect_rejection(candle, zones)
    assert signal.signal_type == SignalType.BULLISH_REJECTION
    assert signal.zone.name == "DAILY_LOW_SUP"
    assert signal.wick_ratio >= 0.50


def test_fetch_historical_reference_levels_smartapi_success(tmp_path: Path):
    cache_file = str(tmp_path / "test_ref_levels.json")
    mock_smart_api = MagicMock()

    # Provide simulated daily candles for the past 2 weeks
    mock_smart_api.getCandleData.return_value = {
        "status": True,
        "data": [
            ["2026-09-21T09:15:00+05:30", 24900.0, 25100.0, 24850.0, 25050.0, 50000],
            ["2026-09-22T09:15:00+05:30", 25050.0, 25200.0, 25000.0, 25150.0, 60000],
            ["2026-09-23T09:15:00+05:30", 25150.0, 25250.0, 25100.0, 25180.0, 55000],
            ["2026-09-24T09:15:00+05:30", 25180.0, 25300.0, 25120.0, 25280.0, 70000],
            ["2026-09-25T09:15:00+05:30", 25280.0, 25350.0, 25220.0, 25310.0, 80000],
            ["2026-10-02T09:15:00+05:30", 25300.0, 25420.0, 25280.0, 25390.0, 65000],
        ],
    }

    res = fetch_historical_reference_levels(auth=mock_smart_api, cache_file=cache_file)
    assert res["source"] == "SMART_API"
    assert res["pdh"] == 25420.0
    assert res["pdl"] == 25280.0
    assert res["pdc"] == 25390.0
    assert "pwh" in res and res["pwh"] > 0
    assert "pwl" in res and res["pwl"] > 0
    assert Path(cache_file).exists()


def test_fetch_historical_reference_levels_cache_fallback(tmp_path: Path):
    cache_file = str(tmp_path / "test_ref_levels.json")
    cached_payload = {
        "pdh": 25180.0,
        "pdl": 24950.0,
        "pdc": 25090.0,
        "pwh": 25300.0,
        "pwl": 24820.0,
    }
    Path(cache_file).write_text(json.dumps(cached_payload), encoding="utf-8")

    mock_smart_api = MagicMock()
    mock_smart_api.getCandleData.side_effect = RuntimeError("Network timeout")

    res = fetch_historical_reference_levels(auth=mock_smart_api, cache_file=cache_file)
    assert res["source"] == "LOCAL_CACHE"
    assert res["pdh"] == 25180.0
    assert res["pdl"] == 24950.0
    assert res["pdc"] == 25090.0
    assert res["pwh"] == 25300.0
    assert res["pwl"] == 24820.0


def test_architect_agent_emits_signal_at_daily_support():
    dispatched: list[AgentMessage] = []

    daily_levels = {"pdh": 25250.0, "pdl": 24950.0, "pdc": 25050.0}
    weekly_levels = {"pwh": 25400.0, "pwl": 24800.0}

    architect = ArchitectAgent(
        dispatch_fn=lambda msg: dispatched.append(msg),
        daily_levels=daily_levels,
        weekly_levels=weekly_levels,
    )

    # 1. Feed 6 IB candles to establish and lock Initial Balance (09:15 - 09:40)
    for m in range(15, 45, 5):
        c = make_candle(9, m, open_p=25040.0, high_p=25080.0, low_p=25020.0, close_p=25050.0)
        architect.on_candle(c)

    # 2. Lock IB
    architect.regime_filter.ib_tracker.lock_manual()

    # 3. Feed a candle at 10:00 IST that bounces off DAILY_LOW_SUP (24950.0)
    # Range: 25000 - 24945 = 55 pts.
    # Lower Wick: 24990 - 24945 = 45 pts. Wick ratio: 45 / 55 = 81.8% >= 50%
    rejection_candle = make_candle(10, 0, open_p=24995.0, high_p=25000.0, low_p=24945.0, close_p=24990.0)
    architect.on_candle(rejection_candle)

    # 4. Feed subsequent confirmation candle (10:05 IST) crossing above rejection high 25000
    confirm_candle = make_candle(10, 5, open_p=24992.0, high_p=25010.0, low_p=24985.0, close_p=25005.0)
    architect.on_candle(confirm_candle)

    # Assert that Architect detected rejection and dispatched a strategy signal upon confirmation
    assert len(dispatched) >= 1
    signal_msg = dispatched[-1]
    assert signal_msg.msg_type == MessageType.STRATEGY_SIGNAL
    assert signal_msg.payload["zone_name"] == "DAILY_LOW_SUP"
    assert signal_msg.payload["signal_type"] == SignalType.BULLISH_REJECTION.value
