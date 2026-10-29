"""Business rule configuration, separate from environment settings."""

from typing import Literal

from pydantic import Field, field_validator

from .common import ContractModel, NonBlankStr, PositiveInt, TimezoneName

MaterialField = Literal[
    "material_id", "title", "content", "channel",
    "authorization_status", "authorization_evidence",
]


class RuleConfig(ContractModel):
    version: NonBlankStr
    timezone: TimezoneName
    allowed_channels: list[NonBlankStr] = Field(min_length=1)
    required_material_fields: list[MaterialField] = Field(min_length=1)
    blocked_phrases: list[NonBlankStr]
    slot_duration_minutes: PositiveInt
    candidate_step_minutes: PositiveInt

    @field_validator("allowed_channels", "required_material_fields", "blocked_phrases")
    @classmethod
    def unique_values(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("list entries must be unique")
        return value
