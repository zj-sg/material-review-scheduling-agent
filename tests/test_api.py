"""Exercise the HTTP boundary with the actual workflow and SQLite, without provider calls."""

import json
import socket
import sqlite3

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

from conftest import read_example
from src.agents.offline import OfflineTestModel
from src.api import create_app
from src.config import Settings
from src.main import _FailingSaveStore
from src.services.storage import SQLiteStore, request_key
from src.schemas import ScheduleRequest, ScheduledSlot


@pytest.fixture(autouse=True)
def forbid_provider_connections(monkeypatch):
    original_connect = socket.socket.connect
    original_getaddrinfo = socket.getaddrinfo

    def local_connect(connection, address):
        # Windows asyncio uses a loopback socket pair to wake its event loop.
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1", "localhost"}:
            return original_connect(connection, address)
        raise AssertionError("HTTP acceptance tests must not call a provider")

    def local_getaddrinfo(host, *args, **kwargs):
        if host in {"127.0.0.1", "::1", "localhost"}:
            return original_getaddrinfo(host, *args, **kwargs)
        raise AssertionError("HTTP acceptance tests must not resolve a provider")

    monkeypatch.setattr(socket.socket, "connect", local_connect)
    monkeypatch.setattr(socket, "getaddrinfo", local_getaddrinfo)


def setup_case(tmp_path, name="04_time_conflict", *, fail_save=False, model_factory=None):
    # HTTP uses wall time, unlike fixture replay. Keep these boundary tests in the future.
    raw = json.loads(json.dumps(read_example(name)).replace("2026-11-02", "2099-11-02"))
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"rules": raw["rules"], "initial_state": raw["initial_state"]}), encoding="utf-8")
    settings = Settings(
        _env_file=None, api_data_path=path, database_path=tmp_path / "api.sqlite3", api_offline=True,
    )
    store = (_FailingSaveStore if fail_save else SQLiteStore)(settings.database_path)
    app = create_app(settings=settings, store=store, model_factory=model_factory)
    return app, settings, store, [step["request"] for step in raw["steps"]]


def test_http_conflict_duplicate_and_persisted_draft(tmp_path):
    app, _, store, requests = setup_case(tmp_path)
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok", "mode": "offline_test"}
        first = client.post("/schedule", json=requests[0])
        assert first.status_code == 200
        body = first.json()
        assert body["decision"] == "request_confirmation"
        result = body["result"]
        assert result["scheduling"]["suggested_at"].startswith("2099-11-02T19:")
        assert result["scheduling"]["accepted_at"] is None
        assert result["confirmation"]["status"] == "pending"
        assert result["publication_status"] == "not_published"
        assert body["status"] == "waiting_for_human"
        assert result["persistence"]["status"] == "not_attempted"
        assert [tool["name"] for tool in body["tool_calls"]] == [
            "read_material", "check_rules", "query_slots", "refresh_time_choices",
        ]
        assert store.count() == 0
        assert client.get("/drafts/" + requests[0]["request_id"]).status_code == 404
        assert client.get("/runs/" + body["run_id"]).json() == body
        repeated = client.post("/schedule", json=requests[0]).json()
        assert repeated["run_id"] == body["run_id"]
        assert repeated["interrupt"] == body["interrupt"]
        # Choose 20:00 instead of the first candidate (19:00).
        resumed = choose(client, body, index=1)
        assert resumed.status_code == 200
        result = resumed.json()["result"]
        assert resumed.json()["status"] == "completed"
        assert resumed.json()["interrupt"] is None
        assert result["scheduling"]["requested_at"].startswith("2099-11-02T18:")
        assert result["scheduling"]["accepted_at"].startswith("2099-11-02T20:")
        assert result["confirmation"]["status"] == "confirmed"
        assert result["confirmation"]["interrupt_id"] == body["interrupt"]["interrupt_id"]
        assert result["persistence"]["status"] == "saved"
        assert store.get(requests[0]["request_id"])["request_json"] == request_key(ScheduleRequest.model_validate(requests[0]))
        draft = client.get("/drafts/" + requests[0]["request_id"])
        assert draft.status_code == 200
        assert draft.json() == result
        next_wait = client.post("/schedule", json=requests[0]).json()
        second = choose(client, next_wait, index=1).json()
        assert second["result"]["persistence"]["status"] == "already_exists"
        assert second["result"]["persistence"]["draft_id"] == result["persistence"]["draft_id"]
        assert store.count() == 1


