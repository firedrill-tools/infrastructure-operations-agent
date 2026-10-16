"""Bounded original fictional source; exact values are never coerced."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

MAX_INTEGER = 2**53 - 1
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_SERVICES = 64
MAX_VERSIONS = 64


def parse_json(text: str) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate original JSON key")
            result[key] = value
        return result

    def constant(_value):
        raise ValueError("nonfinite original JSON value")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def read_regular(path: str | Path) -> bytes:
    path = Path(path)
    initial = path.lstat()
    if not stat.S_ISREG(initial.st_mode) or initial.st_size > MAX_SOURCE_BYTES:
        raise ValueError("bounded regular nonsymlink source file required")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        with os.fdopen(os.dup(fd), "rb") as source:
            raw = source.read(MAX_SOURCE_BYTES + 1)
        after, current = os.fstat(fd), path.lstat()
        if (
            len(raw) > MAX_SOURCE_BYTES
            or (initial.st_dev, initial.st_ino) != (before.st_dev, before.st_ino)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
            or not stat.S_ISREG(current.st_mode)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or len(raw) != before.st_size
        ):
            raise ValueError("original source changed while reading")
        return raw
    finally:
        os.close(fd)


def service_name(value: Any) -> str:
    if type(value) is not str or not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_-]{0,63}", value
    ):
        raise ValueError("canonical service identifier required")
    return value


def stable_version(value: Any) -> str:
    if (
        type(value) is not str
        or not re.fullmatch(
            r"(?:0|[1-9][0-9]{0,15})(?:\.(?:0|[1-9][0-9]{0,15})){2}", value
        )
        or any(int(part) > MAX_INTEGER for part in value.split("."))
    ):
        raise ValueError("canonical safe stable version required")
    return value


def revision(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= MAX_INTEGER:
        raise ValueError("exact positive safe revision required")
    return value


def next_revision(value: Any) -> int:
    current = revision(value)
    if current == MAX_INTEGER:
        raise ValueError("revision_exhausted")
    return current + 1


def keys(
    value: Any, required: set[str], optional: set[str] | frozenset[str] = frozenset()
) -> dict:
    if (
        type(value) is not dict
        or not required <= value.keys()
        or value.keys() - required - optional
    ):
        raise ValueError("original source fields differ from declared schema")
    return value


def validate_fixture(value: Any) -> dict:
    data = keys(value, {"services", "allowed_versions"}, {"failing_versions"})
    services, versions = data["services"], data["allowed_versions"]
    failures = data.get("failing_versions", {})
    if type(services) is not list or not 1 <= len(services) <= MAX_SERVICES:
        raise ValueError("bounded nonempty synthetic services required")
    known = set()
    for item in services:
        keys(item, {"name", "version", "healthy", "revision"})
        name = service_name(item["name"])
        stable_version(item["version"])
        revision(item["revision"])
        if type(item["healthy"]) is not bool or name in known:
            raise ValueError("unique service and exact boolean health required")
        known.add(name)
    if type(versions) is not dict or set(versions) != known:
        raise ValueError("allowed version catalog must match every exact service")
    if type(failures) is not dict or not set(failures) <= known:
        raise ValueError("failure rules must refer to known services")
    for name, rows in versions.items():
        if type(rows) is not list or not 1 <= len(rows) <= MAX_VERSIONS:
            raise ValueError("bounded version catalog required")
        checked = [stable_version(v) for v in rows]
        if len(set(checked)) != len(checked):
            raise ValueError("duplicate allowed version")
        failing = failures.get(name, [])
        if type(failing) is not list:
            raise ValueError("exact failure version list required")
        failed = [stable_version(v) for v in failing]
        if len(set(failed)) != len(failed) or not set(failed) <= set(checked):
            raise ValueError("failure rules differ from allowed versions")
    for item in services:
        if item["version"] not in versions[item["name"]]:
            raise ValueError("initial service version missing from allowed catalog")
        if item["healthy"] and item["version"] in failures.get(item["name"], []):
            raise ValueError(
                "healthy initial service contradicts deterministic failure rule"
            )
    return data


def load_fixture(path: str | Path) -> tuple[bytes, dict]:
    raw = read_regular(path)
    return raw, validate_fixture(parse_json(raw.decode("utf-8", errors="strict")))
