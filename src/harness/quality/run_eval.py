"""P2 质量门 CLI：加载 golden set → 评估 → 输出 report.md + report.json。

运行（任选其一）：
    cd oraclemind-ai-py && PYTHONPATH=. python -m src.harness.quality.run_eval
    cd oraclemind-ai-py && PYTHONPATH=. python src/harness/quality/run_eval.py [golden.json 路径]

退出码：全部样本符合期望 → 0；存在期望不符 → 1（可作 CI 质量门失败信号）。
"""
from __future__ import annotations

import json
import os
import sys

try:
    from .gate import QualityGate
except ImportError:  # 直接 `python src/harness/quality/run_eval.py` 时退化为绝对导入
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
    from src.harness.quality.gate import QualityGate  # type: ignore

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_GOLDEN = os.path.normpath(
    os.path.join(_HERE, "..", "..", "..", "tests", "eval", "golden.json")
)


def load_golden(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def render_report(result: dict, golden: dict) -> str:
    lines = ["# 玄镜 AI 输出质量门报告（P2 Harness 化）", ""]
    lines.append(
        f"- 总体判定：**{'通过 ✅' if result['overall_passed'] else '未通过 ❌'}**"
    )
    lines.append(f"- 期望不符样本数：{result['mismatches']}")
    lines.append("")
    lines.append("| 样本 | 模块 | 通过 | 期望 | 符合 | 评分 |")
    lines.append("|---|---|---|---|---|---|")
    for s in result["samples"]:
        lines.append(
            f"| {s['name']} | {s.get('module', '')} "
            f"| {'✅' if s['passed'] else '❌'} "
            f"| {s.get('expect_pass')} "
            f"| {'✅' if s.get('match') else '❌'} "
            f"| {s['score']} |"
        )
    lines.append("")
    lines.append("## 指标说明")
    lines.append("- format_compliance：必含章节齐备性（格式合规率）")
    lines.append("- orphaned_heading_rate：孤立编号标题占比（孤立编号率，阈值上限 0.1）")
    lines.append("- hallucination_keyword：越界 / 幻觉关键词命中（0 容忍）")
    lines.append("")
    lines.append("## 各样本检查明细")
    for s in result["samples"]:
        lines.append(f"### {s['name']}（{s.get('module', '')}）")
        for c in s["checks"]:
            lines.append(f"- {c['name']}: {'✅' if c['passed'] else '❌'} {c['detail']}")
        lines.append("")
    return "\n".join(lines)


def main(golden_path: str | None = None) -> int:
    golden_path = golden_path or _DEFAULT_GOLDEN
    golden = load_golden(golden_path)
    gate = QualityGate(golden.get("thresholds"), golden.get("required_sections"))
    result = gate.evaluate_dataset(golden.get("samples", []))
    report_md = render_report(result, golden)
    out_dir = os.path.dirname(os.path.abspath(golden_path))
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write(report_md)
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(report_md)
    print(f"\n报告已写入：{out_dir}/report.md, report.json")
    return 0 if result["overall_passed"] else 1


if __name__ == "__main__":
    gp = sys.argv[1] if len(sys.argv) > 1 else None
    raise SystemExit(main(gp))
