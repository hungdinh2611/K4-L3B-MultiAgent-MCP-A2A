from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any

from ..a2a import A2AMessage, Agent, CaseContext
from ..timeline import Window, brl, claimed_topic, in_window, money, total, unique_rows

REFUND_TOPICS = {"refund_pending", "refund_failed"}
REFUND_DONE = {"completed", "succeeded", "confirmed", "refunded"}
REFUND_OPEN = {"pending", "processing", "requested"}
MISMATCH_OPEN = {"open", "confirmed"}


class PaymentAgent(Agent):
    """Reconcile captures, payment lifecycle and refunds."""

    actor = "payment-agent"
    tools = frozenset({"get_order_payments", "get_payment_timeline", "get_refund_timeline"})

    async def handle(self, ctx: CaseContext, message: A2AMessage) -> A2AMessage:
        order_id = message.payload["order_id"]
        window = Window.from_payload(message.payload.get("window"))
        order_value = money(message.payload.get("order_value_brl"))
        refs: dict[str, str] = {}

        timeline = await ctx.fetch(self, "get_payment_timeline", order_id=order_id)
        data = timeline.data if timeline is not None and isinstance(timeline.data, dict) else None
        if data is None:
            # Base payment rows carry no timestamps, so they cannot be scoped to the episode.
            fallback = await ctx.fetch(self, "get_order_payments", order_id=order_id)
            if fallback is not None:
                refs["payment"] = fallback.ref
            payload = _payload("insufficient_evidence", None, None, refs)
            return self.reply(message, "insufficient_evidence", payload, refs.values())
        refs["payment"] = timeline.ref

        # Duplicated episode rows repeat identical events; they are one capture, not two.
        events = in_window(unique_rows(_dicts(data.get("events"))), window, "event_at")
        captures = [
            money(event.get("amount_brl"))
            for event in events
            if event.get("event_type") == "captured" and event.get("status") == "confirmed"
        ]
        captures = [amount for amount in captures if amount is not None]
        conflicts = []
        scoped = _order_payment_captures(
            captures, _payment_sets(_dicts(data.get("payments"))), order_value
        )
        foreign = set(captures) - set(scoped)
        if scoped != captures:
            conflicts.append(
                {
                    "field": "captured_payments",
                    "sources": ["get_payment_timeline.events", "get_payment_timeline.payments"],
                    "selected_source": "get_payment_timeline.payments",
                    "resolution_code": "ORDER_VALUE_PAYMENT_SET",
                }
            )
            captures = scoped
        mismatch = total(
            money(event.get("amount_brl"))
            for event in events
            if event.get("event_type") == "reconciliation_mismatch"
            and event.get("status") in MISMATCH_OPEN
        )

        # A duplicated order record is cross-checked against the independent base payment rows.
        duplicated = bool(message.payload.get("duplicated_records"))
        if duplicated:
            rows = await ctx.fetch(self, "get_order_payments", order_id=order_id)
            if rows is not None:
                refs["payment_rows"] = rows.ref

        # Refund lifecycle is only fetched when the complaint is about a refund (or the record is
        # duplicated); for other orders the tool has no data or only unrelated refunds.
        refund = None
        if duplicated or claimed_topic(ctx.case) in REFUND_TOPICS:
            refund = await ctx.fetch(self, "get_refund_timeline", order_id=order_id)
        refund_events: list[dict[str, Any]] = []
        if refund is not None and isinstance(refund.data, dict):
            refund_events = [
                event
                for event in in_window(_dicts(refund.data.get("events")), window, "event_at")
                # a refund of a foreign capture belongs to that other scenario, not this case
                if money(event.get("amount_brl")) not in foreign
            ]
            if refund_events:  # refunds of other episodes do not support this case
                refs["refund"] = refund.ref
        refunded = total(
            money(event.get("amount_brl"))
            for event in refund_events
            if event.get("status") in REFUND_DONE
        )
        refund_statuses = {str(event.get("status")) for event in refund_events}

        captured = total(captures)
        repeated = [amount for amount, count in Counter(captures).items() if count > 1]
        split = len(captures) > 1 and order_value is not None and captured == order_value
        duplicate = bool(repeated) and not split

        if "failed" in refund_statuses and refunded == 0:
            verdict = "refund_failed"
        elif refund_statuses & REFUND_OPEN and refunded == 0:
            verdict = "refund_pending"
        elif mismatch > 0:
            verdict = "capture_mismatch"
        elif duplicate:
            verdict = "duplicate_capture"
        elif refunded > 0:
            verdict = "refunded"
        elif captures:
            verdict = "reconciled"
        else:
            verdict = "insufficient_evidence"

        payload = _payload(verdict, captured, refunded, refs)
        payload.update(
            {
                "capture_count": len(captures),
                "split_payment": split,
                "duplicate_amount_brl": brl(max(repeated)) if duplicate else 0.0,
                "mismatch_amount_brl": brl(mismatch),
                "refund_statuses": sorted(refund_statuses),
                "refund_requested_brl": brl(
                    total(money(event.get("amount_brl")) for event in refund_events)
                ),
                "conflicts": conflicts,
            }
        )
        return self.reply(message, "findings", payload, refs.values())


def _payment_sets(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split payment rows into payment sets; a set restarts at payment_sequential 1."""
    sets: list[list[dict[str, Any]]] = []
    for row in rows:
        if not sets or str(row.get("payment_sequential")) == "1":
            sets.append([])
        sets[-1].append(row)
    return sets


def _order_payment_captures(
    captures: list[Decimal], sets: list[list[dict[str, Any]]], order_value: Decimal | None
) -> list[Decimal]:
    """Drop captures of foreign amounts when one payment set settles exactly the order value.

    A capture repeating an amount of that set is kept: it may be a genuine duplicate charge.
    """
    if order_value is None or len(captures) < 2:
        return captures
    available = Counter(captures)
    for payment_set in sets:
        amounts = [money(row.get("payment_value")) for row in payment_set]
        if None in amounts or total(amounts) != order_value:
            continue
        needed = Counter(amounts)
        if any(available[amount] < count for amount, count in needed.items()):
            continue
        extras = available - needed
        if extras and not set(extras) & set(needed):
            return list(needed.elements())
        return captures
    return captures


def _payload(
    verdict: str, captured: Decimal | None, refunded: Decimal | None, refs: dict[str, str]
) -> dict[str, Any]:
    refundable = None
    if captured is not None and refunded is not None:
        refundable = max(captured - refunded, Decimal("0"))
    return {
        "payment_analysis": {
            "verdict": verdict,
            "captured_total_brl": brl(captured) if captured is not None else None,
            "refunded_total_brl": brl(refunded) if refunded is not None else None,
            "refundable_total_brl": brl(refundable) if refundable is not None else None,
        },
        "evidence": dict(refs),
    }


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []
