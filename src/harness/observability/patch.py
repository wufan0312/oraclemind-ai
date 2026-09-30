"""玄镜 OracleMind · Observability 决策树追踪 —— LLM 埋点

唯一 LLM 入口是 src.services.provider.LLMProvider（stream_messages / chat）。
这里做「方法级包装」：替换 provider 实例的方法属性，使其在执行前后自动往
当前 Tracer 写入 llm 类型的子节点。由于所有业务代码都通过同一个全局单例
llm_provider 调用，包装一次即全局生效，report_agent / interpret / chat 等零改动。

无激活 Tracer 时完全透传，零开销。
"""

from __future__ import annotations

import time

from src.config import config
from src.harness.observability.tracer import get_tracer


# 已包装标记：挂到 provider 实例上，用于实现真正的幂等。
_TRACED_FLAG = "_om_tracing_installed"


def install_tracing(provider) -> None:
    """给一个 LLMProvider 实例装上埋点。幂等（重复调用安全）。

    注意：这里必须把原始方法**先取出来存进局部变量**（_orig_stream / _orig_chat），
    嵌套函数才能通过闭包拿到它们。若在嵌套函数里直接写裸名字（如 _orig_chat），
    CPython 会按「全局名」解析 → NameError。历史上就因为这个漏捕获 _orig_chat，
    导致 TRACE_ENABLED=true 时所有 provider.chat() 调用必抛
    `name '_orig_chat' is not defined`（结构化解读全链路降级）。
    """
    # 幂等：已包装过就直接返回，避免重复包裹造成 span 重复计数 / 无限嵌套
    if getattr(provider, _TRACED_FLAG, False):
        return

    _orig_stream = provider.stream_messages
    # chat 可能不存在（测试替身 / 精简 provider），用 getattr 兜底
    _orig_chat = getattr(provider, "chat", None)

    async def traced_stream_messages(messages, *, temperature=0.7, max_tokens=2048, **kw):
        tracer = get_tracer()
        if tracer is None:
            async for c in _orig_stream(
                messages, temperature=temperature, max_tokens=max_tokens, **kw
            ):
                yield c
            return

        t0 = time.monotonic()
        in_chars = sum(len(str(m.get("content", ""))) for m in (messages or []))
        out_chars = 0
        model = config.ai_model_chat
        provider_name = config.ai_provider

        try:
            async for c in _orig_stream(
                messages, temperature=temperature, max_tokens=max_tokens, **kw
            ):
                out_chars += len(c)
                yield c
        except Exception as e:  # noqa: BLE001
            tracer.record_step(
                "llm",
                "stream_messages",
                status="error",
                error=f"{type(e).__name__}: {e}",
                meta={
                    "model": model,
                    "provider": provider_name,
                    "temperature": temperature,
                    "in_chars": in_chars,
                    "out_chars": out_chars,
                    "duration_ms": round((time.monotonic() - t0) * 1000, 1),
                },
            )
            raise

        tracer.record_step(
            "llm",
            "stream_messages",
            meta={
                "model": model,
                "provider": provider_name,
                "temperature": temperature,
                "in_chars": in_chars,
                "out_chars": out_chars,
                "duration_ms": round((time.monotonic() - t0) * 1000, 1),
            },
        )

    async def traced_chat(
        system_prompt, user_prompt, *, temperature=0.7, max_tokens=2048, timeout_ms=30000, **kw
    ):
        tracer = get_tracer()
        if tracer is None:
            return await _orig_chat(
                system_prompt,
                user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout_ms=timeout_ms,
                **kw,
            )

        t0 = time.monotonic()
        in_chars = len(system_prompt or "") + len(user_prompt or "")
        try:
            result = await _orig_chat(
                system_prompt,
                user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout_ms=timeout_ms,
                **kw,
            )
            out = result.get("content", "") or ""
            tracer.record_step(
                "llm",
                "chat",
                meta={
                    "model": result.get("model"),
                    "provider": result.get("provider"),
                    "in_chars": in_chars,
                    "out_chars": len(out),
                    "tokens": result.get("tokens"),
                    "duration_ms": round((time.monotonic() - t0) * 1000, 1),
                },
            )
            return result
        except Exception as e:  # noqa: BLE001
            tracer.record_step(
                "llm",
                "chat",
                status="error",
                error=f"{type(e).__name__}: {e}",
                meta={
                    "model": config.ai_model_chat,
                    "in_chars": in_chars,
                    "duration_ms": round((time.monotonic() - t0) * 1000, 1),
                },
            )
            raise

    provider.stream_messages = traced_stream_messages
    if _orig_chat is not None:
        provider.chat = traced_chat

    setattr(provider, _TRACED_FLAG, True)
