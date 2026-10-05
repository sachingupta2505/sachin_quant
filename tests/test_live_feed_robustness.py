"""
Regression tests for LiveMarketFeed robustness.
Verifies null-pointer defense when ib_tracker.get_ib() returns None after 09:45 IST,
ensuring graceful fallback to raw tick prices without throwing AttributeError.
"""

import threading
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo
import pytest

from bus import SystemBus
from main_runner import live_market_feed_worker

IST = ZoneInfo("Asia/Kolkata")


def test_live_market_feed_none_ib_fallback(tmp_path: Path):
    """
    Test scenario where ib_tracker.get_ib() returns None at 09:45 IST.
    Assert that live_market_feed_worker handles None without throwing AttributeError,
    and falls back to raw tick ib_high and ib_low.
    """
    db_path = tmp_path / "test_bus.db"
    bus = SystemBus(db_path=db_path)
    stop_event = threading.Event()

    # Mock AngelAuth to return valid authentication and tick quotes
    mock_auth_instance = MagicMock()
    mock_auth_instance.login.return_value = True
    mock_auth_instance.smart_api.ltpData.return_value = {
        "status": True,
        "data": {"ltp": 25120.5},
    }

    # Simulate post-09:45 IST time (e.g. 09:50:00)
    fake_now = datetime(2026, 10, 5, 9, 50, 0, tzinfo=IST)

    with patch("main_runner.AngelAuth", return_value=mock_auth_instance), \
         patch("main_runner.datetime") as mock_datetime, \
         patch("main_runner.InitialBalanceTracker") as mock_tracker_cls:

        mock_datetime.now.return_value = fake_now
        mock_datetime.fromisoformat = datetime.fromisoformat

        # Configure tracker mock to return None for get_ib()
        mock_tracker_instance = MagicMock()
        mock_tracker_instance.get_ib.return_value = None
        mock_tracker_cls.return_value = mock_tracker_instance

        # Execute 1 tick of the live_market_feed_worker
        # Should not raise AttributeError: 'NoneType' object has no attribute 'high'
        try:
            live_market_feed_worker(
                bus=bus,
                stop_event=stop_event,
                poll_interval=0.01,
                max_ticks=1,
            )
        except AttributeError as e:
            pytest.fail(f"live_market_feed_worker raised AttributeError when get_ib() is None: {e}")

    # Verify that INITIAL_BALANCE_LOCKED event was published with fallback raw tick values
    events = bus.consume(topic="INITIAL_BALANCE_LOCKED", target="ALL")
    assert len(events) >= 1, "INITIAL_BALANCE_LOCKED event must be published"
    payload = events[0].payload
    assert payload["ib_high"] == 25120.5
    assert payload["ib_low"] == 25120.5


def test_live_market_feed_valid_ib(tmp_path: Path):
    """
    Test scenario where ib_tracker.get_ib() returns a valid InitialBalance.
    Assert that live_market_feed_worker uses the tracker's high and low.
    """
    db_path = tmp_path / "test_bus.db"
    bus = SystemBus(db_path=db_path)
    stop_event = threading.Event()

    mock_auth_instance = MagicMock()
    mock_auth_instance.login.return_value = True
    mock_auth_instance.smart_api.ltpData.return_value = {
        "status": True,
        "data": {"ltp": 25150.0},
    }

    fake_now = datetime(2026, 10, 5, 9, 50, 0, tzinfo=IST)

    mock_ib = MagicMock()
    mock_ib.high = 25200.0
    mock_ib.low = 25000.0

    with patch("main_runner.AngelAuth", return_value=mock_auth_instance), \
         patch("main_runner.datetime") as mock_datetime, \
         patch("main_runner.InitialBalanceTracker") as mock_tracker_cls:

        mock_datetime.now.return_value = fake_now
        mock_datetime.fromisoformat = datetime.fromisoformat

        mock_tracker_instance = MagicMock()
        mock_tracker_instance.get_ib.return_value = mock_ib
        mock_tracker_cls.return_value = mock_tracker_instance

        live_market_feed_worker(
            bus=bus,
            stop_event=stop_event,
            poll_interval=0.01,
            max_ticks=1,
        )

    events = bus.consume(topic="INITIAL_BALANCE_LOCKED", target="ALL")
    assert len(events) >= 1
    payload = events[0].payload
    assert payload["ib_high"] == 25200.0
    assert payload["ib_low"] == 25000.0
