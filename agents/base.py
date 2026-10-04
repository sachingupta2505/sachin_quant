"""
Base Agent and Message Protocol for Multi-Agent Trading System
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


class MessageType(str, Enum):
    # Market & Strategy signals
    MARKET_CANDLE = "MARKET_CANDLE"
    STRATEGY_SIGNAL = "STRATEGY_SIGNAL"
    REGIME_UPDATE = "REGIME_UPDATE"

    # Order proposals & compliance
    PROPOSED_ORDER = "PROPOSED_ORDER"
    AUDIT_APPROVED = "AUDIT_APPROVED"
    AUDIT_REJECTED = "AUDIT_REJECTED"

    # Execution & Telemetry
    EXECUTION_REQUEST = "EXECUTION_REQUEST"
    EXECUTION_CONFIRMATION = "EXECUTION_CONFIRMATION"
    POSITION_CLOSED = "POSITION_CLOSED"
    SQUARE_OFF_ALERT = "SQUARE_OFF_ALERT"
    HEALTH_STATUS = "HEALTH_STATUS"


@dataclass
class AgentMessage:
    msg_id: str
    sender: str
    recipient: str
    msg_type: MessageType
    payload: dict[str, Any]
    timestamp: datetime = field(default_factory=lambda: datetime.now(IST))


class BaseAgent:
    """Base class for autonomous trading agents."""

    def __init__(self, name: str):
        self.name = name
        self.logger = logging.getLogger(f"agent.{name.lower()}")
        self._inbox: list[AgentMessage] = []

    def receive_message(self, message: AgentMessage) -> None:
        self._inbox.append(message)
        self.handle_message(message)

    def handle_message(self, message: AgentMessage) -> None:
        """Override in subclasses to handle incoming messages."""
        pass
