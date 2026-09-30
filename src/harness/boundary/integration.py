"""
玄镜 Harness Boundary —— 接入 ai-py 现有结构化生成链路
落点：oraclemind-ai/src/harness/boundary/integration.py

现有 src.services.structured.generate_structured 已做 LLM 调用 + JSON 解析 + 重试一次，
但只校验「能否解析成 JSON」，不校验「是否符合业务契约」。本模块在其之上加一层契约校验：
无效则把 human_message() 回灌，触发一次修正生成（复用原链路，不新增供应商压力）。
"""
from __future__ import annotations

from typing import Optional, Tuple

from .validator import BoundaryValidator

_VALIDATOR = BoundaryValidator()


def get_validator() -> BoundaryValidator:
    return _VALIDATOR


async def generate_structured_with_boundary(
    contract: str,
    system: str,
    user: str,
    *,
    temperature: float = 0.8,
    max_tokens: int = 1280,
    max_boundary_retries: int = 1,
) -> Tuple[Optional[dict], Optional[str], bool, Optional[int], Optional[str], Optional[str]]:
    """调用 generate_structured 并做 Boundary 契约校验。

    Args:
        contract: 契约键，对应 MODULE_SCHEMAS（如 "astro_natal" / "report"）。
        system / user: 原样透传给 generate_structured。
        max_boundary_retries: 契约不通过时，最多回灌修正几次（不含首次）。

    Returns:
        (parsed, error_code, degraded, tokens, provider, model)
        - degraded=True 表示重试后仍不满足契约，调用方应降级而非信任该数据。
        - tokens/provider/model 透传自 generate_structured，便于调用方做成本核算。
    """
    from src.services.structured import generate_structured  # 延迟导入：避免重型依赖链

    parsed, err, tokens, provider, model = await generate_structured(
        system, user, temperature=temperature, max_tokens=max_tokens, retries=1
    )
    if parsed is None:
        return None, err or "LLM_FAIL", True, tokens, provider, model

    res = _VALIDATOR.validate(contract, parsed)
    attempts = 0
    while not res.ok and attempts < max_boundary_retries:
        attempts += 1
        feedback = res.human_message()
        retry_user = f"{user}\n\n[系统] {feedback}"
        parsed2, err2, tokens, provider, model = await generate_structured(
            system, retry_user, temperature=temperature, max_tokens=max_tokens, retries=1
        )
        if parsed2 is None:
            return parsed, err2 or "LLM_FAIL", True, tokens, provider, model
        parsed = parsed2
        res = _VALIDATOR.validate(contract, parsed)

    if not res.ok:
        return parsed, "BOUNDARY_FAIL", True, tokens, provider, model
    return parsed, None, False, tokens, provider, model
