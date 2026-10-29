"""Capture two live API paths and check observable model decisions and tool feedback.

Run from the project root:
    .venv/Scripts/python.exe -X utf8 examples/verify_react.py
The running server must use mode=api. This script never reads or records .env.
"""

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
BEIJING = timezone(timedelta(hours=8))


def inspect_evidence(response, path="missing"):
    """Require actual model observations and selections, not just a list of tool names."""
    steps = response.get("model_steps", [])
    tools = response.get("tool_calls", [])
    supplement = next((step for step in steps if (
        step.get("status") == "ok"
        and (step.get("observation") or {}).get("name") == "check_rules"
        and ((step["observation"].get("output") or {}).get("data") or {}).get("status") == "needs_info"
        and any(call.get("name") == "supplement_material" for call in step.get("tool_calls", []))
    )), None)
    executed = next((item for item in tools if item.get("name") == "supplement_material"
                     and item.get("output", {}).get("status") == "empty"), None)
    observed_empty = next((step for step in steps if (
        step.get("status") == "ok"
        and (step.get("observation") or {}).get("name") == "supplement_material"
        and (step["observation"].get("output") or {}).get("status") == "empty"
    )), None)
    finish = next((step for step in steps if (
        step.get("status") == "ok" and not step.get("tool_calls")
        and observed_empty is not None and step["step"] >= observed_empty["step"]
    )), None)
    # Link the executed tool result to the model's actual function-call ID.
    linked = bool(supplement and observed_empty and observed_empty["step"] > supplement["step"] and any(
        call.get("id") == observed_empty["observation"].get("tool_call_id")
        for call in supplement["tool_calls"] if call.get("name") == "supplement_material"
    ))
    result = response.get("result") or {}
    checks = {
        "live_api_mode": response.get("mode") == "api",
        "deepseek_model_reported": any(
            (step.get("metadata") or {}).get("model_provider") == "deepseek"
            or str((step.get("metadata") or {}).get("model_name", "")).startswith("deepseek")
            for step in steps
        ),
        "completed_without_error": response.get("status") == "completed" and response.get("error") is None,
        "model_observed_missing_fields_and_chose_supplement": supplement is not None,
        "supplement_tool_actually_returned_empty": executed is not None,
        "model_observed_empty_result_and_finished": observed_empty is not None and finish is not None,
        "observation_matches_selected_tool_call": linked,
        "model_call_count_matches_records": response.get("model_calls") == len(steps) and len(steps) >= 3,
        "requested_missing_information": response.get("decision") == "request_info"
            and result.get("review", {}).get("status") == "needs_info",
        "no_draft_or_publication": result.get("persistence", {}).get("status") == "not_attempted"
            and result.get("publication_status") == "not_published",
    }
    if path == "normal":
        query = next((step for step in steps if (
            step.get("status") == "ok"
            and (step.get("observation") or {}).get("name") == "check_rules"
            and ((step["observation"].get("output") or {}).get("data") or {}).get("status") == "passed"
            and any(call.get("name") == "query_slots" for call in step.get("tool_calls", []))
        )), None)
        query_finish = next((step for step in steps if (
            step.get("status") == "ok" and not step.get("tool_calls")
            and (step.get("observation") or {}).get("name") == "query_slots"
            and (step["observation"].get("output") or {}).get("status") == "ok"
        )), None)
        for key in ("model_observed_missing_fields_and_chose_supplement", "supplement_tool_actually_returned_empty",
                    "model_observed_empty_result_and_finished", "observation_matches_selected_tool_call", "requested_missing_information"):
            checks.pop(key)
        checks.update({
            "model_observed_passed_review_and_chose_query": query is not None,
            "query_actually_executed": any(item.get("name") == "query_slots" and item.get("output", {}).get("status") == "ok" for item in tools),
            "model_observed_query_result_and_finished": query_finish is not None,
            "observation_matches_selected_tool_call": bool(query and query_finish and any(
                call.get("id") == query_finish["observation"].get("tool_call_id")
                for call in query["tool_calls"] if call.get("name") == "query_slots")),
            "normal_scheduling_completed": response.get("decision") == "ready"
                and result.get("review", {}).get("status") == "passed"
                and result.get("scheduling", {}).get("status") == "available",
        })
    rows = response.get("execution_steps", [])
    checks["execution_records_include_all_required_fields"] = bool(rows) and len(rows) == len(tools) and all(
        row.get("step") and isinstance(row.get("arguments"), dict)
        and row.get("return_summary") and row.get("next_action") and row.get("decision_reason")
        for row in rows
    )
    cap = (response.get("execution_limits") or {}).get("max_model_calls", 0)
    tool_cap = (response.get("execution_limits") or {}).get("max_tool_calls", 0)
    checks["execution_within_configured_limits"] = (
        isinstance(cap, int) and cap > 0 and isinstance(tool_cap, int) and tool_cap > 0
        and isinstance(response.get("model_calls"), int) and response["model_calls"] <= cap and len(tools) <= tool_cap
    )
    return {"passed": all(checks.values()), "checks": checks}


