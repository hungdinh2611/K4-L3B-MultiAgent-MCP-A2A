from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..a2a import A2AMessage, Agent, CaseContext
from ..timeline import brl, claimed_topic, money

LATE_DELIVERY = {"late_delivery_seller", "late_delivery_logistics"}
NO_FAULT = {"valid_split_payment", "unsupported_claim"}
PAYMENT_SIGNALS = {
    "refund_failed": "refund_failed",
    "refund_pending": "refund_pending",
    "capture_mismatch": "payment_mismatch",
    "duplicate_capture": "duplicate_charge",
}
SHIPMENT_SIGNALS = {
    "seller_delay": "late_delivery_seller",
    "logistics_delay": "late_delivery_logistics",
}
# Evidence domains that support each conclusion; order, customer and policy always do.
# Citing every consumed domain lowers evidence precision (scored as F1), so the rest are
# cited only where they support the primary issue or a claim assessment.
BASE_DOMAINS = ("order", "customer", "policy")
RELEVANT_DOMAINS = {
    "late_delivery_seller": ("shipment", "item"),
    "late_delivery_logistics": ("shipment", "item"),
    "canceled_order_paid": ("payment", "refund"),
    "unavailable_order_paid": ("payment", "item", "product"),
    "valid_split_payment": ("payment",),
    "payment_mismatch": ("payment",),
    "duplicate_charge": ("payment",),
    "refund_pending": ("payment", "refund"),
    "refund_failed": ("payment", "refund"),
    "unsupported_claim": ("shipment", "payment"),
    "insufficient_evidence": ("shipment", "payment", "refund", "item"),
}
INSUFFICIENT_RULE = {
    "case_status": "needs_investigation",
    "recommended_action": "request_additional_evidence",
    "refund_brl": 0.0,
    "responsible_parties": [{"party_type": "unknown", "party_id": None}],
}


class PolicyAgent(Agent):
    """Apply the case policy version to specialist findings; hands off to verifier."""

    actor = "policy-agent"
    tools = frozenset({"get_policy"})

    async def handle(self, ctx: CaseContext, message: A2AMessage) -> A2AMessage:
        findings: dict[str, dict[str, Any]] = message.payload.get("findings", {})
        conflicts: list[dict[str, Any]] = message.payload.get("conflicts", [])
        refs = _evidence_by_domain(findings)

        version = ctx.case.get("policy_version")
        policy = await ctx.fetch(self, "get_policy", policy_version=version) if version else None
        rules = {}
        if policy is not None and isinstance(policy.data, dict):
            refs["policy"] = policy.ref
            rules = policy.data.get("rules") or {}

        claimed = claimed_topic(ctx.case)
        signals = detect_signals(findings)
        primary = choose_primary(findings, signals, claimed)
        rule = rules.get(primary) or INSUFFICIENT_RULE
        if primary != "insufficient_evidence" and primary not in rules:
            primary, rule = "insufficient_evidence", INSUFFICIENT_RULE

        refund, policy_amount = _refund(primary, rule, findings)
        order_id = findings.get("entity-agent", {}).get("order_id")
        action = str(rule.get("recommended_action") or "request_additional_evidence")
        case_status = str(rule.get("case_status") or "needs_investigation")
        claims = _claims(ctx.case, primary, case_status, refund, findings, refs)
        cited = list(
            dict.fromkeys(
                [
                    *_cite(refs, primary),
                    *(ref for claim in claims for ref in claim["evidence_refs"]),
                ]
            )
        )
        if findings.get("entity-agent", {}).get("duplicated_records"):
            # A duplicated record is only settled by the independent sources checked against it.
            cited = list(dict.fromkeys([*cited, *refs.values()]))

        confidence = 0.9
        if primary == "insufficient_evidence":
            confidence = 0.35
        elif claimed and claimed != primary:
            confidence = 0.7
        if len(signals) > 1:
            confidence -= 0.05
        if message.payload.get("unresolved_conflicts"):
            confidence -= 0.15
        if "policy" not in refs:
            confidence -= 0.2
        if policy_amount is not None and refund != policy_amount:
            confidence -= 0.05

        decision = {
            "primary_issue": primary,
            "secondary_issues": [signal for signal in signals if signal != primary],
            "case_status": case_status,
            "confidence": round(min(max(confidence, 0.05), 0.95), 2),
            "ranked_causes": [{"cause_code": primary.upper(), "rank": 1}],
            "responsible_parties": _parties(rule, findings),
            "recommended_refund_brl": brl(refund),
            "refund_lines": (
                [{"reason_code": action, "amount_brl": brl(refund), "entity_id": order_id}]
                if refund > 0
                else []
            ),
            "resolution_actions": [action],
            "claim_assessments": claims,
            "evidence_refs": cited,
        }
        ctx.emit(
            "policy_decided",
            actor=self.actor,
            decision_code=primary,
            evidence_refs=[refs["policy"]] if "policy" in refs else None,
            attributes={
                "case_status": case_status,
                "recommended_refund_brl": brl(refund),
                "policy_version": version,
                "signals": len(signals),
                "claim_matches": claimed == primary,
            },
        )
        payload = {"findings": findings, "conflicts": conflicts, "decision": decision}
        return self.reply(
            message,
            "policy_decided",
            payload,
            [*message.evidence_refs, *refs.values()],
            "verifier-agent",
        )


