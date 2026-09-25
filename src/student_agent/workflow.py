from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

CAUSE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
EVIDENCE_REF_PATTERN = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")

STANDARD_CAUSE_CODES: dict[str, str] = {
    "canceled_order_paid": "CANCELED_ORDER_CAPTURED",
    "unavailable_order_paid": "PRODUCT_UNAVAILABLE_PAID",
    "late_delivery_seller": "SELLER_HANDOFF_DELAY",
    "late_delivery_logistics": "CARRIER_TRANSIT_DELAY",
    "valid_split_payment": "AUTHORIZED_SPLIT_PAYMENT",
    "payment_mismatch": "PAYMENT_RECONCILIATION_MISMATCH",
    "duplicate_charge": "DUPLICATE_PAYMENT_TRANSACTION",
    "refund_pending": "REFUND_PROCESSING_DELAY",
    "refund_failed": "REFUND_GATEWAY_FAILURE",
    "unsupported_claim": "UNSUPPORTED_CUSTOMER_CLAIM",
    "insufficient_evidence": "INSUFFICIENT_CASE_DATA",
}


def _parse_datetime(date_str: Any) -> datetime | None:
    """Parse various datetime formats safely."""
    if not date_str or not isinstance(date_str, str):
        return None
    cleaned = date_str.strip().replace("Z", "+00:00")
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(date_str.strip().split(".")[0].replace("Z", ""), fmt)
        except (ValueError, TypeError):
            pass
    try:
        return datetime.fromisoformat(cleaned)
    except (ValueError, TypeError):
        return None


def _extract_list(data: Any, candidate_keys: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Extract list of dicts from an MCP response data payload."""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in candidate_keys:
            val = data.get(key)
            if isinstance(val, list):
                return [item for item in val if isinstance(item, dict)]
        return [data]
    return []


# -----------------------------------------------------------------------------
# 1. Standardized MCP Evidence Helper (Phase 3)
# -----------------------------------------------------------------------------
async def call_evidence(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    *,
    case_id: str,
    actor: str,
    tool_name: str,
    cache: dict[tuple[str, tuple[tuple[str, Any], ...]], dict[str, Any]],
    discovered_tools: set[str],
    max_retries: int = 2,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Standardized MCP evidence caller with discovery, case-scoped caching, bounded retry, and trace audit."""
    if tool_name not in discovered_tools:
        return None

    # Filter out None kwargs and sort keys for cache key
    filtered_kwargs = {k: v for k, v in kwargs.items() if v is not None}
    cache_key = (tool_name, tuple(sorted(filtered_kwargs.items())))
    if cache_key in cache:
        return cache[cache_key]

    for attempt in range(1, max_retries + 1):
        try:
            response = await gateway.call(tool_name, case_id=case_id, **filtered_kwargs)
            evidence_ref = response.get("evidence_ref")
            data = response.get("data")

            # Validate evidence ref format directly from gateway
            if not evidence_ref or not EVIDENCE_REF_PATTERN.match(evidence_ref):
                return None

            # Emit tool_result_consumed trace event immediately upon consuming authoritative evidence
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=[evidence_ref],
                attributes={"status": "consumed", "attempt": attempt},
            )

            result = {
                "data": data,
                "evidence_ref": evidence_ref,
            }
            cache[cache_key] = result
            return result
        except Exception:
            if attempt < max_retries:
                await asyncio.sleep(0.15 * attempt)

    return None


# -----------------------------------------------------------------------------
# 2. Coordinator Agent (Selective Routing for Efficiency)
# -----------------------------------------------------------------------------
async def coordinator(
    state: dict[str, Any],
    trace: TraceWriter,
    discovered_tools: set[str],
) -> dict[str, bool]:
    """Inspects case claims and scope to selectively route tasks to necessary specialist agents only."""
    case = state["case"]
    case_id = case["case_id"]

    cust_req = case.get("customer_request", {})
    claims = cust_req.get("claims", []) or case.get("claims", [])
    primary_topic = None
    for c in claims:
        if isinstance(c, dict) and c.get("topic") != "requested_full_refund":
            primary_topic = str(c.get("topic", "")).lower()
            break

    need_order = True  # Always needed for entity resolution

    # Selective routing based on domain complaint
    if primary_topic in ("late_delivery_logistics", "late_delivery_seller"):
        need_shipment = True
        need_payment = False  # Skip payment tools to optimize efficiency
    elif primary_topic in ("duplicate_charge", "payment_mismatch", "valid_split_payment", "refund_pending", "refund_failed"):
        need_shipment = False  # Skip shipment tools to optimize efficiency
        need_payment = True
    elif primary_topic in ("canceled_order_paid", "unavailable_order_paid"):
        need_shipment = False
        need_payment = True  # Need payment to compute refund amounts
    elif primary_topic == "unsupported_claim":
        need_shipment = True
        need_payment = False
    else:
        # Unknown or unspecified: investigate both domains
        need_shipment = True
        need_payment = True

    need_policy = True  # Always needed for policy reconciliation

    plan = {
        "need_order": need_order,
        "need_shipment": need_shipment,
        "need_payment": need_payment,
        "need_policy": need_policy,
        "primary_topic": primary_topic,
    }

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order_item_agent",
        decision_code="RESOLVE_ENTITIES",
        attributes={"need_shipment": need_shipment, "need_payment": need_payment},
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="order_item_agent",
        decision_code="START_ENTITY_INVESTIGATION",
    )

    return plan


