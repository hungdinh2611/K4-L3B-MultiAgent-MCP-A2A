<<<<<<< HEAD
from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, ClassVar

import httpx2
from mcp.shared.exceptions import MCPError

from .mcp_gateway import EvidenceGateway, GatewayUnavailable
from .trace import TraceWriter

COORDINATOR = "coordinator"
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (TimeoutError, httpx2.TransportError)
TRACE_MAX_REFS = 20


class ToolPermissionError(PermissionError):
    """An actor asked for a tool outside its least-privilege allowlist."""


class ProtocolViolation(RuntimeError):
    """An A2A message broke case scope, addressing or the hop limit."""


@dataclass(frozen=True)
class Evidence:
    ref: str
    tool_name: str
    domain: str
    data: Any
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolFailure:
    actor: str
    tool_name: str
    code: str
    detail: str
=======
"""Phase 3 - the agent-to-agent protocol and the one door to the gateway.

No agent holds the gateway, the trace, or another agent.  Each is handed a
``CaseContext`` and answers with an ``A2AMessage``; the context is what actually
calls MCP, registers the references that came back, and writes the observable
trace.  Routing a message through the context is therefore the same act as
recording that it happened, which is what makes the trace an audit of the run
rather than a commentary on it.

The context also refuses a reply citing a reference this case never earned, so a
fabricated ``evidence_ref`` cannot reach the output by way of a handoff.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

COORDINATOR = "coordinator"
ENTITY_AGENT = "entity-agent"
ORDER_AGENT = "order-agent"
SHIPMENT_AGENT = "shipment-agent"
PAYMENT_AGENT = "payment-agent"
POLICY_AGENT = "policy-agent"
CONFLICT_AGENT = "conflict-agent"
VERIFIER_AGENT = "verifier-agent"

# Tool discovery result mapped to the evidence domain each tool is allowed to
# answer with.  An envelope carrying any other domain is discarded unconsumed.
TOOL_DOMAINS = {
    "get_order": "order",
    "get_order_items": "item",
    "get_sellers": "seller",
    "get_product_context": "product",
    "get_shipment_summary": "shipment",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
    "get_customer_history": "customer",
    "get_policy": "policy",
}

MCP_TIMEOUT = 30.0
MCP_ATTEMPTS = 2
MCP_BACKOFF = 0.2

# The review chain is conflict -> policy -> verifier.  A couple of spare hops
# absorb a legitimate extra referral; anything past that is a routing loop.
MAX_HOPS = 8

# Long enough for a sibling agent to finish its own gateway calls, short enough
# that a specialist which never answers fails the case instead of hanging it.
FINDING_TIMEOUT = MCP_TIMEOUT * MCP_ATTEMPTS + 5.0


class ProtocolViolation(RuntimeError):
    """An agent broke the A2A contract, rather than merely finding nothing."""


def unique_refs(*groups: Any) -> list[str]:
    """Merge reference lists, keeping first appearance and dropping repeats."""
    merged: list[str] = []
    for group in groups:
        for ref in group or ():
            if isinstance(ref, str) and ref and ref not in merged:
                merged.append(ref)
    return merged
>>>>>>> 9a7a84c24ef1cfa8a96be6d80805dc1c9e3b0e8e


@dataclass(frozen=True)
class A2AMessage:
<<<<<<< HEAD
    """Envelope exchanged between agents. Correlated by case_id and never crosses cases."""
=======
    """One addressed exchange between two agents.

    ``payload`` is the wire content the recipient may act on and
    ``evidence_refs`` are the references the sender stands behind.  ``intent``
    names the work; the reserved intent ``finalize`` ends a review chain.
    """
>>>>>>> 9a7a84c24ef1cfa8a96be6d80805dc1c9e3b0e8e

    case_id: str
    sender: str
    recipient: str
    intent: str
    payload: dict[str, Any] = field(default_factory=dict)
<<<<<<< HEAD
    evidence_refs: tuple[str, ...] = ()


def unique_refs(*groups: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(ref for group in groups for ref in group))


class Agent:
    actor: ClassVar[str]
    tools: ClassVar[frozenset[str]] = frozenset()

    async def handle(self, ctx: CaseContext, message: A2AMessage) -> A2AMessage:
        raise NotImplementedError(f"{self.actor} has no handler yet")

    def reply(
        self,
        message: A2AMessage,
        intent: str,
        payload: dict[str, Any],
        evidence_refs: Iterable[str] = (),
        recipient: str | None = None,
    ) -> A2AMessage:
        return A2AMessage(
            case_id=message.case_id,
            sender=self.actor,
            recipient=recipient or message.sender,
            intent=intent,
            payload=payload,
            evidence_refs=unique_refs(evidence_refs),
        )


class CaseContext:
    """Everything one case may touch: scoped MCP access, evidence ledger and trace.

    A new context is created per case, so cache and evidence can never leak across cases.
    """

    def __init__(
        self,
        case: dict[str, Any],
        gateway: EvidenceGateway,
        trace: TraceWriter,
        *,
        call_budget: int = 12,
        max_attempts: int = 2,
        call_timeout: float = 60.0,
        max_hops: int = 8,
    ) -> None:
        self.case = case
        self.case_id: str = case["case_id"]
        self.call_budget = call_budget
        self.max_attempts = max_attempts
        self.call_timeout = call_timeout
        self.max_hops = max_hops
        self.calls_made = 0
        self.evidence: dict[str, Evidence] = {}
        self.failures: list[ToolFailure] = []
        self.contracts = trace.contracts
        self._gateway = gateway
        self._trace = trace
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], asyncio.Task[Any]] = {}
        self._consumed: set[tuple[str, str]] = set()

    def consumed_refs(self) -> set[str]:
        return {ref for _, ref in self._consumed}

    async def fetch(self, agent: Agent, tool_name: str, **arguments: str) -> Evidence | None:
        """Call one MCP tool for this case; returns None when evidence is unavailable."""
        if tool_name not in agent.tools:
            raise ToolPermissionError(f"{agent.actor} may not call {tool_name}")
        key = (tool_name, tuple(sorted(arguments.items())))
        task = self._cache.get(key)
        if task is None:
            task = asyncio.ensure_future(self._call(agent.actor, tool_name, arguments))
            self._cache[key] = task
        evidence: Evidence | None = await task
        if evidence is not None and (agent.actor, evidence.ref) not in self._consumed:
            self._consumed.add((agent.actor, evidence.ref))
            self.emit(
                "tool_result_consumed",
                actor=agent.actor,
                tool_name=tool_name,
                evidence_refs=[evidence.ref],
                attributes={"domain": evidence.domain, "warnings": len(evidence.warnings)},
            )
        return evidence

    async def _call(self, actor: str, tool_name: str, arguments: dict[str, str]) -> Evidence | None:
        last_error = "no attempt made"
        for _ in range(self.max_attempts):
            if self.calls_made >= self.call_budget:
                return self._fail(actor, tool_name, "budget_exhausted", f"{self.call_budget} calls")
            self.calls_made += 1
            try:
                async with asyncio.timeout(self.call_timeout):
                    envelope = await self._gateway.call(
                        tool_name, case_id=self.case_id, **arguments
                    )
            except TRANSIENT_ERRORS as exc:
                last_error = type(exc).__name__
                continue
            except GatewayUnavailable:
                raise
            except (RuntimeError, MCPError) as exc:
                return self._fail(actor, tool_name, "tool_error", str(exc))
            except ValueError as exc:
                return self._fail(actor, tool_name, "invalid_response", str(exc))
            evidence = Evidence(
                ref=envelope["evidence_ref"],
                tool_name=tool_name,
                domain=envelope["domain"],
                data=envelope["data"],
                warnings=tuple(envelope.get("warnings", ())),
            )
            self.evidence[evidence.ref] = evidence
            return evidence
        return self._fail(actor, tool_name, "transient_exhausted", last_error)

    def _fail(self, actor: str, tool_name: str, code: str, detail: str) -> None:
        self.failures.append(ToolFailure(actor, tool_name, code, detail[:160]))
        return None

    async def dispatch(self, agent: Agent, message: A2AMessage) -> A2AMessage:
        """Deliver one message to its recipient and trace the resulting handoff."""
        if message.case_id != self.case_id:
            raise ProtocolViolation(f"message for {message.case_id} delivered in {self.case_id}")
        if message.recipient != agent.actor:
            raise ProtocolViolation(f"message for {message.recipient} delivered to {agent.actor}")
        if message.sender == COORDINATOR:
            self.emit(
                "task_assigned",
                actor=COORDINATOR,
                target=agent.actor,
                decision_code=message.intent,
            )
        result = await agent.handle(self, message)
        if result.case_id != self.case_id or result.sender != agent.actor:
            raise ProtocolViolation(f"{agent.actor} returned a message outside its scope")
        unknown = [ref for ref in result.evidence_refs if ref not in self.evidence]
        if unknown:
            raise ProtocolViolation(f"{agent.actor} cited evidence it never received: {unknown}")
        self.emit(
            "handoff",
            actor=result.sender,
            target=result.recipient,
            decision_code=result.intent,
            evidence_refs=list(result.evidence_refs[:TRACE_MAX_REFS]) or None,
        )
        return result

    async def route(self, agents: dict[str, Agent], message: A2AMessage) -> A2AMessage:
        """Follow a handoff chain until it returns to the coordinator."""
        for _ in range(self.max_hops):
            agent = agents.get(message.recipient)
            if agent is None:
                raise ProtocolViolation(f"unknown recipient {message.recipient}")
            message = await self.dispatch(agent, message)
            if message.recipient == COORDINATOR:
                return message
        raise ProtocolViolation(f"handoff chain exceeded {self.max_hops} hops")

    def emit(self, event_type: str, *, actor: str, **fields: Any) -> None:
        self._trace.emit(case_id=self.case_id, event_type=event_type, actor=actor, **fields)
=======
    evidence_refs: list[str] = field(default_factory=list)
    decision_code: str | None = None
    event_type: str = "handoff"

    def reply(
        self,
        intent: str,
        recipient: str,
        payload: dict[str, Any] | None = None,
        *,
        evidence_refs: list[str] | None = None,
        decision_code: str | None = None,
        event_type: str = "handoff",
    ) -> A2AMessage:
        """An answer sent back out by whoever received this message."""
        return A2AMessage(
            self.case_id,
            self.recipient,
            recipient,
            intent,
            payload or {},
            evidence_refs or [],
            decision_code,
            event_type,
        )


class Agent(Protocol):
    """What the coordinator needs of a specialist: a name and one entry point."""

    actor: str

    async def handle(self, ctx: CaseContext, message: A2AMessage) -> A2AMessage: ...


class EvidenceLedger:
    """Case-scoped registry of references handed back by the gateway.

    Only values that arrived inside a validated envelope for this case are
    stored, so the output can never cite a reference this run did not earn.
    """

    def __init__(self) -> None:
        self._by_tool: dict[str, str] = {}
        self._order: list[str] = []

    def register(self, tool_name: str, evidence_ref: str) -> str:
        if evidence_ref not in self._order:
            self._order.append(evidence_ref)
        self._by_tool[tool_name] = evidence_ref
        return evidence_ref

    def refs(self, *tool_names: str) -> list[str]:
        found = [self._by_tool[name] for name in tool_names if name in self._by_tool]
        return list(dict.fromkeys(found))

    def consulted(self, tool_name: str) -> bool:
        return tool_name in self._by_tool

    def all(self) -> list[str]:
        return list(self._order)

    def owns(self, refs: list[str]) -> bool:
        return set(refs).issubset(self._order)


class CaseContext:
    """One case's shared workbench: the gateway door, the ledger, the trace.

    Specialists keep their typed findings in ``findings`` and hand each other
    summaries in message payloads.  Both are case-scoped and die with the case,
    so nothing can leak from one case's reasoning into another's.
    """

    def __init__(self, case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.case = case
        self.case_id: str = case["case_id"]
        self.ledger = EvidenceLedger()
        self.findings: dict[str, Any] = {}
        self.calls = 0
        self.sequence: list[str] = []
        self._gateway = gateway
        self._trace = trace
        self._exhausted: set[str] = set()
        self._announced: dict[str, asyncio.Event] = {}

    def announce(self, name: str, finding: Any) -> None:
        """Publish a finding for the agents that come after this one."""
        self.findings[name] = finding
        self._announced.setdefault(name, asyncio.Event()).set()

    async def awaited(self, name: str, timeout: float = FINDING_TIMEOUT) -> Any:
        """Wait for a finding a sibling agent is still working on.

        Specialists that run in parallel are independent in the common case but
        not always: a payment cannot decide whether a refund still matters until
        the shipment verdict is in.  Waiting here, at the one point the answer is
        needed, keeps the two calls concurrent while the dependency stays honest.
        """
        gate = self._announced.setdefault(name, asyncio.Event())
        try:
            await asyncio.wait_for(gate.wait(), timeout)
        except TimeoutError:
            raise ProtocolViolation(f"no agent ever announced {name!r}") from None
        return self.findings[name]

    def note(
        self,
        actor: str,
        event_type: str,
        *,
        target: str | None = None,
        decision_code: str | None = None,
        evidence_refs: list[str] | None = None,
    ) -> None:
        self._trace.emit(
            case_id=self.case_id,
            event_type=event_type,
            actor=actor,
            target=target,
            decision_code=decision_code,
            evidence_refs=evidence_refs or None,
        )
        self.sequence.append(event_type)

    async def fetch(self, actor: str, tool_name: str, **arguments: str) -> dict[str, Any] | None:
        """Call one discovered tool and consume its evidence, or return None.

        ``case_id`` travels with every request.  A tool-level failure is a final
        answer and is not retried; only transport faults get the second attempt.
        """
        key = f"{tool_name}:{sorted(arguments.items())}"
        if key in self._exhausted:
            return None
        expected_domain = TOOL_DOMAINS[tool_name]
        for attempt in range(MCP_ATTEMPTS):
            try:
                evidence = await asyncio.wait_for(
                    self._gateway.call(tool_name, case_id=self.case_id, **arguments),
                    timeout=MCP_TIMEOUT,
                )
            except RuntimeError:
                # The gateway answered: this tool has nothing for this scope.
                self.calls += 1
                break
            except (TimeoutError, ConnectionError, OSError, ValueError):
                self.calls += 1
                if attempt + 1 < MCP_ATTEMPTS:
                    await asyncio.sleep(MCP_BACKOFF)
                continue
            self.calls += 1
            if evidence.get("domain") != expected_domain:
                break
            evidence_ref = evidence.get("evidence_ref")
            if not isinstance(evidence_ref, str) or not evidence_ref:
                break
            self.ledger.register(tool_name, evidence_ref)
            self._trace.emit(
                case_id=self.case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=[evidence_ref],
            )
            self.sequence.append("tool_result_consumed")
            return evidence
        self._exhausted.add(key)
        return None

    async def dispatch(
        self, agent: Agent, message: A2AMessage, *, assign: bool = True
    ) -> A2AMessage:
        """Assign one message to one agent and accept its answer.

        ``assign`` is false for a message that is itself an agent's referral: the
        sender's own handoff already recorded the transfer, and tracing a second
        assignment for it would report the same hop twice.
        """
        if message.case_id != self.case_id:
            raise ProtocolViolation(
                f"{message.intent!r} carries case {message.case_id!r} inside {self.case_id!r}"
            )
        if message.recipient != agent.actor:
            raise ProtocolViolation(
                f"{message.intent!r} is addressed to {message.recipient!r} "
                f"but reached {agent.actor!r}"
            )
        if assign:
            self.note(message.sender, "task_assigned", target=message.recipient)
        return self._accept(agent, await agent.handle(self, message))

    def _accept(self, agent: Agent, reply: Any) -> A2AMessage:
        """Hold an answer to the protocol, then trace it."""
        if not isinstance(reply, A2AMessage):
            raise ProtocolViolation(
                f"{agent.actor} answered with {type(reply).__name__}, not a message"
            )
        if reply.case_id != self.case_id:
            raise ProtocolViolation(f"{agent.actor} answered for case {reply.case_id!r}")
        if reply.sender != agent.actor:
            raise ProtocolViolation(f"{agent.actor} signed its answer {reply.sender!r}")
        if not self.ledger.owns(reply.evidence_refs):
            raise ProtocolViolation(f"{agent.actor} cited a reference this case never consumed")
        self.note(
            reply.sender,
            reply.event_type,
            target=reply.recipient,
            decision_code=reply.decision_code,
            evidence_refs=reply.evidence_refs or None,
        )
        return reply

    async def route(self, agents: dict[str, Agent], message: A2AMessage) -> A2AMessage:
        """Follow a referral chain until an agent finalises it."""
        visited: list[str] = []
        current = message
        for hop in range(MAX_HOPS):
            agent = agents.get(current.recipient)
            if agent is None:
                raise ProtocolViolation(f"no agent is registered as {current.recipient!r}")
            visited.append(current.recipient)
            reply = await self.dispatch(agent, current, assign=hop == 0)
            if reply.intent == "finalize":
                return reply
            if reply.recipient == current.recipient:
                raise ProtocolViolation(f"{agent.actor} referred {reply.intent!r} to itself")
            current = reply
        raise ProtocolViolation(f"the review chain never finalised: {' -> '.join(visited)}")
>>>>>>> 9a7a84c24ef1cfa8a96be6d80805dc1c9e3b0e8e
