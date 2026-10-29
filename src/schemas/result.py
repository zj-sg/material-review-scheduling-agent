"""Structured results with structural and cross-state consistency checks."""

from typing import Literal

from pydantic import TypeAdapter, model_validator

from .common import ContractModel, NonBlankStr, Timestamp, utc
from .confirmation import ConfirmationResult
from .rules import MaterialField
from .scheduling import ScheduledSlot

ErrorCode = Literal[
    "INVALID_REQUEST", "MATERIAL_NOT_FOUND", "MATERIAL_INCOMPLETE",
    "TOOL_ERROR", "IDEMPOTENCY_CONFLICT", "DRAFT_SAVE_FAILED",
]


class ErrorDetail(ContractModel):
    code: ErrorCode
    message: NonBlankStr


class Risk(ContractModel):
    rule_id: Literal["R_AUTH_BLOCK", "R_AUTH_REVIEW", "R_TEXT_BLOCK", "R_CHANNEL"]
    level: Literal["block", "review"]
    field: NonBlankStr
    reason: NonBlankStr

    @model_validator(mode="after")
    def check_level(self) -> "Risk":
        expected = "review" if self.rule_id == "R_AUTH_REVIEW" else "block"
        if self.level != expected:
            raise ValueError("risk level does not match its rule")
        return self


class ReviewResult(ContractModel):
    status: Literal["passed", "needs_info", "needs_review", "blocked", "error"]
    missing_fields: list[MaterialField]
    risks: list[Risk]

    @model_validator(mode="after")
    def check_findings(self) -> "ReviewResult":
        blocked = any(risk.level == "block" for risk in self.risks)
        review = any(risk.level == "review" for risk in self.risks)
        if len(set(self.missing_fields)) != len(self.missing_fields):
            raise ValueError("missing_fields must be unique")
        if self.status == "passed" and (self.missing_fields or self.risks):
            raise ValueError("passed review cannot contain missing fields or risks")
        if self.status == "blocked" and not blocked:
            raise ValueError("blocked review requires a blocking risk")
        if blocked and self.status != "blocked":
            raise ValueError("blocking risk requires blocked status")
        if self.status == "needs_info" and not self.missing_fields:
            raise ValueError("needs_info requires missing_fields")
        if self.status == "needs_review" and (not review or self.missing_fields):
            raise ValueError("needs_review requires a review risk and no missing fields")
        return self


class SchedulingResult(ContractModel):
    status: Literal["not_evaluated", "available", "alternative_proposed", "no_slot"]
    requested_at: Timestamp
    suggested_at: Timestamp | None
    accepted_at: Timestamp | None
    conflicts: list[ScheduledSlot]

    @model_validator(mode="after")
    def check_times(self) -> "SchedulingResult":
        if self.status in {"not_evaluated", "no_slot"}:
            if self.suggested_at is not None or self.accepted_at is not None:
                raise ValueError("unevaluated or unavailable scheduling cannot select a time")
        if self.status == "not_evaluated" and self.conflicts:
            raise ValueError("unevaluated scheduling cannot claim conflict evidence")
        if self.status == "available":
            if self.suggested_at is None or self.accepted_at is None:
                raise ValueError("available scheduling requires suggested_at and accepted_at")
            if utc(self.suggested_at) != utc(self.accepted_at):
                raise ValueError("accepted_at must match the available suggested_at")
        if self.status == "alternative_proposed":
            if self.suggested_at is None or self.accepted_at is not None:
                raise ValueError("alternative proposal requires a suggestion and no accepted_at")
        return self


class PersistenceResult(ContractModel):
    status: Literal["not_attempted", "saved", "already_exists", "failed"]
    draft_id: NonBlankStr | None
    error: ErrorDetail | None

    @model_validator(mode="after")
    def check_commit_shape(self) -> "PersistenceResult":
        if self.status in {"saved", "already_exists"}:
            if self.draft_id is None or self.error is not None:
                raise ValueError("successful persistence requires a draft ID and no error")
        else:
            if self.draft_id is not None:
                raise ValueError("uncommitted creation cannot return a draft ID")
            if self.status == "failed":
                if self.error is None or self.error.code != "DRAFT_SAVE_FAILED":
                    raise ValueError("failed persistence requires DRAFT_SAVE_FAILED")
            elif self.error is not None:
                raise ValueError("not_attempted persistence cannot contain a save error")
        return self