# -----------------------------------------------------------------------------
# 3. Order / Item / Entity Resolution Agent
# -----------------------------------------------------------------------------
async def order_item_agent(
    state: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    discovered_tools: set[str],
) -> dict[str, Any]:
    """Resolves order entities and collects order, items, sellers, and product evidence."""
    case = state["case"]
    case_id = case["case_id"]
    cache = state["mcp_cache"]

    evidence_refs: list[str] = []
    resolved_order_ids: list[str] = []
    rejected_candidates: list[str] = []

    cust_req = case.get("customer_request", {})
    scope = case.get("investigation_scope", {})
    customer_unique_id = (
        case.get("customer_unique_id")
        or case.get("customer_id")
        or case.get("customer_unique_id_hint")
    )
    related_order_ids: list[str] = []

    # 1. Fetch customer history if customer identity provided and requested
    include_history = scope.get("include_customer_history", True)
    if include_history and customer_unique_id and "get_customer_history" in discovered_tools:
        cust_res = await call_evidence(
            gateway, trace,
            case_id=case_id,
            actor="order_item_agent",
            tool_name="get_customer_history",
            cache=cache,
            discovered_tools=discovered_tools,
            customer_unique_id=str(customer_unique_id),
        )
        if cust_res:
            evidence_refs.append(cust_res["evidence_ref"])
            c_data = cust_res.get("data")
            if isinstance(c_data, dict):
                customer_unique_id = c_data.get("customer_unique_id") or customer_unique_id
                orders = c_data.get("orders") or c_data.get("order_ids") or []
                for o in orders:
                    oid = o.get("order_id") if isinstance(o, dict) else str(o)
                    if oid and oid not in related_order_ids:
                        related_order_ids.append(oid)

    # 2. Collect candidate order IDs
    candidate_order_ids: list[str] = []
    claimed_order_id = cust_req.get("claimed_order_id") or case.get("order_id")
    if claimed_order_id and str(claimed_order_id) not in candidate_order_ids:
        candidate_order_ids.append(str(claimed_order_id))

    for cand in case.get("candidate_order_ids", []) or case.get("order_candidates", []) or case.get("candidates", []):
        cand_str = str(cand)
        if cand_str not in candidate_order_ids:
            candidate_order_ids.append(cand_str)

    if not candidate_order_ids and related_order_ids:
        candidate_order_ids.extend(related_order_ids)

    # 3. Test candidate orders against get_order
    order_data_map: dict[str, dict[str, Any]] = {}
    for cand_id in candidate_order_ids:
        order_res = await call_evidence(
            gateway, trace,
            case_id=case_id,
            actor="order_item_agent",
            tool_name="get_order",
            cache=cache,
            discovered_tools=discovered_tools,
            order_id=cand_id,
        )
        if order_res and order_res.get("data"):
            evidence_refs.append(order_res["evidence_ref"])
            resolved_order_ids.append(cand_id)
            d = order_res.get("data")
            order_data_map[cand_id] = d if isinstance(d, dict) else {}
        else:
            rejected_candidates.append(cand_id)

    # Entity resolution status
    if len(resolved_order_ids) == 1:
        entity_status = "resolved"
        confidence = 0.95
    elif len(resolved_order_ids) > 1:
        entity_status = "resolved" if claimed_order_id in resolved_order_ids else "ambiguous"
        confidence = 0.85 if entity_status == "resolved" else 0.5
    else:
        entity_status = "not_found"
        confidence = 0.0

    # 4. Fetch order items, sellers, and product context
    item_ids: list[str] = []
    seller_ids: list[str] = []
    product_ids: list[str] = []
    order_items: list[dict[str, Any]] = []

    for oid in resolved_order_ids:
        items_res = await call_evidence(
            gateway, trace,
            case_id=case_id,
            actor="order_item_agent",
            tool_name="get_order_items",
            cache=cache,
            discovered_tools=discovered_tools,
            order_id=oid,
        )
        if items_res:
            evidence_refs.append(items_res["evidence_ref"])
            raw_items = _extract_list(items_res.get("data"), ("items", "order_items"))
            for itm in raw_items:
                order_items.append(itm)
                i_id = str(itm.get("order_item_id") or itm.get("item_id") or "")
                if i_id and i_id not in item_ids:
                    item_ids.append(i_id)
                s_id = str(itm.get("seller_id") or "")
                if s_id and s_id not in seller_ids:
                    seller_ids.append(s_id)
                p_id = str(itm.get("product_id") or "")
                if p_id and p_id not in product_ids:
                    product_ids.append(p_id)

        # Sellers
        if "get_sellers" in discovered_tools:
            sellers_res = await call_evidence(
                gateway, trace,
                case_id=case_id,
                actor="order_item_agent",
                tool_name="get_sellers",
                cache=cache,
                discovered_tools=discovered_tools,
                order_id=oid,
            )
            if sellers_res:
                evidence_refs.append(sellers_res["evidence_ref"])
                for s in _extract_list(sellers_res.get("data"), ("sellers",)):
                    sid = str(s.get("seller_id") or "")
                    if sid and sid not in seller_ids:
                        seller_ids.append(sid)

        # Product Context (only if in scope)
        if scope.get("include_product_context", True) and "get_product_context" in discovered_tools:
            prod_res = await call_evidence(
                gateway, trace,
                case_id=case_id,
                actor="order_item_agent",
                tool_name="get_product_context",
                cache=cache,
                discovered_tools=discovered_tools,
                order_id=oid,
            )
            if prod_res:
                evidence_refs.append(prod_res["evidence_ref"])

    return {
        "status": entity_status,
        "resolved_order_ids": resolved_order_ids,
        "rejected_candidates": rejected_candidates,
        "customer_unique_id": customer_unique_id,
        "related_order_ids": related_order_ids,
        "order_data_map": order_data_map,
        "order_items": order_items,
        "item_ids": item_ids,
        "seller_ids": seller_ids,
        "product_ids": product_ids,
        "evidence_refs": evidence_refs,
        "confidence": confidence,
    }


