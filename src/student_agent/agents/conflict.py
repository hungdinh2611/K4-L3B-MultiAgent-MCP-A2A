from __future__ import annotations

from typing import Any

from ..a2a import A2AMessage, Agent, CaseContext

MAX_CONFLICTS = 5


class ConflictAgent(Agent):
    """Detect disagreeing sources and pick the authoritative one; hands off to policy."""

    actor = "conflict-agent"

    async def handle(self, ctx: CaseContext, message: A2AMessage) -> A2AMessage:
        findings: dict[str, dict[str, Any]] = message.payload.get("findings", {})
        conflicts: dict[str, dict[str, Any]] = {}
        for payload in findings.values():
            for conflict in payload.get("conflicts", []):
                if _valid(conflict):
                    conflicts.setdefault(conflict["field"], conflict)

        shipment = findings.get("shipment-agent", {}).get("shipment_analysis", {})
        if shipment.get("verdict") == "conflicting":
            # Event actor and milestone dates disagree; no source outranks the other.
            conflicts.setdefault(
                "shipment_responsibility",
                {
                    "field": "shipment_responsibility",
                    "sources": ["get_shipment_summary.events", "get_customer_history"],
                    "selected_source": None,
                    "resolution_code": "UNRESOLVED_NEEDS_REVIEW",
                },
            )

        resolved = list(conflicts.values())[:MAX_CONFLICTS]
        payload = {
            "findings": findings,
            "conflicts": resolved,
            "unresolved_conflicts": sum(1 for item in resolved if item["selected_source"] is None),
        }
        return self.reply(
            message, "conflicts_resolved", payload, message.evidence_refs, "policy-agent"
        )


def _valid(conflict: Any) -> bool:
    return (
        isinstance(conflict, dict)
        and isinstance(conflict.get("field"), str)
        and isinstance(conflict.get("sources"), list)
        and len(set(conflict["sources"])) >= 2
        and "resolution_code" in conflict
    )
