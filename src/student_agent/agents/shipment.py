from __future__ import annotations

from typing import Any

from ..a2a import A2AMessage, Agent, CaseContext
from ..timeline import Window, claimed_topic, delivered_late, in_window, parse_time

SUMMARY_TOPICS = {"late_delivery_seller", "late_delivery_logistics", "unsupported_claim"}

# shipment summary field -> order row field
SUMMARY_FIELDS = {
    "order_status": "order_status",
    "delivered_carrier_at": "order_delivered_carrier_date",
    "delivered_customer_at": "order_delivered_customer_date",
    "estimated_delivery_at": "order_estimated_delivery_date",
}
EVENT_ACTOR_VERDICT = {"seller": "seller_delay", "logistics_provider": "logistics_delay"}


class ShipmentAgent(Agent):
    """Judge delivery timeline and seller/logistics lateness."""

    actor = "shipment-agent"
    tools = frozenset({"get_shipment_summary"})

    async def handle(self, ctx: CaseContext, message: A2AMessage) -> A2AMessage:
        order_id = message.payload["order_id"]
        window = Window.from_payload(message.payload.get("window"))
        episode = message.payload.get("episode_order") or {}
        episode_source = message.payload.get("episode_source", "get_order")

        # The episode row and item shipping limits already give the verdict; the summary is only
        # fetched where its events corroborate the conclusion (and its evidence gets cited).
        summary = None
        if (
            not episode
            or delivered_late(episode)
            or claimed_topic(ctx.case) in SUMMARY_TOPICS
            or message.payload.get("duplicated_records")
        ):
            summary = await ctx.fetch(self, "get_shipment_summary", order_id=order_id)
            if summary is None:
                return self.reply(message, "insufficient_evidence", _missing(episode))
        data = summary.data if summary is not None and isinstance(summary.data, dict) else {}
        if not episode:
            episode = {row_field: data.get(field) for field, row_field in SUMMARY_FIELDS.items()}

        conflicts = []
        if data and any(
            data.get(field) != episode.get(row_field) for field, row_field in SUMMARY_FIELDS.items()
        ):
            conflicts.append(
                {
                    "field": "shipment_timeline",
                    "sources": ["get_shipment_summary", episode_source],
                    "selected_source": episode_source,
                    "resolution_code": "CASE_TIMELINE_SCOPE",
                }
            )

        raw_limits = data.get("shipping_limits") if data else message.payload.get("items")
        limits = in_window(_dicts(raw_limits), window, "shipping_limit_at")
        events = in_window(_dicts(data.get("events")), window, "event_at")
        opened_at = parse_time(ctx.case.get("opened_at"))
        verdict, late_sellers = _verdict(episode, limits, events, opened_at)
        milestones = (
            "order_purchase_timestamp",
            "order_approved_at",
            "order_delivered_carrier_date",
            "order_delivered_customer_date",
            "order_estimated_delivery_date",
        )
        payload: dict[str, Any] = {
            "shipment_analysis": {
                "verdict": verdict,
                "late_seller_ids": late_sellers,
                "timeline_complete": all(parse_time(episode.get(name)) for name in milestones),
            },
            "order_status": episode.get("order_status"),
            "late_event_actors": sorted(
                {str(event.get("actor")) for event in events if _is_late_event(event)}
            ),
            "conflicts": conflicts,
            "evidence": {"shipment": summary.ref} if summary is not None else {},
        }
        return self.reply(message, "findings", payload, [summary.ref] if summary else [])


def _verdict(
    episode: dict[str, Any],
    limits: list[dict[str, Any]],
    events: list[dict[str, Any]],
    opened_at: Any,
) -> tuple[str, list[str]]:
    status = episode.get("order_status")
    carrier = parse_time(episode.get("order_delivered_carrier_date"))
    delivered = parse_time(episode.get("order_delivered_customer_date"))
    estimated = parse_time(episode.get("order_estimated_delivery_date"))
    late_sellers = list(
        dict.fromkeys(
            str(limit.get("seller_id"))
            for limit in limits
            if carrier is not None
            and (deadline := parse_time(limit.get("shipping_limit_at"))) is not None
            and carrier > deadline
        )
    )
    event_verdicts = {
        EVENT_ACTOR_VERDICT[event.get("actor")]
        for event in events
        if _is_late_event(event) and event.get("actor") in EVENT_ACTOR_VERDICT
    }

    if status == "returned":
        return "returned", []
    if delivered is not None and estimated is not None:
        if delivered <= estimated:
            return ("conflicting" if event_verdicts else "on_time"), []
        by_dates = "seller_delay" if late_sellers else "logistics_delay"
        if event_verdicts and event_verdicts != {by_dates}:
            return "conflicting", []
        return by_dates, late_sellers if by_dates == "seller_delay" else []
    if status in {"canceled", "unavailable"}:
        return "insufficient_evidence", []
    if carrier is not None and estimated is not None and opened_at and opened_at > estimated:
        return "lost", []
    return "insufficient_evidence", []


def _missing(episode: dict[str, Any]) -> dict[str, Any]:
    return {
        "shipment_analysis": {
            "verdict": "insufficient_evidence",
            "late_seller_ids": [],
            "timeline_complete": False,
        },
        "order_status": episode.get("order_status"),
        "late_event_actors": [],
        "conflicts": [],
        "evidence": {},
    }


def _is_late_event(event: dict[str, Any]) -> bool:
    return event.get("event_type") == "delivered_late" and event.get("status") == "confirmed"


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []
