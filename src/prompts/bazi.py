"""八字命理 Prompt"""

from __future__ import annotations

import json

from src.prompts.shared import (
    COMPLIANCE_BLOCK, INPUT_DISCLAIMER, OUTPUT_FORMAT,
    INJECTION_GUARD, REFERENCE_GUIDE, TONE_BLOCK, role_block,
    PromptTemplate,
)

SYSTEM = f"""{role_block("你是精通传统命理的八字命理师，擅长四柱八字、十神、大运流年分析。")}
{INPUT_DISCLAIMER}
{OUTPUT_FORMAT}
{COMPLIANCE_BLOCK}
{INJECTION_GUARD}
{REFERENCE_GUIDE}
{TONE_BLOCK}"""


def _build_user(result: dict, focus: str | None = None) -> str:
    if focus == "refs":
        return f"""请针对以下排盘结果中的命理原文（检索到的古籍）做白话解读：

{json.dumps(result, ensure_ascii=False, indent=2)}

逐条引用典籍原文，用白话串讲其对命盘的含义。"""

    if focus == "wuxing":
        # 五行能量专属解读
        day_master = result.get("dayMaster", "")
        dm_wuxing = result.get("dayMasterWuxing", "")
        wuxing = result.get("wuxing", [])
        wuxing_count = result.get("wuxingCount", [])
        lacking = result.get("lacking", [])
        yongshen_raw = result.get("yongshen", {})
        if isinstance(yongshen_raw, dict):
            yongshen_text = (
                f"喜={json.dumps(yongshen_raw.get('xi', []), ensure_ascii=False)} "
                f"忌={json.dumps(yongshen_raw.get('ji', []), ensure_ascii=False)}"
            )
        else:
            yongshen_text = str(yongshen_raw)

        return f"""请针对以下八字排盘的五行能量分布做专属解读：

日主：{day_master}（{dm_wuxing}）
五行能量分布：{json.dumps(wuxing, ensure_ascii=False)}
五行缺旺计数：{json.dumps(wuxing_count, ensure_ascii=False)}
缺失五行：{json.dumps(lacking, ensure_ascii=False)}
喜忌用神：{yongshen_text}

请从以下维度做五行能量专属解读：
1. 五行强弱分析：明确指出偏旺与偏弱的五行
2. 缺失五行影响：缺失五行对整体五行平衡的影响
3. 用神调候建议：基于喜忌的调理方向
4. 生活改运建议：饮食、方位、颜色、饰品等
5. 五行平衡策略：如何让五行能量更均衡

请用通俗易懂的语言，给出具体可操作的建议。"""

    # 提取关键字段
    pillars = result.get("pillars", [])
    day_master = result.get("dayMaster", "")
    dm_wuxing = result.get("dayMasterWuxing", "")
    shishen = result.get("shiShen", [])
    wuxing = result.get("wuxing", [])
    dayun = result.get("dayun", [])
    yongshen_raw = result.get("yongshen", {})
    if isinstance(yongshen_raw, dict):
        yongshen_text = (
            f"喜={json.dumps(yongshen_raw.get('xi', []), ensure_ascii=False)} "
            f"忌={json.dumps(yongshen_raw.get('ji', []), ensure_ascii=False)}"
        )
    else:
        yongshen_text = str(yongshen_raw)

    return f"""请解读以下八字排盘结果：

日主：{day_master}（{dm_wuxing}）
四柱：{json.dumps(pillars, ensure_ascii=False)}
十神：{json.dumps(shishen, ensure_ascii=False)}
五行能量：{json.dumps(wuxing, ensure_ascii=False)}
大运：{json.dumps(dayun, ensure_ascii=False)}
喜用神：{yongshen_text}

请从性格、事业财运、情感人际、格局印证四维度解读。"""


bazi_prompt = PromptTemplate(
    module="bazi",
    version="2.0.1",
    system=SYSTEM,
    build_user=_build_user,
    temperature=0.75,
    max_tokens=2048,
)

bazi_refs_explain_prompt = PromptTemplate(
    module="bazi",
    version="2.0.1-refs",
    system=SYSTEM,
    build_user=lambda r, f=None: _build_user(r, "refs"),
    temperature=0.7,
    max_tokens=1536,
)
