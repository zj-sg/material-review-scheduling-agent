"""Pending items and confirmation status for a scheduling suggestion."""

from typing import Literal

from pydantic import JsonValue, TypeAdapter, model_validator

from .common import ContractModel, NonBlankStr, Timestamp, utc


class PendingItem(ContractModel):
    kind: Literal[
        "time_change", "risk_resolution", "provide_metadata", "provide_time_window",
    ]
    field: NonBlankStr
    original_value: JsonValue
    proposed_value: JsonValue
    message: NonBlankStr

    @model_validator(mode="after")
    def check_time_change(self) -> "PendingItem":
        if self.kind == "time_change":
            original = TypeAdapter(Timestamp).validate_python(self.original_value)
            proposed = TypeAdapter(Timestamp).validate_python(self.proposed_value)
            if utc(original) == utc(proposed):
                raise ValueError("time_change must propose a different instant")
        return self


class ConfirmationResult(ContractModel):
    status: Literal["not_required", "pending", "confirmed"]
    pending_items: list[PendingItem]
    selected_at: Timestamp | None = None
    confirmed_at: Timestamp | None = None
    interrupt_id: NonBlankStr | None = None

    @model_validator(mode="after")
    def check_pending_items(self) -> "ConfirmationResult":
        if self.status == "pending" and not self.pending_items:
            raise ValueError("pending confirmation requires pending_items")
        if self.status != "pending" and self.pending_items:
            raise ValueError("unnecessary confirmation cannot have pending_items")
        values = (self.selected_at, self.confirmed_at, self.interrupt_id)
        if self.status == "confirmed":
            if any(value is None for value in values):
                raise ValueError("confirmed selection requires selected_at, confirmed_at and interrupt_id")
            if utc(self.selected_at) <= utc(self.confirmed_at):
                raise ValueError("confirmed time must be in the future")
        elif any(value is not None for value in values):
            raise ValueError("only confirmed selections may contain confirmation details")
        return self
