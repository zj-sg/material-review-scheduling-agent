"""Draft tool returns the actual database outcome, never a model assertion."""

from datetime import datetime

from src.schemas.request import ScheduleRequest
from src.schemas.result import ScheduleResult
from src.schemas.rules import RuleConfig
from src.services.draft_service import DraftService


def save_draft(
    request: ScheduleRequest, rules: RuleConfig, service: DraftService,
    *, evaluated_at: datetime | None = None, require_time_selection: bool = False, require_human_review: bool = False,
) -> ScheduleResult:
    return service.save(request, rules, evaluated_at=evaluated_at, require_time_selection=require_time_selection, require_human_review=require_human_review)
