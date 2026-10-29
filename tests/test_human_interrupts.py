"""Human data/review branches use actual graphs, tools and SQLite, with an offline model."""

import json

import pytest
from fastapi.testclient import TestClient

from src.api import create_app
from test_api import setup_case, choose, forbid_provider_connections


def supply(client, waiting, fields):
    return client.post("/runs/" + waiting["run_id"] + "/resume", json={
        "interrupt_id": waiting["interrupt"]["interrupt_id"], "fields": fields,
    })


def review(client, waiting, action="approve", **extra):
    return client.post("/runs/" + waiting["run_id"] + "/resume", json={
        "interrupt_id": waiting["interrupt"]["interrupt_id"], "action": action,
        "reviewer": "reviewer-demo", "note": "已核对演示资料。", **extra,
    })


def rewrite_catalog(tmp_path, change):
    path = tmp_path / "catalog.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    change(raw)
    path.write_text(json.dumps(raw), encoding="utf-8")


def risk_case(tmp_path, **options):
    app, settings, store, requests = setup_case(tmp_path, "03_rule_risk", **options)
    return app, settings, store, requests[1]


def test_missing_metadata_pauses_after_lookup_and_resumes_with_validated_data(tmp_path):
    app, _, store, requests = setup_case(tmp_path, "02_missing_metadata")
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=requests[0]).json()
        assert waiting["status"] == "waiting_for_human"
        assert waiting["interrupt"]["kind"] == "provide_metadata"
        assert waiting["interrupt"]["missing_fields"] == ["authorization_status"]
        assert all(item["why"] and item["request"] for item in waiting["interrupt"]["requirements"])
        assert [item["name"] for item in waiting["tool_calls"]] == ["read_material", "check_rules", "supplement_material"]
        assert waiting["model_steps"][-1]["observation"]["output"]["status"] == "empty"
        assert store.count() == 0
        response = supply(client, waiting, {"authorization_status": "authorized", "authorization_evidence": "AUTH-USER"})
        assert response.status_code == 200
        final = response.json()
        assert final["run_id"] == waiting["run_id"]
        assert final["status"] == "completed"
        assert final["result"]["review"]["status"] == "passed"
        assert final["result"]["persistence"]["status"] == "saved"
        assert final["model_calls"] == 8
        assert [item["step"] for item in final["model_steps"]] == list(range(1, 9))
        assert final["human_actions"][0]["status"] == "applied"
        stored = json.loads(store.get(requests[0]["request_id"])["report_json"])
        assert stored["material"]["authorization_evidence"] == "AUTH-USER"
        assert stored["human_actions"][0]["kind"] == "provide_metadata"
        original = json.loads((tmp_path / "catalog.json").read_text())
        assert original["initial_state"]["materials"][0].get("authorization_status") is None
        assert app.state.runs.graph.get_state({"configurable": {"thread_id": final["run_id"]}}).next == ()


def test_missing_channel_then_time_conflict_retains_the_supplement(tmp_path):
    app, _, store, requests = setup_case(tmp_path)
    rewrite_catalog(tmp_path, lambda raw: raw["initial_state"]["materials"][0].update(channel=None))
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=requests[0]).json()
        assert waiting["interrupt"]["missing_fields"] == ["channel"]
        assert waiting["interrupt"]["required_channel"] == "wechat"
        selected = supply(client, waiting, {"channel": "wechat"}).json()
        assert selected["interrupt"]["kind"] == "choose_time"
        assert store.count() == 0
        final = choose(client, selected, index=1).json()
        assert final["result"]["persistence"]["status"] == "saved"
        assert final["result"]["scheduling"]["accepted_at"] == "2099-11-02T20:00:00+08:00"
        assert [action["kind"] for action in final["human_actions"]] == ["provide_metadata", "choose_time"]
        assert json.loads(store.get(requests[0]["request_id"])["report_json"])["material"]["channel"] == "wechat"


