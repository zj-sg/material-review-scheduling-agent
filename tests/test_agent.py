"""Offline acceptance only: real LangGraph/services, controlled model responses."""

import json
import socket

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage, convert_to_messages
from langchain_core.tools import BaseTool

from conftest import ROOT, read_example
from src.agents.model import CompatibleChatModel, DeepSeekChatModel, build_model
from src.agents.offline import OfflineTestModel
from src.agents.workflow import ReviewSchedulingAgent
from src.config import Settings
from src.main import _FailingSaveStore, main
from src.schemas import ScenarioExpectation, ScenarioInput, ScheduleRequest
from src.services.material_service import JsonMaterialService
from src.services.storage import SQLiteStore
from examples.verify_react import inspect_evidence

SCENARIOS = sorted(path.name for path in (ROOT / "examples").iterdir() if path.is_dir() and path.name != "__pycache__")


@pytest.fixture(autouse=True)
def forbid_external_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("offline tests must not access the network")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def case(tmp_path, name="01_normal", *, source=None):
    scenario = ScenarioInput.model_validate(read_example(name))
    store = SQLiteStore(tmp_path / (name + ".sqlite3"))
    store.replace_schedule(scenario.initial_state.existing_schedule)
    material_source = JsonMaterialService(source or ROOT / "examples" / name / "input.json")
    return scenario, store, material_source


def run(model, scenario, store, source, step=0, **options):
    item = scenario.steps[step]
    return ReviewSchedulingAgent(model, source, store, **options).run(
        item.request, scenario.rules, evaluated_at=item.evaluated_at,
    )


class ScriptedModel(OfflineTestModel):
    mode = "offline_test"

    def __init__(self, replies):
        self.replies = iter(replies)

    def invoke(self, messages):
        return next(self.replies)


@pytest.mark.parametrize("name", SCENARIOS)
def test_offline_agent_matches_all_business_scenarios(tmp_path, name):
    scenario, store, source = case(tmp_path, name)
    expected = ScenarioExpectation.model_validate(read_example(name, "expected.json"))
    outcomes = []
    for index, desired in enumerate(expected.results):
        active = _FailingSaveStore(store.path) if scenario.steps[index].save_behavior == "fail" else store
        outcome = run(OfflineTestModel(), scenario, active, source, index)
        outcomes.append(outcome)
        assert outcome.mode == "offline_test"
        assert outcome.result.review == desired.review
        assert outcome.result.scheduling == desired.scheduling
        assert outcome.result.persistence.status == desired.persistence.status
        assert outcome.result.confirmation.status == desired.confirmation.status
        assert [item.code for item in outcome.result.errors] == [item.code for item in desired.errors]
        assert outcome.result.publication_status == "not_published"
        assert outcome.status == ("failed" if name == "06_save_failure" else "completed")
        assert outcome.model_calls >= 2
        assert outcome.tool_calls[0]["name"] == "read_material"
    assert store.count() == expected.final_state.draft_count
    if name == "05_duplicate_request":
        assert outcomes[0].result.persistence.draft_id == outcomes[1].result.persistence.draft_id
    if name == "04_time_conflict":
        assert outcomes[0].decision == "request_confirmation"


def test_agent_chooses_supplement_then_rechecks(tmp_path):
    scenario, store, source = case(
        tmp_path, "02_missing_metadata", source=ROOT / "examples/materials-with-supplement.json",
    )
    outcome = run(OfflineTestModel(), scenario, store, source)
    assert outcome.status == "completed"
    assert outcome.result.persistence.status == "saved"
    assert [item["name"] for item in outcome.tool_calls] == [
        "read_material", "check_rules", "supplement_material", "check_rules", "query_slots", "save_draft",
    ]


def test_invented_success_text_is_not_a_business_result(tmp_path):
    scenario, store, source = case(tmp_path)
    model = ScriptedModel([AIMessage(content="published, saved DRAFT-FAKE")])
    outcome = run(model, scenario, store, source)
    assert outcome.status == "failed"
    assert outcome.error["code"] == "AGENT_INCOMPLETE"
    assert outcome.result is None
    assert outcome.model_steps[0]["content"] == "published, saved DRAFT-FAKE"
    assert outcome.tool_calls == []
    assert store.count() == 0


