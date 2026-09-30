"""玄镜 Harness Quality —— 实时接入 helper

在结构化生成服务（src/services/astrology.py、summary.py、tarot_daily.py）生成后调用：
对最终文本做一次 QualityGate 评估，记录到当前激活的 Tracer（无 tracer 时仅 logger），
异常全部 no-op，绝不拖垮主业务。

开关：环境变量 HARNESS_QUALITY_ENABLED（默认 "1" 开启；置 "0" 关闭）。
"""
from __future__ import annotations

import json
import logging
import os

from src.harness.observability.tracer import get_tracer
from .gate import QualityGate

_logger = logging.getLogger(__name__)
_gate = QualityGate()


def maybe_quality_check(module: str, text: str) -> None:
    """对生成文本做质量门禁评估。fail-safe：异常 / 未开启 / 空文本均直接返回。"""
    if os.environ.get("HARNESS_QUALITY_ENABLED", "1") == "0":
        return
    if not text:
        return
    try:
        report = _gate.evaluate(text, module)
        tracer = get_tracer()
        if tracer is not None:
            tracer.record_step(
                "quality",
                module,
                status="ok" if report["passed"] else "warn",
                meta={
                    "passed": report["passed"],
                    "score": report["score"],
                    "checks": report["checks"],
                },
            )
        if not report["passed"]:
            _logger.warning(
                f"[harness:quality] 未通过 module={module} score={report['score']} "
                f"checks={report['checks']}"
            )
    except Exception:  # noqa: BLE001
        pass
