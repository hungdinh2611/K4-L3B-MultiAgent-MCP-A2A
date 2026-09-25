from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.a2a import (
    A2AMessage,
    Agent,
    CaseContext,
    ProtocolViolation,
    ToolPermissionError,
)
from student_agent.agents import AGENT_TYPES
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import ORDER_SPECIALISTS, Coordinator

ROOT = Path(__file__).resolve().parents[1]
CASE = {"case_id": "L3B_CASE_TEST"}
ALL_TOOLS = {
    "get_order", "get_order_items", "get_order_payments", "get_shipment_summary", "get_sellers",
    "get_policy", "get_customer_history", "get_product_context", "get_payment_timeline",
    "get_refund_timeline",
}  # fmt: skip


class FakeGateway:
    def __init__(self, failures: list[BaseException] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.failures = list(failures or [])

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, {"case_id": case_id, **arguments}))
        await asyncio.sleep(0)
        if self.failures:
            raise self.failures.pop(0)
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{tool_name}_{len(self.calls):02d}_abcdefghijkl",
            "result_hash": "sha256:" + "0" * 64,
            "domain": "order",
            "data": {"tool": tool_name, **arguments},
        }


class Probe(Agent):
    actor = "entity-agent"
    tools = frozenset({"get_order"})


def make_context(tmp_path: Path, gateway: FakeGateway, **options: Any) -> CaseContext:
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    return CaseContext(CASE, gateway, trace, **options)  # type: ignore[arg-type]