def test_successful_automatic_supplement_does_not_need_human_input(tmp_path):
    app, _, store, requests = setup_case(tmp_path, "02_missing_metadata")
    rewrite_catalog(tmp_path, lambda raw: raw["initial_state"]["supplemental_materials"].update(
        M02={"authorization_status": "authorized", "authorization_evidence": "AUTH-AUTO"},
    ))
    with TestClient(app) as client:
        result = client.post("/schedule", json=requests[0]).json()
        assert result["status"] == "completed"
        assert result["interrupt"] is None
        assert result["human_actions"] == []
        assert store.count() == 1


def test_partial_metadata_requires_another_round_and_uses_one_global_budget(tmp_path):
    app, _, store, requests = setup_case(tmp_path, "02_missing_metadata")
    with TestClient(app) as client:
        first = client.post("/schedule", json=requests[0]).json()
        second = supply(client, first, {"authorization_status": "authorized"}).json()
        assert second["status"] == "waiting_for_human"
        assert second["interrupt"]["missing_fields"] == ["authorization_evidence"]
        assert second["interrupt"]["interrupt_id"] != first["interrupt"]["interrupt_id"]
        assert second["model_calls"] == 8
        assert supply(client, first, {"authorization_status": "denied"}).status_code == 409
        final = supply(client, second, {"authorization_evidence": "AUTH-ROUND-2"}).json()
        assert final["status"] == "completed"
        assert final["model_calls"] == 12
        assert len(final["human_actions"]) == 2
        assert store.count() == 1


@pytest.mark.parametrize("values", [
    {}, {"authorization_status": ""}, {"authorization_status": "wrong"}, {"authorization_status": None},
    {"title": "overwrite-existing"}, {"material_id": "OTHER"}, {"authorization_evidence": " "},
])
def test_invalid_supplements_do_not_consume_the_interrupt(tmp_path, values):
    app, _, store, requests = setup_case(tmp_path, "02_missing_metadata")
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=requests[0]).json()
        assert supply(client, waiting, values).status_code == 422
        assert client.get("/runs/" + waiting["run_id"]).json() == waiting
        assert store.count() == 0


def test_material_channel_must_match_the_unchanged_request(tmp_path):
    app, _, store, requests = setup_case(tmp_path)
    rewrite_catalog(tmp_path, lambda raw: raw["initial_state"]["materials"][0].update(channel=None))
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=requests[0]).json()
        assert supply(client, waiting, {"channel": "weibo"}).status_code == 422
        assert store.count() == 0


def test_review_pause_exposes_rule_context_and_approval_requires_evidence(tmp_path):
    app, _, store, payload = risk_case(tmp_path)
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=payload).json()
        assert waiting["status"] == "waiting_for_human"
        assert waiting["interrupt"]["kind"] == "review_risk"
        risk = waiting["interrupt"]["risks"][0]
        assert risk["rule_id"] == "R_AUTH_REVIEW"
        assert risk["related_value"] == "unknown"
        assert risk["content_excerpt"]
        assert {item["action"] for item in waiting["interrupt"]["options"]} == {"approve", "reject", "request_changes"}
        assert review(client, waiting).status_code == 422
        assert client.get("/runs/" + waiting["run_id"]).json() == waiting
        assert store.count() == 0
        final = review(client, waiting, authorization_evidence="AUTH-REVIEWED").json()
        assert final["status"] == "completed"
        assert final["result"]["review"]["status"] == "passed"
        assert final["result"]["persistence"]["status"] == "saved"
        assert final["model_calls"] == 6
        assert final["human_actions"][0]["reviewer"] == "reviewer-demo"
        assert review(client, waiting, authorization_evidence="AUTH-REVIEWED").status_code == 409


def test_reject_stops_without_saving_or_calling_the_model_again(tmp_path):
    app, _, store, payload = risk_case(tmp_path)
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=payload).json()
        final = review(client, waiting, action="reject", note="无法确认授权，驳回。").json()
        assert final["status"] == "rejected"
        assert final["decision"] == "reject"
        assert final["interrupt"] is None
        assert final["model_calls"] == waiting["model_calls"]
        assert final["result"]["review"]["status"] == "needs_review"  # Rule findings are retained.
        assert final["human_actions"][-1]["action"] == "reject"
        assert store.count() == 0


