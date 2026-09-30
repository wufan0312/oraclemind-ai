"""玄镜 OracleMind · Observability 决策树追踪 —— SSE 注入与路由级接线

route_trace：路由层一行接入。生成 trace_id，挂到 contextvar，包装业务生成器：
- 在首个 `open` 事件注入 traceId + traceUrl（前端据此拉取决策树）；
- 在生成器结束后（done/error）flush 到存储层。

业务路由从：
    return EventSourceResponse(event_generator())
改为：
    return EventSourceResponse(route_trace(event_generator(), meta={...}))
即可，内部 LLM 调用会被自动记录为决策树节点。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator, Optional

from src.harness.observability.store import trace_store
from src.harness.observability.tracer import Tracer, get_tracer, reset_tracer, set_tracer


async def trace_sse(gen: AsyncIterator[dict], trace_id: str, tracer: Tracer) -> AsyncIterator[dict]:
    """包装业务 SSE 生成器：注入 traceId、结束后落盘。"""
    injected = False
    async for ev in gen:
        if not injected and ev.get("event") == "open":
            data = ev.get("data", {})
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:  # noqa: BLE001
                    data = {}
            if not isinstance(data, dict):
                data = {}
            data = {
                **data,
                "traceId": trace_id,
                "traceUrl": f"/api/v1/trace/{trace_id}",
            }
            ev = {"event": ev["event"], "data": json.dumps(data, ensure_ascii=False)}
            injected = True
        yield ev

    # 生成器结束（done/error 已发出）—— 落盘决策树
    try:
        await trace_store.save(tracer)
    except Exception:  # noqa: BLE001
        pass


async def route_trace(
    gen: AsyncIterator[dict], *, meta: Optional[dict] = None
) -> AsyncIterator[dict]:
    """路由层接线：建立 Tracer 生命周期并包装 SSE 流。

    用法：
        return EventSourceResponse(route_trace(event_generator(), meta={"module": "bazi"}))
    """
    trace_id = f"tr_{uuid.uuid4().hex[:20]}"
    tracer = Tracer(trace_id, meta=meta or {})
    token = set_tracer(tracer)
    # 自动标记顶层阶段，使所有经 route_trace 的 agent 至少有一个横向可比的 trace 节点，
    # 无需逐个业务 agent 内部调用 mark_phase（业务内部仍可在其上追加 analyzer/paipan...）。
    scope = (meta or {}).get("agent") or (meta or {}).get("module") or "unknown"
    kind = (meta or {}).get("kind") or "agent"
    try:
        tracer.mark_phase(f"{kind}:{scope}")
        async for ev in trace_sse(gen, trace_id, tracer):
            yield ev
    finally:
        reset_tracer(token)
