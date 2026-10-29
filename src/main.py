"""Contract validation and local deterministic review commands."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from pydantic import ValidationError

from .config import load_settings
from .schemas import ScenarioExpectation, ScenarioInput, ScheduleRequest
from .services.material_service import JsonMaterialService
from .services.review_service import ReviewService
from .services.planning_service import PlanningService
from .services.schedule_service import JsonScheduleService
from .services.storage import SQLiteStore
from .services.draft_service import DraftService
from .tools.draft_tools import save_draft


class _FailingSaveStore(SQLiteStore):
    """CLI fixture injection only: fail after SQL, before the transaction commits."""

    def write_draft(self, connection, **values):
        super().write_draft(connection, **values)
        raise sqlite3.OperationalError("示例注入保存失败，事务未提交")


def validate_examples(directory: Path) -> tuple[int, int]:
    if not directory.is_dir():
        raise ValueError(f"example directory does not exist: {directory}")
    case_paths = sorted(path for path in directory.iterdir() if path.is_dir() and path.name != "__pycache__")
    if not case_paths:
        raise ValueError("no scenario directories found")
    steps = 0
    for case_path in case_paths:
        source = ScenarioInput.model_validate_json(
            (case_path / "input.json").read_text(encoding="utf-8-sig")
        )
        expected = ScenarioExpectation.model_validate_json(
            (case_path / "expected.json").read_text(encoding="utf-8-sig")
        )
        if source.scenario_id != case_path.name or expected.scenario_id != source.scenario_id:
            raise ValueError(f"{case_path.name}: scenario ID mismatch")
        if len(source.steps) != len(expected.results):
            raise ValueError(f"{case_path.name}: request/result count mismatch")
        for step, result in zip(source.steps, expected.results, strict=True):
            if (
                step.request.request_id != result.request_id
                or step.request.material_id != result.material_id
                or step.request.time_requirement.requested_at != result.scheduling.requested_at
            ):
                raise ValueError(f"{case_path.name}: request/result identity or original time mismatch")
            if result.evaluated_at > step.evaluated_at:
                raise ValueError(f"{case_path.name}: result evaluation cannot be in the future")
            if result.persistence.status != "already_exists" and result.evaluated_at != step.evaluated_at:
                raise ValueError(f"{case_path.name}: fresh result must use this step's evaluation time")
        steps += len(source.steps)
        print(f"通过：{case_path.name}（{len(source.steps)} 个请求步骤）")
    return len(case_paths), steps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="素材排期项目：数据校验与素材审核")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show-config", help="查看基础配置，不创建数据库")
    examples = commands.add_parser("validate-examples", help="校验静态验收样例，不执行审核或保存")
    examples.add_argument("--directory", type=Path)
    request = commands.add_parser("validate-request", help="校验一份独立业务请求 JSON")
    request.add_argument("path", type=Path)
    review = commands.add_parser("review-example", help="按夹具记录时间执行素材读取、补查和审核")
    review.add_argument("path", type=Path, help="场景 input.json")
    review.add_argument("--step", type=int, default=1, help="从 1 开始的请求步骤编号")
    review.add_argument("--material-source", type=Path, help="可选：使用独立的可信本地素材文件")
    plan = commands.add_parser("plan-example", help="执行素材审核、冲突检测和候选查询，不保存草稿")
    plan.add_argument("path", type=Path, help="场景 input.json")
    plan.add_argument("--step", type=int, default=1, help="从 1 开始的请求步骤编号")
    plan.add_argument("--material-source", type=Path, help="可选：可信本地素材文件")
    plan.add_argument("--schedule-source", type=Path, help="可选：可信正式排期文件")
    save = commands.add_parser("save-example", help="执行场景并实际保存 SQLite 草稿")
    save.add_argument("path", type=Path, help="场景 input.json，默认顺序执行所有步骤")
    save.add_argument("--step", type=int, help="可选：只执行指定步骤，从 1 开始")
    save.add_argument("--database", type=Path, help="SQLite 文件，默认使用基础配置")
    save.add_argument("--material-source", type=Path, help="可选：可信素材文件")
    save.add_argument("--schedule-source", type=Path, help="可选：导入正式排期的 JSON 文件")
    agent = commands.add_parser("agent-example", help="运行单个 LangGraph Agent")
    agent.add_argument("path", type=Path, help="场景 input.json，默认执行全部步骤")
    agent.add_argument("--step", type=int, help="可选：从 1 开始的步骤编号")
    agent.add_argument("--database", type=Path, help="独立验收数据库文件")
    agent.add_argument("--material-source", type=Path, help="可选：可信素材文件")
    agent.add_argument("--schedule-source", type=Path, help="可选：导入正式排期文件")
    agent.add_argument("--offline", action="store_true", help="显式使用离线测试模型，不调用外部模型")
    args = parser.parse_args(argv)

    try:
        if args.command == "show-config":
            print(json.dumps(load_settings().model_dump(mode="json"), ensure_ascii=False, indent=2))
        elif args.command == "validate-examples":
            directory = args.directory
            if directory is None:
                directory = load_settings().examples_dir
            count, steps = validate_examples(directory)
            print(f"共 {count} 类场景、{steps} 个请求步骤，数据结构校验通过。未执行真实审核或保存。")
        elif args.command == "agent-example":
            from .agents.model import build_model
            from .agents.offline import OfflineTestModel
            from .agents.workflow import ReviewSchedulingAgent

            scenario = ScenarioInput.model_validate_json(args.path.read_text(encoding="utf-8-sig"))
            if args.step is not None and not 1 <= args.step <= len(scenario.steps):
                raise ValueError(f"step must be between 1 and {len(scenario.steps)}")
            settings = load_settings()
            model = OfflineTestModel() if args.offline else build_model(settings)
            try:
                store = SQLiteStore(args.database if args.database is not None else settings.database_path)
                source = JsonMaterialService(args.material_source if args.material_source else args.path)
                store.replace_schedule(JsonScheduleService(
                    args.schedule_source if args.schedule_source else args.path,
                ).read_all())
                selected = [scenario.steps[args.step - 1]] if args.step is not None else scenario.steps
                runs = []
                for step in selected:
                    active_store = _FailingSaveStore(store.path) if step.save_behavior == "fail" else store
                    runs.append(ReviewSchedulingAgent(model, source, active_store, max_calls=settings.max_model_calls).run(
                        step.request, scenario.rules, evaluated_at=step.evaluated_at,
                    ))
                try:
                    count = store.count()
                except (OSError, sqlite3.Error):
                    count = None
                print(json.dumps({
                    "scenario_id": scenario.scenario_id, "mode": model.mode,
                    "model": None if args.offline else settings.model,
                    "runs": [run.to_dict() for run in runs], "draft_count": count,
                }, ensure_ascii=False, indent=2))
                if any(run.status in {"failed", "incomplete"} for run in runs):
                    return 1
            finally:
                if hasattr(model, "close"):
                    model.close()
        elif args.command == "save-example":
            scenario = ScenarioInput.model_validate_json(args.path.read_text(encoding="utf-8-sig"))
            if args.step is not None and not 1 <= args.step <= len(scenario.steps):
                raise ValueError(f"step must be between 1 and {len(scenario.steps)}")
            store = SQLiteStore(args.database if args.database is not None else load_settings().database_path)
            source = JsonMaterialService(args.material_source if args.material_source else args.path)
            store.replace_schedule(JsonScheduleService(
                args.schedule_source if args.schedule_source else args.path,
            ).read_all())
            selected = [scenario.steps[args.step - 1]] if args.step is not None else scenario.steps
            results = []
            for step in selected:
                active_store = _FailingSaveStore(store.path) if step.save_behavior == "fail" else store
                results.append(save_draft(
                    step.request, scenario.rules, DraftService(active_store, source),
                    evaluated_at=step.evaluated_at,
                ))
            try:
                count = store.count()
            except (OSError, sqlite3.Error):
                count = None
            print(json.dumps({
                "scenario_id": scenario.scenario_id, "database": str(store.path),
                "results": [result.model_dump(mode="json") for result in results],
                "draft_count": count,
            }, ensure_ascii=False, indent=2))
            if any(
                error.code != "MATERIAL_INCOMPLETE" for result in results for error in result.errors
            ):
                return 1
        elif args.command in {"review-example", "plan-example"}:
            scenario = ScenarioInput.model_validate_json(args.path.read_text(encoding="utf-8-sig"))
            if not 1 <= args.step <= len(scenario.steps):
                raise ValueError(f"step must be between 1 and {len(scenario.steps)}")
            step = scenario.steps[args.step - 1]
            source = JsonMaterialService(args.material_source if args.material_source else args.path)
            if args.command == "review-example":
                report = ReviewService(source).review(
                    step.request, scenario.rules, evaluated_at=step.evaluated_at,
                )
            else:
                schedule_source = JsonScheduleService(
                    args.schedule_source if args.schedule_source else args.path,
                )
                report = PlanningService(source, schedule_source).plan(
                    step.request, scenario.rules, evaluated_at=step.evaluated_at,
                )
            print(report.model_dump_json(indent=2))
            if any(error.code in {"TOOL_ERROR", "MATERIAL_NOT_FOUND"} for error in report.result.errors):
                return 1
        else:
            ScheduleRequest.model_validate_json(args.path.read_text(encoding="utf-8-sig"))
            print("业务请求数据结构校验通过。")
    except ValidationError as exc:
        print("数据校验失败：", file=sys.stderr)
        for error in exc.errors(include_input=False, include_url=False):
            location = ".".join(str(part) for part in error["loc"]) or "<root>"
            print(f"  {location}: {error['msg']}", file=sys.stderr)
        return 1
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"校验失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
