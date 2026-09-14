"""紫微斗数排盘工具 — Agent 调用"""

from __future__ import annotations

import logging

from src.config import config

logger = logging.getLogger(__name__)


def ziwei_paipan(
    year: int,
    month: int,
    day: int,
    time_text: str,
    gender: str,
) -> str:
    """调用紫微斗数排盘服务，获取命盘十二宫、主星、四化等信息。

    Args:
        year: 出生年（公历）
        month: 出生月（公历）
        day: 出生日（公历）
        time_text: 时辰文本（如"辰时"）
        gender: 性别（"男"/"女"）
    """
    import httpx
    import json

    try:
        resp = httpx.post(
            f"{config.paipan_api_base}/api/v1/ziwei/paipan",
            json={
                "year": year,
                "month": month,
                "day": day,
                "timeText": time_text,
                "gender": gender,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return json.dumps(resp.json().get("data", {}), ensure_ascii=False)
    except Exception as e:
        logger.warning("ziwei_paipan 调用失败: %s", e)
        raise RuntimeError("紫微斗数排盘服务暂时不可用，请稍后重试") from e