@pytest.mark.parametrize("name,arguments", [
    ("save_draft", {"accepted_at": "2026-11-02T19:00:00+08:00", "confirmed": True}),
    ("read_material", {"material_id": "UNAUTHORIZED"}),
    ("publish", {}),
])
def test_tool_parameters_cannot_override_bound_request(tmp_path, name, arguments):
    scenario, store, source = case(tmp_path)
    model = ScriptedModel([
        OfflineTestModel.call(name, 1, arguments), OfflineTestModel.stop(),
    ])
    outcome = run(model, scenario, store, source)
    assert outcome.status == "failed"
    assert outcome.tool_calls[0]["output"]["error"]["code"] == "INVALID_TOOL_CALL"
    assert store.count() == 0


def test_model_cannot_skip_slot_query_and_save(tmp_path):
    scenario, store, source = case(tmp_path)
    model = ScriptedModel([
        OfflineTestModel.call("read_material", 1),
        OfflineTestModel.call("check_rules", 2),
        OfflineTestModel.call("save_draft", 3),
        OfflineTestModel.stop(),
    ])
    outcome = run(model, scenario, store, source)
    assert outcome.error["code"] == "AGENT_INCOMPLETE"
    assert outcome.result.persistence.status == "not_attempted"
    assert store.count() == 0


def test_missing_fields_cannot_be_abandoned_without_supplement(tmp_path):
    scenario, store, source = case(tmp_path, "02_missing_metadata")
    outcome = run(ScriptedModel([
        OfflineTestModel.call("read_material", 1),
        OfflineTestModel.call("check_rules", 2), OfflineTestModel.stop(),
    ]), scenario, store, source)
    assert outcome.error["code"] == "AGENT_INCOMPLETE"
    assert store.count() == 0


def test_loop_has_bounded_model_calls(tmp_path):
    class LoopModel(OfflineTestModel):
        def invoke(self, messages):
            return self.call("read_material", len(messages))

    scenario, store, source = case(tmp_path)
    outcome = run(LoopModel(), scenario, store, source, max_calls=3)
    assert outcome.error["code"] == "LIMIT_EXCEEDED"
    assert outcome.status == "incomplete"
    assert outcome.model_calls == 3
    assert len(outcome.tool_calls) == 3
    assert outcome.execution_limits == {"max_model_calls": 3, "max_tool_calls": 3}
    assert outcome.execution_steps[-1]["next_action"] == "incomplete"
    assert outcome.execution_steps[-1]["next_action_source"] == "workflow"
    assert store.count() == 0


def test_model_exception_is_redacted(tmp_path):
    class FailedModel(OfflineTestModel):
        mode = "offline_test"

        def invoke(self, messages):
            raise RuntimeError("must-not-leak-api-key")

    scenario, store, source = case(tmp_path)
    outcome = run(FailedModel(), scenario, store, source)
    assert outcome.error["code"] == "MODEL_ERROR"
    assert "must-not-leak-api-key" not in json.dumps(outcome.to_dict())
    assert store.count() == 0


def test_save_returns_fresh_result_if_occupancy_changes_during_model_decision(tmp_path):
    scenario, store, source = case(tmp_path, "04_time_conflict")

    class ChangedScheduleModel(OfflineTestModel):
        def invoke(self, messages):
            if isinstance(messages[-1], ToolMessage) and messages[-1].name == "query_slots":
                store.replace_schedule([])
            return super().invoke(messages)

    outcome = run(ChangedScheduleModel(), scenario, store, source)
    query = next(item for item in outcome.tool_calls if item["name"] == "query_slots")
    assert query["output"]["result"]["scheduling"]["suggested_at"].startswith("2026-11-02T19:")
    assert outcome.result.scheduling.accepted_at.hour == 18
    assert outcome.decision == "ready"
    assert outcome.result.persistence.status == "saved"


def test_no_save_request_finishes_without_save_tool(tmp_path):
    scenario, store, source = case(tmp_path)
    step = scenario.steps[0]
    data = step.request.model_dump()
    data["save_draft"] = False
    request = ScheduleRequest.model_validate(data, context={"now": step.evaluated_at})
    outcome = ReviewSchedulingAgent(OfflineTestModel(), source, store).run(
        request, scenario.rules, evaluated_at=step.evaluated_at,
    )
    assert outcome.status == "completed"
    assert outcome.result.persistence.status == "not_attempted"
    assert all(item["name"] != "save_draft" for item in outcome.tool_calls)
    assert store.count() == 0


