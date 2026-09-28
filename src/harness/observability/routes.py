"""玄镜 OracleMind · Observability 决策树追踪 —— /trace/{id} 查询端点

前端拿到 open 事件里的 traceId 后，可拉取本次请求完整的决策树：
analyzer → paipan → synthesizer(LLM) → reviewer(LLM) → writer(LLM) 及各步耗时/状态。

若配置了 AI_SERVICE_API_KEY，则要求携带 X-API-Key（与 server 鉴权一致）；
未配置（开发模式）放行。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from src.config import config
from src.harness.observability.store import trace_store

_TAG = "observability"


def get_trace_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=[_TAG])

    @router.get("/trace/{trace_id}")
    async def get_trace(trace_id: str, request: Request):
        if config.ai_service_api_key:
            provided = request.headers.get("X-API-Key")
            if provided != config.ai_service_api_key:
                raise HTTPException(
                    status_code=401,
                    detail={"error": "UNAUTHORIZED", "message": "缺少或无效的 API Key"},
                )

        trace = trace_store.load(trace_id)
        if trace is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "TRACE_NOT_FOUND",
                    "message": "trace 不存在或已过期（热存储 TTL 默认 1h）",
                },
            )
        return trace

    return router