@pytest.mark.parametrize("name,decisions,count", [
    ("01_normal", ["ready"], 1),
    ("02_missing_metadata", ["request_info"], 0),
    ("03_rule_risk", ["block", "request_confirmation"], 1),
    ("05_duplicate_request", ["ready", "ready"], 1),
    ("07_no_available_slot", ["request_info"], 1),
])
def test_business_outcomes_are_http_200(tmp_path, name, decisions, count):
    app, _, store, requests = setup_case(tmp_path, name)
    with TestClient(app) as client:
        for payload, decision in zip(requests, decisions, strict=True):
            response = client.post("/schedule", json=payload)
            assert response.status_code == 200
            assert response.json()["decision"] == decision
        assert store.count() == count


def test_failed_write_returns_500_and_rolls_back(tmp_path):
    app, _, store, requests = setup_case(tmp_path, "06_save_failure", fail_save=True)
    with TestClient(app) as client:
        response = client.post("/schedule", json=requests[0])
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "DRAFT_SAVE_FAILED"
        assert response.json()["result"]["persistence"]["status"] == "failed"
        assert store.count() == 0
        assert client.get("/drafts/" + requests[0]["request_id"]).status_code == 404


def test_changed_input_with_same_id_returns_409(tmp_path):
    app, _, store, requests = setup_case(tmp_path)
    with TestClient(app) as client:
        assert client.post("/schedule", json=requests[0]).status_code == 200
        changed = requests[0]
        changed["time_requirement"]["requested_at"] = "2099-11-02T20:00:00+08:00"
        response = client.post("/schedule", json=changed)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert store.count() == 0


def test_missing_material_and_draft_return_404(tmp_path):
    app, _, store, requests = setup_case(tmp_path)
    requests[0]["material_id"] = "UNKNOWN"
    with TestClient(app) as client:
        response = client.post("/schedule", json=requests[0])
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "MATERIAL_NOT_FOUND"
        assert client.get("/drafts/UNKNOWN").status_code == 404
        assert store.count() == 0


@pytest.mark.parametrize("extra", [
    {"evaluated_at": "2000-01-01T00:00:00Z"}, {"rules": {}}, {"save_behavior": "fail"},
    {"accepted_at": "2099-11-02T19:00:00+08:00"}, {"api_key": "must-not-echo"},
    {"max_model_calls": 1000},
])
def test_client_cannot_override_server_boundaries(tmp_path, extra):
    app, _, store, requests = setup_case(tmp_path)
    with TestClient(app) as client:
        response = client.post("/schedule", json=requests[0] | extra)
        assert response.status_code == 422
        assert "must-not-echo" not in response.text
        assert store.count() == 0


def test_expired_times_and_invalid_json_return_422(tmp_path):
    app, _, _, requests = setup_case(tmp_path)
    requests[0]["time_requirement"]["requested_at"] = "2000-01-01T18:00:00+08:00"
    with TestClient(app) as client:
        assert client.post("/schedule", json=requests[0]).status_code == 422
        assert client.post("/schedule", content="{", headers={"Content-Type": "application/json"}).status_code == 422


def test_model_failure_is_502_and_does_not_echo_secret(tmp_path):
    class FailedModel(OfflineTestModel):
        def invoke(self, messages):
            raise RuntimeError("must-not-echo-api-key")

    app, _, store, requests = setup_case(tmp_path, model_factory=lambda _: FailedModel())
    with TestClient(app) as client:
        response = client.post("/schedule", json=requests[0])
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "MODEL_ERROR"
        assert "must-not-echo-api-key" not in response.text
        assert store.count() == 0


def test_http_response_preserves_actual_model_observation_records(tmp_path):
    app, _, _, requests = setup_case(tmp_path, "02_missing_metadata")
    with TestClient(app) as client:
        response = client.post("/schedule", json=requests[0])
        assert response.status_code == 200
        body = response.json()
        steps = body["model_steps"]
        assert len(steps) == body["model_calls"] == 4
        assert steps[2]["observation"]["name"] == "check_rules"
        assert steps[2]["tool_calls"][0]["name"] == "supplement_material"
        assert steps[3]["observation"]["output"]["status"] == "empty"
        assert steps[3]["tool_calls"] == []


@pytest.mark.parametrize("cap", [1, 2, 3])
def test_model_call_limit_returns_explicit_incomplete_http_state(tmp_path, cap):
    _, settings, store, requests = setup_case(tmp_path, "01_normal")
    settings.max_model_calls = cap
    with TestClient(create_app(settings=settings, store=store)) as client:
        response = client.post("/schedule", json=requests[0])
        assert response.status_code == 409
        body = response.json()
        assert body["status"] == "incomplete"
        assert body["error"]["code"] == "LIMIT_EXCEEDED"
        assert body["model_calls"] == len(body["model_steps"]) == cap
        assert len(body["tool_calls"]) == cap
        assert body["execution_limits"] == {"max_model_calls": cap, "max_tool_calls": cap}
        assert store.count() == 0


