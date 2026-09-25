"""Specialist agents on synthetic two-episode orders (no competition data, no network)."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

from student_agent.a2a import CaseContext
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import Coordinator

ROOT = Path(__file__).resolve().parents[1]
ORDER_ID = "0123456789abcdef0123456789abcdef"
SELLER = "seller-test"


def order_row(day: str, delivered: str | None, estimated: str, status: str = "delivered"):
    return {
        "order_id": ORDER_ID,
        "customer_id": "customer-row-test",
        "order_status": status,
        "order_purchase_timestamp": f"{day}T09:00:00-03:00",
        "order_approved_at": f"{day}T10:00:00-03:00",
        "order_delivered_carrier_date": f"{day}T12:00:00-03:00",
        "order_delivered_customer_date": delivered and f"{delivered}T09:00:00-03:00",
        "order_estimated_delivery_date": f"{estimated}T09:00:00-03:00",
    }


# Distractor episode (after the complaint) is returned first, like the real gateway does.
DISTRACTOR = order_row("2018-05-01", "2018-05-20", "2018-05-10")
SCOPED = order_row("2017-12-20", "2018-01-04", "2017-12-30")


def responses() -> dict[str, Any]:
    return {
        "get_order": DISTRACTOR,
        "get_customer_history": {
            "customer_unique_id": "customer-hint",
            "orders": [DISTRACTOR, SCOPED],
        },
        "get_order_items": [
            {"order_item_id": "item-1", "product_id": "p", "seller_id": SELLER,
             "shipping_limit_date": "2018-05-03T09:00:00-03:00", "price": "79.00",
             "freight_value": "10.00"},
            {"order_item_id": "item-1", "product_id": "p", "seller_id": SELLER,
             "shipping_limit_date": "2017-12-23T09:00:00-03:00", "price": "79.00",
             "freight_value": "18.00"},
        ],
        "get_product_context": [{"order_item_id": "item-1", "category_name_english": "housewares"}],
        "get_shipment_summary": {
            "order_status": "delivered",
            "delivered_carrier_at": DISTRACTOR["order_delivered_carrier_date"],
            "delivered_customer_at": DISTRACTOR["order_delivered_customer_date"],
            "estimated_delivery_at": DISTRACTOR["order_estimated_delivery_date"],
            "shipping_limits": [
                {"order_item_id": "item-1", "seller_id": SELLER,
                 "shipping_limit_at": "2018-05-03T09:00:00-03:00"},
                {"order_item_id": "item-1", "seller_id": SELLER,
                 "shipping_limit_at": "2017-12-23T09:00:00-03:00"},
            ],
            "events": [
                {"event_at": "2018-05-20T09:00:00-03:00", "event_type": "delivered_late",
                 "actor": "seller", "status": "confirmed"},
                {"event_at": "2018-01-04T09:00:00-03:00", "event_type": "delivered_late",
                 "actor": "logistics_provider", "status": "confirmed"},
            ],
        },
        "get_payment_timeline": {
            "payments": [],
            "events": [
                {"event_at": "2018-05-01T10:00:00-03:00", "event_type": "captured",
                 "amount_brl": "89.00", "status": "confirmed"},
                {"event_at": "2017-12-20T10:00:00-03:00", "event_type": "captured",
                 "amount_brl": "16.00", "status": "confirmed"},
            ],
        },
        "get_refund_timeline": RuntimeError("Error executing tool get_refund_timeline"),
    }  # fmt: skip


class CannedGateway:
    def __init__(self, canned: dict[str, Any]) -> None:
        self.canned = canned
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, arguments))
        value = self.canned[tool_name]
        if isinstance(value, Exception):
            raise value
        if tool_name == "get_order" and arguments["order_id"] != ORDER_ID:
            raise RuntimeError("Error executing tool get_order")
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{tool_name}_{len(self.calls):02d}_synthetic",
            "result_hash": "sha256:" + "0" * 64,
            "domain": "order",
            "data": copy.deepcopy(value),
        }


def investigate(
    tmp_path: Path,
    canned: dict[str, Any],
    topic: str | None = None,
    opened_at: str = "2018-01-01T09:00:00-03:00",
) -> tuple[dict[str, Any], CannedGateway]:
    claims = [{"claim_id": "claim-a", "topic": topic}] if topic else []
    case = {
        "case_id": "L3B_CASE_TEST",
        "opened_at": opened_at,
        "customer_request": {"claimed_order_id": ORDER_ID, "claims": claims},
        "candidate_order_ids": [ORDER_ID, "candidate-test"],
        "investigation_scope": {"include_product_context": True},
        "customer_unique_id_hint": "customer-hint",
    }
    gateway = CannedGateway(canned)
    trace = TraceWriter(tmp_path / "trace.jsonl", Contracts(ROOT / "contracts" / "schemas"))
    ctx = CaseContext(case, gateway, trace)  # type: ignore[arg-type]
    results = asyncio.run(Coordinator().investigate(ctx))
    return {message.sender: message.payload for message in results}, gateway


def test_entity_rejects_placeholder_without_lookup_and_scopes_episode(tmp_path: Path) -> None:
    findings, gateway = investigate(tmp_path, responses())
    entity = findings["entity-agent"]
    assert entity["entity_resolution"]["status"] == "resolved"
    assert entity["entity_resolution"]["rejected_candidates"] == ["candidate-test"]
    assert [args for tool, args in gateway.calls if tool == "get_order"] == [{"order_id": ORDER_ID}]
    assert entity["episode"]["order"] == SCOPED
    assert entity["customer_context"]["customer_unique_id"] == "customer-hint"
    assert [conflict["field"] for conflict in entity["conflicts"]] == ["order_timeline"]


def test_specialists_ignore_distractor_episode(tmp_path: Path) -> None:
    findings, gateway = investigate(tmp_path, responses())
    assert findings["order-agent"]["order_value_brl"] == 97.0
    assert findings["shipment-agent"]["shipment_analysis"] == {
        "verdict": "logistics_delay",
        "late_seller_ids": [],
        "timeline_complete": True,
    }
    payment = findings["payment-agent"]["payment_analysis"]
    assert payment["verdict"] == "reconciled"
    assert payment["captured_total_brl"] == 16.0
    assert payment["refundable_total_brl"] == 16.0
    called = {tool for tool, _ in gateway.calls}
    assert "get_sellers" not in called and "get_order_payments" not in called


def test_payment_separates_split_from_duplicate(tmp_path: Path) -> None:
    def with_captures(amount: str) -> dict[str, Any]:
        canned = responses()
        canned["get_payment_timeline"]["events"] = [
            {"event_at": f"2017-12-20T1{hour}:00:00-03:00", "event_type": "captured",
             "amount_brl": amount, "status": "confirmed"}
            for hour in (0, 1)
        ]  # fmt: skip
        return canned

    split, _ = investigate(tmp_path / "split", with_captures("48.50"))
    duplicate, _ = investigate(tmp_path / "duplicate", with_captures("64.00"))
    assert split["payment-agent"]["split_payment"] is True
    assert split["payment-agent"]["payment_analysis"]["verdict"] == "reconciled"
    assert duplicate["payment-agent"]["payment_analysis"]["verdict"] == "duplicate_capture"
    assert duplicate["payment-agent"]["duplicate_amount_brl"] == 64.0


def test_seller_delay_uses_scoped_shipping_limit(tmp_path: Path) -> None:
    canned = responses()
    late = dict(SCOPED, order_delivered_carrier_date="2017-12-26T09:00:00-03:00")
    canned["get_customer_history"]["orders"] = [DISTRACTOR, late]
    canned["get_shipment_summary"]["events"][1]["actor"] = "seller"
    findings, _ = investigate(tmp_path, canned)
    assert findings["shipment-agent"]["shipment_analysis"]["verdict"] == "seller_delay"
    assert findings["shipment-agent"]["shipment_analysis"]["late_seller_ids"] == [SELLER]


def test_refund_and_product_lookups_follow_the_claim(tmp_path: Path) -> None:
    _, plain = investigate(tmp_path / "plain", responses())
    skipped = {tool for tool, _ in plain.calls}
    assert not skipped & {"get_refund_timeline", "get_product_context"}

    canned = responses()
    canned["get_refund_timeline"] = {
        "events": [
            {"event_at": "2018-01-05T09:00:00-03:00", "event_type": "refund_requested",
             "amount_brl": "16.00", "status": "failed"},
        ]
    }  # fmt: skip
    refund, gateway = investigate(tmp_path / "refund", canned, topic="refund_failed")
    assert "get_refund_timeline" in {tool for tool, _ in gateway.calls}
    assert refund["payment-agent"]["payment_analysis"]["verdict"] == "refund_failed"
    assert "refund" in refund["payment-agent"]["evidence"]

    _, gateway = investigate(tmp_path / "product", responses(), topic="unavailable_order_paid")
    assert "get_product_context" in {tool for tool, _ in gateway.calls}


def test_claim_picks_matching_episode_among_eligible_ones(tmp_path: Path) -> None:
    canceled = order_row("2017-12-01", None, "2017-12-10", status="canceled")
    canned = responses()
    canned["get_customer_history"]["orders"] = [DISTRACTOR, canceled, SCOPED]
    opened = "2018-01-10T09:00:00-03:00"

    latest, _ = investigate(tmp_path / "latest", canned, opened_at=opened)
    claimed, _ = investigate(tmp_path / "claimed", canned, "canceled_order_paid", opened)
    assert latest["entity-agent"]["episode"]["order"] == SCOPED
    assert claimed["entity-agent"]["episode"]["order"] == canceled
    assert claimed["shipment-agent"]["shipment_analysis"]["verdict"] == "insufficient_evidence"


def test_payment_ignores_duplicated_events_and_foreign_payment_sets(tmp_path: Path) -> None:
    def captures(*amounts_at: tuple[str, str]) -> list[dict[str, str]]:
        return [
            {"event_at": f"2017-12-20T{at}:00-03:00", "event_type": "captured",
             "amount_brl": amount, "status": "confirmed"}
            for amount, at in amounts_at
        ]  # fmt: skip

    duplicated = responses()
    duplicated["get_payment_timeline"]["events"] = captures(("97.00", "10:00"), ("97.00", "10:00"))
    findings, _ = investigate(tmp_path / "duplicated", duplicated)
    assert findings["payment-agent"]["payment_analysis"]["captured_total_brl"] == 97.0
    assert findings["payment-agent"]["payment_analysis"]["verdict"] == "reconciled"

    mixed = responses()
    mixed["get_payment_timeline"] = {
        "payments": [
            {"payment_sequential": "1", "payment_type": "credit_card", "payment_value": "52.00"},
            {"payment_sequential": "1", "payment_type": "credit_card", "payment_value": "48.50"},
            {"payment_sequential": "2", "payment_type": "voucher", "payment_value": "48.50"},
        ],
        "events": captures(("52.00", "10:00"), ("48.50", "10:00"), ("48.50", "11:00")),
    }  # fmt: skip
    findings, _ = investigate(tmp_path / "mixed", mixed)
    payment = findings["payment-agent"]
    assert payment["split_payment"] is True
    assert payment["payment_analysis"]["captured_total_brl"] == 97.0
    assert [conflict["field"] for conflict in payment["conflicts"]] == ["captured_payments"]


def test_shipment_summary_only_fetched_when_it_supports_the_conclusion(tmp_path: Path) -> None:
    on_time = order_row("2017-12-20", "2017-12-28", "2017-12-30")
    canned = responses()
    canned["get_customer_history"]["orders"] = [DISTRACTOR, on_time]

    payment_claim, gateway = investigate(tmp_path / "payment", canned, topic="payment_mismatch")
    assert "get_shipment_summary" not in {tool for tool, _ in gateway.calls}
    assert payment_claim["shipment-agent"]["shipment_analysis"]["verdict"] == "on_time"
    assert payment_claim["shipment-agent"]["evidence"] == {}

    late_claim, gateway = investigate(tmp_path / "late", canned, topic="late_delivery_seller")
    assert "get_shipment_summary" in {tool for tool, _ in gateway.calls}
    # the summary adds a delivered_late event that contradicts the on-time milestones
    assert late_claim["shipment-agent"]["shipment_analysis"]["verdict"] == "conflicting"
