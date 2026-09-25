from __future__ import annotations

from ..a2a import Agent
from .conflict import ConflictAgent
from .entity import EntityAgent
from .order import OrderAgent
from .payment import PaymentAgent
from .policy import PolicyAgent
from .shipment import ShipmentAgent
from .verifier import VerifierAgent

AGENT_TYPES: tuple[type[Agent], ...] = (
    EntityAgent,
    OrderAgent,
    ShipmentAgent,
    PaymentAgent,
    ConflictAgent,
    PolicyAgent,
    VerifierAgent,
)


def build_agents() -> dict[str, Agent]:
    return {agent_type.actor: agent_type() for agent_type in AGENT_TYPES}


__all__ = ["AGENT_TYPES", "build_agents"]
