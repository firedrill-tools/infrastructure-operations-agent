import base64
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from infra_ops.authority import sealed
from infra_ops.cases import initialize, inspect_case, reset_case
from infra_ops.populations import dataset, definition, fixture_bytes
from infra_ops.state import canonical, digest
from infra_ops.store import DeploymentStore
from infra_ops.runtime import run_agent
from native_helper import environment
from test_native_runtime import rejected

CASES = [c["id"] for c in dataset()["cases"]]


def cli(*args, success=True):
    result = subprocess.run(
        [sys.executable, "-I", "-m", "infra_ops.cases", *map(str, args)],
        capture_output=True,
        timeout=40,
    )
    assert result.returncode == (0 if success else 2), (
        result.stdout[-2000:],
        result.stderr[-2000:],
    )
    return json.loads(result.stdout)


def test_pinned_profiles_preserve_original_fixture_and_distinct_business_cases():
    data = dataset()
    assert len(CASES) == 12 and len(set(CASES)) == 12
    assert Counter(c["profile"] for c in data["cases"]) == {
        "starter": 3,
        "busy": 3,
        "ambiguous": 3,
        "boundary": 3,
    }
    assert (
        fixture_bytes(definition(CASES[0]))
        == (Path(__file__).parents[1] / "fixtures/starter.json").read_bytes()
    )
    busy = definition("busy_mixed_history")
    fixture = json.loads(fixture_bytes(busy))
    assert len(fixture["services"]) == 24
    assert sum(len(v) for v in fixture["allowed_versions"].values()) == 240
    assert len(busy["setup"]) == 40
    assert data["provenance"]["prior_model_evidence"] is None
    assert data["provenance"]["prior_cloud_delivery"] is None


@pytest.mark.parametrize("case_id", CASES)
def test_all_twelve_actual_private_cli_initializers_and_inspection_have_fresh_authority(
    case_id, tmp_path
):
    bundle = cli("init", "--case", case_id, "--root", tmp_path / "case")
    inspected = cli("inspect", "--root", bundle["root"])
    assert bundle == inspected
    source = inspected["source"]
    assert not source["records"]["operations"]
    assert len(source["records"]["observations"]) == len(definition(case_id)["setup"])
    assert all(r["operation_id"] is None for r in source["records"]["observations"])
    assert all(
        r["operation_id"] is None and r["read_id"] is None
        for r in source["records"]["decisions"]
    )
    assert source["source"]["creation"] == "population"
    assert inspected["private_reference"]["expected_outcome"]
    assert not (Path(bundle["root"]) / ".case-control.lock").exists()
    if case_id == "ambiguous_stale_cache":
        store = DeploymentStore(bundle["database"])
        assert store.status("worker", True)["health"] == "rolling"
        assert store.status("worker")["health"] == "healthy"


