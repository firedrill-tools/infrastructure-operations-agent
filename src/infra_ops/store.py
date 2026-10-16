"""Managed fictional deployment source and atomic scoped SQLite transitions."""

from __future__ import annotations

import base64
import os
import secrets
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from .source import load_fixture, revision, service_name, stable_version
from . import state
from .authority import (
    intent as validate_intent,
    operation,
    same,
    sealed,
    status_value,
    history_value,
    workflow,
)


class DeploymentStore:
    def __init__(self, path: str | Path, *, timeout: float = 5.0):
        supplied = Path(path).absolute()
        self.path = supplied.parent.resolve() / supplied.name
        self.timeout = timeout
        if type(timeout) not in (int, float) or not 0 <= timeout <= 5:
            raise ValueError("bounded SQLite lock timeout required")
        self.instance_id = None
        self.source_sha256 = None
        self.identity = self._file_identity()
        self.commit_guard = lambda: None
        with self._connect() as (_conn, snapshot):
            self.instance_id = snapshot["source"]["instance_id"]
            self.source_sha256 = state.sha256(
                snapshot["original"]["source_document"].encode()
            )

    def _file_identity(self):
        try:
            file = self.path.lstat()
            parent = self.path.parent.stat()
        except FileNotFoundError:
            raise ValueError(
                "managed deployment database missing; use a fresh seed"
            ) from None
        if (
            not stat.S_ISREG(file.st_mode)
            or file.st_nlink != 1
            or not 0 < file.st_size <= state.MAX_DATABASE_BYTES
            or self.path.parent.resolve() != self.path.parent
        ):
            raise ValueError(
                "bounded regular unshared nonsymlink deployment database required"
            )
        return {
            "database": f"{file.st_dev}:{file.st_ino}",
            "parent": f"{parent.st_dev}:{parent.st_ino}",
            "name": self.path.name,
        }

    def _guard_file(self):
        if self._file_identity() != self.identity:
            raise ValueError("managed deployment database/root identity changed")

    @classmethod
    def seed(cls, path: str | Path, fixture: str | Path) -> "DeploymentStore":
        raw, data = load_fixture(fixture)
        supplied = Path(path).absolute()
        supplied.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = supplied.parent.resolve() / supplied.name
        fd = os.open(db, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        original = os.fstat(fd)
        os.close(fd)
        parent = db.parent.stat()
        identity = {
            "database": f"{original.st_dev}:{original.st_ino}",
            "parent": f"{parent.st_dev}:{parent.st_ino}",
            "name": db.name,
        }
        document = {
            "schema_version": state.SCHEMA_VERSION,
            "synthetic": True,
            "instance_id": str(uuid.uuid4()),
            "identity": identity,
            "fixture_base64": base64.b64encode(raw).decode(),
            "fixture_sha256": state.sha256(raw),
            "provenance": {
                "kind": "explicit_synthetic_fixture_author",
                "prior_model_evidence": None,
                "prior_human_authentication": None,
                "prior_cloud_delivery": None,
            },
            "population": None,
            "creation": "fixture",
        }
        conn = sqlite3.connect(db.as_uri() + "?mode=rw", uri=True, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(f"PRAGMA application_id={state.APPLICATION_ID}")
            conn.execute(f"PRAGMA user_version={state.SCHEMA_VERSION}")
            for sql in state.TABLES.values():
                conn.execute(sql)
            conn.execute(
                "INSERT INTO metadata VALUES (1,?)", (state.canonical(document),)
            )
            for item in state.initial_services(data):
                conn.execute(
                    "INSERT INTO services VALUES (?,?,?,?,?)",
                    tuple(
                        item[k]
                        for k in (
                            "name",
                            "version",
                            "previous_version",
                            "health",
                            "revision",
                        )
                    ),
                )
                conn.execute(
                    "INSERT INTO status_cache VALUES (?,?)",
                    (item["name"], state.canonical(item)),
                )
            for item in state.version_rows(data):
                conn.execute(
                    "INSERT INTO versions VALUES (?,?,?)",
                    tuple(item[k] for k in ("service", "version", "fails_health")),
                )
            state.snapshot(conn, identity, document["instance_id"])
            current = db.lstat()
            if not stat.S_ISREG(current.st_mode) or (
                current.st_dev,
                current.st_ino,
            ) != (original.st_dev, original.st_ino):
                raise ValueError("seed destination changed during initialization")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        fd = os.open(db, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != (
                original.st_dev,
                original.st_ino,
            ):
                raise ValueError("seed destination changed before flush")
            os.fsync(fd)
        finally:
            os.close(fd)
        parent_fd = os.open(db.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return cls(db)

    @contextmanager
    def _connect(self, *, write=False, control_only=False):
        self._guard_file()
        conn = sqlite3.connect(
            self.path.as_uri() + "?mode=rw",
            uri=True,
            timeout=self.timeout,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA trusted_schema=OFF")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            self._guard_file()
            if control_only:
                state.check_schema(conn)
                doc, _fixture, original = state.source_document(
                    conn, self.identity, self.instance_id
                )
                before = {"source": doc, "original": {"source_document": original}}
            else:
                before = state.snapshot(conn, self.identity, self.instance_id)
            original_sha = state.sha256(before["original"]["source_document"].encode())
            if self.source_sha256 is not None and original_sha != self.source_sha256:
                raise ValueError("original deployment source document changed")
            yield conn, before
            if not control_only:
                self.commit_guard()
            self._guard_file()
            if control_only:
                state.check_schema(conn)
                _doc, _fixture, after_source = state.source_document(
                    conn, self.identity, self.instance_id
                )
            else:
                after_source = state.snapshot(conn, self.identity, self.instance_id)[
                    "original"
                ]["source_document"]
            if state.sha256(after_source.encode()) != original_sha:
                raise ValueError("original deployment source changed before commit")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def snapshot(self):
        """Private consistent original table/schema image; never a model tool."""
        with self._connect() as (_conn, observed):
            return deepcopy(observed)

    @staticmethod
    def _service(snapshot, name):
        service_name(name)
        row = next(
            (r for r in snapshot["business"]["services"] if r["name"] == name), None
        )
        if row is None:
            raise ValueError("unknown service")
        return deepcopy(row)

    def _operation(self, conn, credential, *, allow_closed=False):
        if credential is None:
            return None  # Explicit trusted synthetic-human source/CLI seam.
        state.hex_digest(credential)
        row = conn.execute(
            "SELECT * FROM operations WHERE credential_sha256=?",
            (state.sha256(credential.encode()),),
        ).fetchone()
        if row is None:
            raise ValueError("unknown_operation")
        op = operation(state.bounded_json(row["document"]))
        if (
            row["id"] != op["id"]
            or row["credential_sha256"] != op["credential_sha256"]
            or op["instance_id"] != self.instance_id
        ):
            raise ValueError("current operation authority differs")
        if not allow_closed and op["status"] != "active":
            raise ValueError("operation_closed")
        return op

    def register_operation(self, task_sha256, allow_writes, intent=None):
        state.hex_digest(task_sha256)
        if type(allow_writes) is not bool:
            raise ValueError("explicit boolean write permission required")
        if allow_writes:
            intent = validate_intent(intent)
        elif intent is not None:
            raise ValueError("read-only operation cannot carry rollout intent")
        with self._connect(write=True) as (conn, observed):
            if len(observed["records"]["operations"]) >= state.MAX_OPERATIONS:
                raise ValueError("deployment operation budget exhausted")
            credential = secrets.token_hex(32)
            op = sealed(
                {
                    "id": str(uuid.uuid4()),
                    "instance_id": self.instance_id,
                    "credential_sha256": state.sha256(credential.encode()),
                    "task_sha256": task_sha256,
                    "allow_writes": allow_writes,
                    "intent": deepcopy(intent),
                    "initial_history_frontier": len(observed["business"]["history"]),
                    "initial_context_sha256": observed["context_sha256"],
                    "status": "active",
                }
            )
            conn.execute(
                "INSERT INTO operations VALUES (?,?,?)",
                (op["id"], op["credential_sha256"], state.canonical(op)),
            )
            return {"credential": credential, "operation": op}

    def close_operation(self, credential):
        # Revocation needs original identity/schema/header/operation, not a
        # healthy business state. A lock/schema/header failure stays unconfirmed.
        with self._connect(write=True, control_only=True) as (conn, _observed):
            op = self._operation(conn, credential)
            if op is None:
                raise ValueError("operator credential required")
            closed = sealed(
                {
                    k: ("closed" if k == "status" else v)
                    for k, v in op.items()
                    if k != "content_sha256"
                }
            )
            conn.execute(
                "UPDATE operations SET document=? WHERE id=?",
                (state.canonical(closed), op["id"]),
            )
            return {"operation": closed}

    def current_operation(self, credential, *, allow_closed=False):
        with self._connect(control_only=True) as (conn, _source):
            return deepcopy(
                self._operation(conn, credential, allow_closed=allow_closed)
            )

    def observe(self, name, *, kind="status", cached=False, credential=None):
        service_name(name)
        if (
            type(cached) is not bool
            or kind not in {"status", "history"}
            or (kind == "history" and cached)
        ):
            raise ValueError("exact observation kind/boolean cached flag required")
        with self._connect(write=True) as (conn, observed):
            op = self._operation(conn, credential)
            current = self._service(observed, name)
            context = observed["business"]
            if kind == "history":
                value = history_value(context, name)
            elif cached:
                original = next(
                    r["snapshot"]
                    for r in observed["original"]["tables"]["status_cache"]
                    if r["name"] == name
                )
                value = status_value(context, state.bounded_json(original), "cached")
            else:
                value = status_value(context, current, "fresh")
            if len(state.canonical(value).encode()) > 128 * 1024:
                raise ValueError(
                    "complete business observation exceeds model view bound"
                )
            if len(observed["records"]["observations"]) >= state.MAX_OBSERVATIONS:
                raise ValueError("durable observation budget exhausted")
            secret = secrets.token_hex(32)
            record = sealed(
                {
                    "id": str(uuid.uuid4()),
                    "sequence": len(observed["records"]["observations"]) + 1,
                    "instance_id": self.instance_id,
                    "operation_id": op["id"] if op else None,
                    "kind": kind,
                    "service": name,
                    "source": "cached" if cached else "fresh",
                    "history_frontier": len(context["history"]),
                    "context_sha256": observed["context_sha256"],
                    "value": value,
                    "token_sha256": state.sha256(secret.encode()),
                }
            )
            if kind == "status" and not cached:
                conn.execute(
                    "UPDATE status_cache SET snapshot=? WHERE name=?",
                    (state.canonical(current), name),
                )
            conn.execute(
                "INSERT INTO observations VALUES (?,?,?,?)",
                (
                    record["id"],
                    record["sequence"],
                    record["operation_id"],
                    state.canonical(record),
                ),
            )
            return {
                "value": value,
                "observation": record,
                "proof": {"id": record["id"], "token": secret},
            }

    def status(self, name, cached=False):
        return self.observe(name, cached=cached)["value"]

    def history(self, name):
        return self.observe(name, kind="history")["value"]

    def _authorize(self, conn, observed, credential, proof, kind, args):
        op = self._operation(conn, credential)
        if op is None:
            if proof is not None:
                raise ValueError("synthetic-human mutation cannot borrow operator read")
            return None, None, False
        if not op["allow_writes"]:
            raise ValueError("writes_not_authorized")
        state.keys(proof, {"id", "token"})
        state.identifier(proof["id"])
        state.hex_digest(proof["token"])
        read = next(
            (r for r in observed["records"]["observations"] if r["id"] == proof["id"]),
            None,
        )
        current = self._service(observed, args["service"])
        if (
            read is None
            or read["token_sha256"] != state.sha256(proof["token"].encode())
            or read["operation_id"] != op["id"]
            or read["kind"] != "status"
            or read["source"] != "fresh"
            or read["service"] != args["service"]
            or read["context_sha256"] != observed["context_sha256"]
            or not same(
                read["value"], status_value(observed["business"], current, "fresh")
            )
        ):
            raise ValueError("genuine_current_scoped_status_required")
        if any(d["read_id"] == read["id"] for d in observed["records"]["decisions"]):
            raise ValueError("status_observation_already_consumed")
        allow_noop = workflow(
            op, kind, args, observed["business"], observed["records"]["decisions"]
        )
        return op, read, allow_noop

    def _mutate(self, kind, args, *, credential=None, proof=None):
        with self._connect(write=True) as (conn, observed):
            before = self._service(observed, args["service"])
            op, read, allow_noop = self._authorize(
                conn, observed, credential, proof, kind, args
            )
            after, event = state.next_state(
                before,
                kind,
                args,
                observed["business"]["versions"],
                allow_noop=allow_noop,
            )
            if len(observed["records"]["decisions"]) >= state.MAX_DECISIONS:
                raise ValueError("durable transition budget exhausted")
            decision_id, history_id = str(uuid.uuid4()), None
            after_context = deepcopy(observed["business"])
            if event:
                if len(observed["business"]["history"]) >= state.MAX_HISTORY:
                    raise ValueError("deployment history budget exhausted")
                conn.execute(
                    "UPDATE services SET version=?,previous_version=?,health=?,revision=? WHERE name=?",
                    tuple(
                        after[k]
                        for k in (
                            "version",
                            "previous_version",
                            "health",
                            "revision",
                            "name",
                        )
                    ),
                )
                history_id = conn.execute(
                    "INSERT INTO history (service,event,version,revision,decision_id) VALUES (?,?,?,?,?)",
                    (
                        after["name"],
                        event,
                        after["version"],
                        after["revision"],
                        decision_id,
                    ),
                ).lastrowid
                after_context["services"] = [
                    deepcopy(after) if r["name"] == after["name"] else r
                    for r in after_context["services"]
                ]
                after_context["history"].append(
                    {
                        "id": history_id,
                        "service": after["name"],
                        "event": event,
                        "version": after["version"],
                        "revision": after["revision"],
                        "decision_id": decision_id,
                    }
                )
            decision = sealed(
                {
                    "id": decision_id,
                    "sequence": len(observed["records"]["decisions"]) + 1,
                    "instance_id": self.instance_id,
                    "operation_id": op["id"] if op else None,
                    "read_id": read["id"] if read else None,
                    "kind": kind,
                    "args": deepcopy(args),
                    "status": "applied" if event else "unchanged",
                    "event": event,
                    "before": before,
                    "after": after,
                    "history_frontier": len(observed["business"]["history"]),
                    "history_id": history_id,
                    "before_context_sha256": observed["context_sha256"],
                    "after_context_sha256": state.digest(after_context),
                }
            )
            conn.execute(
                "INSERT INTO decisions VALUES (?,?,?,?)",
                (
                    decision_id,
                    decision["sequence"],
                    decision["operation_id"],
                    state.canonical(decision),
                ),
            )
            return {
                "value": {**after, "outcome": decision["status"]},
                "decision": decision,
            }

    def deploy(self, name, version, expected_revision, *, credential=None, proof=None):
        service_name(name)
        stable_version(version)
        revision(expected_revision)
        result = self._mutate(
            "deploy",
            {
                "service": name,
                "version": version,
                "expected_revision": expected_revision,
            },
            credential=credential,
            proof=proof,
        )
        return result if credential is not None else result["value"]

    def health_check(self, name, expected_revision, *, credential=None, proof=None):
        service_name(name)
        revision(expected_revision)
        result = self._mutate(
            "health",
            {"service": name, "expected_revision": expected_revision},
            credential=credential,
            proof=proof,
        )
        return result if credential is not None else result["value"]

    def rollback(self, name, expected_revision, *, credential=None, proof=None):
        service_name(name)
        revision(expected_revision)
        result = self._mutate(
            "rollback",
            {"service": name, "expected_revision": expected_revision},
            credential=credential,
            proof=proof,
        )
        return result if credential is not None else result["value"]
