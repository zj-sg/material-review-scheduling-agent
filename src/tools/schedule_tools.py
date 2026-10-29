"""Typed occupancy and candidate tools; an empty result is not a failure."""

from datetime import datetime, timezone

from pydantic import TypeAdapter

from src.schemas.common import NonBlankStr, Timestamp, utc
from src.schemas.request import ScheduleRequest
from src.schemas.result import ErrorDetail
from src.schemas.review import ToolResult
from src.schemas.rules import RuleConfig
from src.schemas.scheduling import CandidateSlots, ScheduleSnapshot, ScheduledSlot, TimeWindow
from src.services.schedule_service import ScheduleSource, SlotSearchService


def query_schedule(
    channel: str, window: TimeWindow, service: ScheduleSource,
    *, queried_at: datetime | None = None,
) -> ToolResult[ScheduleSnapshot]:
    try:
        TypeAdapter(NonBlankStr).validate_python(channel)
        window = TimeWindow.model_validate(window.model_dump())
        raw = service.query(channel, window)
        slots = [
            ScheduledSlot.model_validate(item.model_dump() if isinstance(item, ScheduledSlot) else item)
            for item in raw
        ]
        snapshot = ScheduleSnapshot(
            channel=channel, window=window, existing_schedule=slots,
            queried_at=TypeAdapter(Timestamp).validate_python(
                queried_at if queried_at is not None else datetime.now(timezone.utc)
            ),
        )
        return ToolResult[ScheduleSnapshot](
            status="ok", data=snapshot, source=service.reference, error=None,
        )
    except Exception as exc:
        return ToolResult[ScheduleSnapshot](
            status="error", data=None, source=None,
            error=ErrorDetail(
                code="TOOL_ERROR", message=f"排期查询失败（{type(exc).__name__}），不能判定时段可用。",
            ),
        )


def find_alternative_slots(
    request: ScheduleRequest, rules: RuleConfig, snapshot: ScheduleSnapshot,
    service: SlotSearchService | None = None,
) -> ToolResult[CandidateSlots]:
    try:
        snapshot = ScheduleSnapshot.model_validate(snapshot.model_dump())
        request = ScheduleRequest.model_validate(request.model_dump(), context={"now": snapshot.queried_at})
        rules = RuleConfig.model_validate(rules.model_dump())
        if (
            snapshot.channel != request.channel
            or utc(snapshot.window.start) != utc(request.search_window.start)
            or utc(snapshot.window.end) != utc(request.search_window.end)
        ):
            raise ValueError("snapshot scope differs from request")
        if request.channel not in rules.allowed_channels:
            raise ValueError("cannot schedule an unconfigured channel")
        engine = service if service is not None else SlotSearchService()
        raw = engine.find(request, rules, snapshot)
        candidates = CandidateSlots.model_validate(
            raw.model_dump() if isinstance(raw, CandidateSlots) else raw
        )
        for candidate in candidates.candidates:
            SlotSearchService.validate_candidate(candidate, request, rules, snapshot)
        return ToolResult[CandidateSlots](
            status="ok", data=candidates, source=f"slot-search:rules:{rules.version}", error=None,
        )
    except Exception as exc:
        return ToolResult[CandidateSlots](
            status="error", data=None, source=None,
            error=ErrorDetail(
                code="TOOL_ERROR", message=f"候选时段计算失败（{type(exc).__name__}），不能视为无可用时段。",
            ),
        )
