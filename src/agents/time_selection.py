"""Single-process human time selection using real LangGraph interrupts."""

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import TypedDict
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from langsmith import tracing_context
from pydantic import TypeAdapter, ValidationError

from src.schemas import RuleConfig, ScheduleRequest
from src.schemas.common import Timestamp, utc
from src.services.draft_service import DraftService
from src.services.planning_service import PlanningService, mark_selection_pending
from src.services.storage import SQLiteScheduleSource, request_key

from .workflow import ReviewSchedulingAgent


class RunError(Exception):
    def __init__(self, status_code, code, message):
        self.status_code = status_code
        self.code = code
        super().__init__(message)


class SelectionState(TypedDict, total=False):
    request: dict
    rules: dict
    outcome: dict
    candidates: list[dict]
    selected_at: str
    selection_interrupt_id: str


@dataclass
class RunSession:
    run_id: str
    canonical: str
    request: dict
    lock: Lock = field(default_factory=Lock)
    output: dict | None = None


def report_outcome(previous, report, *, tool, arguments=None):
    result = report.result
    error = next((item.model_dump() for item in result.errors if item.code != "MATERIAL_INCOMPLETE"), None)
    decision = (
        "failed" if error else "block" if result.review.status == "blocked" else
        "request_info" if result.review.status == "needs_info" or result.scheduling.status == "no_slot" else
        "request_confirmation" if result.confirmation.status == "pending" else "ready"
    )
    return {
        **previous, "status": "failed" if error else "completed", "decision": decision,
        "result": result.model_dump(mode="json"), "error": error,
        "tool_calls": previous["tool_calls"] + [{
            "name": tool, "arguments": arguments or {}, "output": report.model_dump(mode="json"),
        }],
    }


def report_candidates(report):
    found = next((item.candidate_slots for item in reversed(report.actions)
                  if item.candidate_slots is not None), None)
    return found.model_dump(mode="json")["candidates"] if found is not None else []


