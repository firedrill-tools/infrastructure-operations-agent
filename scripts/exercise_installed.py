"""Production dependency-only entry points, cases and normal SDK HTTP/CLI proof."""

import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import infra_ops

BASE = Path(sys.argv[1])
BASE.mkdir()
assert Path(infra_ops.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
if "--production-only" in sys.argv:
    assert importlib.util.find_spec("pytest") is None
BIN = Path(sys.executable).parent
ENV = {
    k: os.environ[k]
    for k in ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "SystemRoot", "WINDIR")
    if k in os.environ
}
KEY, MODEL = "synthetic-installed-infrastructure-key", "gpt-4.1-synthetic"


def execute(command, expected=0, env=ENV):
    run = subprocess.run(
        list(map(str, command)), cwd=BASE, env=env, capture_output=True, timeout=45
    )
    assert run.returncode == expected, (run.stdout[-3000:], run.stderr[-3000:])
    return json.loads(run.stdout)


manifest = execute([BIN / "infra-cases", "list"])
assert len(manifest["cases"]) == 12
bundles = []
for row in manifest["cases"]:
    bundle = execute(
        [BIN / "infra-cases", "init", "--case", row["id"], "--root", BASE / row["id"]]
    )
    inspected = execute([BIN / "infra-cases", "inspect", "--root", bundle["root"]])
    assert bundle == inspected
    assert not bundle["source"]["records"]["operations"]
    bundles.append(bundle)
assert bundles[3]["metrics"] == {"services": 24, "versions": 240, "history": 40}
old_bytes = (Path(bundles[0]["database"])).read_bytes()
reset = execute(
    [
        BIN / "infra-cases",
        "reset",
        "--root",
        bundles[0]["root"],
        "--new-root",
        BASE / "fresh-reset",
    ]
)
assert reset["instance_id"] != bundles[0]["instance_id"]
assert reset["source"]["records"]["observations"] == []
assert Path(bundles[0]["database"]).read_bytes() == old_bytes

launchers = BASE / "launchers"
launchers.mkdir()
launcher = launchers / "infra-agent"
launcher.symlink_to(BIN / "infra-agent")


def response(n, action):
    item = (
        {
            "id": f"fc_{n}",
            "type": "function_call",
            "status": "completed",
            "call_id": f"call_{n}",
            "name": action[0],
            "arguments": json.dumps(action[1]),
        }
        if action
        else {
            "id": f"msg_{n}",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": "The failed synthetic API rollout was recovered and final status and history were observed.",
                    "annotations": [],
                }
            ],
        }
    )
    return {
        "id": f"resp_{n}",
        "object": "response",
        "created_at": 1792110000,
        "status": "completed",
        "model": MODEL,
        "error": None,
        "incomplete_details": None,
        "output": [item],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
        "original_unknown": "retained",
    }


proof = []
for mode in ("completed", "incomplete-final", "SIGINT", "SIGTERM", "SIGHUP"):
    root = BASE / ("normal-" + mode)
    case = execute(
        [
            BIN / "infra-cases",
            "init",
            "--case",
            "starter_failed_recovery",
            "--root",
            root,
        ]
    )
    actions = [
        ("deployment_status", {"service": "api", "cached": False}),
        (
            "deploy_version",
            {"service": "api", "version": "2.1.0", "expected_revision": 1},
        ),
        ("deployment_status", {"service": "api", "cached": False}),
        ("check_rollout_health", {"service": "api", "expected_revision": 2}),
        ("deployment_status", {"service": "api", "cached": False}),
        ("rollback_version", {"service": "api", "expected_revision": 3}),
        ("deployment_status", {"service": "api", "cached": False}),
        ("deployment_history", {"service": "api"}),
    ]
    requests, entered, release, errors = [], threading.Event(), threading.Event(), []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                n = len(requests)
                assert self.path == "/v1/responses"
                assert self.headers.get("Authorization") == "Bearer " + KEY
                assert (
                    body["model"] == MODEL
                    and body["parallel_tool_calls"] is False
                    and body["store"] is False
                )
                assert body["max_output_tokens"] == 4096
                if mode.startswith("SIG") and n == 3:
                    entered.set()
                    assert release.wait(15)
                value = response(n, actions[n - 1] if n <= len(actions) else None)
                if mode == "incomplete-final" and n == 9:
                    value.update(
                        status="incomplete",
                        incomplete_details={"reason": "max_output_tokens"},
                    )
                raw = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except BaseException as error:
                errors.append(str(error))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    command = [
        launcher,
        "--db",
        case["database"],
        "--request",
        case["request"],
        "--model",
        MODEL,
        "--base-url",
        f"http://127.0.0.1:{server.server_port}/v1",
        "--allow-writes",
        "--intent-json",
        json.dumps(case["intent"]),
        "--output",
        root / "evidence",
    ]
    child = subprocess.Popen(
        list(map(str, command)),
        cwd=BASE,
        env={**ENV, "OPENAI_API_KEY": KEY},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        if mode.startswith("SIG"):
            assert entered.wait(8)
            child.send_signal(getattr(signal, mode))
        stdout, stderr = child.communicate(timeout=20)
        assert child.returncode == (0 if mode == "completed" else 1), (
            stdout[-3000:],
            stderr[-3000:],
        )
        result = json.loads(stdout)
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert receipt["cleanup_confirmed"], receipt["lifecycle"]
        assert len(receipt["acknowledged_effects"]) == (
            1 if mode.startswith("SIG") else 3
        )
        assert len(requests) == (3 if mode.startswith("SIG") else 9)
        assert all(c["pid"] > 0 and c["exit_observed"] for c in receipt["commands"])
        assert not errors, errors
        assert KEY not in json.dumps(receipt)
        if mode == "completed":
            assert result["business"]["outcome"] == "verified_recovery"
            assert (
                result["usage"]["input"]["value"] == 90 and result["cost_usd"] is None
            )
        if mode == "incomplete-final":
            assert (
                receipt["status"] == "failed"
                and receipt["observed_business"]["outcome"] == "verified_recovery"
            )
        proof.append(
            {
                "mode": mode,
                "requests": len(requests),
                "actual_cli_children": len(receipt["commands"]),
                "acknowledged_effects": len(receipt["acknowledged_effects"]),
                "cleanup_confirmed": receipt["cleanup_confirmed"],
                "outcome": receipt["observed_business"]["outcome"],
            }
        )
    finally:
        release.set()
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

seeded = execute([BIN / "infra-demo", "--db", BASE / "default.db", "seed"])
assert seeded["seeded"]
status = execute([BIN / "infra-demo", "--db", BASE / "default.db", "status", "api"])
assert status["version"] == "2.0.0"
missing = execute(
    [
        launcher,
        "--db",
        BASE / "default.db",
        "--model",
        MODEL,
        "--request",
        "Inspect synthetic status",
        "--output",
        BASE / "missing-key",
    ],
    expected=1,
)
receipt = json.loads(Path(missing["receipt_path"]).read_text())
assert (
    receipt["status"] == "failed" and not receipt["calls"] and not receipt["commands"]
)
assert receipt["cleanup_confirmed"] and not receipt["lifecycle"]["sdk_started"]
print(
    json.dumps(
        {
            "twelve_actual_private_cli_cases": len(bundles),
            "private_preserving_reset": True,
            "actual_installed_agent": proof,
            "installed_default_seed": True,
            "prepared_missing_key": True,
            "production_only": "--production-only" in sys.argv,
        }
    )
)