def test_restart_preserves_empty_formal_schedule_and_existing_draft(tmp_path):
    app, settings, store, requests = setup_case(tmp_path)
    with TestClient(app) as client:
        first_wait = client.post("/schedule", json=requests[0]).json()
        first = choose(client, first_wait).json()
        store.replace_schedule([])
    restarted = create_app(settings=settings)
    with TestClient(restarted) as client:
        second = client.post("/schedule", json=requests[0]).json()
        assert second["decision"] == "ready"
        assert second["result"]["scheduling"]["accepted_at"].startswith("2099-11-02T18:")
        assert second["result"]["persistence"]["draft_id"] == first["result"]["persistence"]["draft_id"]
        assert store.count() == 1


def test_no_save_request_does_not_persist(tmp_path):
    app, _, store, requests = setup_case(tmp_path)
    requests[0]["save_draft"] = False
    with TestClient(app) as client:
        response = client.post("/schedule", json=requests[0])
        assert response.status_code == 200
        assert response.json()["result"]["persistence"]["status"] == "not_attempted"
        assert store.count() == 0


def test_unconfigured_live_server_fails_startup_without_fallback_or_database(tmp_path):
    _, settings, store, _ = setup_case(tmp_path)
    settings.api_offline = False
    with pytest.raises(ValueError, match="MATERIAL_AGENT_API_KEY"):
        with TestClient(create_app(settings=settings)):
            pass
    assert not store.path.exists()


def test_model_client_closes_even_if_database_initialization_fails(tmp_path):
    closed = []

    class ClosingModel(OfflineTestModel):
        def close(self):
            closed.append(True)

    setup_case(tmp_path)
    # A directory cannot be opened as a database file.
    app_with_bad_store = create_app(
        settings=Settings(_env_file=None, api_data_path=tmp_path / "catalog.json"),
        store=SQLiteStore(tmp_path), model_factory=lambda _: ClosingModel(),
    )
    with pytest.raises(sqlite3.OperationalError):
        with TestClient(app_with_bad_store):
            pass
    assert closed == [True]


def test_swagger_and_openapi_expose_request_and_response_models(tmp_path):
    app, _, _, _ = setup_case(tmp_path)
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 200
        schema = client.get("/openapi.json").json()
        assert "/schedule" in schema["paths"]
        assert "ScheduleRequest" in schema["components"]["schemas"]
        assert "AgentResponse" in schema["components"]["schemas"]


def choose(client, body, *, index=0, selected_at=None, interrupt_id=None):
    pending = body["interrupt"]
    return client.post("/runs/" + body["run_id"] + "/resume", json={
        "interrupt_id": interrupt_id or pending["interrupt_id"],
        "selected_at": selected_at or pending["candidates"][index]["start_at"],
    })


def test_selected_time_becomes_occupied_and_requires_a_new_choice(tmp_path):
    app, _, store, requests = setup_case(tmp_path)
    with TestClient(app) as client:
        first = client.post("/schedule", json=requests[0]).json()
        slots = json.loads((tmp_path / "catalog.json").read_text())["initial_state"]["existing_schedule"]
        slots.append({"schedule_id": "NEW-19", "channel": "wechat", "start_at": "2099-11-02T19:00:00+08:00", "end_at": "2099-11-02T20:00:00+08:00"})
        store.replace_schedule([ScheduledSlot.model_validate(item) for item in slots])
        response = choose(client, first)
        assert response.status_code == 200
        again = response.json()
        assert again["status"] == "waiting_for_human"
        assert again["interrupt"]["interrupt_id"] != first["interrupt"]["interrupt_id"]
        assert [item["start_at"] for item in again["interrupt"]["candidates"]] == ["2099-11-02T20:00:00+08:00"]
        assert again["result"]["scheduling"]["accepted_at"] is None
        assert store.count() == 0
        assert choose(client, first).status_code == 409
        final = choose(client, again).json()
        assert final["result"]["scheduling"]["accepted_at"] == "2099-11-02T20:00:00+08:00"
        assert store.count() == 1


