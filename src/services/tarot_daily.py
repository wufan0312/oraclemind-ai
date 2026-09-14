"""玄镜 OracleMind · 塔罗每日解读编排

对应 TS 版 src/services/tarotDaily.ts —— 缓存 + 预算熔断 + 结构化 JSON 生成 + 本地降级。
返回 { data, disclaimer, meta }，data 为结构化 TarotDailyOutput。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

from src.config import config
from src.prompts.shared import DISCLAIMER
from src.prompts.tarot_daily import tarot_daily_prompt
from src.services.budget import (
    budget_broken, record_cost, estimate_cost,
    cache_get, cache_set, make_cache_key,
)
from src.services.structured import generate_structured

logger = logging.getLogger(__name__)


def _fallback(result: dict) -> dict:
    """本地降级：服务端无牌库，具体牌义由前端本地牌库兜底渲染。"""
    cards = result.get("cards", []) if isinstance(result, dict) else []
    theme_name = (result.get("theme") or {}).get("name", "塔罗") if isinstance(result, dict) else "塔罗"
    return {
        "ok": True,
        "themeKw": f"{theme_name} · 今日能量",
        "summary": f"今日牌面已定：主题牌「{theme_name}」奠定整体基调，三张指引牌从不同侧面提醒当下的节奏与方向。建议先静心感受主题牌的能量，再逐一翻开三张牌的指引，按牌义提示安排今日生活；重要决定仍应结合现实理性判断。",
        "energy": [
            {"k": "🔮 能量提醒", "v": "AI 解读暂不可用，已切换本地规则。今日牌面已抽定，静心感受主题牌的能量。"},
            {"k": "💡 行动建议", "v": "翻开每日三牌，参考每张牌的牌义提示；重要决定仍应结合现实理性判断。"},
            {"k": "⚠️ 避坑", "v": "避免在能量未明时仓促做重大决定，先安顿情绪再行动。"},
        ],
        "cards": [
            {"name": c.get("name", ""), "posi": "逆位" if c.get("isRev") else "正位",
             "read": f"今日第{i + 1}张牌「{c.get('name', '')}」（{'逆位' if c.get('isRev') else '正位'}）能量已就位，可参考牌义提示安排今日节奏。"}
            for i, c in enumerate(cards)
        ],
    }


def _align_cards(parsed: dict, result: dict) -> dict:
    """按请求牌面校正 AI 返回的 cards 数组

    LLM（尤其 flash 级模型）偶发把「今日主题牌」写进 cards、或漏掉某张牌，
    导致与请求牌面错位。这里以请求为准重排：命中牌名的用 AI 文案，缺失的补本地兜底句。
    """
    req_cards = result.get("cards") or []
    if not req_cards:
        return parsed

    ai_cards = parsed.get("cards")
    ai_cards = ai_cards if isinstance(ai_cards, list) else []
    by_name = {
        str(c.get("name", "")): c
        for c in ai_cards
        if isinstance(c, dict) and c.get("name")
    }

    aligned = []
    for c in req_cards:
        name = str(c.get("name", ""))
        posi = "逆位" if c.get("isRev") else "正位"
        hit = by_name.get(name)
        read = str(hit.get("read", "")).strip() if isinstance(hit, dict) else ""
        if read:
            aligned.append({
                "name": name,
                "posi": str(hit.get("posi") or posi),
                "read": read,
            })
        else:
            aligned.append({
                "name": name,
                "posi": posi,
                "read": f"今日「{name}」（{posi}）能量已就位，可参考牌义提示安排今日节奏；重要决定仍应结合现实理性判断。",
            })

    parsed["cards"] = aligned
    return parsed


def _build(data: dict, rid: str, version: str, start: float, degraded: bool,
           reason: Optional[str], tokens: Optional[dict], provider: Optional[str],
           model: Optional[str], cost: Optional[float] = None, cache_hit: bool = False) -> dict:
    return {
        "data": data,
        "disclaimer": DISCLAIMER,
        "meta": {
            "requestId": rid,
            "module": "tarot-daily",
            "promptVersion": version,
            "provider": provider or ("local-rules" if degraded else config.ai_provider),
            "model": model or ("local-rules" if degraded else config.ai_model_chat),
            "cacheHit": cache_hit,
            "degraded": degraded,
            "degradedReason": reason,
            "tokens": tokens,
            "costYuan": cost if cost is not None else (estimate_cost(tokens) if tokens else 0.0),
            "latencyMs": int((time.time() - start) * 1000),
            "truncated": False,
        },
    }


async def interpret_daily_tarot(result: dict, request_id: Optional[str] = None) -> dict:
    """主编排入口：POST /api/v1/tarot/daily"""
    rid = request_id or uuid.uuid4().hex[:8]
    start = time.time()
    prompt = tarot_daily_prompt

    user_msg = prompt.build_user(result)
    key = make_cache_key("tarot-daily", json.dumps(result, ensure_ascii=False, sort_keys=True), None)

    # 缓存命中直返
    cached = cache_get(key)
    if cached:
        try:
            return _build(_align_cards(json.loads(cached), result), rid, prompt.version, start, False, None, None, "cache", "cache", cache_hit=True)
        except Exception:
            pass

    # 预算熔断 → 降级
    if budget_broken():
        return _build(_fallback(result), rid, prompt.version, start, True, "单日预算超限，熔断降级", None, None, None)

    # LLM 调用（失败重试一次）
    parsed, err, tokens, provider, model = await generate_structured(
        prompt.system, user_msg, temperature=prompt.temperature, max_tokens=prompt.max_tokens, retries=1
    )
    if err:
        return _build(_fallback(result), rid, prompt.version, start, True, err, tokens, provider, model)

    # 成功：记录成本、写缓存
    cost = estimate_cost(tokens) if tokens else 0.0
    record_cost(cost)
    cache_set(key, json.dumps(parsed, ensure_ascii=False))

    return _build(_align_cards(parsed, result), rid, prompt.version, start, False, None, tokens, provider, model, cost)
