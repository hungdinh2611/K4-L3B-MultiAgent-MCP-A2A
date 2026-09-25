from __future__ import annotations

import asyncio
from typing import Any

from .a2a import COORDINATOR, A2AMessage, Agent, CaseContext, ProtocolViolation, unique_refs
from .agents import build_agents
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ORDER_SPECIALISTS = ("order-agent", "shipment-agent", "payment-agent")


class Coordinator:
    """Hub of the A2A workflow: assigns tasks, fans out specialists, routes the review chain."""

    actor = COORDINATOR

    def __init__(self, agents: dict[str, Agent] | None = None) -> None:
        self.agents = agents if agents is not None else build_agents()

    def task(self, ctx: CaseContext, recipient: str, intent: str, **payload: Any) -> A2AMessage:
        return A2AMessage(ctx.case_id, self.actor, recipient, intent, payload)

    async def investigate(self, ctx: CaseContext) -> list[A2AMessage]:
        """Entity first, then order facts, then shipment and payment in parallel."""
        entity = await ctx.dispatch(
            self.agents["entity-agent"], self.task(ctx, "entity-agent", "resolve_entity")
        )
        order_id = entity.payload.get("order_id")
        if not order_id:
            return [entity]
        episode = entity.payload.get("episode") or {}
        scope = {
            "order_id": order_id,
            "window": episode.get("window"),
            "episode_order": episode.get("order"),
            "episode_source": (
                "get_customer_history" if "customer" in entity.payload.get("evidence", {})
                else "get_order"
            ),
        }  # fmt: skip
        order = await ctx.dispatch(
            self.agents["order-agent"], self.task(ctx, "order-agent", "investigate_order", **scope)
        )
        shipment, payment = await asyncio.gather(
            ctx.dispatch(
                self.agents["shipment-agent"],
                self.task(
                    ctx,
                    "shipment-agent",
                    "investigate_shipment",
                    items=order.payload.get("items"),
                    **scope,
                ),
            ),
            ctx.dispatch(
                self.agents["payment-agent"],
                self.task(
                    ctx,
                    "payment-agent",
                    "investigate_payment",
                    order_value_brl=order.payload.get("order_value_brl"),
                    **scope,
                ),
            ),
        )
        return [entity, order, shipment, payment]

    async def run(self, ctx: CaseContext) -> dict[str, Any]:
        results = await self.investigate(ctx)
        review = A2AMessage(
            ctx.case_id,
            self.actor,
            "conflict-agent",
            "resolve_conflicts",
            {"findings": {message.sender: message.payload for message in results}},
            unique_refs(*(message.evidence_refs for message in results)),
        )
        final = await ctx.route(self.agents, review)
        output = final.payload.get("output")
        if final.intent != "finalize" or not isinstance(output, dict):
            raise ProtocolViolation(f"review chain ended with {final.intent!r} and no output")
        return output


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    return await Coordinator().run(CaseContext(case, gateway, trace))
