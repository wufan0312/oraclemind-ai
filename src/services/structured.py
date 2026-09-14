"""玄镜 OracleMind · 结构化 JSON 生成助手

对应 TS 版 services/interpret.ts 的 attemptGenerate：
主/备用供应商降级链 + 注入防护 + JSON 解析 + 失败重试一次。

返回 (parsed_dict, error_code, tokens, provider, model)。
- error_code=None 表示成功
- 'PARSE_FAIL' = JSON 解析失败（触发重试）
- 'LLM_FAIL'   = 所有供应商不可用
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from src.config import config
from src.services.provider import llm_provider

logger = logging.getLogger(__name__)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _extract_json(text: str) -> dict:
    """从 LLM 输出中提取并解析 JSON（兼容 ```json 围栏 与裸 JSON）。"""
    text = (text or "").strip()
    m = _JSON_FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    # 兜底：截取第一个 { 到最后一个 }
    if not text.startswith("{"):
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e != -1:
            text = text[s : e + 1]
    return json.loads(text)


async def generate_structured(
    system: str,
    user: str,
    *,
    temperature: float = 0.8,
    max_tokens: int = 1280,
    retries: int = 1,
) -> tuple[Optional[dict], Optional[str], Optional[dict], Optional[str], Optional[str]]:
    """调用 LLM 并解析为结构化 dict。

    Returns:
        (parsed, error_code, tokens, provider, model)
    """
    if not config.llm_available:
        return None, "LLM_FAIL", None, None, None

    last_err: Optional[str] = None
    for attempt in range(retries + 1):
        try:
            result = await llm_provider.chat(
                system_prompt=system,
                user_prompt=user,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout_ms=config.total_timeout_ms,
            )
            content = result.get("content", "")
            try:
                parsed = _extract_json(content)
            except Exception as e:  # 解析失败
                last_err = "PARSE_FAIL"
                logger.warning(f"结构化解析失败(尝试{attempt + 1}): {e}")
                continue
            return parsed, None, result.get("tokens"), result.get("provider"), result.get("model")
        except Exception as e:  # 供应商失败
            last_err = "LLM_FAIL"
            logger.warning(f"结构化 LLM 调用失败(尝试{attempt + 1}): {e}")
            continue

    return None, last_err, None, None, None