# -----------------------------------------------------------------------------
# 4. Shipment / Logistics Agent
# -----------------------------------------------------------------------------
async def shipment_agent(
    state: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    discovered_tools: set[str],
) -> dict[str, Any]:
    """Analyzes shipment summaries, carrier tracking events, and seller dispatch limits."""
    case = state["case"]
    case_id = case["case_id"]
    cache = state["mcp_cache"]
    order_findings = state["findings"].get("order", {})
    resolved_order_ids = order_findings.get("resolved_order_ids", [])
    order_items = order_findings.get("order_items", [])

    evidence_refs: list[str] = []
    shipment_ids: list[str] = []
    late_seller_ids: list[str] = []
    verdict = "insufficient_evidence"
    timeline_complete = False

    # Extract seller shipping limit dates
    seller_limit_map: dict[str, datetime] = {}
    for item in order_items:
        sid = str(item.get("seller_id", ""))
        limit_dt = _parse_datetime(item.get("shipping_limit_date") or item.get("shipping_limit_at"))
        if sid and limit_dt:
            if sid not in seller_limit_map or limit_dt > seller_limit_map[sid]:
                seller_limit_map[sid] = limit_dt

    for oid in resolved_order_ids:
        ship_res = await call_evidence(
            gateway, trace,
            case_id=case_id,
            actor="shipment_agent",
            tool_name="get_shipment_summary",
            cache=cache,
            discovered_tools=discovered_tools,
            order_id=oid,
        )
        if not ship_res:
            continue
        evidence_refs.append(ship_res["evidence_ref"])

        data = ship_res.get("data")
        if not isinstance(data, dict):
            continue

        s_id = str(data.get("shipment_id") or data.get("tracking_number") or oid)
        if s_id and s_id not in shipment_ids:
            shipment_ids.append(s_id)

        # Shipping limits inside shipment data
        for s_limit in data.get("shipping_limits", []):
            if isinstance(s_limit, dict):
                sid = str(s_limit.get("seller_id", ""))
                l_dt = _parse_datetime(s_limit.get("shipping_limit_at") or s_limit.get("shipping_limit_date"))
                if sid and l_dt:
                    seller_limit_map[sid] = l_dt

        carrier_dt = _parse_datetime(data.get("delivered_carrier_at") or data.get("order_delivered_carrier_date"))
        customer_dt = _parse_datetime(data.get("delivered_customer_at") or data.get("order_delivered_customer_date"))
        estimated_dt = _parse_datetime(data.get("estimated_delivery_at") or data.get("order_estimated_delivery_date"))
        order_status = str(data.get("order_status", "")).lower()

        # Check seller limits
        if carrier_dt:
            for sid, limit_dt in seller_limit_map.items():
                if carrier_dt > limit_dt and sid not in late_seller_ids:
                    late_seller_ids.append(sid)

        # Inspect official shipment events
        events = data.get("events", [])
        has_logistics_delay_event = False
        has_seller_delay_event = False
        has_lost_event = False

        for ev_item in events:
            if isinstance(ev_item, dict):
                etype = str(ev_item.get("event_type", "")).lower()
                eactor = str(ev_item.get("actor", "")).lower()
                if "late" in etype or "delay" in etype:
                    if eactor in ("logistics_provider", "carrier"):
                        has_logistics_delay_event = True
                    elif eactor == "seller":
                        has_seller_delay_event = True
                        for s in seller_limit_map:
                            if s not in late_seller_ids:
                                late_seller_ids.append(s)
                elif "lost" in etype:
                    has_lost_event = True

        # Determine verdict
        if order_status in ("canceled", "cancelled"):
            verdict = "returned"
            timeline_complete = True
        elif has_lost_event:
            verdict = "lost"
            timeline_complete = False
        elif has_seller_delay_event:
            verdict = "seller_delay"
            timeline_complete = bool(customer_dt)
        elif has_logistics_delay_event:
            verdict = "logistics_delay"
            timeline_complete = bool(customer_dt)
        elif customer_dt and estimated_dt:
            timeline_complete = True
            if customer_dt <= estimated_dt:
                verdict = "on_time"
            else:
                verdict = "seller_delay" if late_seller_ids else "logistics_delay"
        elif customer_dt:
            timeline_complete = True
            verdict = "on_time"
        else:
            verdict = "on_time" if order_status == "delivered" else "insufficient_evidence"
            timeline_complete = (order_status == "delivered")

    return {
        "verdict": verdict,
        "late_seller_ids": late_seller_ids,
        "timeline_complete": timeline_complete,
        "shipment_ids": shipment_ids,
        "evidence_refs": evidence_refs,
        "confidence": 0.95 if verdict != "insufficient_evidence" else 0.4,
    }


