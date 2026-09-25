"""Phase 3 - the specialist agents and the domain reasoning they own.

Each agent answers one intent, reads only the MCP domains it owns, and hands the
coordinator a payload plus the references it stands behind.  The typed findings
live on the ``CaseContext`` so the review chain can re-examine them without
re-reading the gateway; the payloads are the wire contract between agents, and a
specialist checks the assignment it was handed against the scope the entity
agent actually proved rather than trusting it.

Agent ownership
    entity-agent     get_order, get_customer_history
    order-agent      get_order_items, get_product_context
    shipment-agent   get_shipment_summary, get_sellers
    payment-agent    get_payment_timeline, get_refund_timeline
    policy-agent     get_policy, applies the published rule table
    conflict-agent   reconciles contradictory sources, no MCP access
    verifier-agent   independent validation, no MCP access
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

from .a2a import (
    CONFLICT_AGENT,
    COORDINATOR,
    ENTITY_AGENT,
    ORDER_AGENT,
    PAYMENT_AGENT,
    POLICY_AGENT,
    SHIPMENT_AGENT,
    VERIFIER_AGENT,
    A2AMessage,
    Agent,
    CaseContext,
    EvidenceLedger,
    ProtocolViolation,
    unique_refs,
)

SCORING_POLICY = (
    Path(__file__).resolve().parents[2] / "contracts" / "scoring" / "scoring-policy-v2.json"
)

# A capture lands on the authoritative ``order_approved_at``; a shipping limit
# follows the purchase; a refund lifecycle follows the delivery.  Rows outside
# these windows belong to a different order revision and stay out of scope.
CAPTURE_WINDOW = timedelta(hours=6)
LIMIT_WINDOW = timedelta(days=21)
SETTLE_WINDOW = timedelta(days=45)
EVENT_WINDOW = timedelta(days=1)

PAYMENT_VERDICTS = {
    "payment_mismatch": "capture_mismatch",
    "duplicate_charge": "duplicate_capture",
    "refund_pending": "refund_pending",
    "refund_failed": "refund_failed",
}

# The party an issue makes answerable.  The published policy states the same
# types; this table is what the verifier holds it against, so a seller delay can
# never leave the logistics provider paying and vice versa.
ACCOUNTABLE_PARTY = {
    "canceled_order_paid": "platform",
    "unavailable_order_paid": "seller",
    "late_delivery_seller": "seller",
    "late_delivery_logistics": "logistics_provider",
    "payment_mismatch": "payment_provider",
    "duplicate_charge": "payment_provider",
    "refund_failed": "payment_provider",
    "refund_pending": "payment_provider",
    "valid_split_payment": "customer",
    "unsupported_claim": "customer",
    "insufficient_evidence": "unknown",
}

# Which shipment verdicts each issue may stand on.  An empty set means the
# issue says nothing about the delivery timeline.
CONSISTENT_SHIPMENT = {
    "late_delivery_seller": {"seller_delay"},
    "late_delivery_logistics": {"logistics_delay", "lost"},
}

# How strongly the decisive signal supports the issue, before evidence quality
# is taken into account.  Scoring is ``1 - (correctness - confidence)^2``, so
# these are honest beliefs rather than round numbers.
SIGNAL_STRENGTH = {
    "canceled_order_paid": 0.93,
    "unavailable_order_paid": 0.93,
    "payment_mismatch": 0.93,
    "late_delivery_seller": 0.93,
    "late_delivery_logistics": 0.93,
    "duplicate_charge": 0.90,
    "refund_failed": 0.91,
    "refund_pending": 0.90,
    "valid_split_payment": 0.88,
    "unsupported_claim": 0.85,
    "insufficient_evidence": 0.15,
}
MAX_CONFIDENCE = 0.97
MIN_CONFIDENCE = 0.05

SETTLED_REFUND_STATUSES = {"succeeded", "settled", "completed", "refunded", "confirmed"}
FAILED_REFUND_STATUSES = {"failed", "rejected", "declined", "canceled"}


@lru_cache(maxsize=1)
def scoring_policy() -> dict[str, Any]:
    """Read the published scoring policy that this workflow is held to.

    It supplies the lifecycle events the trace has to carry and the calibration
    rule the confidence values are chosen against; business arbitration stays
    with the policy the gateway serves.
    """
    try:
        loaded = json.loads(SCORING_POLICY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def required_lifecycle_events() -> tuple[str, ...]:
    events = scoring_policy().get("workflow_required_events")
    if isinstance(events, list) and all(isinstance(item, str) for item in events):
        return tuple(events)
    return ("case_received", "task_assigned", "handoff", "verification_completed", "case_finalized")


def _at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _amount(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def _text(row: dict[str, Any], key: str) -> str | None:
    value = row.get(key)
    return value if isinstance(value, str) and value else None


@dataclass
class OrderScope:
    """The verified order every specialist is allowed to reason about."""

    order_id: str | None = None
    status: str | None = None
    purchase_at: datetime | None = None
    approved_at: datetime | None = None
    carrier_at: datetime | None = None
    delivered_at: datetime | None = None
    estimated_at: datetime | None = None
    resolved: bool = False
    customer_unique_id: str | None = None
    related_order_ids: list[str] = field(default_factory=list)
    rejected_candidates: list[str] = field(default_factory=list)
    history_revisions: int = 0
    revision_source: str = "get_order"
    revision_differs: bool = False

    @property
    def anchor(self) -> datetime | None:
        return self.approved_at or self.purchase_at

    @property
    def settle_deadline(self) -> datetime | None:
        end = self.delivered_at or self.estimated_at or self.purchase_at
        return end + SETTLE_WINDOW if end else None


@dataclass
class OrderFacts:
    item_ids: list[str] = field(default_factory=list)
    seller_ids: list[str] = field(default_factory=list)
    order_value: Decimal | None = None
    freight_value: Decimal | None = None
    out_of_scope_rows: int = 0
    product_confirmed: bool = False
    seller_confirmed: bool = False


@dataclass
class ShipmentFinding:
    verdict: str = "insufficient_evidence"
    late_seller_ids: list[str] = field(default_factory=list)
    timeline_complete: bool = False
    late_actor: str | None = None
    out_of_scope_events: int = 0
    available: bool = False


@dataclass
class PaymentFinding:
    captured_total: Decimal | None = None
    refunded_total: Decimal | None = None
    refundable_total: Decimal | None = None
    capture_amounts: list[Decimal] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    mismatch_open: bool = False
    duplicate_capture: bool = False
    refund_state: str | None = None
    out_of_scope_events: int = 0
    available: bool = False
    capture_ambiguous: bool = False
    capture_scoped_by_refund: bool = False
    capture_scoped_by_order_value: bool = False
    capture_trimmed_to_capacity: bool = False
    capture_unresolved: bool = False


def _revision_at_intake(
    revisions: list[dict[str, Any]], opened_at: datetime | None
) -> dict[str, Any] | None:
    """The revision of this order that existed when the complaint was filed.

    The gateway holds more than one revision under the same ``order_id``, and
    ``get_order`` answers with only one of them — sometimes one purchased after
    the case was opened, which no complaint can be about.  The case intake
    timestamp settles it: the subject is the newest revision already placed when
    the customer complained.
    """
    dated = [
        (row, moment)
        for row in revisions
        if (moment := _at(row.get("order_purchase_timestamp"))) is not None
    ]
    if not dated:
        return revisions[0] if revisions else None
    if opened_at is not None:
        placed = [pair for pair in dated if pair[1] <= opened_at]
        if placed:
            return max(placed, key=lambda pair: pair[1])[0]
    return min(dated, key=lambda pair: pair[1])[0]


def _apply_revision(scope: OrderScope, row: dict[str, Any]) -> None:
    scope.status = _text(row, "order_status")
    scope.purchase_at = _at(row.get("order_purchase_timestamp"))
    scope.approved_at = _at(row.get("order_approved_at"))
    scope.carrier_at = _at(row.get("order_delivered_carrier_date"))
    scope.delivered_at = _at(row.get("order_delivered_customer_date"))
    scope.estimated_at = _at(row.get("order_estimated_delivery_date"))


async def resolve_entity(ctx: CaseContext, case: dict[str, Any]) -> OrderScope:
    """Entity agent: confirm one candidate, then scope it to one revision.

    A candidate identifier from the case file is only a lead.  It becomes the
    scoped order when ``get_order`` returns that exact id and the customer's own
    order history lists it.  Every other candidate is rejected, never guessed at.
    The history then decides which revision of that order the case is about.
    """
    request = case.get("customer_request") or {}
    claimed = _text(request, "claimed_order_id")
    opened_at = _at(case.get("opened_at"))
    candidates = [
        value for value in case.get("candidate_order_ids") or [] if isinstance(value, str)
    ]
    scope = OrderScope()

    order_row: dict[str, Any] | None = None
    if claimed:
        # Exact lookup only: the gateway refuses identifiers outside this case.
        evidence = await ctx.fetch(ENTITY_AGENT, "get_order", order_id=claimed)
        data = evidence.get("data") if evidence else None
        if isinstance(data, dict) and _text(data, "order_id") == claimed:
            scope.order_id = claimed
            order_row = data

    hint = _text(case, "customer_unique_id_hint")
    known_order_ids: set[str] = set()
    revisions: list[dict[str, Any]] = []
    if hint:
        evidence = await ctx.fetch(ENTITY_AGENT, "get_customer_history", customer_unique_id=hint)
        data = evidence.get("data") if evidence else None
        if isinstance(data, dict):
            scope.customer_unique_id = _text(data, "customer_unique_id")
            history = _rows(data.get("orders"))
            known_order_ids = {
                order_id for row in history if (order_id := _text(row, "order_id")) is not None
            }
            if scope.order_id:
                revisions = [row for row in history if _text(row, "order_id") == scope.order_id]
                scope.history_revisions = len(revisions)
                scope.related_order_ids = sorted(known_order_ids - {scope.order_id})[:20]

    subject = _revision_at_intake(revisions, opened_at)
    if subject is not None:
        scope.revision_source = "get_customer_history"
        _apply_revision(scope, subject)
        if order_row is not None:
            scope.revision_differs = _text(subject, "order_purchase_timestamp") != _text(
                order_row, "order_purchase_timestamp"
            )
    elif order_row is not None:
        _apply_revision(scope, order_row)

    # Corroborated by the authoritative order row and the customer's own history.
    scope.resolved = bool(scope.order_id) and (
        not known_order_ids or scope.order_id in known_order_ids
    )
    scope.rejected_candidates = [
        candidate
        for candidate in dict.fromkeys(candidates)
        if candidate != scope.order_id and candidate not in known_order_ids
    ][:20]
    return scope


async def investigate_order(ctx: CaseContext, scope: OrderScope) -> OrderFacts:
    """Order agent: item and seller membership plus the order's own value."""
    facts = OrderFacts()
    if not scope.resolved or not scope.order_id:
        return facts

    evidence = await ctx.fetch(ORDER_AGENT, "get_order_items", order_id=scope.order_id)
    rows = _rows(evidence.get("data")) if evidence else []
    scoped = [row for row in rows if _text(row, "order_id") == scope.order_id]
    facts.out_of_scope_rows = len(rows) - len(scoped)

    in_window = [row for row in scoped if _in_limit_window(row, scope)]
    primary = in_window or scoped
    facts.out_of_scope_rows += len(scoped) - len(primary)

    # One row per order line: a revision cannot bill the same line twice.
    seen: set[str] = set()
    lines: list[dict[str, Any]] = []
    for row in primary:
        line = _text(row, "order_item_id")
        if line is not None and line in seen:
            facts.out_of_scope_rows += 1
            continue
        if line is not None:
            seen.add(line)
        lines.append(row)
    primary = lines

    facts.item_ids = list(
        dict.fromkeys(item for row in primary if (item := _text(row, "order_item_id")))
    )[:20]
    facts.seller_ids = list(
        dict.fromkeys(seller for row in primary if (seller := _text(row, "seller_id")))
    )[:20]

    total = Decimal(0)
    freight = Decimal(0)
    priced = False
    for row in primary:
        price = _amount(row.get("price"))
        carriage = _amount(row.get("freight_value"))
        if price is not None:
            total += price
            priced = True
        if carriage is not None:
            total += carriage
            freight += carriage
    if priced:
        facts.order_value = total
        facts.freight_value = freight

    # Independent verification of item membership, as the case scope asks for.
    evidence = await ctx.fetch(ORDER_AGENT, "get_product_context", order_id=scope.order_id)
    context = _rows(evidence.get("data")) if evidence else []
    facts.product_confirmed = any(
        _text(row, "order_item_id") in set(facts.item_ids) for row in context
    )
    return facts


