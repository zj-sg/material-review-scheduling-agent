"""Stage 3 tool receipts and review-only reports."""

from typing import Generic, Literal, TypeVar

from pydantic import Field, model_validator

from .common import ContractModel, NonBlankStr
from .material import MaterialRecord
from .result import ErrorDetail, ReviewResult, ScheduleResult
from .rules import MaterialField
from .scheduling import CandidateSlots, ScheduleSnapshot

ToolData = TypeVar("ToolData", bound=ContractModel)
ToolStatus = Literal["ok", "empty", "not_found", "error"]
ToolName = Literal[
    "read_material", "supplement_material", "check_rules", "query_schedule", "find_alternative_slots",
]


class ToolResult(ContractModel, Generic[ToolData]):
    status: ToolStatus
    data: ToolData | None
    source: NonBlankStr | None
    error: ErrorDetail | None

    @model_validator(mode="after")
    def check_receipt(self) -> "ToolResult[ToolData]":
        if self.status == "ok":
            if self.data is None or self.source is None or self.error is not None:
                raise ValueError("successful tool result requires data, source and no error")
        else:
            if self.data is not None:
                raise ValueError("unsuccessful tool result cannot contain successful data")
            if self.status == "empty":
                if self.error is not None or self.source is None:
                    raise ValueError("empty lookup requires a source and no tool error")
            else:
                expected = "MATERIAL_NOT_FOUND" if self.status == "not_found" else "TOOL_ERROR"
                if self.error is None or self.error.code != expected:
                    raise ValueError(f"{self.status} tool result requires {expected}")
        return self


class ToolTrace(ContractModel):
    tool: ToolName
    status: ToolStatus
    source: NonBlankStr | None
    requested_fields: list[MaterialField] = Field(default_factory=list)
    returned_fields: list[MaterialField] = Field(default_factory=list)
    review: ReviewResult | None = None
    schedule_snapshot: ScheduleSnapshot | None = None
    candidate_slots: CandidateSlots | None = None
    error: ErrorDetail | None = None


class MaterialReviewReport(ContractModel):
    result: ScheduleResult
    material: MaterialRecord | None
    rules_version: NonBlankStr
    actions: list[ToolTrace] = Field(min_length=1)

    @model_validator(mode="after")
    def check_review_only(self) -> "MaterialReviewReport":
        if self.result.scheduling.status != "not_evaluated":
            raise ValueError("review-only reports cannot claim evaluated scheduling")
        if self.result.persistence.status != "not_attempted":
            raise ValueError("review-only reports cannot claim a saved draft")
        if self.material is not None and self.material.material_id != self.result.material_id:
            raise ValueError("reviewed material must match result material_id")
        return self


class SchedulePlanningReport(ContractModel):
    result: ScheduleResult
    material: MaterialRecord | None
    rules_version: NonBlankStr
    actions: list[ToolTrace] = Field(min_length=1)

    @model_validator(mode="after")
    def check_unsaved(self) -> "SchedulePlanningReport":
        if self.result.persistence.status != "not_attempted":
            raise ValueError("planning reports cannot claim a saved draft")
        if self.material is not None and self.material.material_id != self.result.material_id:
            raise ValueError("planned material must match result material_id")
        return self


class DraftSelectionReport(ContractModel):
    """Fresh review/query evidence plus the outcome of a human-selected draft save."""

    result: ScheduleResult
    material: MaterialRecord | None
    rules_version: NonBlankStr
    actions: list[ToolTrace] = Field(min_length=1)
