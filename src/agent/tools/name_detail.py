"""名字详批工具 — Agent 调用"""

from __future__ import annotations

import logging

from src.config import config

logger = logging.getLogger(__name__)


def name_detail(name: str) -> str:
    """查询名字的笔画、五行、三才五格等详细信息

    Args:
        name: 完整名字（不含姓，如"子涵"）
    """
    import httpx

    try:
        resp = httpx.get(
            f"{config.paipan_api_base}/api/v1/ming/name/detail",
            params={"name": name},
            timeout=10,
        )
        resp.raise_for_status()
        import json
        return json.dumps(resp.json().get("data", {}), ensure_ascii=False)
    except Exception as e:
        logger.warning("name_detail 调用失败: %s", e)
        raise RuntimeError("名字详批服务暂时不可用，请稍后重试") from e
