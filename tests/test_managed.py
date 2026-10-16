import asyncio
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

from infra_ops.store import DeploymentStore
from infra_ops.state import digest
from infra_ops.authority import sealed

FIXTURE = Path(__file__).parents[1] / "fixtures/starter.json"
INTENT = {
    "service": "api",
    "version": "2.1.0",
    "expected_revision": 1,
    "rollback_on_failure": True,
}


@pytest.mark.parametrize(
    "target",
    [
        "operation-permission",
        "observed-boolean-revision",
        "decision-extra-args",
        "decision-after-state",
    ],
)
def test_rehashing_tampered_authority_cannot_launder_wrong_source_or_permission(
    target, tmp_path
):
    store, key = setup(tmp_path)
    store.deploy("api", "2.1.0", 1, credential=key, proof=fresh(store, key))
    table = (
        "operations"
        if target == "operation-permission"
        else "observations" if target == "observed-boolean-revision" else "decisions"
    )
    with closing(sqlite3.connect(store.path)) as conn:
        row = conn.execute(
            f"SELECT id,document FROM {table} ORDER BY rowid LIMIT 1"
        ).fetchone()
        value = json.loads(row[1])
        if target == "operation-permission":
            value["allow_writes"], value["intent"] = False, None
        elif target == "observed-boolean-revision":
            value["value"]["revision"] = True
        elif target == "decision-extra-args":
            value["args"]["publish"] = True
        else:
            value["after"]["health"] = "healthy"
        value = sealed({k: v for k, v in value.items() if k != "content_sha256"})
        conn.execute(
            f"UPDATE {table} SET document=? WHERE id=?", (json.dumps(value), row[0])
        )
        conn.commit()
    with pytest.raises(ValueError):
        store.snapshot()


def setup(tmp_path, chosen=INTENT):
    store = DeploymentStore.seed(tmp_path / "state.db", FIXTURE)
    scope = store.register_operation(
        digest("exact synthetic API rollout"), True, chosen
    )
    return store, scope["credential"]


def fresh(store, credential, name="api"):
    return store.observe(name, credential=credential)["proof"]


def test_managed_actual_rollout_health_recovery_require_current_own_scope_and_verify_final_source(
    tmp_path,
):
    store, key = setup(tmp_path)
    original = store.snapshot()
    started = store.deploy("api", "2.1.0", 1, credential=key, proof=fresh(store, key))
    assert started["value"]["health"] == "rolling"
    with pytest.raises(ValueError):
        store.health_check("api", 2, credential=key)
    failed = store.health_check("api", 2, credential=key, proof=fresh(store, key))
    assert failed["value"]["health"] == "degraded"
    restored = store.rollback("api", 3, credential=key, proof=fresh(store, key))
    assert restored["value"]["version"] == "2.0.0"
    assert restored["value"]["health"] == "healthy"
    observed = store.observe("api", credential=key)
    assert observed["value"]["revision"] == 4
    final = store.snapshot()
    assert [d["event"] for d in final["records"]["decisions"]] == [
        "deploy_started",
        "health_failed",
        "rolled_back",
    ]
    assert all(
        d["operation_id"] == store.current_operation(key)["id"]
        for d in final["records"]["decisions"]
    )
    assert final["business"]["services"][1] == original["business"]["services"][1]
    assert len({d["read_id"] for d in final["records"]["decisions"]}) == 3
    store.close_operation(key)
    assert store.current_operation(key, allow_closed=True)["status"] == "closed"
    with pytest.raises(ValueError, match="operation_closed"):
        store.deploy("api", "2.1.0", 4, credential=key, proof=observed["proof"])
    assert store.snapshot()["business"] == final["business"]


@pytest.mark.parametrize(
    "kind", ["missing", "cached", "wrong-token", "other-operation", "synthetic-human"]
)
def test_no_guessed_cached_forged_or_borrowed_observation_can_start_a_rollout(
    kind, tmp_path
):
    store, key = setup(tmp_path)
    if kind == "missing":
        proof = None
    elif kind == "cached":
        proof = store.observe("api", cached=True, credential=key)["proof"]
    elif kind == "synthetic-human":
        proof = store.observe("api")["proof"]
    elif kind == "other-operation":
        other = store.register_operation(digest("other scope"), True, INTENT)
        proof = fresh(store, other["credential"])
    else:
        proof = {**fresh(store, key), "token": "0" * 64}
    with pytest.raises(ValueError):
        store.deploy("api", "2.1.0", 1, credential=key, proof=proof)
    final = store.snapshot()
    assert final["business"]["history"] == []
    assert final["records"]["decisions"] == []


