import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from conftest import ROOT, read_example
from src.main import main
from src.schemas import ScenarioExpectation, ScenarioInput, SchedulePlanningReport, ScheduleRequest
from src.services.draft_service import DraftService
from src.services.material_service import JsonMaterialService
from src.services.planning_service import PlanningService
from src.services.storage import SQLiteScheduleSource, SQLiteStore
from src.tools.draft_tools import save_draft

SCENARIOS = sorted(path.name for path in (ROOT / "examples").iterdir() if path.is_dir() and path.name != "__pycache__")


class FailedWriteStore(SQLiteStore):
    def write_draft(self, connection, **values):
        super().write_draft(connection, **values)
        raise sqlite3.OperationalError("simulated failure after INSERT")


def setup_case(tmp_path, name="01_normal"):
    scenario = ScenarioInput.model_validate(read_example(name))
    source = JsonMaterialService(ROOT / "examples" / name / "input.json")
    store = SQLiteStore(tmp_path / (name + ".sqlite3"))
    store.replace_schedule(scenario.initial_state.existing_schedule)
    return store, source, scenario


def run_step(store, source, scenario, step=0):
    item = scenario.steps[step]
    return save_draft(
        item.request, scenario.rules, DraftService(store, source), evaluated_at=item.evaluated_at,
    )


@pytest.mark.parametrize("name", SCENARIOS)
def test_save_scenarios_match_final_business_expectations(tmp_path, name):
    store, source, scenario = setup_case(tmp_path, name)
    expected = ScenarioExpectation.model_validate(read_example(name, "expected.json"))
    results = []
    for index, (step, desired) in enumerate(zip(scenario.steps, expected.results, strict=True)):
        active = FailedWriteStore(store.path) if step.save_behavior == "fail" else store
        result = run_step(active, source, scenario, index)
        results.append(result)
        assert result.review == desired.review
        assert result.scheduling == desired.scheduling
        assert result.evaluated_at == desired.evaluated_at
        assert result.confirmation.status == desired.confirmation.status
        assert result.persistence.status == desired.persistence.status
        assert bool(result.persistence.draft_id) == bool(desired.persistence.draft_id)
        assert [error.code for error in result.errors] == [error.code for error in desired.errors]
        assert result.publication_status == "not_published"
    assert store.count() == expected.final_state.draft_count
    if name == "05_duplicate_request":
        assert results[0].persistence.draft_id == results[1].persistence.draft_id


def test_saved_data_survives_new_store_instance(tmp_path):
    store, source, scenario = setup_case(tmp_path)
    result = run_step(store, source, scenario)
    row = SQLiteStore(store.path).get(result.request_id)
    document = json.loads(row["report_json"])
    assert row["draft_id"] == result.persistence.draft_id
    assert document["result"] == result.model_dump(mode="json")
    assert json.loads(row["rules_json"])["version"] == scenario.rules.version
    query = next(action for action in document["actions"] if action["tool"] == "query_schedule")
    assert query["source"].startswith("sqlite:")
    with closing(sqlite3.connect(store.path)) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert tables == {"scheduled_slots", "drafts"}


def test_retry_rechecks_occupancy_and_updates_same_draft(tmp_path):
    store, source, scenario = setup_case(tmp_path, "04_time_conflict")
    first = run_step(store, source, scenario)
    store.replace_schedule([])
    second = run_step(store, source, scenario)
    assert first.scheduling.status == "alternative_proposed"
    assert first.scheduling.suggested_at.hour == 19
    assert second.scheduling.status == "available"
    assert second.scheduling.accepted_at.hour == 18
    assert second.persistence.status == "already_exists"
    assert second.persistence.draft_id == first.persistence.draft_id
    assert store.count() == 1
    assert json.loads(store.get(second.request_id)["report_json"])["result"] == second.model_dump(mode="json")


