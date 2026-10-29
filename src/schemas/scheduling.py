"""Time intervals, occupancy snapshots and computed candidates."""

from typing import Literal

from pydantic import StrictBool, model_validator

from .common import ContractModel, NonBlankStr, Timestamp, utc


class TimeWindow(ContractModel):
    start: Timestamp
    end: Timestamp

    @model_validator(mode="after")
    def check_order(self) -> "TimeWindow":
        if utc(self.start) >= utc(self.end):
            raise ValueError("window end must be later than start")
        return self


class ScheduledSlot(ContractModel):
    schedule_id: NonBlankStr
    channel: NonBlankStr
    start_at: Timestamp
    end_at: Timestamp

    @model_validator(mode="after")
    def check_order(self) -> "ScheduledSlot":
        if utc(self.start_at) >= utc(self.end_at):
            raise ValueError("slot end_at must be later than start_at")
        return self


class TimeRequirement(ContractModel):
    mode: Literal["fixed", "window"]
    requested_at: Timestamp
    window_start: Timestamp | None = None
    window_end: Timestamp | None = None

    @model_validator(mode="after")
    def check_mode(self) -> "TimeRequirement":
        if self.mode == "fixed":
            if self.window_start is not None or self.window_end is not None:
                raise ValueError("fixed mode cannot include an allowed window")
        else:
            if self.window_start is None or self.window_end is None:
                raise ValueError("window mode requires window_start and window_end")
            if utc(self.window_start) >= utc(self.window_end):
                raise ValueError("window_end must be later than window_start")
            if not utc(self.window_start) <= utc(self.requested_at) < utc(self.window_end):
                raise ValueError("preferred requested_at must be inside the allowed window [start, end)")
        return self


class ScheduleSnapshot(ContractModel):
    channel: NonBlankStr
    window: TimeWindow
    queried_at: Timestamp
    existing_schedule: list[ScheduledSlot]

    @model_validator(mode="after")
    def check_scope(self) -> "ScheduleSnapshot":
        ids = [slot.schedule_id for slot in self.existing_schedule]
        if len(ids) != len(set(ids)):
            raise ValueError("snapshot schedule IDs must be unique")
        for slot in self.existing_schedule:
            if slot.channel != self.channel:
                raise ValueError("snapshot contains occupancy from a different channel")
            if not (utc(slot.start_at) < utc(self.window.end) and utc(self.window.start) < utc(slot.end_at)):
                raise ValueError("snapshot contains occupancy outside its query window")
        return self


class SlotCandidate(ContractModel):
    start_at: Timestamp
    end_at: Timestamp
    requires_confirmation: StrictBool
    reason: NonBlankStr

    @model_validator(mode="after")
    def check_interval(self) -> "SlotCandidate":
        if utc(self.start_at) >= utc(self.end_at):
            raise ValueError("candidate end_at must be later than start_at")
        return self


class CandidateSlots(ContractModel):
    candidates: list[SlotCandidate]

    @model_validator(mode="after")
    def unique_instants(self) -> "CandidateSlots":
        starts = [utc(candidate.start_at) for candidate in self.candidates]
        if len(starts) != len(set(starts)):
            raise ValueError("candidate starts must represent unique instants")
        return self
