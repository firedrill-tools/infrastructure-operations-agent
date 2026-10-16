import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from infra_ops.state import digest
from infra_ops.store import DeploymentStore

FIXTURE = Path(__file__).parents[1] / "fixtures/starter.json"
INTENT = {
    "service": "api",
    "version": "2.1.0",
    "expected_revision": 1,
    "rollback_on_failure": True,
}


def environment():
    return {
        k: os.environ[k]
        for k in (
            "PATH",
            "HOME",
            "TMPDIR",
            "TMP",
            "TEMP",
            "LANG",
            "SystemRoot",
            "WINDIR",
        )
        if k in os.environ
    }


def command(store, *args):
    return [
        sys.executable,
        "-I",
        "-m",
        "infra_ops.cli",
        "--db",
        str(store.path),
        "--owned-input",
        *args,
    ]


def call(store, credential, args, proof=None):
    result = subprocess.run(
        command(store, *args),
        input=json.dumps({"credential": credential, "proof": proof}),
        text=True,
        capture_output=True,
        env=environment(),
        timeout=10,
    )
    return result.returncode, json.loads(result.stdout)


def setup(tmp_path):
    store = DeploymentStore.seed(tmp_path / "state.db", FIXTURE)
    credential = store.register_operation(
        digest("actual CLI current scope"), True, INTENT
    )["credential"]
    return store, credential


def test_real_owned_cli_carries_current_private_status_proof_to_exact_scoped_rollout(
    tmp_path,
):
    store, key = setup(tmp_path)
    code, read = call(store, key, ["status", "api"])
    assert code == 0 and read["value"]["source"] == "fresh"
    code, result = call(
        store, key, ["deploy", "api", "2.1.0", "--revision", "1"], read["proof"]
    )
    assert code == 0 and result["value"]["health"] == "rolling"
    assert result["decision"]["operation_id"] == store.current_operation(key)["id"]
    assert store.snapshot()["records"]["decisions"][0] == result["decision"]


def test_owned_cli_missing_stdin_cannot_fall_back_to_trusted_human_write(tmp_path):
    store, _key = setup(tmp_path)
    original = store.path.read_bytes()
    result = subprocess.run(
        command(store, "deploy", "api", "2.1.0", "--revision", "1"),
        input="",
        text=True,
        capture_output=True,
        env=environment(),
        timeout=10,
    )
    assert result.returncode == 2
    assert "error" in json.loads(result.stdout)
    assert store.path.read_bytes() == original


@pytest.mark.skipif(
    not hasattr(signal, "SIGSTOP"), reason="actual POSIX stop/resume qualification"
)
def test_actual_paused_cli_with_pending_rollout_stdin_cannot_commit_after_durable_scope_close(
    tmp_path,
):
    store, key = setup(tmp_path)
    proof = store.observe("api", credential=key)["proof"]
    child = subprocess.Popen(
        command(store, "deploy", "api", "2.1.0", "--revision", "1"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment(),
    )
    try:
        os.kill(child.pid, signal.SIGSTOP)
        pid, status = os.waitpid(child.pid, os.WUNTRACED)
        assert pid == child.pid and os.WIFSTOPPED(status)
        child.stdin.write(json.dumps({"credential": key, "proof": proof}).encode())
        child.stdin.close()
        child.stdin = None
        store.close_operation(key)
        assert store.current_operation(key, allow_closed=True)["status"] == "closed"
        os.kill(child.pid, signal.SIGCONT)
        out, err = child.communicate(timeout=10)
        assert child.returncode == 2, err
        assert json.loads(out)["error"] == "operation_closed"
        assert store.snapshot()["business"]["history"] == []
        assert store.snapshot()["records"]["decisions"] == []
    finally:
        if child.poll() is None:
            os.kill(child.pid, signal.SIGCONT)
            child.kill()
        child.wait(timeout=10)


def test_private_caller_intent_preflight_does_not_mint_a_runtime_status_capability(
    tmp_path,
):
    store, key = setup(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "infra_ops.cli",
            "--db",
            str(store.path),
            "inspect",
            "--service",
            "api",
            "--version",
            "2.1.0",
            "--rollback-on-failure",
        ],
        capture_output=True,
        text=True,
        env=environment(),
        timeout=10,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == INTENT
    code, value = call(store, key, ["deploy", "api", "2.1.0", "--revision", "1"])
    assert code == 2 and "error" in value
    assert store.snapshot()["business"]["history"] == []


def test_actual_cached_cli_status_at_same_revision_still_cannot_authorize_rollout(
    tmp_path,
):
    store, key = setup(tmp_path)
    code, read = call(store, key, ["status", "api", "--cached"])
    assert code == 0 and read["value"]["revision"] == 1
    code, value = call(
        store, key, ["deploy", "api", "2.1.0", "--revision", "1"], read["proof"]
    )
    assert code == 2 and "genuine_current" in value["error"]
    assert store.snapshot()["business"]["history"] == []