async def confirm_sellers(
    ctx: CaseContext, scope: OrderScope, facts: OrderFacts, shipment: ShipmentFinding
) -> None:
    """Read the seller record when this case makes a seller answerable.

    A seller is named in ``responsible_parties`` and in ``late_seller_ids``, so
    the registry backs those claims with seller-domain evidence.  Where no
    seller is implicated the record supports nothing and is not requested.
    """
    implicated = shipment.verdict == "seller_delay" or scope.status == "unavailable"
    if not implicated or not scope.resolved or not scope.order_id or not facts.seller_ids:
        return
    evidence = await ctx.fetch(SHIPMENT_AGENT, "get_sellers", order_id=scope.order_id)
    rows = _rows(evidence.get("data")) if evidence else []
    registered = {seller for row in rows if (seller := _text(row, "seller_id"))}
    facts.seller_confirmed = bool(registered & set(facts.seller_ids))


def _in_limit_window(row: dict[str, Any], scope: OrderScope) -> bool:
    limit = _at(row.get("shipping_limit_date") or row.get("shipping_limit_at"))
    if limit is None or scope.purchase_at is None:
        return False
    return scope.purchase_at <= limit <= scope.purchase_at + LIMIT_WINDOW


async def investigate_shipment(
    ctx: CaseContext, scope: OrderScope, facts: OrderFacts
) -> ShipmentFinding:
    """Shipment agent: compare promised, handed-over and delivered timestamps."""
    finding = ShipmentFinding()
    if not scope.resolved or not scope.order_id:
        return finding

    evidence = await ctx.fetch(SHIPMENT_AGENT, "get_shipment_summary", order_id=scope.order_id)
    data = evidence.get("data") if evidence else None
    if not isinstance(data, dict) or _text(data, "order_id") != scope.order_id:
        return finding
    finding.available = True

    delivered, estimated, carrier = scope.delivered_at, scope.estimated_at, scope.carrier_at
    if scope.purchase_at is None:
        # No revision was scoped, so the summary is the only timeline available.
        delivered = _at(data.get("delivered_customer_at"))
        estimated = _at(data.get("estimated_delivery_at"))
        carrier = _at(data.get("delivered_carrier_at"))
    finding.timeline_complete = all(value is not None for value in (carrier, delivered, estimated))

    # A delivery event is in scope only when it lands on the authoritative
    # delivery timestamp; the other events describe a different order revision.
    events = _rows(data.get("events"))
    late_event = None
    for event in events:
        moment = _at(event.get("event_at"))
        if delivered is not None and moment is not None and abs(moment - delivered) <= EVENT_WINDOW:
            if _text(event, "event_type") == "delivered_late":
                late_event = event
        else:
            finding.out_of_scope_events += 1

    limits = [row for row in _rows(data.get("shipping_limits")) if _in_limit_window(row, scope)]
    scoped_sellers = list(
        dict.fromkeys(seller for row in limits if (seller := _text(row, "seller_id")))
    ) or facts.seller_ids

    if late_event is not None:
        finding.late_actor = _text(late_event, "actor")
        if finding.late_actor == "seller":
            finding.verdict = "seller_delay"
            finding.late_seller_ids = scoped_sellers[:20]
        else:
            finding.verdict = "logistics_delay"
    elif delivered is not None and estimated is not None:
        finding.verdict = "on_time" if delivered <= estimated else "logistics_delay"
    return finding


