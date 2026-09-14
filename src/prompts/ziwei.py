"""紫微斗数 Prompt

支持 focus 参数以生成聚焦解读：
- None / full: 整盘综合解读（右侧 AI 实时解读面板使用）
- sihua: 四化飞星 · 生年四化 专属解读
- stars: 十四主星 · 北斗南斗 专属解读
"""

from __future__ import annotations

import json

from src.prompts.shared import (
    COMPLIANCE_BLOCK, INPUT_DISCLAIMER, OUTPUT_FORMAT,
    INJECTION_GUARD, REFERENCE_GUIDE, TONE_BLOCK, role_block,
    PromptTemplate,
)

# —— 四化专用的输出格式模板 ——
SIHUA_OUTPUT_FORMAT = """【输出格式】
必须输出合法 JSON（不要输出任何 JSON 以外的文字、不要 markdown 代码块包裹）：
{
  "ok": true,
  "summary": "四化整体概述，≤60字",
  "aspects": [
    {"title":"化禄·星名落宫位","text":"≤120字"},
    {"title":"化权·星名落宫位","text":"≤120字"},
    {"title":"化科·星名落宫位","text":"≤120字"},
    {"title":"化忌·星名落宫位","text":"≤120字"},
    {"title":"四化协调","text":"≤120字"}
  ]
}
若无法解读：{"ok": false, "reason": "简短原因"}"""

# —— 主星专用的输出格式模板 ——
STARS_OUTPUT_FORMAT = """【输出格式】
必须输出合法 JSON（不要输出任何 JSON 以外的文字、不要 markdown 代码块包裹）：
{
  "ok": true,
  "summary": "主星格局概述，≤60字",
  "aspects": [
    {"title":"命宫主星特质","text":"≤120字"},
    {"title":"三方四正格局","text":"≤120字"},
    {"title":"北斗南斗配合","text":"≤120字"},
    {"title":"经典格局印证","text":"逐条引用命理原文，以 markdown 列表呈现：先写 **《典籍·篇目》：原文金句**，再用 1~2 句白话串讲其对本命盘的含义；不要输出 JSON 代码。"}
  ]
}
若无法解读：{"ok": false, "reason": "简短原因"}"""


def _build_system(focus: str | None = None) -> str | None:
    """根据 focus 生成不同的 system prompt，覆盖默认 OUTPUT_FORMAT"""
    if focus == "sihua":
        return f"""{role_block("你是精通紫微斗数四化飞星的命理师，擅长生年四化、宫干飞星解读。")}
{INPUT_DISCLAIMER}
{SIHUA_OUTPUT_FORMAT}
{COMPLIANCE_BLOCK}
{INJECTION_GUARD}
{TONE_BLOCK}"""
    elif focus == "stars":
        return f"""{role_block("你是精通紫微斗数主星的命理师，擅长十四主星性格、北斗南斗、经典格局解读。")}
{INPUT_DISCLAIMER}
{STARS_OUTPUT_FORMAT}
{COMPLIANCE_BLOCK}
{INJECTION_GUARD}
{REFERENCE_GUIDE}
{TONE_BLOCK}"""
    return None  # 使用默认 system


SYSTEM = f"""{role_block("你是精通紫微斗数的命理师，擅长星曜、宫位、四化解读。")}
{INPUT_DISCLAIMER}
{OUTPUT_FORMAT}
{COMPLIANCE_BLOCK}
{INJECTION_GUARD}
{REFERENCE_GUIDE}
{TONE_BLOCK}"""


