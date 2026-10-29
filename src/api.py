"""HTTP adapter for the existing single-agent workflow."""

import json
import sqlite3
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from .agents.model import build_model
from .agents.offline import OfflineTestModel
from .agents.time_selection import RunError, SchedulingRuns
from .config import Settings, load_settings
from .schemas import RuleConfig, ScheduleRequest, ScheduleResult
from .schemas.common import ContractModel, NonBlankStr, Timestamp
from .schemas.human_input import MetadataResponse, RiskReviewResponse
from .services.material_service import JsonMaterialService, MaterialCatalog
from .services.schedule_service import ScheduleCatalog
from .services.storage import SQLiteStore


class APIInitialState(MaterialCatalog, ScheduleCatalog):
    pass


class APIData(ContractModel):
    """Trusted server-side data; never accepted in an HTTP scheduling request."""

    rules: RuleConfig
    initial_state: APIInitialState


class AgentResponse(BaseModel):
    mode: str
    status: Literal["completed", "failed", "waiting_for_human", "incomplete", "rejected"]
    decision: str
    result: ScheduleResult | None
    model_calls: int
    tool_calls: list[dict]
    model_steps: list[dict]
    execution_steps: list[dict]
    execution_limits: dict
    error: dict | None
    run_id: str
    interrupt: dict | None
    human_actions: list[dict]


class TimeSelectionRequest(ContractModel):
    interrupt_id: NonBlankStr
    selected_at: Timestamp


def outcome_response(body):
    if body["status"] == "incomplete":
        return JSONResponse(status_code=409, content=body)
    if body["status"] != "failed":
        return body
    code = (body.get("error") or {}).get("code")
    status = {
        "MATERIAL_NOT_FOUND": 404, "IDEMPOTENCY_CONFLICT": 409,
        "MODEL_ERROR": 502, "AGENT_INCOMPLETE": 502, "LIMIT_EXCEEDED": 502,
    }.get(code, 500)
    return JSONResponse(status_code=status, content=body)


REQUEST_EXAMPLE = {
    "request_id": "REQ-HTTP-001", "material_id": "M01", "channel": "wechat",
    "time_requirement": {"mode": "fixed", "requested_at": "2026-11-02T18:00:00+08:00"},
    "search_window": {"start": "2026-11-02T18:00:00+08:00", "end": "2026-11-02T21:00:00+08:00"},
    "save_draft": True,
}


def create_app(*, settings: Settings | None = None, model_factory=None, store=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings if settings is not None else load_settings()
        data = APIData.model_validate_json(config.api_data_path.read_text(encoding="utf-8-sig"))
        model = (model_factory(config) if model_factory is not None else
                 OfflineTestModel() if config.api_offline else build_model(config))
        try:
            active_store = store if store is not None else SQLiteStore(config.database_path)
            with active_store.transaction(initial_schedule=data.initial_state.existing_schedule):
                pass
            app.state.model = model
            app.state.store = active_store
            app.state.rules = data.rules
            app.state.source = JsonMaterialService(config.api_data_path)
            app.state.runs = SchedulingRuns(
                model, app.state.source, active_store, data.rules, max_calls=config.max_model_calls,
            )
            yield
        finally:
            if hasattr(model, "close"):
                model.close()

    app = FastAPI(
        title="素材审核与发布排期 Agent", version="1.3.0", lifespan=lifespan,
        description="支持资料补充、风险人工审核和时间选择三类中断；恢复后重新校验，只保存草稿。",
    )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Do not echo arbitrary input (including accidental credentials) in validation errors.
        return JSONResponse(status_code=422, content={
            "error": {"code": "INVALID_REQUEST", "message": "请求数据校验失败。"},
            "details": [{"loc": list(item["loc"]), "type": item["type"], "message": item["msg"]}
                        for item in exc.errors()],
        })

    @app.exception_handler(RunError)
    async def invalid_run(request: Request, exc: RunError):
        return JSONResponse(status_code=exc.status_code, content={
            "error": {"code": exc.code, "message": str(exc)},
        })

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        return JSONResponse(status_code=500, content={
            "error": {"code": "INTERNAL_ERROR", "message": "服务执行失败，请检查服务端日志。"},
        })

    @app.get("/health", summary="检查服务状态")
    def health(request: Request):
        return {"status": "ok", "mode": request.app.state.model.mode}

    @app.post(
        "/schedule", response_model=AgentResponse, summary="审核排期；缺失资料、待复核风险或时间冲突时暂停",
        responses={404: {"description": "素材不存在"}, 409: {"description": "请求 ID 内容冲突"},
                   422: {"description": "请求校验失败"}, 500: {"description": "工具或保存失败"},
                   502: {"description": "模型调用或决策失败"}},
        openapi_extra={"requestBody": {"content": {"application/json": {"example": REQUEST_EXAMPLE}}}},
    )
    def schedule(payload: ScheduleRequest, request: Request):
        state = request.app.state
        try:
            outcome = state.runs.start(payload)
        except ValidationError as exc:
            # The live clock is rechecked by the workflow after HTTP body validation.
            raise RequestValidationError(exc.errors(include_input=False)) from exc
        return outcome_response(outcome)

    @app.get("/runs/{run_id}", response_model=AgentResponse, summary="查看运行状态和当前人工待办")
    def get_run(run_id: str, request: Request):
        return request.app.state.runs.get(run_id)

    @app.post(
        "/runs/{run_id}/resume", response_model=AgentResponse,
        summary="提交时间选择、缺失资料或人工风险审核，继续当前运行",
        responses={404: {"description": "运行不存在或服务已重启"},
                   409: {"description": "中断已失效、运行已完成或原请求时间过期"},
                   422: {"description": "时间、补充资料或审核结论不符合当前中断要求"},
                   500: {"description": "保存或工具失败"}},
        openapi_extra={"requestBody": {"content": {"application/json": {"examples": {
            "time": {"summary": "选择时间", "value": {"interrupt_id": "复制实际中断 ID", "selected_at": "2026-11-02T20:00:00+08:00"}},
            "metadata": {"summary": "补充资料", "value": {"interrupt_id": "复制实际中断 ID", "fields": {"authorization_status": "authorized", "authorization_evidence": "AUTH-M02-VERIFIED"}}},
            "review": {"summary": "人工审核通过", "value": {"interrupt_id": "复制实际中断 ID", "action": "approve", "reviewer": "reviewer-demo", "note": "已核对演示授权资料", "authorization_evidence": "AUTH-M04-VERIFIED"}},
        }}}}},
    )
    def resume(run_id: str, payload: TimeSelectionRequest | MetadataResponse | RiskReviewResponse, request: Request):
        return outcome_response(request.app.state.runs.resume(
            run_id, **payload.model_dump(mode="python", exclude_none=True, exclude_unset=True),
        ))

    @app.get("/drafts/{request_id}", response_model=ScheduleResult, summary="按请求 ID 查询当前已保存草稿")
    def get_draft(request_id: str, request: Request):
        try:
            row = request.app.state.store.get(request_id)
            if row is not None:
                return ScheduleResult.model_validate(json.loads(row["report_json"])["result"])
        except (OSError, sqlite3.Error, ValueError, KeyError):
            return JSONResponse(status_code=500, content={
                "error": {"code": "DRAFT_READ_FAILED", "message": "读取已保存草稿失败。"},
            })
        return JSONResponse(status_code=404, content={
            "error": {"code": "DRAFT_NOT_FOUND", "message": "没有找到该请求的已保存草稿。"},
        })

    return app


app = create_app()
