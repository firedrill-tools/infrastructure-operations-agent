"""Owned real HTTP transport checks original Responses before SDK delivery."""

from __future__ import annotations

import asyncio
import base64
import os
import time
from dataclasses import asdict, is_dataclass
from urllib.parse import urlsplit

import httpx2
from agents import RunHooks
from pydantic import BaseModel

from .contracts import MAX_REQUESTS, arguments
from .evidence import bytes_image, error_info, usage
from .source import parse_json
from .state import canonical


def configuration(model=None, api_key=None, base_url=None):
    model = os.getenv("INFRA_MODEL") if model is None else model
    key = os.getenv("OPENAI_API_KEY") if api_key is None else api_key
    if (
        type(model) is not str
        or not model
        or len(model) > 200
        or any(ord(c) < 33 or ord(c) > 126 for c in model)
    ):
        raise ValueError("INFRA_MODEL must explicitly name an enabled model")
    if (
        type(key) is not str
        or not key
        or len(key) > 1024
        or any(ord(c) < 33 or ord(c) > 126 for c in key)
    ):
        raise ValueError("OPENAI_API_KEY is required")
    url = (
        base_url
        if base_url is not None
        else os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    )
    if type(url) is not str or len(url) > 1024:
        raise ValueError("bounded explicit provider URL required")
    if any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise ValueError("provider URL contains unsupported whitespace/control")
    parsed = urlsplit(url)
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/v1"
        or not (
            parsed.scheme == "https"
            or parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        )
    ):
        raise ValueError(
            "provider requires exact HTTPS or owned loopback HTTP /v1 origin"
        )
    _ = parsed.port
    return {
        "model": model,
        "key": key,
        "base_url": str(httpx2.URL(url.rstrip("/") + "/")),
    }