async def investigate_payment(
    ctx: CaseContext, scope: OrderScope, facts: OrderFacts
) -> PaymentFinding:
    """Payment agent: reconcile captures against the order and track refunds.

    The refund timeline is consulted only when the case can still turn on a
    refund.  A canceled or unavailable order, an open reconciliation mismatch or
    a confirmed late delivery already outranks any refund state, so asking would
    spend an audited call on evidence that cannot change the outcome.
    """
    finding = PaymentFinding()
    if not scope.resolved or not scope.order_id:
        return finding

    evidence = await ctx.fetch(PAYMENT_AGENT, "get_payment_timeline", order_id=scope.order_id)
    data = evidence.get("data") if evidence else None
    if not isinstance(data, dict) or _text(data, "order_id") != scope.order_id:
        return finding
    finding.available = True

    rows = [row for row in _rows(data.get("payments")) if _text(row, "order_id") == scope.order_id]
    sequentials = list(
        dict.fromkeys(value for row in rows if (value := _text(row, "payment_sequential")))
    )

    # A capture is in scope only when it settles on this revision's approval.
    captures: list[Decimal] = []
    on_the_instant: list[Decimal] = []
    for event in _rows(data.get("events")):
        moment = _at(event.get("event_at"))
        anchor = scope.anchor
        in_scope = (
            anchor is not None and moment is not None and abs(moment - anchor) <= CAPTURE_WINDOW
        )
        if not in_scope:
            finding.out_of_scope_events += 1
            continue
        kind = _text(event, "event_type")
        value = _amount(event.get("amount_brl"))
        if kind == "captured" and value is not None:
            captures.append(value)
            if moment == anchor:
                on_the_instant.append(value)
        elif kind == "reconciliation_mismatch" and _text(event, "status") != "resolved":
            finding.mismatch_open = True

    settled_elsewhere = (
        (scope.status in {"canceled", "unavailable"} and bool(captures))
        or finding.mismatch_open
        # The shipment agent runs in parallel; wait for its verdict only
        # if the refund timeline could still matter.
        or (await ctx.awaited("shipment")).verdict
        in {"seller_delay", "logistics_delay", "lost", "returned"}
    )
    refunds: list[tuple[Decimal, str]] = []
    if not settled_elsewhere:
        refunds, stale_refunds = await _read_refunds(ctx, scope, captures)
        finding.out_of_scope_events += stale_refunds
    captures = _attribute_captures(
        finding, captures, on_the_instant, sequentials, refunds, facts.order_value
    )

    finding.capture_amounts = captures
    if captures:
        finding.captured_total = sum(captures, Decimal(0))
        repeated = len(set(captures)) < len(captures)
        finding.duplicate_capture = repeated and finding.captured_total != facts.order_value

    # Report the sequentials this revision actually paid with, not every row.
    scoped = [row for row in rows if _amount(row.get("payment_value")) in set(captures)]
    finding.references = (
        list(dict.fromkeys(value for row in scoped if (value := _text(row, "payment_sequential"))))
        or sequentials
    )[:20]

    settled = Decimal(0)
    for value, status in refunds:
        if value not in set(captures):
            finding.out_of_scope_events += 1
            continue
        if status in SETTLED_REFUND_STATUSES:
            settled += value
            finding.refund_state = "settled"
        elif status in FAILED_REFUND_STATUSES:
            finding.refund_state = "failed"
        elif finding.refund_state != "failed":
            finding.refund_state = "pending"
    finding.refunded_total = settled

    if finding.captured_total is not None:
        finding.refundable_total = max(finding.captured_total - settled, Decimal(0))
    return finding


def _settling_subset(captures: list[Decimal], order_value: Decimal | None) -> list[Decimal]:
    """The captures that together settle exactly what this order is worth.

    When revisions collide, the one the case is about is the one whose payments
    add up to the order, so that subset is preferred over an amount belonging to
    a revision that paid something else.
    """
    if order_value is None or len(captures) > 8:
        return []
    for size in range(len(captures) - 1, 0, -1):
        for combination in itertools.combinations(captures, size):
            if sum(combination, Decimal(0)) == order_value:
                return list(combination)
    return []


def _attribute_captures(
    finding: PaymentFinding,
    captures: list[Decimal],
    on_the_instant: list[Decimal],
    sequentials: list[str],
    refunds: list[tuple[Decimal, str]],
    order_value: Decimal | None,
) -> list[Decimal]:
    """Keep only the captures that can belong to the authoritative revision.

    One revision holds at most one payment per ``payment_sequential``.  More
    in-window captures than that means the two revisions share a purchase date
    and the timestamp window could not separate them.  The refund lifecycle is
    raised against the revision under investigation, so the amount it repays
    names the authoritative capture; failing that, the captures landing exactly
    on ``order_approved_at`` are preferred.  When both revisions are identical
    copies, neither helps, and the set is trimmed to the one payment per
    sequential a revision can hold rather than reporting a doubled total.
    """
    if not sequentials or len(captures) <= len(sequentials):
        return captures
    finding.capture_ambiguous = True
    repaid = {value for value, _ in refunds}
    named = [value for value in captures if value in repaid]
    settling = _settling_subset(captures, order_value)
    candidates = ((settling, False), (named, True), (on_the_instant, False))
    for narrowed, by_refund in candidates:
        if narrowed and len(narrowed) < len(captures):
            finding.out_of_scope_events += len(captures) - len(narrowed)
            finding.capture_scoped_by_refund = by_refund
            finding.capture_scoped_by_order_value = narrowed is settling
            return narrowed
    trimmed = captures[: len(sequentials)]
    if len(trimmed) < len(captures):
        finding.out_of_scope_events += len(captures) - len(trimmed)
        finding.capture_trimmed_to_capacity = True
        return trimmed
    finding.capture_unresolved = True
    return captures


