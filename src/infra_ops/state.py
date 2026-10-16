"""Original SQLite schema/source and complete synthetic business lineage."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import uuid
from copy import deepcopy

from .source import (
    MAX_INTEGER,
    keys,
    next_revision,
    parse_json,
    revision,
    service_name,
    stable_version,
    validate_fixture,
)

APPLICATION_ID = 0x4644494E
SCHEMA_VERSION = 2
MAX_DATABASE_BYTES = 32 * 1024 * 1024
MAX_HISTORY = 256
MAX_OPERATIONS = 128
MAX_OBSERVATIONS = 512
MAX_DECISIONS = 512
TABLES = {
    "metadata": "CREATE TABLE metadata (id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL) STRICT",
    "services": "CREATE TABLE services (name TEXT PRIMARY KEY, version TEXT NOT NULL, previous_version TEXT, health TEXT NOT NULL CHECK(health IN ('healthy','rolling','degraded')), revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740991)) STRICT",
    "versions": "CREATE TABLE versions (service TEXT NOT NULL, version TEXT NOT NULL, fails_health INTEGER NOT NULL CHECK(fails_health IN (0,1)), PRIMARY KEY (service,version)) STRICT",
    "status_cache": "CREATE TABLE status_cache (name TEXT PRIMARY KEY, snapshot TEXT NOT NULL) STRICT",
    "history": "CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT NOT NULL, event TEXT NOT NULL, version TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740991), decision_id TEXT NOT NULL UNIQUE) STRICT",
    "operations": "CREATE TABLE operations (id TEXT PRIMARY KEY, credential_sha256 TEXT NOT NULL UNIQUE, document TEXT NOT NULL) STRICT",
    "observations": "CREATE TABLE observations (id TEXT PRIMARY KEY, sequence INTEGER NOT NULL UNIQUE, operation_id TEXT, document TEXT NOT NULL) STRICT",
    "decisions": "CREATE TABLE decisions (id TEXT PRIMARY KEY, sequence INTEGER NOT NULL UNIQUE, operation_id TEXT, document TEXT NOT NULL) STRICT",
    "seed_state": "CREATE TABLE seed_state (id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL) STRICT",
}
AUTO_INDEXES = {
    "services": 1,
    "versions": 1,
    "status_cache": 1,
    "history": 1,
    "operations": 2,
    "observations": 2,
    "decisions": 2,
}


def canonical(value) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def digest(value) -> str:
    return sha256(canonical(value).encode("utf-8", errors="strict"))


def identifier(value):
    if type(value) is not str:
        raise ValueError("canonical UUID required")
    try:
        valid = str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("canonical UUID required")
    return value


def hex_digest(value):
    if (
        type(value) is not str
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError("original SHA256 required")
    return value


def bounded_json(text):
    if (
        type(text) is not str
        or len(text.encode("utf-8", errors="strict")) > 8 * 1024 * 1024
    ):
        raise ValueError("bounded original JSON cell required")
    return parse_json(text)


def rows(conn, table, order, limit):
    # Both identifiers are application constants, never model/user SQL.
    result = [
        dict(row)
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchmany(
            limit + 1
        )
    ]
    if len(result) > limit:
        raise ValueError("complete original table exceeds row bound")
    return result


def check_schema(conn):
    if (
        conn.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
        or conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
    ):
        raise ValueError(
            "managed synthetic deployment schema required; use a fresh seed"
        )
    if (
        conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete"
        or conn.execute("PRAGMA encoding").fetchone()[0] != "UTF-8"
    ):
        raise ValueError("unsupported deployment database storage mode")
    actual = [
        dict(r)
        for r in conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"
        )
    ]
    expected = [
        {"type": "table", "name": name, "tbl_name": name, "sql": sql}
        for name, sql in TABLES.items()
    ]
    expected.append(
        {
            "type": "table",
            "name": "sqlite_sequence",
            "tbl_name": "sqlite_sequence",
            "sql": "CREATE TABLE sqlite_sequence(name,seq)",
        }
    )
    for table, count in AUTO_INDEXES.items():
        expected.extend(
            {
                "type": "index",
                "name": f"sqlite_autoindex_{table}_{n}",
                "tbl_name": table,
                "sql": None,
            }
            for n in range(1, count + 1)
        )
    if actual != sorted(expected, key=lambda r: (r["type"], r["name"])):
        raise ValueError(
            "original deployment schema differs: triggers/views/unknown tables or indexes are unsupported"
        )
    if [tuple(r) for r in conn.execute("PRAGMA quick_check")] != [("ok",)]:
        raise ValueError("original deployment database integrity failed")
    return actual


def source_document(conn, identity, instance_id=None):
    entries = rows(conn, "metadata", "id", 1)
    if len(entries) != 1 or entries[0]["id"] != 1:
        raise ValueError("one original synthetic source document required")
    original = entries[0]["document"]
    doc = keys(
        bounded_json(original),
        {
            "schema_version",
            "synthetic",
            "instance_id",
            "identity",
            "fixture_base64",
            "fixture_sha256",
            "provenance",
            "population",
        },
    )
    if (
        type(doc["schema_version"]) is not int
        or doc["schema_version"] != SCHEMA_VERSION
        or doc["synthetic"] is not True
    ):
        raise ValueError("qualified original synthetic source required")
    identifier(doc["instance_id"])
    if instance_id is not None and doc["instance_id"] != instance_id:
        raise ValueError("managed deployment instance changed")
    if doc["identity"] != identity:
        raise ValueError("managed deployment database/root identity changed")
    if doc["provenance"] != {
        "kind": "explicit_synthetic_fixture_author",
        "prior_model_evidence": None,
        "prior_human_authentication": None,
        "prior_cloud_delivery": None,
    }:
        raise ValueError("synthetic source provenance differs")
    try:
        raw = base64.b64decode(doc["fixture_base64"], validate=True)
    except (ValueError, TypeError):
        raise ValueError("original fixture bytes required") from None
    if (
        len(raw) > 8 * 1024 * 1024
        or base64.b64encode(raw).decode() != doc["fixture_base64"]
        or sha256(raw) != hex_digest(doc["fixture_sha256"])
    ):
        raise ValueError("original fixture bytes/hash differ")
    fixture = validate_fixture(parse_json(raw.decode("utf-8", errors="strict")))
    if doc["population"] is not None:
        ref = keys(
            doc["population"],
            {"id", "version", "case_id", "dataset_sha256", "definition_sha256"},
        )
        if ref["id"] != "infrastructure-populations" or ref["version"] != "1.0.0":
            raise ValueError("unsupported original population source")
        service_name(ref["case_id"])
        hex_digest(ref["dataset_sha256"])
        hex_digest(ref["definition_sha256"])
    return doc, fixture, original


def initial_services(fixture):
    return sorted(
        (
            {
                "name": r["name"],
                "version": r["version"],
                "previous_version": None,
                "health": "healthy" if r["healthy"] else "degraded",
                "revision": r["revision"],
            }
            for r in fixture["services"]
        ),
        key=lambda r: r["name"],
    )


def version_rows(fixture):
    return sorted(
        (
            {
                "service": name,
                "version": version,
                "fails_health": int(
                    version in fixture.get("failing_versions", {}).get(name, [])
                ),
            }
            for name, versions in fixture["allowed_versions"].items()
            for version in versions
        ),
        key=lambda r: (r["service"], r["version"]),
    )


def context(source_sha256, services, versions, history):
    return {
        "source_sha256": source_sha256,
        "services": sorted((dict(r) for r in services), key=lambda r: r["name"]),
        "versions": versions,
        "history": list(history),
    }


def next_state(current, kind, args, versions, *, allow_noop=False):
    expected_keys = (
        {"service", "expected_revision", "version"}
        if kind == "deploy"
        else {"service", "expected_revision"}
    )
    keys(args, expected_keys)
    if service_name(args["service"]) != current["name"]:
        raise ValueError("wrong deployment service")
    if revision(args["expected_revision"]) != current["revision"]:
        raise ValueError("stale revision")
    if kind == "deploy":
        version = stable_version(args["version"])
        if not any(
            r["service"] == current["name"] and r["version"] == version
            for r in versions
        ):
            raise ValueError("version not allowed in synthetic environment")
        if current["health"] != "healthy":
            raise ValueError("service not ready for this rollout")
        if current["version"] == version:
            return deepcopy(current), None
        return {
            **current,
            "version": version,
            "previous_version": current["version"],
            "health": "rolling",
            "revision": next_revision(current["revision"]),
        }, "deploy_started"
    if kind == "health":
        if current["health"] != "rolling":
            if allow_noop and current["health"] in {"healthy", "degraded"}:
                return deepcopy(current), None
            raise ValueError("no rollout awaiting health check")
        rule = next(
            r
            for r in versions
            if r["service"] == current["name"] and r["version"] == current["version"]
        )
        failed = rule["fails_health"] == 1
        return {
            **current,
            "health": "degraded" if failed else "healthy",
            "revision": next_revision(current["revision"]),
        }, ("health_failed" if failed else "health_passed")
    if kind == "rollback":
        if current["health"] != "degraded" or not current["previous_version"]:
            if (
                allow_noop
                and current["health"] == "healthy"
                and current["previous_version"] is None
            ):
                return deepcopy(current), None
            raise ValueError("rollback requires a failed rollout and previous version")
        target = next(
            (
                r
                for r in versions
                if r["service"] == current["name"]
                and r["version"] == current["previous_version"]
            ),
            None,
        )
        if target is None or target["fails_health"]:
            raise ValueError("rollback target is not a known healthy version")
        return {
            **current,
            "version": current["previous_version"],
            "previous_version": None,
            "health": "healthy",
            "revision": next_revision(current["revision"]),
        }, "rolled_back"
    raise ValueError("unsupported deployment transition")


def business_lineage(doc, fixture, original, tables):
    versions = version_rows(fixture)
    if tables["versions"] != versions:
        raise ValueError("original service version/health catalog changed")
    current = {r["name"]: r for r in initial_services(fixture)}
    history = tables["history"]
    source_identity_sha = digest({k: v for k, v in doc.items() if k != "population"})
    contexts = [context(source_identity_sha, list(current.values()), versions, [])]
    valid_states = [{name: [deepcopy(row)] for name, row in current.items()}]
    seen_decisions, last_events = set(), {}
    for n, event in enumerate(history):
        if (
            event["id"] != n + 1
            or type(event["id"]) is not int
            or event["service"] not in current
        ):
            raise ValueError("complete consecutive deployment history required")
        identifier(event["decision_id"])
        if event["decision_id"] in seen_decisions:
            raise ValueError("duplicate deployment history decision")
        seen_decisions.add(event["decision_id"])
        prior = current[event["service"]]
        if event["event"] == "deploy_started":
            kind, args = "deploy", {
                "service": event["service"],
                "version": event["version"],
                "expected_revision": prior["revision"],
            }
        elif event["event"] in {"health_passed", "health_failed"}:
            kind, args = "health", {
                "service": event["service"],
                "expected_revision": prior["revision"],
            }
        elif event["event"] == "rolled_back":
            if last_events.get(event["service"]) != "health_failed":
                raise ValueError("rollback lacks genuine preceding failed health")
            kind, args = "rollback", {
                "service": event["service"],
                "expected_revision": prior["revision"],
            }
        else:
            raise ValueError("unknown deployment history event")
        after, expected_event = next_state(prior, kind, args, versions)
        if (
            expected_event is None
            or expected_event != event["event"]
            or after["version"] != event["version"]
            or after["revision"] != event["revision"]
        ):
            raise ValueError(
                "deployment event contradicts original state/version/health rule"
            )
        current[event["service"]] = after
        last_events[event["service"]] = event["event"]
        contexts.append(
            context(
                source_identity_sha, list(current.values()), versions, history[: n + 1]
            )
        )
        valid = dict(valid_states[-1])
        valid[event["service"]] = [*valid[event["service"]], dict(after)]
        valid_states.append(valid)
    if tables["services"] != sorted(current.values(), key=lambda r: r["name"]):
        raise ValueError(
            "current deployment services differ from complete original history"
        )
    if [r["name"] for r in tables["status_cache"]] != sorted(current):
        raise ValueError("complete exact service status cache required")
    for row in tables["status_cache"]:
        cached = bounded_json(row["snapshot"])
        if cached not in valid_states[-1][row["name"]]:
            raise ValueError("cached status never existed in original service history")
    sequence = tables["sqlite_sequence"]
    if sequence != ([{"name": "history", "seq": len(history)}] if history else []):
        raise ValueError("deployment history sequence differs")
    return contexts, valid_states


def snapshot(conn, identity, instance_id=None):
    schema = check_schema(conn)
    doc, fixture, original = source_document(conn, identity, instance_id)
    tables = {
        "services": rows(conn, "services", "name", 64),
        "versions": rows(conn, "versions", "service,version", 4096),
        "status_cache": rows(conn, "status_cache", "name", 64),
        "history": rows(conn, "history", "id", MAX_HISTORY),
        "operations": rows(conn, "operations", "rowid", MAX_OPERATIONS),
        "observations": rows(conn, "observations", "sequence", MAX_OBSERVATIONS),
        "decisions": rows(conn, "decisions", "sequence", MAX_DECISIONS),
        "seed_state": rows(conn, "seed_state", "id", 1),
        "sqlite_sequence": rows(conn, "sqlite_sequence", "name", 1),
    }
    contexts, valid_states = business_lineage(doc, fixture, original, tables)
    from .authority import validate_records

    records = validate_records(doc, tables, contexts, valid_states)
    if doc["population"] is None and tables["seed_state"]:
        raise ValueError("private population seed has no source reference")
    if doc["population"] is not None and (
        len(tables["seed_state"]) != 1 or tables["seed_state"][0]["id"] != 1
    ):
        raise ValueError("private population original seed required")
    observed = {"schema": schema, "source_document": original, "tables": tables}
    return {
        "source": doc,
        "fixture": fixture,
        "original": observed,
        "sha256": digest(observed),
        "business": contexts[-1],
        "context_sha256": digest(contexts[-1]),
        "records": records,
    }
