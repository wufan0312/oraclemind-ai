"""奇门遁甲 Prompt"""

from __future__ import annotations

import json

from src.prompts.shared import (
    COMPLIANCE_BLOCK, INPUT_DISCLAIMER, OUTPUT_FORMAT,
    INJECTION_GUARD, REFERENCE_GUIDE, TONE_BLOCK, role_block,
    PromptTemplate,
)

SYSTEM = f"""{role_block("你是精通奇门遁甲的命理师，擅长九宫、八门、九星、八神、三奇六仪分析。")}
{INPUT_DISCLAIMER}
{OUTPUT_FORMAT}
{COMPLIANCE_BLOCK}
{INJECTION_GUARD}
{REFERENCE_GUIDE}
{TONE_BLOCK}"""


def _build_user(result: dict, focus: str | None = None) -> str:
    palaces = result.get("palaces", []) or []
    qi = result.get("qi", []) or []
    vf = result.get("valueFu", "")
    vs = result.get("valueShi", "")
    ys = result.get("yongShen")
    pats = result.get("patterns") or []
    kw = result.get("kongWang")
    ms = result.get("maStar")
    ws = result.get("wangShuai")
    yq = result.get("yingqi")

    palace_txt = "\n".join(
        f"{p.get('dir','')}宫：{p.get('star','')}｜门{p.get('door','')}｜神{p.get('god','')}｜"
        f"天盘{p.get('tianpan','')} 地盘{p.get('dipan','')}｜{p.get('jixiong','')}"
        for p in palaces
    ) or "无"
    qi_txt = "\n".join(f"{q.get('title','')}：{q.get('text','')}" for q in qi) or "无"

    ys_txt = "无"
    if ys:
        ys_txt = "\n".join(
            f"- {it.get('name','')}：落{it.get('gongName','')}（{it.get('wuxing','')}），关系{it.get('relation','')}，"
            f"旺衰{it.get('wang','')}，吉凶{it.get('jixiong','')}；{it.get('note','')}"
            for it in (ys.get("items") or [])
        ) or "无"

    pat_txt = "无"
    if pats:
        pat_txt = "\n".join(
            f"- {p.get('gongName','')}宫（天{p.get('tianpan','')}/地{p.get('dipan','')}）：{p.get('name','')}（{p.get('level','')}）— {p.get('desc','')}"
            for p in pats
        )

    kw_txt = (kw or {}).get("desc", "无") if kw else "无"
    ms_txt = (ms or {}).get("desc", "无") if ms else "无"
    ws_txt = (ws or {}).get("desc", "无") if ws else "无"
    if ws and ws.get("yong"):
        ws_txt += "\n" + "\n".join(
            f"- {y.get('name','')}（{y.get('wx','')}）：{y.get('state','')}" for y in ws.get("yong", [])
        )

    yq_txt = "无"
    if yq:
        yq_txt = (yq.get("summary") or "")
        pts = yq.get("points") or []
        if pts:
            yq_txt += "\n" + "\n".join(f"- {pt}" for pt in pts)

    focus_line = f"\n求测重点：{focus}" if focus else ""

    return f"""请解读以下奇门遁甲排盘结果（已计算好，请勿重新推算）：

盘面：{result.get('type','')}　值符：{vf}　值使：{vs}
时干支：{result.get('shiGanZhi','')}　日干支：{result.get('riGanZhi','')}{focus_line}

【九宫】
{palace_txt}

【三奇方位】
{qi_txt}

【用神立极】（{ys.get('category','通用') if ys else '通用'}）
{ys_txt}

【奇仪格局】
{pat_txt}

【空亡】{kw_txt}
【驿马星】{ms_txt}

【月令旺衰·生克】
{ws_txt}

【应期推断】
{yq_txt}

请从用神旺衰、奇仪格局吉凶、空亡马星虚实、应期远近四维度，结合求测者所问之事，给出温和、专业的解读。"""


qimen_prompt = PromptTemplate(
    module="qimen",
    version="2.0",
    system=SYSTEM,
    build_user=_build_user,
    temperature=0.75,
    max_tokens=2048,
)