class SchedulingRuns:
    """One shared graph/checkpointer per server; per-run locks prevent duplicate resumes.

    Pending runs are in memory. Restarting the process clears them, while SQLite
    drafts and occupancy remain intact. The CLI retains its fixture replay mode.
    """

    def __init__(self, model, source, store, rules, *, max_calls=12):
        self.model, self.source, self.store, self.rules = model, source, store, rules
        self.max_calls = max_calls
        self._registry_lock = Lock()
        self._runs: dict[str, RunSession] = {}
        self._latest: dict[str, RunSession] = {}
        graph = StateGraph(SelectionState)
        graph.add_node("analyze", self._analyze)
        graph.add_node("prepare_choices", self._prepare)
        graph.add_node("choose_time", self._choose)
        graph.add_node("apply_choice", self._apply)
        graph.add_edge(START, "analyze")
        graph.add_conditional_edges("analyze", lambda state:
                                    "prepare_choices" if self._waiting(state) else END)
        graph.add_conditional_edges("prepare_choices", lambda state:
                                    "choose_time" if self._waiting(state) else END)
        graph.add_edge("choose_time", "apply_choice")
        graph.add_conditional_edges("apply_choice", lambda state:
                                    "choose_time" if self._waiting(state) else END)
        self.graph = graph.compile(checkpointer=InMemorySaver())

    @staticmethod
    def _waiting(state):
        return state["outcome"]["status"] == "waiting_for_human"

    def _analyze(self, state):
        request = ScheduleRequest.model_validate(state["request"])
        rules = RuleConfig.model_validate(state["rules"])
        outcome = ReviewSchedulingAgent(self.model, self.source, self.store, max_calls=self.max_calls).run(
            request, rules, pause_on_conflict=True,
        )
        return {"outcome": outcome.to_dict(), "candidates": []}

    def _prepare(self, state):
        request = ScheduleRequest.model_validate(state["request"])
        rules = RuleConfig.model_validate(state["rules"])
        with self.store.transaction() as connection:
            report = PlanningService(
                self.source, SQLiteScheduleSource(connection, self.store.reference),
            ).plan(request, rules)
        report = mark_selection_pending(report)
        outcome = report_outcome(state["outcome"], report, tool="refresh_time_choices")
        candidates = report_candidates(report)
        if candidates and not report.result.errors and report.result.review.status == "passed":
            outcome.update(status="waiting_for_human", decision="request_confirmation")
        return {"outcome": outcome, "candidates": candidates}

    def _choose(self, state):
        # Pure interrupt node: no model calls, SQL writes or other side effects before interrupt.
        answer = interrupt({
            "kind": "choose_time", "request_id": state["request"]["request_id"],
            "original_at": state["request"]["time_requirement"]["requested_at"],
            "message": "请选择一个可用时间；恢复时将重新检查占用，再按请求保存草稿。",
            "candidates": state["candidates"],
        })
        return {"selected_at": answer["selected_at"], "selection_interrupt_id": answer["interrupt_id"]}

    def _apply(self, state):
        request = ScheduleRequest.model_validate(state["request"])
        rules = RuleConfig.model_validate(state["rules"])
        report = DraftService(self.store, self.source).save_selected(
            request, rules, selected_at=TypeAdapter(Timestamp).validate_python(state["selected_at"]),
            interrupt_id=state["selection_interrupt_id"],
        )
        outcome = report_outcome(state["outcome"], report, tool="apply_time_selection", arguments={
            "selected_at": state["selected_at"], "interrupt_id": state["selection_interrupt_id"],
        })
        candidates = report_candidates(report)
        if (report.result.confirmation.status != "confirmed" and candidates
                and not report.result.errors and report.result.review.status == "passed"):
            outcome.update(status="waiting_for_human", decision="request_confirmation")
        return {"outcome": outcome, "candidates": candidates}

    def start(self, request: ScheduleRequest):
        canonical = request_key(request)
        stored = self.store.get(request.request_id)
        if stored is not None and stored["request_json"] != canonical:
            raise RunError(409, "IDEMPOTENCY_CONFLICT", "相同 request_id 对应不同输入，请使用新的请求 ID。")
        with self._registry_lock:
            previous = self._latest.get(request.request_id)
            if previous is not None and previous.canonical != canonical:
                raise RunError(409, "IDEMPOTENCY_CONFLICT", "相同 request_id 对应不同输入，请使用新的请求 ID。")
            if previous is not None and (previous.output is None or previous.output["status"] == "waiting_for_human"):
                session = previous
            else:
                session = RunSession("RUN-" + uuid4().hex, canonical, request.model_dump(mode="json"))
                self._runs[session.run_id] = session
                self._latest[request.request_id] = session
        with session.lock:
            if session.output is None:
                self._invoke(session, {"request": session.request, "rules": self.rules.model_dump(mode="json")})
            return deepcopy(session.output)

    def _session(self, run_id):
        with self._registry_lock:
            session = self._runs.get(run_id)
        if session is None:
            raise RunError(404, "RUN_NOT_FOUND", "运行不存在或服务已重启，请重新提交业务请求。")
        return session

    def get(self, run_id):
        session = self._session(run_id)
        with session.lock:
            return deepcopy(session.output)

    def resume(self, run_id, *, interrupt_id, selected_at):
        session = self._session(run_id)
        with session.lock:
            output = session.output
            if not output or output["status"] != "waiting_for_human":
                raise RunError(409, "RUN_NOT_WAITING", "该运行没有等待时间选择，不能重复恢复。")
            pending = output["interrupt"]
            if pending["interrupt_id"] != interrupt_id:
                raise RunError(409, "STALE_INTERRUPT", "该选择请求已过期，请使用最新的中断 ID 和候选列表。")
            selected_at = TypeAdapter(Timestamp).validate_python(selected_at)
            if not any(utc(selected_at) == utc(TypeAdapter(Timestamp).validate_python(item["start_at"]))
                       for item in pending["candidates"]):
                raise RunError(422, "INVALID_SELECTION", "只能选择本次中断返回的候选时间。")
            # Prevent resuming an expired business request using an old fixture clock.
            try:
                ScheduleRequest.model_validate(session.request, context={"now": datetime.now(timezone.utc)})
            except ValidationError as exc:
                raise RunError(409, "RUN_EXPIRED", "原请求的时间条件已过期，请使用新请求 ID 重新提交。") from exc
            self._invoke(session, Command(resume={
                "selected_at": selected_at.isoformat(), "interrupt_id": interrupt_id,
            }))
            return deepcopy(session.output)

    def _invoke(self, session, value):
        config = {"configurable": {"thread_id": session.run_id}, "recursion_limit": 20}
        with tracing_context(enabled=False):
            result = self.graph.invoke(value, config=config)
        pending = result.get("__interrupt__", ())
        session.output = {
            **result["outcome"], "run_id": session.run_id,
            "interrupt": {"interrupt_id": pending[0].id, **pending[0].value} if pending else None,
        }
