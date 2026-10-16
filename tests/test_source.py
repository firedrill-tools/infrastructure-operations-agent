import json
from copy import deepcopy
from pathlib import Path

import pytest

from infra_ops.source import MAX_INTEGER
from infra_ops.store import DeploymentStore

FIXTURE = Path(__file__).parents[1] / "fixtures" / "starter.json"


def test_healthy_source_cannot_contradict_its_deterministic_failure_rule(tmp_path):
    data = json.loads(FIXTURE.read_text())
    data["failing_versions"]["api"].append("2.0.0")
    source, database = tmp_path / "source.json", tmp_path / "state.db"
    source.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="contradicts"):
        DeploymentStore.seed(database, source)
    assert not database.exists()


@pytest.mark.parametrize(
    "fault",
    [
        "health-string",
        "boolean-revision",
        "orphan-service",
        "orphan-failure",
        "unknown-initial-version",
        "duplicate-version",
        "unknown-field",
        "negative-revision",
    ],
)
def test_complete_invalid_fixture_rejects_before_any_database(fault, tmp_path):
    data = deepcopy(json.loads(FIXTURE.read_text()))
    if fault == "health-string":
        data["services"][0]["healthy"] = "false"
    elif fault == "boolean-revision":
        data["services"][0]["revision"] = True
    elif fault == "orphan-service":
        data["allowed_versions"]["ghost"] = ["8.0.0"]
    elif fault == "orphan-failure":
        data["failing_versions"]["api"] = ["9.9.9"]
    elif fault == "unknown-initial-version":
        data["services"][0]["version"] = "1.9.0"
    elif fault == "duplicate-version":
        data["allowed_versions"]["api"].append("2.0.0")
    elif fault == "unknown-field":
        data["services"][0]["published"] = True
    elif fault == "negative-revision":
        data["services"][0]["revision"] = -1
    fixture, db = tmp_path / "source.json", tmp_path / "state.db"
    fixture.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        DeploymentStore.seed(db, fixture)
    assert not db.exists()


@pytest.mark.parametrize(
    "raw",
    [
        b'{"services":[],"services":[],"allowed_versions":{}}',
        b'{"services":[],"allowed_versions":{},"x":NaN}',
        b'{"services":"\xff","allowed_versions":{}}',
    ],
)
def test_original_duplicate_nonfinite_or_nonutf8_bytes_reject_before_seed(
    raw, tmp_path
):
    fixture, db = tmp_path / "source.json", tmp_path / "state.db"
    fixture.write_bytes(raw)
    with pytest.raises((ValueError, UnicodeError)):
        DeploymentStore.seed(db, fixture)
    assert not db.exists()


def test_existing_dangling_symlink_cannot_author_a_different_owned_database(tmp_path):
    alias, outside = tmp_path / "alias.db", tmp_path / "outside-owned.db"
    alias.symlink_to(outside)
    with pytest.raises((OSError, ValueError)):
        DeploymentStore.seed(alias, FIXTURE)
    assert alias.is_symlink()
    assert not outside.exists()


def test_original_fixture_symlink_is_not_silently_followed(tmp_path):
    source, db = tmp_path / "fixture-link.json", tmp_path / "state.db"
    source.symlink_to(FIXTURE)
    with pytest.raises((OSError, ValueError)):
        DeploymentStore.seed(db, source)
    assert not db.exists()


def test_private_seed_existing_destination_is_never_overwritten(tmp_path):
    db = tmp_path / "state.db"
    store = DeploymentStore.seed(db, FIXTURE)
    original = db.read_bytes()
    with pytest.raises((OSError, ValueError)):
        DeploymentStore.seed(db, FIXTURE)
    assert db.read_bytes() == original
    assert db.stat().st_mode & 0o777 == 0o600
    assert store.status("api")["version"] == "2.0.0"


def test_maximum_safe_revision_cannot_overflow_into_a_committed_rollout(tmp_path):
    data = json.loads(FIXTURE.read_text())
    data["services"][0]["revision"] = MAX_INTEGER
    source, db = tmp_path / "source.json", tmp_path / "state.db"
    source.write_text(json.dumps(data))
    store = DeploymentStore.seed(db, source)
    before = store.status("api")
    with pytest.raises(ValueError, match="revision_exhausted"):
        store.deploy("api", "2.1.0", MAX_INTEGER)
    assert store.status("api") == before
    assert store.history("api") == []


def test_exact_direct_source_arguments_do_not_coerce_boolean_revisions_or_cached_flags(
    tmp_path,
):
    store = DeploymentStore.seed(tmp_path / "state.db", FIXTURE)
    with pytest.raises(ValueError, match="exact positive"):
        store.deploy("api", "2.1.0", True)
    with pytest.raises(ValueError, match="boolean cached"):
        store.status("api", cached="false")
    with pytest.raises(ValueError, match="stable version"):
        store.deploy("api", "02.1.0", 1)
    assert store.status("api")["revision"] == 1
    assert store.history("api") == []