def test_readonly_scope_cannot_mutate_even_with_genuine_current_status(tmp_path):
    store = DeploymentStore.seed(tmp_path / "state.db", FIXTURE)
    scope = store.register_operation(digest("read only"), False)
    with pytest.raises(ValueError, match="writes_not_authorized"):
        store.deploy(
            "api",
            "2.1.0",
            1,
            credential=scope["credential"],
            proof=fresh(store, scope["credential"]),
        )
    assert store.snapshot()["business"]["history"] == []


@pytest.mark.parametrize(
    "chosen",
    [
        {**INTENT, "service": "worker", "version": "1.5.0"},
        {**INTENT, "version": "9.9.9"},
        {**INTENT, "expected_revision": 2},
    ],
)
def test_exact_caller_service_version_and_initial_revision_cannot_be_expanded(
    chosen, tmp_path
):
    store, key = setup(tmp_path, chosen)
    with pytest.raises(ValueError):
        store.deploy("api", "2.1.0", 1, credential=key, proof=fresh(store, key))
    assert store.snapshot()["business"]["history"] == []


def test_failed_health_does_not_grant_rollback_without_exact_caller_permission(
    tmp_path,
):
    store, key = setup(tmp_path, {**INTENT, "rollback_on_failure": False})
    store.deploy("api", "2.1.0", 1, credential=key, proof=fresh(store, key))
    store.health_check("api", 2, credential=key, proof=fresh(store, key))
    with pytest.raises(ValueError, match="authorized_own_failed"):
        store.rollback("api", 3, credential=key, proof=fresh(store, key))
    store.close_operation(key)
    assert store.snapshot()["business"]["services"][0]["health"] == "degraded"
    assert len(store.snapshot()["records"]["decisions"]) == 2


def test_another_operation_cannot_health_check_or_recover_an_unowned_rollout(tmp_path):
    store, key = setup(tmp_path)
    store.deploy("api", "2.1.0", 1, credential=key, proof=fresh(store, key))
    other = store.register_operation(
        digest("different operation"), True, {**INTENT, "expected_revision": 2}
    )
    with pytest.raises(ValueError, match="initial_source_and_intent"):
        store.health_check(
            "api",
            2,
            credential=other["credential"],
            proof=fresh(store, other["credential"]),
        )
    assert store.snapshot()["business"]["services"][0]["health"] == "rolling"


def test_explicit_at_target_noop_consumes_current_read_without_repeating_business_effects(
    tmp_path,
):
    store, key = setup(tmp_path, {**INTENT, "version": "2.0.0"})
    proof = fresh(store, key)
    result = store.deploy("api", "2.0.0", 1, credential=key, proof=proof)
    assert result["value"]["outcome"] == "unchanged"
    assert result["decision"]["history_id"] is None
    with pytest.raises(ValueError, match="already_consumed"):
        store.deploy("api", "2.0.0", 1, credential=key, proof=proof)
    assert store.snapshot()["business"]["history"] == []
    again = store.deploy("api", "2.0.0", 1, credential=key, proof=fresh(store, key))
    assert again["decision"]["status"] == "unchanged"


def test_intervening_actual_human_transition_invalidates_an_earlier_operation_read(
    tmp_path,
):
    store, key = setup(tmp_path)
    proof = fresh(store, key)
    store.deploy("worker", "1.5.0", 1)
    with pytest.raises(ValueError, match="genuine_current"):
        store.deploy("api", "2.1.0", 1, credential=key, proof=proof)
    state = store.snapshot()
    assert state["records"]["decisions"][0]["operation_id"] is None
    assert state["business"]["services"][0]["version"] == "2.0.0"
    assert state["business"]["services"][1]["health"] == "rolling"


def test_interruption_before_actual_sqlite_commit_rolls_back_service_event_and_decision_together(
    tmp_path,
):
    store, key = setup(tmp_path)
    proof = fresh(store, key)
    original = store.snapshot()["original"]

    def interrupted():
        raise RuntimeError("controlled interruption before commit")

    store.commit_guard = interrupted
    with pytest.raises(RuntimeError, match="before commit"):
        store.deploy("api", "2.1.0", 1, credential=key, proof=proof)
    store.commit_guard = lambda: None
    assert store.snapshot()["original"] == original
    assert (
        store.deploy("api", "2.1.0", 1, credential=key, proof=proof)["value"]["outcome"]
        == "applied"
    )


