"""Deterministic review plus scheduling; no persistence or publication."""

from datetime import datetime

from src.schemas.confirmation import ConfirmationResult, PendingItem
from src.schemas.request import ScheduleRequest
from src.schemas.result import ScheduleResult, SchedulingResult
from src.schemas.review import MaterialReviewReport, SchedulePlanningReport, ToolTrace
from src.schemas.rules import RuleConfig
from src.schemas.common import utc
from src.tools.schedule_tools import find_alternative_slots, query_schedule

from .material_service import MaterialSource
from .review_service import ReviewService
from .rule_service import RuleService
from .schedule_service import ScheduleSource, SlotSearchService, end_after, overlaps


def needs_time_selection(result: ScheduleResult) -> bool:
    return (
        result.review.status == "passed" and not result.errors
        and result.scheduling.suggested_at is not None
        and utc(result.scheduling.suggested_at) != utc(result.scheduling.requested_at)
    )


def mark_selection_pending(report: SchedulePlanningReport) -> SchedulePlanningReport:
    """Interactive callers also ask before replacing a preferred time inside a window."""
    if needs_time_selection(report.result) and report.result.scheduling.status != "alternative_proposed":
        payload = report.result.model_dump()
        payload["scheduling"].update(status="alternative_proposed", accepted_at=None)
        payload["confirmation"] = ConfirmationResult(status="pending", pending_items=[PendingItem(
            kind="time_change", field="time_requirement.requested_at",
            original_value=report.result.scheduling.requested_at.isoformat(),
            proposed_value=report.result.scheduling.suggested_at.isoformat(),
            message="原优选时段不可用，请从可用候选中选择时间。",
        )]).model_dump()
        report.result = ScheduleResult.model_validate(payload)
    return report


class PlanningService:
    def __init__(
        self, material_source: MaterialSource, schedule_source: ScheduleSource,
        *, rule_service: RuleService | None = None, slot_service: SlotSearchService | None = None,
    ):
        self.reviewer = ReviewService(material_source, rule_service)
        self.schedule_source = schedule_source
        self.slot_service = slot_service

    def plan(
        self, request: ScheduleRequest, rules: RuleConfig,
        *, evaluated_at: datetime | None = None,
    ) -> SchedulePlanningReport:
        reviewed = self.reviewer.review(request, rules, evaluated_at=evaluated_at)
        return self.plan_reviewed(
            request, rules, reviewed,
            queried_at=reviewed.result.evaluated_at if evaluated_at is not None else None,
        )

    def plan_reviewed(
        self, request: ScheduleRequest, rules: RuleConfig, reviewed: MaterialReviewReport,
        *, queried_at: datetime | None = None,
    ) -> SchedulePlanningReport:
        if (
            reviewed.result.request_id != request.request_id
            or reviewed.result.material_id != request.material_id
            or utc(reviewed.result.scheduling.requested_at) != utc(request.time_requirement.requested_at)
            or reviewed.rules_version != rules.version
        ):
            raise ValueError("review does not match the current request and rules")
        report = SchedulePlanningReport.model_validate(reviewed.model_dump())
        if report.result.review.status != "passed":
            return report
        receipt = query_schedule(
            request.channel, request.search_window, self.schedule_source,
            queried_at=queried_at,
        )
        report.actions.append(ToolTrace(
            tool="query_schedule", status=receipt.status, source=receipt.source,
            schedule_snapshot=receipt.data, error=receipt.error,
        ))
        if receipt.status != "ok":
            return self._error(report, receipt.error)

        snapshot = receipt.data
        payload = report.result.model_dump()
        payload["evaluated_at"] = snapshot.queried_at
        report.result = ScheduleResult.model_validate(payload)
        found = find_alternative_slots(request, rules, snapshot, self.slot_service)
        report.actions.append(ToolTrace(
            tool="find_alternative_slots", status=found.status, source=found.source,
            candidate_slots=found.data, error=found.error,
        ))
        if found.status != "ok":
            return self._error(report, found.error)

        requirement = request.time_requirement
        original_end = end_after(requirement.requested_at, rules.slot_duration_minutes)
        original_conflicts = [
            slot for slot in snapshot.existing_schedule
            if overlaps(requirement.requested_at, original_end, slot)
        ]
        pending: list[PendingItem] = []
        if not found.data.candidates:
            status = "no_slot"
            suggested = accepted = None
            conflicts = snapshot.existing_schedule
            pending.append(PendingItem(
                kind="provide_time_window", field="search_window",
                original_value=request.search_window.model_dump(mode="json"), proposed_value=None,
                message="原时间要求与查询范围内没有可用时段，请明确补充或修改时间要求；当前未放宽任何约束。",
            ))
        else:
            chosen = found.data.candidates[0]
            suggested = chosen.start_at
            conflicts = original_conflicts
            if chosen.requires_confirmation:
                status, accepted = "alternative_proposed", None
                if utc(chosen.start_at) != utc(requirement.requested_at):
                    detail = (
                        "原时段与排期 " + "、".join(slot.schedule_id for slot in original_conflicts) + " 冲突。"
                        if original_conflicts else "原要求时段无法同时满足当前范围与占用条件。"
                    )
                    pending.append(PendingItem(
                        kind="time_change", field="time_requirement.requested_at",
                        original_value=requirement.requested_at.isoformat(),
                        proposed_value=chosen.start_at.isoformat(),
                        message=detail + chosen.reason + "请确认具体建议时间。",
                    ))
            else:
                status, accepted = "available", chosen.start_at

        payload = report.result.model_dump()
        payload["scheduling"] = SchedulingResult(
            status=status, requested_at=requirement.requested_at,
            suggested_at=suggested, accepted_at=accepted, conflicts=conflicts,
        ).model_dump()
        payload["confirmation"] = ConfirmationResult(
            status="pending" if pending else "not_required", pending_items=pending,
        ).model_dump()
        report.result = ScheduleResult.model_validate(payload)
        return SchedulePlanningReport.model_validate(report.model_dump())

    @staticmethod
    def _error(report: SchedulePlanningReport, error) -> SchedulePlanningReport:
        payload = report.result.model_dump()
        payload["errors"].append(error.model_dump())
        report.result = ScheduleResult.model_validate(payload)
        return SchedulePlanningReport.model_validate(report.model_dump())
