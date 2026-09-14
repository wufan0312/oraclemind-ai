"""风水堪舆 Prompt"""

from __future__ import annotations

import json

from src.prompts.shared import (
    COMPLIANCE_BLOCK, INPUT_DISCLAIMER, OUTPUT_FORMAT,
    INJECTION_GUARD, TONE_BLOCK, role_block, PromptTemplate,
)

SYSTEM = f"""{role_block("你是精通风水堪舆的命理师，擅长九宫飞星、八宅明镜、形煞分析。")}
{INPUT_DISCLAIMER}
{OUTPUT_FORMAT}
{COMPLIANCE_BLOCK}
{INJECTION_GUARD}
{TONE_BLOCK}"""


def _build_user(result: dict, focus: str | None = None) -> str:
    return f"""请解读以下风水堪舆排盘结果：

维度：{result.get('dimension','')}
流年：{result.get('year','')}
九宫飞星：{json.dumps(result.get('stars',[]), ensure_ascii=False)}
命卦：{json.dumps(result.get('mingGua',{}), ensure_ascii=False)}
八宅：{json.dumps(result.get('bazhai',[]), ensure_ascii=False)}
八字喜用：{json.dumps(result.get('bazi',{}), ensure_ascii=False)}
形煞：{json.dumps(result.get('xingSha',[]), ensure_ascii=False)}
坐向：{result.get('facing','')}

请从该维度（{result.get('dimension','')}）切入，结合飞星、八宅、八字综合分析吉凶并给出风水建议。"""


fengshui_prompt = PromptTemplate(
    module="fengshui",
    version="2.0",
    system=SYSTEM,
    build_user=_build_user,
    temperature=0.75,
    max_tokens=2048,
)