@pytest.mark.parametrize("name", ["01_normal", "02_missing_metadata"])
def test_langchain_deepseek_bound_tool_protocol_using_http_mock(tmp_path, name):
    scenario, store, source = case(tmp_path, name)
    requests = []
    offline = OfflineTestModel()

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.headers["authorization"] == "Bearer unit-test-key"
        assert len(body["tools"]) == 5
        assert body["parallel_tool_calls"] is False
        assert body["tool_choice"] == "auto"
        messages = convert_to_messages(body["messages"])
        names = {call["id"]: call["name"] for message in messages if isinstance(message, AIMessage)
                 for call in message.tool_calls}
        for message in messages:
            if isinstance(message, ToolMessage):
                message.name = names[message.tool_call_id]
        answer = offline.invoke(messages)
        reply = {"role": "assistant", "content": answer.content}
        if answer.tool_calls:
            reply["tool_calls"] = [{"id": call["id"], "type": "function", "function": {
                "name": call["name"], "arguments": json.dumps(call["args"]),
            }} for call in answer.tool_calls]
        # Preserve provider extension fields when sending the assistant message back.
        reply["reasoning_content"] = "offline test extension"
        return httpx.Response(200, json={
            "id": "chatcmpl-offline", "object": "chat.completion", "created": 0,
            "model": "test-model",
            "choices": [{
                "index": 0, "message": reply,
                "finish_reason": "tool_calls" if reply.get("tool_calls") else "stop",
            }],
        })

    chat_model = DeepSeekChatModel(
        model="test-model", api_key="unit-test-key", base_url="https://unit.invalid/v1", max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    model = CompatibleChatModel(chat_model)
    try:
        outcome = run(model, scenario, store, source)
    finally:
        model.close()
    assert outcome.result.persistence.status == ("saved" if name == "01_normal" else "not_attempted")
    assert len(requests) == 4
    assert any(
        message.get("reasoning_content") == "offline test extension"
        for message in requests[-1]["messages"] if message["role"] == "assistant"
    )
    assert "offline test extension" not in json.dumps(outcome.to_dict())
    if name == "02_missing_metadata":
        # Synthetic HTTP transport validates the checker; this is not a live provider test.
        assert inspect_evidence(outcome.to_dict())["passed"] is True


def test_model_steps_record_actual_observations_and_offline_is_not_live_evidence(tmp_path):
    scenario, store, source = case(tmp_path, "02_missing_metadata")
    outcome = run(OfflineTestModel(), scenario, store, source)
    steps = outcome.model_steps
    assert len(steps) == outcome.model_calls == 4
    assert steps[2]["observation"]["name"] == "check_rules"
    assert steps[2]["observation"]["output"]["data"]["status"] == "needs_info"
    assert steps[2]["tool_calls"][0]["name"] == "supplement_material"
    assert steps[3]["observation"]["tool_call_id"] == steps[2]["tool_calls"][0]["id"]
    assert steps[3]["observation"]["output"]["status"] == "empty"
    assert steps[3]["tool_calls"] == []
    inspection = inspect_evidence(outcome.to_dict())
    assert inspection["passed"] is False
    assert inspection["checks"]["live_api_mode"] is False
    assert inspection["checks"]["model_observed_missing_fields_and_chose_supplement"] is True
    assert inspection["checks"]["model_observed_empty_result_and_finished"] is True


def test_execution_rows_link_actual_parameters_results_and_next_model_choice(tmp_path):
    scenario, store, source = case(tmp_path, "02_missing_metadata")
    outcome = run(OfflineTestModel(), scenario, store, source)
    rows = outcome.execution_steps
    assert len(rows) == len(outcome.tool_calls) == 3
    assert rows[1]["tool"] == "check_rules"
    assert "needs_info" in rows[1]["return_summary"]
    assert rows[1]["next_action"] == "supplement_material"
    assert rows[1]["next_action_source"] == "model"
    assert rows[2]["arguments"]["fields"] == ["authorization_status"]
    assert rows[2]["decision_reason"] == rows[2]["arguments"]["reason"]
    assert rows[2]["return_summary"] == "status=empty; returned_fields=[]"
    assert rows[2]["next_action"] == "finish"
    assert rows[2]["next_decision_reason"] == outcome.model_steps[-1]["content"]


def test_normal_path_records_different_action_after_passed_review(tmp_path):
    scenario, store, source = case(tmp_path)
    item = scenario.steps[0]
    payload = item.request.model_dump()
    payload["save_draft"] = False
    request = ScheduleRequest.model_validate(payload, context={"now": item.evaluated_at})
    outcome = ReviewSchedulingAgent(OfflineTestModel(), source, store).run(
        request, scenario.rules, evaluated_at=item.evaluated_at,
    )
    inspection = inspect_evidence(outcome.to_dict(), path="normal")
    assert inspection["checks"]["model_observed_passed_review_and_chose_query"] is True
    assert inspection["checks"]["model_observed_query_result_and_finished"] is True
    assert inspection["checks"]["execution_records_include_all_required_fields"] is True
    assert inspection["passed"] is False  # Offline responses are never live evidence.
    assert outcome.execution_steps[1]["next_action"] == "query_slots"


@pytest.mark.parametrize("reason", ["", " ", "x" * 241])
def test_model_public_reason_is_required_and_short(tmp_path, reason):
    scenario, store, source = case(tmp_path)
    outcome = run(ScriptedModel([
        OfflineTestModel.call("read_material", 1, {"reason": reason}), OfflineTestModel.stop(),
    ]), scenario, store, source)
    assert outcome.tool_calls[0]["output"]["error"]["code"] == "INVALID_TOOL_CALL"
    assert store.count() == 0


def test_evidence_checker_does_not_accept_tool_names_without_model_observations(tmp_path):
    scenario, store, source = case(tmp_path, "02_missing_metadata")
    response = run(OfflineTestModel(), scenario, store, source).to_dict()
    response["mode"] = "api"
    response["model_steps"] = []
    inspection = inspect_evidence(response)
    assert inspection["passed"] is False
    assert inspection["checks"]["model_observed_missing_fields_and_chose_supplement"] is False


def test_evidence_checker_rejects_unlinked_tool_call_ids(tmp_path):
    scenario, store, source = case(tmp_path, "02_missing_metadata")
    response = run(OfflineTestModel(), scenario, store, source).to_dict()
    response["model_steps"][-1]["observation"]["tool_call_id"] = "unrelated-tool-call"
    inspection = inspect_evidence(response)
    assert inspection["passed"] is False
    assert inspection["checks"]["observation_matches_selected_tool_call"] is False


def test_api_key_is_not_exported_by_settings():
    settings = Settings(_env_file=None, api_key="unit-secret", model="test-model", base_url="https://unit.invalid/v1")
    assert "unit-secret" not in settings.model_dump_json()
    assert "unit-secret" not in repr(settings)
    assert "api_key" not in settings.model_dump()


def test_unconfigured_model_has_actionable_error():
    with pytest.raises(ValueError, match="MATERIAL_AGENT_BASE_URL"):
        build_model(Settings(_env_file=None))


def test_agent_binds_real_tools_and_framework_executes_them(tmp_path):
    class RecordingModel(OfflineTestModel):
        def bind_tools(self, tools):
            self.bound_tools = tools
            return super().bind_tools(tools)

    model = RecordingModel()
    scenario, store, source = case(tmp_path)
    outcome = run(model, scenario, store, source)
    assert all(isinstance(item, BaseTool) for item in model.bound_tools)
    assert [item.name for item in model.bound_tools] == [
        "read_material", "check_rules", "supplement_material", "query_slots", "save_draft",
    ]
    assert store.count() == 1
    assert outcome.result.persistence.status == "saved"


def test_invalid_tool_call_json_does_not_execute_any_tool(tmp_path):
    malformed = AIMessage(content="", additional_kwargs={"tool_calls": [{
        "id": "bad-json", "type": "function", "function": {"name": "read_material", "arguments": "{"},
    }]})
    scenario, store, source = case(tmp_path)
    outcome = run(ScriptedModel([malformed]), scenario, store, source)
    assert outcome.error["code"] == "MODEL_ERROR"
    assert outcome.tool_calls == []
    assert store.count() == 0


@pytest.mark.parametrize("model_name,endpoint,expected_type", [
    ("deepseek-flash", "https://api.deepseek.com", "DeepSeekChatModel"),
    ("compatible-model", "https://unit.invalid/v1", "ChatOpenAI"),
])
def test_configured_models_use_langchain_integrations(model_name, endpoint, expected_type):
    model = build_model(Settings(_env_file=None, model=model_name, base_url=endpoint, api_key="unit-test-key"))
    try:
        assert type(model.chat_model).__name__ == expected_type
    finally:
        model.close()


def test_offline_agent_cli_has_explicit_mode(tmp_path, capsys):
    assert main([
        "agent-example", str(ROOT / "examples/05_duplicate_request/input.json"),
        "--database", str(tmp_path / "cli.sqlite3"), "--offline",
    ]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "offline_test"
    assert output["model"] is None
    assert output["draft_count"] == 1
    assert all(run["status"] == "completed" for run in output["runs"])


def test_live_cli_missing_key_does_not_fall_back_or_create_database(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("src.main.load_settings", lambda: Settings(_env_file=None))
    database = tmp_path / "not-created.sqlite3"
    assert main([
        "agent-example", str(ROOT / "examples/01_normal/input.json"), "--database", str(database),
    ]) == 1
    assert "MATERIAL_AGENT_API_KEY" in capsys.readouterr().err
    assert not database.exists()
