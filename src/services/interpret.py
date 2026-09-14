"""玄镜 OracleMind · 解读编排服务

整合 Prompt、Provider、Cache、Budget、Retrieval（RAG）。
"""

from __future__ import annotations

import json
import asyncio
import logging
import re
import time
import uuid
from typing import Any, AsyncIterator, Optional

from src.config import config
from src.prompts.shared import DISCLAIMER, PromptTemplate
from src.prompts.registry import get_prompt, get_refs_explain_prompt
from src.services.provider import llm_provider
from src.services.budget import budget_broken, record_cost, estimate_cost, make_cache_key, cache_get, cache_set
from src.services.retrieval import retrieve_for_module
from src.services.formatter import format_interpret_json
from src.oracle_types import InterpretRequest, InterpretResponse, InterpretMeta

logger = logging.getLogger(__name__)


# 旧格式 aspect 标题（LLM 有时会忽略自定义格式，输出这些默认标题）
_LEGACY_SIHUA_TITLES = {"性格倾向", "事业财运", "情感人际", "格局印证"}
_LEGACY_STARS_TITLES = {"性格倾向", "事业财运", "情感人际", "格局印证"}


def _post_process_ziwei(raw_text: str, focus: str, result: dict) -> str:
    """紫微斗数后处理：动态替换 aspect 标题

    当 LLM 忽略自定义格式、输出旧标题（性格倾向/事业财运等）时，
    根据 result 中的 sihua/stars 数据动态生成正确的标题。
    """
    if not raw_text or not raw_text.strip():
        return raw_text

    # 尝试提取 JSON
    json_str = raw_text.strip()
    if json_str.startswith("```"):
        json_str = re.sub(r"^```(?:json)?\s*", "", json_str)
        json_str = re.sub(r"\s*```$", "", json_str).strip()

    # 找第一个 { 到最后一个 }
    start = json_str.find("{")
    end = json_str.rfind("}")
    if start == -1 or end == -1:
        return raw_text

    candidate = json_str[start : end + 1]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return raw_text

    if not isinstance(data, dict):
        return raw_text

    aspects = data.get("aspects", [])
    if not isinstance(aspects, list) or not aspects:
        return raw_text

    # 检查是否需要标题替换
    needs_rewrite = False
    for aspect in aspects:
        if isinstance(aspect, dict):
            title = aspect.get("title", "")
            if title in _LEGACY_SIHUA_TITLES or title in _LEGACY_STARS_TITLES:
                needs_rewrite = True
                break

    if not needs_rewrite:
        return raw_text

    # 根据 focus 和 result 构建正确的标题列表
    if focus == "sihua":
        sihua_list = result.get("sihua", []) if isinstance(result, dict) else []
        correct_titles = []
        for s in sihua_list:
            star = s.get("star", "")
            hua = s.get("hua", "")
            palace = s.get("palace", "")
            if star and hua and palace:
                correct_titles.append(f"化{hua}·{star}落{palace}")
        correct_titles.append("四化协调")
    elif focus == "stars":
        correct_titles = ["命宫主星特质", "三方四正格局", "北斗南斗配合", "经典格局印证"]
    else:
        return raw_text

    # 替换 aspect 标题（按顺序对应）
    for i, aspect in enumerate(aspects):
        if i < len(correct_titles) and isinstance(aspect, dict):
            aspect["title"] = correct_titles[i]

    # 返回修改后的 JSON 字符串
    return json.dumps(data, ensure_ascii=False)


def _build_degraded_response(
    module: str,
    reason: str,
    request_id: str,
    latency_ms: int,
) -> InterpretResponse:
    """降级响应"""
    text = f"> AI 解读暂时不可用：{reason}\n\n建议稍后重试。"
    meta = InterpretMeta(
        requestId=request_id,
        module=module,
        promptVersion="degraded",
        provider="none",
        model="none",
        degraded=True,
        degradedReason=reason,
        latencyMs=latency_ms,
    )
    return InterpretResponse(text=text, disclaimer=DISCLAIMER, meta=meta)


