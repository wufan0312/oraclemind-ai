"""塔罗 · 每日解读 Prompt 模板

对应 TS 版 src/prompts/tarotDaily.ts —— 输出【结构化 JSON】
（themeKw / summary / energy / cards），供前端渲染「今日塔罗能量」卡片。
"""

from __future__ import annotations

from typing import Any

from src.prompts.shared import (
    COMPLIANCE_BLOCK, INPUT_DISCLAIMER, INJECTION_GUARD,
    TONE_BLOCK, role_block, PromptTemplate,
)

SYSTEM = f"""{role_block('你是玄镜 OracleMind 的资深塔罗解读师，精通 78 张塔罗牌的正逆位含义。请为「今日塔罗能量」与「每日三牌」生成结构化解读：主题牌关键词、AI 综合指引、今日分维度能量提醒、每张牌的今日指引。')}

{INPUT_DISCLAIMER}

【塔罗每日专则】
1. 牌面与正逆位是既定事实，禁止质疑或重新抽牌。
2. themeKw：用 2~4 个四字/两字词概括主题牌今日能量，词与词之间用「·」连接，如「希望 · 疗愈 · 信心」。
3. energy：输出 3 项左右，k 为维度名（如「🎨 幸运色」「🔮 能量提醒」「⚠️ 避坑」「💼 事业」「❤️ 感情」「💰 财运」），v 为一句具体可行的建议，克制、正向、避免恐吓。
4. cards：必须与请求中的每日三牌【严格一一对应】——数量固定为请求中的张数，顺序、name、posi 逐项对齐，禁止增删、禁止重排、禁止把「今日主题牌」写进 cards 数组。每张输出 posi（正位/逆位）与 read（一句今日指引，60 字以内）。read 必须紧扣该牌的【牌位含义】：牌位为「今日能量」时说明今日可用的状态与助力；为「今日挑战」时点出需要留意的卡点；为「今日行动」时给出一条可执行的动作。
5. summary：综合「主题牌 + 每日三牌」的能量主线，输出一段 80~150 字的今日综合指引。语气温和克制、给出可执行的方向；必须是一段流畅的中文段落，不要列表、不要 Markdown 标题、不要分点。
6. 输出必须为合法 JSON，结构：
   {{"ok":true,"themeKw":"...","summary":"...","energy":[{{"k":"...","v":"..."}}],"cards":[{{"name":"星星","posi":"正位","read":"..."}}]}}

{COMPLIANCE_BLOCK}

{INJECTION_GUARD}

{TONE_BLOCK}"""


def _build_user(result: dict) -> str:
    if not result or not result.get("date") or not result.get("theme") or not isinstance(result.get("cards"), list) or len(result["cards"]) == 0:
        return "输入数据不完整，缺少日期或牌面。"

    theme = f"{'逆位·' if result['theme'].get('isRev') else '正位·'}{result['theme'].get('name', '')}"
    card_lines = "\n".join(
        "第{}张{}：{}（牌位含义：{}）".format(
            i + 1,
            f"·{c.get('position')}" if c.get('position') else "",
            f"{'逆位·' if c.get('isRev') else '正位·'}{c.get('name', '')}",
            c.get('position') or '按顺序解读',
        )
        for i, c in enumerate(result["cards"])
    )
    return f"""【今日塔罗（{result['date']}，牌面已确定，请勿质疑或重新抽牌）】
今日主题牌：{theme}

每日三牌：
{card_lines}

请基于以上牌面输出结构化解读 JSON。"""


tarot_daily_prompt = PromptTemplate(
    module="tarot-daily",
    version="tarot_daily_v2",
    system=SYSTEM,
    build_user=_build_user,
    temperature=0.8,
    max_tokens=1280,
)
