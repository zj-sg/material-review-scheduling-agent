"""Read formal occupancy and compute candidates using absolute time comparisons."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from pydantic import model_validator

from src.schemas.common import BUSINESS_TIMEZONE, ContractModel, utc
from src.schemas.request import ScheduleRequest
from src.schemas.rules import RuleConfig
from src.schemas.scheduling import (
    CandidateSlots, ScheduleSnapshot, ScheduledSlot, SlotCandidate, TimeWindow,
)


def overlaps(start: datetime, end: datetime, slot: ScheduledSlot) -> bool:
    return utc(start) < utc(slot.end_at) and utc(slot.start_at) < utc(end)


def contains(window: TimeWindow, start: datetime, end: datetime) -> bool:
    return utc(window.start) <= utc(start) and utc(end) <= utc(window.end)


def end_after(start: datetime, minutes: int) -> datetime:
    return (utc(start) + timedelta(minutes=minutes)).astimezone(start.tzinfo)


class ScheduleCatalog(ContractModel):
    existing_schedule: list[ScheduledSlot]

    @model_validator(mode="after")
    def unique_ids(self) -> "ScheduleCatalog":
        ids = [slot.schedule_id for slot in self.existing_schedule]
        if len(set(ids)) != len(ids):
            raise ValueError("schedule_id values must be unique")
        return self


class ScheduleSource(Protocol):
    @property
    def reference(self) -> str: ...

    def query(self, channel: str, window: TimeWindow) -> list[ScheduledSlot]: ...


class JsonScheduleService:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()

    @property
    def reference(self) -> str:
        return f"local-json:{self.path.as_posix()}#existing_schedule"

    def read_all(self) -> list[ScheduledSlot]:
        raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("schedule source must be a JSON object")
        state = raw.get("initial_state", raw)
        if not isinstance(state, dict):
            raise ValueError("initial_state must be an object")
        # Only formal occupancy is used; draft entries never reserve a slot.
        catalog = ScheduleCatalog.model_validate({"existing_schedule": state.get("existing_schedule")})
        return catalog.existing_schedule

    def query(self, channel: str, window: TimeWindow) -> list[ScheduledSlot]:
        selected = [
            slot for slot in self.read_all()
            if slot.channel == channel and overlaps(window.start, window.end, slot)
        ]
        return sorted(selected, key=lambda slot: (utc(slot.start_at), utc(slot.end_at), slot.schedule_id))


class SlotSearchService:
    def find(
        self, request: ScheduleRequest, rules: RuleConfig, snapshot: ScheduleSnapshot,
    ) -> CandidateSlots:
        if (
            snapshot.channel != request.channel
            or utc(snapshot.window.start) != utc(request.search_window.start)
            or utc(snapshot.window.end) != utc(request.search_window.end)
        ):
            raise ValueError("snapshot does not match the requested channel and range")
        if request.channel not in rules.allowed_channels:
            raise ValueError("cannot schedule an unconfigured channel")
        preferred = request.time_requirement.requested_at.astimezone(BUSINESS_TIMEZONE)
        original = self._candidate(preferred, request, rules, snapshot)
        if original is not None:
            return CandidateSlots(candidates=[original])

        start = request.search_window.start.astimezone(BUSINESS_TIMEZONE)
        midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
        step = timedelta(minutes=rules.candidate_step_minutes)
        cursor = midnight + ((start - midnight) // step) * step
        if cursor < start:
            cursor += step
        candidates = []
        while utc(end_after(cursor, rules.slot_duration_minutes)) <= utc(request.search_window.end):
            candidate = self._candidate(cursor, request, rules, snapshot)
            if candidate is not None:
                candidates.append(candidate)
            cursor += step
        candidates.sort(key=lambda item: (
            abs(utc(item.start_at) - utc(preferred)),
            utc(preferred) - utc(item.start_at),
        ))
        return CandidateSlots(candidates=candidates)

    @staticmethod
    def validate_candidate(
        candidate: SlotCandidate, request: ScheduleRequest,
        rules: RuleConfig, snapshot: ScheduleSnapshot,
    ) -> None:
        expected = SlotSearchService._candidate(candidate.start_at, request, rules, snapshot)
        if expected is None or (
            utc(candidate.end_at) != utc(expected.end_at)
            or candidate.requires_confirmation != expected.requires_confirmation
        ):
            raise ValueError("candidate violates occupancy, duration or confirmation constraints")
        if utc(candidate.start_at) != utc(request.time_requirement.requested_at):
            start = request.search_window.start.astimezone(BUSINESS_TIMEZONE)
            midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
            if (utc(candidate.start_at) - utc(midnight)) % timedelta(minutes=rules.candidate_step_minutes):
                raise ValueError("candidate does not follow the configured step")

    @staticmethod
    def _candidate(
        start: datetime, request: ScheduleRequest, rules: RuleConfig, snapshot: ScheduleSnapshot,
    ) -> SlotCandidate | None:
        end = end_after(start, rules.slot_duration_minutes)
        if utc(start) <= utc(snapshot.queried_at) or not contains(request.search_window, start, end):
            return None
        if any(overlaps(start, end, slot) for slot in snapshot.existing_schedule):
            return None
        requirement = request.time_requirement
        if requirement.mode == "window":
            allowed = TimeWindow(start=requirement.window_start, end=requirement.window_end)
            if not contains(allowed, start, end):
                return None
        needs_confirmation = (
            requirement.mode == "fixed" and utc(start) != utc(requirement.requested_at)
        )
        return SlotCandidate(
            start_at=start, end_at=end, requires_confirmation=needs_confirmation,
            reason=(
                "候选改变了硬性指定时间，需要用户确认。"
                if needs_confirmation else "时段符合原时间要求，且查询时无占用。"
            ),
        )
