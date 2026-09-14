"""八字排盘工具 — Agent 调用（带结果缓存）

排盘是确定性计算：同一出生日期+时辰+性别永远得到同一结果。
命中缓存可避免重复调用后端排盘服务，也保证 Agent 多轮引用的数据一致。
"""

from __future__ import annotations

import json
import logging

from src.config import config
from src.services.budget import cache_get, cache_set

logger = logging.getLogger(__name__)

# 排盘结果终身不变，缓存 30 天
_PAIPAN_TTL = 30 * 24 * 3600


def bazi_paipan(
    year: int,
    month: int,
    day: int,
    time_text: str,
    gender: str,
    lunar: dict | None = None,
) -> str:
    """调用排盘服务获取八字排盘结果（带缓存）

    Args:
        year: 出生年（公历）
        month: 出生月（公历）
        day: 出生日（公历）
        time_text: 时辰文本（如"辰时"）
        gender: 性别（"男"/"女"）
        lunar: 可选的农历分量 {"year","month","day"}；传入时后端**优先**按农历换算公历
            （month 为负表示闰月）。用于「用户明确说农历、且系统尚未换算出公历」的兜底，
            换算权威在后端 lunar-python，避免本地重复实现历法。
    """
    lunar_part = ""
    if lunar and lunar.get("year") and lunar.get("month") and lunar.get("day"):
        lunar_part = f":lunar{lunar['year']}-{lunar['month']}-{lunar['day']}"
    cache_key = f"tool:bazi:v2:{year}-{month}-{day}:{time_text}:{gender}{lunar_part}"
    cached = cache_get(cache_key)
    if cached:
        logger.info(f"[ming] bazi_paipan 缓存命中: {cache_key}")
        return cached

    import httpx

    try:
        body: dict = {
            "year": year,
            "month": month,
            "day": day,
            "timeText": time_text,
            "gender": gender,
        }
        if lunar_part:
            body["lunar"] = {
                "year": int(lunar["year"]),
                "month": int(lunar["month"]),
                "day": int(lunar["day"]),
            }
        resp = httpx.post(
            f"{config.paipan_api_base}/api/v1/bazi/paipan",
            json=body,
            timeout=10,
        )
        resp.raise_for_status()
        payload = resp.json()
        # 后端 /api/v1/bazi/paipan 平铺返回数据（无 "data" 包裹）；
        # 两种格式都兼容，避免拿到空 {} 导致 Agent 无数据可引而编造。
        result = json.dumps(payload.get("data") or payload, ensure_ascii=False)
        if result and result != "{}":
            cache_set(cache_key, result, ttl_seconds=_PAIPAN_TTL)
        return result
    except Exception as e:
        logger.warning("bazi_paipan 调用失败: %s", e)
        # 改 return 失败串为 raise：失败串会被 ReAct Agent 当成排盘数据吞进去编出假结果；
        # 抛出后由 LangGraph 统一转成 error ToolMessage，LLM 可知排盘失败而非编造（对应缺陷报告 P2·Tool 异常改 raise）。
        raise RuntimeError("八字排盘服务暂时不可用，请稍后重试") from e
