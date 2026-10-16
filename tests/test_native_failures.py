import asyncio
import base64
import copy
import json
import os
import signal
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

from infra_ops import evidence
from infra_ops.native import NativeTransport
from infra_ops.owned_cli import OwnedCLI
from infra_ops.runtime import Cancellation, run_agent, run_agent_async
from native_helper import ACTIONS, KEY, MODEL, environment
from test_native_runtime import rejected


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate",
        "boolean-revision",
        "extra-argument",
        "unknown-tool",
        "parallel",
        "refusal",
        "replayed-call",
        "bad-utf8",
        "oversize",
        "non-json",
        "partial-body",
        "http-error",
    ],
)
def test_original_response_faults_stop_before_second_cli_without_retry(fault, tmp_path):
    def mutate(body, n, _request, handler):
        if n != 2:
            return body
        item = body["output"][0]
        if fault == "duplicate":
            return (
                json.dumps(body)
                .replace(
                    '"status": "completed"',
                    '"status":"completed","status":"completed"',
                    1,
                )
                .encode()
            )
        if fault == "boolean-revision":
            item["arguments"] = json.dumps({**ACTIONS[1][1], "expected_revision": True})
        if fault == "extra-argument":
            item["arguments"] = json.dumps({**ACTIONS[1][1], "shell": "publish"})
        if fault == "unknown-tool":
            item["name"] = "cloud_admin"
        if fault == "parallel":
            body["output"].append({**item, "id": "fc_other", "call_id": "call_other"})
        if fault == "refusal":
            body["output"] = [
                {
                    "type": "message",
                    "id": "m2",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "refusal", "refusal": "no"}],
                }
            ]
        if fault == "replayed-call":
            item["call_id"] = "call_1"
        if fault == "bad-utf8":
            return b"\xff"
        if fault == "oversize":
            return b" " * (1024 * 1024 + 1)
        if fault in ("non-json", "partial-body", "http-error"):
            raw = json.dumps(body).encode()
            handler.send_response(503 if fault == "http-error" else 200)
            handler.send_header(
                "Content-Type",
                "text/html" if fault == "non-json" else "application/json",
            )
            handler.send_header(
                "Content-Length", str(len(raw) + (20 if fault == "partial-body" else 0))
            )
            handler.end_headers()
            handler.wfile.write(raw)
            return None
        return body

    with environment(tmp_path, mutate=mutate) as e:
        receipt = rejected(e["options"], None)
        assert len(e["requests"]) == 2 and len(receipt["commands"]) == 1
        assert e["store"].snapshot()["business"]["history"] == []
        assert receipt["cleanup_confirmed"], receipt["lifecycle"]
        assert receipt["calls"][-1]["status"] == "failed"
        original = receipt["calls"][-1]["response"]
        if fault in ("partial-body", "oversize"):
            assert not original["bytes"]["complete"]
            assert original["bytes"]["sha256"] is None
        if fault == "oversize":
            assert original["bytes"]["retained_bytes"] == 1024 * 1024
            assert original["bytes"]["truncated"]


def test_original_missing_invalid_and_zero_counters_are_distinct_from_sdk_estimates(
    tmp_path,
):
    def mutate(body, n, _request, _handler):
        if n == 1:
            body["usage"]["input_tokens"] = True
            body["usage"]["output_tokens"] = 0
            body["usage"]["input_tokens_details"] = {}
        else:
            body.pop("usage")
        return body

    with environment(tmp_path, actions=ACTIONS[:1], mutate=mutate) as e:
        result = run_agent(**{**e["options"], "allow_writes": False, "intent": None})
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        usage = receipt["reported_usage"]
        assert usage["input"]["invalid"] == 1 and usage["input"]["unknown"] == 1
        assert usage["input"]["value"] is None and usage["input"]["known_subtotal"] == 0
        assert (
            usage["output"]["observed"] == 1 and usage["output"]["known_subtotal"] == 0
        )
        assert usage["output"]["unknown"] == 1 and usage["output"]["value"] is None
        assert usage["cached_input"]["observed"] == 0
        assert result["cost_usd"] is None


@pytest.mark.parametrize("point", ["initial", "after-commit"])
def test_private_recording_failure_preserves_actual_effects_without_success(
    point, tmp_path
):
    calls = []

    def writer(path, data):
        calls.append(data["status"])
        committed = any(
            a.get("cli_observation", {}).get("decision", {}).get("status") == "applied"
            for a in data["actions"]
        )
        if point == "initial" or committed:
            raise OSError("controlled private recording failure")
        evidence.publish(path, data)

    with environment(tmp_path) as e:
        receipt = rejected(
            {**e["options"], "writer": writer}, "controlled private recording failure"
        )
        assert receipt["durable_status"] == "unknown"
        assert receipt["status"] == "failed" and receipt["result"] is None
        history = e["store"].snapshot()["business"]["history"]
        if point == "initial":
            assert len(calls) == 1 and not history and not e["requests"]
            assert not e["store"].snapshot()["records"]["operations"]
        else:
            assert len(history) == 1 and len(e["requests"]) == 2
            assert len(receipt["acknowledged_effects"]) == 1
        assert receipt["cleanup_confirmed"], receipt["lifecycle"]


