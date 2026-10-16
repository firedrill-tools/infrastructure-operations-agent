"""Pinned fictional populations and private original seed authority validation."""

from __future__ import annotations

import base64
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

from . import state
from ._dataset_manifest import DATASET_BYTES, DATASET_CASES, DATASET_SHA256
from .authority import same, status_value, checked_hash
from .source import keys, parse_json, validate_fixture, read_regular


@lru_cache(maxsize=1)
def _dataset():
    raw = read_regular(Path(__file__).parent / "data/populations.json")
    if len(raw) != DATASET_BYTES or state.sha256(raw) != DATASET_SHA256:
        raise ValueError("original pinned deployment dataset differs")
    value = parse_json(raw.decode("utf-8", errors="strict"))
    if (
        value["id"] != "infrastructure-populations"
        or value["version"] != "1.0.0"
        or value["synthetic"] is not True
        or len(value["cases"]) != DATASET_CASES
    ):
        raise ValueError("pinned deployment dataset identity differs")
    ids = set()
    for case in value["cases"]:
        if case["id"] in ids:
            raise ValueError("duplicate pinned deployment case")
        ids.add(case["id"])
        fixture_bytes(case)
    return value


def dataset():
    return deepcopy(_dataset())


def definition(case_id):
    found = next((c for c in _dataset()["cases"] if c["id"] == case_id), None)
    if found is None:
        raise ValueError("unknown deployment case")
    return deepcopy(found)


def reference(case):
    return {
        "id": "infrastructure-populations",
        "version": "1.0.0",
        "case_id": case["id"],
        "dataset_sha256": DATASET_SHA256,
        "definition_sha256": state.digest(case),
    }


def fixture_bytes(case):
    raw = base64.b64decode(case["fixture_base64"], validate=True)
    if (
        base64.b64encode(raw).decode() != case["fixture_base64"]
        or state.sha256(raw) != case["fixture_sha256"]
    ):
        raise ValueError("pinned original deployment fixture differs")
    validate_fixture(parse_json(raw.decode("utf-8", errors="strict")))
    return raw


def validate_seed(doc, tables, records, contexts, schema):
    case = definition(doc["population"]["case_id"])
    ref = reference(case)
    if (
        not same(doc["population"], ref)
        or doc["fixture_sha256"] != case["fixture_sha256"]
        or doc["fixture_base64"] != case["fixture_base64"]
    ):
        raise ValueError(
            "original population reference/fixture differs from pinned case"
        )
    seed = keys(
        state.bounded_json(tables["seed_state"][0]["document"]),
        {
            "schema_version",
            "instance_id",
            "reference",
            "original",
            "original_sha256",
            "context_sha256",
            "provenance",
            "content_sha256",
        },
    )
    checked_hash(seed)
    if (
        type(seed["schema_version"]) is not int
        or seed["schema_version"] != 1
        or seed["instance_id"] != doc["instance_id"]
        or not same(seed["reference"], ref)
        or not same(seed["provenance"], _dataset()["provenance"])
    ):
        raise ValueError("original population seed identity/provenance differs")
    original = keys(seed["original"], {"schema", "source_document", "tables"})
    if state.digest(original) != seed["original_sha256"] or not same(
        original["schema"], schema
    ):
        raise ValueError("original population seed bytes/schema hash differs")
    old_doc = {**doc, "creation": "fixture", "population": None}
    if original["source_document"] != state.canonical(old_doc):
        raise ValueError("original population initialization source differs")
    prior = keys(original["tables"], set(tables))
    if prior["operations"] or prior["seed_state"]:
        raise ValueError("imported runtime authority is not a population seed")
    count = len(case["setup"])
    if len(prior["observations"]) != count or len(prior["decisions"]) != count:
        raise ValueError("original population authoring records differ")
    for name in ("observations", "decisions", "history"):
        if not same(prior[name], tables[name][: len(prior[name])]):
            raise ValueError("original population seed prefix changed")
    frontier = len(prior["history"])
    if (
        frontier >= len(contexts)
        or seed["context_sha256"] != state.digest(contexts[frontier])
        or not same(prior["services"], contexts[frontier]["services"])
        or not same(prior["versions"], tables["versions"])
        or not same(
            prior["sqlite_sequence"],
            [{"name": "history", "seq": frontier}] if frontier else [],
        )
    ):
        raise ValueError("original population baseline context differs")
    fixture = validate_fixture(parse_json(fixture_bytes(case).decode()))
    current = {s["name"]: s for s in state.initial_services(fixture)}
    cache = deepcopy(current)
    versions, event_count = state.version_rows(fixture), 0
    for n, action in enumerate(case["setup"]):
        before = current[action["service"]]
        args = {
            "service": action["service"],
            "expected_revision": before["revision"],
            **({"version": action["version"]} if action["kind"] == "deploy" else {}),
        }
        observation = records["observations"][n]
        decision = records["decisions"][n]
        expected_value = status_value(contexts[event_count], before, "fresh")
        if (
            observation["operation_id"] is not None
            or observation["kind"] != "status"
            or observation["source"] != "fresh"
            or observation["service"] != action["service"]
            or observation["history_frontier"] != event_count
            or not same(observation["value"], expected_value)
            or decision["operation_id"] is not None
            or decision["read_id"] is not None
            or decision["kind"] != action["kind"]
            or not same(decision["args"], args)
        ):
            raise ValueError(
                "original population actions/reads are not declared synthetic authoring"
            )
        cache[action["service"]] = deepcopy(before)
        after, event = state.next_state(before, action["kind"], args, versions)
        current[action["service"]] = after
        event_count += int(event is not None)
    expected_cache = [
        {"name": name, "snapshot": state.canonical(row)}
        for name, row in sorted(cache.items())
    ]
    if (
        event_count != frontier
        or not same(
            prior["services"], sorted(current.values(), key=lambda s: s["name"])
        )
        or not same(prior["status_cache"], expected_cache)
    ):
        raise ValueError(
            "original population current/cache state differs from declared setup"
        )
    return seed