def test_same_request_id_with_changed_input_is_rejected(tmp_path):
    store, source, scenario = setup_case(tmp_path)
    first = run_step(store, source, scenario)
    previous = store.get(first.request_id)
    step = scenario.steps[0]
    changed = step.request.model_dump()
    changed["time_requirement"]["requested_at"] = "2026-11-02T19:00:00+08:00"
    request = ScheduleRequest.model_validate(changed, context={"now": step.evaluated_at})
    result = DraftService(store, source).save(request, scenario.rules, evaluated_at=step.evaluated_at)
    assert result.errors[0].code == "IDEMPOTENCY_CONFLICT"
    assert result.persistence.status == "not_attempted"
    assert store.get(first.request_id) == previous


def test_pending_draft_never_occupies_formal_slot(tmp_path):
    store, source, scenario = setup_case(tmp_path, "04_time_conflict")
    first = run_step(store, source, scenario)
    step = scenario.steps[0]
    values = step.request.model_dump()
    values["request_id"] = "ANOTHER-REQUEST"
    another = ScheduleRequest.model_validate(values, context={"now": step.evaluated_at})
    second = DraftService(store, source).save(another, scenario.rules, evaluated_at=step.evaluated_at)
    assert first.scheduling.accepted_at is second.scheduling.accepted_at is None
    assert first.scheduling.suggested_at == second.scheduling.suggested_at
    with closing(sqlite3.connect(store.path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM scheduled_slots").fetchone()[0] == 1
    assert store.count() == 2


def test_failed_update_rolls_back_and_keeps_previous_draft(tmp_path):
    store, source, scenario = setup_case(tmp_path, "04_time_conflict")
    first = run_step(store, source, scenario)
    previous = store.get(first.request_id)
    store.replace_schedule([])
    failed = run_step(FailedWriteStore(store.path), source, scenario)
    assert failed.persistence.status == "failed"
    assert failed.persistence.draft_id is None
    assert store.get(first.request_id) == previous
    assert store.count() == 1


def test_commit_failure_is_not_reported_as_success(tmp_path, monkeypatch):
    store, source, scenario = setup_case(tmp_path)
    original_connect = sqlite3.connect

    class FailedCommit(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("simulated COMMIT failure")

    def connect(*args, **kwargs):
        return original_connect(*args, factory=FailedCommit, **kwargs)

    monkeypatch.setattr("src.services.storage.sqlite3.connect", connect)
    result = run_step(store, source, scenario)
    assert result.persistence.status == "failed"
    assert "COMMIT" in result.persistence.error.message
    assert result.scheduling.status == "available"
    assert result.persistence.draft_id is None
    assert store.count() == 0


def test_cannot_open_database_returns_explicit_failure(tmp_path):
    _, source, scenario = setup_case(tmp_path)
    directory = tmp_path / "directory-is-not-a-database"
    directory.mkdir()
    result = run_step(SQLiteStore(directory), source, scenario)
    assert result.review.status == "passed"
    assert result.scheduling.status == "not_evaluated"
    assert result.persistence.status == "failed"
    assert result.errors[0].code == "DRAFT_SAVE_FAILED"


def test_query_failure_rolls_back_without_creating_draft(tmp_path, monkeypatch):
    store, source, scenario = setup_case(tmp_path)

    def fail(self, channel, window):
        raise sqlite3.OperationalError("simulated query failure")

    monkeypatch.setattr(SQLiteScheduleSource, "query", fail)
    result = run_step(store, source, scenario)
    assert result.persistence.status == "not_attempted"
    assert result.errors[0].code == "TOOL_ERROR"
    assert store.count() == 0


def test_query_and_save_share_write_transaction(tmp_path, monkeypatch):
    store, source, scenario = setup_case(tmp_path)
    original_query = SQLiteScheduleSource.query
    observed = []

    def query(self, channel, window):
        observed.append(self.connection.in_transaction)
        with closing(sqlite3.connect(store.path, timeout=0, isolation_level=None)) as competitor:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competitor.execute("BEGIN IMMEDIATE")
        return original_query(self, channel, window)

    monkeypatch.setattr(SQLiteScheduleSource, "query", query)
    result = run_step(store, source, scenario)
    assert result.persistence.status == "saved"
    assert observed == [True]


def test_lost_connection_during_check_returns_failure(tmp_path, monkeypatch):
    store, source, scenario = setup_case(tmp_path)

    def disconnected(self, channel, window):
        self.connection.close()
        raise sqlite3.OperationalError("simulated lost connection")

    monkeypatch.setattr(SQLiteScheduleSource, "query", disconnected)
    result = run_step(store, source, scenario)
    assert result.persistence.status == "failed"
    assert result.persistence.draft_id is None
    assert {error.code for error in result.errors} == {"TOOL_ERROR", "DRAFT_SAVE_FAILED"}
    assert store.count() == 0


def test_save_guard_rejects_unconfirmed_fixed_time_change(tmp_path, monkeypatch):
    store, source, scenario = setup_case(tmp_path)
    original = PlanningService.plan_reviewed

    def forged(self, request, rules, reviewed, **kwargs):
        report = original(self, request, rules, reviewed, **kwargs)
        data = report.model_dump()
        data["result"]["scheduling"].update(
            suggested_at="2026-11-02T19:00:00+08:00",
            accepted_at="2026-11-02T19:00:00+08:00",
        )
        return SchedulePlanningReport.model_validate(data)

    monkeypatch.setattr(PlanningService, "plan_reviewed", forged)
    result = run_step(store, source, scenario)
    assert result.persistence.status == "failed"
    assert result.persistence.draft_id is None
    assert store.count() == 0


@pytest.mark.parametrize("name,step", [("02_missing_metadata", 0), ("03_rule_risk", 0)])
def test_ineligible_review_does_not_open_database(tmp_path, name, step):
    scenario = ScenarioInput.model_validate(read_example(name))
    source = JsonMaterialService(ROOT / "examples" / name / "input.json")
    store = SQLiteStore(tmp_path / "should-not-exist.sqlite3")
    result = run_step(store, source, scenario, step)
    assert result.persistence.status == "not_attempted"
    assert not store.path.exists()


def test_save_false_does_not_write(tmp_path):
    store, source, scenario = setup_case(tmp_path)
    item = scenario.steps[0]
    values = item.request.model_dump()
    values["save_draft"] = False
    request = ScheduleRequest.model_validate(values, context={"now": item.evaluated_at})
    result = DraftService(store, source).save(request, scenario.rules, evaluated_at=item.evaluated_at)
    assert result.persistence.status == "not_attempted"
    assert store.count() == 0


def test_concurrent_duplicate_saves_produce_one_row(tmp_path):
    store, source, scenario = setup_case(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: run_step(SQLiteStore(store.path), source, scenario), range(2)))
    assert {result.persistence.status for result in results} == {"saved", "already_exists"}
    assert len({result.persistence.draft_id for result in results}) == 1
    assert store.count() == 1


def test_save_cli_runs_duplicate_steps_in_same_database(tmp_path, capsys):
    database = tmp_path / "cli.sqlite3"
    assert main([
        "save-example", str(ROOT / "examples/05_duplicate_request/input.json"),
        "--database", str(database),
    ]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["draft_count"] == 1
    assert [result["persistence"]["status"] for result in output["results"]] == ["saved", "already_exists"]
    assert output["results"][0]["persistence"]["draft_id"] == output["results"][1]["persistence"]["draft_id"]


def test_save_cli_simulated_failure_returns_nonzero(tmp_path, capsys):
    assert main([
        "save-example", str(ROOT / "examples/06_save_failure/input.json"),
        "--database", str(tmp_path / "failed.sqlite3"),
    ]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["results"][0]["persistence"]["status"] == "failed"
    assert output["results"][0]["persistence"]["draft_id"] is None
    assert output["draft_count"] == 0


def test_invalid_cli_step_does_not_change_database(tmp_path, capsys):
    database = tmp_path / "untouched.sqlite3"
    assert main([
        "save-example", str(ROOT / "examples/01_normal/input.json"),
        "--database", str(database), "--step", "0",
    ]) == 1
    assert not database.exists()
