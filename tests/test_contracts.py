from datetime import datetime, timezone
import pytest
from pydantic import TypeAdapter, ValidationError

from src.schemas import (
    Material, MaterialRecord, PersistenceResult,
    RuleConfig, ScenarioExpectation, ScenarioInput, ScheduleRequest,
    ScheduleResult, ScheduledSlot, TimeRequirement, TimeWindow,
)
from src.schemas.common import Timestamp
from conftest import ROOT, read_example

NOW = datetime.fromisoformat("2026-11-02T09:00:00+08:00")
SCENARIOS = sorted(path.name for path in (ROOT / "examples").iterdir() if path.is_dir() and path.name != "__pycache__")


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_all_examples_parse_and_round_trip(scenario):
    source = ScenarioInput.model_validate(read_example(scenario))
    expected = ScenarioExpectation.model_validate(read_example(scenario, "expected.json"))
    assert source.scenario_id == expected.scenario_id == scenario
    assert len(source.steps) == len(expected.results)
    assert ScenarioInput.model_validate_json(source.model_dump_json()) == source
    assert ScenarioExpectation.model_validate_json(expected.model_dump_json()) == expected
    for step, result in zip(source.steps, expected.results, strict=True):
        assert result.request_id == step.request.request_id
        assert result.scheduling.requested_at == step.request.time_requirement.requested_at


def test_incomplete_material_remains_available_for_supplementing():
    raw = read_example("02_missing_metadata")["initial_state"]["materials"][0]
    record = MaterialRecord.model_validate(raw)
    assert record.authorization_status is None
    assert "authorization_status" not in record.model_fields_set
    with pytest.raises(ValidationError) as error:
        Material.model_validate(raw)
    assert any(item["loc"] == ("authorization_status",) for item in error.value.errors())


def test_complete_material_requires_authorization_evidence():
    raw = read_example("01_normal")["initial_state"]["materials"][0]
    del raw["authorization_evidence"]
    with pytest.raises(ValidationError, match="authorization_evidence"):
        Material.model_validate(raw)


@pytest.mark.parametrize("value", ["", " \n "])
def test_blank_source_authorization_can_be_supplemented(value):
    record = MaterialRecord(material_id="M1", authorization_status=value)
    assert record.authorization_status is None


def test_invalid_authorization_value_is_not_treated_as_missing():
    with pytest.raises(ValidationError):
        MaterialRecord(material_id="M1", authorization_status="approved")


@pytest.mark.parametrize("status", ["unknown", "denied"])
def test_material_completeness_does_not_claim_authorization_passed(status):
    raw = read_example("01_normal")["initial_state"]["materials"][0]
    raw.update(authorization_status=status, authorization_evidence=None)
    assert Material.model_validate(raw).authorization_status == status


@pytest.mark.parametrize("field", ["material_id", "title", "content", "channel"])
def test_complete_material_rejects_blank_fields(field):
    raw = read_example("01_normal")["initial_state"]["materials"][0]
    raw[field] = " \n "
    with pytest.raises(ValidationError, match=field):
        Material.model_validate(raw)


def test_source_content_is_preserved_verbatim():
    raw = read_example("01_normal")["initial_state"]["materials"][0]
    raw["content"] = "  保留原文与空格。\n"
    assert Material.model_validate(raw).content == raw["content"]


@pytest.mark.parametrize("bad_time", [
    "2026-11-02T18:00:00", "2026-11-02", "2026-02-30T18:00:00+08:00",
    "2026-11-02T18:00:00+25:00", 1793613600, True,
    datetime(2026, 11, 2, 18),
])
def test_timezone_and_datetime_format_are_required(bad_time):
    with pytest.raises(ValidationError):
        TypeAdapter(Timestamp).validate_python(bad_time)


def test_equivalent_instants_are_compared_across_offsets(request_data):
    request_data["time_requirement"]["requested_at"] = "2026-11-02T10:00:00Z"
    request = ScheduleRequest.model_validate(request_data, context={"now": NOW})
    assert request.time_requirement.requested_at.astimezone(timezone.utc).hour == 10


