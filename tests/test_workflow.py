from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import (
    EvidenceDesk,
    EvidenceLedger,
    OrderFacts,
    PaymentFinding,
    ShipmentFinding,
    classify,
    resolve_entity,
    solve_case,
)

ROOT = Path(__file__).resolve().parents[1]
ORDER_ID = "af0bbb47f125381ce9f3597dc70ef07b"
CUSTOMER_ID = "customer-597dc70ef07b"
REF = "ev_" + "a" * 24


def contracts() -> Contracts:
    return Contracts(ROOT / "contracts" / "schemas")


def envelope(domain: str, data: Any, ref: str = REF) -> dict[str, Any]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": ref,
        "result_hash": "sha256:" + "0" * 64,
        "domain": domain,
        "data": data,
    }


def order_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "order_id": ORDER_ID,
        "customer_id": "customer-row-af0bbb47f125",
        "order_status": "delivered",
        "order_purchase_timestamp": "2018-05-11T09:00:00-03:00",
        "order_approved_at": "2018-05-11T10:00:00-03:00",
        "order_delivered_carrier_date": "2018-05-13T09:00:00-03:00",
        "order_delivered_customer_date": "2018-05-20T09:00:00-03:00",
        "order_estimated_delivery_date": "2018-05-21T09:00:00-03:00",
    }
    row.update(overrides)
    return row


