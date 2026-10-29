"""Single-process human metadata, risk review and time-selection interrupts."""

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
from src.schemas.human_input import MetadataResponse, RiskReviewResponse
from src.schemas.material import MaterialPatch
from src.services.material_service import is_missing
from src.services.run_material_service import RunMaterialSource
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
    material: dict | None
    material_updates: dict
    patch_signature: str | None
    base_signature: str | None
    human_actions: list[dict]
    human_answer: dict
    requested_changes: str | None


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
        graph.add_node("provide_metadata", self._metadata)
        graph.add_node("apply_metadata", self._apply_metadata)
        graph.add_node("review_risk", self._review)
        graph.add_node("apply_review", self._apply_review)
        graph.add_edge(START, "analyze")
        graph.add_conditional_edges("analyze", lambda state: self._route(state, "prepare_choices"))
        graph.add_conditional_edges("prepare_choices", lambda state: self._route(state, "choose_time"))
        graph.add_edge("choose_time", "apply_choice")
        graph.add_conditional_edges("apply_choice", lambda state: self._route(state, "choose_time"))
        graph.add_edge("provide_metadata", "apply_metadata")
        graph.add_edge("apply_metadata", "analyze")
        graph.add_edge("review_risk", "apply_review")
        graph.add_conditional_edges("apply_review", lambda state:
                                    END if state["outcome"]["status"] == "rejected" else
                                    "provide_metadata" if state.get("requested_changes") else "analyze")
        self.graph = graph.compile(checkpointer=InMemorySaver())

    @staticmethod
    def _waiting(state):
        return state["outcome"]["status"] == "waiting_for_human"

    @staticmethod
    def _route(state, time_node):
        outcome = state["outcome"]
        if outcome["status"] in {"failed", "incomplete", "rejected"}:
            return END
        review = (outcome.get("result") or {}).get("review", {}).get("status")
        if review == "needs_info":
            return "provide_metadata"
        if review == "needs_review":
            return "review_risk"
        return time_node if outcome["status"] == "waiting_for_human" else END

    @staticmethod
    def _pause_for_review(outcome):
        review = (outcome.get("result") or {}).get("review", {}).get("status")
        if outcome["status"] not in {"failed", "incomplete"} and review in {"needs_info", "needs_review"}:
            outcome.update(status="waiting_for_human", decision="request_info" if review == "needs_info" else "request_confirmation")
        return outcome

    def _source(self, state):
        return RunMaterialSource(self.source, state["request"]["material_id"],
                                 state.get("material_updates"), state.get("patch_signature"), state.get("human_actions"))

    @staticmethod
    def _source_state(source, state):
        return {
            "material": source.last_material.model_dump(mode="json") if source.last_material else None,
            "base_signature": source.base_signature, "human_actions": source.human_actions,
            "material_updates": {} if source.updates_ignored else state.get("material_updates", {}),
            "patch_signature": None if source.updates_ignored else state.get("patch_signature"),
        }

    def _merge_outcome(self, previous, current):
        previous = previous or {}
        offset = previous.get("model_calls", 0)
        cycle = max((step.get("cycle", 1) for step in previous.get("model_steps", [])), default=0) + 1
        for step in current["model_steps"]:
            step["step"] += offset
            step["cycle"] = cycle
        for row in current["execution_steps"]:
            row["step"] += len(previous.get("execution_steps", []))
            if row["model_step"] is not None:
                row["model_step"] += offset
            row["cycle"] = cycle
        current["model_calls"] += offset
        for key in ("model_steps", "tool_calls", "execution_steps"):
            current[key] = previous.get(key, []) + current[key]
        current["execution_limits"] = {"max_model_calls": self.max_calls, "max_tool_calls": self.max_calls}
        return self._pause_for_review(current)

    def _analyze(self, state):
        request = ScheduleRequest.model_validate(state["request"])
        rules = RuleConfig.model_validate(state["rules"])
        remaining = self.max_calls - (state.get("outcome") or {}).get("model_calls", 0)
        if remaining <= 0:
            outcome = deepcopy(state["outcome"])
            outcome.update(status="incomplete", decision="failed", error={
                "code": "LIMIT_EXCEEDED", "message": "当前运行的模型调用预算已用尽，流程未完成，请重新提交新请求。",
            })
            return {"outcome": outcome}
        source = self._source(state)
        outcome = ReviewSchedulingAgent(self.model, source, self.store, max_calls=remaining).run(
            request, rules, pause_on_conflict=True, pause_on_review=True,
            human_context=state.get("human_actions", []),
        )
        return {"outcome": self._merge_outcome(state.get("outcome"), outcome.to_dict()),
                "candidates": [], "requested_changes": None, **self._source_state(source, state)}

    def _prepare(self, state):
        request = ScheduleRequest.model_validate(state["request"])
        rules = RuleConfig.model_validate(state["rules"])
        source = self._source(state)
        with self.store.transaction() as connection:
            report = PlanningService(
                source, SQLiteScheduleSource(connection, self.store.reference),
            ).plan(request, rules)
        report = mark_selection_pending(report)
        outcome = report_outcome(state["outcome"], report, tool="refresh_time_choices")
        candidates = report_candidates(report)
        if candidates and not report.result.errors and report.result.review.status == "passed":
            outcome.update(status="waiting_for_human", decision="request_confirmation")
        return {"outcome": self._pause_for_review(outcome), "candidates": candidates, **self._source_state(source, state)}

    def _choose(self, state):
        # Pure interrupt node: no model calls, SQL writes or other side effects before interrupt.
        answer = interrupt({
            "kind": "choose_time", "request_id": state["request"]["request_id"],
            "original_at": state["request"]["time_requirement"]["requested_at"],
            "message": "请选择一个可用时间；恢复时将重新检查占用，再按请求保存草稿。",
            "candidates": state["candidates"],
        })
        return {"selected_at": answer["selected_at"], "selection_interrupt_id": answer["interrupt_id"],
                "human_actions": state.get("human_actions", []) + [self._action(answer, "choose_time", status="recorded")]}

    def _apply(self, state):
        request = ScheduleRequest.model_validate(state["request"])
        rules = RuleConfig.model_validate(state["rules"])
        source = self._source(state)
        report = DraftService(self.store, source).save_selected(
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
        return {"outcome": self._pause_for_review(outcome), "candidates": candidates, **self._source_state(source, state)}

    @staticmethod
    def _action(answer, kind, *, status="submitted"):
        return {**deepcopy(answer), "kind": kind, "status": status,
                "recorded_at": datetime.now(timezone.utc).isoformat()}

    def _metadata(self, state):
        missing = (state["outcome"]["result"]["review"]["missing_fields"])
        editable = list(MaterialPatch.model_fields) if state.get("requested_changes") else list(missing)
        if "authorization_status" in editable and "authorization_evidence" not in editable:
            editable.append("authorization_evidence")
        why = {
            "title": "用于识别素材并展示审核对象。", "content": "用于检查禁用表述和内容风险。",
            "channel": "用于匹配请求渠道并查询同渠道的正式占用。",
            "authorization_status": "用于判断素材是否有权使用，未知授权须人工复核。",
            "authorization_evidence": "声明已授权时，需要提供可核验的授权凭证编号或资料。",
        }
        answer = interrupt({
            "kind": "provide_metadata", "request_id": state["request"]["request_id"],
            "material_id": state["request"]["material_id"], "missing_fields": missing,
            "editable_fields": editable,
            "requirements": [{"field": field, "why": why.get(field, "审核必须的信息。"),
                              "request": f"请提供 {field} 的非空有效值。"} for field in editable],
            "required_channel": state["request"]["channel"],
            "message": state.get("requested_changes") or "自动补查后仍缺少信息，请补充资料；系统会重新审核。",
            "changes_requested": bool(state.get("requested_changes")),
        })
        return {"human_answer": answer}

    def _apply_metadata(self, state):
        answer = state["human_answer"]
        return {"material_updates": {**state.get("material_updates", {}), **answer["fields"]},
                "patch_signature": state["base_signature"], "requested_changes": None,
                "human_actions": state.get("human_actions", []) + [self._action(answer, "provide_metadata")]}

    def _review(self, state):
        material = state.get("material") or {}
        risks = [{**risk, "related_value": material.get(risk["field"]),
                  "content_excerpt": (material.get("content") or "")[:1000]}
                 for risk in state["outcome"]["result"]["review"]["risks"]]
        answer = interrupt({
            "kind": "review_risk", "request_id": state["request"]["request_id"],
            "material_id": state["request"]["material_id"], "material": material, "risks": risks,
            "options": [{"action": "approve", "label": "通过", "requirement": "填写审核人和说明；授权存疑须有授权凭证。"},
                        {"action": "reject", "label": "驳回", "requirement": "填写审核人和驳回原因，终止本次运行。"},
                        {"action": "request_changes", "label": "要求修改", "requirement": "填写审核人和具体修改要求，等待用户补充后重新审核。"}],
            "message": "仅可处理待复核风险；明确禁止的内容、未授权或渠道冲突不能由人工确认绕过。",
        })
        return {"human_answer": answer}

    def _apply_review(self, state):
        answer = state["human_answer"]
        actions = state.get("human_actions", []) + [self._action(answer, "review_risk",
                     status="submitted" if answer["action"] == "approve" else "recorded")]
        if answer["action"] == "reject":
            outcome = deepcopy(state["outcome"])
            outcome.update(status="rejected", decision="reject")
            return {"outcome": outcome, "human_actions": actions, "requested_changes": None}
        if answer["action"] == "request_changes":
            outcome = deepcopy(state["outcome"])
            outcome.update(status="waiting_for_human", decision="request_info")
            return {"outcome": outcome, "human_actions": actions, "requested_changes": answer["note"]}
        evidence = answer.get("authorization_evidence") or (state.get("material") or {}).get("authorization_evidence")
        updates = {**state.get("material_updates", {}), "authorization_status": "authorized", "authorization_evidence": evidence}
        return {"material_updates": updates, "patch_signature": state["base_signature"],
                "human_actions": actions, "requested_changes": None}

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

    def resume(self, run_id, *, interrupt_id, selected_at=None, fields=None, action=None,
               reviewer=None, note=None, authorization_evidence=None):
        session = self._session(run_id)
        with session.lock:
            output = session.output
            if not output or output["status"] != "waiting_for_human":
                raise RunError(409, "RUN_NOT_WAITING", "该运行没有等待人工输入，不能重复恢复。")
            pending = output["interrupt"]
            if pending["interrupt_id"] != interrupt_id:
                raise RunError(409, "STALE_INTERRUPT", "该选择请求已过期，请使用最新的中断 ID 和候选列表。")
            kind = pending["kind"]
            try:
                if kind == "choose_time":
                    if fields is not None or action is not None or reviewer is not None or note is not None or authorization_evidence is not None:
                        raise RunError(422, "INVALID_RESUME_TYPE", "当前等待时间选择，不能提交资料或审核结论。")
                    selected_at = TypeAdapter(Timestamp).validate_python(selected_at)
                    if not any(utc(selected_at) == utc(TypeAdapter(Timestamp).validate_python(item["start_at"]))
                               for item in pending["candidates"]):
                        raise RunError(422, "INVALID_SELECTION", "只能选择本次中断返回的候选时间。")
                    answer = {"interrupt_id": interrupt_id, "selected_at": selected_at.isoformat()}
                elif kind == "provide_metadata":
                    if selected_at is not None or action is not None or reviewer is not None or note is not None or authorization_evidence is not None:
                        raise RunError(422, "INVALID_RESUME_TYPE", "当前等待资料补充，请通过 fields 提交。")
                    response = MetadataResponse.model_validate({"interrupt_id": interrupt_id, "fields": fields})
                    values = response.fields.model_dump(exclude_unset=True)
                    if not set(values) <= set(pending["editable_fields"]):
                        raise RunError(422, "INVALID_METADATA_FIELDS", "只能补充本次要求的字段，不能改写其他原始信息。")
                    if "channel" in values and values["channel"] != session.request["channel"]:
                        raise RunError(422, "INVALID_CHANNEL", "素材渠道须与原请求渠道一致；修改请求渠道请使用新请求 ID。")
                    if pending.get("changes_requested") and values.get("authorization_status") == "authorized":
                        raise RunError(422, "REVIEW_REQUIRED", "请提交授权凭证后由人工审核通过，不能用资料修改代替授权审核。")
                    answer = {"interrupt_id": interrupt_id, "fields": values}
                elif kind == "review_risk":
                    if selected_at is not None or fields is not None:
                        raise RunError(422, "INVALID_RESUME_TYPE", "当前等待风险审核，请提交审核动作、审核人和说明。")
                    response = RiskReviewResponse.model_validate({
                        "interrupt_id": interrupt_id, "action": action, "reviewer": reviewer,
                        "note": note, "authorization_evidence": authorization_evidence,
                    })
                    if any(risk["level"] != "review" or risk["rule_id"] != "R_AUTH_REVIEW" for risk in pending["risks"]):
                        raise RunError(422, "RISK_CANNOT_BE_OVERRIDDEN", "该风险不能通过本人工审核入口解除。")
                    evidence = response.authorization_evidence or pending.get("material", {}).get("authorization_evidence")
                    if response.action == "approve" and is_missing(evidence):
                        raise RunError(422, "EVIDENCE_REQUIRED", "授权状态存疑时，通过审核必须提供非空授权凭证。")
                    answer = response.model_dump(mode="json")
                else:
                    raise RunError(422, "INVALID_RESUME_TYPE", "不支持的人工中断类型。")
            except ValidationError as exc:
                raise RunError(422, "INVALID_HUMAN_INPUT", "人工输入格式无效、字段为空或类型不符，请按照本次中断要求补充。") from exc
            # Prevent resuming an expired business request using an old fixture clock.
            try:
                ScheduleRequest.model_validate(session.request, context={"now": datetime.now(timezone.utc)})
            except ValidationError as exc:
                raise RunError(409, "RUN_EXPIRED", "原请求的时间条件已过期，请使用新请求 ID 重新提交。") from exc
            self._invoke(session, Command(resume=answer))
            return deepcopy(session.output)

    def _invoke(self, session, value):
        config = {"configurable": {"thread_id": session.run_id}, "recursion_limit": self.max_calls * 4 + 10}
        with tracing_context(enabled=False):
            result = self.graph.invoke(value, config=config)
        pending = result.get("__interrupt__", ())
        session.output = {
            **result["outcome"], "run_id": session.run_id,
            "interrupt": {"interrupt_id": pending[0].id, **pending[0].value} if pending else None,
            "human_actions": result.get("human_actions", []),
        }
