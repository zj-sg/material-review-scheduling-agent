"""Record actual offline HTTP/graph/SQLite metadata and risk-review interactions."""

import json
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from src.api import create_app
from src.config import Settings
from src.services.storage import SQLiteStore


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def record_case(runtime_dir, *, risk):
    directory = ROOT / "examples" / ("03_rule_risk" if risk else "02_missing_metadata") / "human"
    directory.mkdir(exist_ok=True)
    date = (datetime.now(timezone(timedelta(hours=8))) + timedelta(days=1)).date().isoformat()
    raw = json.loads((ROOT / "data/api-data.json").read_text(encoding="utf-8").replace("2026-11-02", date))
    catalog = directory / "catalog.json"
    write_json(catalog, raw)
    store = SQLiteStore(runtime_dir / ("human-risk.sqlite3" if risk else "human-metadata.sqlite3"))
    settings = Settings(_env_file=None, api_offline=True, api_data_path=catalog, database_path=store.path,
                        max_model_calls=12)
    events, checks = [], {}
    with TestClient(create_app(settings=settings, store=store)) as client:
        def call(path, payload):
            response = client.post(path, json=payload)
            output = response.json()
            events.append({"step": len(events) + 1, "method": "POST", "path": path,
                           "arguments": deepcopy(payload), "http_status": response.status_code, "output": output})
            return response.status_code, output

        def start(material_id, suffix):
            return call("/schedule", {
                "request_id": "REQ-HUMAN-" + suffix, "material_id": material_id, "channel": "wechat",
                "time_requirement": {"mode": "fixed", "requested_at": date + "T18:00:00+08:00"},
                "search_window": {"start": date + "T18:00:00+08:00", "end": date + "T21:00:00+08:00"},
                "save_draft": True,
            })[1]

        def resume(waiting, **answer):
            return call("/runs/" + waiting["run_id"] + "/resume", {
                "interrupt_id": waiting["interrupt"]["interrupt_id"], **answer,
            })

        def review(waiting, action, **extra):
            return resume(waiting, action=action, reviewer="reviewer-demo", note="已核对合成演示资料。", **extra)[1]

        def select(waiting):
            return resume(waiting, selected_at=waiting["interrupt"]["candidates"][1]["start_at"])[1]

        if not risk:
            waiting = start("M02", "METADATA")
            checks["automatic_lookup_before_human"] = (
                waiting["interrupt"]["kind"] == "provide_metadata"
                and [tool["name"] for tool in waiting["tool_calls"]] == ["read_material", "check_rules", "supplement_material"]
                and store.count() == 0
            )
            code, _ = resume(waiting, fields={"authorization_status": ""})
            checks["invalid_input_preserves_interrupt"] = code == 422 and client.get("/runs/" + waiting["run_id"]).json() == waiting
            _, time_choice = resume(waiting, fields={"authorization_status": "authorized", "authorization_evidence": "AUTH-USER-M02"})
            checks["rechecks_then_waits_for_time"] = time_choice["interrupt"]["kind"] == "choose_time" and store.count() == 0
            final = select(time_choice)
            checks["same_run_saved_after_both_inputs"] = (
                final["status"] == "completed" and final["run_id"] == waiting["run_id"]
                and final["result"]["persistence"]["status"] == "saved" and store.count() == 1
            )
            checks["metadata_and_time_audited"] = [event["kind"] for event in final["human_actions"]] == ["provide_metadata", "choose_time"]
        else:
            waiting = start("M04", "APPROVE")
            checks["review_paused_without_draft"] = waiting["interrupt"]["kind"] == "review_risk" and store.count() == 0
            code, _ = resume(waiting, action="approve", reviewer="demo", note="无凭证")
            checks["approval_without_evidence_rejected"] = code == 422
            final = select(review(waiting, "approve", authorization_evidence="AUTH-REVIEW-M04"))
            checks["approval_rechecked_and_saved"] = final["result"]["review"]["status"] == "passed" and store.count() == 1
            waiting = start("M04", "REJECT")
            rejected = review(waiting, "reject")
            checks["reject_ends_without_saving"] = rejected["status"] == "rejected" and rejected["interrupt"] is None and store.count() == 1
            waiting = start("M04", "CHANGES")
            changes = review(waiting, "request_changes")
            checks["request_changes_waits_for_metadata"] = changes["interrupt"]["kind"] == "provide_metadata"
            _, recheck = resume(changes, fields={"authorization_evidence": "AUTH-MODIFIED-M04"})
            checks["modified_data_requires_new_review"] = recheck["interrupt"]["kind"] == "review_risk" and store.count() == 1
            final = select(review(recheck, "approve"))
            checks["modified_then_approved_and_saved"] = final["status"] == "completed" and store.count() == 2
            blocked = start("M03", "BLOCK")
            checks["explicit_block_cannot_enter_review"] = blocked["decision"] == "block" and blocked["interrupt"] is None and store.count() == 2
        checks["source_json_unchanged"] = json.loads(catalog.read_text(encoding="utf-8")) == raw
        checks["nothing_published"] = all((event["output"].get("result") or {}).get("publication_status", "not_published") == "not_published" for event in events)
    passed = all(checks.values())
    write_json(directory / "input.json", {"mode": "offline_test", "catalog": "catalog.json", "operations": [
        {key: event[key] for key in ("step", "method", "path", "arguments")} for event in events
    ]})
    write_json(directory / "actual.json", {"mode": "offline_test", "real_model_tested": False,
                                           "passed": passed, "checks": checks, "final_draft_count": store.count(), "events": events})
    write_json(directory / "trace.json", {"mode": "offline_test", "real_model_tested": False,
        "recorded_at": datetime.now(timezone.utc).isoformat(), "checks": checks,
        "steps": [{"http_step": event["step"], "run_id": event["output"].get("run_id"),
                   "status": event["output"].get("status"), "interrupt": event["output"].get("interrupt"),
                   "execution_steps": event["output"].get("execution_steps", []),
                   "human_actions": event["output"].get("human_actions", [])} for event in events]})
    (directory / "summary.md").write_text(
        "# 人工中断运行记录（离线模型）\n\n实际执行 HTTP 接口、LangGraph interrupt/Command 和 SQLite，未调用真实模型。\n\n"
        + "\n".join(f"- {key}: {value}" for key, value in checks.items())
        + "\n\ninput.json 保存各步请求，actual.json 保存 HTTP 返回，trace.json 保存累计工具步骤和人工输入。\n", encoding="utf-8")
    print(f"HTTP human {'risk' if risk else 'metadata'}: {'PASS' if passed else 'FAIL'}; events={len(events)}", flush=True)
    return {"passed": passed, **{name: (directory / (name + ".json")).relative_to(ROOT).as_posix() for name in ("input", "actual", "trace")}}


def record_human_interrupts(runtime_dir):
    return [record_case(runtime_dir, risk=False), record_case(runtime_dir, risk=True)]


if __name__ == "__main__":
    runtime = ROOT / "data" / ("human-validation-" + uuid4().hex)
    runtime.mkdir(parents=True)
    raise SystemExit(0 if all(case["passed"] for case in record_human_interrupts(runtime)) else 1)
