"""Public data contracts."""

from .confirmation import ConfirmationResult, PendingItem
from .fixtures import ScenarioExpectation, ScenarioInput
from .material import Material, MaterialPatch, MaterialRecord
from .request import ScheduleRequest
from .result import (
    ErrorDetail, PersistenceResult, ReviewResult, Risk, ScheduleResult, SchedulingResult,
)
from .rules import RuleConfig
from .review import MaterialReviewReport, SchedulePlanningReport, ToolResult, ToolTrace
from .scheduling import CandidateSlots, ScheduleSnapshot, ScheduledSlot, SlotCandidate, TimeRequirement, TimeWindow

__all__ = [
    "ConfirmationResult", "ErrorDetail",
    "Material", "MaterialPatch", "MaterialRecord", "PendingItem", "PersistenceResult",
    "ReviewResult", "Risk", "RuleConfig", "ScenarioExpectation", "ScenarioInput",
    "ScheduleRequest", "ScheduleResult", "ScheduledSlot", "SchedulingResult",
    "TimeRequirement", "TimeWindow", "MaterialReviewReport", "ToolResult", "ToolTrace",
    "SchedulePlanningReport", "ScheduleSnapshot", "SlotCandidate", "CandidateSlots",
]