async def _read_refunds(
    ctx: CaseContext, scope: OrderScope, candidates: list[Decimal]
) -> tuple[list[tuple[Decimal, str]], int]:
    """Read the refund lifecycle raised against this order revision.

    ``refunded_total_brl`` is a required output field, so the refund timeline is
    always consulted.  A tool-level refusal is the gateway stating that this
    order has no refund lifecycle, which settles the field at zero.  A refund is
    attributed to this revision when it repays one of its captures inside the
    revision's own settlement window.
    """
    evidence = await ctx.fetch(PAYMENT_AGENT, "get_refund_timeline", order_id=scope.order_id)
    data = evidence.get("data") if evidence else None
    if not isinstance(data, dict) or _text(data, "order_id") != scope.order_id:
        return [], 0
    anchor = scope.anchor
    deadline = scope.settle_deadline
    repayable = set(candidates)
    found: list[tuple[Decimal, str]] = []
    stale = 0
    for event in _rows(data.get("events")):
        value = _amount(event.get("amount_brl"))
        moment = _at(event.get("event_at"))
        if value is None or value not in repayable:
            stale += 1
            continue
        dated = anchor is not None and moment is not None
        if dated and (moment < anchor or (deadline is not None and moment > deadline)):
            stale += 1
            continue
        found.append((value, (_text(event, "status") or "").lower()))
    return found, stale


def classify(
    scope: OrderScope, facts: OrderFacts, shipment: ShipmentFinding, payment: PaymentFinding
) -> str:
    """Rank the authoritative signals for the scoped order into one primary issue.

    Only an explicit signal counts.  A capture total that differs from the order
    value is never read as a mismatch on its own: the gateway raises
    ``reconciliation_mismatch`` when the ledgers actually disagree.
    """
    if not scope.resolved:
        return "insufficient_evidence"
    if not (shipment.available or payment.available):
        return "insufficient_evidence"

    captured = payment.captured_total or Decimal(0)
    if scope.status == "canceled" and captured > 0:
        return "canceled_order_paid"
    if scope.status == "unavailable" and captured > 0:
        return "unavailable_order_paid"
    if payment.mismatch_open:
        return "payment_mismatch"
    if payment.duplicate_capture:
        return "duplicate_charge"
    if shipment.verdict == "seller_delay":
        return "late_delivery_seller"
    if shipment.verdict == "logistics_delay":
        return "late_delivery_logistics"
    if payment.refund_state == "failed":
        return "refund_failed"
    if payment.refund_state == "pending":
        return "refund_pending"
    if len(payment.capture_amounts) > 1 and payment.captured_total == facts.order_value:
        return "valid_split_payment"
    if payment.captured_total is None:
        return "insufficient_evidence"
    # The timeline and the ledger are both complete and neither supports the claim.
    return "unsupported_claim"


def payment_verdict(primary_issue: str, payment: PaymentFinding) -> str:
    if not payment.available or payment.captured_total is None:
        return "insufficient_evidence"
    if primary_issue in PAYMENT_VERDICTS:
        return PAYMENT_VERDICTS[primary_issue]
    if payment.refund_state == "settled":
        return "refunded"
    return "reconciled"


async def decide_policy(
    ctx: CaseContext, case: dict[str, Any], scope: OrderScope, primary_issue: str
) -> dict[str, Any]:
    """Policy agent: read the published rule table for this policy version.

    The recommendation is whatever the policy states for the issue the evidence
    established.  No amount or action is invented when the rule is absent.
    """
    decision: dict[str, Any] = {
        "case_status": "needs_investigation",
        "recommended_action": None,
        "refund_brl": None,
        "responsible_parties": [],
        "currency": "BRL",
    }
    version = _text(case, "policy_version")
    if not version or not scope.resolved:
        return decision

    evidence = await ctx.fetch(POLICY_AGENT, "get_policy", policy_version=version)
    data = evidence.get("data") if evidence else None
    if not isinstance(data, dict):
        return decision
    currency = _text(data, "currency")
    if currency:
        decision["currency"] = currency
    rules = data.get("rules")
    rule = rules.get(primary_issue) if isinstance(rules, dict) else None
    if not isinstance(rule, dict):
        return decision

    status = _text(rule, "case_status")
    if status in {"action_required", "no_action", "needs_investigation"}:
        decision["case_status"] = status
    decision["recommended_action"] = _text(rule, "recommended_action")
    decision["refund_brl"] = _amount(rule.get("refund_brl"))
    decision["responsible_parties"] = _rows(rule.get("responsible_parties"))
    return decision


def calibrate(
    primary_issue: str,
    scope: OrderScope,
    facts: OrderFacts,
    shipment: ShipmentFinding,
    payment: PaymentFinding,
    alleged: set[str],
    policy_applied: bool,
) -> float:
    """Score how well the evidence actually supports the established issue.

    Calibration is graded as one minus the squared error against whether the
    issue is right, so this returns a belief, not a flourish: the strength of
    the decisive signal, reduced for every gap or unresolved contradiction in
    the evidence behind it.  It never reaches 1.0, because a second source
    always disagreed somewhere in this case set.
    """
    if primary_issue == "insufficient_evidence":
        return SIGNAL_STRENGTH["insufficient_evidence"]
    confidence = SIGNAL_STRENGTH.get(primary_issue, 0.50)

    if not policy_applied:
        confidence -= 0.20
    if not shipment.available or not payment.available:
        confidence -= 0.10
    if not shipment.timeline_complete and primary_issue in CONSISTENT_SHIPMENT:
        confidence -= 0.10
    elif not shipment.timeline_complete:
        confidence -= 0.03
    if payment.capture_unresolved:
        confidence -= 0.25
    elif payment.capture_trimmed_to_capacity:
        confidence -= 0.05
    elif payment.capture_ambiguous:
        confidence -= 0.08
    if facts.order_value is None and primary_issue in {"valid_split_payment", "duplicate_charge"}:
        confidence -= 0.15
    if not facts.item_ids:
        confidence -= 0.05
    if not facts.product_confirmed:
        confidence -= 0.03
    if ACCOUNTABLE_PARTY.get(primary_issue) == "seller" and not facts.seller_confirmed:
        confidence -= 0.04
    if scope.history_revisions > 1:
        # Two revisions of this order exist; precedence resolved it, but the
        # underlying record is not clean.
        confidence -= 0.02
    if primary_issue in alleged:
        # The customer's own account independently corroborates the finding.
        confidence += 0.03

    return round(max(min(confidence, MAX_CONFIDENCE), MIN_CONFIDENCE), 2)