@pytest.mark.parametrize("field", ["request_id", "material_id", "channel", "time_requirement", "save_draft"])
def test_request_missing_required_fields_reports_location(request_data, field):
    del request_data[field]
    with pytest.raises(ValidationError) as error:
        ScheduleRequest.model_validate(request_data, context={"now": NOW})
    assert any(item["loc"] == (field,) and item["type"] == "missing" for item in error.value.errors())


@pytest.mark.parametrize("field,value", [
    ("request_id", "  "), ("material_id", 123), ("save_draft", "true"),
    ("channel", ""), ("save_behavior", "fail"), ("publication_status", "published"),
])
def test_public_request_rejects_blank_coerced_and_internal_fields(request_data, field, value):
    request_data[field] = value
    with pytest.raises(ValidationError):
        ScheduleRequest.model_validate(request_data, context={"now": NOW})


@pytest.mark.parametrize("requested_at", ["2026-11-02T08:59:59+08:00", "2026-11-02T09:00:00+08:00"])
def test_request_rejects_past_or_current_requested_time(request_data, requested_at):
    request_data["time_requirement"]["requested_at"] = requested_at
    with pytest.raises(ValidationError, match="later than the evaluation"):
        ScheduleRequest.model_validate(request_data, context={"now": NOW})


def test_request_defaults_to_real_current_clock(request_data):
    request_data["time_requirement"]["requested_at"] = "2000-01-01T18:00:00+08:00"
    with pytest.raises(ValidationError, match="evaluation time"):
        ScheduleRequest.model_validate(request_data)


def test_request_rejects_naive_injected_clock(request_data):
    with pytest.raises(ValidationError):
        ScheduleRequest.model_validate(request_data, context={"now": datetime(2026, 11, 2, 9)})


def test_request_rejects_past_search_window(request_data):
    request_data["search_window"]["start"] = "2026-11-02T08:00:00+08:00"
    with pytest.raises(ValidationError, match="search_window"):
        ScheduleRequest.model_validate(request_data, context={"now": NOW})


def test_window_request_cannot_start_in_past(request_data):
    request_data["time_requirement"].update(
        mode="window", window_start="2026-11-02T08:00:00+08:00",
        window_end="2026-11-02T21:00:00+08:00",
    )
    with pytest.raises(ValidationError, match="allowed window"):
        ScheduleRequest.model_validate(request_data, context={"now": NOW})


def test_historical_fixture_uses_its_recorded_clock():
    raw = read_example("01_normal")
    for step in raw["steps"]:
        step["evaluated_at"] = step["evaluated_at"].replace("2026-11-02", "2000-01-01")
        requirement = step["request"]["time_requirement"]
        requirement["requested_at"] = requirement["requested_at"].replace("2026-11-02", "2000-01-01")
        window = step["request"]["search_window"]
        window.update({key: value.replace("2026-11-02", "2000-01-01") for key, value in window.items()})
    assert ScenarioInput.model_validate(raw).steps[0].evaluated_at.year == 2000


@pytest.mark.parametrize("start,end", [
    ("2026-11-02T18:00:00+08:00", "2026-11-02T18:00:00+08:00"),
    ("2026-11-02T19:00:00+08:00", "2026-11-02T18:00:00+08:00"),
    ("2026-11-02T10:00:00Z", "2026-11-02T18:00:00+08:00"),
])
def test_time_window_requires_positive_length(start, end):
    with pytest.raises(ValidationError, match="later"):
        TimeWindow(start=start, end=end)


def test_schedule_slot_rejects_reversed_interval():
    with pytest.raises(ValidationError, match="end_at"):
        ScheduledSlot(
            schedule_id="S1", channel="wechat",
            start_at="2026-11-02T19:00:00+08:00", end_at="2026-11-02T18:00:00+08:00",
        )


def test_fixed_requirement_rejects_implicit_flexible_window():
    with pytest.raises(ValidationError, match="fixed mode"):
        TimeRequirement(
            mode="fixed", requested_at="2026-11-02T18:00:00+08:00",
            window_start="2026-11-02T18:00:00+08:00",
        )


@pytest.mark.parametrize("preferred", ["2026-11-02T17:00:00+08:00", "2026-11-02T21:00:00+08:00"])
def test_window_preference_must_be_inside_half_open_window(preferred):
    with pytest.raises(ValidationError, match="inside"):
        TimeRequirement(
            mode="window", requested_at=preferred,
            window_start="2026-11-02T18:00:00+08:00",
            window_end="2026-11-02T21:00:00+08:00",
        )


