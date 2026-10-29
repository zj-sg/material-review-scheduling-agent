"""Per-request tool bindings; model arguments cannot change business inputs."""

from datetime import datetime, timezone

from langchain_core.tools import tool
from pydantic import Field, TypeAdapter

from src.schemas import ReviewResult, ScheduleResult, ToolTrace
from src.schemas.result import ErrorDetail
from src.schemas.rules import MaterialField
from src.schemas.common import ContractModel, NonBlankStr
from src.services.draft_service import DraftService
from src.services.material_service import is_missing, merge_missing_fields
from src.services.planning_service import PlanningService, mark_selection_pending, needs_time_selection
from src.services.review_service import ReviewService
from src.services.storage import SQLiteScheduleSource
from src.tools.draft_tools import save_draft
from src.tools.material_tools import read_material, supplement_material
from src.tools.rule_tools import check_rules


class DecisionArguments(ContractModel):
    reason: NonBlankStr = Field(max_length=240, description="用一句话说明本动作依据的请求或已返回事实，不输出内部思维过程")


class SupplementArguments(DecisionArguments):
    fields: list[MaterialField] = Field(min_length=1, description="本轮审核确认缺失、需要补查的字段")


class AgentTools:
    def __init__(self, request, rules, source, store, evaluated_at=None, *, pause_on_conflict=False):
        self.request, self.rules = request, rules
        self.source, self.store = source, store
        self.evaluated_at = evaluated_at
        self.material = None
        self.review = None
        self.errors = []
        self.traces = []
        self.supplemented = False
        self.plan = None
        self.saved = None
        self.pause_on_conflict = pause_on_conflict

    def waiting_for_time(self):
        result = self.result()
        return self.pause_on_conflict and result is not None and needs_time_selection(result)

    def now(self):
        return self.evaluated_at or datetime.now(timezone.utc)

    def report(self):
        if self.review is None:
            return None
        errors = list(self.errors)
        if self.review.status == "needs_info" and not errors:
            errors.append(ErrorDetail(
                code="MATERIAL_INCOMPLETE",
                message="补查后仍缺少：" + "、".join(self.review.missing_fields),
            ))
        return ReviewService._report(
            self.request, self.rules, self.now(), self.material, self.review, self.traces, errors,
        )

    def result(self) -> ScheduleResult | None:
        if self.saved is not None:
            return self.saved
        if self.plan is not None:
            return self.plan.result
        report = self.report()
        return report.result if report else None

    def registered_tools(self):
        """Create actual LangChain tools bound to this request, without a name dispatcher."""
        @tool(args_schema=DecisionArguments)
        def read_material(reason: str) -> dict:
            """读取当前请求的真实素材，不能指定其他素材或改动请求。"""
            return self._read_material()

        @tool(args_schema=DecisionArguments)
        def check_rules(reason: str) -> dict:
            """检查当前已读取或补齐的素材，返回规则风险和缺失项。"""
            return self._check_rules()

        @tool(args_schema=SupplementArguments)
        def supplement_material(fields: list[MaterialField], reason: str) -> dict:
            """最多一次补查实际缺失字段；已有内容不会被覆盖。"""
            return self._supplement_material(fields)

        @tool(args_schema=DecisionArguments)
        def query_slots(reason: str) -> dict:
            """审核通过后查询正式占用与候选，返回建议和待确认项，不保存。"""
            return self._query_slots()

        @tool(args_schema=DecisionArguments)
        def save_draft(reason: str) -> dict:
            """实际保存草稿；事务中重查，返回真实保存状态，永不发布或替用户确认。"""
            return self._save_draft()

        return [read_material, check_rules, supplement_material, query_slots, save_draft]

    def _read_material(self):
        receipt = read_material(self.request.material_id, self.source)
        self.traces.append(ReviewService._trace("read_material", receipt))
        self.plan = None
        self.material = receipt.data
        self.review = None if receipt.status == "ok" else ReviewResult(
            status="error", missing_fields=[], risks=[],
        )
        self.errors = [] if receipt.error is None else [receipt.error]
        return receipt.model_dump(mode="json")

    def _check_rules(self):
        if self.material is None:
            raise ValueError("必须先成功读取素材")
        receipt = check_rules(self.material, self.request, self.rules)
        self.traces.append(ReviewService._trace("check_rules", receipt))
        self.plan = None
        self.review = receipt.data if receipt.status == "ok" else ReviewResult(
            status="error", missing_fields=[], risks=[],
        )
        self.errors = [] if receipt.error is None else [receipt.error]
        return receipt.model_dump(mode="json")

    def _supplement_material(self, fields):
        if self.review is None or self.review.status != "needs_info" or self.supplemented:
            raise ValueError("仅在检查发现缺失时补查一次")
        checked = TypeAdapter(list[MaterialField]).validate_python(fields)
        allowed = set(self.review.missing_fields)
        if "authorization_status" in allowed and is_missing(self.material.authorization_evidence):
            allowed.add("authorization_evidence")
        if not checked or len(set(checked)) != len(checked) or not set(checked) <= allowed:
            raise ValueError("只能补查已确认缺失的字段")
        # Resolve the authorization/evidence dependency in the same lookup.
        if "authorization_status" in checked and "authorization_evidence" in allowed:
            if "authorization_evidence" not in checked:
                checked.append("authorization_evidence")
        self.supplemented = True
        receipt = supplement_material(self.request.material_id, checked, self.source)
        trace = ReviewService._trace("supplement_material", receipt)
        trace.requested_fields = checked
        if receipt.status == "ok":
            trace.returned_fields = sorted(receipt.data.model_fields_set)
            self.material = merge_missing_fields(self.material, receipt.data)
            self.review = None  # New data must be checked again.
        elif receipt.status == "error":
            self.review = ReviewResult(status="error", missing_fields=[], risks=[])
        self.plan = None
        self.errors = [] if receipt.error is None else [receipt.error]
        self.traces.append(trace)
        return receipt.model_dump(mode="json")

    def _query_slots(self):
        if self.review is None or self.review.status != "passed" or self.errors:
            raise ValueError("只有审核通过后才能查询时段")
        try:
            with self.store.transaction() as connection:
                planner = PlanningService(
                    self.source, SQLiteScheduleSource(connection, self.store.reference),
                )
                self.plan = planner.plan_reviewed(
                    self.request, self.rules, self.report(),
                    queried_at=self.evaluated_at,
                )
                if self.pause_on_conflict:
                    self.plan = mark_selection_pending(self.plan)
                self.traces = list(self.plan.actions)
            candidates = next(
                (trace.candidate_slots for trace in reversed(self.traces) if trace.candidate_slots is not None),
                None,
            )
            return {
                "status": "error" if self.plan.result.errors else "ok",
                "result": self.plan.result.model_dump(mode="json"),
                "candidates": candidates.model_dump(mode="json") if candidates else None,
            }
        except Exception as exc:
            error = ErrorDetail(code="TOOL_ERROR", message=f"排期查询失败（{type(exc).__name__}）")
            self.errors = [error]
            self.plan = None
            self.traces.append(ToolTrace(
                tool="query_schedule", status="error", source=None, error=error,
            ))
            return {"status": "error", "error": error.model_dump()}

    def _save_draft(self):
        if not self.request.save_draft:
            raise ValueError("当前请求未要求保存")
        if self.review is None or self.review.status not in {"passed", "needs_review"} or self.errors:
            raise ValueError("当前审核结果不允许保存")
        if self.review.status == "passed" and (self.plan is None or self.plan.result.errors):
            raise ValueError("必须先成功查询时段，才能决定保存")
        self.saved = save_draft(
            self.request, self.rules, DraftService(self.store, self.source),
            evaluated_at=self.evaluated_at,
            require_time_selection=self.pause_on_conflict,
        )
        return self.saved.model_dump(mode="json")

    def completion_problem(self):
        if self.saved is not None:
            return None  # Saving independently rechecks and returns the latest authoritative result.
        result = self.result()
        if result is None:
            return "Agent 未完成素材读取及规则检查"
        if result.review.status == "needs_info" and not self.supplemented:
            return "Agent 未尝试补查缺失信息"
        if result.review.status == "passed" and self.plan is None and self.saved is None and not result.errors:
            return "Agent 未查询时段"
        if (
            self.request.save_draft and result.review.status in {"passed", "needs_review"}
            and self.saved is None and not result.errors
        ):
            return "Agent 未实际调用保存工具"
        return None
