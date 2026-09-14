"""六爻 Prompt"""

from __future__ import annotations

import json

from src.prompts.shared import (
    COMPLIANCE_BLOCK, INPUT_DISCLAIMER, OUTPUT_FORMAT,
    INJECTION_GUARD, REFERENCE_GUIDE, TONE_BLOCK, role_block,
    PromptTemplate,
)

SYSTEM = f"""{role_block("你是精通六爻占卜的命理师，擅长用神、世应、动爻分析。")}
{INPUT_DISCLAIMER}
{OUTPUT_FORMAT}
{COMPLIANCE_BLOCK}
{INJECTION_GUARD}
{REFERENCE_GUIDE}
{TONE_BLOCK}"""


def _build_user(result: dict, focus: str | None = None) -> str:
    bg = result.get("benGua") if isinstance(result.get("benGua"), dict) else {}
    bian = result.get("bianGua") if isinstance(result.get("bianGua"), dict) else {}
    lines = result.get("lines", [])
    ys = result.get("yongshen", {})
    dong = result.get("dongYao", 0)

    return f"""请解读以下六爻排盘结果：

本卦：{bg.get('name','')}（{bg.get('symbol','')}） {bg.get('gong','')}宫
变卦：{bian.get('name','') if bian else '无'}
动爻：第{dong}爻动
六爻：{json.dumps(lines, ensure_ascii=False)}
用神：{json.dumps(ys, ensure_ascii=False)}

请从卦象、用神旺衰、动爻变化、吉凶倾向四维度解读。"""


liuyao_prompt = PromptTemplate(
    module="liuyao",
    version="2.0",
    system=SYSTEM,
    build_user=_build_user,
    temperature=0.75,
    max_tokens=2048,
)