def test_request_changes_waits_for_input_then_returns_to_risk_review(tmp_path):
    app, _, store, payload = risk_case(tmp_path)
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=payload).json()
        changes = review(client, waiting, action="request_changes", note="请补充授权文件编号。").json()
        assert changes["interrupt"]["kind"] == "provide_metadata"
        assert changes["interrupt"]["changes_requested"] is True
        assert changes["interrupt"]["message"] == "请补充授权文件编号。"
        assert supply(client, changes, {"authorization_status": "authorized", "authorization_evidence": "A"}).status_code == 422
        again = supply(client, changes, {"authorization_evidence": "AUTH-MODIFIED"}).json()
        assert again["interrupt"]["kind"] == "review_risk"
        assert again["interrupt"]["material"]["authorization_evidence"] == "AUTH-MODIFIED"
        assert store.count() == 0
        final = review(client, again).json()
        assert final["status"] == "completed"
        assert final["human_actions"][-1]["action"] == "approve"
        assert len(final["human_actions"]) == 3
        assert store.count() == 1


def test_missing_unknown_authorization_can_flow_into_review(tmp_path):
    app, _, store, requests = setup_case(tmp_path, "02_missing_metadata")
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=requests[0]).json()
        risk = supply(client, waiting, {"authorization_status": "unknown"}).json()
        assert risk["interrupt"]["kind"] == "review_risk"
        final = review(client, risk, authorization_evidence="AUTH-CHAIN").json()
        assert final["status"] == "completed"
        assert final["model_calls"] == 10
        assert store.count() == 1


def test_explicit_block_has_no_approval_bypass(tmp_path):
    app, _, store, requests = setup_case(tmp_path, "03_rule_risk")
    with TestClient(app) as client:
        blocked = client.post("/schedule", json=requests[0]).json()
        assert blocked["decision"] == "block"
        assert blocked["interrupt"] is None
        response = client.post("/runs/" + blocked["run_id"] + "/resume", json={
            "interrupt_id": "fake", "action": "approve", "reviewer": "demo", "note": "同意", "authorization_evidence": "A",
        })
        assert response.status_code == 409
        assert store.count() == 0


@pytest.mark.parametrize("change", ["content", "authorization_status"])
def test_changed_source_invalidates_approval_and_cannot_hide_new_block(tmp_path, change):
    app, _, store, payload = risk_case(tmp_path)
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=payload).json()
        def update(raw):
            record = next(item for item in raw["initial_state"]["materials"] if item["material_id"] == "M04")
            record[change] = "稳赚不赔" if change == "content" else "denied"
        rewrite_catalog(tmp_path, update)
        final = review(client, waiting, authorization_evidence="OLD-APPROVAL").json()
        assert final["decision"] == "block"
        assert final["human_actions"][-1]["status"] == "invalidated_source_changed"
        assert store.count() == 0


def test_changed_nonblocking_source_needs_new_risk_approval(tmp_path):
    app, _, store, payload = risk_case(tmp_path)
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=payload).json()
        rewrite_catalog(tmp_path, lambda raw: raw["initial_state"]["materials"][1].update(content="更新后的正常素材正文。"))
        again = review(client, waiting, authorization_evidence="OLD").json()
        assert again["status"] == "waiting_for_human"
        assert again["interrupt"]["interrupt_id"] != waiting["interrupt"]["interrupt_id"]
        assert store.count() == 0
        final = review(client, again, authorization_evidence="NEW").json()
        assert final["status"] == "completed"
        assert store.count() == 1


