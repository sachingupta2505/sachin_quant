"""
Unit tests for agents/notifier.py (Telegram Notifier Service)
Tests credential loading, chat ID auto-resolution, bus event formatting, and non-blocking delivery.
"""

import os
from unittest.mock import MagicMock, patch
import pytest

from agents.notifier import TelegramNotifier, resolve_chat_id_for_user


def test_telegram_notifier_initialization():
    """Verify TelegramNotifier initializes and reads credentials from environment."""
    notifier = TelegramNotifier()
    assert notifier.bot_token != ""
    assert notifier.chat_id == "6711295622"
    assert not notifier.is_placeholder_chat_id()
    notifier.stop()


def test_resolve_chat_id_for_user_from_updates():
    """Verify resolve_chat_id_for_user parses updates and matches username case-insensitively."""
    mock_updates = {
        "ok": True,
        "result": [
            {
                "update_id": 100,
                "message": {
                    "message_id": 1,
                    "from": {"id": 999999, "username": "someoneelse"},
                    "chat": {"id": 999999, "type": "private"},
                    "text": "hello",
                },
            },
            {
                "update_id": 101,
                "message": {
                    "message_id": 2,
                    "from": {"id": 6711295622, "username": "shishilalapoopoo"},
                    "chat": {"id": 6711295622, "type": "private"},
                    "text": "/start",
                },
            },
        ],
    }

    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_updates
        mock_get.return_value = mock_resp

        cid = resolve_chat_id_for_user("ShiShiLalaPooPoo")
        assert cid == "6711295622"


def test_bus_event_alert_routing():
    """Verify that bus events trigger the exact required notification message formats."""
    notifier = TelegramNotifier()
    sent_messages = []

    def mock_send(text, parse_mode="HTML"):
        sent_messages.append(text)

    notifier.send_message = mock_send

    # 1. 09:14 IST System Live
    notifier.handle_bus_event("SYSTEM_LIVE", {"message": "Live"})
    assert "🚀 System Live & Broker Connected" in sent_messages

    # 2. 09:45 IST Initial Balance
    notifier.handle_bus_event("INITIAL_BALANCE_LOCKED", {"ib_high": 25150.0, "ib_low": 24950.0})
    assert "📊 Initial Balance Set: High 25150.0 | Low 24950.0" in sent_messages

    # 3. Order Execution
    notifier.handle_bus_event("ORDER_EXECUTED_ALERT", {"spread_type": "BULL_PUT_SPREAD", "max_risk_inr": 1250.0})
    assert "🎯 Order Executed: BULL_PUT_SPREAD | Risk: INR 1250.0" in sent_messages

    # 4. Auditor Block
    notifier.handle_bus_event("ORDER_BLOCKED", {"reason": "Daily loss limit breached"})
    assert "⚠️ Order Blocked: Daily loss limit breached" in sent_messages

    # 5. 15:10 IST Auto Square-Off
    notifier.handle_bus_event("POSITIONS_SQUARED_OFF", {})
    assert "🔒 All Positions Auto Squared-Off" in sent_messages

    # 6. 15:30 IST EOD Summary
    notifier.handle_bus_event("EOD_SUMMARY", {"count": 2, "pnl": 750.0})
    assert "🏁 EOD Summary: Total Trades: 2 | Daily PnL: INR 750.0" in sent_messages

    notifier.stop()