@pytest.mark.parametrize(
    "fault",
    [
        "trigger",
        "view",
        "forged-rollback",
        "invented-cache",
        "changed-catalog",
        "duplicate-source",
        "lost-history",
    ],
)
def test_corrupt_persisted_source_rejects_on_open_and_before_any_action_without_repair(
    fault, tmp_path
):
    store, _key = setup(tmp_path)
    if fault == "lost-history":
        store.deploy("api", "2.1.0", 1)
    with closing(sqlite3.connect(store.path)) as conn:
        if fault == "trigger":
            conn.execute(
                "CREATE TRIGGER unexpected AFTER UPDATE ON services WHEN NEW.name='api' BEGIN UPDATE services SET version='1.5.0',revision=revision+1 WHERE name='worker'; END"
            )
        elif fault == "view":
            conn.execute("CREATE VIEW copied_services AS SELECT * FROM services")
        elif fault == "forged-rollback":
            conn.execute(
                "UPDATE services SET version='2.1.0',previous_version='9.9.9',health='degraded' WHERE name='api'"
            )
        elif fault == "invented-cache":
            conn.execute(
                "UPDATE status_cache SET snapshot=? WHERE name='api'",
                (
                    json.dumps(
                        {
                            "name": "worker",
                            "version": "9.9.9",
                            "previous_version": None,
                            "health": "healthy",
                            "revision": 99,
                        }
                    ),
                ),
            )
        elif fault == "changed-catalog":
            conn.execute(
                "UPDATE versions SET fails_health=0 WHERE service='api' AND version='2.1.0'"
            )
        elif fault == "duplicate-source":
            raw = conn.execute("SELECT document FROM metadata").fetchone()[0]
            conn.execute(
                "UPDATE metadata SET document=?",
                (raw.replace("{", '{"synthetic":false,', 1),),
            )
        else:
            conn.execute("DELETE FROM history")
        conn.commit()
    original = store.path.read_bytes()
    with pytest.raises(ValueError):
        DeploymentStore(store.path)
    with pytest.raises(ValueError):
        store.deploy("api", "2.1.0", 1)
    assert store.path.read_bytes() == original


def test_closed_authority_is_durable_even_if_current_business_source_is_invalid(
    tmp_path,
):
    store, key = setup(tmp_path)
    with closing(sqlite3.connect(store.path)) as conn:
        conn.execute("UPDATE services SET version='9.9.9' WHERE name='api'")
        conn.commit()
    store.close_operation(key)
    assert store.current_operation(key, allow_closed=True)["status"] == "closed"
    with pytest.raises(ValueError):
        store.snapshot()
    with closing(sqlite3.connect(store.path)) as conn:
        assert (
            conn.execute("SELECT version FROM services WHERE name='api'").fetchone()[0]
            == "9.9.9"
        )


def test_copied_or_replaced_database_cannot_borrow_another_file_instance(tmp_path):
    store, _key = setup(tmp_path)
    copied = tmp_path / "copy.db"
    copied.write_bytes(store.path.read_bytes())
    with pytest.raises(ValueError, match="identity changed"):
        DeploymentStore(copied)
    original = store.path
    moved = tmp_path / "old.db"
    original.rename(moved)
    original.write_bytes(moved.read_bytes())
    with pytest.raises(ValueError, match="identity changed"):
        store.snapshot()


def test_six_real_independent_processes_consume_distinct_reads_but_commit_one_scoped_rollout(
    tmp_path,
):
    store, key = setup(tmp_path)
    code = """import json,os,sys
from infra_ops.store import DeploymentStore
s=DeploymentStore(sys.argv[1]);key=os.environ['INFRA_TEST_OPERATION'];proof=s.observe('api',credential=key)['proof'];print('ready',flush=True);sys.stdin.readline()
try:print(json.dumps(s.deploy('api','2.1.0',1,credential=key,proof=proof)),flush=True)
except Exception as e:print(json.dumps({'error':str(e)}),flush=True)
"""

    async def compete():
        entries = []
        try:
            for _ in range(6):
                env = {
                    k: os.environ[k]
                    for k in (
                        "PATH",
                        "HOME",
                        "TMPDIR",
                        "TMP",
                        "TEMP",
                        "LANG",
                        "SystemRoot",
                        "WINDIR",
                    )
                    if k in os.environ
                }
                child = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    code,
                    str(store.path),
                    env={**env, "INFRA_TEST_OPERATION": key},
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                entries.append(child)
            ready = await asyncio.wait_for(
                asyncio.gather(*(c.stdout.readline() for c in entries)), 10
            )
            assert ready == [b"ready\n"] * 6
            for child in entries:
                child.stdin.write(b"go\n")
                await child.stdin.drain()
            lines = await asyncio.wait_for(
                asyncio.gather(*(c.communicate() for c in entries)), 10
            )
            assert all(c.returncode == 0 for c in entries)
            return [json.loads(out) for out, _err in lines]
        finally:
            for child in entries:
                if child.returncode is None:
                    child.kill()
                await child.wait()

    results = asyncio.run(compete())
    assert sum(r.get("value", {}).get("outcome") == "applied" for r in results) == 1
    final = store.snapshot()
    assert len(final["records"]["observations"]) == 6
    assert len(final["records"]["decisions"]) == 1
    assert len(final["business"]["history"]) == 1
    assert final["business"]["services"][0]["health"] == "rolling"
