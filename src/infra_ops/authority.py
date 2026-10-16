"""Scoped current observations and operation-owned deployment transitions."""

from __future__ import annotations

from .source import keys, revision, service_name, stable_version
from .state import (
    MAX_HISTORY,
    bounded_json,
    canonical,
    digest,
    hex_digest,
    identifier,
    next_state,
)


def same(left, right):
    return canonical(left) == canonical(right)


def checked_hash(record):
    hex_digest(record["content_sha256"])
    if record["content_sha256"] != digest(
        {k: v for k, v in record.items() if k != "content_sha256"}
    ):
        raise ValueError("original authority record content/hash differs")
    return record


def sealed(record):
    return {**record, "content_sha256": digest(record)}


def intent(value):
    value = keys(
        value, {"service", "version", "expected_revision", "rollback_on_failure"}
    )
    service_name(value["service"])
    stable_version(value["version"])
    revision(value["expected_revision"])
    if type(value["rollback_on_failure"]) is not bool:
        raise ValueError("exact rollback-on-failure intent required")
    return value


def operation(value):
    value = keys(
        value,
        {
            "id",
            "instance_id",
            "credential_sha256",
            "task_sha256",
            "allow_writes",
            "intent",
            "initial_history_frontier",
            "initial_context_sha256",
            "status",
            "content_sha256",
        },
    )
    identifier(value["id"])
    identifier(value["instance_id"])
    hex_digest(value["credential_sha256"])
    hex_digest(value["task_sha256"])
    hex_digest(value["initial_context_sha256"])
    if (
        type(value["allow_writes"]) is not bool
        or type(value["status"]) is not str
        or value["status"] not in {"active", "closed"}
    ):
        raise ValueError("explicit operation permission/status required")
    if (
        type(value["initial_history_frontier"]) is not int
        or not 0 <= value["initial_history_frontier"] <= MAX_HISTORY
    ):
        raise ValueError("exact original operation frontier required")
    if value["allow_writes"]:
        intent(value["intent"])
    elif value["intent"] is not None:
        raise ValueError("read-only operation cannot carry rollout intent")
    return checked_hash(value)


def status_value(context, state, source):
    return {
        **state,
        "source": source,
        "allowed_versions": [
            r["version"] for r in context["versions"] if r["service"] == state["name"]
        ],
        **(
            {"description": context["descriptions"][state["name"]]}
            if state["name"] in context.get("descriptions", {})
            else {}
        ),
    }


def history_value(context, name):
    return [
        {"event": r["event"], "version": r["version"], "revision": r["revision"]}
        for r in context["history"]
        if r["service"] == name
    ]


def workflow(op, kind, args, context, prior):
    if not op["allow_writes"]:
        raise ValueError("writes_not_authorized")
    chosen = op["intent"]
    current = next(
        (r for r in context["services"] if r["name"] == args["service"]), None
    )
    if (
        current is None
        or args["service"] != chosen["service"]
        or (kind == "deploy" and args["version"] != chosen["version"])
    ):
        raise ValueError("writes_not_authorized_for_exact_rollout_intent")
    applied = [
        d for d in prior if d["operation_id"] == op["id"] and d["status"] == "applied"
    ]
    if not applied:
        if (
            kind != "deploy"
            or digest(context) != op["initial_context_sha256"]
            or current["revision"] != chosen["expected_revision"]
        ):
            raise ValueError("rollout_requires_exact_initial_source_and_intent")
        return False
    last = applied[-1]
    if digest(context) != last["after_context_sha256"]:
        raise ValueError("operation_source_changed_after_own_transition")
    if kind == "deploy":
        if (
            last["kind"] == "health"
            and current["health"] == "healthy"
            and current["version"] == chosen["version"]
        ):
            return True  # Verified already-at-target; never restart after rollback.
        raise ValueError("operation_rollout_already_started")
    if kind == "health":
        if current["version"] != chosen["version"]:
            raise ValueError("health_target_differs_from_owned_rollout")
        if last["kind"] == "deploy" and current["health"] == "rolling":
            return False
        if last["kind"] == "health" and current["health"] in {"healthy", "degraded"}:
            return True
        raise ValueError("health_requires_own_pending_rollout")
    if kind == "rollback":
        failed = [
            d
            for d in applied
            if d["kind"] == "health" and d["event"] == "health_failed"
        ]
        started = [d for d in applied if d["kind"] == "deploy"]
        if not chosen["rollback_on_failure"] or not failed or len(started) != 1:
            raise ValueError("rollback_requires_authorized_own_failed_rollout")
        previous = started[0]["before"]["version"]
        if (
            last["kind"] == "health"
            and current["health"] == "degraded"
            and current["version"] == chosen["version"]
            and current["previous_version"] == previous
        ):
            return False
        if (
            last["kind"] == "rollback"
            and current["health"] == "healthy"
            and current["version"] == previous
            and current["previous_version"] is None
        ):
            return True
        raise ValueError("rollback_requires_exact_owned_failed_state")
    raise ValueError("unsupported operation transition")


