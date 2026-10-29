import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import ROOT, read_example
from src.main import main
from src.schemas import (
    RuleConfig, ScenarioExpectation, ScenarioInput,
    ScheduleRequest, ScheduleSnapshot, ScheduledSlot, TimeWindow,
)
from src.services.material_service import JsonMaterialService
from src.services.planning_service import PlanningService
from src.services.schedule_service import JsonScheduleService, SlotSearchService
from src.tools.schedule_tools import find_alternative_slots, query_schedule

NOW = datetime.fromisoformat("2026-11-02T09:00:00+08:00")
SCENARIOS = sorted(path.name for path in (ROOT / "examples").iterdir() if path.is_dir() and path.name != "__pycache__")


def at(hour, minute=0):
    return datetime(2026, 11, 2, hour, minute, tzinfo=timezone(timedelta(hours=8)))


def slot(start, end, channel="wechat", identity="S1"):
    return ScheduledSlot(schedule_id=identity, channel=channel, start_at=start, end_at=end)


@pytest.fixture
def rules():
    return RuleConfig.model_validate(read_example("01_normal")["rules"])


def request_with(**updates):
    raw = read_example("01_normal")["steps"][0]["request"]
    raw.update(updates)
    return ScheduleRequest.model_validate(raw, context={"now": NOW})


def snapshot(request, records=(), now=NOW):
    return ScheduleSnapshot(
        channel=request.channel, window=request.search_window,
        queried_at=now, existing_schedule=list(records),
    )


def candidates(request, rules, records=()):
    return SlotSearchService().find(request, rules, snapshot(request, records)).candidates


def source_file(tmp_path, records, **extra):
    path = tmp_path / "schedule.json"
    path.write_text(json.dumps({
        "existing_schedule": [record.model_dump(mode="json") for record in records], **extra,
    }), encoding="utf-8")
    return JsonScheduleService(path)


def planner(source, slot_service=None):
    return PlanningService(
        JsonMaterialService(ROOT / "examples/01_normal/input.json"), source, slot_service=slot_service,
    )


@pytest.mark.parametrize("name", SCENARIOS)
def test_real_planning_matches_review_and_scheduling_from_each_example(name):
    path = ROOT / "examples" / name / "input.json"
    scenario = ScenarioInput.model_validate(read_example(name))
    expected = ScenarioExpectation.model_validate(read_example(name, "expected.json"))
    for step, desired in zip(scenario.steps, expected.results, strict=True):
        report = PlanningService(JsonMaterialService(path), JsonScheduleService(path)).plan(
            step.request, scenario.rules, evaluated_at=step.evaluated_at,
        )
        assert report.result.review == desired.review
        assert report.result.scheduling == desired.scheduling
        assert report.result.confirmation.status == desired.confirmation.status
        assert report.result.persistence.status == "not_attempted"
        assert report.result.publication_status == "not_published"
        assert report.model_validate_json(report.model_dump_json()) == report


def test_empty_formal_schedule_is_a_successful_query(tmp_path):
    source = source_file(tmp_path, [])
    request = request_with()
    receipt = query_schedule(request.channel, request.search_window, source, queried_at=NOW)
    assert receipt.status == "ok"
    assert receipt.data.existing_schedule == []
    assert receipt.data.queried_at == NOW
    assert receipt.source


@pytest.mark.parametrize("record", [
    slot(at(17), at(18)), slot(at(21), at(22)),
    slot(at(18), at(19), channel="weibo"),
])
def test_query_ignores_adjacent_or_other_channel_occupancy(tmp_path, record):
    source = source_file(tmp_path, [record])
    request = request_with()
    assert source.query(request.channel, request.search_window) == []


@pytest.mark.parametrize("start,end", [(at(17), at(19)), (at(20), at(22)), (at(17), at(22))])
def test_query_retains_occupancy_crossing_window_edges(tmp_path, start, end):
    source = source_file(tmp_path, [slot(start, end)])
    request = request_with()
    assert len(source.query(request.channel, request.search_window)) == 1


def test_drafts_never_reserve_formal_slots(tmp_path, rules):
    source = source_file(tmp_path, [], existing_drafts=[{"start_at": at(18).isoformat()}])
    report = planner(source).plan(request_with(), rules, evaluated_at=NOW)
    assert report.result.scheduling.accepted_at == at(18)


@pytest.mark.parametrize("start,end,expected_hour", [
    (at(18), at(19), 19),
    (at(18, 30), at(19, 30), 20),
    (at(17, 30), at(18, 30), 19),
])
def test_any_partial_overlap_blocks_a_candidate(rules, start, end, expected_hour):
    found = candidates(request_with(), rules, [slot(start, end)])
    assert found[0].start_at.hour == expected_hour
    assert found[0].requires_confirmation


def test_adjacent_occupancy_does_not_block_requested_time(rules):
    request = request_with()
    found = candidates(request, rules, [slot(at(19), at(20))])
    assert found[0].start_at == at(18)
    assert not found[0].requires_confirmation


