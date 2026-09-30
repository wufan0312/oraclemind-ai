"""P2 质量门：把指标聚合成可判定的 QualityReport，并支持对数据集批量评估。

阈值与必含章节均可外部注入（来自 golden.json / CI 配置），默认值覆盖玄镜主流模块。
"""
from __future__ import annotations

from typing import Any

from .metrics import (
    format_compliance,
    hallucination_keyword_rate,
    orphaned_heading_rate,
)

DEFAULT_THRESHOLDS: dict[str, Any] = {
    "max_orphaned_rate": 0.1,  # 孤立编号率上限
    "forbidden_keywords": [  # 命中即判越界（0 容忍）
        # 身份冒充：命理/占星 AI 不得自称 AI / 人工智能（输出须为解读口吻）
        "作为人工智能",
        "我是一个AI",
        "我是人工智能",
        # 医疗越界：命理/占星 AI 不得给出医疗建议（强合规）
        "请咨询专业医生",
        "请咨询专业医师",
        # 封建迷信骗财话术（对应合规三要件：消灾方法/物品 + 获利引流）
        "包治百病",
        "保证有效",
        "百分百准",
        "一定发财",
        "破财消灾",
        "改运收费",
        # 注：http(s):// 外链交由输出层「外链硬清洗」统一处理，此处不再内容层硬禁，
        #     避免与清洗层重叠且层级错位；「免责声明」为中性词易误杀合规文案，已移除。
    ],
}

DEFAULT_REQUIRED_SECTIONS: dict[str, list[str]] = {
    "tarot": ["正位", "逆位", "建议"],
    "bazi": ["八字", "五行", "大运"],
    "ziwei": ["命宫", "格局", "建议"],
}


class QualityGate:
    def __init__(
        self,
        thresholds: dict | None = None,
        required_sections: dict | None = None,
    ) -> None:
        self.thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        self.required_sections = {**DEFAULT_REQUIRED_SECTIONS, **(required_sections or {})}

    def evaluate(self, text: str, module: str = "") -> dict:
        # 1) 格式合规
        req = self.required_sections.get(module, [])
        fmt = format_compliance(text, req)
        # 2) 孤立编号率
        orphan = orphaned_heading_rate(text)
        # 3) 幻觉关键词
        hallu = hallucination_keyword_rate(text, self.thresholds["forbidden_keywords"])

        checks: list[dict] = []
        if req:
            checks.append(
                {
                    "name": "format_compliance",
                    "passed": fmt["compliant"],
                    "detail": {"missing": fmt["missing"]},
                }
            )
        checks.append(
            {
                "name": "orphaned_heading_rate",
                "passed": orphan["rate"] <= self.thresholds["max_orphaned_rate"],
                "detail": orphan,
            }
        )
        checks.append(
            {
                "name": "hallucination_keyword",
                "passed": not hallu["has_hallucination"],
                "detail": {"total_hits": hallu["total_hits"], "hits": hallu["hits"]},
            }
        )
        passed = all(c["passed"] for c in checks)
        failed = sum(1 for c in checks if not c["passed"])
        score = 1.0 if passed else max(0.0, 1.0 - 0.34 * failed)
        return {
            "module": module,
            "passed": passed,
            "score": round(score, 3),
            "checks": checks,
        }

    def evaluate_dataset(self, samples: list[dict]) -> dict:
        """samples: [{name, module, text, expect_pass}] -> 聚合报告。

        expect_pass 用于回归敏感性：good 样本应 passed=true，bad 样本应 passed=false；
        全部符合则 overall_passed=true，否则 mismatches>0。
        """
        per_sample: list[dict] = []
        mismatches = 0
        for s in samples:
            r = self.evaluate(s.get("text", ""), s.get("module", ""))
            expect = s.get("expect_pass")
            match = (expect is None) or (r["passed"] == expect)
            if not match:
                mismatches += 1
            per_sample.append(
                {**r, "name": s.get("name", ""), "expect_pass": expect, "match": match}
            )
        return {
            "overall_passed": mismatches == 0,
            "mismatches": mismatches,
            "samples": per_sample,
        }