async def interpret(req: InterpretRequest) -> InterpretResponse:
    """主编排入口：检索增强 → 预算熔断 → LLM 调用 → 降级"""
    request_id = req.requestId or str(uuid.uuid4())[:8]
    start = time.time()

    # 选择 Prompt 模板
    use_refs = req.focus == "refs"
    if use_refs:
        tpl = get_refs_explain_prompt(req.module)
    else:
        tpl = get_prompt(req.module)

    # 检索增强（RAG）
    refs_text = ""
    retrieval_info = None
    if config.retrieval_enabled:
        try:
            # RAG 检索为同步 I/O（Chroma 查询 + embedding API），移出事件循环避免冻结整站并发
            refs_text = await asyncio.to_thread(
                retrieve_for_module, req.module, req.result if isinstance(req.result, dict) else {}
            )
            if refs_text:
                retrieval_info = {
                    "enabled": True,
                    "count": refs_text.count("[") if refs_text else 0,
                    "sources": [],  # 从 refs_text 中提取
                    "text": refs_text,
                }
        except Exception as e:
            logger.warning(f"检索增强失败: {e}")

    # 缓存检查
    result_json = json.dumps(req.result, ensure_ascii=False, sort_keys=True)
    cache_key = make_cache_key(req.module, result_json, req.focus)
    cached = cache_get(cache_key)
    if cached:
        latency = int((time.time() - start) * 1000)
        meta = InterpretMeta(
            requestId=request_id,
            module=req.module,
            promptVersion=tpl.version,
            provider="cache",
            model="cache",
            cacheHit=True,
            latencyMs=latency,
            retrieval=retrieval_info,
        )
        return InterpretResponse(text=cached, disclaimer=DISCLAIMER, meta=meta)

    # 预算熔断
    if budget_broken():
        latency = int((time.time() - start) * 1000)
        return _build_degraded_response(req.module, "单日预算超限", request_id, latency)

    # LLM 不可用
    if not config.llm_available:
        latency = int((time.time() - start) * 1000)
        return _build_degraded_response(req.module, "AI_API_KEY 未配置", request_id, latency)

    # 构造 system prompt（注入检索增强）
    system_prompt = tpl.get_system(req.focus)
    if refs_text:
        system_prompt = f"{system_prompt}\n\n{refs_text}"

    # 构造 user prompt
    user_prompt = tpl.build_user(req.result, req.focus)

    # LLM 调用
    try:
        result = await llm_provider.chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=tpl.temperature,
            max_tokens=tpl.max_tokens,
            timeout_ms=config.total_timeout_ms,
        )

        latency = int((time.time() - start) * 1000)

        # 将 LLM 返回的 JSON 转换为 Markdown（方案 B）
        raw_text = result["content"]
        
        # 紫微斗数后处理：根据 focus 动态替换 aspect 标题
        if req.module == "ziwei" and req.focus in ("sihua", "stars"):
            raw_text = _post_process_ziwei(raw_text, req.focus, req.result)
        
        formatted_text = format_interpret_json(raw_text, module=req.module, focus=req.focus)

        # 记录成本
        if result.get("tokens"):
            cost = estimate_cost(result["tokens"])
            record_cost(cost)
        else:
            cost = None

        # 缓存格式化后的文本
        cache_set(cache_key, formatted_text, ttl_seconds=3600)

        meta = InterpretMeta(
            requestId=request_id,
            module=req.module,
            promptVersion=tpl.version,
            provider=result["provider"],
            model=result["model"],
            tokens=result.get("tokens"),
            costYuan=cost,
            latencyMs=latency,
            retrieval=retrieval_info,
        )
        return InterpretResponse(text=formatted_text, disclaimer=DISCLAIMER, meta=meta)

    except Exception as e:
        logger.error(f"LLM 调用失败: {e}")
        latency = int((time.time() - start) * 1000)
        return _build_degraded_response(req.module, str(e), request_id, latency)