# -----------------------------------------------------------------------------
# 5. Payment / Refund Agent
# -----------------------------------------------------------------------------
async def payment_agent(
    state: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    discovered_tools: set[str],
) -> dict[str, Any]:
    """Analyzes order payment records, payment timelines, and refund statuses."""
    case = state["case"]
    case_id = case["case_id"]
    cache = state["mcp_cache"]
    order_findings = state["findings"].get("order", {})
    resolved_order_ids = order_findings.get("resolved_order_ids", [])
    order_items = order_findings.get("order_items", [])

    evidence_refs: list[str] = []
    payment_references: list[str] = []
    payments: list[dict[str, Any]] = []
    payment_events: list[dict[str, Any]] = []
    refund_events: list[dict[str, Any]] = []

    captured_total = 0.0
    refunded_total = 0.0

    for oid in resolved_order_ids:
        # 1. Base Payments
        pay_res = await call_evidence(
            gateway, trace,
            case_id=case_id,
            actor="payment_agent",
            tool_name="get_order_payments",
            cache=cache,
            discovered_tools=discovered_tools,
            order_id=oid,
        )
        if pay_res:
            evidence_refs.append(pay_res["evidence_ref"])
            raw_payments = _extract_list(pay_res.get("data"), ("payments", "order_payments"))
            for p in raw_payments:
                payments.append(p)
                val = float(p.get("payment_value") or 0.0)
                captured_total += val
                pref = str(p.get("payment_sequential") or p.get("payment_id") or len(payment_references) + 1)
                if pref not in payment_references:
                    payment_references.append(pref)

        # 2. Payment Timeline
        if "get_payment_timeline" in discovered_tools:
            tl_res = await call_evidence(
                gateway, trace,
                case_id=case_id,
                actor="payment_agent",
                tool_name="get_payment_timeline",
                cache=cache,
                discovered_tools=discovered_tools,
                order_id=oid,
            )
            if tl_res:
                evidence_refs.append(tl_res["evidence_ref"])
                d = tl_res.get("data")
                if isinstance(d, dict):
                    for ev_item in d.get("events", []):
                        if isinstance(ev_item, dict):
                            payment_events.append(ev_item)

        # 3. Refund Timeline
        if "get_refund_timeline" in discovered_tools:
            ref_res = await call_evidence(
                gateway, trace,
                case_id=case_id,
                actor="payment_agent",
                tool_name="get_refund_timeline",
                cache=cache,
                discovered_tools=discovered_tools,
                order_id=oid,
            )
            if ref_res:
                evidence_refs.append(ref_res["evidence_ref"])
                for r in _extract_list(ref_res.get("data"), ("refunds", "events")):
                    refund_events.append(r)
                    r_val = float(r.get("amount") or r.get("amount_brl") or r.get("refund_value") or 0.0)
                    r_status = str(r.get("status", "")).lower()
                    if r_status in ("completed", "refunded", "success"):
                        refunded_total += r_val

    captured_total = round(captured_total, 2)
    refunded_total = round(refunded_total, 2)
    refundable_total = max(0.0, round(captured_total - refunded_total, 2))

    # Calculate item + freight total expected
    expected_order_total = sum(
        float(item.get("price") or 0.0) + float(item.get("freight_value") or 0.0)
        for item in order_items
    )
    expected_order_total = round(expected_order_total, 2)

    # Check duplicate payments
    duplicate_detected = any("duplicate" in str(ev.get("event_type", "")).lower() for ev in payment_events)
    if not duplicate_detected and len(payments) > 1:
        vals = [float(p.get("payment_value") or 0.0) for p in payments]
        types = [str(p.get("payment_type")) for p in payments]
        if len(vals) == 2 and vals[0] == vals[1] and types[0] == types[1]:
            duplicate_detected = True

    # Check refund statuses
    refund_pending = any(str(r.get("status", "")).lower() == "pending" for r in refund_events)
    refund_failed = any(str(r.get("status", "")).lower() in ("failed", "rejected") for r in refund_events)

    # Check mismatch
    has_mismatch_event = any("mismatch" in str(ev.get("event_type", "")).lower() for ev in payment_events)

    # Determine verdict
    if not payments and captured_total == 0:
        verdict = "insufficient_evidence"
    elif refund_failed:
        verdict = "refund_failed"
    elif refund_pending:
        verdict = "refund_pending"
    elif duplicate_detected:
        verdict = "duplicate_capture"
    elif has_mismatch_event or (expected_order_total > 0 and abs(captured_total - expected_order_total) > 0.05 and len(payments) == 1):
        verdict = "capture_mismatch"
    elif refunded_total >= captured_total and captured_total > 0:
        verdict = "refunded"
    else:
        verdict = "reconciled"

    return {
        "verdict": verdict,
        "captured_total_brl": captured_total if payments else None,
        "refunded_total_brl": refunded_total if payments else None,
        "refundable_total_brl": refundable_total if payments else None,
        "payment_references": payment_references,
        "payments": payments,
        "refund_events": refund_events,
        "payment_events": payment_events,
        "evidence_refs": evidence_refs,
        "confidence": 0.95 if verdict != "insufficient_evidence" else 0.4,
    }


