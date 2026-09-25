"""Case-scoped timeline helpers shared by the specialist agents.

The same order ID can carry several order episodes (rows with different purchase dates).
The case episode is opened on or before the complaint: the latest one whose row matches the
claimed topic, otherwise the latest one. Events, item rows and payments are attributed to an
episode by the half-open window [episode purchase, next episode purchase).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

ORDER_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
CENT = Decimal("0.01")


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value)).quantize(CENT)
    except (InvalidOperation, ValueError):
        return None


def brl(value: Decimal) -> float:
    return float(value.quantize(CENT))


def total(values: Iterable[Decimal | None]) -> Decimal:
    return sum((value for value in values if value is not None), Decimal("0.00"))


@dataclass(frozen=True)
class Window:
    """Half-open period [start, end) owned by one order episode."""

    start: datetime | None
    end: datetime | None

    def contains(self, moment: datetime | None) -> bool:
        if moment is None:
            return False
        if self.start is not None and moment < self.start:
            return False
        return self.end is None or moment < self.end

    def to_payload(self) -> dict[str, str | None]:
        return {
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
        }

    @classmethod
    def from_payload(cls, value: dict[str, Any] | None) -> Window:
        value = value or {}
        return cls(parse_time(value.get("start")), parse_time(value.get("end")))


def unique_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop exact duplicate rows (duplicated episodes repeat them verbatim), keeping order."""
    return list({repr(sorted(row.items())): row for row in rows}.values())


def claimed_topic(case: dict[str, Any]) -> str | None:
    """The customer's specific complaint topic (every case also asks for a full refund)."""
    for claim in (case.get("customer_request") or {}).get("claims", []):
        if claim.get("topic") != "requested_full_refund":
            return claim.get("topic")
    return None


def delivered_late(row: dict[str, Any]) -> bool:
    delivered = parse_time(row.get("order_delivered_customer_date"))
    estimated = parse_time(row.get("order_estimated_delivery_date"))
    return delivered is not None and estimated is not None and delivered > estimated


def _delivered_on_time(row: dict[str, Any]) -> bool:
    delivered = parse_time(row.get("order_delivered_customer_date"))
    return delivered is not None and not delivered_late(row)


# Claims whose episode is recognisable from the order row alone.
ROW_MATCHES_CLAIM = {
    "canceled_order_paid": lambda row: row.get("order_status") == "canceled",
    "unavailable_order_paid": lambda row: row.get("order_status") == "unavailable",
    "late_delivery_seller": delivered_late,
    "late_delivery_logistics": delivered_late,
    "unsupported_claim": _delivered_on_time,
}


def select_episode(
    rows: list[dict[str, Any]], opened_at: datetime | None, claim: str | None = None
) -> tuple[dict[str, Any], Window, list[dict[str, Any]]]:
    """Pick the episode the complaint is about; returns (episode, window, other episodes)."""
    unique = unique_rows(rows)

    def purchase_key(row: dict[str, Any]) -> float:
        start = parse_time(row.get("order_purchase_timestamp"))
        return start.timestamp() if start else float("-inf")

    dated = sorted(unique, key=purchase_key)
    starts = [parse_time(row.get("order_purchase_timestamp")) for row in dated]
    eligible = [
        index
        for index, start in enumerate(starts)
        if start is not None and (opened_at is None or start <= opened_at)
    ]
    matches = ROW_MATCHES_CLAIM.get(claim or "")
    preferred = [index for index in eligible if matches and matches(dated[index])]
    index = (preferred or eligible or [0])[-1]
    end = next((start for start in starts[index + 1 :] if start is not None), None)
    others = [row for position, row in enumerate(dated) if position != index]
    return dated[index], Window(starts[index], end), others


def in_window(
    rows: Iterable[dict[str, Any]], window: Window, time_field: str
) -> list[dict[str, Any]]:
    return [row for row in rows if window.contains(parse_time(row.get(time_field)))]