def events(tmp_path: Path) -> list[dict[str, Any]]:
    lines = (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_every_tool_is_owned_by_exactly_one_agent() -> None:
    owned = [tool for agent_type in AGENT_TYPES for tool in agent_type.tools]
    assert sorted(owned) == sorted(ALL_TOOLS)


def test_fetch_enforces_least_privilege(tmp_path: Path) -> None:
    ctx = make_context(tmp_path, FakeGateway())
    with pytest.raises(ToolPermissionError):
        asyncio.run(ctx.fetch(Probe(), "get_order_payments", order_id="o1"))
    assert ctx.calls_made == 0


def test_fetch_scopes_case_and_caches_concurrent_duplicates(tmp_path: Path) -> None:
    gateway = FakeGateway()
    ctx = make_context(tmp_path, gateway)

    async def twice() -> list[Any]:
        return await asyncio.gather(
            ctx.fetch(Probe(), "get_order", order_id="o1"),
            ctx.fetch(Probe(), "get_order", order_id="o1"),
        )

    first, second = asyncio.run(twice())
    assert first is second
    assert gateway.calls == [("get_order", {"case_id": "L3B_CASE_TEST", "order_id": "o1"})]
    consumed = [e for e in events(tmp_path) if e["event_type"] == "tool_result_consumed"]
    assert [event["evidence_refs"] for event in consumed] == [[first.ref]]


def test_fetch_retries_transient_errors_within_budget(tmp_path: Path) -> None:
    gateway = FakeGateway(failures=[TimeoutError()])
    ctx = make_context(tmp_path, gateway)
    evidence = asyncio.run(ctx.fetch(Probe(), "get_order", order_id="o1"))
    assert evidence is not None and ctx.calls_made == 2 and ctx.failures == []


def test_fetch_does_not_retry_tool_errors_or_exceed_budget(tmp_path: Path) -> None:
    gateway = FakeGateway(failures=[RuntimeError("not found"), TimeoutError(), TimeoutError()])
    ctx = make_context(tmp_path, gateway, call_budget=2)
    assert asyncio.run(ctx.fetch(Probe(), "get_order", order_id="missing")) is None
    assert asyncio.run(ctx.fetch(Probe(), "get_order", order_id="slow")) is None
    assert [failure.code for failure in ctx.failures] == ["tool_error", "budget_exhausted"]
    assert ctx.calls_made == 2


class Specialist(Agent):
    def __init__(self, actor: str, tool: str | None = None) -> None:
        self.actor = actor  # type: ignore[misc]
        self.tools = frozenset({tool} if tool else set())  # type: ignore[misc]
        self.tool = tool

    async def handle(self, ctx: CaseContext, message: A2AMessage) -> A2AMessage:
        refs: list[str] = []
        if self.tool:
            evidence = await ctx.fetch(self, self.tool, order_id="o1")
            refs = [evidence.ref] if evidence else []
        payload = {"order_id": "o1"} if self.actor == "entity-agent" else {}
        return self.reply(message, "findings", payload, refs)


class Chain(Agent):
    def __init__(self, actor: str, next_actor: str, intent: str) -> None:
        self.actor = actor  # type: ignore[misc]
        self.next_actor = next_actor
        self.intent = intent

    async def handle(self, ctx: CaseContext, message: A2AMessage) -> A2AMessage:
        payload = dict(message.payload)
        if self.intent == "finalize":
            payload["output"] = {"case_id": ctx.case_id}
        return self.reply(message, self.intent, payload, message.evidence_refs, self.next_actor)


def chain_agents() -> dict[str, Agent]:
    agents: dict[str, Agent] = {
        "entity-agent": Specialist("entity-agent", "get_order"),
        **{name: Specialist(name, "get_order_items") for name in ORDER_SPECIALISTS},
        "conflict-agent": Chain("conflict-agent", "policy-agent", "conflicts_resolved"),
        "policy-agent": Chain("policy-agent", "verifier-agent", "policy_draft"),
        "verifier-agent": Chain("verifier-agent", "coordinator", "finalize"),
    }
    return agents


def test_coordinator_traces_assignments_and_handoff_chain(tmp_path: Path) -> None:
    ctx = make_context(tmp_path, FakeGateway())
    output = asyncio.run(Coordinator(chain_agents()).run(ctx))
    assert output == {"case_id": "L3B_CASE_TEST"}

    trace = events(tmp_path)
    assigned = {event["target"] for event in trace if event["event_type"] == "task_assigned"}
    assert assigned == {"entity-agent", *ORDER_SPECIALISTS, "conflict-agent"}
    handoffs = [(e["actor"], e["target"]) for e in trace if e["event_type"] == "handoff"]
    assert handoffs[-3:] == [
        ("conflict-agent", "policy-agent"),
        ("policy-agent", "verifier-agent"),
        ("verifier-agent", "coordinator"),
    ]
    assert ctx.calls_made == 2


def test_route_rejects_cross_case_messages_and_loops(tmp_path: Path) -> None:
    ctx = make_context(tmp_path, FakeGateway(), max_hops=3)
    foreign = A2AMessage("OTHER_CASE", "coordinator", "entity-agent", "resolve_entity")
    with pytest.raises(ProtocolViolation, match="delivered in"):
        asyncio.run(ctx.route(chain_agents(), foreign))

    loop = {
        "policy-agent": Chain("policy-agent", "verifier-agent", "policy_draft"),
        "verifier-agent": Chain("verifier-agent", "policy-agent", "rework"),
    }
    start = A2AMessage(ctx.case_id, "coordinator", "policy-agent", "decide")
    with pytest.raises(ProtocolViolation, match="exceeded 3 hops"):
        asyncio.run(ctx.route(loop, start))


def test_gateway_circuit_breaker_stops_after_consecutive_failures(tmp_path: Path) -> None:
    from mcp.types import CallToolResult, TextContent

    from student_agent.mcp_gateway import EvidenceGateway, GatewayUnavailable

    class RejectingSession:
        calls = 0

        async def call_tool(self, name: str, arguments: dict[str, str]) -> CallToolResult:
            RejectingSession.calls += 1
            text = TextContent(type="text", text="Error executing tool")
            return CallToolResult(content=[text], is_error=True)

    contracts = Contracts(ROOT / "contracts" / "schemas")
    gateway = EvidenceGateway(RejectingSession(), contracts, max_consecutive_failures=3)  # type: ignore[arg-type]
    ctx = CaseContext(CASE, gateway, TraceWriter(tmp_path / "trace.jsonl", contracts))
    for order_id in ("o1", "o2", "o3"):
        assert asyncio.run(ctx.fetch(Probe(), "get_order", order_id=order_id)) is None
    with pytest.raises(GatewayUnavailable):
        asyncio.run(ctx.fetch(Probe(), "get_order", order_id="o4"))
    assert RejectingSession.calls == 3