def test_original_non_aligned_time_is_preserved(rules):
    raw = read_example("01_normal")["steps"][0]["request"]["time_requirement"]
    raw["requested_at"] = at(18, 15).isoformat()
    found = candidates(request_with(time_requirement=raw), rules)
    assert found[0].start_at == at(18, 15)


def test_same_distance_prefers_later_candidate(rules):
    requirement = {"mode": "fixed", "requested_at": at(19).isoformat()}
    request = request_with(time_requirement=requirement, search_window={"start": at(17).isoformat(), "end": at(22).isoformat()})
    found = candidates(request, rules, [slot(at(19), at(20))])
    assert [item.start_at.hour for item in found[:2]] == [20, 18]


def test_full_duration_must_fit_search_window(rules):
    request = request_with(search_window={"start": at(18).isoformat(), "end": at(18, 30).isoformat()})
    assert candidates(request, rules) == []


def test_original_outside_search_range_is_not_accepted_automatically(rules):
    request = request_with(search_window={"start": at(19).isoformat(), "end": at(21).isoformat()})
    found = candidates(request, rules)
    assert found[0].start_at == at(19)
    assert found[0].requires_confirmation


def test_range_boundary_does_not_offer_slot_starting_at_end(rules):
    request = request_with()
    found = candidates(request, rules, [
        slot(at(18), at(19), identity="S18"), slot(at(19), at(20), identity="S19"),
    ])
    assert [item.start_at.hour for item in found] == [20]


def test_candidate_step_and_duration_use_rule_configuration(rules):
    values = rules.model_dump()
    values.update(candidate_step_minutes=30, slot_duration_minutes=30)
    changed = RuleConfig.model_validate(values)
    found = candidates(request_with(), changed, [slot(at(18), at(18, 30))])
    assert found[0].start_at == at(18, 30)
    assert found[0].end_at == at(19)


def test_cross_midnight_candidates_use_next_local_day(rules):
    requirement = {"mode": "fixed", "requested_at": at(23).isoformat()}
    next_day = at(0) + timedelta(days=1)
    request = request_with(
        time_requirement=requirement,
        search_window={"start": at(23).isoformat(), "end": (next_day + timedelta(hours=2)).isoformat()},
    )
    found = candidates(request, rules, [slot(at(23), next_day)])
    assert found[0].start_at == next_day


def window_request(preferred=at(18), start=at(18), end=at(20)):
    return request_with(time_requirement={
        "mode": "window", "requested_at": preferred.isoformat(),
        "window_start": start.isoformat(), "window_end": end.isoformat(),
    })


def test_window_can_select_another_allowed_time_without_confirmation(rules):
    found = candidates(window_request(), rules, [slot(at(18), at(19))])
    assert found[0].start_at == at(19)
    assert not found[0].requires_confirmation


def test_window_candidates_stay_inside_original_bounds(rules):
    request = window_request(preferred=at(19, 30), end=at(20))
    found = candidates(request, rules)
    assert found[0].start_at == at(19)
    assert found[0].end_at <= request.time_requirement.window_end


def test_window_without_free_slot_requests_new_input(tmp_path, rules):
    request = window_request(end=at(19))
    report = planner(source_file(tmp_path, [slot(at(18), at(19))])).plan(request, rules, evaluated_at=NOW)
    assert report.result.scheduling.status == "no_slot"
    assert report.result.scheduling.suggested_at is None
    assert report.result.scheduling.accepted_at is None
    assert report.result.confirmation.pending_items[0].kind == "provide_time_window"
    assert request.time_requirement.window_end == at(19)


def test_window_requires_full_slot_duration(tmp_path, rules):
    request = window_request(preferred=at(18, 30), start=at(18, 30), end=at(19))
    report = planner(source_file(tmp_path, [])).plan(request, rules, evaluated_at=NOW)
    assert report.result.scheduling.status == "no_slot"
    assert report.result.scheduling.accepted_at is None




def test_utc_occupancy_blocks_equivalent_local_time(rules):
    occupied = slot(
        datetime(2026, 11, 2, 10, tzinfo=timezone.utc),
        datetime(2026, 11, 2, 11, tzinfo=timezone.utc),
    )
    assert candidates(request_with(), rules, [occupied])[0].start_at == at(19)




def test_changed_formal_occupancy_is_read_again(tmp_path, rules):
    source = source_file(tmp_path, [slot(at(18), at(19))])
    service = planner(source)
    first = service.plan(request_with(), rules, evaluated_at=NOW)
    source.path.write_text(json.dumps({
        "existing_schedule": [slot(at(19), at(20)).model_dump(mode="json")],
    }), encoding="utf-8")
    second = service.plan(request_with(), rules, evaluated_at=NOW)
    assert first.result.scheduling.suggested_at == at(19)
    assert second.result.scheduling.suggested_at == at(18)


@pytest.mark.parametrize("text", [
    "{", "[]", "{}", '{"existing_schedule": null}', '{"initial_state": []}',
])
def test_invalid_schedule_source_cannot_be_treated_as_empty(tmp_path, rules, text):
    path = tmp_path / "bad.json"
    path.write_text(text, encoding="utf-8")
    report = planner(JsonScheduleService(path)).plan(request_with(), rules, evaluated_at=NOW)
    assert report.result.scheduling.status == "not_evaluated"
    assert report.result.errors[0].code == "TOOL_ERROR"


