"""Private model-free population initialization, inspection and preserving reset."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import state
from .authority import sealed, checked_hash, same
from .evidence import publish
from .populations import dataset, definition, fixture_bytes, reference
from .source import keys, read_regular, parse_json
from .store import DeploymentStore


def root_path(path):
    supplied = Path(path).absolute()
    if not stat.S_ISDIR(supplied.lstat().st_mode):
        raise ValueError("original regular nonsymlink case root required")
    return supplied.resolve()


@contextmanager
def control_lock(root):
    lock = root / ".case-control.lock"
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    identity = os.fstat(fd)
    try:
        os.write(fd, str(uuid.uuid4()).encode())
        os.fsync(fd)
        yield
    finally:
        os.close(fd)
        current = lock.lstat()
        if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
            raise ValueError("private case control mutex identity changed")
        lock.unlink()


def initialize(root, case_id, *, reset_of=None, generation=1):
    case = definition(case_id)
    supplied = Path(root).absolute()
    supplied.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = supplied.parent.resolve() / supplied.name
    root.mkdir(
        mode=0o700
    )  # Exclusive including files/symlinks; preserve failed attempts.
    fixture = root / "fixture.json"
    fd = os.open(fixture, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(fixture_bytes(case))
        output.flush()
        os.fsync(output.fileno())
    store = DeploymentStore.seed(root / "deployments.db", fixture)
    for action in case["setup"]:
        current = store.status(action["service"])  # Actual fresh synthetic-human reads.
        if action["kind"] == "deploy":
            store.deploy(action["service"], action["version"], current["revision"])
        elif action["kind"] == "health":
            store.health_check(action["service"], current["revision"])
        elif action["kind"] == "rollback":
            store.rollback(action["service"], current["revision"])
        else:
            raise ValueError("unknown pinned population authoring action")
    before = store.snapshot()
    ref = reference(case)
    seed = sealed(
        {
            "schema_version": 1,
            "instance_id": store.instance_id,
            "reference": ref,
            "original": before["original"],
            "original_sha256": before["sha256"],
            "context_sha256": before["context_sha256"],
            "provenance": dataset()["provenance"],
        }
    )
    # Private initialization publishes source pointer and original baseline together.
    # Normal source/agent tools cannot call this initialization-only transaction.
    conn = sqlite3.connect(
        store.path.as_uri() + "?mode=rw", uri=True, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("BEGIN IMMEDIATE")
        current = state.snapshot(conn, store.identity, store.instance_id)
        if current["sha256"] != before["sha256"]:
            raise ValueError("original deployment source changed during initialization")
        doc = {**before["source"], "creation": "population", "population": ref}
        conn.execute(
            "UPDATE metadata SET document=? WHERE id=1", (state.canonical(doc),)
        )
        conn.execute("INSERT INTO seed_state VALUES (1,?)", (state.canonical(seed),))
        state.snapshot(conn, store.identity, store.instance_id)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    store = DeploymentStore(store.path)
    control = sealed(
        {
            "schema_version": 1,
            "kind": "private_synthetic_deployment_case",
            "root": str(root),
            "root_identity": f"{root.stat().st_dev}:{root.stat().st_ino}",
            "database": "deployments.db",
            "instance_id": store.instance_id,
            "reference": ref,
            "seed_sha256": seed["content_sha256"],
            "source_document_sha256": state.sha256(
                store.snapshot()["original"]["source_document"].encode()
            ),
            "generation": generation,
            "reset_of": reset_of,
        }
    )
    publish(root / "control.json", control)
    return _inspect(root)


def _inspect(root):
    control = keys(
        parse_json(
            read_regular(root / "control.json").decode("utf-8", errors="strict")
        ),
        {
            "schema_version",
            "kind",
            "root",
            "root_identity",
            "database",
            "instance_id",
            "reference",
            "seed_sha256",
            "source_document_sha256",
            "generation",
            "reset_of",
            "content_sha256",
        },
    )
    checked_hash(control)
    identity = f"{root.stat().st_dev}:{root.stat().st_ino}"
    if (
        type(control["schema_version"]) is not int
        or control["schema_version"] != 1
        or control["kind"] != "private_synthetic_deployment_case"
        or control["root"] != str(root)
        or control["root_identity"] != identity
        or control["database"] != "deployments.db"
        or type(control["generation"]) is not int
        or not 1 <= control["generation"] <= 2**31 - 1
    ):
        raise ValueError("original private case control identity differs")
    state.identifier(control["instance_id"])
    state.hex_digest(control["seed_sha256"])
    state.hex_digest(control["source_document_sha256"])
    if control["reset_of"] is not None:
        previous = keys(control["reset_of"], {"root", "instance_id", "source_sha256"})
        if type(previous["root"]) is not str or not previous["root"]:
            raise ValueError("original reset predecessor root required")
        state.identifier(previous["instance_id"])
        state.hex_digest(previous["source_sha256"])
    elif control["generation"] != 1:
        raise ValueError("reset generation has no predecessor")
    store = DeploymentStore(root / control["database"])
    source = store.snapshot()
    if (
        not source["source"]["population"]
        or not source["original"]["tables"]["seed_state"]
    ):
        raise ValueError("original private case source authority missing")
    keys(
        control["reference"],
        {"id", "version", "case_id", "dataset_sha256", "definition_sha256"},
    )
    case = definition(control["reference"]["case_id"])
    seed = state.bounded_json(source["original"]["tables"]["seed_state"][0]["document"])
    if (
        control["instance_id"] != store.instance_id
        or not same(control["reference"], reference(case))
        or not same(source["source"]["population"], control["reference"])
        or control["seed_sha256"] != seed["content_sha256"]
        or control["source_document_sha256"]
        != state.sha256(source["original"]["source_document"].encode())
        or read_regular(root / "fixture.json") != fixture_bytes(case)
    ):
        raise ValueError("original private case control/source/seed differs")
    target = next(
        s
        for s in source["business"]["services"]
        if s["name"] == case["reference"]["service"]
    )
    intent = {
        k: case["reference"][k] for k in ("service", "version", "rollback_on_failure")
    }
    intent["expected_revision"] = target["revision"]
    return {
        "root": str(root),
        "database": str(store.path),
        "instance_id": store.instance_id,
        "case_id": case["id"],
        "profile": case["profile"],
        "title": case["title"],
        "generation": control["generation"],
        "reset_of": control["reset_of"],
        "request": case["request"],
        "intent": intent,
        "private_reference": case["reference"],
        "population": control["reference"],
        "seed_sha256": seed["content_sha256"],
        "source_sha256": source["sha256"],
        "metrics": {
            k: len(source["business"][k]) for k in ("services", "versions", "history")
        },
        "source": source,
        "delivery": {
            "real_cloud": None,
            "prior_model": None,
            "prior_human_authentication": None,
        },
        "authority": "private model-free inspection; no runtime read or write capability",
    }


def inspect_case(root):
    root = root_path(root)
    with control_lock(root):
        return _inspect(root)


def reset_case(root, new_root):
    root = root_path(root)
    target = Path(new_root).absolute()
    candidate = target.parent.resolve() / target.name
    if candidate.is_relative_to(root) or root.is_relative_to(candidate):
        raise ValueError(
            "reset requires a separate new case root; preserve original state and evidence"
        )
    with control_lock(root):
        previous = _inspect(root)
        if previous["generation"] == 2**31 - 1:
            raise ValueError("case reset generation exhausted")
        return initialize(
            candidate,
            previous["case_id"],
            generation=previous["generation"] + 1,
            reset_of={
                "root": str(root),
                "instance_id": previous["instance_id"],
                "source_sha256": previous["source_sha256"],
            },
        )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Private model-free synthetic deployment cases"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    init = commands.add_parser("init")
    init.add_argument("--case", required=True)
    init.add_argument("--root", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--root", required=True)
    reset = commands.add_parser("reset")
    reset.add_argument("--root", required=True)
    reset.add_argument("--new-root", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            result = {
                "id": dataset()["id"],
                "version": dataset()["version"],
                "cases": [
                    {k: c[k] for k in ("id", "profile", "title")}
                    for c in dataset()["cases"]
                ],
            }
        elif args.command == "init":
            result = initialize(args.root, args.case)
        elif args.command == "inspect":
            result = inspect_case(args.root)
        else:
            result = reset_case(args.root, args.new_root)
        print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
    except (OSError, ValueError, sqlite3.Error) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=True))
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
