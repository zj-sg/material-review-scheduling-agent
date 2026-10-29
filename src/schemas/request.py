"""Public request validation; an injected clock makes fixtures reproducible."""

from datetime import datetime, timezone

from pydantic import StrictBool, TypeAdapter, ValidationInfo, model_validator

from .common import ContractModel, NonBlankStr, Timestamp, utc
from .scheduling import TimeRequirement, TimeWindow


class ScheduleRequest(ContractModel):
    request_id: NonBlankStr
    material_id: NonBlankStr
    channel: NonBlankStr
    time_requirement: TimeRequirement
    search_window: TimeWindow
    save_draft: StrictBool

    @model_validator(mode="after")
    def check_future(self, info: ValidationInfo) -> "ScheduleRequest":
        context = info.context or {}
        now = TypeAdapter(Timestamp).validate_python(context.get("now", datetime.now(timezone.utc)))
        if utc(self.time_requirement.requested_at) <= utc(now):
            raise ValueError("requested_at must be later than the evaluation time")
        if utc(self.search_window.start) < utc(now):
            raise ValueError("search_window cannot start in the past")
        if (
            self.time_requirement.window_start is not None
            and utc(self.time_requirement.window_start) < utc(now)
        ):
            raise ValueError("allowed window cannot start in the past")
        return self
