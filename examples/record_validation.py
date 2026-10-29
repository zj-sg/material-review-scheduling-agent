"""Run the seven offline scenarios and save actual outputs and traces for review.

Usage: .venv/Scripts/python.exe -X utf8 examples/record_validation.py
This explicitly selects OfflineTestModel; no external model is called.
"""

import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.schemas import ScenarioExpectation


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def source_fingerprint():
    files = sorted((ROOT / "src").rglob("*.py")) + [ROOT / "requirements.txt", ROOT / "data/api-data.json"]
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(b"\0" + path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def confirmation_fields(value):
    """Compare business facts; descriptive pending-item text permits synonyms."""
    value = json.loads(json.dumps(value))
    for item in value["pending_items"]:
        item.pop("message", None)
    return value


def record_interactive(runtime_dir):
    from copy import deepcopy
    from fastapi.testclient import TestClient
    from src.api import create_app
    from src.config import Settings
    from src.schemas import ScheduledSlot
    from src.services.storage import SQLiteStore

    directory = ROOT / "examples/04_time_conflict/interactive"
    directory.mkdir(exist_ok=True)
    date = (datetime.now(timezone(timedelta(hours=8))) + timedelta(days=1)).date().isoformat()
    raw = json.loads((ROOT / "examples/04_time_conflict/input.json").read_text(encoding="utf-8").replace("2026-11-02", date))
    catalog = directory / "catalog.json"
    write_json(catalog, {"rules": raw["rules"], "initial_state": raw["initial_state"]})
    store = SQLiteStore(runtime_dir / "interactive.sqlite3")
    settings = Settings(_env_file=None, api_offline=True, api_data_path=catalog, database_path=store.path)
    events = []
    checks = {}
    with TestClient(create_app(settings=settings, store=store)) as client:
        def call(method, path, payload=None):
            response = client.request(method, path, json=payload)
            result = response.json()
            events.append({"step": len(events) + 1, "operation": "HTTP", "method": method, "path": path,
                           "arguments": deepcopy(payload), "http_status": response.status_code, "output": result})
            return response.status_code, result

        def resume(waiting, index):
            return call("POST", "/runs/" + waiting["run_id"] + "/resume", {
                "interrupt_id": waiting["interrupt"]["interrupt_id"],
                "selected_at": waiting["interrupt"]["candidates"][index]["start_at"],
            })

        def replace(slots):
            store.replace_schedule([ScheduledSlot.model_validate(item) for item in slots])
            events.append({"step": len(events) + 1, "operation": "service:replace_schedule",
                           "arguments": deepcopy(slots), "output": {"formal_slot_count": len(slots)}})

        payload = raw["steps"][0]["request"]
        _, waiting = call("POST", "/schedule", payload)
        checks["paused_without_new_draft"] = waiting["status"] == "waiting_for_human" and store.count() == 0
        _, chosen = resume(waiting, 1)
        original_id = chosen["result"]["persistence"]["draft_id"]
        checks["selected_second_candidate"] = chosen["result"]["scheduling"]["accepted_at"].startswith(date + "T20:")
        _, repeated = call("POST", "/schedule", payload)
        _, repeated = resume(repeated, 1)
        checks["duplicate_reuses_draft"] = repeated["result"]["persistence"]["draft_id"] == original_id and store.count() == 1
        race = dict(payload, request_id=payload["request_id"] + "-RACE")
        _, old = call("POST", "/schedule", race)
        occupied = deepcopy(raw["initial_state"]["existing_schedule"])
        occupied.append({"schedule_id": "LATE-19", "channel": "wechat", "start_at": date + "T19:00:00+08:00", "end_at": date + "T20:00:00+08:00"})
        replace(occupied)
        _, refreshed = resume(old, 0)
        checks["occupied_choice_reinterrupts"] = refreshed["status"] == "waiting_for_human" and refreshed["interrupt"]["interrupt_id"] != old["interrupt"]["interrupt_id"]
        stale_code, _ = resume(old, 0)
        checks["old_interrupt_rejected"] = stale_code == 409
        _, race_saved = resume(refreshed, 0)
        checks["new_choice_saved"] = race_saved["result"]["persistence"]["status"] == "saved" and store.count() == 2
        replace([])
        _, updated = call("POST", "/schedule", payload)
        checks["changed_occupancy_rechecked_on_resubmit"] = (
            updated["result"]["scheduling"]["accepted_at"].startswith(date + "T18:")
            and updated["result"]["persistence"]["draft_id"] == original_id and store.count() == 2
        )
        _, persisted = call("GET", "/drafts/" + payload["request_id"])
        checks["not_published"] = persisted["publication_status"] == "not_published"
    passed = all(checks.values())
    write_json(directory / "input.json", {"mode": "offline_test", "catalog": "catalog.json", "operations": [
        {key: item[key] for key in ("step", "operation", "method", "path", "arguments") if key in item} for item in events
    ]})
    write_json(directory / "actual.json", {"mode": "offline_test", "real_model_tested": False, "passed": passed,
                                           "checks": checks, "events": events, "final_draft_count": store.count()})
    write_json(directory / "trace.json", {"mode": "offline_test", "real_model_tested": False,
                                          "recorded_at": datetime.now(timezone.utc).isoformat(), "checks": checks, "events": events})
    (directory / "summary.md").write_text(
        "# HTTP 人工时间选择运行记录（离线模型）\n\n本记录实际执行 FastAPI、LangGraph 中断和 SQLite；不是外部模型验收。\n\n"
        + "\n".join(f"- {key}: {value}" for key, value in checks.items())
        + "\n\n实际输入见 input.json，完整输出见 actual.json，逐步 HTTP 与正式占用变更见 trace.json。\n", encoding="utf-8")
    print(f"HTTP interactive: {'PASS' if passed else 'FAIL'}; events={len(events)}", flush=True)
    return {"passed": passed, "input": "examples/04_time_conflict/interactive/input.json",
            "actual": "examples/04_time_conflict/interactive/actual.json", "trace": "examples/04_time_conflict/interactive/trace.json"}


def main():
    runtime_dir = ROOT / "data" / ("submission-validation-" + uuid4().hex)
    runtime_dir.mkdir(parents=True)
    records = []
    cases = sorted(path for path in (ROOT / "examples").iterdir()
                   if path.is_dir() and path.name[:2].isdigit() and (path / "input.json").is_file())
    for case in cases:
        relative_input = (case / "input.json").relative_to(ROOT).as_posix()
        database = runtime_dir / (case.name + ".sqlite3")
        command = [sys.executable, "-X", "utf8", "-m", "src.main", "agent-example", relative_input,
                   "--offline", "--database", str(database)]
        run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        (case / "actual.json").write_text(run.stdout, encoding="utf-8")
        (case / "stderr.txt").write_text(run.stderr, encoding="utf-8")
        actual = json.loads(run.stdout)
        expected = ScenarioExpectation.model_validate_json((case / "expected.json").read_text(encoding="utf-8"))
        comparisons = []
        for outcome, desired in zip(actual["runs"], expected.results, strict=True):
            result = outcome["result"]
            comparisons.append({
                "request_id": result["request_id"],
                "review": result["review"] == desired.review.model_dump(mode="json"),
                "scheduling": result["scheduling"] == desired.scheduling.model_dump(mode="json"),
                "confirmation": confirmation_fields(result["confirmation"]) == confirmation_fields(desired.confirmation.model_dump(mode="json")),
                "persistence": result["persistence"]["status"] == desired.persistence.status,
                "error_codes": [item["code"] for item in result["errors"]] == [item.code for item in desired.errors],
                "not_published": result["publication_status"] == "not_published",
            })
        expected_exit = 1 if case.name == "06_save_failure" else 0
        duplicate_ok = case.name != "05_duplicate_request" or (
            actual["runs"][0]["result"]["persistence"]["draft_id"]
            == actual["runs"][1]["result"]["persistence"]["draft_id"]
        )
        passed = (run.returncode == expected_exit and actual["draft_count"] == expected.final_state.draft_count
                  and duplicate_ok and all(all(value for key, value in entry.items() if key != "request_id") for entry in comparisons))
        trace = {
            "mode": "offline_test", "real_model_tested": False,
            "recorded_at": datetime.now(timezone.utc).isoformat(), "command": command,
            "exit_code": run.returncode, "expected_exit_code": expected_exit,
            "fixture_clock": True, "passed": passed, "checks": comparisons,
            "comparison_policy": "Compare review, status, pending-item fields, times, error codes and draft count; descriptive pending-item messages may use synonymous wording.",
            "runs": [{key: outcome.get(key) for key in (
                "status", "decision", "model_calls", "model_steps", "tool_calls", "execution_steps", "execution_limits", "error",
            )} for outcome in actual["runs"]],
        }
        write_json(case / "trace.json", trace)
        records.append({"scenario_id": case.name, "passed": passed, "input": relative_input,
                        "actual": (case / "actual.json").relative_to(ROOT).as_posix(),
                        "trace": (case / "trace.json").relative_to(ROOT).as_posix(),
                        "exit_code": run.returncode, "draft_count": actual["draft_count"]})
        print(f"{case.name}: {'PASS' if passed else 'FAIL'}; exit={run.returncode}; drafts={actual['draft_count']}", flush=True)
    interactive = record_interactive(runtime_dir)
    manifest = {
        "mode": "offline_test", "real_model_tested": False,
        "recorded_at": datetime.now(timezone.utc).isoformat(), "python": sys.version,
        "platform": platform.platform(), "source_sha256": source_fingerprint(),
        "source_sha256_policy": "Relative paths and source bytes normalized to LF line endings.",
        "scenario_count": len(records), "request_count": sum(len(json.loads((ROOT / entry['actual']).read_text(encoding='utf-8'))['runs']) for entry in records),
        "all_passed": all(entry["passed"] for entry in records) and interactive["passed"],
        "scenarios": records, "interactive": interactive,
    }
    write_json(ROOT / "examples/validation-manifest.json", manifest)
    return 0 if manifest["all_passed"] and len(records) == 7 else 1


if __name__ == "__main__":
    raise SystemExit(main())