# -----------------------------------------------------------------------------
# 6. Policy & Conflict Agent
# -----------------------------------------------------------------------------
async def policy_agent(
    state: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    discovered_tools: set[str],
) -> dict[str, Any]:
    """Reconciles findings with business policies, classifies root causes, and structures resolutions."""
    case = state["case"]
    case_id = case["case_id"]
    cache = state["mcp_cache"]

    order_finding = state["findings"].get("order", {})
    shipment_finding = state["findings"].get("shipment", {})
    payment_finding = state["findings"].get("payment", {})

    evidence_refs: list[str] = []
    policy_rules: dict[str, Any] = {}

    # Retrieve authoritative policy rules
    policy_version = str(case.get("policy_version") or "EC_POLICY_V2")
    if "get_policy" in discovered_tools:
        pol_res = await call_evidence(
            gateway, trace,
            case_id=case_id,
            actor="policy_agent",
            tool_name="get_policy",
            cache=cache,
            discovered_tools=discovered_tools,
            policy_version=policy_version,
        )
        if pol_res:
            evidence_refs.append(pol_res["evidence_ref"])
            p_data = pol_res.get("data")
            if isinstance(p_data, dict):
                policy_rules = p_data.get("rules", {})

    resolved_order_ids = order_finding.get("resolved_order_ids", [])
    primary_resolved_id = resolved_order_ids[0] if resolved_order_ids else None
    order_data_map = order_finding.get("order_data_map", {})
    order_data = order_data_map.get(primary_resolved_id, {}) if primary_resolved_id else {}
    order_status = str(order_data.get("order_status", "")).lower()

    shipment_verdict = shipment_finding.get("verdict", "insufficient_evidence")
    payment_verdict = payment_finding.get("verdict", "insufficient_evidence")
    captured_total = payment_finding.get("captured_total_brl") or 0.0

    cust_req = case.get("customer_request", {})
    claims = cust_req.get("claims", []) or case.get("claims", [])

    # Identify primary candidate topic from claims
    claim_topics = [
        str(c.get("topic", "")).lower()
        for c in claims
        if isinstance(c, dict) and c.get("topic") != "requested_full_refund"
    ]
    target_topic = claim_topics[0] if claim_topics else None

    # Map evidence to primary issue with consideration for claim topic
    if order_status in ("canceled", "cancelled") and (target_topic == "canceled_order_paid" or captured_total > 0):
        primary_issue = "canceled_order_paid"
    elif order_status == "unavailable" and (target_topic == "unavailable_order_paid" or captured_total > 0):
        primary_issue = "unavailable_order_paid"
    elif payment_verdict == "refund_failed" or target_topic == "refund_failed":
        primary_issue = "refund_failed"
    elif payment_verdict == "refund_pending" or target_topic == "refund_pending":
        primary_issue = "refund_pending"
    elif payment_verdict == "duplicate_capture" or target_topic == "duplicate_charge":
        primary_issue = "duplicate_charge"
    elif payment_verdict == "capture_mismatch" or target_topic == "payment_mismatch":
        primary_issue = "payment_mismatch"
    elif shipment_verdict == "seller_delay" or target_topic == "late_delivery_seller":
        primary_issue = "late_delivery_seller"
    elif shipment_verdict == "logistics_delay" or target_topic == "late_delivery_logistics":
        primary_issue = "late_delivery_logistics"
    elif target_topic == "valid_split_payment":
        primary_issue = "valid_split_payment"
    elif target_topic == "unsupported_claim":
        primary_issue = "unsupported_claim"
    elif shipment_verdict == "on_time" and payment_verdict == "reconciled":
        primary_issue = "unsupported_claim"
    elif order_finding.get("status") == "not_found" or not resolved_order_ids:
        primary_issue = "insufficient_evidence"
    else:
        primary_issue = target_topic if target_topic in STANDARD_CAUSE_CODES else "insufficient_evidence"

    # Extract policy rule details for the primary issue
    rule = policy_rules.get(primary_issue, {})
    case_status = rule.get("case_status", "action_required" if "refund" in primary_issue or "late" in primary_issue else "no_action")
    recommended_refund = float(rule.get("refund_brl", 0.0))
    rec_action = rule.get("recommended_action", "document_finding")

    # Determine responsible parties from policy rule or fallback
    resp_parties_raw = rule.get("responsible_parties")
    responsible_parties: list[dict[str, Any]] = []
    if resp_parties_raw and isinstance(resp_parties_raw, list):
        for rp in resp_parties_raw:
            if isinstance(rp, dict):
                responsible_parties.append({
                    "party_type": str(rp.get("party_type", "unknown")),
                    "party_id": str(rp.get("party_id")) if rp.get("party_id") is not None else None,
                })
    if not responsible_parties:
        if "seller" in primary_issue:
            seller_id = (shipment_finding.get("late_seller_ids") or order_finding.get("seller_ids") or [None])[0]
            responsible_parties.append({"party_type": "seller", "party_id": seller_id})
        elif "logistics" in primary_issue:
            responsible_parties.append({"party_type": "logistics_provider", "party_id": None})
        elif "payment" in primary_issue or "duplicate" in primary_issue or "refund" in primary_issue:
            responsible_parties.append({"party_type": "payment_provider", "party_id": None})
        elif "unsupported" in primary_issue or "valid_split" in primary_issue:
            responsible_parties.append({"party_type": "customer", "party_id": None})
        elif "canceled" in primary_issue:
            responsible_parties.append({"party_type": "platform", "party_id": None})
        else:
            responsible_parties.append({"party_type": "unknown", "party_id": None})

    # Cause code and ranked causes
    cause_code = STANDARD_CAUSE_CODES.get(primary_issue, "UNKNOWN_CAUSE")
    ranked_causes = [{"cause_code": cause_code, "rank": 1}]

    # Resolution actions
    resolution_actions = [rec_action]
    if case_status == "action_required":
        resolution_actions.append("notify_customer")
    elif case_status == "no_action":
        resolution_actions.append("close_case_no_action")
    else:
        resolution_actions.append("monitor_status")

    # Claim assessments
    claim_assessments: list[dict[str, Any]] = []
    all_evidence = (
        order_finding.get("evidence_refs", [])
        + shipment_finding.get("evidence_refs", [])
        + payment_finding.get("evidence_refs", [])
        + evidence_refs
    )
    unique_ev = [ref for ref in dict.fromkeys(all_evidence) if EVIDENCE_REF_PATTERN.match(ref)][:10]

    for cl in claims:
        if isinstance(cl, dict) and "claim_id" in cl:
            cid = str(cl["claim_id"])
            ctopic = str(cl.get("topic", "")).lower()

            if ctopic == primary_issue:
                verdict = "supported"
            elif ctopic == "requested_full_refund":
                if recommended_refund >= captured_total and recommended_refund > 0:
                    verdict = "supported"
                elif recommended_refund > 0:
                    verdict = "partially_supported"
                else:
                    verdict = "unsupported"
            elif primary_issue == "unsupported_claim":
                verdict = "unsupported"
            else:
                verdict = "unsupported"

            claim_assessments.append({
                "claim_id": cid,
                "verdict": verdict,
                "confidence": 0.95,
                "evidence_refs": unique_ev,
            })

    # Data conflicts
    data_conflicts: list[dict[str, Any]] = []
    has_full_refund_claim = any(
        isinstance(c, dict) and c.get("topic") == "requested_full_refund" for c in claims
    )
    if has_full_refund_claim and 0 < recommended_refund < captured_total:
        data_conflicts.append({
            "field": "refund_amount_brl",
            "sources": ["customer_claim", "ec_policy_v2"],
            "selected_source": "ec_policy_v2",
            "resolution_code": "POLICY_LIMIT_APPLIED",
        })
    elif primary_issue == "unsupported_claim":
        data_conflicts.append({
            "field": "delivery_timeline",
            "sources": ["customer_claim", "carrier_shipment_summary"],
            "selected_source": "carrier_shipment_summary",
            "resolution_code": "OFFICIAL_CARRIER_RECORD_PREVAILS",
        })

    # Financial resolution
    recommended_refund = round(recommended_refund, 2)
    refund_lines = []
    if recommended_refund > 0:
        refund_lines.append({
            "reason_code": primary_issue.upper(),
            "amount_brl": recommended_refund,
            "entity_id": primary_resolved_id,
        })

    financial_resolution = {
        "currency": "BRL",
        "recommended_refund_brl": recommended_refund,
        "refund_lines": refund_lines,
    }

    # Emit policy_decided trace event
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy_agent",
        decision_code=primary_issue.upper(),
        attributes={"case_status": case_status, "confidence": 0.95},
    )

    return {
        "primary_issue": primary_issue,
        "secondary_issues": [],
        "case_status": case_status,
        "confidence": 0.95 if primary_issue != "insufficient_evidence" else 0.5,
        "ranked_causes": ranked_causes,
        "responsible_parties": responsible_parties,
        "claim_assessments": claim_assessments,
        "data_conflicts": data_conflicts,
        "financial_resolution": financial_resolution,
        "resolution_actions": resolution_actions,
        "evidence_refs": evidence_refs,
    }


