"""Conflict -> policy -> verifier chain on synthetic cases (no competition data, no network)."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

from student_agent.a2a import CaseContext
from student_agent.agents.verifier import verify
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import Coordinator
from test_specialists import ORDER_ID, SELLER, CannedGateway, responses

ROOT = Path(__file__).resolve().parents[1]
POLICY = {
    "currency": "BRL",
    "policy_version": "EC_POLICY_V2",
    "rules": {
        "late_delivery_logistics": {
            "case_status": "action_required", "recommended_action": "refund_freight",
            "refund_brl": 16.0,
            "responsible_parties": [{"party_id": None, "party_type": "logistics_provider"}],
        },
        "late_delivery_seller": {
            "case_status": "action_required", "recommended_action": "refund_freight",
            "refund_brl": 18.0,
            "responsible_parties": [{"party_id": "seller-from-other-case", "party_type": "seller"}],
        },
        "unsupported_claim": {
            "case_status": "no_action", "recommended_action": "document_no_action",
            "refund_brl": 0.0,
            "responsible_parties": [{"party_id": None, "party_type": "customer"}],
        },
    },
}  # fmt: skip


def solve(tmp_path: Path, canned: dict[str, Any], **case_overrides: Any):
    case = {
        "case_id": "L3B_CASE_TEST",
        "opened_at": "2018-01-01T09:00:00-03:00",
        "customer_request": {
            "claimed_order_id": ORDER_ID,
            "claims": [
                {"claim_id": "claim-a", "topic": "late_delivery_logistics"},
                {"claim_id": "claim-b", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V2",
        "candidate_order_ids": [ORDER_ID, "candidate-test"],
        "investigation_scope": {"include_product_context": True},
        "customer_unique_id_hint": "customer-hint",
        **case_overrides,
    }
    canned = {**canned, "get_policy": POLICY}
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace_path = tmp_path / "trace.jsonl"
    ctx = CaseContext(case, CannedGateway(canned), TraceWriter(trace_path, contracts))  # type: ignore[arg-type]
    output = asyncio.run(Coordinator().run(ctx))
    contracts.validate_output(output, "output")
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    return output, events, ctx


def test_end_to_end_output_is_scoped_consistent_and_traced(tmp_path: Path) -> None:
    output, events, ctx = solve(tmp_path, responses())
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["assessment"]["case_status"] == "action_required"
    # min(freight 18, refundable 16) and matches the policy amount
    assert output["financial_resolution"]["recommended_refund_brl"] == 16.0
    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "logistics_provider", "party_id": None}
    ]
    assert output["entity_resolution"]["rejected_candidates"] == ["candidate-test"]
    assert [claim["verdict"] for claim in output["claim_assessments"]] == ["supported"] * 2

    consumed = {ref for e in events if e["event_type"] == "tool_result_consumed"
                for ref in e["evidence_refs"]}  # fmt: skip
    # order, customer, item, shipment, payment and policy evidence are all cited
    assert set(output["evidence_refs"]) == consumed
    kinds = [event["event_type"] for event in events]
    assert kinds.index("policy_decided") < kinds.index("verification_completed")
    assert kinds[-1] == "handoff" and events[-1]["target"] == "coordinator"
    assert {event["actor"] for event in events} >= {
        "coordinator", "entity-agent", "order-agent", "shipment-agent", "payment-agent",
        "conflict-agent", "policy-agent", "verifier-agent",
    }  # fmt: skip
    # order, customer history, items, shipment, payment timeline, policy; no product/refund
    assert ctx.calls_made == 6


def test_evidence_overrides_contradicting_claim(tmp_path: Path) -> None:
    request = {
        "claimed_order_id": ORDER_ID,
        "claims": [{"claim_id": "claim-a", "topic": "late_delivery_seller"}],
    }
    output, _, _ = solve(tmp_path, responses(), customer_request=request)
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["assessment"]["confidence"] < 0.9
    assert output["claim_assessments"][0]["verdict"] == "unsupported"


def test_seller_party_comes_from_case_not_policy(tmp_path: Path) -> None:
    canned = responses()
    late = copy.deepcopy(canned["get_customer_history"]["orders"][1])
    late["order_delivered_carrier_date"] = "2017-12-26T09:00:00-03:00"
    canned["get_customer_history"]["orders"][1] = late
    canned["get_shipment_summary"]["events"][1]["actor"] = "seller"
    output, _, _ = solve(tmp_path, canned)
    assert output["assessment"]["primary_issue"] == "late_delivery_seller"
    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "seller", "party_id": SELLER}
    ]


def test_unresolved_entity_yields_needs_investigation(tmp_path: Path) -> None:
    request = {"claimed_order_id": "candidate-x", "claims": []}
    output, _, ctx = solve(
        tmp_path, responses(), customer_request=request, candidate_order_ids=["candidate-x"]
    )
    assert output["entity_resolution"]["status"] == "not_found"
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["assessment"]["confidence"] <= 0.4
    assert output["financial_resolution"]["recommended_refund_brl"] == 0.0
    assert ctx.calls_made == 2  # customer history + policy; no order lookups for placeholders


def test_verifier_repairs_inconsistent_draft(tmp_path: Path) -> None:
    output, _, ctx = solve(tmp_path, responses())
    broken = copy.deepcopy(output)
    broken["assessment"]["case_status"] = "no_action"
    broken["resolution_actions"] = ["refund_freight", "refund_freight"]
    broken["evidence_refs"].append("ev_never_returned_by_mcp_000")
    broken["root_cause_analysis"]["responsible_parties"] = [
        {"party_type": "seller", "party_id": "seller-from-other-case"}
    ]
    corrections = verify(ctx, broken)
    assert {"UNTRACED_EVIDENCE", "DUPLICATE_ACTIONS", "NO_ACTION_WITH_REFUND",
            "SELLER_PARTY_SCOPE"} <= set(corrections)  # fmt: skip
    assert broken["financial_resolution"] == {
        "currency": "BRL",
        "recommended_refund_brl": 0.0,
        "refund_lines": [],
    }
    assert broken["root_cause_analysis"]["responsible_parties"][0]["party_id"] == SELLER
    assert "ev_never_returned_by_mcp_000" not in broken["evidence_refs"]
    assert broken["assessment"]["confidence"] < output["assessment"]["confidence"]


def test_duplicated_record_is_cross_checked_against_independent_sources(tmp_path: Path) -> None:
    canned = responses()
    scoped = canned["get_customer_history"]["orders"][1]
    canned["get_order"] = scoped
    canned["get_customer_history"]["orders"] = [scoped, dict(scoped)]  # identical duplicate rows
    item = canned["get_order_items"][1]
    canned["get_order_items"] = [item, dict(item)]  # items are duplicated verbatim as well
    canned["get_sellers"] = [{"seller_id": SELLER, "seller_state": "SP"}]
    canned["get_order_payments"] = [
        {"payment_sequential": "1", "payment_type": "credit_card", "payment_value": "52.00"},
        {"payment_sequential": "1", "payment_type": "credit_card", "payment_value": "48.50"},
        {"payment_sequential": "2", "payment_type": "voucher", "payment_value": "48.50"},
    ]
    canned["get_payment_timeline"] = {
        "payments": canned["get_order_payments"],
        "events": [
            {"event_at": f"2017-12-20T{at}:00-03:00", "event_type": "captured",
             "amount_brl": amount, "status": "confirmed"}
            for amount, at in (("52.00", "10:00"), ("48.50", "10:00"), ("48.50", "11:00"))
        ],
    }
    canned["get_refund_timeline"] = {
        "events": [{"event_at": "2018-01-05T09:00:00-03:00", "event_type": "refund_requested",
                    "amount_brl": "52.00", "status": "failed"}]
    }  # fmt: skip
    request = {
        "claimed_order_id": ORDER_ID,
        "claims": [{"claim_id": "claim-a", "topic": "valid_split_payment"}],
    }
    output, events, ctx = solve(tmp_path, canned, customer_request=request)
    called = {tool for tool in (e.get("tool_name") for e in events) if tool}
    assert {"get_order_payments", "get_sellers", "get_shipment_summary"} <= called
    assert "order_record" in {conflict["field"] for conflict in output["data_conflicts"]}
    # the failed refund belongs to the foreign 52.00 capture, not to this split payment
    assert output["payment_analysis"]["verdict"] == "reconciled"
    assert output["payment_analysis"]["captured_total_brl"] == 97.0
    cited = {ctx.evidence[ref].tool_name for ref in output["evidence_refs"]}
    assert {"get_order_payments", "get_sellers", "get_shipment_summary"} <= cited