@pytest.mark.parametrize("selected", ["2099-11-02T21:00:00+08:00", "2099-11-02T19:30:00+08:00"])
def test_choice_must_come_from_current_candidates(tmp_path, selected):
    app, _, store, requests = setup_case(tmp_path)
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=requests[0]).json()
        response = choose(client, waiting, selected_at=selected)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_SELECTION"
        assert client.get("/runs/" + waiting["run_id"]).json() == waiting
        assert store.count() == 0


def test_completed_run_cannot_resume_twice(tmp_path):
    app, _, store, requests = setup_case(tmp_path)
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=requests[0]).json()
        assert choose(client, waiting).status_code == 200
        repeated = choose(client, waiting)
        assert repeated.status_code == 409
        assert repeated.json()["error"]["code"] == "RUN_NOT_WAITING"
        assert store.count() == 1


def test_resume_failure_does_not_claim_saved_or_leave_partial_draft(tmp_path):
    app, _, store, requests = setup_case(tmp_path, fail_save=True)
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=requests[0]).json()
        response = choose(client, waiting)
        assert response.status_code == 500
        assert response.json()["result"]["persistence"]["status"] == "failed"
        assert response.json()["result"]["persistence"]["draft_id"] is None
        assert store.count() == 0


def test_source_is_rechecked_before_applying_selection(tmp_path):
    app, _, store, requests = setup_case(tmp_path)
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=requests[0]).json()
        path = tmp_path / "catalog.json"
        raw = json.loads(path.read_text())
        raw["initial_state"]["materials"][0]["content"] = "稳赚不赔"
        path.write_text(json.dumps(raw), encoding="utf-8")
        response = choose(client, waiting)
        assert response.status_code == 200
        assert response.json()["decision"] == "block"
        assert response.json()["result"]["confirmation"]["status"] != "confirmed"
        assert store.count() == 0


def test_no_save_request_can_select_time_without_writing(tmp_path):
    app, _, store, requests = setup_case(tmp_path)
    requests[0]["save_draft"] = False
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=requests[0]).json()
        assert waiting["status"] == "waiting_for_human"
        final = choose(client, waiting).json()
        assert final["result"]["confirmation"]["status"] == "confirmed"
        assert final["result"]["persistence"]["status"] == "not_attempted"
        assert store.count() == 0


def test_when_all_slots_fill_during_pause_return_no_slot(tmp_path):
    app, _, store, requests = setup_case(tmp_path)
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=requests[0]).json()
        store.replace_schedule([ScheduledSlot(
            schedule_id="FULL", channel="wechat",
            start_at="2099-11-02T18:00:00+08:00", end_at="2099-11-02T21:00:00+08:00",
        )])
        response = choose(client, waiting).json()
        assert response["status"] == "completed"
        assert response["decision"] == "request_info"
        assert response["result"]["scheduling"]["status"] == "no_slot"
        assert response["interrupt"] is None
        assert store.count() == 0


def test_freed_original_time_does_not_override_user_choice(tmp_path):
    app, _, store, requests = setup_case(tmp_path)
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=requests[0]).json()
        store.replace_schedule([])
        final = choose(client, waiting, index=1).json()
        assert final["result"]["scheduling"]["accepted_at"] == "2099-11-02T20:00:00+08:00"
        assert final["result"]["scheduling"]["requested_at"] == "2099-11-02T18:00:00+08:00"


def test_interrupts_are_backed_by_actual_graph_checkpoints(tmp_path):
    app, _, _, requests = setup_case(tmp_path)
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=requests[0]).json()
        config = {"configurable": {"thread_id": waiting["run_id"]}}
        snapshot = app.state.runs.graph.get_state(config)
        assert snapshot.next == ("choose_time",)
        assert snapshot.tasks[0].interrupts[0].id == waiting["interrupt"]["interrupt_id"]
        json.dumps(snapshot.values)
        assert choose(client, waiting).status_code == 200
        assert app.state.runs.graph.get_state(config).next == ()


def test_pending_runs_are_not_claimed_recoverable_after_restart(tmp_path):
    app, settings, store, requests = setup_case(tmp_path)
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=requests[0]).json()
    with TestClient(create_app(settings=settings)) as client:
        assert client.get("/runs/" + waiting["run_id"]).status_code == 404
        assert choose(client, waiting).status_code == 404
        fresh = client.post("/schedule", json=requests[0]).json()
        assert fresh["run_id"] != waiting["run_id"]
        assert fresh["status"] == "waiting_for_human"
        assert store.count() == 0