def _build_user(result: dict, focus: str | None = None) -> str:
    """根据 focus 生成不同的解读 prompt，避免模块间重复"""
    palaces = result.get("palaces", [])
    sihua = result.get("sihua", [])
    patterns = result.get("patterns", [])
    ming_gong = result.get("mingGong", "")
    main_stars_ref = result.get("mainStarsRef", [])
    palace_sihua = result.get("palaceSihua", [])

    if focus == "sihua":
        # —— 四化飞星 · 生年四化：每张卡片逐一解读 ——
        sihua_cards = []
        for s in sihua:
            star = s.get("star", "")
            hua = s.get("hua", "")
            palace = s.get("palace", "")
            if star and hua and palace:
                sihua_cards.append(f"化{hua}｜{star}｜落{palace}")

        cards_text = "、".join(sihua_cards) if sihua_cards else "无"

        # 构建动态 aspects 模板
        aspect_titles = []
        for s in sihua:
            star = s.get("star", "")
            hua = s.get("hua", "")
            palace = s.get("palace", "")
            if star and hua and palace:
                aspect_titles.append(f'    {{\"title\": \"化{hua}·{star}落{palace}\", \"text\": \"≤120字\"}}')
        aspect_titles.append('    {"title": "四化协调", "text": "≤120字"}')

        aspects_json = ",\n".join(aspect_titles)

        lines = [
            "请对以下紫微斗数的「生年四化」进行逐卡解读：",
            f"",
            f"命宫：{ming_gong}",
            f"四化卡片：{cards_text}",
            f"原始数据：{json.dumps(sihua, ensure_ascii=False)}",
            f"各宫位星曜：{json.dumps(palaces, ensure_ascii=False)}",
            f"",
            f"【重要】你的输出 JSON 必须使用以下 aspects 格式（覆盖默认格式）：",
            f'{{"ok": true, "summary": "四化整体概述≤60字", "aspects": [',
            f'{aspects_json}',
            f'  ]}}',
            f"",
            f"各 aspect 解读要求：",
        ]

        # 动态生成四个 aspect 的解读要求
        for s in sihua:
            star = s.get("star", "")
            hua = s.get("hua", "")
            palace = s.get("palace", "")
            if star and hua and palace:
                lines.append(f'- 化{hua}·{star}落{palace}：解读{star}化{hua}落在{palace}宫的含义，包括该宫所主人事领域、吉凶影响、能量流向、星曜庙旺对力量的增减')

        lines.extend([
            f'- 四化协调：分析四个四化之间的能量协调与冲突关系，如禄权同宫、科忌对冲等',
            f"",
            f"注意：只解读四化卡片内容，不要涉及主星性格、大限流年等其他维度。",
        ])
        return "\n".join(lines)

    elif focus == "stars":
        # —— 十四主星 · 北斗南斗：聚焦主星性格与格局 ——
        lines = [
            "请专注解读以下紫微斗数的「十四主星」部分：",
            f"",
            f"命宫：{ming_gong}",
            f"本命盘出现的主星：{json.dumps(main_stars_ref, ensure_ascii=False)}",
            f"各宫位星曜配置：{json.dumps(palaces, ensure_ascii=False)}",
            f"经典格局：{json.dumps(patterns, ensure_ascii=False)}",
            f"",
            f"【重要】你的输出 JSON 必须使用以下 aspects 格式（覆盖默认格式）：",
            f'{{"ok": true, "summary": "主星格局概述≤60字", "aspects": [',
            f'    {{"title": "命宫主星特质", "text": "≤120字"}},',
            f'    {{"title": "三方四正格局", "text": "≤120字"}},',
            f'    {{"title": "北斗南斗配合", "text": "≤120字"}},',
            f'    {{"title": "经典格局印证", "text": "逐条引用命理原文"}}',
            f'  ]}}',
            f"",
            f"各 aspect 解读要求：",
            f'- 命宫主星特质：命宫主星的性格基调、核心特质、庙旺利陷对性格强弱的影响',
            f'- 三方四正格局：三方四正主星组合形成的格局层次与力量',
            f'- 北斗南斗配合：北斗星群与南斗星群的配合与制衡',
            f'- 经典格局印证：主星组合形成的经典格局（如紫府同宫、机月同梁、杀星独坐等）',
            f"",
            f"注意：只解读主星性格与格局内容，不要涉及四化能量、大限流年等其他维度。",
        ]
        return "\n".join(lines)

    elif focus == "timeline":
        # —— 大限 · 流年 · 流月：时间维度分块解读 ——
        dafen = result.get("dafen", [])
        liu_nian = result.get("liuNian", {})
        liu_yue = result.get("liuYue", {})
        liu_nian_sihua = result.get("liuNianSihua", [])
        lines = [
            "请解读以下紫微斗数的「大限·流年·流月」时间维度：",
            "",
            f"命宫：{ming_gong}",
            f"大限（十年一转运）：{json.dumps(dafen, ensure_ascii=False)}",
            f"流年（当前/所选年）：{json.dumps(liu_nian, ensure_ascii=False)}",
            f"流月：{json.dumps(liu_yue, ensure_ascii=False)}",
            f"流年四化：{json.dumps(liu_nian_sihua, ensure_ascii=False)}",
            "",
            "请从三个层次解读：1) 大限走势（十年主题与重心）；2) 流年机遇与风险（当年太岁/命宫与四化）；"
            "3) 流月细化（短期起伏）。帮助用户把握时运节点与应期。",
        ]
        return "\n".join(lines)

    else:
        # —— 整盘综合解读：右侧 AI 实时解读面板 ——
        lines = [
            "请解读以下紫微斗数排盘结果：",
            "",
            f"命宫：{ming_gong}",
            f"十二宫：{json.dumps(palaces, ensure_ascii=False)}",
            f"四化：{json.dumps(sihua, ensure_ascii=False)}",
            f"格局：{json.dumps(patterns, ensure_ascii=False)}",
            "",
            "请从性格、事业财运、情感人际、格局印证四维度解读。",
        ]
        return "\n".join(lines)


ziwei_prompt = PromptTemplate(
    module="ziwei",
    version="3.2",
    system=SYSTEM,
    build_user=_build_user,
    temperature=0.75,
    max_tokens=2048,
    build_system=_build_system,
)