class ScheduleResult(ContractModel):
    request_id: NonBlankStr
    material_id: NonBlankStr
    evaluated_at: Timestamp
    review: ReviewResult
    scheduling: SchedulingResult
    confirmation: ConfirmationResult
    persistence: PersistenceResult
    errors: list[ErrorDetail]
    publication_status: Literal["not_published"]

    @model_validator(mode="after")
    def check_states(self) -> "ScheduleResult":
        if self.confirmation.status == "confirmed":
            if (
                self.review.status != "passed" or self.scheduling.status != "available"
                or self.scheduling.accepted_at is None
                or utc(self.confirmation.selected_at) != utc(self.scheduling.accepted_at)
                or utc(self.confirmation.confirmed_at) > utc(self.evaluated_at)
            ):
                raise ValueError("confirmed selection must match a reviewed, available time")
        if self.review.status != "passed" and self.scheduling.status != "not_evaluated":
            raise ValueError("scheduling requires a passed review")
        if self.review.status == "passed" and self.scheduling.status == "not_evaluated":
            if self.persistence.status in {"saved", "already_exists"}:
                raise ValueError("passed material needs an evaluated schedule before draft saving")
        if self.review.status in {"needs_info", "blocked", "error"}:
            if self.persistence.status != "not_attempted":
                raise ValueError("incomplete, blocked or failed review cannot save a draft")
        if self.review.status in {"needs_info", "needs_review"} and self.confirmation.status != "pending":
            raise ValueError("incomplete or review-required material needs pending items")
        if self.review.status == "needs_review":
            if not any(item.kind == "risk_resolution" for item in self.confirmation.pending_items):
                raise ValueError("needs_review requires a risk_resolution item")
        if self.review.status == "needs_info":
            if not any(item.kind == "provide_metadata" for item in self.confirmation.pending_items):
                raise ValueError("needs_info requires a provide_metadata item")
        if self.scheduling.status == "alternative_proposed":
            if self.confirmation.status != "pending":
                raise ValueError("alternative proposal requires pending confirmation")
            changes = [
                item for item in self.confirmation.pending_items if item.kind == "time_change"
            ]
            if utc(self.scheduling.suggested_at) == utc(self.scheduling.requested_at):
                raise ValueError("alternative proposal must change the requested time")
            if self.confirmation.status == "pending" and not changes:
                raise ValueError("changed start requires a time_change item")
            for item in changes:
                original = TypeAdapter(Timestamp).validate_python(item.original_value)
                proposed = TypeAdapter(Timestamp).validate_python(item.proposed_value)
                if (
                    utc(original) != utc(self.scheduling.requested_at)
                    or utc(proposed) != utc(self.scheduling.suggested_at)
                ):
                    raise ValueError("confirmation item must match the original and proposed times")
        if self.scheduling.status == "no_slot":
            if self.confirmation.status != "pending":
                raise ValueError("no_slot requires pending confirmation")
            if self.confirmation.status == "pending" and not any(
                item.kind == "provide_time_window" for item in self.confirmation.pending_items
            ):
                raise ValueError("no_slot requires a pending provide_time_window item")
        if self.scheduling.suggested_at is not None:
            if utc(self.scheduling.suggested_at) <= utc(self.evaluated_at):
                raise ValueError("suggested_at must be later than evaluated_at")
        if self.persistence.status == "failed":
            if not any(error.code == "DRAFT_SAVE_FAILED" for error in self.errors):
                raise ValueError("failed save must also appear in result errors")
        if self.review.status == "error" and not self.errors:
            raise ValueError("review error requires an error detail")
        if any(error.code == "TOOL_ERROR" for error in self.errors):
            if self.persistence.status in {"saved", "already_exists"}:
                raise ValueError("a tool failure cannot produce a saved draft")
        return self