def test_interrupt_id_cannot_be_used_for_another_run(tmp_path):
    app, _, store, requests = setup_case(tmp_path)
    with TestClient(app) as client:
        first = client.post("/schedule", json=requests[0]).json()
        other = dict(requests[0], request_id="OTHER")
        second = client.post("/schedule", json=other).json()
        assert choose(client, second, interrupt_id=first["interrupt"]["interrupt_id"]).status_code == 409
        assert store.count() == 0


def test_expired_request_cannot_resume_with_historical_clock(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    class FutureClock:
        @staticmethod
        def now(tz):
            return datetime(2099, 11, 3, tzinfo=timezone.utc)

    app, _, store, requests = setup_case(tmp_path)
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=requests[0]).json()
        monkeypatch.setattr("src.agents.time_selection.datetime", FutureClock)
        response = choose(client, waiting)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "RUN_EXPIRED"
        assert store.count() == 0


def test_conflict_introduced_between_query_and_save_also_pauses(tmp_path):
    app, _, store, requests = setup_case(tmp_path, "01_normal")

    class LateConflict(OfflineTestModel):
        def invoke(self, messages):
            if isinstance(messages[-1], ToolMessage) and messages[-1].name == "query_slots":
                store.replace_schedule([ScheduledSlot(
                    schedule_id="LATE", channel="wechat", start_at="2099-11-02T18:00:00+08:00",
                    end_at="2099-11-02T19:00:00+08:00",
                )])
            return super().invoke(messages)

    with TestClient(app) as client:
        app.state.runs.model = LateConflict()
        waiting = client.post("/schedule", json=requests[0]).json()
        assert waiting["status"] == "waiting_for_human"
        assert waiting["result"]["persistence"]["status"] == "not_attempted"
        assert store.count() == 0
        assert choose(client, waiting).json()["result"]["persistence"]["status"] == "saved"


def test_window_preference_conflict_also_allows_user_selection(tmp_path):
    app, _, store, requests = setup_case(tmp_path)
    requests[0]["time_requirement"].update(
        mode="window", window_start="2099-11-02T18:00:00+08:00", window_end="2099-11-02T21:00:00+08:00",
    )
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=requests[0]).json()
        assert waiting["status"] == "waiting_for_human"
        assert waiting["result"]["scheduling"]["accepted_at"] is None
        final = choose(client, waiting, index=1).json()
        assert final["result"]["confirmation"]["status"] == "confirmed"
        assert final["result"]["scheduling"]["accepted_at"] == "2099-11-02T20:00:00+08:00"
        assert store.count() == 1


def test_model_cannot_batch_save_past_the_time_selection_boundary(tmp_path):
    class BatchedModel(OfflineTestModel):
        def invoke(self, messages):
            calls = [self.call(name, index).tool_calls[0] for index, name in enumerate(
                ["read_material", "check_rules", "query_slots", "save_draft"], start=1,
            )]
            return AIMessage(content="", tool_calls=calls)

    app, _, store, requests = setup_case(tmp_path, model_factory=lambda _: BatchedModel())
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=requests[0]).json()
        assert waiting["status"] == "failed"
        assert waiting["error"]["code"] == "MODEL_ERROR"
        assert "save_draft" not in [item["name"] for item in waiting["tool_calls"]]
        assert store.count() == 0


def test_simultaneous_resumes_apply_one_choice_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    app, _, store, requests = setup_case(tmp_path)
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=requests[0]).json()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(choose, client, waiting, index=index) for index in range(2)]
            responses = [future.result() for future in futures]
        assert sorted(response.status_code for response in responses) == [200, 409]
        assert store.count() == 1


@pytest.mark.parametrize("patch", [
    {"selected_at": "2099-11-02T19:00:00"}, {"confirmed": True},
])
def test_resume_input_requires_explicit_time_and_forbids_override_fields(tmp_path, patch):
    app, _, store, requests = setup_case(tmp_path)
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=requests[0]).json()
        payload = {"interrupt_id": waiting["interrupt"]["interrupt_id"], "selected_at": "2099-11-02T19:00:00+08:00"}
        payload.update(patch)
        response = client.post("/runs/" + waiting["run_id"] + "/resume", json=payload)
        assert response.status_code == 422
        assert store.count() == 0


def test_existing_request_cannot_change_content_after_server_restart(tmp_path):
    app, settings, store, requests = setup_case(tmp_path)
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=requests[0]).json()
        assert choose(client, waiting).status_code == 200
    requests[0]["time_requirement"]["requested_at"] = "2099-11-02T20:00:00+08:00"
    with TestClient(create_app(settings=settings)) as client:
        response = client.post("/schedule", json=requests[0])
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert store.count() == 1
