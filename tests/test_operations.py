import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from agents.tool_context import ToolContext

from infra_ops.agent import (
    InfrastructureContext,
    check_rollout_health,
    deploy_version,
    deployment_status,
    rollback_version,
)
from infra_ops.store import DeploymentStore


FIXTURE = Path(__file__).parents[1] / "fixtures" / "starter.json"


def seeded(tmp_path):
    db = tmp_path / "deployments.db"
    return DeploymentStore.seed(db, FIXTURE)


def invoke(tool, ctx, args):
    encoded = json.dumps(args)
    wrapped = ToolContext(
        ctx, tool_name=tool.name, tool_call_id="offline-test", tool_arguments=encoded
    )
    return json.loads(asyncio.run(tool.on_invoke_tool(wrapped, encoded)))


def test_failed_rollout_stale_cache_and_safe_rollback(tmp_path):
    store = seeded(tmp_path)
    initial = store.status("api")
    assert initial["version"] == "2.0.0" and initial["revision"] == 1
    rolling = store.deploy("api", "2.1.0", expected_revision=1)
    assert rolling["health"] == "rolling" and rolling["revision"] == 2
    cached = store.status("api", cached=True)
    assert cached["revision"] == 1 and cached["source"] == "cached"
    with pytest.raises(ValueError, match="stale revision"):
        store.health_check("api", expected_revision=cached["revision"])
    failed = store.health_check("api", expected_revision=2)
    assert failed["health"] == "degraded" and failed["revision"] == 3
    with pytest.raises(ValueError, match="stale revision"):
        store.rollback("api", expected_revision=2)
    restored = store.rollback("api", expected_revision=store.status("api")["revision"])
    assert restored["version"] == "2.0.0" and restored["health"] == "healthy"
    assert store.status("worker")["version"] == "1.4.0"
    assert [item["event"] for item in store.history("api")] == [
        "deploy_started",
        "health_failed",
        "rolled_back",
    ]


def test_successful_rollout_and_invalid_rollback(tmp_path):
    store = seeded(tmp_path)
    assert store.deploy("worker", "1.5.0", 1)["revision"] == 2
    assert store.health_check("worker", 2)["health"] == "healthy"
    with pytest.raises(ValueError, match="failed rollout"):
        store.rollback("worker", 3)
    with pytest.raises(ValueError, match="version not allowed"):
        store.deploy("api", "9.9.9", 1)
    assert store.status("api")["version"] == "2.0.0"


def test_cli_process_operates_synthetic_database(tmp_path):
    db = tmp_path / "deployments.db"

    def cli(*args):
        result = subprocess.run(
            [sys.executable, "-m", "infra_ops.cli", "--db", str(db), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode, json.loads(result.stdout)

    assert cli("seed", "--fixture", str(FIXTURE))[1]["seeded"] is True
    assert cli("status", "api")[1]["source"] == "fresh"
    assert cli("deploy", "api", "2.1.0", "--revision", "1")[1]["health"] == "rolling"
    assert cli("health", "api", "--revision", "1")[0] == 2
    assert cli("health", "api", "--revision", "2")[1]["health"] == "degraded"
    assert cli("rollback", "api", "--revision", "3")[1]["version"] == "2.0.0"


def test_actual_agents_sdk_tools_reject_generic_context_without_owned_native_authority(
    tmp_path,
):
    store = seeded(tmp_path)
    for enabled in (False, True):
        context = InfrastructureContext(store.path, enabled)
        with pytest.raises(ValueError, match="owned_runtime_required"):
            invoke(deployment_status, context, {"service": "api", "cached": False})
        with pytest.raises(ValueError, match="owned_runtime_required"):
            invoke(
                deploy_version,
                context,
                {"service": "api", "version": "2.1.0", "expected_revision": 1},
            )
    assert store.status("api")["revision"] == 1
    assert store.history("api") == []
