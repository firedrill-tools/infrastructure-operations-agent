"""Actual owned HTTP provider controls; never a substituted model/SDK/CLI."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from infra_ops.store import DeploymentStore

KEY = "synthetic-infrastructure-owned-key"
MODEL = "gpt-4.1-synthetic"
INTENT = {
    "service": "api",
    "version": "2.1.0",
    "expected_revision": 1,
    "rollback_on_failure": True,
}
ACTIONS = [
    ("deployment_status", {"service": "api", "cached": False}),
    ("deploy_version", {"service": "api", "version": "2.1.0", "expected_revision": 1}),
    ("deployment_status", {"service": "api", "cached": False}),
    ("check_rollout_health", {"service": "api", "expected_revision": 2}),
    ("deployment_status", {"service": "api", "cached": False}),
    ("rollback_version", {"service": "api", "expected_revision": 3}),
    ("deployment_status", {"service": "api", "cached": False}),
    ("deployment_history", {"service": "api"}),
]
FIXTURE = Path(__file__).parents[1] / "fixtures/starter.json"


def reply(n, action, model=MODEL):
    output = (
        [
            {
                "type": "function_call",
                "id": f"fc_{n}",
                "call_id": f"call_{n}",
                "status": "completed",
                "name": action[0],
                "arguments": json.dumps(action[1]),
            }
        ]
        if action
        else [
            {
                "type": "message",
                "id": f"msg_{n}",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "The synthetic rollout failed health and was rolled back. The final local API state and complete history were verified; no real cloud deployment occurred.",
                        "annotations": [],
                    }
                ],
            }
        ]
    )
    return {
        "id": f"resp_{n}",
        "object": "response",
        "created_at": 1792110000,
        "model": model,
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "output": output,
        "usage": {
            "input_tokens": 10,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 2,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 12,
        },
        "unknown_original": {"retained": True},
    }


@contextmanager
def environment(
    tmp_path,
    actions=ACTIONS,
    mutate=lambda body, n, request, handler: body,
    database=None,
):
    store = (
        DeploymentStore(database)
        if database is not None
        else DeploymentStore.seed(tmp_path / "state.db", FIXTURE)
    )
    requests = []
    errors = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                request = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                requests.append(
                    {
                        "path": self.path,
                        "body": request,
                        "key_matches": self.headers.get("Authorization")
                        == "Bearer " + KEY,
                        "organization": self.headers.get("OpenAI-Organization"),
                        "project": self.headers.get("OpenAI-Project"),
                    }
                )
                n = len(requests)
                assert self.path == "/v1/responses"
                assert requests[-1]["key_matches"]
                assert request["model"] == MODEL
                assert request["max_output_tokens"] == 4096
                assert request["parallel_tool_calls"] is False
                assert request["store"] is False
                assert {t["name"] for t in request["tools"]} == {
                    "deployment_status",
                    "deployment_history",
                    "deploy_version",
                    "check_rollout_health",
                    "rollback_version",
                }
                body = mutate(
                    reply(
                        n,
                        actions[n - 1] if n <= len(actions) else None,
                        request["model"],
                    ),
                    n,
                    request,
                    self,
                )
                if body is None:
                    return
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("x-request-id", f"original-http-{n}")
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except BaseException as error:
                errors.append(str(error))
                try:
                    raw = json.dumps({"error": {"message": str(error)}}).encode()
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

    class Server(ThreadingHTTPServer):
        daemon_threads = False

    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield {
            "store": store,
            "requests": requests,
            "errors": errors,
            "options": {
                "database": store.path,
                "request": "Deploy only API2.1.0; read fresh status before every change, check health, recover only this failed rollout if allowed, reread final state and full history.",
                "model": MODEL,
                "allow_writes": True,
                "intent": INTENT,
                "api_key": KEY,
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                "output_root": tmp_path / "receipts",
            },
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def source(receipt, ref):
    return None if ref is None else receipt["source_images"][ref["sha256"]]
