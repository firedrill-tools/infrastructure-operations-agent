"""Actual SDK function tools; a generic write flag is never model authority."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from agents import RunContextWrapper, function_tool

from .contracts import WRITE_TOOLS, arguments
from .authority import same
from .source import keys, parse_json
from .state import canonical, hex_digest, identifier, sha256
from .evidence import error_info


@dataclass
class InfrastructureContext:
    database: Path
    allow_writes: bool
    owner: object = None
    cli: object = None
    proofs: dict = field(default_factory=dict)
    pending: set = field(default_factory=set)

    async def dispatch(self, wrapped, name, typed):
        if self.owner is None or self.cli is None:
            raise ValueError(
                "owned_runtime_required; generic write flag is not deployment authority"
            )
        owner, receipt = self.owner, self.owner.receipt
        task, action = asyncio.current_task(), None
        self.pending.add(task)
        try:
            owner.guard()
            raw = arguments(name, parse_json(wrapped.tool_arguments))
            if not same(raw, typed):
                raise ValueError("SDK coerced or changed original tool arguments")
            authority = owner.authority
            if (
                authority is None
                or authority["consumed"]
                or authority["accepted"]["name"] != name
                or authority["accepted"]["call_id"] != wrapped.tool_call_id
                or not same(authority["accepted"]["args"], raw)
            ):
                raise ValueError(
                    "SDK tool lacks completed original native ID/name/argument authority"
                )
            authority["consumed"] = True
            action = {
                "number": len(receipt.data["actions"]) + 1,
                "request_number": authority["request_number"],
                "original_tool_id": wrapped.tool_call_id,
                "name": name,
                "original_arguments": wrapped.tool_arguments,
                "args": raw,
                "status": "prepared",
                "source_before": receipt.source(owner.source),
                "source_after": None,
            }
            receipt.data["actions"].append(action)
            receipt.save()
            if name in WRITE_TOOLS and not self.allow_writes:
                raise ValueError("writes_not_authorized")
            envelope = await self.cli.execute(
                name,
                raw,
                self.proofs.get(raw["service"]) if name in WRITE_TOOLS else None,
            )
            action["cli_observation"] = envelope
            after = owner.store.snapshot()
            if name in WRITE_TOOLS:
                keys(envelope, {"value", "decision"})
                decision = envelope["decision"]
                actual = next(
                    (
                        d
                        for d in after["records"]["decisions"]
                        if d["id"] == decision.get("id")
                    ),
                    None,
                )
                if (
                    actual is None
                    or not same(actual, decision)
                    or decision["operation_id"] != owner.scope["id"]
                    or decision["before_context_sha256"] != owner.expected_context
                    or decision["after_context_sha256"] != after["context_sha256"]
                    or not same(decision["args"], raw)
                    or decision["kind"]
                    != {
                        "deploy_version": "deploy",
                        "check_rollout_health": "health",
                        "rollback_version": "rollback",
                    }[name]
                    or not same(
                        envelope["value"],
                        {**decision["after"], "outcome": decision["status"]},
                    )
                ):
                    raise ValueError(
                        "actual CLI decision differs from exact committed scoped source"
                    )
            else:
                keys(envelope, {"value", "observation", "proof"})
                observation, proof = envelope["observation"], envelope["proof"]
                keys(proof, {"id", "token"})
                identifier(proof["id"])
                hex_digest(proof["token"])
                actual = next(
                    (
                        r
                        for r in after["records"]["observations"]
                        if r["id"] == observation.get("id")
                    ),
                    None,
                )
                if (
                    actual is None
                    or not same(actual, observation)
                    or observation["id"] != proof["id"]
                    or observation["operation_id"] != owner.scope["id"]
                    or observation["token_sha256"] != sha256(proof["token"].encode())
                    or observation["context_sha256"] != owner.expected_context
                    or after["context_sha256"] != owner.expected_context
                    or not same(observation["value"], envelope["value"])
                ):
                    raise ValueError(
                        "actual CLI observation differs from original current scoped source"
                    )
                if name == "deployment_status":
                    self.proofs[raw["service"]] = proof
            owner.source, owner.expected_context = after, after["context_sha256"]
            owner.guard()
            action.update(
                status="finished",
                source_after=receipt.source(after),
                business_observation=envelope["value"],
            )
            receipt.save()
            return canonical(envelope["value"])
        except BaseException as error:
            if action is not None:
                action.update(status="failed", error=error_info(error))
                try:
                    action["source_after"] = receipt.source(owner.store.snapshot())
                except BaseException as observation_error:
                    action["source_after_error"] = error_info(observation_error)
            try:
                receipt.save()
            except BaseException:
                pass
            owner.fail(error)
            raise
        finally:
            self.pending.discard(task)


@function_tool(failure_error_function=None)
async def deployment_status(
    ctx: RunContextWrapper[InfrastructureContext], service: str, cached: bool = False
) -> str:
    """Read complete synthetic service status and allowed versions. Cached state cannot authorize a change."""
    return await ctx.context.dispatch(
        ctx, "deployment_status", {"service": service, "cached": cached}
    )


@function_tool(failure_error_function=None)
async def deployment_history(
    ctx: RunContextWrapper[InfrastructureContext], service: str
) -> str:
    """Read the complete original synthetic rollout, health and rollback event history for one service."""
    return await ctx.context.dispatch(ctx, "deployment_history", {"service": service})


@function_tool(failure_error_function=None)
async def deploy_version(
    ctx: RunContextWrapper[InfrastructureContext],
    service: str,
    version: str,
    expected_revision: int,
) -> str:
    """Start only the exact caller-authorized rollout after a fresh current status. At-target retries are explicit no-ops."""
    return await ctx.context.dispatch(
        ctx,
        "deploy_version",
        {
            "service": service,
            "version": version,
            "expected_revision": expected_revision,
        },
    )


@function_tool(failure_error_function=None)
async def check_rollout_health(
    ctx: RunContextWrapper[InfrastructureContext], service: str, expected_revision: int
) -> str:
    """Check only this operation's actual pending rollout after fresh status. Record healthy or degraded state."""
    return await ctx.context.dispatch(
        ctx,
        "check_rollout_health",
        {"service": service, "expected_revision": expected_revision},
    )


@function_tool(failure_error_function=None)
async def rollback_version(
    ctx: RunContextWrapper[InfrastructureContext], service: str, expected_revision: int
) -> str:
    """Recover only this operation's own failed rollout with exact caller recovery permission and fresh status."""
    return await ctx.context.dispatch(
        ctx,
        "rollback_version",
        {"service": service, "expected_revision": expected_revision},
    )


TOOLS = [
    deployment_status,
    deployment_history,
    deploy_version,
    check_rollout_health,
    rollback_version,
]