async def interpret_stream(req: InterpretRequest) -> AsyncIterator[dict]:
    """流式解读，yield SSE 事件 dict

    优化策略：
    1. 缓存命中直接返回（秒回）
    2. LLM 全量生成后，以打字机效果逐段发送格式化 Markdown
    """
    import asyncio

    request_id = req.requestId or str(uuid.uuid4())[:8]
    start = time.time()

    # 选择 Prompt
    use_refs = req.focus == "refs"
    tpl = get_refs_explain_prompt(req.module) if use_refs else get_prompt(req.module)

    # 检索增强（同步 I/O 移出事件循环）
    refs_text = ""
    if config.retrieval_enabled:
        try:
            refs_text = await asyncio.to_thread(
                retrieve_for_module, req.module, req.result if isinstance(req.result, dict) else {}
            )
        except Exception:
            pass

    # 缓存检查 —— 命中则秒回
    result_json = json.dumps(req.result, ensure_ascii=False, sort_keys=True)
    cache_key = make_cache_key(req.module, result_json, req.focus)
    cached = cache_get(cache_key)
    if cached:
        yield {"event": "open", "data": {"requestId": request_id, "module": req.module, "promptVersion": tpl.version}}
        # 缓存命中也走打字机效果，但非常快
        for i in range(0, len(cached), 3):
            yield {"event": "delta", "data": {"chunk": cached[i : i + 3]}}
            await asyncio.sleep(0.01)
        latency = int((time.time() - start) * 1000)
        yield {"event": "meta", "data": {"meta": {"requestId": request_id, "module": req.module, "promptVersion": tpl.version, "provider": "cache", "model": "cache", "latencyMs": latency}, "disclaimer": DISCLAIMER}}
        yield {"event": "done", "data": {"text": cached, "disclaimer": DISCLAIMER}}
        return

    # 降级检查
    if not config.llm_available or budget_broken():
        reason = "AI_API_KEY 未配置" if not config.llm_available else "单日预算超限"
        yield {"event": "error", "data": {"code": "LLM_UNAVAILABLE", "message": reason}}
        return

    # 构造 prompt
    system_prompt = tpl.get_system(req.focus)
    if refs_text:
        system_prompt = f"{system_prompt}\n\n{refs_text}"
    user_prompt = tpl.build_user(req.result, req.focus)

    # 发送 open 事件
    yield {
        "event": "open",
        "data": {
            "requestId": request_id,
            "module": req.module,
            "promptVersion": tpl.version,
        },
    }

    # 流式 LLM —— 收集全量文本后统一做 JSON→Markdown 转换
    raw_text = ""
    try:
        async for chunk in llm_provider.stream(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=tpl.temperature,
            max_tokens=tpl.max_tokens,
        ):
            raw_text += chunk

        # 全量收齐后转换为 Markdown
        if req.module == "ziwei" and req.focus in ("sihua", "stars"):
            raw_text = _post_process_ziwei(raw_text, req.focus, req.result)
        formatted_text = format_interpret_json(raw_text, module=req.module, focus=req.focus)

        # 缓存结果
        cache_set(cache_key, formatted_text, ttl_seconds=3600)

        # 打字机效果：每 30ms 发送 2-3 个字符，模拟真实阅读速度
        step = 2
        for i in range(0, len(formatted_text), step):
            chunk_text = formatted_text[i : i + step]
            yield {"event": "delta", "data": {"chunk": chunk_text}}
            await asyncio.sleep(0.03)

        # 发送 meta + done
        latency = int((time.time() - start) * 1000)
        yield {
            "event": "meta",
            "data": {
                "meta": {
                    "requestId": request_id,
                    "module": req.module,
                    "promptVersion": tpl.version,
                    "provider": config.ai_provider,
                    "model": config.ai_model_chat,
                    "latencyMs": latency,
                },
                "disclaimer": DISCLAIMER,
            },
        }
        yield {"event": "done", "data": {"text": formatted_text, "disclaimer": DISCLAIMER}}

    except Exception as e:
        yield {"event": "error", "data": {"code": "STREAM_ERROR", "message": str(e) or "流式解读失败"}}