def detect_signals(findings: dict[str, dict[str, Any]]) -> list[str]:
    """Issues evidenced inside the case episode, in precedence order."""
    entity = findings.get("entity-agent", {})
    shipment = findings.get("shipment-agent", {})
    payment = findings.get("payment-agent", {})
    analysis = payment.get("payment_analysis", {})
    status = shipment.get("order_status") or (entity.get("episode") or {}).get("order", {}).get(
        "order_status"
    )
    outstanding = money(analysis.get("refundable_total_brl")) or Decimal("0")

    signals = []
    if status == "canceled" and outstanding > 0:
        signals.append("canceled_order_paid")
    if status == "unavailable" and outstanding > 0:
        signals.append("unavailable_order_paid")
    if analysis.get("verdict") in PAYMENT_SIGNALS:
        signals.append(PAYMENT_SIGNALS[analysis["verdict"]])
    shipment_verdict = shipment.get("shipment_analysis", {}).get("verdict")
    if shipment_verdict in SHIPMENT_SIGNALS:
        signals.append(SHIPMENT_SIGNALS[shipment_verdict])
    if payment.get("split_payment"):
        signals.append("valid_split_payment")
    return signals


def choose_primary(
    findings: dict[str, dict[str, Any]], signals: list[str], claimed: str | None
) -> str:
    entity = findings.get("entity-agent", {})
    if entity.get("entity_resolution", {}).get("status") != "resolved":
        return "insufficient_evidence"
    if claimed in signals:
        return claimed
    if signals:
        return signals[0]
    shipment = findings.get("shipment-agent", {}).get("shipment_analysis", {}).get("verdict")
    payment = findings.get("payment-agent", {}).get("payment_analysis", {}).get("verdict")
    if shipment == "conflicting" or (
        shipment in {None, "insufficient_evidence"} and payment in {None, "insufficient_evidence"}
    ):
        return "insufficient_evidence"
    return "unsupported_claim"


def _refund(
    primary: str, rule: dict[str, Any], findings: dict[str, dict[str, Any]]
) -> tuple[Decimal, Decimal | None]:
    """Case-specific refund from evidence, capped by what is still refundable."""
    policy_amount = money(rule.get("refund_brl"))
    if policy_amount is None or policy_amount == 0:
        return Decimal("0.00"), policy_amount
    order = findings.get("order-agent", {})
    payment = findings.get("payment-agent", {})
    refundable = money(payment.get("payment_analysis", {}).get("refundable_total_brl"))
    if primary in {"canceled_order_paid", "unavailable_order_paid"}:
        amount = refundable
    elif primary in LATE_DELIVERY:
        amount = money(order.get("freight_brl"))
    elif primary == "payment_mismatch":
        amount = money(payment.get("mismatch_amount_brl"))
    elif primary == "duplicate_charge":
        amount = money(payment.get("duplicate_amount_brl"))
    elif primary == "refund_failed":
        amount = money(payment.get("refund_requested_brl"))
    else:
        amount = None
    if not amount:
        amount = policy_amount
    if refundable is not None:
        amount = min(amount, refundable)
    return max(amount, Decimal("0.00")), policy_amount


def _parties(rule: dict[str, Any], findings: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Policy party types; seller IDs always come from this case, never from the policy."""
    shipment = findings.get("shipment-agent", {}).get("shipment_analysis", {})
    sellers = shipment.get("late_seller_ids") or findings.get("order-agent", {}).get("seller_ids")
    parties = []
    for party in rule.get("responsible_parties") or []:
        party_type = party.get("party_type", "unknown")
        party_id = sellers[0] if party_type == "seller" and sellers else None
        parties.append({"party_type": party_type, "party_id": party_id})
    return parties or [{"party_type": "unknown", "party_id": None}]


def _claims(
    case: dict[str, Any],
    primary: str,
    case_status: str,
    refund: Decimal,
    findings: dict[str, dict[str, Any]],
    refs: dict[str, str],
) -> list[dict[str, Any]]:
    refundable = money(
        findings.get("payment-agent", {}).get("payment_analysis", {}).get("refundable_total_brl")
    )
    assessments = []
    for claim in (case.get("customer_request") or {}).get("claims", [])[:5]:
        topic = claim.get("topic")
        if topic == "requested_full_refund":
            if refund > 0 and refundable is not None and refund >= refundable:
                verdict = "supported"
            elif refund > 0:
                verdict = "partially_supported"
            elif case_status == "needs_investigation":
                verdict = "insufficient_evidence"
            else:
                verdict = "unsupported"
            domains = ("payment", "refund", "policy")
        else:
            if primary == "insufficient_evidence":
                verdict = "insufficient_evidence"
            elif topic == primary and primary not in NO_FAULT:
                verdict = "supported"
            else:
                verdict = "unsupported"
            domains = RELEVANT_DOMAINS.get(primary, ())
        assessments.append(
            {
                "claim_id": str(claim.get("claim_id"))[:64],
                "verdict": verdict,
                "confidence": 0.85 if verdict != "insufficient_evidence" else 0.4,
                "evidence_refs": [refs[domain] for domain in domains if domain in refs],
            }
        )
    return assessments


def _evidence_by_domain(findings: dict[str, dict[str, Any]]) -> dict[str, str]:
    refs: dict[str, str] = {}
    for payload in findings.values():
        refs.update(payload.get("evidence") or {})
    return refs


def _cite(refs: dict[str, str], primary: str) -> list[str]:
    domains = (*BASE_DOMAINS, *RELEVANT_DOMAINS.get(primary, ()))
    return list(dict.fromkeys(refs[domain] for domain in domains if domain in refs))