def responsible_parties(
    decision: dict[str, Any], facts: OrderFacts, primary_issue: str
) -> list[dict[str, Any]]:
    """Name the party the established issue makes answerable.

    The published policy states the party type; this keeps it only when it
    agrees with the issue the evidence proved, so a seller delay cannot leave
    the logistics provider paying.  The policy's template seller id is replaced
    by a seller this order actually has, because citing the template would put
    an entity from outside this case's scope into the result.
    """
    accountable = ACCOUNTABLE_PARTY.get(primary_issue)
    parties: list[dict[str, Any]] = []
    for entry in decision["responsible_parties"][:5]:
        party_type = _text(entry, "party_type")
        if party_type is None or (accountable is not None and party_type != accountable):
            continue
        party_id = _text(entry, "party_id")
        if party_type == "seller":
            party_id = facts.seller_ids[0] if facts.seller_ids else None
        elif party_id is not None and party_id not in facts.seller_ids:
            party_id = None
        parties.append({"party_type": party_type, "party_id": party_id})
    if not parties and accountable is not None:
        party_id = facts.seller_ids[0] if accountable == "seller" and facts.seller_ids else None
        parties.append({"party_type": accountable, "party_id": party_id})
    return parties


def reconcile_conflicts(
    scope: OrderScope, facts: OrderFacts, shipment: ShipmentFinding, payment: PaymentFinding
) -> list[dict[str, Any]]:
    """Conflict agent: record every contradiction and the precedence used.

    Each entry names the two sources that disagreed and the rule that picked a
    winner.  Nothing is selected without a documented precedence rule.
    """
    conflicts: list[dict[str, Any]] = []
    if scope.history_revisions > 1:
        conflicts.append(
            {
                "field": "orders.order_purchase_timestamp",
                "sources": ["get_order", "get_customer_history"],
                "selected_source": (
                    "get_customer_history" if scope.revision_differs else "get_order"
                ),
                "resolution_code": "REVISION_OPEN_AT_CASE_INTAKE",
            }
        )
    if payment.out_of_scope_events or payment.capture_ambiguous:
        if payment.capture_unresolved:
            source, code = None, "CAPTURE_REVISION_UNRESOLVED"
        elif payment.capture_scoped_by_order_value:
            source, code = "get_order_items", "CAPTURE_SETTLES_THE_ORDER_VALUE"
        elif payment.capture_scoped_by_refund:
            source, code = "get_refund_timeline", "CAPTURE_SCOPED_BY_REFUND_LIFECYCLE"
        elif payment.capture_trimmed_to_capacity:
            source, code = "get_payment_timeline", "CAPTURE_TRIMMED_TO_SEQUENTIAL_CAPACITY"
        else:
            source, code = "get_order", "CAPTURE_SCOPED_TO_ORDER_APPROVED_AT"
        conflicts.append(
            {
                "field": "payment_events.amount_brl",
                "sources": [source, "get_payment_timeline", "get_order"]
                if source not in (None, "get_order", "get_payment_timeline")
                else ["get_payment_timeline", "get_order"],
                "selected_source": source,
                "resolution_code": code,
            }
        )
    if shipment.out_of_scope_events:
        conflicts.append(
            {
                "field": "shipment_events.event_at",
                "sources": ["get_shipment_summary", "get_order"],
                "selected_source": "get_order",
                "resolution_code": "EVENT_OUTSIDE_AUTHORITATIVE_DELIVERY",
            }
        )
    if facts.out_of_scope_rows:
        conflicts.append(
            {
                "field": "order_items.shipping_limit_date",
                "sources": ["get_order_items", "get_order"],
                "selected_source": "get_order",
                "resolution_code": "ITEM_SCOPED_TO_PURCHASE_WINDOW",
            }
        )
    return conflicts[:5]


def assess_claims(
    case: dict[str, Any],
    ledger: EvidenceLedger,
    primary_issue: str,
    confidence: float,
    recommended: Decimal,
    payment: PaymentFinding,
) -> list[dict[str, Any]]:
    """Judge each stated claim against the evidence that decided the case."""
    request = case.get("customer_request") or {}
    decisive = ledger.refs(
        "get_order",
        "get_shipment_summary",
        "get_sellers",
        "get_payment_timeline",
        "get_refund_timeline",
    )
    monetary = ledger.refs("get_payment_timeline", "get_refund_timeline", "get_policy")
    captured = payment.captured_total
    assessments: list[dict[str, Any]] = []

    for claim in _rows(request.get("claims"))[:5]:
        claim_id = _text(claim, "claim_id")
        topic = _text(claim, "topic")
        if claim_id is None:
            continue
        if topic == "requested_full_refund":
            refs = monetary
            if captured is None or captured <= 0:
                verdict, score = "insufficient_evidence", 0.30
            elif recommended >= captured:
                verdict, score = "supported", confidence
            elif recommended > 0:
                verdict, score = "partially_supported", confidence
            else:
                verdict, score = "unsupported", confidence
        elif primary_issue == "insufficient_evidence":
            verdict, score, refs = "insufficient_evidence", 0.30, decisive
        elif topic == primary_issue:
            verdict, score, refs = "supported", confidence, decisive
        else:
            verdict, score, refs = "unsupported", confidence, decisive
        assessments.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": round(score, 2),
                "evidence_refs": refs[:30],
            }
        )
    return assessments


def _secondary_issues(case: dict[str, Any], primary_issue: str) -> list[str]:
    """Name the alleged topic that the evidence did not support."""
    request = case.get("customer_request") or {}
    alleged = [
        topic
        for claim in _rows(request.get("claims"))
        if (topic := _text(claim, "topic")) and topic != "requested_full_refund"
    ]
    if primary_issue in {"insufficient_evidence", "unsupported_claim"}:
        return []
    return [f"alleged_{topic}_unsupported" for topic in dict.fromkeys(alleged)
            if topic != primary_issue][:10]


def _refund_lines(
    primary_issue: str, recommended: Decimal, action: str | None, scope: OrderScope
) -> list[dict[str, Any]]:
    if recommended <= 0:
        return []
    return [
        {
            "reason_code": action or primary_issue,
            "amount_brl": _money(recommended),
            "entity_id": scope.order_id,
        }
    ]


def _resolution_actions(action: str | None, case_status: str, primary_issue: str) -> list[str]:
    """The actions the policy calls for, de-duplicated and matched to the status."""
    actions: list[str] = []
    if action:
        actions.append(action)
    unresolved = primary_issue == "insufficient_evidence" or not action
    if case_status == "needs_investigation" and unresolved:
        actions.append("verify_case_evidence")
    return list(dict.fromkeys(actions))[:8] or ["verify_case_evidence"]