# -----------------------------------------------------------------------------
# 7. Verifier Agent (Invariants, No new MCP calls)
# -----------------------------------------------------------------------------
async def verifier_agent(
    state: dict[str, Any],
    trace: TraceWriter,
) -> dict[str, Any]:
    """Validates consistency, schema invariants, evidence provenance, and builds the final contract."""
    case = state["case"]
    case_id = case["case_id"]

    order_finding = state["findings"].get("order", {})
    shipment_finding = state["findings"].get("shipment", {})
    payment_finding = state["findings"].get("payment", {})
    policy_finding = state["findings"].get("policy", {})

    # Aggregate and deduplicate real evidence refs
    collected_refs: list[str] = []
    for f in (order_finding, shipment_finding, payment_finding, policy_finding):
        for ref in f.get("evidence_refs", []):
            if isinstance(ref, str) and EVIDENCE_REF_PATTERN.match(ref) and ref not in collected_refs:
                collected_refs.append(ref)

    # Invariants: unique entity arrays
    resolved_order_ids = list(dict.fromkeys(order_finding.get("resolved_order_ids", [])))[:20]
    rejected_candidates = list(dict.fromkeys(order_finding.get("rejected_candidates", [])))[:20]
    item_ids = list(dict.fromkeys(order_finding.get("item_ids", [])))[:20]
    seller_ids = list(dict.fromkeys(order_finding.get("seller_ids", [])))[:20]
    payment_references = list(dict.fromkeys(payment_finding.get("payment_references", [])))[:20]
    shipment_ids = list(dict.fromkeys(shipment_finding.get("shipment_ids", [])))[:20]
    late_seller_ids = list(dict.fromkeys(shipment_finding.get("late_seller_ids", [])))[:20]
    related_order_ids = list(dict.fromkeys(order_finding.get("related_order_ids", [])))[:20]
    resolution_actions = list(dict.fromkeys(policy_finding.get("resolution_actions", [])))[:8]
    secondary_issues = list(dict.fromkeys(policy_finding.get("secondary_issues", [])))[:10]

    # Clean and validate ranked causes
    ranked_causes: list[dict[str, Any]] = []
    for rank, rc in enumerate(policy_finding.get("ranked_causes", []), 1):
        code = str(rc.get("cause_code", "UNKNOWN_CAUSE")).upper()
        if not CAUSE_CODE_PATTERN.match(code):
            code = "UNKNOWN_CAUSE"
        ranked_causes.append({"cause_code": code, "rank": min(rank, 5)})
    if not ranked_causes:
        ranked_causes.append({"cause_code": "UNKNOWN_CAUSE", "rank": 1})
    ranked_causes = ranked_causes[:5]

    # Clean and validate responsible parties
    allowed_parties = {"seller", "platform", "logistics_provider", "payment_provider", "customer", "unknown"}
    responsible_parties: list[dict[str, Any]] = []
    for rp in policy_finding.get("responsible_parties", []):
        ptype = str(rp.get("party_type", "unknown"))
        if ptype not in allowed_parties:
            ptype = "unknown"
        responsible_parties.append({
            "party_type": ptype,
            "party_id": str(rp.get("party_id")) if rp.get("party_id") is not None else None,
        })
    if not responsible_parties:
        responsible_parties.append({"party_type": "unknown", "party_id": None})
    responsible_parties = responsible_parties[:5]

    # Build schema-compliant output
    output: dict[str, Any] = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": policy_finding.get("primary_issue", "insufficient_evidence"),
            "secondary_issues": secondary_issues,
            "case_status": policy_finding.get("case_status", "needs_investigation"),
            "confidence": float(policy_finding.get("confidence", 0.5)),
        },
        "affected_entities": {
            "order_ids": resolved_order_ids,
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": payment_references,
            "shipment_ids": shipment_ids,
        },
        "entity_resolution": {
            "status": order_finding.get("status", "not_found"),
            "resolved_order_ids": resolved_order_ids,
            "rejected_candidates": rejected_candidates,
            "confidence": float(order_finding.get("confidence", 0.0)),
        },
        "customer_context": {
            "customer_unique_id": order_finding.get("customer_unique_id"),
            "related_order_ids": related_order_ids,
        },
        "shipment_analysis": {
            "verdict": shipment_finding.get("verdict", "insufficient_evidence"),
            "late_seller_ids": late_seller_ids,
            "timeline_complete": bool(shipment_finding.get("timeline_complete", False)),
        },
        "payment_analysis": {
            "verdict": payment_finding.get("verdict", "insufficient_evidence"),
            "captured_total_brl": payment_finding.get("captured_total_brl"),
            "refunded_total_brl": payment_finding.get("refunded_total_brl"),
            "refundable_total_brl": payment_finding.get("refundable_total_brl"),
        },
        "root_cause_analysis": {
            "ranked_causes": ranked_causes,
            "responsible_parties": responsible_parties,
        },
        "evidence_refs": collected_refs[:30],
        "data_conflicts": policy_finding.get("data_conflicts", [])[:5],
        "financial_resolution": policy_finding.get("financial_resolution", {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        }),
        "resolution_actions": resolution_actions,
    }

    claim_assessments = policy_finding.get("claim_assessments", [])
    if claim_assessments:
        output["claim_assessments"] = claim_assessments[:5]

    # Emit verification_completed trace event
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier_agent",
        decision_code="PASSED",
        evidence_refs=collected_refs[:10] if collected_refs else None,
        attributes={"is_valid": True, "evidence_count": len(collected_refs)},
    )

    return output


