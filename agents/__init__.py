"""
Multi-Agent Algorithmic Trading Architecture
Contains autonomous agents:
- ArchitectAgent (Strategy, Regime, Invariants)
- CoderAgent (Dynamic Order Payload Generation & Risk Adjustments)
- AuditorAgent (FSM Compliance, Kill-Switch, MAE/MFE Journal)
- DevOpsAgent (Broker Connectivity, TOTP, Health Checks, Execution Dispatch)
"""

from agents.architect import ArchitectAgent
from agents.auditor import AuditorAgent
from agents.coder import CoderAgent
from agents.devops import DevOpsAgent

__all__ = ["ArchitectAgent", "CoderAgent", "AuditorAgent", "DevOpsAgent"]
