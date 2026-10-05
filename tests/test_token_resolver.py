"""
Unit Tests for Dynamic NFO Instrument Master Token Resolver
Validates:
1. Token resolution for valid Nifty CE & PE strikes.
2. Caching behavior (does not redownload if cached today).
3. Fallback handling when offline or API unreachable.
4. Nearest active expiry auto-selection vs explicit expiry selection.
5. DevOpsAgent integration with resolved symbol tokens.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from agents.base import AgentMessage, MessageType
from agents.devops import DevOpsAgent
from nfo_token_resolver import NFOTokenResolver, parse_expiry_date, parse_strike_price

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def sample_instruments_data():
    today = datetime.now(IST).date()
    near_thursday = today + timedelta(days=((3 - today.weekday()) % 7 or 7))
    far_thursday = near_thursday + timedelta(days=28)
    past_date = today - timedelta(days=14)

    near_str = near_thursday.strftime("%d%b%Y").upper()
    far_str = far_thursday.strftime("%d%b%Y").upper()
    past_str = past_date.strftime("%d%b%Y").upper()

    near_tag = near_thursday.strftime("%d%b%y").upper()
    far_tag = far_thursday.strftime("%d%b%y").upper()
    past_tag = past_date.strftime("%d%b%y").upper()

    return {
        "updated_at": datetime.now(IST).isoformat(),
        "count": 6,
        "instruments": [
            # Past expiry (should be filtered out during nearest lookup)
            {
                "token": "10001",
                "symbol": f"NIFTY{past_tag}25000CE",
                "name": "NIFTY",
                "expiry": past_str,
                "strike": 25000.0,
                "lotsize": 25,
                "instrumenttype": "OPTIDX",
                "exch_seg": "NFO",
                "option_type": "CE",
            },
            # Near active expiry
            {
                "token": "20001",
                "symbol": f"NIFTY{near_tag}25000CE",
                "name": "NIFTY",
                "expiry": near_str,
                "strike": 25000.0,
                "lotsize": 25,
                "instrumenttype": "OPTIDX",
                "exch_seg": "NFO",
                "option_type": "CE",
            },
            {
                "token": "20002",
                "symbol": f"NIFTY{near_tag}25000PE",
                "name": "NIFTY",
                "expiry": near_str,
                "strike": 25000.0,
                "lotsize": 25,
                "instrumenttype": "OPTIDX",
                "exch_seg": "NFO",
                "option_type": "PE",
            },
            {
                "token": "20003",
                "symbol": f"NIFTY{near_tag}24950PE",
                "name": "NIFTY",
                "expiry": near_str,
                "strike": 24950.0,
                "lotsize": 25,
                "instrumenttype": "OPTIDX",
                "exch_seg": "NFO",
                "option_type": "PE",
            },
            # Far active expiry
            {
                "token": "30001",
                "symbol": f"NIFTY{far_tag}25000CE",
                "name": "NIFTY",
                "expiry": far_str,
                "strike": 25000.0,
                "lotsize": 25,
                "instrumenttype": "OPTIDX",
                "exch_seg": "NFO",
                "option_type": "CE",
            },
            {
                "token": "30002",
                "symbol": f"NIFTY{far_tag}25000PE",
                "name": "NIFTY",
                "expiry": far_str,
                "strike": 25000.0,
                "lotsize": 25,
                "instrumenttype": "OPTIDX",
                "exch_seg": "NFO",
                "option_type": "PE",
            },
        ],
    }


def test_token_resolution_ce_and_pe(tmp_path: Path, sample_instruments_data):
    cache_file = tmp_path / "nfo_instruments.json"
    cache_file.write_text(json.dumps(sample_instruments_data), encoding="utf-8")

    resolver = NFOTokenResolver(cache_path=cache_file, auto_load=True)
    assert resolver.is_cache_fresh()

    # Resolve 25000 CE
    sym_ce, tok_ce = resolver.resolve_token("NIFTY", 25000.0, "CE")
    assert "25000CE" in sym_ce
    assert tok_ce == "20001"

    # Resolve 25000 PE
    sym_pe, tok_pe = resolver.resolve_token("NIFTY", 25000.0, "PE")
    assert "25000PE" in sym_pe
    assert tok_pe == "20002"

    # Resolve 24950 PE
    sym_hedge, tok_hedge = resolver.resolve_token("NIFTY", 24950.0, "PE")
    assert "24950PE" in sym_hedge
    assert tok_hedge == "20003"


def test_caching_behavior_does_not_redownload_if_cached_today(tmp_path: Path, sample_instruments_data):
    cache_file = tmp_path / "nfo_instruments.json"
    cache_file.write_text(json.dumps(sample_instruments_data), encoding="utf-8")

    with patch("urllib.request.urlopen") as mock_urlopen:
        resolver = NFOTokenResolver(cache_path=cache_file, auto_load=True)
        assert resolver.is_cache_fresh()
        # Verify urlopen was NOT invoked because today's cache is present and valid
        mock_urlopen.assert_not_called()

    # Assert indexed correctly
    sym, tok = resolver.resolve_token("NIFTY", 25000.0, "CE")
    assert tok == "20001"


def test_offline_fallback_handling(tmp_path: Path):
    absent_cache = tmp_path / "absent_instruments.json"

    # Mock urllib urlopen raising network connection error
    with patch("urllib.request.urlopen", side_effect=RuntimeError("DNS resolution failed")):
        resolver = NFOTokenResolver(
            cache_path=absent_cache,
            master_url="https://invalid.example.com/ScripMaster.json",
            auto_load=True,
        )

    # Cache file was not created, but resolver does not raise exception
    assert not absent_cache.exists()

    # Synthetic fallback resolution
    sym_ce, tok_ce = resolver.resolve_token("NIFTY", 25000.0, "CE")
    assert "NIFTY" in sym_ce
    assert "25000CE" in sym_ce
    assert tok_ce != "0"
    assert tok_ce.isdigit()

    sym_pe, tok_pe = resolver.resolve_token("NIFTY", 24900.0, "PE")
    assert "24900PE" in sym_pe
    assert tok_pe != "0"
    assert tok_pe.isdigit()


def test_nearest_vs_explicit_expiry_selection(tmp_path: Path, sample_instruments_data):
    cache_file = tmp_path / "nfo_instruments.json"
    cache_file.write_text(json.dumps(sample_instruments_data), encoding="utf-8")

    resolver = NFOTokenResolver(cache_path=cache_file, auto_load=True)

    # 1. Without expiry: picks nearest active expiry (token "20001")
    sym_near, tok_near = resolver.resolve_token("NIFTY", 25000.0, "CE", expiry_date=None)
    assert tok_near == "20001"

    # 2. With far expiry: picks the explicit far expiry (token "30001")
    far_str = sample_instruments_data["instruments"][4]["expiry"]
    sym_far, tok_far = resolver.resolve_token("NIFTY", 25000.0, "CE", expiry_date=far_str)
    assert tok_far == "30001"
    assert sym_far != sym_near


def test_devops_agent_order_execution_with_token_resolver(tmp_path: Path, sample_instruments_data):
    cache_file = tmp_path / "nfo_instruments.json"
    cache_file.write_text(json.dumps(sample_instruments_data), encoding="utf-8")

    resolver = NFOTokenResolver(cache_path=cache_file, auto_load=True)

    dispatched_msgs: list[AgentMessage] = []
    devops = DevOpsAgent(
        dispatch_fn=lambda msg: dispatched_msgs.append(msg),
        paper_trading=True,
        token_resolver=resolver,
    )

    order_payload = {
        "trade_id": "SPD-TEST-001",
        "spread_type": "BULL_PUT_SPREAD",
        "net_credit": 20.0,
        "legs": [
            {
                "symbol": "NIFTY_24950_PE",
                "strike": 24950.0,
                "option_type": "PE",
                "action": "BUY",
                "quantity": 65,
                "price": 55.0,
            },
            {
                "symbol": "NIFTY_25000_PE",
                "strike": 25000.0,
                "option_type": "PE",
                "action": "SELL",
                "quantity": 65,
                "price": 75.0,
            },
        ],
    }

    # Dispatch order through DevOps
    devops.dispatch_order(order_payload)

    assert len(dispatched_msgs) == 1
    conf_msg = dispatched_msgs[0]
    assert conf_msg.msg_type == MessageType.EXECUTION_CONFIRMATION

    executed_legs = conf_msg.payload["executed_legs"]
    assert len(executed_legs) == 2

    # Verify BUY leg
    buy_leg = executed_legs[0]
    assert buy_leg["symboltoken"] == "20003"
    assert "24950PE" in buy_leg["tradingsymbol"]
    assert buy_leg["status"] == "FILLED"

    # Verify SELL leg
    sell_leg = executed_legs[1]
    assert sell_leg["symboltoken"] == "20002"
    assert "25000PE" in sell_leg["tradingsymbol"]
    assert sell_leg["status"] == "FILLED"