def request_json(url, payload=None, timeout=300):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def markdown_cell(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def save_case(folder, path, payload, status, raw):
    folder.mkdir(parents=True, exist_ok=True)
    write_json(folder / "request.json", payload)
    (folder / "response.json").write_text(raw, encoding="utf-8")
    write_json(folder / "http.json", {"status_code": status, "recorded_at": datetime.now(BEIJING).isoformat()})
    response = json.loads(raw)
    inspection = inspect_evidence(response, path)
    inspection["checks"]["http_200"] = status == 200
    inspection["passed"] = all(inspection["checks"].values())
    write_json(folder / "inspection.json", inspection)
    rows = response.get("execution_steps", [])
    write_json(folder / "execution-steps.json", rows)
    lines = ["# ReAct 实际调用记录", "", f"路径：{path}", f"请求 ID：{payload['request_id']}",
             f"HTTP 状态：{status}；运行模式：{response.get('mode', 'unknown')}；流程状态：{response.get('status')}",
             f"验收结果：{'通过' if inspection['passed'] else '未通过'}", "",
             "| 步骤 | 工具 | 实际参数 | 返回摘要 | 后续动作及来源 | 简要决策依据（模型公开参数） |",
             "| --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        lines.append("| " + " | ".join(markdown_cell(value) for value in (
            row["step"], row["tool"], row["arguments"], row["return_summary"],
            f"{row['next_action']} ({row['next_action_source']})", row["decision_reason"],
        )) + " |")
    lines += ["", "完整观察/动作与后续动作依据见 response.json 的 model_steps、execution_steps。",
              "workflow 标注的是程序流转，不冒充模型决策。没有导出内部思维过程。",
              "依据来自本次模型返回的 reason 参数；工具返回摘要只整理实际结果。"]
    (folder / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return response, inspection


def main(argv=None):
    parser = argparse.ArgumentParser(description="保存真实模型的正常/缺失两条执行路径及决策证据")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--path", choices=["all", "normal", "missing"], default="all")
    args = parser.parse_args(argv)
    now = datetime.now(BEIJING)
    folder = ROOT / "data/react-evidence" / (now.strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8])
    folder.mkdir(parents=True)
    base = args.base_url.rstrip("/")
    try:
        status, raw = request_json(base + "/health", timeout=10)
        (folder / "health.json").write_text(raw, encoding="utf-8")
        health = json.loads(raw)
        if status != 200 or health.get("mode") != "api":
            print("未发送模型请求：/health 必须返回 mode=api；检查 API_OFFLINE 并重启服务。")
            return 1
        # A new request ID and a future date make the demonstration repeatable.
        start = (now + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
        cases = ["normal", "missing"] if args.path == "all" else [args.path]
        results = []
        for path in cases:
            payload = {
                "request_id": "REQ-REACT-" + uuid4().hex,
                "material_id": "M01" if path == "normal" else "M02", "channel": "wechat",
                "time_requirement": {"mode": "fixed", "requested_at": start.isoformat()},
                "search_window": {"start": start.isoformat(), "end": (start + timedelta(hours=3)).isoformat()},
                "save_draft": False,
            }
            case_folder = folder / path
            case_folder.mkdir()
            write_json(case_folder / "request.json", payload)
            print(f"正在运行 {path} 路径，等待实际模型响应……", flush=True)
            status, raw = request_json(base + "/schedule", payload)
            response, inspection = save_case(case_folder, path, payload, status, raw)
            selected = next((call["name"] for step in response.get("model_steps", [])
                             if (step.get("observation") or {}).get("name") == "check_rules"
                             for call in step.get("tool_calls", [])), None)
            results.append({"path": path, "passed": inspection["passed"], "review_followup": selected,
                            "record": str(case_folder), "status": response.get("status")})
            print(f"{path}：{'通过' if inspection['passed'] else '未通过'}；审核后的模型动作：{selected}", flush=True)
        passed = all(item["passed"] for item in results)
        two_paths = len(results) >= 2 and passed and len({item["review_followup"] for item in results}) >= 2
        passed = passed and (args.path != "all" or two_paths)
        write_json(folder / "inspection.json", {"passed": passed, "two_path_requirement_met": two_paths, "paths": results})
        summary = ["# 两条真实模型执行路径", "", "| 路径 | 审核后的实际模型动作 | 验收结果 | 运行记录 |",
                   "| --- | --- | --- | --- |"]
        for item in results:
            summary.append(f"| {item['path']} | {item['review_followup']} | {item['passed']} | [{item['path']}/summary.md]({item['path']}/summary.md) |")
        summary += ["", f"两条不同路径要求是否满足：{two_paths}", "", "所有记录均来自本次 HTTP 响应；未导出内部思维过程。"]
        (folder / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
        print(f"证据目录：{folder}")
        if passed:
            print("验收通过；两条不同路径要求满足情况：" + str(two_paths))
            return 0
        print("验收未通过：请检查各路径 inspection.json。确认已重启服务，且使用默认 M01、M02 演示数据。")
        return 1
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        write_json(folder / "runner-error.json", {"error_type": type(exc).__name__, "acceptance_passed": False})
        print(f"未完成验收（{type(exc).__name__}），已保留取得的证据：{folder}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
