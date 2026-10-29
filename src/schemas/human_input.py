"""Typed human responses; business constraints remain enforced on resume."""

from typing import Literal

from pydantic import Field, model_validator

from .common import ContractModel, NonBlankStr
from .material import MaterialPatch


class MetadataResponse(ContractModel):
    interrupt_id: NonBlankStr
    fields: MaterialPatch

    @model_validator(mode="after")
    def nonempty_values(self):
        values = self.fields.model_dump(exclude_unset=True)
        if not values or any(value is None or (isinstance(value, str) and not value.strip()) for value in values.values()):
            raise ValueError("fields must contain nonempty values for at least one requested field")
        return self


class RiskReviewResponse(ContractModel):
    interrupt_id: NonBlankStr
    action: Literal["approve", "reject", "request_changes"]
    reviewer: NonBlankStr = Field(max_length=100)
    note: NonBlankStr = Field(max_length=1000)
    authorization_evidence: NonBlankStr | None = Field(default=None, max_length=1000)
