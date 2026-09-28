"""P2 质量门单测：golden set 敏感性验证（good 应过、bad 应拦）。

运行（受管 venv，纯标准库无额外依赖）：
    cd oraclemind-ai-py && PYTHONPATH=. python tests/test_harness_quality_gate.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # ai-py 根
from src.harness.quality.gate import QualityGate

_GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval", "golden.json")


def main() -> None:
    with open(_GOLDEN, "r", encoding="utf-8") as f:
        golden = json.load(f)
    gate = QualityGate(golden.get("thresholds"), golden.get("required_sections"))
    samples = golden.get("samples", [])
    passed = 0
    for s in samples:
        r = gate.evaluate(s.get("text", ""), s.get("module", ""))
        expect = s.get("expect_pass")
        ok = (expect is None) or (r["passed"] == expect)
        if ok:
            passed += 1
        print(
            f"{'PASS' if ok else 'FAIL'} {s.get('name')}: "
            f"passed={r['passed']} expect={expect} score={r['score']}"
        )
    print(f"\n{passed}/{len(samples)} PASS")
    if passed != len(samples):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
