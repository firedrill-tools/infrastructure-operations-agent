"""Actual owned fixed CLI children, bounded original streams and awaited exits."""

from __future__ import annotations

import asyncio
import os
import sys
import time

from .contracts import command, MAX_REQUESTS
from .evidence import bytes_image, error_info
from .source import parse_json
from .state import canonical, sha256


def minimal_environment():
    return {
        k: os.environ[k]
        for k in (
            "PATH",
            "HOME",
            "TMPDIR",
            "TMP",
            "TEMP",
            "USER",
            "LOGNAME",
            "LANG",
            "SystemRoot",
            "WINDIR",
        )
        if k in os.environ
    }


class OwnedCLI:
    def __init__(self, owner, database, directory):
        self.owner, self.database, self.directory = owner, str(database), str(directory)
        self.children, self.allocations, self.work = [], [], []
        self.active = False
        self.close_attempted = self.closed = False

    async def allocate(self, record, args):
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-m",
            "infra_ops.cli",
            "--db",
            self.database,
            "--owned-input",
            *args,
            cwd=self.directory,
            env=minimal_environment(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=65536,
        )
        entry = {
            "process": child,
            "record": record,
            "retained": 0,
            "buffers": {"stdout": [], "stderr": []},
            "seen": {"stdout": 0, "stderr": 0},
            "eof": {"stdout": False, "stderr": False},
            "truncated": False,
            "io": [],
        }
        self.children.append(entry)
        record["pid"] = child.pid

        async def wait_exit():
            code = await child.wait()
            record.update(exit_code=code, exit_observed=True)
            return code

        entry["exit"] = asyncio.create_task(wait_exit())
        for name in ("stdout", "stderr"):
            entry["io"].append(asyncio.create_task(self.read_stream(entry, name)))
        if self.close_attempted:
            child.terminate()
        return entry

    async def read_stream(self, entry, name):
        try:
            while True:
                chunk = await getattr(entry["process"], name).read(65536)
                if not chunk:
                    entry["eof"][name] = True
                    break
                entry["seen"][name] += len(chunk)
                retained = chunk[: max(0, 1024 * 1024 - entry["retained"])]
                entry["buffers"][name].append(retained)
                entry["retained"] += len(retained)
                if len(retained) != len(chunk) and not entry["truncated"]:
                    entry["truncated"] = True
                    self.owner.fail(
                        ValueError(
                            "actual CLI output exceeds combined1MiB retained bound"
                        )
                    )
        finally:
            entry["record"][name] = bytes_image(
                b"".join(entry["buffers"][name]),
                complete=entry["eof"][name],
                observed_bytes=entry["seen"][name],
                truncated=entry["truncated"],
            )

    async def execute(self, name, arguments, proof=None):
        self.owner.basic_guard()
        if self.active or self.close_attempted:
            raise ValueError("owned CLI is parallel or closing")
        self.active = True
        task = asyncio.create_task(self._execute(name, arguments, proof))
        self.work.append(task)
        # Keep real child/stream work owned if its SDK waiter is cancelled.
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and not task.cancelled():
                task.exception()

    async def _execute(self, name, arguments, proof):
        self.active = True
        receipt, record, entry = self.owner.receipt, None, None
        try:
            self.owner.basic_guard()
            args = command(name, arguments)
            if len(receipt.data["commands"]) >= MAX_REQUESTS - 1:
                raise ValueError("prepared CLI command budget exhausted")
            payload = (
                canonical({"credential": self.owner.credential, "proof": proof}) + "\n"
            ).encode()
            record = {
                "number": len(receipt.data["commands"]) + 1,
                "name": name,
                "args": args,
                "environment_keys": sorted(minimal_environment()),
                "status": "prepared",
                "pid": None,
                "exit_code": None,
                "exit_observed": False,
                "stdin": {
                    "bytes": len(payload),
                    "sha256": sha256(payload),
                    "opaque_credential_bytes_retained": False,
                },
                "stdout": None,
                "stderr": None,
            }
            receipt.data["commands"].append(record)
            receipt.save()
            allocation = asyncio.create_task(self.allocate(record, args))
            self.allocations.append(allocation)
            remaining = min(15, self.owner.deadline - time.monotonic())
            if remaining <= 0:
                raise TimeoutError(
                    "infrastructure deadline expired before CLI allocation"
                )
            async with asyncio.timeout(remaining):
                entry = await asyncio.shield(allocation)
                receipt.save()
                self.owner.basic_guard()
                if self.close_attempted:
                    raise ValueError("owned CLI closed before private input")
                entry["process"].stdin.write(payload)
                await entry["process"].stdin.drain()
                record["stdin_written"] = True
                entry["process"].stdin.close()
                await asyncio.shield(asyncio.gather(*entry["io"], entry["exit"]))
            self.owner.basic_guard()
            raw = b"".join(entry["buffers"]["stdout"])
            result = parse_json(raw.decode("utf-8", errors="strict"))
            record["parsed"] = result
            if (
                entry["truncated"]
                or not record["exit_observed"]
                or record["exit_code"] != 0
                or type(result) is not dict
                or "error" in result
            ):
                message = result.get("error") if type(result) is dict else None
                raise ValueError(
                    message
                    if type(message) is str and message
                    else "actual synthetic CLI failed without qualified result"
                )
            record["status"] = "finished"
            receipt.save()
            return result
        except BaseException as error:
            if record is not None:
                record.update(status="failed", error=error_info(error))
                try:
                    receipt.save()
                except BaseException:
                    pass
            self.owner.fail(error)
            raise
        finally:
            self.active = False

    async def close(self):
        if self.close_attempted:
            raise ValueError("owned CLI close already attempted")
        self.close_attempted = True
        failure = None
        for allocation in self.allocations:
            try:
                await asyncio.wait_for(asyncio.shield(allocation), 3)
            except asyncio.TimeoutError as error:
                failure = failure or error
                allocation.cancel()
                await asyncio.gather(allocation, return_exceptions=True)
            except BaseException:
                # A failed allocation can still have published a tracked real child.
                pass
        for entry in self.children:
            child = entry["process"]
            if child.returncode is None:
                try:
                    child.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(asyncio.shield(entry["exit"]), 1)
                except asyncio.TimeoutError:
                    try:
                        child.kill()
                    except ProcessLookupError:
                        pass
            try:
                await asyncio.wait_for(
                    asyncio.shield(asyncio.gather(*entry["io"], entry["exit"])), 3
                )
            except BaseException as error:
                failure = failure or error
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.gather(*self.work, return_exceptions=True)), 3
            )
        except BaseException as error:
            failure = failure or error
        if (
            any(not e["record"]["exit_observed"] for e in self.children)
            or failure is not None
        ):
            raise failure or ValueError("actual CLI child exit unconfirmed")
        self.closed = True
        try:
            self.owner.receipt.save()
        except BaseException:
            pass
