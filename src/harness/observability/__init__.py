"""玄镜 OracleMind · Observability 决策树追踪（P0-2）

一键启用：
    from src.harness.observability import install_observability
    install_observability(app)

内部会：① 给 llm_provider 装自动埋点；② 注册 /trace/{id} 端点；
③ 由路由层 route_trace 在 SSE open 事件注入 traceId。
"""

from __future__ import annotations

import logging

from src.config import config

logger = logging.getLogger(__name__)


def install_observability(app) -> None:
    """在 FastAPI 应用上启用 Observability（受 TRACE_ENABLED 开关控制）。"""
    if not config.trace_enabled:
        logger.info("[observability] 已禁用（TRACE_ENABLED=false）")
        return

    from src.harness.observability.patch import install_tracing
    from src.harness.observability.routes import get_trace_router
    from src.services.provider import llm_provider

    install_tracing(llm_provider)
    app.include_router(get_trace_router())

    backend = "redis" if _redis_available() else "memory(ttl)"
    logger.info(
        f"[observability] 已启用：LLM 自动埋点 + /trace/{{id}} 端点(热存储={backend})"
    )


def _redis_available() -> bool:
    from src.harness.observability.store import trace_store

    return trace_store.redis is not None


__all__ = ["install_observability", "route_trace"]

# route_trace 供路由层直接 import
from src.harness.observability.sse import route_trace  # noqa: E402
