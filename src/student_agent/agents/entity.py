from __future__ import annotations

from typing import Any

from ..a2a import A2AMessage, Agent, CaseContext, Evidence
from ..timeline import ORDER_ID_PATTERN, claimed_topic, parse_time, select_episode, unique_rows

MAX_ORDER_LOOKUPS = 2


class EntityAgent(Agent):
    """Resolve the complaint to one scoped order and its customer history."""

    actor = "entity-agent"
    tools = frozenset({"get_order", "get_customer_history"})

    async def handle(self, ctx: CaseContext, message: A2AMessage) -> A2AMessage:
        case = ctx.case
        request = case.get("customer_request") or {}
        claimed = request.get("claimed_order_id")
        candidates = list(
            dict.fromkeys(
                value
                for value in [claimed, *case.get("candidate_order_ids", [])]
                if isinstance(value, str) and value
            )
        )
        # Malformed candidates (e.g. placeholders) are rejected without spending MCP calls.
        lookups = [value for value in candidates if ORDER_ID_PATTERN.fullmatch(value)]

        found: dict[str, Evidence] = {}
        for candidate in lookups[:MAX_ORDER_LOOKUPS]:
            evidence = await ctx.fetch(self, "get_order", order_id=candidate)
            if evidence and _order_row(evidence.data, candidate):
                found[candidate] = evidence
                if candidate == claimed:
                    break

        history = await self._history(ctx, case.get("customer_unique_id_hint"))
        history_rows = _history_rows(history)
        history_order_ids = {row.get("order_id") for row in history_rows}

        if claimed in found:
            resolved = [claimed]
        elif len(found) > 1:
            resolved = [order_id for order_id in found if order_id in history_order_ids]
        else:
            resolved = list(found)
        status = "resolved" if len(resolved) == 1 else ("ambiguous" if found else "not_found")
        order_id = resolved[0] if status == "resolved" else None
        rejected = [value for value in candidates if value != order_id]

        refs: dict[str, str] = {}
        payload: dict[str, Any] = {
            "order_id": order_id,
            "entity_resolution": {
                "status": status,
                "resolved_order_ids": [order_id] if order_id else [],
                "rejected_candidates": rejected if order_id else [],
                "confidence": 0.0,
            },
            "customer_context": {"customer_unique_id": None, "related_order_ids": []},
            "conflicts": [],
        }
        if order_id is None:
            payload["entity_resolution"]["confidence"] = 0.4 if status == "ambiguous" else 0.1
            payload["evidence"] = refs
            return self.reply(message, status, payload, [ev.ref for ev in found.values()])

        order_evidence = found[order_id]
        order_row = order_evidence.data
        refs["order"] = order_evidence.ref
        customer_row_id = order_row.get("customer_id")
        linked = [
            row
            for row in history_rows
            if row.get("order_id") == order_id and row.get("customer_id") == customer_row_id
        ]
        if history is not None and linked:
            refs["customer"] = history.ref
            payload["customer_context"] = {
                "customer_unique_id": history.data.get("customer_unique_id"),
                "related_order_ids": sorted(
                    {row["order_id"] for row in history_rows if row.get("order_id")}
                ),
            }
        episodes = linked or [order_row]
        episode, window, others = select_episode(
            episodes, parse_time(case.get("opened_at")), claimed_topic(case)
        )
        if episode != order_row:
            payload["conflicts"].append(
                {
                    "field": "order_timeline",
                    "sources": ["get_order", "get_customer_history"],
                    "selected_source": "get_customer_history",
                    "resolution_code": "CASE_TIMELINE_SCOPE",
                }
            )
        # Identical duplicate rows mean the order record itself is corrupted, so every
        # specialist cross-checks it against its independent sources.
        duplicated = len(linked) > len(unique_rows(linked))
        if duplicated:
            payload["conflicts"].append(
                {
                    "field": "order_record",
                    "sources": ["get_order", "get_customer_history"],
                    "selected_source": "get_customer_history",
                    "resolution_code": "DUPLICATE_RECORD_MERGED",
                }
            )
        payload["duplicated_records"] = duplicated
        payload["episode"] = {"order": episode, "window": window.to_payload()}
        payload["other_episodes"] = len(others)
        payload["entity_resolution"]["confidence"] = 0.95 if "customer" in refs else 0.8
        payload["evidence"] = refs
        return self.reply(message, "resolved", payload, refs.values())

    async def _history(self, ctx: CaseContext, hint: Any) -> Evidence | None:
        if not isinstance(hint, str) or not hint:
            return None
        evidence = await ctx.fetch(self, "get_customer_history", customer_unique_id=hint)
        if evidence is None or not isinstance(evidence.data, dict):
            return None
        return evidence


def _order_row(data: Any, order_id: str) -> bool:
    return isinstance(data, dict) and data.get("order_id") == order_id


def _history_rows(history: Evidence | None) -> list[dict[str, Any]]:
    if history is None:
        return []
    rows = history.data.get("orders")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
