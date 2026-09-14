"""起名生成工具 — Agent 调用"""

from __future__ import annotations

import logging

from src.config import config

logger = logging.getLogger(__name__)


def name_generate(
    surname: str,
    gender: str,
    wuxing_xi: str = "",
    count: int = 5,
) -> str:
    """根据姓氏、性别、喜用神五行生成候选名字

    Args:
        surname: 姓氏（如"李"）
        gender: 性别（"男"/"女"）
        wuxing_xi: 喜用神五行（如"水"，可选）
        count: 生成数量（默认5个）
    """
    import httpx

    try:
        resp = httpx.post(
            f"{config.paipan_api_base}/api/v1/ming/name/generate",
            json={
                "surname": surname,
                "gender": gender,
                "wuxingXi": wuxing_xi,
                "count": count,
            },
            timeout=15,
        )
        resp.raise_for_status()
        import json
        return json.dumps(resp.json().get("data", {}), ensure_ascii=False)
    except Exception as e:
        logger.warning("name_generate 调用失败: %s", e)
        raise RuntimeError("起名服务暂时不可用，请稍后重试") from e
