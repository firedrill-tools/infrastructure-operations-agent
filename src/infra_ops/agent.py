"""OpenAI Agents SDK boundary over the synthetic deployment CLI."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from agents import Agent, RunContextWrapper, Runner, function_tool

from .store import DeploymentStore


@dataclass
class InfrastructureContext:
    database: Path
    allow_writes: bool


def _cli(ctx: InfrastructureContext, *args: str) -> str:
    command = [sys.executable, "-m", "infra_ops.cli", "--db", str(ctx.database), *args]
    proc = subprocess.run(command, text=True, capture_output=True, check=False, timeout=15)
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("synthetic CLI returned invalid JSON") from error
    if proc.returncode not in (0, 2):
        raise ValueError("synthetic CLI failed unexpectedly")
    return json.dumps(result)


@function_tool
def deployment_status(ctx: RunContextWrapper[InfrastructureContext], service: str,
                      cached: bool = False) -> str:
    """Read a service's deployment status and revision. Default is fresh; cached snapshots may be stale."""
    return _cli(ctx.context, "status", service, *(["--cached"] if cached else []))


@function_tool
def deployment_history(ctx: RunContextWrapper[InfrastructureContext], service: str) -> str:
    """Read the synthetic deployment and health event history for one service."""
    return _cli(ctx.context, "history", service)


@function_tool
def deploy_version(ctx: RunContextWrapper[InfrastructureContext], service: str,
                   version: str, expected_revision: int) -> str:
    """Start an authorized synthetic rollout against a fresh expected revision; no cloud operation occurs."""
    if not ctx.context.allow_writes:
        return json.dumps({"error": "writes_not_authorized"})
    return _cli(ctx.context, "deploy", service, version, "--revision", str(expected_revision))


@function_tool
def check_rollout_health(ctx: RunContextWrapper[InfrastructureContext], service: str,
                         expected_revision: int) -> str:
    """Complete a pending synthetic health check; it may mark a rollout degraded."""
    if not ctx.context.allow_writes:
        return json.dumps({"error": "writes_not_authorized"})
    return _cli(ctx.context, "health", service, "--revision", str(expected_revision))


@function_tool
def rollback_version(ctx: RunContextWrapper[InfrastructureContext], service: str,
                     expected_revision: int) -> str:
    """Roll back only a degraded synthetic rollout at its current revision."""
    if not ctx.context.allow_writes:
        return json.dumps({"error": "writes_not_authorized"})
    return _cli(ctx.context, "rollback", service, "--revision", str(expected_revision))


def run_agent(database: str | Path, request: str, model: str | None = None,
              allow_writes: bool = False) -> dict:
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY required for a live Agents SDK run")
    selected = model or os.environ.get("INFRA_MODEL")
    if not selected:
        raise RuntimeError("INFRA_MODEL must name an enabled model")
    if not request.strip():
        raise ValueError("request required")
    db = Path(database).resolve()
    store = DeploymentStore(db)
    agent = Agent[InfrastructureContext](
        name="Synthetic Infrastructure Operator", model=selected,
        instructions=(
            "Operate only the synthetic deployment CLI tools. Read fresh status before any change; "
            "cached status can be stale. Use expected revisions. After deploy, check health and "
            "read fresh status; if degraded, roll back to the previous version using the current "
            "revision and verify health again. Do not claim success from a rollout-start response. "
            "If status or revision changes unexpectedly, stop and re-evaluate. Never imply a real "
            "cloud deployment or use credentials beyond this local simulation."
        ),
        tools=[deployment_status, deployment_history, deploy_version,
               check_rollout_health, rollback_version],
    )
    result = Runner.run_sync(agent, request, context=InfrastructureContext(db, allow_writes), max_turns=14)
    usage = result.context_wrapper.usage
    return {"response": result.final_output,
            "usage": {"requests": usage.requests, "input_tokens": usage.input_tokens,
                      "output_tokens": usage.output_tokens},
            "api": store.status("api"), "worker": store.status("worker"),
            "api_history": store.history("api")}


def main() -> None:
    parser = argparse.ArgumentParser(description="Synthetic infrastructure agent")
    parser.add_argument("--db", default="workspace/deployments.db")
    parser.add_argument("--request", required=True)
    parser.add_argument("--model")
    parser.add_argument("--allow-writes", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(run_agent(args.db, args.request, args.model, args.allow_writes), indent=2))
    except (ValueError, RuntimeError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