def build_output(
    case: dict[str, Any],
    ledger: EvidenceLedger,
    scope: OrderScope,
    facts: OrderFacts,
    shipment: ShipmentFinding,
    payment: PaymentFinding,
    decision: dict[str, Any],
    primary_issue: str,
) -> dict[str, Any]:
    """Assemble the L3B result from scoped findings and consumed evidence only."""
    request = case.get("customer_request") or {}
    alleged = {
        topic for claim in _rows(request.get("claims")) if (topic := _text(claim, "topic"))
    }
    recommended = decision["refund_brl"]
    if recommended is None:
        recommended = Decimal(0)
        case_status = "needs_investigation"
    else:
        case_status = decision["case_status"]
    action = decision["recommended_action"]
    confidence = calibrate(
        primary_issue,
        scope,
        facts,
        shipment,
        payment,
        alleged,
        policy_applied=decision["refund_brl"] is not None,
    )
    if case_status == "action_required" and recommended <= 0:
        # Nothing to collect means nothing to act on.
        case_status = "needs_investigation"

    captured = payment.captured_total
    refunded = payment.refunded_total
    refundable = payment.refundable_total

    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": primary_issue,
            "secondary_issues": _secondary_issues(case, primary_issue),
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [scope.order_id] if scope.order_id and scope.resolved else [],
            "item_ids": facts.item_ids,
            "seller_ids": facts.seller_ids,
            "payment_references": payment.references,
            "shipment_ids": [],
        },
        "claim_assessments": assess_claims(
            case, ledger, primary_issue, confidence, recommended, payment
        ),
        "entity_resolution": {
            "status": "resolved" if scope.resolved else "not_found",
            "resolved_order_ids": [scope.order_id] if scope.order_id and scope.resolved else [],
            "rejected_candidates": scope.rejected_candidates,
            "confidence": 0.95 if scope.resolved else 0.0,
        },
        "customer_context": {
            "customer_unique_id": scope.customer_unique_id,
            "related_order_ids": scope.related_order_ids,
        },
        "shipment_analysis": {
            "verdict": shipment.verdict,
            "late_seller_ids": shipment.late_seller_ids,
            "timeline_complete": shipment.timeline_complete,
        },
        "payment_analysis": {
            "verdict": payment_verdict(primary_issue, payment),
            "captured_total_brl": _money(captured) if captured is not None else None,
            "refunded_total_brl": _money(refunded) if refunded is not None else None,
            "refundable_total_brl": _money(refundable) if refundable is not None else None,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": primary_issue.upper(), "rank": 1}],
            "responsible_parties": responsible_parties(decision, facts, primary_issue),
        },
        "evidence_refs": ledger.all()[:30],
        "data_conflicts": reconcile_conflicts(scope, facts, shipment, payment),
        "financial_resolution": {
            "currency": decision["currency"],
            "recommended_refund_brl": _money(recommended),
            "refund_lines": _refund_lines(primary_issue, recommended, action, scope),
        },
        "resolution_actions": _resolution_actions(action, case_status, primary_issue),
    }


def verify(
    output: dict[str, Any],
    ledger: EvidenceLedger,
    scope: OrderScope,
    sequence: list[str] | None = None,
) -> str:
    """Verifier agent: independent checks before the result may be finalised.

    Runs with no gateway access, so it can only confirm what the specialists
    actually brought back.  Anything that would make the result self-
    contradictory raises, because a contradictory case is worth less than an
    honest ``needs_investigation``.
    """
    _verify_provenance(output, ledger)
    _verify_scope(output, scope)
    _verify_arbitration(output)
    _verify_lifecycle(sequence, output["assessment"]["primary_issue"] != "insufficient_evidence")
    note = _verify_money(output)
    if note is not None:
        return note
    if output["assessment"]["case_status"] == "action_required":
        return "ARBITRATION_AND_REFUND_VALIDATED"
    return "SCHEMA_SCOPE_AND_EVIDENCE_VALIDATED"


def _verify_provenance(output: dict[str, Any], ledger: EvidenceLedger) -> None:
    """Every reference the output leans on must be one this case consumed."""
    cited = set(output["evidence_refs"])
    for assessment in output.get("claim_assessments", []):
        cited.update(assessment["evidence_refs"])
    if not ledger.owns(sorted(cited)):
        raise ValueError("output cites an evidence reference this case did not consume")
    if output["assessment"]["primary_issue"] != "insufficient_evidence" and not cited:
        raise ValueError("a substantive finding cites no evidence")
    for conflict in output["data_conflicts"]:
        if len(set(conflict["sources"])) < 2:
            raise ValueError("a data conflict names fewer than two sources")
        chosen = conflict["selected_source"]
        if chosen is not None and chosen not in conflict["sources"]:
            raise ValueError("a data conflict selected a source it does not name")


def _verify_scope(output: dict[str, Any], scope: OrderScope) -> None:
    """Rejected candidates stay out, and reported entities stay in scope."""
    resolution = output["entity_resolution"]
    entities = output["affected_entities"]
    rejected = set(resolution["rejected_candidates"])
    reported = {value for group in entities.values() for value in group}
    if rejected & reported:
        raise ValueError("a rejected candidate appears in affected_entities")
    if rejected & set(resolution["resolved_order_ids"]):
        raise ValueError("a candidate is both resolved and rejected")
    if scope.resolved and not entities["order_ids"]:
        raise ValueError("a resolved order is missing from affected_entities")
    if resolution["status"] == "resolved" and not resolution["resolved_order_ids"]:
        raise ValueError("a resolved entity resolution names no order")
    if resolution["status"] != "resolved" and resolution["resolved_order_ids"]:
        raise ValueError("an unresolved entity resolution names an order")
    if entities["order_ids"] and resolution["resolved_order_ids"] != entities["order_ids"]:
        raise ValueError("affected order ids disagree with the resolved order")

    shipment = output["shipment_analysis"]
    if shipment["verdict"] == "on_time" and scope.delivered_at is None:
        raise ValueError("an on-time verdict needs a delivery timestamp")
    if shipment["late_seller_ids"] and shipment["verdict"] != "seller_delay":
        raise ValueError("late sellers named without a seller delay verdict")
    if not set(shipment["late_seller_ids"]).issubset(set(entities["seller_ids"])):
        raise ValueError("a late seller is not among this order's sellers")


def _verify_arbitration(output: dict[str, Any]) -> None:
    """The issue, the liable party and the analyses must tell one story."""
    issue = output["assessment"]["primary_issue"]
    status = output["assessment"]["case_status"]
    parties = output["root_cause_analysis"]["responsible_parties"]
    accountable = ACCOUNTABLE_PARTY.get(issue)

    if not parties:
        raise ValueError(f"{issue} names no responsible party")
    for party in parties:
        if accountable is not None and party["party_type"] != accountable:
            raise ValueError(
                f"{issue} cannot hold {party['party_type']} responsible; "
                f"the policy answers {accountable}"
            )

    allowed = CONSISTENT_SHIPMENT.get(issue)
    if allowed is not None and output["shipment_analysis"]["verdict"] not in allowed:
        raise ValueError(
            f"{issue} is inconsistent with a {output['shipment_analysis']['verdict']} timeline"
        )
    expected_payment = PAYMENT_VERDICTS.get(issue)
    if expected_payment is not None and output["payment_analysis"]["verdict"] != expected_payment:
        raise ValueError(f"{issue} is inconsistent with the reported payment verdict")

    causes = output["root_cause_analysis"]["ranked_causes"]
    if issue != "insufficient_evidence" and not causes:
        raise ValueError("a substantive finding ranks no root cause")
    if causes and causes[0]["cause_code"] != issue.upper():
        raise ValueError("the top ranked cause does not name the primary issue")
    if len({cause["rank"] for cause in causes}) != len(causes):
        raise ValueError("ranked causes repeat a rank")

    actions = output["resolution_actions"]
    if len(set(actions)) != len(actions):
        raise ValueError("resolution actions repeat")
    if not actions:
        raise ValueError("no resolution action was recorded")
    if status == "no_action" and output["financial_resolution"]["recommended_refund_brl"] > 0:
        raise ValueError("no_action cannot carry a refund")