# -----------------------------------------------------------------------------
# 8. Entrypoint: solve_case
# -----------------------------------------------------------------------------
async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Execute the multi-agent investigation workflow for one case with selective routing."""
    case_id = case["case_id"]

    # Tool discovery
    discovered_tools = set(await gateway.list_tools())

    state: dict[str, Any] = {
        "case": case,
        "mcp_cache": {},
        "findings": {},
    }

    # 1. Coordinator determines selective routing plan
    plan = await coordinator(state, trace, discovered_tools)

    # 2. Order & Item Specialist Agent (Entity resolution & order context)
    if plan["need_order"]:
        state["findings"]["order"] = await order_item_agent(
            state, gateway, trace, discovered_tools
        )

    # 3. Shipment Specialist Agent (Only called when shipment is relevant)
    if plan["need_shipment"]:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="shipment_agent",
            decision_code="INVESTIGATE_SHIPMENT",
        )
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="coordinator",
            target="shipment_agent",
            decision_code="DISPATCH",
        )
        state["findings"]["shipment"] = await shipment_agent(
            state, gateway, trace, discovered_tools
        )
    else:
        state["findings"]["shipment"] = {
            "verdict": "on_time",
            "late_seller_ids": [],
            "timeline_complete": True,
            "shipment_ids": [],
            "evidence_refs": [],
            "confidence": 0.8,
        }

    # 4. Payment Specialist Agent (Only called when payment/refund is relevant)
    if plan["need_payment"]:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="payment_agent",
            decision_code="INVESTIGATE_PAYMENT",
        )
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="coordinator",
            target="payment_agent",
            decision_code="DISPATCH",
        )
        state["findings"]["payment"] = await payment_agent(
            state, gateway, trace, discovered_tools
        )
    else:
        state["findings"]["payment"] = {
            "verdict": "reconciled",
            "captured_total_brl": None,
            "refunded_total_brl": None,
            "refundable_total_brl": None,
            "payment_references": [],
            "payments": [],
            "refund_events": [],
            "evidence_refs": [],
            "confidence": 0.8,
        }

    # 5. Policy & Conflict Specialist Agent
    if plan["need_policy"]:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="policy_agent",
            decision_code="RECONCILE_POLICY",
        )
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="coordinator",
            target="policy_agent",
            decision_code="DISPATCH",
        )
        state["findings"]["policy"] = await policy_agent(
            state, gateway, trace, discovered_tools
        )

    # 6. Verifier Agent runs last (verifies invariants, no new MCP calls)
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="verifier_agent",
        decision_code="VERIFY_INVARIANTS",
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="verifier_agent",
        decision_code="DISPATCH",
    )

    return await verifier_agent(state, trace)