@pytest.mark.parametrize("fault", ["native-close", "cli-close", "final-source"])
def test_one_attempt_close_and_source_failure_after_commits_is_not_success(
    fault, monkeypatch, tmp_path
):
    native_close, cli_close = NativeTransport.aclose, OwnedCLI.close
    attempts = []

    async def close_native(self):
        attempts.append("native")
        await native_close(self)
        if fault == "native-close":
            raise OSError("controlled native close failure")

    async def close_cli(self):
        attempts.append("cli")
        await cli_close(self)
        if fault == "cli-close":
            raise OSError("controlled CLI close failure")

    monkeypatch.setattr(NativeTransport, "aclose", close_native)
    monkeypatch.setattr(OwnedCLI, "close", close_cli)
    with environment(tmp_path) as e:
        if fault == "final-source":
            from infra_ops.store import DeploymentStore

            close_scope = DeploymentStore.close_operation

            def corrupt_after_close(self, credential):
                value = close_scope(self, credential)
                with sqlite3.connect(self.path) as db:
                    db.execute(
                        "UPDATE services SET health='degraded' WHERE name='worker'"
                    )
                return value

            monkeypatch.setattr(DeploymentStore, "close_operation", corrupt_after_close)
        receipt = rejected(e["options"], None)
        assert attempts.count("native") == attempts.count("cli") == 1
        assert len(receipt["acknowledged_effects"]) == 3
        assert receipt["status"] == "failed"
        if fault == "final-source":
            assert receipt["lifecycle"]["operation_closed"]
            assert (
                receipt["final_source"] is None and receipt["observed_business"] is None
            )
            assert receipt["final_source_error"]
        else:
            assert not receipt["cleanup_confirmed"]
            assert receipt["observed_business"]["outcome"] == "verified_recovery"


def test_precancel_prepares_failure_before_source_scope_or_sdk_allocation(tmp_path):
    with environment(tmp_path) as e:
        cancellation = Cancellation()
        cancellation.cancel("already cancelled")
        receipt = rejected(
            {**e["options"], "cancellation": cancellation}, "already cancelled"
        )
        assert receipt["cleanup_confirmed"] and not e["requests"]
        assert not receipt["commands"] and not receipt["calls"]
        assert not e["store"].snapshot()["records"]["operations"]


@pytest.mark.parametrize("kind", ["deadline", "caller-cancel", "parent-task"])
def test_cancel_or_deadline_drains_actual_pending_native_http(kind, tmp_path):
    entered, release = threading.Event(), threading.Event()

    def mutate(body, n, _request, handler):
        if n == 3:
            entered.set()
            assert release.wait(15)
        return body

    async def run(options):
        cancellation = Cancellation()
        task = asyncio.create_task(
            run_agent_async(
                **{
                    **options,
                    "cancellation": cancellation,
                    "timeout_seconds": 1.5 if kind == "deadline" else 10,
                }
            )
        )
        assert await asyncio.to_thread(entered.wait, 5)
        if kind == "caller-cancel":
            cancellation.cancel("caller ended operation")
        elif kind == "parent-task":
            task.cancel()
        try:
            await task
        except BaseException as error:
            return error.evidence
        raise AssertionError("cancelled native request completed")

    with environment(tmp_path, mutate=mutate) as e:
        try:
            receipt = asyncio.run(run(e["options"]))
            assert len(e["requests"]) == 3 and len(receipt["commands"]) == 2
            assert len(e["store"].snapshot()["business"]["history"]) == 1
            assert receipt["cleanup_confirmed"], receipt["lifecycle"]
            assert receipt["calls"][-1]["response"] is None
            assert receipt["reported_usage"]["input"]["known_subtotal"] == 20
            assert receipt["reported_usage"]["input"]["unknown"] == 1
        finally:
            release.set()


