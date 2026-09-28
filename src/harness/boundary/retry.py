"""
玄镜 Harness Boundary —— 重试回路
落点：oraclemind-ai-py/src/harness/boundary/retry.py
"""
from dataclasses import dataclass
from typing import Callable, Dict, Any, Optional, Tuple

from .validator import BoundaryValidator, BoundaryResult


@dataclass
class RetryConfig:
    max_retries: int = 2
    """无效输出允许的最大重试次数（不含首次生成）。"""


def validate_with_retry(
    validator: BoundaryValidator,
    module: str,
    data: dict,
    *,
    on_invalid: Callable[[BoundaryResult, int], dict],
    config: Optional[RetryConfig] = None,
) -> Tuple[BoundaryResult, int]:
    """
    强校验 + 重试回路（命令式 / 非图场景）。

    :param on_invalid: 校验失败时回调，接收 (失败结果, 第几次重试)，返回修正后的新 data。
                       典型实现：把 result.human_message() 回灌给模型重生成。
    :return: (最终 BoundaryResult, 实际重试次数)
    """
    config = config or RetryConfig()
    result = validator.validate(module, data)
    attempts = 0
    while not result.ok and attempts < config.max_retries:
        attempts += 1
        try:
            data = on_invalid(result, attempts)
        except Exception:
            # 回调失败（如模型再调用抛错）直接终止，保留当前失败结果
            break
        if not isinstance(data, dict):
            break
        result = validator.validate(module, data)
    return result, attempts
