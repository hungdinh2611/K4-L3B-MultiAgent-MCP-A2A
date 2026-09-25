from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts


class EvidenceGateway:
    """One authenticated MCP session, plus a tally of how it answered.

    A refused tool is a legitimate answer for one call - the scope simply holds
    nothing - so the agents absorb it and move on.  In bulk that is misleading:
    a gateway refusing every call looks exactly like a set of genuinely empty
    cases.  The tallies and the last refusal text are what let the runner tell
    those apart and quote the gateway's own words instead of guessing at them.
    """

    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts
        self.answered = 0
        self.refused = 0
        self.last_refusal: str | None = None

    async def list_tools(self) -> list[str]:
        response = await self._session.list_tools()
        return sorted(tool.name for tool in response.tools)

    def _refusal(self, tool_name: str, reason: str) -> RuntimeError:
        """Record a refusal and return the error to raise for it."""
        self.refused += 1
        self.last_refusal = reason
        if self.refused == 1 and not self.answered:
            # A refusal on its own is ordinary - an empty scope answers this way -
            # so it is only worth saying out loud while nothing has worked yet.
            # Otherwise a whole outage runs to completion in silence.
            print(f"warning: MCP Gateway refused {tool_name}: {reason}", file=sys.stderr)
        return RuntimeError(f"MCP tool {tool_name} failed: {reason}")

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        payload = {"case_id": case_id, **arguments}
        result = await self._session.call_tool(tool_name, arguments=payload)
        if getattr(result, "is_error", getattr(result, "isError", False)):
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            raise self._refusal(tool_name, message or "unknown error")
        evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            evidence = getattr(result, "structured_content", None)
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            try:
                evidence = json.loads(text_blocks[0])
            except json.JSONDecodeError as error:
                # Prose where an envelope belongs is the gateway answering, not
                # a transport fault: final, and worth repeating verbatim.
                raise self._refusal(
                    tool_name, f"replied with text, not evidence: {text_blocks[0][:200]}"
                ) from error
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        self.answered += 1
        return evidence


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts)
