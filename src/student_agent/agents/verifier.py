from __future__ import annotations

from decimal import Decimal
from typing import Any

from .. import OUTPUT_SCHEMA_VERSION
from ..a2a import COORDINATOR, A2AMessage, Agent, CaseContext
from ..timeline import brl, money, total

NO_ACTION_ACTIONS = {"document_no_action"}
MISSING_SHIPMENT = {
    "verdict": "insufficient_evidence",
    "late_seller_ids": [],
    "timeline_complete": False,
}
MISSING_PAYMENT = {
    "verdict": "insufficient_evidence",
    "captured_total_brl": None,
    "refunded_total_brl": None,
    "refundable_total_brl": None,
}


class VerifierAgent(Agent):
    """Check cross-field invariants, calibrate confidence and assemble the output."""

    actor = "verifier-agent"

    async def handle(self, ctx: CaseContext, message: A2AMessage) -> A2AMessage:
        findings: dict[str, dict[str, Any]] = message.payload.get("findings", {})
        conflicts: list[dict[str, Any]] = message.payload.get("conflicts", [])
        decision: dict[str, Any] = message.payload["decision"]

        output = assemble(ctx, findings, conflicts, decision)
        corrections = verify(ctx, output, unresolved=message.payload.get("unresolved_conflicts", 0))
        ctx.contracts.validate_output(output, f"outputs/{ctx.case_id}.json")

        ctx.emit(
            "verification_completed",
            actor=self.actor,
            decision_code="corrected" if corrections else "passed",
            evidence_refs=output["evidence_refs"][:20] or None,
            attributes={
                "checks": len(CHECKS),
                "corrections": len(corrections),
                "correction_codes": ",".join(corrections)[:160] or None,
                "confidence": output["assessment"]["confidence"],
            },
        )
        return self.reply(
            message, "finalize", {"output": output}, output["evidence_refs"], COORDINATOR
        )


def assemble(
    ctx: CaseContext,
    findings: dict[str, dict[str, Any]],
    conflicts: list[dict[str, Any]],
    decision: dict[str, Any],
) -> dict[str, Any]:
    entity = findings.get("entity-agent", {})
    order = findings.get("order-agent", {})
    order_id = entity.get("order_id")
    resolution = entity.get("entity_resolution") or {
        "status": "not_found",
        "resolved_order_ids": [],
        "rejected_candidates": [],
        "confidence": 0.1,
    }
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": ctx.case_id,
        "assessment": {
            "primary_issue": decision["primary_issue"],
            "secondary_issues": decision["secondary_issues"],
            "case_status": decision["case_status"],
            "confidence": decision["confidence"],
        },
        "affected_entities": {
            "order_ids": [order_id] if order_id else [],
            "item_ids": list(order.get("item_ids", [])),
            "seller_ids": list(order.get("seller_ids", [])),
            # Evidence carries no payment or shipment identifiers; none are invented.
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": decision["claim_assessments"],
        "entity_resolution": dict(resolution),
        "customer_context": dict(
            entity.get("customer_context") or {"customer_unique_id": None, "related_order_ids": []}
        ),
        "shipment_analysis": dict(
            findings.get("shipment-agent", {}).get("shipment_analysis") or MISSING_SHIPMENT
        ),
        "payment_analysis": dict(
            findings.get("payment-agent", {}).get("payment_analysis") or MISSING_PAYMENT
        ),
        "root_cause_analysis": {
            "ranked_causes": decision["ranked_causes"],
            "responsible_parties": decision["responsible_parties"],
        },
        "evidence_refs": list(decision["evidence_refs"]),
        "data_conflicts": list(conflicts),
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": decision["recommended_refund_brl"],
            "refund_lines": decision["refund_lines"],
        },
        "resolution_actions": decision["resolution_actions"],
    }


def _check_entity_scope(ctx: CaseContext, output: dict[str, Any]) -> list[str]:
    resolution = output["entity_resolution"]
    request = ctx.case.get("customer_request") or {}
    candidates = {*ctx.case.get("candidate_order_ids", []), request.get("claimed_order_id")}
    fixes = []
    resolved = [order for order in resolution["resolved_order_ids"] if order in candidates]
    if resolved != resolution["resolved_order_ids"]:
        resolution["resolved_order_ids"] = resolved
        fixes.append("ENTITY_OUT_OF_SCOPE")
    rejected = [order for order in resolution["rejected_candidates"] if order not in resolved]
    if rejected != resolution["rejected_candidates"]:
        resolution["rejected_candidates"] = rejected
        fixes.append("REJECTED_OVERLAP")
    if output["affected_entities"]["order_ids"] != resolved:
        output["affected_entities"]["order_ids"] = list(resolved)
        fixes.append("AFFECTED_ORDER_MISMATCH")
    return fixes


