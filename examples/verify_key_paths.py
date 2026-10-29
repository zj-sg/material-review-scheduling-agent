"""Run isolated Uvicorn/HTTP demonstrations with offline or explicitly live models.

python examples/verify_key_paths.py --cases normal conflict_stale
python examples/verify_key_paths.py --live --repeats 2
No fault injection endpoints or production data changes are introduced.
"""

import argparse
import hashlib
import json
import socket
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import uvicorn

from src.api import create_app
from src.config import Settings, load_settings
from src.schemas import ScheduledSlot
from src.services.material_service import JsonMaterialService
from examples.record_validation import source_fingerprint

CASES = ("normal", "conflict_stale", "supplement_success", "human_metadata", "human_review", "tool_failure")
BEIJING = timezone(timedelta(hours=8))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class ScenarioMismatch(Exception):
    pass


class FailingSupplementSource(JsonMaterialService):
    """Trusted test fixture; the normal registered tool converts this to TOOL_ERROR."""

    def supplement(self, material_id):
        raise OSError("Synthetic supplement-source outage for acceptance testing")


@contextmanager
def serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Isolated Uvicorn server failed to start")
            time.sleep(0.05)
        yield "http://127.0.0.1:" + str(sock.getsockname()[1])
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def observed_choice(body, tool, output_status, next_tool=None):
    """Only count actual observations linked to a previously selected call ID."""
    steps = body.get("model_steps", [])
    for step in steps:
        observation = step.get("observation") or {}
        output = observation.get("output") or {}
        status = (output.get("data") or {}).get("status", output.get("status"))
        if step.get("status") != "ok" or observation.get("name") != tool or status != output_status:
            continue
        calls = step.get("tool_calls", [])
        chosen = any(call.get("name") == next_tool for call in calls) if next_tool else not calls
        linked = any(
            earlier.get("step", 0) < step["step"]
            and earlier.get("cycle", 1) == step.get("cycle", 1)
            and any(call.get("id") == observation.get("tool_call_id") for call in earlier.get("tool_calls", []))
            for earlier in steps
        )
        if chosen and linked:
            return True
    return False