@pytest.mark.parametrize("cap", [4, 5])
def test_human_resume_does_not_reset_model_budget(tmp_path, cap):
    _, settings, store, requests = setup_case(tmp_path, "02_missing_metadata")
    settings.max_model_calls = cap
    with TestClient(create_app(settings=settings, store=store)) as client:
        waiting = client.post("/schedule", json=requests[0]).json()
        assert waiting["model_calls"] == 4
        response = supply(client, waiting, {"authorization_status": "authorized", "authorization_evidence": "AUTH"})
        assert response.status_code == 409
        assert response.json()["status"] == "incomplete"
        assert response.json()["error"]["code"] == "LIMIT_EXCEEDED"
        assert response.json()["model_calls"] == cap
        assert store.count() == 0


def test_run_scoped_input_does_not_fill_another_run_or_the_json_file(tmp_path):
    app, _, store, requests = setup_case(tmp_path, "02_missing_metadata")
    with TestClient(app) as client:
        first = client.post("/schedule", json=requests[0]).json()
        second = client.post("/schedule", json=dict(requests[0], request_id="OTHER-RUN")).json()
        assert supply(client, first, {"authorization_status": "authorized", "authorization_evidence": "RUN-A"}).json()["status"] == "completed"
        assert client.get("/runs/" + second["run_id"]).json() == second
        assert store.count() == 1


def test_cross_kind_input_is_rejected_without_consuming_the_interrupt(tmp_path):
    app, _, _, requests = setup_case(tmp_path, "02_missing_metadata")
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=requests[0]).json()
        response = client.post("/runs/" + waiting["run_id"] + "/resume", json={
            "interrupt_id": waiting["interrupt"]["interrupt_id"], "selected_at": "2099-11-02T20:00:00+08:00",
        })
        assert response.status_code == 422
        assert client.get("/runs/" + waiting["run_id"]).json() == waiting


def test_save_failure_after_supplement_rolls_back(tmp_path):
    app, _, store, requests = setup_case(tmp_path, "02_missing_metadata", fail_save=True)
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=requests[0]).json()
        result = supply(client, waiting, {"authorization_status": "authorized", "authorization_evidence": "AUTH"})
        assert result.status_code == 500
        assert result.json()["result"]["persistence"]["status"] == "failed"
        assert store.count() == 0


def test_no_save_flag_is_preserved_after_human_review(tmp_path):
    app, _, store, payload = risk_case(tmp_path)
    payload["save_draft"] = False
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=payload).json()
        final = review(client, waiting, authorization_evidence="AUTH").json()
        assert final["status"] == "completed"
        assert final["result"]["persistence"]["status"] == "not_attempted"
        assert store.count() == 0


@pytest.mark.parametrize("field,value,kind", [
    ("authorization_status", "unknown", "review_risk"),
    ("channel", None, "provide_metadata"),
])
def test_time_resume_routes_new_material_findings_to_the_correct_interrupt(tmp_path, field, value, kind):
    app, _, store, requests = setup_case(tmp_path)
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=requests[0]).json()
        rewrite_catalog(tmp_path, lambda raw: raw["initial_state"]["materials"][0].update({field: value}))
        follow_up = choose(client, waiting).json()
        assert follow_up["status"] == "waiting_for_human"
        assert follow_up["interrupt"]["kind"] == kind
        assert store.count() == 0
        if kind == "review_risk":
            follow_up = review(client, follow_up, authorization_evidence="AUTH-NEW").json()
        else:
            follow_up = supply(client, follow_up, {"channel": "wechat"}).json()
        assert follow_up["interrupt"]["kind"] == "choose_time"
        assert choose(client, follow_up).json()["result"]["persistence"]["status"] == "saved"


def test_requested_content_changes_are_checked_and_cannot_override_a_block(tmp_path):
    app, _, store, payload = risk_case(tmp_path)
    with TestClient(app) as client:
        waiting = client.post("/schedule", json=payload).json()
        changes = review(client, waiting, action="request_changes", note="请修改正文并补充凭证。").json()
        blocked = supply(client, changes, {"content": "稳赚不赔", "authorization_evidence": "AUTH"}).json()
        assert blocked["decision"] == "block"
        assert blocked["interrupt"] is None
        assert any(risk["rule_id"] == "R_TEXT_BLOCK" for risk in blocked["result"]["review"]["risks"])
        assert store.count() == 0