def test_duplicate_schedule_id_is_a_query_error(tmp_path, rules):
    records = [slot(at(18), at(19)), slot(at(19), at(20))]
    report = planner(source_file(tmp_path, records)).plan(request_with(), rules, evaluated_at=NOW)
    assert report.result.errors[0].code == "TOOL_ERROR"


def test_schedule_query_failure_never_becomes_no_slot(rules):
    class FailedSource:
        reference = "test:failed"

        def query(self, channel, window):
            raise PermissionError("denied")

    report = planner(FailedSource()).plan(request_with(), rules, evaluated_at=NOW)
    assert report.result.scheduling.status == "not_evaluated"
    assert report.result.errors[0].code == "TOOL_ERROR"


def test_candidate_engine_failure_is_different_from_no_candidates(tmp_path, rules):
    class FailedEngine:
        def find(self, request, rules, snapshot):
            raise RuntimeError("unavailable")

    report = planner(source_file(tmp_path, []), FailedEngine()).plan(request_with(), rules, evaluated_at=NOW)
    assert report.result.scheduling.status == "not_evaluated"
    assert report.actions[-1].status == "error"


@pytest.mark.parametrize("channel", ["weibo", "wechat"])
def test_query_rejects_wrong_channel_or_out_of_scope_results(channel):
    class BadSource:
        reference = "test:bad"

        def query(self, requested_channel, window):
            return [slot(at(22), at(23), channel=channel)]

    request = request_with()
    assert query_schedule(request.channel, request.search_window, BadSource(), queried_at=NOW).status == "error"


@pytest.mark.parametrize("name,step", [("02_missing_metadata", 0), ("03_rule_risk", 0), ("03_rule_risk", 1)])
def test_non_passed_material_never_queries_schedule(name, step):
    class ForbiddenSource:
        def query(self, channel, window):
            raise AssertionError("schedule query should not happen")

    path = ROOT / "examples" / name / "input.json"
    data = ScenarioInput.model_validate(read_example(name))
    item = data.steps[step]
    report = PlanningService(JsonMaterialService(path), ForbiddenSource()).plan(
        item.request, data.rules, evaluated_at=item.evaluated_at,
    )
    assert not any(action.tool == "query_schedule" for action in report.actions)


def test_candidate_tool_rejects_snapshot_from_other_window(rules):
    request = request_with()
    data = snapshot(request)
    data.window = TimeWindow(start=at(19), end=at(21))
    assert find_alternative_slots(request, rules, data).status == "error"


def test_candidate_tool_rejects_forged_confirmation_flag(rules):
    class ForgedEngine:
        def find(self, request, rules, snapshot):
            raw = SlotSearchService().find(request, rules, snapshot).model_dump()
            raw["candidates"][0]["requires_confirmation"] = False
            return raw

    request = request_with()
    data = snapshot(request, [slot(at(18), at(19))])
    assert find_alternative_slots(request, rules, data, ForgedEngine()).status == "error"


def test_no_available_slots_is_a_successful_empty_candidate_set(rules):
    request = request_with()
    records = [slot(at(18), at(21))]
    receipt = find_alternative_slots(request, rules, snapshot(request, records))
    assert receipt.status == "ok"
    assert receipt.data.candidates == []


def test_plan_cli_runs_conflict_without_saving(capsys):
    assert main(["plan-example", str(ROOT / "examples/04_time_conflict/input.json")]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["result"]["scheduling"]["suggested_at"] == at(19).isoformat()
    assert report["result"]["scheduling"]["accepted_at"] is None
    assert report["result"]["persistence"]["status"] == "not_attempted"


def test_plan_cli_source_error_returns_nonzero(tmp_path, capsys):
    assert main([
        "plan-example", str(ROOT / "examples/01_normal/input.json"),
        "--schedule-source", str(tmp_path / "missing.json"),
    ]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["result"]["errors"][0]["code"] == "TOOL_ERROR"


def test_plan_cli_no_slot_is_a_business_result(capsys):
    assert main(["plan-example", str(ROOT / "examples/07_no_available_slot/input.json")]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["result"]["scheduling"]["status"] == "no_slot"
    assert report["result"]["scheduling"]["accepted_at"] is None


def test_live_planning_records_actual_query_clock(tmp_path, rules, monkeypatch):
    import src.services.review_service as review_module
    import src.tools.schedule_tools as schedule_module

    class ReviewClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    class QueryClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW + timedelta(minutes=10)

    monkeypatch.setattr(review_module, "datetime", ReviewClock)
    monkeypatch.setattr(schedule_module, "datetime", QueryClock)
    report = planner(source_file(tmp_path, [])).plan(request_with(), rules)
    query = next(action for action in report.actions if action.tool == "query_schedule")
    assert query.schedule_snapshot.queried_at == NOW + timedelta(minutes=10)
    assert report.result.evaluated_at == query.schedule_snapshot.queried_at
