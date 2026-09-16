"""Agent 路由（起名 ReAct Agent + 综合报告 Agent）"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from src.agent.ming_agent import run_ming_agent, stream_ming_agent
from src.agent.report_agent import run_report_agent
from src.agent.home_agent import run_home_agent, stream_home_agent

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["agent"])


class MingRequest(BaseModel):
    message: str = Field(max_length=5000)
    birthHint: dict | None = None
    requestId: str | None = None


@router.post("/agent/ming")
async def post_agent_ming(req: MingRequest):
    """起名 Agent（非流式）"""
    text, degraded, reason = await run_ming_agent(req.message, req.birthHint)
    return {
        "text": text,
        "degraded": degraded,
        "degradedReason": reason,
    }


@router.post("/agent/ming/stream")
async def post_agent_ming_stream(req: MingRequest):
    """起名 Agent（SSE 流式）"""
    async def event_generator():
        async for evt in stream_ming_agent(req.message, req.birthHint):
            yield {
                "event": evt["event"],
                "data": json.dumps(evt.get("data", {}), ensure_ascii=False),
            }

    return EventSourceResponse(event_generator())


# ============================ 首页通用命理助手 Agent ============================
class HomeRequest(BaseModel):
    message: str = Field(max_length=5000)
    history: list[dict] | None = Field(default=None, max_items=20)
    birthHint: dict | None = None
    requestId: str | None = None


@router.post("/agent/home")
async def post_agent_home(req: HomeRequest):
    """首页通用命理助手（非流式）"""
    text, degraded, reason = await run_home_agent(req.message, req.history, req.birthHint)
    return {
        "text": text,
        "degraded": degraded,
        "degradedReason": reason,
    }


@router.post("/agent/home/stream")
async def post_agent_home_stream(req: HomeRequest):
    """首页通用命理助手（SSE 流式）

    事件流：open → tool_start/tool_end*(可选) → delta*(LLM token)
            → meta(disclaimer) → done / error
    """
    async def event_generator():
        async for evt in stream_home_agent(req.message, req.history, req.birthHint):
            yield {
                "event": evt["event"],
                "data": json.dumps(evt.get("data", {}), ensure_ascii=False),
            }

    return EventSourceResponse(event_generator())
# ============================ 综合报告 Agent ============================
class ReportAgentBirthInfo(BaseModel):
    year: int
    month: int
    day: int
    hour: int | None = None
    timeText: str | None = None
    gender: str | None = None
    # 农历出生年/月/日（可选）：生命灵数统一农历口径时由前端传入，数字命理模块排盘使用
    lunarYear: int | None = None
    lunarMonth: int | None = None
    lunarDay: int | None = None


class ReportAgentRequest(BaseModel):
    question: str = Field(max_length=5000)
    birthInfo: ReportAgentBirthInfo
    requestId: str | None = None
    # 跨页测算结论（塔罗/星座/数字命理共享池），与排盘同权参与融合
    crossReadings: list[dict] | None = Field(default=None, max_items=20)
    # 换个说法：>0 时要求 writer 换全新切入点（结论须与综合分析一致）
    variant: int = 0


@router.post("/agent/report/stream")
async def post_agent_report_stream(req: ReportAgentRequest):
    """综合报告 Agent（SSE 流式）

    事件流：open → phase*(analyzer/paipan/synthesizer/reviewer/writer)
            → delta*(LLM 思考 token) → meta(report) → done / error
    """
    birth = req.birthInfo.model_dump()

    async def event_generator():
        async for evt in run_report_agent(req.question, birth, req.crossReadings, req.variant):
            yield {
                "event": evt["event"],
                "data": json.dumps(evt.get("data", {}), ensure_ascii=False),
            }

    return EventSourceResponse(event_generator())
