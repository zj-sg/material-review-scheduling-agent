"""Review-only coordination with one supplementation round and no scheduling."""

from datetime import datetime, timezone

from pydantic import TypeAdapter

from src.schemas.common import Timestamp
from src.schemas.confirmation import ConfirmationResult, PendingItem
from src.schemas.material import MaterialRecord
from src.schemas.request import ScheduleRequest
from src.schemas.result import (
    ErrorDetail, PersistenceResult, ReviewResult, ScheduleResult, SchedulingResult,
)
from src.schemas.review import MaterialReviewReport, ToolResult, ToolTrace
from src.schemas.rules import RuleConfig
from src.tools.material_tools import read_material, supplement_material
from src.tools.rule_tools import check_rules

from .material_service import MaterialSource, is_missing, merge_missing_fields
from .rule_service import RuleService


class ReviewService:
    def __init__(self, source: MaterialSource, rule_service: RuleService | None = None):
        self.source = source
        self.rule_service = rule_service if rule_service is not None else RuleService()

    def review(
        self, request: ScheduleRequest, rules: RuleConfig,
        *, evaluated_at: datetime | None = None,
    ) -> MaterialReviewReport:
        now = TypeAdapter(Timestamp).validate_python(
            evaluated_at if evaluated_at is not None else datetime.now(timezone.utc)
        )
        request = ScheduleRequest.model_validate(request.model_dump(), context={"now": now})
        rules = RuleConfig.model_validate(rules.model_dump())
        actions: list[ToolTrace] = []
        lookup = read_material(request.material_id, self.source)
        actions.append(self._trace("read_material", lookup))
        if lookup.status != "ok":
            return self._report(
                request, rules, now, None, ReviewResult(status="error", missing_fields=[], risks=[]),
                actions, [lookup.error],
            )
        material = lookup.data
        checked = check_rules(material, request, rules, self.rule_service)
        actions.append(self._trace("check_rules", checked))
        if checked.status != "ok":
            return self._report(
                request, rules, now, material, ReviewResult(status="error", missing_fields=[], risks=[]),
                actions, [checked.error],
            )
        review = checked.data

        # Filling empty fields cannot undo a known blocking finding.
        if review.status == "needs_info":
            fields = list(review.missing_fields)
            # Authorization and its conditional evidence are obtained in one round.
            if "authorization_status" in fields and is_missing(material.authorization_evidence):
                if "authorization_evidence" not in fields:
                    fields.append("authorization_evidence")
            supplemented = supplement_material(request.material_id, fields, self.source)
            trace = self._trace("supplement_material", supplemented)
            trace.requested_fields = fields
            if supplemented.status == "ok":
                trace.returned_fields = list(supplemented.data.model_fields_set)
                trace.returned_fields.sort(key=fields.index)
            actions.append(trace)
            if supplemented.status == "error":
                return self._report(
                    request, rules, now, material,
                    ReviewResult(status="error", missing_fields=review.missing_fields, risks=review.risks),
                    actions, [supplemented.error],
                )
            if supplemented.status == "ok":
                material = merge_missing_fields(material, supplemented.data)
                checked = check_rules(material, request, rules, self.rule_service)
                actions.append(self._trace("check_rules", checked))
                if checked.status != "ok":
                    return self._report(
                        request, rules, now, material,
                        ReviewResult(status="error", missing_fields=review.missing_fields, risks=review.risks),
                        actions, [checked.error],
                    )
                review = checked.data

        errors = []
        if review.status == "needs_info":
            errors.append(ErrorDetail(
                code="MATERIAL_INCOMPLETE",
                message="补查后仍缺少：" + "、".join(review.missing_fields) + "。",
            ))
        return self._report(request, rules, now, material, review, actions, errors)

    @staticmethod
    def _trace(tool: str, receipt: ToolResult) -> ToolTrace:
        return ToolTrace(
            tool=tool, status=receipt.status, source=receipt.source,
            review=receipt.data if isinstance(receipt.data, ReviewResult) else None,
            error=receipt.error,
        )

    @staticmethod
    def _report(
        request: ScheduleRequest, rules: RuleConfig, now: datetime,
        material: MaterialRecord | None, review: ReviewResult,
        actions: list[ToolTrace], errors: list[ErrorDetail],
    ) -> MaterialReviewReport:
        pending = []
        if review.status == "needs_info":
            for field in review.missing_fields:
                pending.append(PendingItem(
                    kind="provide_metadata", field=field, original_value=None, proposed_value=None,
                    message=f"补查后仍缺少 {field}，请提供可信资料。",
                ))
        elif review.status == "needs_review":
            for risk in review.risks:
                if risk.level == "review":
                    pending.append(PendingItem(
                        kind="risk_resolution", field=risk.field,
                        original_value=getattr(material, risk.field), proposed_value=None,
                        message="请补充可信授权证据并通过显式修订更新授权状态，再重新审核；单纯确认不能解除风险。",
                    ))
        result = ScheduleResult(
            request_id=request.request_id, material_id=request.material_id, evaluated_at=now,
            review=review,
            scheduling=SchedulingResult(
                status="not_evaluated", requested_at=request.time_requirement.requested_at,
                suggested_at=None, accepted_at=None, conflicts=[],
            ),
            confirmation=ConfirmationResult(
                status="pending" if pending else "not_required", pending_items=pending,
            ),
            persistence=PersistenceResult(
                status="not_attempted", draft_id=None, error=None,
            ),
            errors=errors, publication_status="not_published",
        )
        return MaterialReviewReport(
            result=result, material=material, rules_version=rules.version, actions=actions,
        )
