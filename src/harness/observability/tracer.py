"""玄镜 OracleMind · Observability 决策树追踪 —— Tracer 核心

设计目标（对应「玄镜 Harness 化改造清单」P0-2）：
- 把每一次用户请求在 Agent 内部的「决策路径」沉淀成一棵可回放的树：
  analyzer → paipan → synthesizer(LLM) → reviewer(LLM) → writer(LLM)。
- Tracer 通过 contextvar 在协程内传递，LLM 包装层（patch.py）自动读取并埋点，
  业务代码（report_agent 等）只需在关键节点调 mark_phase / record_step 即可。
- 无 tracer 时（如离线脚本、缓存回放）所有 API 均为 no-op，绝不拖垮主流程。
"""

from __future__ import annotations

import time
import uuid
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# 当前协程激活的 Tracer；路由层 route_trace 内置、LLM 埋点读取。
_tracer_ctx: ContextVar[Optional["Tracer"]] = ContextVar("om_tracer", default=None)


@dataclass
class Span:
    """决策树中的一个节点。"""

    id: str
    trace_id: str
    parent_id: Optional[str]
    kind: str          # phase | llm | paipan | retrieval | tool | error
    name: str
    started_at: float
    ended_at: Optional[float] = None
    duration_ms: Optional[float] = None
    status: str = "ok"  # ok | error
    error: Optional[str] = None
    meta: dict = field(default_factory=dict)


class Tracer:
    """单次请求的决策树记录器（单协程内使用，无需加锁）。"""

    def __init__(self, trace_id: str, meta: Optional[dict] = None):
        self.trace_id = trace_id
        self.meta: dict = dict(meta or {})
        self.spans: list[Span] = []
        self.started_at = time.monotonic()
        self.ended_at: Optional[float] = None
        self.status = "ok"
        # 当前激活的 phase：后续 llm/step 默认挂到它之下，形成决策树。
        self._current_phase_id: Optional[str] = None

    def _new_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def mark_phase(self, name: str, *, meta: Optional[dict] = None) -> str:
        """标记一个顶层阶段（analyzer / paipan / synthesizer / reviewer / writer）。

        调用后，后续的 LLM/工具调用会作为该阶段的子节点，从而构成决策树。
        """
        span = Span(
            id=self._new_id(),
            trace_id=self.trace_id,
            parent_id=None,
            kind="phase",
            name=name,
            started_at=time.monotonic(),
            meta=dict(meta or {}),
        )
        self.spans.append(span)
        self._current_phase_id = span.id
        return span.id

    def record_step(
        self,
        kind: str,
        name: str,
        *,
        status: str = "ok",
        error: Optional[str] = None,
        meta: Optional[dict] = None,
    ) -> str:
        """记录一个瞬时步骤。默认挂到当前 phase 之下（kind=='phase' 时除外）。"""
        parent = self._current_phase_id if kind != "phase" else None
        span = Span(
            id=self._new_id(),
            trace_id=self.trace_id,
            parent_id=parent,
            kind=kind,
            name=name,
            started_at=time.monotonic(),
            status=status,
            error=error,
            meta=dict(meta or {}),
        )
        span.ended_at = span.started_at
        span.duration_ms = 0.0
        self.spans.append(span)
        if status == "error":
            self.status = "error"
        return span.id

    def set_meta(self, key: str, value: Any) -> None:
        self.meta[key] = value

    def finish(self) -> None:
        self.ended_at = time.monotonic()

    def to_dict(self) -> dict:
        ended = self.ended_at or time.monotonic()
        llm_spans = [s for s in self.spans if s.kind == "llm"]
        out_chars = sum((s.meta.get("out_chars") or 0) for s in llm_spans)
        return {
            "trace_id": self.trace_id,
            "started_at": round(self.started_at, 3),
            "ended_at": round(ended, 3),
            "duration_ms": round((ended - self.started_at) * 1000, 1),
            "status": self.status,
            "meta": self.meta,
            "span_count": len(self.spans),
            "llm_calls": len(llm_spans),
            "llm_out_chars": out_chars,
            "spans": [asdict(s) for s in self.spans],
        }


# ----------------------------- 模块级便捷 API（no-op 安全） -----------------------------

def get_tracer() -> Optional[Tracer]:
    return _tracer_ctx.get()


def set_tracer(tracer: Tracer):
    return _tracer_ctx.set(tracer)


def reset_tracer(token) -> None:
    _tracer_ctx.reset(token)


def mark_phase(name: str, *, meta: Optional[dict] = None) -> None:
    """在激活的 Tracer 上标记阶段；无 Tracer 时 no-op。"""
    t = _tracer_ctx.get()
    if t is not None:
        try:
            t.mark_phase(name, meta=meta)
        except Exception:  # 观测层绝不允许影响主流程
            pass


def record_step(
    kind: str,
    name: str,
    *,
    status: str = "ok",
    error: Optional[str] = None,
    meta: Optional[dict] = None,
) -> None:
    """在激活的 Tracer 上记录步骤；无 Tracer 时 no-op。"""
    t = _tracer_ctx.get()
    if t is not None:
        try:
            t.record_step(kind, name, status=status, error=error, meta=meta)
        except Exception:
            pass
