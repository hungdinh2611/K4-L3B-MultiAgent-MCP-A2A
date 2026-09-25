from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx2

from .cases import CaseSet, load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case

# The gateway occasionally drops a streamable HTTP session mid-request, which
# tears down the whole MCP transport rather than one call.  Reconnect and resume
# from the last committed case instead of losing the run.
TRANSIENT_FAULTS = (
    httpx2.HTTPError,
    ConnectionError,
    OSError,
    TimeoutError,
    asyncio.CancelledError,
)
MAX_SESSIONS = 60
MAX_CASE_FAULTS = 3


def _root(value: str) -> Path:
    return Path(value).resolve()


def _transient_only(error: BaseException) -> bool:
    if isinstance(error, BaseExceptionGroup):
        return bool(error.exceptions) and all(
            _transient_only(inner) for inner in error.exceptions
        )
    return isinstance(error, TRANSIENT_FAULTS)


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


def _write_output(output_root: Path, case_id: str, output: dict[str, Any]) -> None:
    target = output_root / f"{case_id}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


async def _drive_session(
    settings: Settings,
    contracts: Contracts,
    case_set: CaseSet,
    pending: list[str],
    trace: TraceWriter,
    trace_path: Path,
    output_root: Path,
    progress: dict[str, Any],
) -> None:
    """Solve the pending cases over one gateway session.

    ``progress['committed']`` is the trace size after the last case that was
    written in full, so a session that dies mid-case can be rolled back to it.
    """
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        discovered_tools = await gateway.list_tools()
        if not discovered_tools:
            raise RuntimeError("MCP Gateway returned no tools")
        for case_id in pending:
            case = case_set.cases[case_id]
            trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
            output = await solve_case(case, gateway, trace)
            contracts.validate_output(output, f"outputs/{case_id}.json")
            if output.get("case_id") != case_id:
                raise ValueError(f"solver returned a mismatched case_id for {case_id}")
            _write_output(output_root, case_id, output)
            trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
            progress["done"].append(case_id)
            progress["committed"] = trace_path.stat().st_size


async def _run(root: Path) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in output_root.glob("*.json"):
        stale.unlink()
    trace_path.unlink(missing_ok=True)
    trace_path.touch()
    trace = TraceWriter(trace_path, contracts)

    progress: dict[str, Any] = {"done": [], "committed": 0}
    faults: dict[str, int] = {}
    total = len(case_set.case_ids)
    for _ in range(MAX_SESSIONS):
        finished = set(progress["done"])
        pending = [case_id for case_id in case_set.case_ids if case_id not in finished]
        if not pending:
            return
        try:
            await _drive_session(
                settings,
                contracts,
                case_set,
                pending,
                trace,
                trace_path,
                output_root,
                progress,
            )
        except Exception as error:
            if not _transient_only(error):
                raise
            solved = set(progress["done"])
            blocked = [case_id for case_id in pending if case_id not in solved]
            if not blocked:
                return
            stalled = blocked[0]
            faults[stalled] = faults.get(stalled, 0) + 1
            if faults[stalled] >= MAX_CASE_FAULTS:
                raise RuntimeError(
                    f"{stalled}: gateway session failed {MAX_CASE_FAULTS} times"
                ) from error
            # Drop the half-written events so the trace matches the outputs.
            with trace_path.open("r+b") as handle:
                handle.truncate(progress["committed"])
            print(
                f"reconnecting after a gateway transport fault "
                f"({len(progress['done'])}/{total} cases complete)",
                file=sys.stderr,
            )
    raise RuntimeError(f"gateway session could not be sustained for {MAX_SESSIONS} attempts")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    commands.add_parser("run", help="run the implemented workflow for all cases")
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / "
                f"{len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