def case_file(
    case_id: str = "L3B_CASE_001", topic: str = "late_delivery_logistics"
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "opened_at": "2018-01-01T09:00:00-03:00",
        "customer_request": {
            "language": "vi",
            "message": "investigate",
            "claimed_order_id": ORDER_ID,
            "claims": [
                {"claim_id": "claim-a", "topic": topic},
                {"claim_id": "claim-b", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V2",
        "candidate_order_ids": [ORDER_ID, "candidate-001"],
        "investigation_scope": {"include_customer_history": True},
        "customer_unique_id_hint": CUSTOMER_ID,
    }


class FakeGateway:
    """Records how the desk calls the gateway and replays canned envelopes."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, {"case_id": case_id, **arguments}))
        outcome = self.responses[tool_name]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def desk_for(tmp_path: Path, gateway: FakeGateway, case_id: str = "L3B_CASE_001") -> EvidenceDesk:
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts())
    return EvidenceDesk(case_id, gateway, trace, EvidenceLedger())


def trace_events(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "trace.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_every_gateway_call_carries_the_case_id(tmp_path: Path) -> None:
    gateway = FakeGateway({"get_order": envelope("order", order_row())})
    desk = desk_for(tmp_path, gateway, "L3B_CASE_042")
    asyncio.run(desk.fetch("order-agent", "get_order", order_id=ORDER_ID))
    assert gateway.calls == [("get_order", {"case_id": "L3B_CASE_042", "order_id": ORDER_ID})]


def test_consumed_evidence_is_traced_with_the_verbatim_reference(tmp_path: Path) -> None:
    gateway = FakeGateway({"get_order": envelope("order", order_row())})
    desk = desk_for(tmp_path, gateway)
    evidence = asyncio.run(desk.fetch("order-agent", "get_order", order_id=ORDER_ID))
    assert evidence is not None
    consumed = [e for e in trace_events(tmp_path) if e["event_type"] == "tool_result_consumed"]
    assert len(consumed) == 1
    assert consumed[0]["actor"] == "order-agent"
    assert consumed[0]["tool_name"] == "get_order"
    assert consumed[0]["evidence_refs"] == [REF] == [evidence["evidence_ref"]]


def test_wrong_domain_is_discarded_without_a_consumed_event(tmp_path: Path) -> None:
    gateway = FakeGateway({"get_order": envelope("payment", order_row())})
    desk = desk_for(tmp_path, gateway)
    assert asyncio.run(desk.fetch("order-agent", "get_order", order_id=ORDER_ID)) is None
    assert trace_events(tmp_path) == []
    assert desk.ledger.all() == []


def test_tool_level_failure_is_final_and_not_retried(tmp_path: Path) -> None:
    gateway = FakeGateway({"get_refund_timeline": RuntimeError("MCP tool failed")})
    desk = desk_for(tmp_path, gateway)
    fetched = asyncio.run(desk.fetch("payment-agent", "get_refund_timeline", order_id=ORDER_ID))
    assert fetched is None
    assert len(gateway.calls) == 1
    assert trace_events(tmp_path) == []


def test_transport_fault_is_retried_once(tmp_path: Path) -> None:
    gateway = FakeGateway({"get_order": ConnectionError("reset")})
    desk = desk_for(tmp_path, gateway)
    assert asyncio.run(desk.fetch("entity-agent", "get_order", order_id=ORDER_ID)) is None
    assert len(gateway.calls) == 2


def test_entity_agent_rejects_a_candidate_absent_from_the_history(tmp_path: Path) -> None:
    gateway = FakeGateway(
        {
            "get_order": envelope("order", order_row()),
            "get_customer_history": envelope(
                "customer",
                {"customer_unique_id": CUSTOMER_ID, "orders": [order_row()]},
                ref="ev_" + "b" * 24,
            ),
        }
    )
    scope = asyncio.run(resolve_entity(desk_for(tmp_path, gateway), case_file()))
    assert scope.resolved is True
    assert scope.order_id == ORDER_ID
    assert scope.rejected_candidates == ["candidate-001"]
    assert scope.customer_unique_id == CUSTOMER_ID


def test_entity_agent_does_not_resolve_a_mismatched_order_row(tmp_path: Path) -> None:
    gateway = FakeGateway(
        {
            "get_order": envelope("order", order_row(order_id="some-other-order")),
            "get_customer_history": RuntimeError("no history"),
        }
    )
    scope = asyncio.run(resolve_entity(desk_for(tmp_path, gateway), case_file()))
    assert scope.resolved is False
    assert scope.order_id is None


def _moment(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value)


def scope_stub(**overrides: Any):
    from student_agent.workflow import OrderScope

    scope = OrderScope(order_id=ORDER_ID, status="delivered", resolved=True)
    for key, value in overrides.items():
        setattr(scope, key, value)
    return scope


@pytest.mark.parametrize(
    ("order_status", "shipment", "payment", "expected"),
    [
        (
            "canceled",
            ShipmentFinding(available=True),
            PaymentFinding(available=True, captured_total=79, capture_amounts=[79]),
            "canceled_order_paid",
        ),
        (
            "unavailable",
            ShipmentFinding(available=True),
            PaymentFinding(available=True, captured_total=89, capture_amounts=[89]),
            "unavailable_order_paid",
        ),
        (
            "delivered",
            ShipmentFinding(available=True, verdict="on_time"),
            PaymentFinding(
                available=True, captured_total=35, capture_amounts=[35], mismatch_open=True
            ),
            "payment_mismatch",
        ),
        (
            "delivered",
            ShipmentFinding(available=True, verdict="on_time"),
            PaymentFinding(
                available=True,
                captured_total=128,
                capture_amounts=[64, 64],
                duplicate_capture=True,
            ),
            "duplicate_charge",
        ),
        (
            "delivered",
            ShipmentFinding(available=True, verdict="seller_delay"),
            PaymentFinding(available=True, captured_total=18, capture_amounts=[18]),
            "late_delivery_seller",
        ),
        (
            "delivered",
            ShipmentFinding(available=True, verdict="logistics_delay"),
            PaymentFinding(available=True, captured_total=16, capture_amounts=[16]),
            "late_delivery_logistics",
        ),
        (
            "delivered",
            ShipmentFinding(available=True, verdict="on_time"),
            PaymentFinding(
                available=True, captured_total=52, capture_amounts=[52], refund_state="failed"
            ),
            "refund_failed",
        ),
        (
            "delivered",
            ShipmentFinding(available=True, verdict="on_time"),
            PaymentFinding(
                available=True, captured_total=89, capture_amounts=[89], refund_state="pending"
            ),
            "refund_pending",
        ),
        (
            "delivered",
            ShipmentFinding(available=True, verdict="on_time"),
            PaymentFinding(available=True, captured_total=89, capture_amounts=[44.5, 44.5]),
            "valid_split_payment",
        ),
        (
            "delivered",
            ShipmentFinding(available=True, verdict="on_time"),
            PaymentFinding(available=True, captured_total=89, capture_amounts=[89]),
            "unsupported_claim",
        ),
    ],
)
def test_classifier_follows_the_authoritative_signal(
    order_status: str, shipment: ShipmentFinding, payment: PaymentFinding, expected: str
) -> None:
    from decimal import Decimal

    payment.captured_total = Decimal(str(payment.captured_total))
    payment.capture_amounts = [Decimal(str(value)) for value in payment.capture_amounts]
    facts = OrderFacts(order_value=Decimal("89"), seller_ids=["seller-af0bbb47f125"])
    scope = scope_stub(status=order_status)
    assert classify(scope, facts, shipment, payment) == expected


def test_unresolved_entity_yields_insufficient_evidence() -> None:
    scope = scope_stub(resolved=False)
    assert (
        classify(scope, OrderFacts(), ShipmentFinding(), PaymentFinding())
        == "insufficient_evidence"
    )


def test_solve_case_output_only_cites_consumed_evidence(tmp_path: Path) -> None:
    refs = iter(f"ev_{letter * 24}" for letter in "abcdefgh")
    gateway = FakeGateway(
        {
            "get_order": envelope("order", order_row(), ref=next(refs)),
            "get_customer_history": envelope(
                "customer",
                {"customer_unique_id": CUSTOMER_ID, "orders": [order_row()]},
                ref=next(refs),
            ),
            "get_order_items": envelope(
                "item",
                [
                    {
                        "order_id": ORDER_ID,
                        "order_item_id": "item-af0bbb47f125",
                        "product_id": "product-af0bbb47f125",
                        "seller_id": "seller-af0bbb47f125",
                        "shipping_limit_date": "2018-05-14T09:00:00-03:00",
                        "price": "79.00",
                        "freight_value": "10.00",
                    }
                ],
                ref=next(refs),
            ),
            "get_product_context": envelope(
                "product",
                [{"order_item_id": "item-af0bbb47f125", "product_id": "product-af0bbb47f125"}],
                ref=next(refs),
            ),
            "get_shipment_summary": envelope(
                "shipment",
                {
                    "order_id": ORDER_ID,
                    "order_status": "delivered",
                    "delivered_carrier_at": "2018-05-13T09:00:00-03:00",
                    "delivered_customer_at": "2018-05-20T09:00:00-03:00",
                    "estimated_delivery_at": "2018-05-21T09:00:00-03:00",
                    "shipping_limits": [],
                    "events": [],
                },
                ref=next(refs),
            ),
            "get_payment_timeline": envelope(
                "payment",
                {
                    "order_id": ORDER_ID,
                    "payments": [
                        {
                            "order_id": ORDER_ID,
                            "payment_sequential": "1",
                            "payment_type": "credit_card",
                            "payment_installments": "1",
                            "payment_value": "89.00",
                        }
                    ],
                    "events": [
                        {
                            "order_id": ORDER_ID,
                            "event_at": "2018-05-11T10:00:00-03:00",
                            "event_type": "captured",
                            "amount_brl": "89.00",
                            "status": "confirmed",
                        }
                    ],
                },
                ref=next(refs),
            ),
            "get_refund_timeline": RuntimeError("no refund lifecycle"),
            "get_policy": envelope(
                "policy",
                {
                    "currency": "BRL",
                    "policy_version": "EC_POLICY_V2",
                    "rules": {
                        "unsupported_claim": {
                            "case_status": "no_action",
                            "recommended_action": "document_no_action",
                            "refund_brl": 0.0,
                            "responsible_parties": [
                                {"party_type": "customer", "party_id": None}
                            ],
                        }
                    },
                },
                ref=next(refs),
            ),
        }
    )
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts())
    output = asyncio.run(solve_case(case_file(), gateway, trace))
    contracts().validate_output(output, "solved")

    consumed = {
        event["evidence_refs"][0]
        for event in trace_events(tmp_path)
        if event["event_type"] == "tool_result_consumed"
    }
    assert set(output["evidence_refs"]) == consumed
    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["assessment"]["case_status"] == "no_action"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0.0
    assert output["payment_analysis"]["captured_total_brl"] == 89.0
    assert output["payment_analysis"]["refunded_total_brl"] == 0.0
    assert output["entity_resolution"]["rejected_candidates"] == ["candidate-001"]
    assert "candidate-001" not in output["affected_entities"]["order_ids"]


def test_solve_case_survives_a_gateway_that_answers_nothing(tmp_path: Path) -> None:
    gateway = FakeGateway(dict.fromkeys(
        [
            "get_order",
            "get_customer_history",
            "get_order_items",
            "get_product_context",
            "get_shipment_summary",
            "get_payment_timeline",
            "get_refund_timeline",
            "get_policy",
        ],
        RuntimeError("unavailable"),
    ))
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts())
    output = asyncio.run(solve_case(case_file(), gateway, trace))
    contracts().validate_output(output, "solved")
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["entity_resolution"]["status"] == "not_found"
    assert output["evidence_refs"] == []
    assert output["financial_resolution"]["refund_lines"] == []


def test_colliding_revisions_are_split_by_the_refund_lifecycle(tmp_path: Path) -> None:
    """Both revisions share a purchase date, so timestamps cannot separate them.

    The single 52.00 capture is the revision the failed refund was raised
    against; the 44.50 pair belongs to the other revision and must not be read
    as a duplicate charge.
    """
    from student_agent.workflow import investigate_payment

    def event(at: str, amount: str) -> dict[str, Any]:
        return {
            "order_id": ORDER_ID,
            "event_at": at,
            "event_type": "captured",
            "amount_brl": amount,
            "status": "confirmed",
        }

    gateway = FakeGateway(
        {
            "get_payment_timeline": envelope(
                "payment",
                {
                    "order_id": ORDER_ID,
                    "payments": [
                        {"order_id": ORDER_ID, "payment_sequential": "1",
                         "payment_type": "credit_card", "payment_value": "52.00"},
                        {"order_id": ORDER_ID, "payment_sequential": "1",
                         "payment_type": "credit_card", "payment_value": "44.50"},
                        {"order_id": ORDER_ID, "payment_sequential": "2",
                         "payment_type": "voucher", "payment_value": "44.50"},
                    ],
                    "events": [
                        event("2018-05-11T10:00:00-03:00", "52.00"),
                        event("2018-05-11T10:00:00-03:00", "44.50"),
                        event("2018-05-11T11:00:00-03:00", "44.50"),
                    ],
                },
            ),
            "get_refund_timeline": envelope(
                "refund",
                {
                    "order_id": ORDER_ID,
                    "events": [
                        {
                            "order_id": ORDER_ID,
                            "event_at": "2018-05-22T09:00:00-03:00",
                            "event_type": "refund_requested",
                            "amount_brl": "52.00",
                            "status": "failed",
                        }
                    ],
                },
                ref="ev_" + "c" * 24,
            ),
        }
    )
    from decimal import Decimal

    scope = scope_stub(
        purchase_at=_moment("2018-05-11T09:00:00-03:00"),
        approved_at=_moment("2018-05-11T10:00:00-03:00"),
        delivered_at=_moment("2018-05-20T09:00:00-03:00"),
        estimated_at=_moment("2018-05-21T09:00:00-03:00"),
    )
    facts = OrderFacts(order_value=Decimal("89.00"), seller_ids=["seller-af0bbb47f125"])
    payment = asyncio.run(investigate_payment(desk_for(tmp_path, gateway), scope, facts))

    assert payment.capture_ambiguous is True
    assert payment.capture_scoped_by_refund is True
    assert payment.capture_unresolved is False
    assert payment.capture_amounts == [Decimal("52.00")]
    assert payment.captured_total == Decimal("52.00")
    assert payment.duplicate_capture is False
    assert payment.refund_state == "failed"
    assert payment.references == ["1"]
    assert classify(scope, facts, ShipmentFinding(available=True, verdict="on_time"), payment) == (
        "refund_failed"
    )


# --------------------------------------------------------------- Phase 4


def _clean_output(**overrides: Any) -> dict[str, Any]:
    output = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": "L3B_CASE_001",
        "assessment": {
            "primary_issue": "late_delivery_seller",
            "secondary_issues": [],
            "case_status": "action_required",
            "confidence": 0.91,
        },
        "affected_entities": {
            "order_ids": [ORDER_ID],
            "item_ids": ["item-af0bbb47f125"],
            "seller_ids": ["seller-af0bbb47f125"],
            "payment_references": ["1"],
            "shipment_ids": [],
        },
        "claim_assessments": [],
        "entity_resolution": {
            "status": "resolved",
            "resolved_order_ids": [ORDER_ID],
            "rejected_candidates": ["candidate-001"],
            "confidence": 0.95,
        },
        "customer_context": {"customer_unique_id": CUSTOMER_ID, "related_order_ids": []},
        "shipment_analysis": {
            "verdict": "seller_delay",
            "late_seller_ids": ["seller-af0bbb47f125"],
            "timeline_complete": True,
        },
        "payment_analysis": {
            "verdict": "reconciled",
            "captured_total_brl": 18.0,
            "refunded_total_brl": 0.0,
            "refundable_total_brl": 18.0,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "LATE_DELIVERY_SELLER", "rank": 1}],
            "responsible_parties": [{"party_type": "seller", "party_id": "seller-af0bbb47f125"}],
        },
        "evidence_refs": [REF],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 18.0,
            "refund_lines": [
                {"reason_code": "refund_freight", "amount_brl": 18.0, "entity_id": ORDER_ID}
            ],
        },
        "resolution_actions": ["refund_freight"],
    }
    output.update(overrides)
    return output


def _loaded_ledger() -> EvidenceLedger:
    ledger = EvidenceLedger()
    ledger.register("get_order", REF)
    return ledger


def _delivered_scope():
    return scope_stub(delivered_at=_moment("2018-05-20T09:00:00-03:00"))


GOOD_SEQUENCE = ["task_assigned", "tool_result_consumed", "handoff", "policy_decided"]


def test_verifier_accepts_a_consistent_result() -> None:
    from student_agent.workflow import verify

    verdict = verify(_clean_output(), _loaded_ledger(), _delivered_scope(), list(GOOD_SEQUENCE))
    assert verdict == "ARBITRATION_AND_REFUND_VALIDATED"


def test_verifier_rejects_a_party_the_issue_does_not_answer() -> None:
    """A seller delay cannot leave the logistics provider paying."""
    from student_agent.workflow import verify

    output = _clean_output()
    output["root_cause_analysis"]["responsible_parties"] = [
        {"party_type": "logistics_provider", "party_id": None}
    ]
    with pytest.raises(ValueError, match="cannot hold logistics_provider responsible"):
        verify(output, _loaded_ledger(), _delivered_scope(), list(GOOD_SEQUENCE))


def test_verifier_rejects_an_issue_that_contradicts_the_timeline() -> None:
    from student_agent.workflow import verify

    output = _clean_output()
    output["shipment_analysis"]["verdict"] = "on_time"
    output["shipment_analysis"]["late_seller_ids"] = []
    with pytest.raises(ValueError, match="inconsistent with a on_time timeline"):
        verify(output, _loaded_ledger(), _delivered_scope(), list(GOOD_SEQUENCE))


def test_verifier_rejects_a_refund_no_line_pays_for() -> None:
    from student_agent.workflow import verify

    output = _clean_output()
    output["financial_resolution"]["refund_lines"] = []
    with pytest.raises(ValueError, match="without a line to pay it on"):
        verify(output, _loaded_ledger(), _delivered_scope(), list(GOOD_SEQUENCE))


def test_verifier_rejects_no_action_carrying_a_refund() -> None:
    from student_agent.workflow import verify

    output = _clean_output()
    output["assessment"]["case_status"] = "no_action"
    with pytest.raises(ValueError, match="no_action cannot carry a refund"):
        verify(output, _loaded_ledger(), _delivered_scope(), list(GOOD_SEQUENCE))


def test_verifier_rejects_a_late_seller_from_another_order() -> None:
    from student_agent.workflow import verify

    output = _clean_output()
    output["shipment_analysis"]["late_seller_ids"] = ["seller-somewhere-else"]
    with pytest.raises(ValueError, match="late seller is not among"):
        verify(output, _loaded_ledger(), _delivered_scope(), list(GOOD_SEQUENCE))


def test_verifier_rejects_a_rejected_candidate_reported_as_an_entity() -> None:
    from student_agent.workflow import verify

    output = _clean_output()
    output["affected_entities"]["order_ids"] = [ORDER_ID, "candidate-001"]
    with pytest.raises(ValueError, match="rejected candidate appears"):
        verify(output, _loaded_ledger(), _delivered_scope(), list(GOOD_SEQUENCE))


def test_verifier_rejects_a_missing_lifecycle_event() -> None:
    from student_agent.workflow import verify

    without_handoff = ["task_assigned", "tool_result_consumed", "policy_decided"]
    with pytest.raises(ValueError, match="missing a handoff event"):
        verify(_clean_output(), _loaded_ledger(), _delivered_scope(), without_handoff)


def test_verifier_reports_a_refund_beyond_the_remaining_ledger() -> None:
    """The policy amount is authoritative, so the gap is reported not rewritten."""
    from student_agent.workflow import verify

    output = _clean_output()
    output["payment_analysis"]["captured_total_brl"] = 10.0
    output["payment_analysis"]["refundable_total_brl"] = 10.0
    verdict = verify(output, _loaded_ledger(), _delivered_scope(), list(GOOD_SEQUENCE))
    assert verdict == "POLICY_REFUND_EXCEEDS_REFUNDABLE_LEDGER"


def _calibrate(payment: PaymentFinding, **kwargs: Any) -> float:
    from decimal import Decimal

    from student_agent.workflow import calibrate

    facts = OrderFacts(
        order_value=Decimal("89.00"), item_ids=["i"], seller_ids=["s"], product_confirmed=True
    )
    shipment = ShipmentFinding(available=True, verdict="seller_delay", timeline_complete=True)
    settings = {"alleged": set(), "policy_applied": True, **kwargs}
    return calibrate(
        "late_delivery_seller",
        scope_stub(history_revisions=2),
        facts,
        shipment,
        payment,
        settings["alleged"],
        policy_applied=settings["policy_applied"],
    )


def test_calibration_stays_short_of_certainty_and_tracks_evidence() -> None:
    from decimal import Decimal

    from student_agent.workflow import MAX_CONFIDENCE

    settled = PaymentFinding(available=True, captured_total=Decimal("18"))
    clean = _calibrate(settled)
    assert 0.80 < clean <= MAX_CONFIDENCE < 1.0

    ambiguous = _calibrate(
        PaymentFinding(available=True, captured_total=Decimal("18"), capture_ambiguous=True)
    )
    unresolved = _calibrate(
        PaymentFinding(available=True, captured_total=Decimal("18"), capture_unresolved=True)
    )
    assert unresolved < ambiguous < clean
    assert _calibrate(settled, policy_applied=False) < clean
    assert _calibrate(settled, alleged={"late_delivery_seller"}) > clean


def test_insufficient_evidence_is_reported_with_low_confidence() -> None:
    from student_agent.workflow import calibrate

    value = calibrate(
        "insufficient_evidence",
        scope_stub(resolved=False),
        OrderFacts(),
        ShipmentFinding(),
        PaymentFinding(),
        set(),
        policy_applied=False,
    )
    assert value <= 0.30


def test_lifecycle_validation_enforces_the_scoring_policy_events() -> None:
    from student_agent.submission import _validate_lifecycle
    from student_agent.workflow import required_lifecycle_events

    required = required_lifecycle_events()
    assert "case_received" in required
    assert "case_finalized" in required

    def line(kind: str) -> str:
        return json.dumps({"case_id": "L3B_CASE_001", "event_type": kind})

    full = [
        line(kind)
        for kind in (
            "case_received",
            "task_assigned",
            "tool_result_consumed",
            "handoff",
            "policy_decided",
            "verification_completed",
            "case_finalized",
        )
    ]
    _validate_lifecycle(full)

    with pytest.raises(ValueError, match="is missing"):
        _validate_lifecycle([entry for entry in full if "handoff" not in entry])
    with pytest.raises(ValueError, match="does not close with case_finalized"):
        _validate_lifecycle([*full, line("handoff")])
    with pytest.raises(ValueError, match="does not open with case_received"):
        _validate_lifecycle(full[1:] + [line("case_received")])
    with pytest.raises(ValueError, match="repeats case_received"):
        _validate_lifecycle([full[0], *full])
