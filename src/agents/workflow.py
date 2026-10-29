"""One model/tool loop in LangGraph, shared by the CLI and HTTP API."""

import json
from dataclasses import dataclass, field
from typing import Annotated, Protocol, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langsmith import tracing_context

from src.schemas import RuleConfig, ScheduleRequest, ScheduleResult

from .prompts import SYSTEM_PROMPT
from .toolbox import AgentTools
from .records import execution_records


class ChatModel(Protocol):
    mode: str

    def bind_tools(self, tools): ...


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    calls: int
    error: dict | None


@dataclass
class AgentOutcome:
    mode: str
    status: str
    decision: str
    result: ScheduleResult | None
    model_calls: int
    tool_calls: list[dict]
    error: dict | None
    model_steps: list[dict] = field(default_factory=list)
    execution_steps: list[dict] = field(default_factory=list)
    execution_limits: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "mode": self.mode, "status": self.status, "decision": self.decision,
            "result": self.result.model_dump(mode="json") if self.result else None,
            "model_calls": self.model_calls, "tool_calls": self.tool_calls, "error": self.error,
            "model_steps": self.model_steps,
            "execution_steps": self.execution_steps, "execution_limits": self.execution_limits,
        }


class ReviewSchedulingAgent:
    def __init__(self, model: ChatModel, source, store, *, max_calls=12):
        if max_calls < 1:
            raise ValueError("max_calls must be positive")
        self.model, self.source, self.store = model, source, store
        self.max_calls = max_calls

    def run(self, request, rules, *, evaluated_at=None, pause_on_conflict=False, pause_on_review=False, human_context=None) -> AgentOutcome:
        runtime = AgentTools(request, rules, self.source, self.store, evaluated_at,
                             pause_on_conflict=pause_on_conflict, pause_on_review=pause_on_review)
        request = ScheduleRequest.model_validate(request.model_dump(), context={"now": runtime.now()})
        rules = RuleConfig.model_validate(rules.model_dump())
        runtime.request, runtime.rules = request, rules
        log = []
        model_steps = []
        tools = runtime.registered_tools()
        model_with_tools = self.model.bind_tools(tools)

        def model_node(state):
            if state["calls"] >= self.max_calls:
                return {"error": {"code": "LIMIT_EXCEEDED", "message":
                    f"达到 {self.max_calls} 次模型调用上限，流程未完成；已执行 {len(log)} 次工具调用。"}}
            last = state["messages"][-1]
            observation = None
            if isinstance(last, ToolMessage):
                try:
                    observed_output = json.loads(last.content)
                except (TypeError, ValueError):
                    observed_output = last.content
                observation = {
                    "name": last.name, "tool_call_id": last.tool_call_id,
                    "status": last.status, "output": observed_output,
                }
            record = {"step": state["calls"] + 1, "observation": observation}
            try:
                reply = model_with_tools.invoke(state["messages"])
                if isinstance(reply, AIMessage):
                    record.update(
                        response_id=reply.id, content=reply.content, tool_calls=reply.tool_calls,
                        invalid_tool_calls=reply.invalid_tool_calls,
                        metadata={key: reply.response_metadata[key] for key in
                                  ("model_name", "model_provider", "finish_reason") if key in reply.response_metadata},
                        usage=reply.usage_metadata,
                    )
                if not isinstance(reply, AIMessage) or reply.invalid_tool_calls:
                    record["validation_error"] = "INVALID_MODEL_MESSAGE"
                    raise ValueError("model must return a valid AIMessage")
                if len(reply.tool_calls) > 1:
                    record["validation_error"] = "MULTIPLE_TOOL_CALLS"
                    raise ValueError("this workflow requires one tool call per model response")
                if any(not call.get("id") for call in reply.tool_calls):
                    record["validation_error"] = "MISSING_TOOL_CALL_ID"
                    raise ValueError("tool call IDs must be nonempty")
                record.update(
                    status="ok", response_id=reply.id, content=reply.content, tool_calls=reply.tool_calls,
                    metadata={key: reply.response_metadata[key] for key in
                              ("model_name", "model_provider", "finish_reason") if key in reply.response_metadata},
                    usage=reply.usage_metadata,
                    decision_reason=reply.tool_calls[0]["args"].get("reason", "") if reply.tool_calls else reply.content,
                )
                model_steps.append(record)
                return {"messages": [reply], "calls": state["calls"] + 1}
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                detail = f"，HTTP {status}" if isinstance(status, int) else ""
                record.update(status="error", error_type=type(exc).__name__, http_status=status)
                model_steps.append(record)
                return {
                    "calls": state["calls"] + 1,
                    "error": {"code": "MODEL_ERROR", "message": f"模型调用或响应失败（{type(exc).__name__}{detail}）。"},
                }

        def tool_error(exc: Exception) -> str:
            return json.dumps({"status": "error", "error": {
                "code": "INVALID_TOOL_CALL", "message": f"工具调用被拒绝（{type(exc).__name__}）。",
            }}, ensure_ascii=False)

        def observe_tools(state):
            # ToolNode has already executed the tool. This node only records its receipt.
            receipt = state["messages"][-1]
            call = state["messages"][-2].tool_calls[0]
            try:
                output = json.loads(receipt.content)
            except (ValueError, TypeError):
                output = {"status": "error", "error": {
                    "code": "INVALID_TOOL_CALL", "message": "工具未注册或返回格式无效。",
                }}
            log.append({"name": receipt.name, "arguments": call["args"], "output": output,
                        "tool_call_id": receipt.tool_call_id})
            return {}

        graph = StateGraph(AgentState)
        graph.add_node("model", model_node)
        graph.add_node("tools", ToolNode(tools, handle_tool_errors=tool_error))
        graph.add_node("observe_tools", observe_tools)
        graph.add_edge(START, "model")
        graph.add_conditional_edges(
            "model",
            lambda state: END if state["error"] or not state["messages"][-1].tool_calls else "tools",
        )
        graph.add_edge("tools", "observe_tools")
        graph.add_conditional_edges(
            "observe_tools", lambda state: END if state["error"] or runtime.saved is not None or runtime.waiting_for_time() or runtime.waiting_for_review() else "model",
        )
        initial = {
            "messages": [
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=json.dumps({
                    "request": request.model_dump(mode="json"), "rules": rules.model_dump(mode="json"),
                    "human_context": human_context or [],
                }, ensure_ascii=False)),
            ],
            "calls": 0, "error": None,
        }
        # Local tool logs only; never send offline test data to an external tracing service.
        with tracing_context(enabled=False):
            state = graph.compile().invoke(initial, config={"recursion_limit": self.max_calls * 3 + 2})
        result = runtime.result()
        error = state["error"]
        if error is None and not runtime.waiting_for_time() and not runtime.waiting_for_review():
            problem = runtime.completion_problem()
            if problem:
                error = {"code": "AGENT_INCOMPLETE", "message": problem}
        if error is None and result:
            error = next(
                (item.model_dump() for item in result.errors if item.code != "MATERIAL_INCOMPLETE"), None,
            )
        decision = "ready"
        if error:
            decision = "failed"
        elif result.review.status == "blocked":
            decision = "block"
        elif result.review.status == "needs_info" or result.scheduling.status == "no_slot":
            decision = "request_info"
        elif result.confirmation.status == "pending":
            decision = "request_confirmation"
        status = ("incomplete" if error and error["code"] == "LIMIT_EXCEEDED" else
                  "failed" if error else "waiting_for_human" if runtime.waiting_for_time() or runtime.waiting_for_review() else "completed")
        return AgentOutcome(
            mode=getattr(self.model, "mode", "test"),
            status=status,
            decision=decision, result=result, model_calls=state["calls"], tool_calls=log, error=error,
            model_steps=model_steps,
            execution_steps=execution_records(model_steps, log, status),
            execution_limits={"max_model_calls": self.max_calls, "max_tool_calls": self.max_calls},
        )
