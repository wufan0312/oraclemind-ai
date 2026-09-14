"""大六壬 Prompt（三式之一）"""

from __future__ import annotations

from src.prompts.shared import (
    COMPLIANCE_BLOCK, INPUT_DISCLAIMER, OUTPUT_FORMAT,
    INJECTION_GUARD, REFERENCE_GUIDE, TONE_BLOCK, role_block,
    PromptTemplate,
)

SYSTEM = f"""{role_block("你是精通大六壬的命理师，擅长天地盘、四课三传、九宗门发用、十二天将与六亲分析。")}
{INPUT_DISCLAIMER}
{OUTPUT_FORMAT}
{COMPLIANCE_BLOCK}
{INJECTION_GUARD}
{REFERENCE_GUIDE}
{TONE_BLOCK}"""


def _build_user(result: dict, focus: str | None = None) -> str:
    sike = result.get("siKe") or []
    san = result.get("sanChuan") or {}
    items = san.get("items") or []
    tp = result.get("tianPan") or []
    tj = result.get("tianJiang") or []

    sike_txt = "\n".join(
        f"{k.get('label','')}：{k.get('lower','')}（{k.get('lowerWuxing','')}）上得"
        f"{k.get('upper','')}（{k.get('upperWuxing','')}）　{k.get('relationText','')}"
        for k in sike
    ) or "无"

    chuan_txt = "\n".join(
        f"{it.get('label','')}：{it.get('zhi','')}{it.get('gan','')}（{it.get('wuxing','')}）"
        f"　将 {it.get('jiang','')}　六亲 {it.get('liuqin','')}"
        f"{'　【落空亡】' if it.get('kongWang') else ''}"
        for it in items
    ) or "无"

    tp_txt = "\n".join(
        f"{p.get('zhi','')}：{p.get('shen','')}{p.get('gan','')}　将 {p.get('jiang','')}"
        for p in tp
    ) or "无"

    tj_txt = "、".join(f"{t.get('zhi','')}{t.get('jiang','')}" for t in tj) or "无"

    focus_line = f"\n求测重点：{focus}" if focus else ""

    return f"""请解读以下大六壬课式（已起课完毕，请勿重新推算）：

节气：{result.get('jieqi','')}　月将：{result.get('yueJiang','')}（{result.get('yueJiangName','')}）　占时：{result.get('zhanShi','')}时
日辰：{result.get('riGanZhi','')}　日干{result.get('riGan','')}寄{result.get('jiGong','')}宫
贵人：{result.get('guiRen',{}).get('zhi','')}（{result.get('guiRen',{}).get('dayNight','')}）
空亡：{'、'.join(result.get('kongWang') or []) or '无'}
伏吟：{'是' if result.get('fuYin') else '否'}　反吟：{'是' if result.get('fanYin') else '否'}{focus_line}

【四课】
{sike_txt}

【三传】（{san.get('method','')} · {san.get('keTi','')}，自{san.get('fromKe','')}发用）
{chuan_txt}
发用说明：{san.get('desc','')}

【天地盘】
{tp_txt}

【十二天将】
{tj_txt}

请从课体吉凶、三传递进（初传为事之始、中传为事之中、末传为事之终）、天将善恶、
六亲生克、空亡虚实五个维度，结合求测者所问之事，给出温和、专业的解读。"""


liuren_prompt = PromptTemplate(
    module="liuren",
    version="1.0",
    system=SYSTEM,
    build_user=_build_user,
    temperature=0.75,
    max_tokens=2048,
)
