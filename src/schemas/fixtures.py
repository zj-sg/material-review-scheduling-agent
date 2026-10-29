"""Local acceptance fixtures. These are not public request models."""

from typing import Literal

from pydantic import Field, ValidationInfo, ValidatorFunctionWrapHandler, field_validator, model_validator

from .common import ContractModel, NonBlankStr, NonNegativeInt, Timestamp
from .material import MaterialPatch, MaterialRecord
from .request import ScheduleRequest
from .result import ScheduleResult
from .rules import RuleConfig
from .scheduling import ScheduledSlot


class InitialState(ContractModel):
    materials: list[MaterialRecord]
    supplemental_materials: dict[NonBlankStr, MaterialPatch]
    existing_schedule: list[ScheduledSlot]

    @model_validator(mode="after")
    def check_identifiers(self) -> "InitialState":
        for values, key in (
            (self.materials, "material_id"),
            (self.existing_schedule, "schedule_id"),
        ):
            identifiers = [getattr(item, key) for item in values]
            if len(set(identifiers)) != len(identifiers):
                raise ValueError(f"{key} values in initial_state must be unique")
        return self


class ScenarioStep(ContractModel):
    evaluated_at: Timestamp
    request: ScheduleRequest
    save_behavior: Literal["success", "fail"]

    @field_validator("request", mode="wrap")
    @classmethod
    def parse_at_fixture_time(
        cls, value: object, handler: ValidatorFunctionWrapHandler, info: ValidationInfo,
    ) -> ScheduleRequest:
        now = info.data.get("evaluated_at")
        if now is None:
            raise ValueError("a valid evaluated_at is required before validating the request")
        # Validate once with the recorded clock, rather than again with wall time.
        return ScheduleRequest.model_validate(value, context={"now": now})


class ScenarioInput(ContractModel):
    schema_version: Literal["1.0"]
    scenario_id: NonBlankStr
    rules: RuleConfig
    initial_state: InitialState
    steps: list[ScenarioStep] = Field(min_length=1)


class FinalStateExpectation(ContractModel):
    draft_count: NonNegativeInt


class ScenarioExpectation(ContractModel):
    scenario_id: NonBlankStr
    results: list[ScheduleResult] = Field(min_length=1)
    final_state: FinalStateExpectation
    required_observations: list[NonBlankStr] = Field(min_length=1)