def _check_evidence(ctx: CaseContext, output: dict[str, Any]) -> list[str]:
    traced = ctx.consumed_refs()
    fixes = []
    kept = [ref for ref in output["evidence_refs"] if ref in ctx.evidence and ref in traced]
    if kept != output["evidence_refs"]:
        output["evidence_refs"] = kept
        fixes.append("UNTRACED_EVIDENCE")
    for claim in output.get("claim_assessments", []):
        claim["evidence_refs"] = [ref for ref in claim["evidence_refs"] if ref in kept]
    return fixes


def _check_money(ctx: CaseContext, output: dict[str, Any]) -> list[str]:
    financial = output["financial_resolution"]
    payment = output["payment_analysis"]
    fixes = []
    captured, refunded = money(payment["captured_total_brl"]), money(payment["refunded_total_brl"])
    if captured is not None and refunded is not None:
        refundable = max(captured - refunded, Decimal("0.00"))
        if money(payment["refundable_total_brl"]) != refundable:
            payment["refundable_total_brl"] = brl(refundable)
            fixes.append("REFUNDABLE_RECOMPUTED")
        if money(financial["recommended_refund_brl"]) > refundable:
            financial["recommended_refund_brl"] = brl(refundable)
            for line in financial["refund_lines"]:
                line["amount_brl"] = min(line["amount_brl"], brl(refundable))
            fixes.append("REFUND_CAPPED")
    lines_total = brl(total(money(line["amount_brl"]) for line in financial["refund_lines"]))
    if lines_total != financial["recommended_refund_brl"]:
        financial["recommended_refund_brl"] = lines_total
        fixes.append("REFUND_LINES_TOTAL")
    return fixes


def _check_status_actions(ctx: CaseContext, output: dict[str, Any]) -> list[str]:
    fixes = []
    actions = list(dict.fromkeys(output["resolution_actions"]))
    if actions != output["resolution_actions"]:
        output["resolution_actions"] = actions
        fixes.append("DUPLICATE_ACTIONS")
    financial = output["financial_resolution"]
    if output["assessment"]["case_status"] == "no_action" and (
        financial["recommended_refund_brl"] > 0 or not set(actions) <= NO_ACTION_ACTIONS
    ):
        financial["recommended_refund_brl"] = 0.0
        financial["refund_lines"] = []
        output["resolution_actions"] = ["document_no_action"]
        fixes.append("NO_ACTION_WITH_REFUND")
    return fixes


def _check_responsibility(ctx: CaseContext, output: dict[str, Any]) -> list[str]:
    sellers = output["affected_entities"]["seller_ids"]
    shipment = output["shipment_analysis"]
    fixes = []
    late = [seller for seller in shipment["late_seller_ids"] if seller in sellers]
    if shipment["verdict"] != "seller_delay":
        late = []
    if late != shipment["late_seller_ids"]:
        shipment["late_seller_ids"] = late
        fixes.append("LATE_SELLER_SCOPE")
    for party in output["root_cause_analysis"]["responsible_parties"]:
        if party["party_type"] == "seller" and party["party_id"] not in sellers:
            party["party_id"] = sellers[0] if sellers else None
            fixes.append("SELLER_PARTY_SCOPE")
        if party["party_type"] != "seller" and party["party_id"] is not None:
            party["party_id"] = None
            fixes.append("FOREIGN_PARTY_ID")
    return fixes


CHECKS = (
    _check_entity_scope,
    _check_evidence,
    _check_money,
    _check_status_actions,
    _check_responsibility,
)


def verify(ctx: CaseContext, output: dict[str, Any], *, unresolved: int = 0) -> list[str]:
    """Apply every invariant in place; returns the correction codes that fired."""
    corrections = [code for check in CHECKS for code in check(ctx, output)]
    confidence = float(output["assessment"]["confidence"])
    if output["entity_resolution"]["status"] != "resolved":
        confidence = min(confidence, 0.4)
    if unresolved:
        confidence = min(confidence, 0.7)
    if corrections:
        confidence -= 0.05 * len(corrections)
    output["assessment"]["confidence"] = round(min(max(confidence, 0.05), 0.95), 2)
    return corrections