def _verify_money(output: dict[str, Any]) -> str | None:
    """Refund lines, the recommendation and the ledger totals must reconcile.

    Returns a decision code when the policy's amount outruns what the payment
    ledger shows as still refundable.  The policy amount is authoritative, so
    that disagreement is reported rather than silently rewritten.
    """
    financial = output["financial_resolution"]
    recommended = Decimal(str(financial["recommended_refund_brl"]))
    lines = sum(
        (Decimal(str(line["amount_brl"])) for line in financial["refund_lines"]), Decimal(0)
    )
    if financial["refund_lines"] and recommended != lines:
        raise ValueError("refund lines do not reconcile with the recommended amount")
    if recommended > 0 and not financial["refund_lines"]:
        raise ValueError("a refund was recommended without a line to pay it on")
    if output["assessment"]["case_status"] == "action_required" and recommended <= 0:
        raise ValueError("action_required without a refund to collect")

    payment = output["payment_analysis"]
    captured = payment["captured_total_brl"]
    refunded = payment["refunded_total_brl"]
    refundable = payment["refundable_total_brl"]
    if None not in (captured, refunded, refundable):
        expected = Decimal(str(captured)) - Decimal(str(refunded))
        if Decimal(str(refundable)) != max(expected, Decimal(0)):
            raise ValueError("refundable total does not follow captured minus refunded")
    order_ids = set(output["affected_entities"]["order_ids"])
    for line in financial["refund_lines"]:
        entity = line["entity_id"]
        if entity is not None and order_ids and entity not in order_ids:
            raise ValueError("a refund line names an entity outside this order")

    if refundable is not None and recommended > Decimal(str(refundable)):
        return "POLICY_REFUND_EXCEEDS_REFUNDABLE_LEDGER"
    return None


def _verify_lifecycle(sequence: list[str] | None, substantive: bool) -> None:
    """The observable events this workflow owes the audit, in order.

    ``case_received``, ``case_finalized`` and the verifier's own event are the
    runner's to emit, so only what was raised inside the investigation is
    checked here.  ``tool_result_consumed`` is owed only by a case that reached
    a substantive finding: a gateway that answers nothing still has to produce
    an honest ``insufficient_evidence`` result rather than no result at all.
    """
    if sequence is None:
        return
    runner_owned = {"case_received", "case_finalized", "verification_completed"}
    owed = [event for event in required_lifecycle_events() if event not in runner_owned]
    owed.append("policy_decided")
    if substantive:
        owed.append("tool_result_consumed")
    for event in owed:
        if event not in sequence:
            raise ValueError(f"the trace is missing a {event} event")
    if sequence.index("task_assigned") > sequence.index("handoff"):
        raise ValueError("a handoff was traced before any task was assigned")
    if sequence.index("task_assigned") > sequence.index("policy_decided"):
        raise ValueError("the policy decided before any task was assigned")


# ---------------------------------------------------------------------------
# The agents.  Domain reasoning above; message handling below.
# ---------------------------------------------------------------------------


def _episode(scope: OrderScope) -> dict[str, Any]:
    """The revision of the order the case is about, as the coordinator sees it.

    ``order`` is how many revisions the customer's history holds for this id and
    ``window`` is the span a row has to fall in to belong to this revision.
    Together they name the episode, so a specialist handed a different one can
    tell that it was assigned work for another revision of the same order.
    """
    anchor, deadline = scope.anchor, scope.settle_deadline
    return {
        "order": scope.history_revisions,
        "window": {
            "from": anchor.isoformat() if anchor else None,
            "to": deadline.isoformat() if deadline else None,
        },
    }


def _optional_money(value: Decimal | None) -> float | None:
    """A money amount for the wire, or None where none was established."""
    return None if value is None else _money(value)


def _assigned_scope(ctx: CaseContext, message: A2AMessage) -> OrderScope:
    """The verified scope this assignment refers to, or a protocol violation.

    A specialist may only reason about the order the entity agent proved.  The
    assignment carries that order and episode, and is held against the scope
    rather than trusted, so a mis-routed task cannot quietly widen the scope.
    """
    scope = ctx.findings.get("scope")
    if not isinstance(scope, OrderScope) or not scope.resolved:
        raise ProtocolViolation(
            f"{message.recipient} was assigned work before any entity was resolved"
        )
    payload = message.payload
    episode = _episode(scope)
    if payload.get("order_id") != scope.order_id:
        raise ProtocolViolation(f"{message.sender} assigned an order outside the verified scope")
    if payload.get("episode_source") != scope.revision_source:
        raise ProtocolViolation(
            f"{message.sender} assigned a revision read from "
            f"{payload.get('episode_source')!r}, not {scope.revision_source!r}"
        )
    if payload.get("episode_order") != episode["order"]:
        raise ProtocolViolation(f"{message.sender} assigned a different episode of the order")
    if payload.get("window") != episode["window"]:
        raise ProtocolViolation(f"{message.sender} assigned a window outside this revision")
    return scope


def _established(
    ctx: CaseContext,
) -> tuple[OrderScope, OrderFacts, ShipmentFinding, PaymentFinding]:
    """Whatever the specialists established, empty where they never ran.

    A case whose entity never resolved has no order, shipment or payment work to
    show, and the review chain still owes it an honest result rather than none.
    """
    scope = ctx.findings.get("scope")
    facts = ctx.findings.get("facts")
    shipment = ctx.findings.get("shipment")
    payment = ctx.findings.get("payment")
    return (
        scope if isinstance(scope, OrderScope) else OrderScope(),
        facts if isinstance(facts, OrderFacts) else OrderFacts(),
        shipment if isinstance(shipment, ShipmentFinding) else ShipmentFinding(),
        payment if isinstance(payment, PaymentFinding) else PaymentFinding(),
    )


def _only(ctx: CaseContext, actor: str, intent: str, message: A2AMessage) -> None:
    if message.intent != intent:
        raise ProtocolViolation(f"{actor} was asked to {message.intent!r} and only does {intent!r}")


class EntityAgent:
    """Turns a candidate identifier into the one order the case is about."""

    actor = ENTITY_AGENT

    async def handle(self, ctx: CaseContext, message: A2AMessage) -> A2AMessage:
        _only(ctx, self.actor, "resolve_entity", message)
        scope = await resolve_entity(ctx, ctx.case)
        ctx.announce("scope", scope)
        # Which domain established which part of the scope.  The coordinator
        # reads the revision source back out of this, so ``customer`` appears
        # only when the history actually settled the revision.
        evidence: dict[str, str] = {}
        order_ref = ctx.ledger.refs("get_order")
        if order_ref:
            evidence["order"] = order_ref[0]
        history_ref = ctx.ledger.refs("get_customer_history")
        if history_ref and scope.revision_source == "get_customer_history":
            evidence["customer"] = history_ref[0]
        payload = {
            "order_id": scope.order_id if scope.resolved else None,
            "resolved": scope.resolved,
            "status": scope.status,
            "customer_unique_id": scope.customer_unique_id,
            "rejected_candidates": scope.rejected_candidates,
            "related_order_ids": scope.related_order_ids,
            "revision_differs": scope.revision_differs,
            "evidence": evidence,
            "episode": _episode(scope),
        }
        return message.reply(
            "entity_resolved",
            COORDINATOR,
            payload,
            evidence_refs=ctx.ledger.refs("get_order", "get_customer_history"),
            decision_code="ENTITY_RESOLVED" if scope.resolved else "ENTITY_NOT_FOUND",
        )