def file_bytes(root):
    return {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def test_reset_after_actual_agent_recovery_preserves_all_old_state_and_evidence_but_creates_new_uuid(
    tmp_path,
):
    root = tmp_path / "old"
    bundle = initialize(root, "starter_failed_recovery")
    with environment(tmp_path, database=bundle["database"]) as e:
        result = run_agent(**{**e["options"], "output_root": root / "evidence"})
        assert result["business"]["outcome"] == "verified_recovery"
    before = file_bytes(root)
    old = inspect_case(root)
    reset = cli("reset", "--root", root, "--new-root", tmp_path / "new")
    assert file_bytes(root) == before
    assert reset["generation"] == 2 and reset["instance_id"] != old["instance_id"]
    assert reset["reset_of"]["source_sha256"] == old["source_sha256"]
    assert reset["source"]["business"]["history"] == []
    assert reset["source"]["records"]["observations"] == []
    assert reset["source"]["records"]["operations"] == []
    assert reset["seed_sha256"] != old["seed_sha256"]
    assert reset["source"]["source"]["identity"] != old["source"]["source"]["identity"]


@pytest.mark.parametrize(
    "fault",
    [
        "seed-row",
        "pointer",
        "both",
        "rehash-definition",
        "seed-context",
        "imported-authority",
        "control",
        "fixture-link",
        "history",
        "source-clone",
    ],
)
def test_private_inspect_and_reset_refuse_corrupt_foreign_or_stripped_authority_without_repair(
    fault, tmp_path
):
    root = tmp_path / "case"
    bundle = initialize(root, "ambiguous_stale_cache")
    if fault == "source-clone":
        backup = tmp_path / "copy.db"
        shutil.copyfile(bundle["database"], backup)
        os.replace(backup, bundle["database"])
    elif fault == "fixture-link":
        original = root / "fixture.json"
        outside = tmp_path / "owned-outside.json"
        outside.write_bytes(original.read_bytes())
        original.unlink()
        original.symlink_to(outside)
    elif fault == "control":
        path = root / "control.json"
        value = json.loads(path.read_text())
        value["generation"] = 2
        value.pop("content_sha256")
        path.write_text(json.dumps(sealed(value)))
    else:
        with sqlite3.connect(bundle["database"]) as db:
            if fault in ("seed-row", "both"):
                db.execute("DELETE FROM seed_state")
            if fault in ("pointer", "both"):
                doc = json.loads(
                    db.execute("SELECT document FROM metadata").fetchone()[0]
                )
                doc["population"] = None
                db.execute("UPDATE metadata SET document=?", (canonical(doc),))
            if fault == "history":
                db.execute("DELETE FROM history WHERE id=2")
            if fault in ("rehash-definition", "seed-context", "imported-authority"):
                seed = json.loads(
                    db.execute("SELECT document FROM seed_state").fetchone()[0]
                )
                if fault == "rehash-definition":
                    seed["reference"]["definition_sha256"] = "0" * 64
                elif fault == "seed-context":
                    seed["context_sha256"] = "0" * 64
                else:
                    seed["original"]["tables"]["operations"] = [
                        {"imported_permission": True}
                    ]
                    seed["original_sha256"] = digest(seed["original"])
                seed.pop("content_sha256")
                db.execute(
                    "UPDATE seed_state SET document=?", (canonical(sealed(seed)),)
                )
    before = file_bytes(root)
    assert "error" in cli("inspect", "--root", root, success=False)
    assert "error" in cli(
        "reset", "--root", root, "--new-root", tmp_path / "new", success=False
    )
    assert file_bytes(root) == before
    assert not (tmp_path / "new").exists()


def test_exclusive_case_collisions_mutex_missing_roots_and_preserving_reset(tmp_path):
    root = tmp_path / "case"
    initialize(root, "starter_healthy_worker")
    before = file_bytes(root)
    assert "error" in cli("init", "--case", CASES[0], "--root", root, success=False)
    assert "error" in cli(
        "reset", "--root", root, "--new-root", root / "nested", success=False
    )
    assert file_bytes(root) == before
    lock = root / ".case-control.lock"
    lock.write_text("held by an owned test controller")
    assert "error" in cli("inspect", "--root", root, success=False)
    assert lock.read_text() == "held by an owned test controller"
    lock.unlink()
    assert "error" in cli("inspect", "--root", tmp_path / "missing", success=False)
    assert not (tmp_path / "missing").exists()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    assert "error" in cli("inspect", "--root", alias, success=False)


def actions_for(intent, unchanged=False):
    service, version, revision = (
        intent[k] for k in ("service", "version", "expected_revision")
    )
    status = ("deployment_status", {"service": service, "cached": False})
    actions = [
        status,
        (
            "deploy_version",
            {"service": service, "version": version, "expected_revision": revision},
        ),
        status,
    ]
    if not unchanged:
        actions += [
            (
                "check_rollout_health",
                {"service": service, "expected_revision": revision + 1},
            ),
            status,
        ]
    return actions


@pytest.mark.parametrize(
    "case_id",
    [
        "starter_healthy_worker",
        "starter_existing_target",
        "boundary_revision_exhausted",
        "boundary_last_health_revision",
        "boundary_unicode_literal",
    ],
)
def test_normal_actual_sdk_uses_cases_without_private_case_branches(case_id, tmp_path):
    bundle = initialize(tmp_path / "case", case_id)
    intent = bundle["intent"]
    actions = actions_for(intent, unchanged=case_id == "starter_existing_target")
    if case_id == "boundary_unicode_literal":
        actions.insert(0, ("deployment_status", {"service": "api", "cached": False}))
    canary = tmp_path / "literal-file.txt"
    canary.write_text("owned sentinel must never enter model request contents")
    request = bundle["request"] + f" This is a literal mention: @{canary}."

    def answer(body, _n, _request, _handler):
        if body["output"][0]["type"] == "message":
            body["output"][0]["content"][0][
                "text"
            ] = "Final synthetic status has been observed; actual committed state determines the result."
        return body

    with environment(
        tmp_path, actions=actions, mutate=answer, database=bundle["database"]
    ) as e:
        options = {**e["options"], "request": request, "intent": intent}
        if case_id == "boundary_revision_exhausted":
            receipt = rejected(options, "revision_exhausted")
            assert not receipt["observed_business"]["applied_decision_ids"]
            assert len(e["requests"]) == 2
        else:
            result = run_agent(**options)
            assert (
                result["business"]["outcome"]
                == bundle["private_reference"]["expected_outcome"]
            )
            assert result["business"]["rollout_succeeded"]
            assert result["cleanup_confirmed"]
            receipt = json.loads(Path(result["receipt_path"]).read_text())
            if case_id == "boundary_last_health_revision":
                assert result["business"]["current_target"]["revision"] == 2**53 - 1
            if case_id == "boundary_unicode_literal":
                wire = json.dumps(e["requests"], ensure_ascii=False)
                assert "Équipe Montréal" in wire and "東京運用" in wire
                assert "Literal @/tmp/firedrill-infra-canary" in wire
        wire = json.dumps(e["requests"])
        assert canary.read_text() not in wire
        assert "private_assessment" not in wire and bundle["seed_sha256"] not in wire
        assert bundle["instance_id"] not in wire
        assert receipt["cleanup_confirmed"]


@pytest.mark.parametrize(
    "case_id", ["busy_pending_foreign", "ambiguous_failed_unowned"]
)
def test_normal_agent_cannot_take_over_another_authors_pending_or_failed_rollout(
    case_id, tmp_path
):
    bundle = initialize(tmp_path / "case", case_id)
    intent = bundle["intent"]
    service, revision = intent["service"], intent["expected_revision"]
    name = (
        "check_rollout_health"
        if case_id == "busy_pending_foreign"
        else "rollback_version"
    )
    actions = [
        ("deployment_status", {"service": service, "cached": False}),
        (name, {"service": service, "expected_revision": revision}),
    ]
    with environment(tmp_path, actions=actions, database=bundle["database"]) as e:
        before = e["store"].snapshot()["business"]
        receipt = rejected({**e["options"], "intent": intent}, None)
        assert e["store"].snapshot()["business"] == before
        assert not receipt["acknowledged_effects"]
        assert receipt["cleanup_confirmed"]


@pytest.mark.parametrize(
    "descriptions",
    [None, {"orphan": "unknown service"}, {"api": True}, {"api": "x" * 8193}],
)
def test_new_business_description_schema_rejects_invalid_source_before_database_creation(
    descriptions, tmp_path
):
    fixture = json.loads(fixture_bytes(definition("starter_failed_recovery")))
    fixture["descriptions"] = descriptions
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture))
    with pytest.raises(ValueError):
        DeploymentStore.seed(tmp_path / "state.db", path)
    assert not (tmp_path / "state.db").exists()
