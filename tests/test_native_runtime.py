import json
from pathlib import Path

import pytest

from infra_ops.runtime import run_agent
from infra_ops.state import digest
from native_helper import environment, source, KEY, INTENT, ACTIONS


def rejected(options, pattern):
    with pytest.raises(BaseException, match=pattern) as caught:
        run_agent(**options)
    receipt = caught.value.evidence
    assert receipt["status"] == "failed"
    return receipt


def test_actual_owned_agents_sdk_native_json_public_tools_real_cli_and_sqlite_verify_failed_rollout_recovery(
    tmp_path,
):
    with environment(tmp_path) as e:
        result = run_agent(**e["options"])
        receipt = json.loads(Path(result["receipt_path"]).read_text())
        assert len(e["requests"]) == 9 and not e["errors"]
        assert len(receipt["actions"]) == 8 and len(receipt["commands"]) == 8
        assert result["cleanup_confirmed"], receipt["lifecycle"]
        assert result["business"]["workflow_complete"]
        assert result["business"]["outcome"] == "verified_recovery"
        assert result["business"]["rollout_succeeded"] is False
        assert result["business"]["recovery_verified"] is True
        assert result["business"]["current_target"]["version"] == "2.0.0"
        assert result["business"]["current_target"]["revision"] == 4
        assert len(result["business"]["applied_decision_ids"]) == 3
        assert all(
            c["pid"] > 0
            and c["exit_observed"]
            and c["exit_code"] == 0
            and c["stdout"]["complete"]
            and c["stderr"]["complete"]
            for c in receipt["commands"]
        )
        assert all(
            c["response"]["body"]["unknown_original"]["retained"]
            for c in receipt["calls"]
        )
        assert (
            result["usage"]["input"]["value"] == 90
            and result["usage"]["output"]["value"] == 18
        )
        assert result["usage"]["cached_input"]["value"] == 0
        assert result["cost_usd"] is None
        assert (
            source(receipt, receipt["final_source"])["original"]["tables"][
                "operations"
            ][0]["document"].find("closed")
            >= 0
        )
        assert all(
            digest(image["original"]) == sha
            for sha, image in receipt["source_images"].items()
        )
        assert KEY not in json.dumps(receipt)
        context = json.dumps(e["requests"])
        for private in (
            e["store"].instance_id,
            receipt["operation"]["id"],
            receipt["operation"]["credential_sha256"],
            "explicit_synthetic_fixture_author",
            str(e["store"].path),
        ):
            assert private not in context


@pytest.mark.parametrize(
    "kind", ["incomplete-response", "incomplete-item", "missing-item-status"]
)
def test_original_unfinished_deploy_cannot_reach_real_sdk_cli_mutation(kind, tmp_path):
    def mutate(body, n, _request, _handler):
        if n == 2:
            if kind == "incomplete-response":
                body.update(
                    status="incomplete",
                    incomplete_details={"reason": "max_output_tokens"},
                )
            elif kind == "incomplete-item":
                body["output"][0]["status"] = "incomplete"
            else:
                body["output"][0].pop("status")
        return body

    with environment(tmp_path, mutate=mutate) as e:
        receipt = rejected(e["options"], "original.*(completed|unfinished)")
        assert len(e["requests"]) == 2
        assert len(receipt["commands"]) == 1
        assert receipt["cleanup_confirmed"], receipt["lifecycle"]
        assert receipt["reported_usage"]["input"]["value"] == 20
        assert e["store"].snapshot()["business"]["history"] == []
        assert e["store"].snapshot()["records"]["operations"][0]["status"] == "closed"


def test_readonly_actual_sdk_tool_cannot_start_a_rollout(tmp_path):
    with environment(tmp_path) as e:
        receipt = rejected(
            {**e["options"], "allow_writes": False, "intent": None},
            "writes_not_authorized",
        )
        assert len(e["requests"]) == 2 and len(receipt["commands"]) == 1
        assert receipt["cleanup_confirmed"]
        assert e["store"].snapshot()["business"]["history"] == []


def test_different_valid_caller_service_intent_is_not_expanded_by_native_tool_arguments(
    tmp_path,
):
    with environment(tmp_path) as e:
        receipt = rejected(
            {
                **e["options"],
                "intent": {**INTENT, "service": "worker", "version": "1.5.0"},
            },
            "writes_not_authorized",
        )
        assert len(e["requests"]) == 2 and len(receipt["commands"]) == 2
        assert receipt["commands"][-1]["exit_code"] == 2
        assert receipt["cleanup_confirmed"]
        assert e["store"].snapshot()["business"]["history"] == []


def test_final_native_failure_preserves_actual_three_committed_scoped_transitions_and_verified_current_state(
    tmp_path,
):
    def mutate(body, n, _request, _handler):
        if n == 9:
            body.update(
                status="incomplete", incomplete_details={"reason": "max_output_tokens"}
            )
        return body

    with environment(tmp_path, mutate=mutate) as e:
        receipt = rejected(e["options"], "not fully completed")
        assert len(e["requests"]) == 9
        assert len(receipt["acknowledged_effects"]) == 3
        assert len(receipt["observed_business"]["applied_decision_ids"]) == 3
        assert receipt["observed_business"]["workflow_complete"]
        assert receipt["observed_business"]["current_target"]["version"] == "2.0.0"
        assert receipt["cleanup_confirmed"]


def test_complete_model_prose_after_only_rollout_start_does_not_complete_a_pending_deployment(
    tmp_path,
):
    with environment(tmp_path, actions=ACTIONS[:2]) as e:
        receipt = rejected(e["options"], "not verified complete")
        assert len(e["requests"]) == 3
        assert receipt["observed_business"]["outcome"] == "pending_health"
        assert receipt["observed_business"]["workflow_complete"] is False
        assert receipt["observed_business"]["current_target"]["health"] == "rolling"
        assert len(receipt["acknowledged_effects"]) == 1
        assert receipt["cleanup_confirmed"]


def test_missing_key_prepares_private_failed_receipt_before_source_operation_sdk_cli_or_http(
    tmp_path,
):
    with environment(tmp_path) as e:
        receipt = rejected({**e["options"], "api_key": ""}, "OPENAI_API_KEY")
        assert (
            e["requests"] == [] and receipt["calls"] == [] and receipt["commands"] == []
        )
        assert receipt["initial_source"] is None
        assert receipt["lifecycle"]["sdk_started"] is False
        assert receipt["cleanup_confirmed"]
        assert e["store"].snapshot()["records"]["operations"] == []


def test_injected_sdk_or_transport_replacement_is_rejected_before_any_resource(
    tmp_path,
):
    with environment(tmp_path) as e:
        receipt = rejected({**e["options"], "client": object()}, "replacements")
        assert receipt["cleanup_confirmed"]
        assert e["requests"] == []
        assert e["store"].snapshot()["records"]["operations"] == []