def validate_records(source, tables, contexts, valid_states):
    ops, observations, decisions = {}, {}, []
    for row in tables["operations"]:
        op = operation(bounded_json(row["document"]))
        if (
            row["id"] != op["id"]
            or row["credential_sha256"] != op["credential_sha256"]
            or op["instance_id"] != source["instance_id"]
            or op["initial_history_frontier"] >= len(contexts)
            or op["initial_context_sha256"]
            != digest(contexts[op["initial_history_frontier"]])
        ):
            raise ValueError("operation differs from original instance/source")
        if op["id"] in ops:
            raise ValueError("duplicate deployment operation")
        ops[op["id"]] = op
    last_frontier = 0
    for n, row in enumerate(tables["observations"]):
        value = keys(
            bounded_json(row["document"]),
            {
                "id",
                "sequence",
                "instance_id",
                "operation_id",
                "kind",
                "service",
                "source",
                "history_frontier",
                "context_sha256",
                "value",
                "token_sha256",
                "content_sha256",
            },
        )
        identifier(value["id"])
        if value["operation_id"] is not None:
            identifier(value["operation_id"])
        hex_digest(value["token_sha256"])
        checked_hash(value)
        frontier = value["history_frontier"]
        if (
            type(value["sequence"]) is not int
            or value["sequence"] != n + 1
            or row["sequence"] != n + 1
            or row["id"] != value["id"]
            or row["operation_id"] != value["operation_id"]
            or value["instance_id"] != source["instance_id"]
            or type(frontier) is not int
            or not last_frontier <= frontier < len(contexts)
        ):
            raise ValueError("observation sequence/instance/source frontier differs")
        last_frontier = frontier
        context = contexts[frontier]
        if value["context_sha256"] != digest(context):
            raise ValueError("observation original complete source differs")
        if value["operation_id"] is not None:
            op = ops.get(value["operation_id"])
            if op is None or op["initial_history_frontier"] > frontier:
                raise ValueError("observation lacks original operation scope")
        name = service_name(value["service"])
        state = next((r for r in context["services"] if r["name"] == name), None)
        if state is None:
            raise ValueError("observation service missing from source")
        if value["kind"] == "status" and value["source"] == "fresh":
            expected = [status_value(context, state, "fresh")]
        elif value["kind"] == "status" and value["source"] == "cached":
            expected = [
                status_value(context, r, "cached") for r in valid_states[frontier][name]
            ]
        elif value["kind"] == "history" and value["source"] == "fresh":
            expected = [history_value(context, name)]
        else:
            raise ValueError("unknown original observation kind/source")
        if not any(same(value["value"], candidate) for candidate in expected):
            raise ValueError("observed value differs from complete original source")
        if value["id"] in observations:
            raise ValueError("duplicate original observation")
        observations[value["id"]] = value
    cursor, consumed = 0, set()
    for n, row in enumerate(tables["decisions"]):
        d = keys(
            bounded_json(row["document"]),
            {
                "id",
                "sequence",
                "instance_id",
                "operation_id",
                "read_id",
                "kind",
                "args",
                "status",
                "event",
                "before",
                "after",
                "history_frontier",
                "history_id",
                "before_context_sha256",
                "after_context_sha256",
                "content_sha256",
            },
        )
        identifier(d["id"])
        if d["operation_id"] is not None:
            identifier(d["operation_id"])
            identifier(d["read_id"])
        checked_hash(d)
        if (
            type(d["sequence"]) is not int
            or d["sequence"] != n + 1
            or row["sequence"] != n + 1
            or row["id"] != d["id"]
            or row["operation_id"] != d["operation_id"]
            or d["instance_id"] != source["instance_id"]
            or type(d["history_frontier"]) is not int
            or d["history_frontier"] != cursor
            or type(d["kind"]) is not str
            or d["kind"] not in {"deploy", "health", "rollback"}
        ):
            raise ValueError("decision sequence/scope/frontier differs")
        before_context = contexts[cursor]
        keys(
            d["args"],
            (
                {"service", "expected_revision", "version"}
                if d["kind"] == "deploy"
                else {"service", "expected_revision"}
            ),
        )
        service_name(d["args"]["service"])
        revision(d["args"]["expected_revision"])
        if d["kind"] == "deploy":
            stable_version(d["args"]["version"])
        name = d["args"]["service"]
        before = next(
            (r for r in before_context["services"] if r["name"] == name), None
        )
        if (
            before is None
            or not same(d["before"], before)
            or d["before_context_sha256"] != digest(before_context)
        ):
            raise ValueError("decision original prior source differs")
        allow_noop = False
        if d["operation_id"] is not None:
            op = ops.get(d["operation_id"])
            read = observations.get(d["read_id"])
            if (
                op is None
                or read is None
                or read["operation_id"] != op["id"]
                or read["kind"] != "status"
                or read["source"] != "fresh"
                or read["service"] != name
                or read["context_sha256"] != d["before_context_sha256"]
                or d["read_id"] in consumed
            ):
                raise ValueError(
                    "transition lacks unique genuine current scoped status"
                )
            if not same(read["value"], status_value(before_context, before, "fresh")):
                raise ValueError("transition observation is not exact current service")
            allow_noop = workflow(op, d["kind"], d["args"], before_context, decisions)
            consumed.add(d["read_id"])
        elif d["read_id"] is not None:
            raise ValueError("synthetic-human transition cannot borrow operator read")
        after, event = next_state(
            before,
            d["kind"],
            d["args"],
            before_context["versions"],
            allow_noop=allow_noop,
        )
        if (
            not same(d["after"], after)
            or d["event"] != event
            or d["status"] != ("applied" if event else "unchanged")
        ):
            raise ValueError("decision contradicts actual source-derived transition")
        if event:
            if (
                type(d["history_id"]) is not int
                or d["history_id"] != cursor + 1
                or cursor >= len(tables["history"])
            ):
                raise ValueError("applied decision lacks exact history effect")
            actual = tables["history"][cursor]
            if (
                actual["decision_id"] != d["id"]
                or actual["service"] != name
                or actual["event"] != event
            ):
                raise ValueError(
                    "history effect differs from original applied decision"
                )
            cursor += 1
        elif d["history_id"] is not None:
            raise ValueError("unchanged decision cannot claim a history write")
        if d["after_context_sha256"] != digest(contexts[cursor]):
            raise ValueError("decision resulting complete source differs")
        decisions.append(d)
    if cursor != len(tables["history"]):
        raise ValueError("deployment history lacks original applied decision")
    return {
        "operations": list(ops.values()),
        "observations": list(observations.values()),
        "decisions": decisions,
    }
