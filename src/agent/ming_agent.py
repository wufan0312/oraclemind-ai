"""玄镜 OracleMind · 起名 ReAct Agent（LangChain Agent）

使用 langchain.agents.create_agent 实现多步推理（LangGraph 1.0 起
langgraph.prebuilt.create_react_agent 已 deprecated，V2.0 移除）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, AsyncIterator, Optional

from src.config import config
from src.services.budget import budget_broken, record_cost, estimate_cost, cache_get, cache_set
from src.agent.prompts import MING_AGENT_SYSTEM_PROMPT, MING_AGENT_DISCLAIMER

logger = logging.getLogger(__name__)

# Agent 结果缓存：同一问题（含出生信息）直接复用已验证文本，
# 避免 LLM 重新生成时出现数据前后不一（幻觉）。
_AGENT_CACHE_TTL = 24 * 3600


def _agent_cache_key(message: str, birth_hint: Optional[dict]) -> str:
    """以「用户消息 + 出生信息」为缓存键。命中即返回同一份文本。"""
    raw = json.dumps(
        {"m": message, "b": birth_hint or {}},
        ensure_ascii=False,
        sort_keys=True,
    )
    h = hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]
    return f"agent:ming:v2:{h}"

# 全局 Agent 单例
_agent = None


def _build_model():
    """构建 ChatOpenAI 实例"""
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=config.ai_model_chat,
        api_key=config.ai_api_key,
        base_url=config.ai_base_url,
        temperature=0.7,
        max_tokens=2048,
        streaming=True,
    )


def _build_tools():
    """构建 LangChain Tool 列表

    注意：仅保留 bazi_paipan_tool（八字排盘，后端有真实接口）。
    原 name_generate_tool / name_detail_tool 调用的 /api/v1/ming/name/*
    接口在后端并不存在（404），Agent 拿到失败信息后会凭记忆编造
    候选名与笔画五格数据 —— 这是幻觉的来源之一，故移除。
    起名候选由前端本地生成并附在用户消息里，Agent 负责点评与推荐。
    """
    from langchain_core.tools import tool

    from src.agent.tools.bazi_paipan import bazi_paipan

    @tool
    def bazi_paipan_tool(year: int, month: int, day: int, time_text: str, gender: str) -> str:
        """调用八字排盘服务，获取四柱八字、喜用神等信息。

        Args:
            year: 出生年（公历）
            month: 出生月（公历）
            day: 出生日（公历）
            time_text: 时辰文本（如"辰时"）
            gender: 性别（"男"/"女"）
        """
        return bazi_paipan(year, month, day, time_text, gender)

    return [bazi_paipan_tool]


def _get_agent():
    """获取/初始化 Agent（惰性单例）"""
    global _agent
    if _agent is not None:
        return _agent

    from langchain.agents import create_agent

    model = _build_model()
    tools = _build_tools()
    _agent = create_agent(
        model=model,
        tools=tools,
        system_prompt=MING_AGENT_SYSTEM_PROMPT,
    )
    logger.info("起名 ReAct Agent 已初始化")
    return _agent


def _build_degraded(message: str) -> str:
    return f"> 起名顾问暂时不可用：{message}\n\n建议您：\n- 稍后重试\n- 或直接在「测字·起名·合婚」页面手动起名"


async def run_ming_agent(
    message: str,
    birth_hint: Optional[dict] = None,
) -> tuple[str, bool, Optional[str]]:
    """运行起名 Agent（非流式）

    Returns: (text, degraded, degraded_reason)
    """
    # LLM 不可用
    if not config.llm_available:
        return _build_degraded("AI_API_KEY 未配置"), True, "AI_API_KEY 未配置"

    # 预算熔断
    if budget_broken():
        return _build_degraded("单日预算超限"), True, "单日预算超限"

    # 构造用户消息
    user_message = message
    if birth_hint and (birth_hint.get("year") or birth_hint.get("gender")):
        b = birth_hint
        user_message += f"\n\n【已知访客信息】"
        if b.get("year"):
            user_message += f" 出生：{b['year']}年{b.get('month','')}月{b.get('day','')}日"
        if b.get("timeText"):
            user_message += f" {b['timeText']}"
        else:
            # 时辰缺失：明确告诉 agent，不要自己补一个时辰
            user_message += "（时辰未提供——请勿假设时辰，不要调用 bazi_paipan_tool）"
        if b.get("gender"):
            user_message += f" {b['gender']}"

    # 结果缓存：同一问题（含出生信息）直接复用已验证文本，不重跑 LLM
    cache_key = _agent_cache_key(message, birth_hint)
    cached = cache_get(cache_key)
    if cached:
        logger.info(f"[ming] Agent 结果缓存命中: {cache_key}")
        return cached, False, None

    try:
        agent = _get_agent()
        result = await agent.ainvoke({
            "messages": [{"role": "user", "content": user_message}],
        })
        # 提取最终消息
        messages = result.get("messages", [])
        text = ""
        for msg in reversed(messages):
            if hasattr(msg, "content") and msg.type == "ai":
                text = msg.content
                break

        if not text:
            return "> 起名顾问未返回内容，请重试。", True, "Agent 未输出内容"

        # 附免责
        if MING_AGENT_DISCLAIMER not in text:
            text += f"\n\n---\n*{MING_AGENT_DISCLAIMER}*"

        cache_set(cache_key, text, ttl_seconds=_AGENT_CACHE_TTL)
        return text, False, None

    except Exception as e:
        reason = str(e)
        return _build_degraded(f"Agent 运行异常：{reason}"), True, reason


async def stream_ming_agent(
    message: str,
    birth_hint: Optional[dict] = None,
) -> AsyncIterator[dict]:
    """运行起名 Agent（流式），yield SSE 事件

    Events: open, tool_start, tool_end, delta, meta, done, error
    """
    # LLM 不可用
    if not config.llm_available:
        yield {"event": "error", "data": {"reason": "AI_API_KEY 未配置"}}
        return

    # 预算熔断
    if budget_broken():
        yield {"event": "error", "data": {"reason": "单日预算超限"}}
        return

    # 构造用户消息
    user_message = message
    if birth_hint and (birth_hint.get("year") or birth_hint.get("gender")):
        b = birth_hint
        user_message += "\n\n【已知访客信息】"
        if b.get("year"):
            user_message += f" 出生：{b['year']}年{b.get('month','')}月{b.get('day','')}日"
        if b.get("timeText"):
            user_message += f" {b['timeText']}"
        else:
            # 时辰缺失：明确告诉 agent，不要自己补一个时辰
            user_message += "（时辰未提供——请勿假设时辰，不要调用 bazi_paipan_tool）"
        if b.get("gender"):
            user_message += f" {b['gender']}"

    # 结果缓存：同一问题（含出生信息）直接复用已验证文本，不重跑 LLM
    cache_key = _agent_cache_key(message, birth_hint)
    cached = cache_get(cache_key)
    if cached:
        logger.info(f"[ming] Agent 结果缓存命中（流式回放）: {cache_key}")
        yield {"event": "open", "data": {}}
        yield {"event": "delta", "data": {"chunk": cached}}
        yield {
            "event": "meta",
            "data": {
                "provider": config.ai_provider,
                "model": config.ai_model_chat,
                "disclaimer": MING_AGENT_DISCLAIMER,
                "degraded": False,
                "degradedReason": None,
                "cached": True,
            },
        }
        yield {"event": "done", "data": {"disclaimer": MING_AGENT_DISCLAIMER}}
        return

    yield {"event": "open", "data": {}}

    try:
        agent = _get_agent()
        full_text = ""

        async for event in agent.astream_events(
            {"messages": [{"role": "user", "content": user_message}]},
            version="v2",
        ):
            evt_type = event.get("event", "")
            name = event.get("name", "")
            data = event.get("data", {})

            # LLM token 流
            if evt_type == "on_chat_model_stream":
                chunk = data.get("chunk")
                text = getattr(chunk, "content", "") if chunk else ""
                if text:
                    full_text += text
                    # 前端 ming 流解析只认 data.chunk
                    yield {"event": "delta", "data": {"chunk": text}}

            # 工具开始
            elif evt_type == "on_tool_start":
                yield {"event": "tool_start", "data": {"tool": name, "input": str(data.get("input", ""))[:500]}}

            # 工具结束
            elif evt_type == "on_tool_end":
                output = data.get("output", "")
                if hasattr(output, "content"):
                    output = output.content
                yield {"event": "tool_end", "data": {"tool": name, "output": str(output)[:1000]}}

        # 写入结果缓存（附免责后再存，回放时格式与首次完全一致）
        if full_text:
            if MING_AGENT_DISCLAIMER not in full_text:
                full_text += f"\n\n---\n*{MING_AGENT_DISCLAIMER}*"
            cache_set(cache_key, full_text, ttl_seconds=_AGENT_CACHE_TTL)

        yield {
            "event": "meta",
            "data": {
                "provider": config.ai_provider,
                "model": config.ai_model_chat,
                "disclaimer": MING_AGENT_DISCLAIMER,
                "degraded": False,
                "degradedReason": None,
            },
        }
        yield {"event": "done", "data": {"disclaimer": MING_AGENT_DISCLAIMER}}

    except Exception as e:
        yield {"event": "error", "data": {"code": "STREAM_ERROR", "message": str(e) or "起名 Agent 失败"}}