def sdk_value(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return sdk_value(asdict(value))
    if isinstance(value, dict):
        return {k: sdk_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sdk_value(v) for v in value]
    return value


def text_id(value):
    if (
        type(value) is not str
        or not value
        or len(value) > 240
        or any(ord(c) < 33 or ord(c) > 126 for c in value)
    ):
        raise ValueError("original bounded native identifier required")
    return value


def well_formed(value, depth=0):
    if depth > 64:
        raise ValueError("original JSON exceeds nesting bound")
    if type(value) is str:
        value.encode("utf-8", errors="strict")
    elif type(value) is dict:
        for key, item in value.items():
            well_formed(key, depth + 1)
            well_formed(item, depth + 1)
    elif type(value) is list:
        for item in value:
            well_formed(item, depth + 1)


def completed(body):
    if (
        type(body) is not dict
        or body.get("status") != "completed"
        or body.get("error") is not None
        or body.get("incomplete_details") is not None
    ):
        raise ValueError("original Responses generation is not fully completed")
    text_id(body.get("id"))
    text_id(body.get("model"))
    output = body.get("output")
    if type(output) is not list or not 1 <= len(output) <= 128:
        raise ValueError("complete bounded original output required")
    functions, messages, ids = [], [], set()
    for item in output:
        if type(item) is not dict:
            raise ValueError("original native output item required")
        item_id = text_id(item.get("id"))
        if item_id in ids:
            raise ValueError("duplicate original native output item")
        ids.add(item_id)
        kind = item.get("type")
        if kind == "reasoning":
            if item.get("status") not in (None, "completed"):
                raise ValueError("original reasoning item is unfinished")
        elif kind == "function_call":
            if item.get("status") != "completed":
                raise ValueError("original function-call item is unfinished")
            call_id, name = text_id(item.get("call_id")), item.get("name")
            if (
                type(item.get("arguments")) is not str
                or len(item["arguments"].encode()) > 8192
            ):
                raise ValueError("original strict function JSON arguments required")
            args = arguments(name, parse_json(item["arguments"]))
            functions.append(
                {
                    "kind": "tool",
                    "id": item_id,
                    "call_id": call_id,
                    "name": name,
                    "args": args,
                }
            )
        elif kind == "message":
            content = item.get("content")
            if (
                item.get("status") != "completed"
                or item.get("role") != "assistant"
                or type(content) is not list
                or not content
                or any(
                    type(p) is not dict
                    or p.get("type") != "output_text"
                    or type(p.get("text")) is not str
                    for p in content
                )
            ):
                raise ValueError("original message is unfinished/refused/malformed")
            text = "".join(p["text"] for p in content)
            if not text.strip():
                raise ValueError("original final answer is blank")
            messages.append({"kind": "answer", "id": item_id, "text": text})
        else:
            raise ValueError("unsupported original native output kind")
    if len(functions) == 1 and not messages:
        return functions[0]
    if len(messages) == 1 and not functions:
        return messages[0]
    raise ValueError(
        "original output is parallel/ambiguous or lacks completed action/answer"
    )


class NativeTransport(httpx2.AsyncBaseTransport):
    def __init__(self, owner, config):
        self.owner, self.config = owner, config
        self.inner = httpx2.AsyncHTTPTransport(
            trust_env=False,
            retries=0,
            http2=False,
            limits=httpx2.Limits(max_connections=1, max_keepalive_connections=1),
        )
        self.pending = set()
        self.active = self.close_attempted = self.closed = False
        self.response_ids, self.call_ids, self.item_ids = set(), set(), set()

    async def handle_async_request(self, request):
        owner, receipt = self.owner, self.owner.receipt
        owner.basic_guard()
        if self.active or self.close_attempted:
            raise ValueError("parallel or closing native HTTP request")
        if len(receipt.data["calls"]) >= MAX_REQUESTS:
            raise ValueError("prepared native request budget exhausted")
        if owner.authority is not None and not owner.authority["consumed"]:
            raise ValueError("previous original native action was not consumed")
        call = {
            "number": len(receipt.data["calls"]) + 1,
            "status": "prepared",
            "request": None,
            "response": None,
            "reported_usage": usage(None),
            "accepted": None,
            "error": None,
        }
        receipt.data["calls"].append(call)
        receipt.save()
        self.active = True
        task = asyncio.current_task()
        self.pending.add(task)
        response = None
        try:
            phase = min(30, owner.deadline - time.monotonic())
            if phase <= 0:
                raise TimeoutError("infrastructure native deadline expired")
            async with asyncio.timeout(phase):
                if (
                    request.method != "POST"
                    or str(request.url) != self.config["base_url"] + "responses"
                    or request.headers.get("authorization")
                    != "Bearer " + self.config["key"]
                ):
                    raise ValueError("unqualified native endpoint/method/credential")
                raw = await request.aread()
                call["request"] = bytes_image(
                    raw[: 2 * 1024 * 1024],
                    complete=True,
                    observed_bytes=len(raw),
                    truncated=len(raw) > 2 * 1024 * 1024,
                )
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError("native request exceeds2MiB retained limit")
                body = parse_json(raw.decode("utf-8", errors="strict"))
                well_formed(body)
                call["request"]["body"] = body
                if (
                    type(body) is not dict
                    or body.get("model") != self.config["model"]
                    or body.get("stream") is not None
                    and body["stream"] is not False
                    or type(body.get("max_output_tokens")) is not int
                    or body.get("max_output_tokens") != 4096
                    or body.get("parallel_tool_calls") is not False
                    or body.get("store") is not False
                ):
                    raise ValueError(
                        "native request model/output/parallel/store bounds differ"
                    )
                owner.guard()
                receipt.save()
                response = await self.inner.handle_async_request(request)
                record = {
                    "http_status": response.status_code,
                    "headers": {
                        k: response.headers[k]
                        for k in (
                            "content-type",
                            "content-encoding",
                            "request-id",
                            "x-request-id",
                        )
                        if k in response.headers
                    },
                    "complete": False,
                    "bytes": None,
                    "body": None,
                }
                call["response"] = record
                parts, total, truncated = [], 0, False
                try:
                    async for chunk in response.aiter_bytes():
                        room = max(0, 1024 * 1024 - total)
                        parts.append(chunk[:room])
                        total += len(chunk)
                        if total > 1024 * 1024:
                            truncated = True
                            raise ValueError(
                                "native decoded response exceeds1MiB retained limit"
                            )
                    record["complete"] = True
                finally:
                    captured = b"".join(parts)
                    record["bytes"] = bytes_image(
                        captured,
                        complete=record["complete"],
                        observed_bytes=total,
                        truncated=truncated,
                    )
                    receipt.save()
                if (
                    response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                    != "application/json"
                ):
                    raise ValueError("native provider did not return original JSON")
                parsed = parse_json(captured.decode("utf-8", errors="strict"))
                record["body"] = parsed
                call["reported_usage"] = usage(parsed)
                receipt.save()
                if not 200 <= response.status_code < 300:
                    raise ValueError(f"native provider HTTP{response.status_code}")
                well_formed(parsed)
                accepted = completed(parsed)
                if (
                    parsed["id"] in self.response_ids
                    or accepted["id"] in self.item_ids
                    or accepted["kind"] == "tool"
                    and (
                        accepted["call_id"] in self.call_ids
                        or call["number"] == MAX_REQUESTS
                    )
                ):
                    raise ValueError(
                        "native response/action is replayed or exceeds final tool budget"
                    )
                owner.guard()
                self.response_ids.add(parsed["id"])
                self.item_ids.add(accepted["id"])
                if accepted["kind"] == "tool":
                    self.call_ids.add(accepted["call_id"])
                call.update(status="accepted", accepted=accepted)
                owner.authority = (
                    {
                        "request_number": call["number"],
                        "accepted": accepted,
                        "consumed": False,
                    }
                    if accepted["kind"] == "tool"
                    else None
                )
                owner.original_answer = (
                    accepted["text"] if accepted["kind"] == "answer" else None
                )
                receipt.save()
                owner.basic_guard()
                # The original decoded bytes were checked before SDK normalization.
                headers = [
                    (k, v)
                    for k, v in response.headers.multi_items()
                    if k.lower() not in {"content-encoding", "content-length"}
                ]
                return httpx2.Response(
                    response.status_code,
                    headers=headers,
                    content=captured,
                    request=request,
                )
        except BaseException as error:
            call.update(status="failed", error=error_info(error))
            try:
                receipt.save()
            except BaseException:
                pass
            owner.fail(error)
            raise
        finally:
            if response is not None:
                try:
                    await response.aclose()
                except BaseException as error:
                    owner.fail(error)
            self.pending.discard(task)
            self.active = False

    async def aclose(self):
        if self.close_attempted:
            raise ValueError("native transport close already attempted")
        self.close_attempted = True
        await self.inner.aclose()
        if self.pending:
            raise ValueError("native HTTP work not drained")
        self.closed = True


class NativeHooks(RunHooks):
    def __init__(self, owner):
        self.owner = owner

    async def on_llm_start(self, context, agent, system_prompt, input_items):
        self.owner.guard()

    async def on_llm_end(self, context, agent, response):
        owner = self.owner
        try:
            owner.guard()
            call = owner.receipt.data["calls"][-1]
            output = [sdk_value(item) for item in response.output]
            owner.receipt.data["sdk"].append(
                {
                    "request_number": call["number"],
                    "response_id": response.response_id,
                    "output": output,
                    "usage": sdk_value(response.usage),
                    "raw_usage": sdk_value(getattr(response, "raw_usage", None)),
                }
            )
            if response.response_id != call["response"]["body"]["id"]:
                raise ValueError(
                    "SDK response ID differs from original native response"
                )
            normalized = completed(
                {
                    "id": response.response_id,
                    "model": call["response"]["body"]["model"],
                    "status": "completed",
                    "output": output,
                }
            )
            if canonical(normalized) != canonical(call["accepted"]):
                raise ValueError(
                    "SDK normalized action/answer differs from original native output"
                )
            owner.receipt.save()
        except BaseException as error:
            owner.fail(error)
            raise