def test_window_requirement_accepts_valid_preference():
    requirement = TimeRequirement(
        mode="window", requested_at="2026-11-02T18:00:00+08:00",
        window_start="2026-11-02T18:00:00+08:00", window_end="2026-11-02T21:00:00+08:00",
    )
    assert requirement.mode == "window"


@pytest.mark.parametrize("patch", [
    {"timezone": "Mars/Olympus"}, {"slot_duration_minutes": 0},
    {"candidate_step_minutes": -1}, {"candidate_step_minutes": True},
    {"allowed_channels": []}, {"allowed_channels": ["wechat", "wechat"]},
    {"required_material_fields": ["nonexistent_field"]},
])
def test_rule_configuration_rejects_invalid_values(rule_data, patch):
    rule_data.update(patch)
    with pytest.raises(ValidationError):
        RuleConfig.model_validate(rule_data)


@pytest.mark.parametrize("patch", [
    {"status": "success"}, {"draft_id": None},
    {"error": {"code": "DRAFT_SAVE_FAILED", "message": "failure"}},
])
def test_persistence_cannot_claim_success_with_invalid_receipt(result_data, patch):
    result_data["persistence"].update(patch)
    with pytest.raises(ValidationError):
        PersistenceResult.model_validate(result_data["persistence"])


@pytest.mark.parametrize("path,value", [
    (("publication_status",), "published"),
    (("review", "status"), "approved"),
    (("review", "missing_fields"), ["content"]),
    (("confirmation", "status"), "pending"),
    (("scheduling", "accepted_at"), None),
    (("persistence", "status"), "failed"),
])
def test_result_rejects_invalid_or_contradictory_states(result_data, path, value):
    target = result_data
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        ScheduleResult.model_validate(result_data)


def test_alternative_cannot_be_silently_accepted():
    raw = read_example("04_time_conflict", "expected.json")["results"][0]
    raw["scheduling"]["accepted_at"] = raw["scheduling"]["suggested_at"]
    with pytest.raises(ValidationError, match="no accepted_at"):
        ScheduleResult.model_validate(raw)


def test_confirmation_must_refer_to_actual_proposal():
    raw = read_example("04_time_conflict", "expected.json")["results"][0]
    raw["confirmation"]["pending_items"][0]["proposed_value"] = "2026-11-02T20:00:00+08:00"
    with pytest.raises(ValidationError, match="match the original and proposed"):
        ScheduleResult.model_validate(raw)


def test_save_failure_must_not_include_fabricated_id():
    raw = read_example("06_save_failure", "expected.json")["results"][0]
    raw["persistence"]["draft_id"] = "FAKE"
    with pytest.raises(ValidationError, match="uncommitted"):
        ScheduleResult.model_validate(raw)


def test_passed_review_without_schedule_cannot_claim_saved_draft(result_data):
    result_data["scheduling"].update(
        status="not_evaluated", suggested_at=None, accepted_at=None,
    )
    with pytest.raises(ValidationError, match="evaluated schedule"):
        ScheduleResult.model_validate(result_data)






def test_duplicate_fixture_reuses_id_with_latest_evaluation():
    source = ScenarioInput.model_validate(read_example("05_duplicate_request"))
    results = ScenarioExpectation.model_validate(read_example("05_duplicate_request", "expected.json")).results
    assert source.steps[1].evaluated_at == results[1].evaluated_at
    assert results[1].persistence.status == "already_exists"
    assert results[0].persistence.draft_id == results[1].persistence.draft_id


def test_no_slot_example_never_accepts_or_invents_a_time():
    raw = read_example("07_no_available_slot", "expected.json")["results"][0]
    result = ScheduleResult.model_validate(raw)
    assert result.scheduling.status == "no_slot"
    assert result.scheduling.suggested_at is None
    assert result.scheduling.accepted_at is None
    assert result.confirmation.pending_items[0].kind == "provide_time_window"
    raw["scheduling"]["suggested_at"] = "2026-11-02T21:00:00+08:00"
    with pytest.raises(ValidationError, match="cannot select a time"):
        ScheduleResult.model_validate(raw)
