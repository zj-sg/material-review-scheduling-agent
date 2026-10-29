"""Save a freshly checked current draft, optionally using an explicit human selection."""

import json
from datetime import datetime, timezone
from uuid import uuid4

from src.schemas.common import utc
from src.schemas.confirmation import ConfirmationResult
from src.schemas.request import ScheduleRequest
from src.schemas.result import ErrorDetail, PersistenceResult, ScheduleResult
from src.schemas.review import DraftSelectionReport
from src.schemas.rules import RuleConfig

from .material_service import MaterialSource
from .planning_service import PlanningService, mark_selection_pending, needs_time_selection
from .review_service import ReviewService
from .schedule_service import SlotSearchService
from .storage import SQLiteScheduleSource, SQLiteStore, request_key


class DraftService:
    def __init__(self, store: SQLiteStore, material_source: MaterialSource):
        self.store = store
        self.material_source = material_source

    def save(
        self, request: ScheduleRequest, rules: RuleConfig,
        *, evaluated_at: datetime | None = None, require_time_selection: bool = False,
    ) -> ScheduleResult:
        reviewed = ReviewService(self.material_source).review(
            request, rules, evaluated_at=evaluated_at,
        )
        current = reviewed.result
        if not request.save_draft or current.review.status not in {"passed", "needs_review"}:
            return current

        try:
            canonical = request_key(request)
            with self.store.transaction() as connection:
                existing = connection.execute(
                    "SELECT draft_id, request_json FROM drafts WHERE request_id = ?",
                    (request.request_id,),
                ).fetchone()
                if existing and existing["request_json"] != canonical:
                    connection.rollback()
                    return self._error(current, ErrorDetail(
                        code="IDEMPOTENCY_CONFLICT",
                        message="相同 request_id 对应不同输入，请使用新的请求 ID。",
                    ))

                # The source reads the same database using the active write transaction.
                planner = PlanningService(
                    self.material_source, SQLiteScheduleSource(connection, self.store.reference),
                )
                report = planner.plan_reviewed(
                    request, rules, reviewed,
                    queried_at=reviewed.result.evaluated_at if evaluated_at is not None else None,
                )
                current = report.result
                if current.errors:
                    connection.rollback()
                    return current
                if require_time_selection and needs_time_selection(current):
                    return mark_selection_pending(report).result
                accepted = current.scheduling.accepted_at
                if (
                    request.time_requirement.mode == "fixed" and accepted is not None
                    and utc(accepted) != utc(request.time_requirement.requested_at)
                ):
                    raise ValueError("未经用户确认，不能接受新的硬性发布时间")

                saved = self._write_report(connection, request, rules, report, existing)
            # This line is reached only after the context manager's commit succeeds.
            return saved
        except Exception as exc:
            return self._error(current, ErrorDetail(
                code="DRAFT_SAVE_FAILED",
                message=f"草稿保存失败（{type(exc).__name__}）：{exc}",
            ), failed=True)

    def save_selected(
        self, request: ScheduleRequest, rules: RuleConfig, *, selected_at: datetime,
        interrupt_id: str, evaluated_at: datetime | None = None,
    ) -> DraftSelectionReport:
        """Trusted resume path only; selection membership is checked by SchedulingRuns.

        Revalidate the exact selected time under the write lock. An unavailable choice
        returns a fresh report without writing or silently choosing another slot.
        """
        now = evaluated_at or datetime.now(timezone.utc)
        reviewed = ReviewService(self.material_source).review(request, rules, evaluated_at=now)
        report = DraftSelectionReport.model_validate(reviewed.model_dump())
        if reviewed.result.review.status != "passed":
            return report
        current = reviewed.result
        try:
            with self.store.transaction() as connection:
                existing = connection.execute(
                    "SELECT draft_id, request_json FROM drafts WHERE request_id = ?", (request.request_id,),
                ).fetchone()
                if existing and existing["request_json"] != request_key(request):
                    report.result = self._error(current, ErrorDetail(
                        code="IDEMPOTENCY_CONFLICT", message="相同 request_id 对应不同输入，请使用新的请求 ID。",
                    ))
                    return report
                planned = PlanningService(
                    self.material_source, SQLiteScheduleSource(connection, self.store.reference),
                ).plan_reviewed(request, rules, reviewed, queried_at=now)
                report = DraftSelectionReport.model_validate(planned.model_dump())
                current = report.result
                if current.errors:
                    return report
                snapshot = next(trace.schedule_snapshot for trace in reversed(report.actions)
                                if trace.schedule_snapshot is not None)
                candidate = SlotSearchService._candidate(selected_at, request, rules, snapshot)
                if candidate is None:
                    return mark_selection_pending(report)
                try:
                    SlotSearchService.validate_candidate(candidate, request, rules, snapshot)
                except ValueError:
                    return mark_selection_pending(report)
                payload = current.model_dump()
                payload["scheduling"].update(
                    status="available", suggested_at=selected_at, accepted_at=selected_at,
                )
                payload["confirmation"] = ConfirmationResult(
                    status="confirmed", pending_items=[], selected_at=selected_at,
                    confirmed_at=now, interrupt_id=interrupt_id,
                ).model_dump()
                report.result = ScheduleResult.model_validate(payload)
                if request.save_draft:
                    saved = self._write_report(connection, request, rules, report, existing)
                else:
                    saved = report.result
            report.result = saved  # Only claim persistence after commit.
            return report
        except Exception as exc:
            report.result = self._error(current, ErrorDetail(
                code="DRAFT_SAVE_FAILED", message=f"草稿保存失败（{type(exc).__name__}）：{exc}",
            ), failed=True)
            return report

    def _write_report(self, connection, request, rules, report, existing):
        draft_id = existing["draft_id"] if existing else "DRAFT-" + uuid4().hex
        payload = report.result.model_dump()
        payload["persistence"] = PersistenceResult(
            status="already_exists" if existing else "saved", draft_id=draft_id, error=None,
        ).model_dump()
        saved = ScheduleResult.model_validate(payload)
        document = report.model_dump(mode="json")
        document["result"] = saved.model_dump(mode="json")
        self.store.write_draft(
            connection, draft_id=draft_id, request_id=request.request_id,
            canonical_request=request_key(request), rules_json=rules.model_dump_json(),
            report_json=json.dumps(document, ensure_ascii=False),
        )
        return saved

    @staticmethod
    def _error(
        result: ScheduleResult, error: ErrorDetail, *, failed: bool = False,
    ) -> ScheduleResult:
        payload = result.model_dump()
        payload["errors"].append(error.model_dump())
        payload["persistence"] = PersistenceResult(
            status="failed" if failed else "not_attempted", draft_id=None,
            error=error if failed else None,
        ).model_dump()
        return ScheduleResult.model_validate(payload)