def run_case(folder, name, *, live, settings=None):
    folder.mkdir(parents=True)
    runtime = ROOT / "data" / ("key-path-runtime-" + uuid4().hex)
    runtime.mkdir(parents=True)
    date = (datetime.now(BEIJING) + timedelta(days=1)).date().isoformat()
    catalog = json.loads((ROOT / "data/api-data.json").read_text(encoding="utf-8").replace("2026-11-02", date))
    if name != "conflict_stale":
        catalog["initial_state"]["existing_schedule"] = []
    if name == "supplement_success":
        catalog["initial_state"]["supplemental_materials"]["M02"] = {
            "authorization_status": "authorized", "authorization_evidence": "AUTH-AUTO-SYNTHETIC",
        }
    write_json(folder / "catalog.json", catalog)
    config = settings if live else Settings(_env_file=None)
    config = config.model_copy(update={
        "api_offline": not live, "api_data_path": (folder / "catalog.json").resolve(),
        "database_path": runtime / "drafts.sqlite3",
    })
    app = create_app(settings=config)
    events, checks, final = [], {}, {}
    failure = None
    started = time.monotonic()

    def expect(label, condition):
        checks[label] = bool(condition)
        if not condition:
            raise ScenarioMismatch(label)

    try:
        with serve(app) as base:
            # Local requests never go through a configured outbound proxy.
            opener = build_opener(ProxyHandler({}))

            def call(path, payload=None):
                nonlocal final
                begin = time.monotonic()
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
                request = Request(base + path, data=data, headers={"Content-Type": "application/json"})
                try:
                    with opener.open(request, timeout=300) as response:
                        status, raw = response.status, response.read().decode("utf-8")
                except HTTPError as exc:
                    status, raw = exc.code, exc.read().decode("utf-8")
                body = json.loads(raw)
                events.append({"step": len(events) + 1, "operation": "HTTP", "method": request.get_method(),
                               "path": path, "arguments": payload, "http_status": status,
                               "elapsed_seconds": round(time.monotonic() - begin, 3), "output": body})
                write_json(folder / "events.json", events)
                if "model_calls" in body:
                    final = body
                print(f"  {request.get_method()} {path}: HTTP {status}, status={body.get('status')}, interrupt={(body.get('interrupt') or {}).get('kind')}", flush=True)
                return status, body

            def resume(waiting, **answer):
                status, body = call("/runs/" + waiting["run_id"] + "/resume", {
                    "interrupt_id": waiting["interrupt"]["interrupt_id"], **answer,
                })
                expect("resume_http_200_step_" + str(len(events)), status == 200)
                return body

            status, health = call("/health")
            expect("expected_runtime_mode", status == 200 and health.get("mode") == ("api" if live else "offline_test"))
            if name == "tool_failure":
                # Inject at the trusted fixture boundary, never in the business HTTP input.
                app.state.runs.source = FailingSupplementSource(folder / "catalog.json")
                events.append({"step": len(events) + 1, "operation": "fixture:source_outage",
                               "tool": "supplement_material", "description": "Synthetic OSError; no provider failure simulated"})
            payload = {
                "request_id": "REQ-KEY-" + uuid4().hex,
                "material_id": "M01" if name in {"normal", "conflict_stale"} else "M04" if name == "human_review" else "M02",
                "channel": "wechat", "time_requirement": {"mode": "fixed", "requested_at": date + "T18:00:00+08:00"},
                "search_window": {"start": date + "T18:00:00+08:00", "end": date + "T21:00:00+08:00"},
                "save_draft": True,
            }
            write_json(folder / "request.json", payload)
            status, body = call("/schedule", payload)
            if name == "conflict_stale":
                expect("time_interrupt_without_new_draft", status == 200 and body.get("status") == "waiting_for_human"
                       and (body.get("interrupt") or {}).get("kind") == "choose_time" and app.state.store.count() == 0)
                expect("model_selected_query_after_passed_review", observed_choice(body, "check_rules", "passed", "query_slots"))
                first = body["interrupt"]["candidates"][0]
                slots = catalog["initial_state"]["existing_schedule"] + [{
                    "schedule_id": "SYNTHETIC-LATE-OCCUPANCY", "channel": "wechat",
                    "start_at": first["start_at"], "end_at": first["end_at"],
                }]
                app.state.store.replace_schedule([ScheduledSlot.model_validate(item) for item in slots])
                events.append({"step": len(events) + 1, "operation": "fixture:replace_schedule",
                               "arguments": slots, "description": "Trusted synthetic occupancy update before human resume"})
                refreshed = resume(body, selected_at=first["start_at"])
                expect("stale_time_causes_new_interrupt", refreshed.get("status") == "waiting_for_human"
                       and refreshed["interrupt"]["interrupt_id"] != body["interrupt"]["interrupt_id"] and app.state.store.count() == 0)
                old_status, _ = call("/runs/" + body["run_id"] + "/resume", {
                    "interrupt_id": body["interrupt"]["interrupt_id"], "selected_at": first["start_at"],
                })
                expect("old_interrupt_rejected", old_status == 409)
                body = resume(refreshed, selected_at=refreshed["interrupt"]["candidates"][0]["start_at"])
                expect("saved_latest_human_choice", body["result"]["scheduling"]["accepted_at"] != first["start_at"])
                expect("original_time_requirement_preserved", body["result"]["scheduling"]["requested_at"] == payload["time_requirement"]["requested_at"])
            elif name == "supplement_success":
                expect("model_supplemented_missing_information", observed_choice(body, "check_rules", "needs_info", "supplement_material"))
                expect("model_rechecked_successful_supplement", observed_choice(body, "supplement_material", "ok", "check_rules"))
                expect("model_queried_after_recheck_passed", observed_choice(body, "check_rules", "passed", "query_slots"))
            elif name == "human_metadata":
                expect("metadata_interrupt_without_new_draft", status == 200 and (body.get("interrupt") or {}).get("kind") == "provide_metadata"
                       and app.state.store.count() == 0)
                expect("model_observed_empty_and_finished", observed_choice(body, "supplement_material", "empty"))
                original_run = body["run_id"]
                body = resume(body, fields={"authorization_status": "authorized", "authorization_evidence": "AUTH-HUMAN-SYNTHETIC"})
                expect("resumed_same_run_with_metadata_audit", body.get("run_id") == original_run
                       and any(item["kind"] == "provide_metadata" for item in body.get("human_actions", [])))
                expect("model_queried_after_human_data_passed_review", observed_choice(body, "check_rules", "passed", "query_slots"))
            elif name == "human_review":
                expect("risk_interrupt_without_new_draft", status == 200 and (body.get("interrupt") or {}).get("kind") == "review_risk"
                       and app.state.store.count() == 0)
                body = resume(body, action="approve", reviewer="synthetic-reviewer", note="已核对合成授权文件。",
                              authorization_evidence="AUTH-REVIEW-SYNTHETIC")
                expect("approval_audited_and_rules_rerun", any(item.get("action") == "approve" for item in body.get("human_actions", []))
                       and body["result"]["review"]["status"] == "passed")
                expect("model_queried_after_approval_recheck", observed_choice(body, "check_rules", "passed", "query_slots"))
            elif name == "tool_failure":
                expect("explicit_tool_failure_http_500", status == 500 and body.get("status") == "failed"
                       and (body.get("error") or {}).get("code") == "TOOL_ERROR")
                expect("model_observed_error_and_chose_stop", observed_choice(body, "supplement_material", "error"))
                expect("no_query_save_or_retry_after_error", [item["name"] for item in body.get("tool_calls", [])]
                       == ["read_material", "check_rules", "supplement_material"] and app.state.store.count() == 0)
            if name != "tool_failure":
                expect("completed_and_saved_one_draft", body.get("status") == "completed" and body.get("error") is None
                       and (body.get("result") or {}).get("persistence", {}).get("status") == "saved" and app.state.store.count() == 1)
                status, persisted = call("/drafts/" + payload["request_id"])
                expect("draft_read_matches_final_result", status == 200 and persisted == body["result"])
            expect("nothing_published", body["result"]["publication_status"] == "not_published")
            expect("model_steps_match_count_and_budget", body["model_calls"] == len(body["model_steps"])
                   and body["model_calls"] <= body["execution_limits"]["max_model_calls"])
            if live:
                expect("live_deepseek_metadata", any(
                    (step.get("metadata") or {}).get("model_provider") == "deepseek"
                    or str((step.get("metadata") or {}).get("model_name", "")).startswith("deepseek")
                    for step in body["model_steps"]))
            expect("source_catalog_unchanged", json.loads((folder / "catalog.json").read_text(encoding="utf-8")) == catalog)
    except ScenarioMismatch as exc:
        failure = {"type": "acceptance_check_failed", "reason": str(exc)}
    except Exception as exc:
        # No exception text or model client configuration is exported.
        failure = {"type": type(exc).__name__, "reason": "Runner could not complete; see recorded HTTP events and model error codes."}
    write_json(folder / "events.json", events)
    write_json(folder / "response.json", final)
    write_json(folder / "trace.json", {key: final.get(key) for key in (
        "mode", "status", "model_calls", "model_steps", "tool_calls", "execution_steps", "execution_limits", "human_actions", "error")})
    result = {
        "case": name, "passed": failure is None and bool(checks) and all(checks.values()), "checks": checks,
        "failure": failure, "final_status": final.get("status"), "business_error": final.get("error"),
        "model_calls": final.get("model_calls", 0),
        "model_selected_tool_calls": sum("tool_call_id" in item for item in final.get("tool_calls", [])),
        "workflow_operations": sum("tool_call_id" not in item for item in final.get("tool_calls", [])),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    write_json(folder / "inspection.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="隔离 Uvicorn 演示及关键路径验收；默认离线，--live 才调用真实模型")
    parser.add_argument("--live", action="store_true", help="读取本地 .env，实际调用供应商 API")
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    settings = load_settings() if args.live else None
    if args.live and not (settings.api_key and settings.model and settings.base_url):
        parser.error("真实模型配置不完整，请在本地 .env 填写地址、模型和密钥。")
    batch = datetime.now(BEIJING).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
    output = (args.output or ROOT / "data/key-paths" / batch).resolve()
    output.mkdir(parents=True, exist_ok=False)
    meta = {"mode": "api" if args.live else "offline_test", "recorded_at": datetime.now(BEIJING).isoformat(),
            "source_sha256": source_fingerprint(), "runner_sha256": hashlib.sha256(Path(__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
            "configured_model": settings.model if settings else "OfflineTestModel", "max_model_calls_per_run": settings.max_model_calls if settings else 12,
            "repeats": args.repeats, "automatic_case_retries": 0, "attempts": []}
    for name in args.cases:
        for repeat in range(1, args.repeats + 1):
            label = f"{name}-{repeat}"
            print(f"[{meta['mode']}] {label}", flush=True)
            result = run_case(output / label, name, live=args.live, settings=settings)
            result.update(record=label, repeat=repeat)
            meta["attempts"].append(result)
            passed = sum(item["passed"] for item in meta["attempts"])
            meta.update(passed=passed, total=len(meta["attempts"]), success_rate=passed / len(meta["attempts"]),
                        total_model_calls=sum(item["model_calls"] for item in meta["attempts"]),
                        total_model_selected_tool_calls=sum(item["model_selected_tool_calls"] for item in meta["attempts"]))
            write_json(output / "summary.json", meta)
            print(f"  {'PASS' if result['passed'] else 'FAIL'}; model_calls={result['model_calls']}; tools={result['model_selected_tool_calls']}; failure={result['failure']}", flush=True)
    lines = ["# 关键路径实际运行记录", "", f"模式：{meta['mode']}；模型：{meta['configured_model']}。",
             f"本批验收通过 {meta['passed']}/{meta['total']}（{meta['success_rate']:.1%}），实际模型调用 {meta['total_model_calls']} 次，模型选择工具 {meta['total_model_selected_tool_calls']} 次。",
             "", "工具失败用例以明确失败、模型观察错误后停止、无保存作为验收通过标准。",
             "时间选择、占用复查和强制人工中断由业务流程执行，不作为模型观察后调整动作的证据。",
             "每个样本均保留，没有自动重跑筛选成功记录；供应商 SDK 仍可能进行配置中的一次网络重试。",
             "", "| 场景 | 次数 | 通过 | 模型调用 | 模型工具调用 | 失败原因 | 记录 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for item in meta["attempts"]:
        reason = (item["failure"] or {}).get("reason", "无")
        lines.append(f"| {item['case']} | {item['repeat']} | {item['passed']} | {item['model_calls']} | {item['model_selected_tool_calls']} | {reason} | [{item['record']}]({item['record']}/inspection.json) |")
    lines += ["", "每个目录包含 request.json、catalog.json、events.json、response.json、trace.json 和 inspection.json。",
              "events 包括实际 HTTP 输入/输出，以及明确标为 fixture 的合成故障或占用变更；trace 包括模型观察、实际参数、公开依据和后续动作。",
              "这是有限合成样本的验收通过率，不是生产环境成功率或稳定性保证。未导出模型内部思维过程。"]
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Records: {output}", flush=True)
    return 0 if meta["passed"] == meta["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