def test_cancellation_revokes_authority_before_genuinely_stopped_normal_cli_reads_queued_deploy(
    monkeypatch, tmp_path
):
    allocate, close = OwnedCLI.allocate, OwnedCLI.close
    cancellation = Cancellation()
    stopped = []

    async def pause_child(self, record, args):
        entry = await allocate(self, record, args)
        if record["name"] == "deploy_version":
            os.kill(entry["process"].pid, signal.SIGSTOP)
            stopped.append(entry)
        return entry

    async def resume_after_durable_fence(self):
        if stopped:
            entry = stopped[0]
            assert entry["record"].get("stdin_written")
            assert (
                self.owner.store.current_operation(
                    self.owner.credential, allow_closed=True
                )["status"]
                == "closed"
            )
            os.kill(entry["process"].pid, signal.SIGCONT)
            await asyncio.wait_for(asyncio.shield(entry["exit"]), 5)
        await close(self)

    monkeypatch.setattr(OwnedCLI, "allocate", pause_child)
    monkeypatch.setattr(OwnedCLI, "close", resume_after_durable_fence)

    async def run(options):
        task = asyncio.create_task(
            run_agent_async(**{**options, "cancellation": cancellation})
        )
        for _ in range(1000):
            if stopped and stopped[0]["record"].get("stdin_written"):
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError(
                "actual stopped child never received queued private input"
            )
        cancellation.cancel("cancel while actual child is stopped")
        try:
            await task
        except BaseException as error:
            return error.evidence
        raise AssertionError("stopped child cancellation completed")

    with environment(tmp_path) as e:
        try:
            receipt = asyncio.run(run(e["options"]))
            child = receipt["commands"][-1]
            assert child["pid"] == stopped[0]["process"].pid
            assert child["exit_observed"] and child["exit_code"] == 2
            assert (
                "operation_closed"
                in base64.b64decode(child["stdout"]["body_base64"]).decode()
            )
            assert not e["store"].snapshot()["business"]["history"]
            assert receipt["cleanup_confirmed"], receipt["lifecycle"]
        finally:
            for entry in stopped:
                if entry["process"].returncode is None:
                    os.kill(entry["process"].pid, signal.SIGCONT)
                    entry["process"].kill()


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_normal_agent_cli_signals_close_pending_native_request_and_keep_committed_effect(
    sig, tmp_path
):
    import subprocess

    entered, release = threading.Event(), threading.Event()

    def mutate(body, n, _request, _handler):
        if n == 3:
            entered.set()
            assert release.wait(15)
        return body

    with environment(tmp_path, mutate=mutate) as e:
        options = e["options"]
        env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR") if k in os.environ}
        env["OPENAI_API_KEY"] = KEY
        command = [
            sys.executable,
            "-I",
            "-m",
            "infra_ops.agent",
            "--db",
            str(options["database"]),
            "--request",
            options["request"],
            "--model",
            MODEL,
            "--base-url",
            options["base_url"],
            "--allow-writes",
            "--intent-json",
            json.dumps(options["intent"]),
            "--output",
            str(options["output_root"]),
        ]
        child = subprocess.Popen(
            command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            assert entered.wait(7)
            child.send_signal(sig)
            stdout, stderr = child.communicate(timeout=10)
            assert child.returncode == 1, (stdout, stderr)
            result = json.loads(stdout)
            receipt = json.loads(Path(result["receipt_path"]).read_text())
            assert result["cleanup_confirmed"], receipt["lifecycle"]
            assert result["business"]["outcome"] == "pending_health"
            assert len(result["acknowledged_effects"]) == 1
            assert len(e["store"].snapshot()["business"]["history"]) == 1
        finally:
            release.set()
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)


def test_real_sqlite_writer_lock_makes_scope_close_honestly_unconfirmed_after_committed_recovery(
    monkeypatch, tmp_path
):
    from infra_ops.store import DeploymentStore

    original = DeploymentStore.close_operation
    held, attempts = [], []

    def lock_before_close(self, credential):
        attempts.append(credential)
        db = sqlite3.connect(self.path, isolation_level=None)
        db.execute("BEGIN IMMEDIATE")
        held.append(db)
        return original(self, credential)

    monkeypatch.setattr(DeploymentStore, "close_operation", lock_before_close)
    with environment(tmp_path) as e:
        try:
            receipt = rejected(e["options"], "database is locked")
            assert len(attempts) == 1
            assert not receipt["cleanup_confirmed"]
            assert not receipt["lifecycle"]["operation_closed"]
            assert (
                receipt["lifecycle"]["cli_closed"]
                and receipt["lifecycle"]["http_transport_closed"]
            )
            assert len(receipt["acknowledged_effects"]) == 3
            assert receipt["observed_business"]["outcome"] == "verified_recovery"
        finally:
            for db in held:
                db.rollback()
                db.close()
        assert e["store"].snapshot()["records"]["operations"][0]["status"] == "active"
        original(e["store"], attempts[0])


def test_partial_real_sdk_client_construction_closes_allocated_owned_http_pool_once(
    monkeypatch, tmp_path
):
    from openai import AsyncOpenAI

    close = NativeTransport.aclose
    attempts = []

    def fail_init(self, **options):
        assert options["http_client"] is not None
        raise RuntimeError("controlled client construction failure")

    async def close_once(self):
        attempts.append(self)
        await close(self)

    monkeypatch.setattr(AsyncOpenAI, "__init__", fail_init)
    monkeypatch.setattr(NativeTransport, "aclose", close_once)
    with environment(tmp_path) as e:
        receipt = rejected(e["options"], "controlled client construction failure")
        assert len(attempts) == 1 and attempts[0].closed
        assert receipt["cleanup_confirmed"], receipt["lifecycle"]
        assert not receipt["calls"] and not receipt["commands"] and not e["requests"]
        assert e["store"].snapshot()["records"]["operations"][0]["status"] == "closed"


def test_actual_sdk_request_budget_reserves_last_response_for_completion(tmp_path):
    with environment(tmp_path, actions=[ACTIONS[0]] * 14) as e:
        receipt = rejected(
            {**e["options"], "allow_writes": False, "intent": None}, "final tool budget"
        )
        assert len(e["requests"]) == 14 and len(receipt["commands"]) == 13
        assert len(receipt["calls"]) == 14
        assert receipt["cleanup_confirmed"]
        assert not e["store"].snapshot()["business"]["history"]