class OrderAgent:
    """Item and seller membership, plus the order's own value."""

    actor = ORDER_AGENT

    async def handle(self, ctx: CaseContext, message: A2AMessage) -> A2AMessage:
        _only(ctx, self.actor, "investigate_order", message)
        scope = _assigned_scope(ctx, message)
        facts = await investigate_order(ctx, scope)
        ctx.announce("facts", facts)
        payload = {
            "items": facts.item_ids,
            "sellers": facts.seller_ids,
            "order_value_brl": _optional_money(facts.order_value),
            "freight_value_brl": _optional_money(facts.freight_value),
            "out_of_scope_rows": facts.out_of_scope_rows,
            "product_confirmed": facts.product_confirmed,
        }
        return message.reply(
            "order_investigated",
            COORDINATOR,
            payload,
            evidence_refs=ctx.ledger.refs("get_order_items", "get_product_context"),
            decision_code=(
                "ITEM_MEMBERSHIP_CONFIRMED" if facts.item_ids else "ITEM_EVIDENCE_MISSING"
            ),
        )


class ShipmentAgent:
    """Promised against handed-over against delivered, and who answers for it."""

    actor = SHIPMENT_AGENT

    async def handle(self, ctx: CaseContext, message: A2AMessage) -> A2AMessage:
        _only(ctx, self.actor, "investigate_shipment", message)
        scope = _assigned_scope(ctx, message)
        _, facts, _, _ = _established(ctx)
        if message.payload.get("items") != facts.item_ids:
            raise ProtocolViolation(
                f"{message.sender} assigned items the order agent never confirmed"
            )
        finding = await investigate_shipment(ctx, scope, facts)
        # Announce before the seller lookup: the payment agent is waiting on the
        # verdict and the registry check cannot change it.
        ctx.announce("shipment", finding)
        await confirm_sellers(ctx, scope, facts, finding)
        payload = {
            "verdict": finding.verdict,
            "late_seller_ids": finding.late_seller_ids,
            "late_actor": finding.late_actor,
            "timeline_complete": finding.timeline_complete,
            "out_of_scope_events": finding.out_of_scope_events,
            "seller_confirmed": facts.seller_confirmed,
        }
        return message.reply(
            "shipment_investigated",
            COORDINATOR,
            payload,
            evidence_refs=ctx.ledger.refs("get_shipment_summary", "get_sellers"),
            decision_code=finding.verdict.upper(),
        )


class PaymentAgent:
    """Captures reconciled against the order, and the refund lifecycle."""

    actor = PAYMENT_AGENT

    async def handle(self, ctx: CaseContext, message: A2AMessage) -> A2AMessage:
        _only(ctx, self.actor, "investigate_payment", message)
        scope = _assigned_scope(ctx, message)
        _, facts, _, _ = _established(ctx)
        expected = None if facts.order_value is None else _money(facts.order_value)
        if message.payload.get("order_value_brl") != expected:
            raise ProtocolViolation(
                f"{message.sender} assigned an order value the order agent never read"
            )
        finding = await investigate_payment(ctx, scope, facts)
        ctx.announce("payment", finding)
        payload = {
            "captured_total_brl": _optional_money(finding.captured_total),
            "refunded_total_brl": _optional_money(finding.refunded_total),
            "refundable_total_brl": _optional_money(finding.refundable_total),
            "references": finding.references,
            "refund_state": finding.refund_state,
            "mismatch_open": finding.mismatch_open,
            "duplicate_capture": finding.duplicate_capture,
            "out_of_scope_events": finding.out_of_scope_events,
        }
        return message.reply(
            "payment_investigated",
            COORDINATOR,
            payload,
            evidence_refs=ctx.ledger.refs("get_payment_timeline", "get_refund_timeline"),
            decision_code=f"CAPTURES_{len(finding.capture_amounts)}",
        )


class ConflictAgent:
    """Reconciles sources that contradict each other.  No gateway access."""

    actor = CONFLICT_AGENT

    async def handle(self, ctx: CaseContext, message: A2AMessage) -> A2AMessage:
        _only(ctx, self.actor, "resolve_conflicts", message)
        conflicts = reconcile_conflicts(*_established(ctx))
        ctx.announce("conflicts", conflicts)
        payload = {
            "findings": message.payload.get("findings") or {},
            "data_conflicts": conflicts,
        }
        return message.reply(
            "decide_policy",
            POLICY_AGENT,
            payload,
            evidence_refs=message.evidence_refs,
            decision_code=f"CONFLICTS_RESOLVED_{len(conflicts)}",
        )


class PolicyAgent:
    """Applies the published rule table for this case's policy version."""

    actor = POLICY_AGENT

    async def handle(self, ctx: CaseContext, message: A2AMessage) -> A2AMessage:
        _only(ctx, self.actor, "decide_policy", message)
        scope, facts, shipment, payment = _established(ctx)
        primary_issue = classify(scope, facts, shipment, payment)
        decision = await decide_policy(ctx, ctx.case, scope, primary_issue)
        ctx.announce("primary_issue", primary_issue)
        ctx.announce("decision", decision)
        ctx.note(
            self.actor,
            "policy_decided",
            decision_code=(decision["recommended_action"] or "NO_APPLICABLE_POLICY_RULE").upper(),
            evidence_refs=ctx.ledger.refs("get_policy") or None,
        )
        payload = {
            "primary_issue": primary_issue,
            "case_status": decision["case_status"],
            "recommended_action": decision["recommended_action"],
            "refund_brl": _optional_money(decision["refund_brl"]),
            "policy_version": decision.get("policy_version"),
            "data_conflicts": message.payload.get("data_conflicts") or [],
        }
        return message.reply(
            "verify",
            VERIFIER_AGENT,
            payload,
            evidence_refs=unique_refs(message.evidence_refs, ctx.ledger.refs("get_policy")),
        )


class VerifierAgent:
    """Independent validation before a result may be finalised.  No gateway."""

    actor = VERIFIER_AGENT

    async def handle(self, ctx: CaseContext, message: A2AMessage) -> A2AMessage:
        _only(ctx, self.actor, "verify", message)
        scope, facts, shipment, payment = _established(ctx)
        decision = ctx.findings.get("decision")
        primary_issue = ctx.findings.get("primary_issue")
        if not isinstance(decision, dict) or not isinstance(primary_issue, str):
            raise ProtocolViolation("the verifier was asked to sign off before any policy decision")
        output = build_output(
            ctx.case, ctx.ledger, scope, facts, shipment, payment, decision, primary_issue
        )
        verdict = verify(output, ctx.ledger, scope, ctx.sequence)
        return message.reply(
            "finalize",
            COORDINATOR,
            {"output": output},
            evidence_refs=output["evidence_refs"][:20],
            decision_code=verdict,
            event_type="verification_completed",
        )


def build_agents() -> dict[str, Agent]:
    """The specialists this workflow routes between, by the name they answer to."""
    agents: tuple[Agent, ...] = (
        EntityAgent(),
        OrderAgent(),
        ShipmentAgent(),
        PaymentAgent(),
        ConflictAgent(),
        PolicyAgent(),
        VerifierAgent(),
    )
    return {agent.actor: agent for agent in agents}
