from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator, RefResolver

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

SCHEMAS = Path(__file__).resolve().parents[2] / 'contracts' / 'schemas'
_SCHEMA_CACHE: dict[str, Draft202012Validator] = {}


def _validator(name: str) -> Draft202012Validator:
    if name not in _SCHEMA_CACHE:
        files = {p.name: json.loads(p.read_text(encoding='utf-8')) for p in SCHEMAS.glob('*.schema.json')}
        schema = files[name]
        store = {s['$id']: s for s in files.values()}
        _SCHEMA_CACHE[name] = Draft202012Validator(schema, resolver=RefResolver.from_schema(schema, store=store))
    return _SCHEMA_CACHE[name]


async def _invoke(fn: Any, *args: Any) -> Any:
    result = fn(*args)
    return await result if inspect.isawaitable(result) else result


async def solve_case(case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter) -> dict[str, Any]:
    """Conservative L3B workflow. Adapter interface: gateway.call(domain, {id: value}),
    trace.write(schema-compliant event). Adapt these two calls to the supplied SDK.
    No policy, payment, or shipment fact is inferred from an unverified input.
    """
    case_id = case['case_id']
    if not isinstance(case_id, str) or not __import__('re').fullmatch(r'[A-Z0-9][A-Z0-9_-]{2,63}', case_id):
        raise ValueError('Invalid case_id')

    async def emit(actor: str, kind: str, **details: Any) -> None:
        event = dict(schema_version='day09-trace-event-v1', event_id='evt_' + uuid4().hex,
                     case_id=case_id, event_type=kind, occurred_at=datetime.now(timezone.utc).isoformat(), actor=actor)
        event.update(details)
        _validator('trace-event-v1.schema.json').validate(event)
        await _invoke(trace.write, event)

    await emit('coordinator', 'case_received')
    # The input is only a lead. Every reported identifier must be corroborated by MCP.
    requested = {domain: str(case[key]) for domain, key in (
        ('order', 'order_id'), ('item', 'item_id'), ('payment', 'payment_reference'),
        ('shipment', 'shipment_id')) if isinstance(case.get(key), str) and case[key]}
    evidence: dict[str, dict[str, Any]] = {}
    failures: set[str] = set()

    async def fetch(domain: str, identifier: str) -> dict[str, Any] | None:
        key = f'{domain}:{identifier}'
        if key in evidence:
            return evidence[key]
        if key in failures:
            return None
        for attempt in range(2):
            try:
                response = await asyncio.wait_for(_invoke(gateway.call, domain, {'id': identifier}), timeout=8)
                _validator('mcp-evidence-response-v1.schema.json').validate(response)
                if response['domain'] != domain:
                    raise ValueError('MCP domain mismatch')
                evidence[key] = response
                await emit(domain + '_agent', 'tool_result_consumed', tool_name=domain,
                           evidence_refs=[response['evidence_ref']])
                return response
            except (TimeoutError, ConnectionError, OSError):
                if attempt == 0:
                    await asyncio.sleep(0.2)
            except (ValueError, TypeError, KeyError, __import__('jsonschema').ValidationError):
                break
        failures.add(key)
        return None

    for domain, identifier in requested.items():
        await emit('coordinator', 'task_assigned', target=domain + '_agent')
        await fetch(domain, identifier)
        await emit(domain + '_agent', 'handoff', target='coordinator')

    order = evidence.get('order:' + requested.get('order', ''))
    order_data = order['data'] if order and isinstance(order['data'], dict) else {}
    resolved = bool(order_data.get('order_id') == requested.get('order'))
    # Never follow a foreign order ID or join a payment/shipment from another order.
    scoped: dict[str, dict[str, Any]] = {}
    if resolved:
        scoped['order'] = order
        for domain in ('item', 'payment', 'shipment'):
            env = evidence.get(domain + ':' + requested.get(domain, ''))
            if env and isinstance(env['data'], dict) and env['data'].get('order_id') == requested['order']:
                scoped[domain] = env

    await emit('coordinator', 'handoff', target='policy_agent')
    # Policy queries need a documented policy key from the verified order.
    policy = None
    if resolved and isinstance(order_data.get('policy_id'), str):
        policy = await fetch('policy', order_data['policy_id'])
        if policy:
            scoped['policy'] = policy
    await emit('policy_agent', 'policy_decided', decision_code='NO_ACTION_WITHOUT_VERIFIED_POLICY')
    await emit('policy_agent', 'handoff', target='verifier_agent')

    ids = {'order_ids': [], 'item_ids': [], 'seller_ids': [], 'payment_references': [], 'shipment_ids': []}
    if resolved:
        ids['order_ids'] = [requested['order']]
        for domain, field in [('item', 'item_ids'), ('payment', 'payment_references'), ('shipment', 'shipment_ids')]:
            if domain in scoped:
                ids[field] = [requested[domain]]
        seller = order_data.get('seller_id')
        if isinstance(seller, str) and seller:
            ids['seller_ids'] = [seller]
    refs = list(dict.fromkeys(env['evidence_ref'] for env in scoped.values()))
    output = {
        'schema_version': 'day09-l3b-output-v2', 'case_id': case_id,
        'assessment': {'primary_issue': 'insufficient_evidence', 'secondary_issues': [],
                       'case_status': 'needs_investigation', 'confidence': 0.0},
        'affected_entities': ids,
        'entity_resolution': {'status': 'resolved' if resolved else 'not_found',
                              'resolved_order_ids': ids['order_ids'], 'rejected_candidates': [],
                              'confidence': 1.0 if resolved else 0.0},
        'customer_context': {'customer_unique_id': order_data.get('customer_unique_id')
                             if resolved and isinstance(order_data.get('customer_unique_id'), str) else None,
                             'related_order_ids': []},
        'shipment_analysis': {'verdict': 'insufficient_evidence', 'late_seller_ids': [], 'timeline_complete': False},
        'payment_analysis': {'verdict': 'insufficient_evidence', 'captured_total_brl': None,
                             'refunded_total_brl': None, 'refundable_total_brl': None},
        'root_cause_analysis': {'ranked_causes': [], 'responsible_parties': []},
        'evidence_refs': refs, 'data_conflicts': [],
        'financial_resolution': {'currency': 'BRL', 'recommended_refund_brl': 0, 'refund_lines': []},
        'resolution_actions': ['VERIFY_CASE_EVIDENCE'],
    }
    # Empty refund lines represent no recommendation; never synthesize monetary amounts.
    _validator('l3b-output-v2.schema.json').validate(output)
    if not set(refs).issubset({v['evidence_ref'] for v in evidence.values()}):
        raise ValueError('Cross-case evidence reference')
    if Decimal(str(output['financial_resolution']['recommended_refund_brl'])) != sum(
        (Decimal(str(x['amount_brl'])) for x in output['financial_resolution']['refund_lines']), Decimal(0)
    ):
        raise ValueError('Refund lines do not reconcile')
    await emit('verifier_agent', 'verification_completed', decision_code='SCHEMA_AND_EVIDENCE_VALIDATED')
    await emit('coordinator', 'case_finalized', evidence_refs=refs[:20])
    return output
