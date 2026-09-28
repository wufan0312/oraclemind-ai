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
from src.agent.assessment_agent import stream_assessment_agent
from src.agent.scale_report_agent import stream_scale_report_agent
from src.harness import route_trace

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

    return EventSourceResponse(route_trace(event_generator(), meta={"agent": "ming", "kind": "agent"}))


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

    return EventSourceResponse(route_trace(event_generator(), meta={"agent": "home", "kind": "agent"}))
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


# ============================ 复原力测评 Agent ============================
class AssessmentRequest(BaseModel):
    """复原力测评请求：只传问卷答案（键值均为题目 key / 用户自评文本）。

    题干与 system prompt 由服务端持有（见 src/agent/assessment_agent.py），
    客户端不可注入任意 prompt；答案长度截断由服务端兜底。
    """

    answers: dict[str, str] = Field(default_factory=dict, max_length=40)
    requestId: str | None = None


@router.post("/agent/assessment/stream")
async def post_agent_assessment_stream(req: AssessmentRequest):
    """复原力测评 Agent（SSE 流式）

    事件流：open → delta*(LLM token) → meta(disclaimer) → done / error
    """
    async def event_generator():
        async for evt in stream_assessment_agent(req.answers):
            yield {
                "event": evt["event"],
                "data": json.dumps(evt.get("data", {}), ensure_ascii=False),
            }

    return EventSourceResponse(route_trace(event_generator(), meta={"agent": "assessment", "kind": "agent"}))


# ============================ 量表 AI 心理报告 Agent ============================
class ScaleReportRequest(BaseModel):
    """量表 AI 心理报告请求：只传计分结果与服务端摘要（维度分 / 等级 / interpretation 均为权威值）。

    system prompt 由服务端持有（见 src/agent/scale_report_agent.py），客户端不可注入任意 prompt；
    字段长度由服务端兜底截断。
    """

    slug: str = Field(max_length=64)
    title: str = Field(max_length=128)
    dimensions: list[dict] = Field(default_factory=list, max_items=20)
    summary: str = Field(default="", max_length=2000)
    requestId: str | None = None


@router.post("/agent/scale-report/stream")
async def post_agent_scale_report_stream(req: ScaleReportRequest):
    """量表 AI 心理报告 Agent（SSE 流式）

    事件流：open → delta*(LLM token) → meta(disclaimer) → done / error
    """
    async def event_generator():
        async for evt in stream_scale_report_agent(req.model_dump(exclude_none=True)):
            yield {
                "event": evt["event"],
                "data": json.dumps(evt.get("data", {}), ensure_ascii=False),
            }

    return EventSourceResponse(route_trace(event_generator(), meta={"agent": "scale-report", "kind": "agent"}))


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

    return EventSourceResponse(route_trace(event_generator(), meta={"agent": "report", "kind": "report", "question": req.question[:200]}))
