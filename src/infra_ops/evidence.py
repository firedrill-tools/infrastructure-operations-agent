"""Private original source/native/process evidence, independent of business commit."""

from __future__ import annotations

import base64
import json
import os
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .state import digest, sha256
from .source import MAX_INTEGER


def now():
    return datetime.now(timezone.utc).isoformat()


def error_info(error):
    seen, chain = set(), []
    while error is not None and id(error) not in seen and len(chain) < 8:
        seen.add(id(error))
        chain.append({"name": type(error).__name__, "message": str(error)})
        error = error.__cause__
    return chain


def counter(body, *path):
    value = body
    for part in path:
        if type(value) is not dict or part not in value:
            return {"status": "unknown", "value": None}
        value = value[part]
    return (
        {"status": "observed", "value": value}
        if type(value) is int and 0 <= value <= MAX_INTEGER
        else {"status": "invalid", "value": None}
    )


def usage(body):
    return {
        "input": counter(body, "usage", "input_tokens"),
        "output": counter(body, "usage", "output_tokens"),
        "total": counter(body, "usage", "total_tokens"),
        "cached_input": counter(body, "usage", "input_tokens_details", "cached_tokens"),
        "reasoning_output": counter(
            body, "usage", "output_tokens_details", "reasoning_tokens"
        ),
    }


def aggregate(calls):
    result = {
        "prepared_attempts": len(calls),
        "source": "original Responses JSON counters; no estimation or billing evidence",
        "cost_usd": None,
    }
    for key in usage(None):
        values = [c.get("reported_usage", usage(None))[key] for c in calls]
        observed = [c["value"] for c in values if c["status"] == "observed"]
        subtotal = sum(observed)
        result[key] = {
            "value": (
                subtotal
                if values and len(observed) == len(values) and subtotal <= MAX_INTEGER
                else None
            ),
            "known_subtotal": subtotal if subtotal <= MAX_INTEGER else None,
            "observed": len(observed),
            "unknown": sum(c["status"] == "unknown" for c in values),
            "invalid": sum(c["status"] == "invalid" for c in values),
            "complete": bool(values)
            and len(observed) == len(values)
            and subtotal <= MAX_INTEGER,
        }
    return result


def bytes_image(raw, *, complete=True, observed_bytes=None, truncated=False):
    return {
        "body_base64": base64.b64encode(raw).decode(),
        "retained_bytes": len(raw),
        "observed_bytes": len(raw) if observed_bytes is None else observed_bytes,
        "complete": complete,
        "truncated": truncated,
        "sha256": sha256(raw) if complete and not truncated else None,
        "prefix_sha256": sha256(raw),
    }


def publish(path, data):
    # ASCII JSON preserves arbitrary decoded error strings; original UTF8 bytes
    # remain separately retained in source/native/process images.
    raw = (
        json.dumps(data, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
    ).encode()
    if len(raw) > 64 * 1024 * 1024:
        raise ValueError("complete private receipt exceeds64MiB; no truncation")
    temporary = path.parent / (".trace-" + str(uuid.uuid4()) + ".tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


class Receipt:
    def __init__(self, root, initial, writer=None):
        supplied = Path(root).absolute()
        supplied.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not stat.S_ISDIR(supplied.lstat().st_mode):
            raise ValueError("regular private receipt root required")
        root = supplied.resolve()
        directory = root / str(uuid.uuid4())
        directory.mkdir(mode=0o700)
        self.path = directory / "trace.json"
        self.recording_error = None
        self.writer = writer or publish
        self.data = {
            "schema_version": 1,
            "run_id": directory.name,
            "receipt_path": str(self.path),
            "status": "prepared",
            "started_at": now(),
            "finished_at": None,
            "calls": [],
            "commands": [],
            "actions": [],
            "sdk": [],
            "source_images": {},
            "initial_source": None,
            "final_source": None,
            "reported_usage": aggregate([]),
            "result": None,
            "error": None,
            "cleanup_confirmed": False,
            **initial,
        }
        try:
            self.save()
        except BaseException as error:
            error._infra_receipt = self
            raise

    def save(self):
        if self.recording_error is not None:
            raise self.recording_error
        self.data["reported_usage"] = aggregate(self.data["calls"])
        try:
            self.writer(self.path, self.data)
        except BaseException as error:
            self.recording_error = error
            self.data["durable_status"] = "unknown"
            raise

    def source(self, snapshot):
        key = snapshot["sha256"]
        if key != digest(snapshot["original"]):
            raise ValueError("original source image hash differs")
        self.data["source_images"].setdefault(
            key,
            {
                "original": snapshot["original"],
                "business": snapshot["business"],
                "context_sha256": snapshot["context_sha256"],
            },
        )
        return {"sha256": key, "context_sha256": snapshot["context_sha256"]}
