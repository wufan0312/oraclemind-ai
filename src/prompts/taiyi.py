"""太乙神数 Prompt（三式之一）

注意：太乙流派分歧极大（积年基准 / 文昌起例 / 主客算法各本不同）。
本 prompt 明确要求 AI **不得把所采之说当作唯一正统**，也不得据此给出确定性的吉凶断言。
"""

from __future__ import annotations

from src.prompts.shared import (
    COMPLIANCE_BLOCK, INPUT_DISCLAIMER, OUTPUT_FORMAT,
    INJECTION_GUARD, REFERENCE_GUIDE, TONE_BLOCK, role_block,
    PromptTemplate,
)

SYSTEM = f"""{role_block("你是熟悉太乙神数的研究者，了解太乙积年、行宫、阴阳遁、文昌始击、主客算与主客将的基本义理。")}
{INPUT_DISCLAIMER}
{OUTPUT_FORMAT}
{COMPLIANCE_BLOCK}
{INJECTION_GUARD}
{REFERENCE_GUIDE}
{TONE_BLOCK}

【太乙专有的表述约束】
1. 太乙流派分歧极大，积年基准、文昌起例、主客算法各本不同。解读时必须说明这是
   「按某一流派起例所推」，不得把结果表述为唯一正统或确定无疑的结论。
2. 禁止把主客算的大小直接等同于现实中的胜负、输赢、盈亏等确定性断言；
   只作趋势性、倾向性的参考描述。
3. 若排盘结果中 provenance 字段声明了「所采之说」，解读中应体现这种不确定性。"""


def _build_user(result: dict, focus: str | None = None) -> str:
    ty = result.get("taiYiGong") or {}
    wc = result.get("wenChang") or {}
    sj = result.get("shiJi") or {}
    zd = result.get("zhuDaJiang") or {}
    kd = result.get("keDaJiang") or {}
    zc = result.get("zhuCanJiang") or {}
    kc = result.get("keCanJiang") or {}
    gong = result.get("shiLiuGong") or []

    gong_txt = "\n".join(
        f"{g.get('pos','')}（{g.get('shen','')}）：{g.get('desc','')}"
        + (f"　★ 落 {'、'.join(g.get('stars') or [])}" if g.get("stars") else "")
        for g in gong
    ) or "无"

    focus_line = f"\n求测重点：{focus}" if focus else ""

    return f"""请解读以下太乙神数年计排局（已排好，请勿重新推算）：

公元年干支：{result.get('ganZhi','')}　太乙积年：{result.get('jiNian','')}
局序：七十二局之第 {result.get('ju','')} 局（小周第 {result.get('xiaoZhou','')} 年）
太乙居：第{ty.get('gong','')}宫 {ty.get('gua','')}（{ty.get('fang','')}·{ty.get('pos','')}位，{ty.get('shen','')}）　{result.get('dun','')}
文昌（主目）：{wc.get('pos','')}位 {wc.get('shen','')} — {wc.get('desc','')}
始击（客目）：{sj.get('pos','')}位 {sj.get('shen','')} — {sj.get('desc','')}
计神：{result.get('jiShen',{}).get('pos','')}　合神：{result.get('heShen',{}).get('pos','')}
主算：{result.get('zhuSuan','')}　客算：{result.get('keSuan','')}
主大将：{zd.get('gong','')}宫{zd.get('gua','')}　主参将：{zc.get('gong','')}宫{zc.get('gua','')}
客大将：{kd.get('gong','')}宫{kd.get('gua','')}　客参将：{kc.get('gong','')}宫{kc.get('gua','')}{focus_line}

【十六宫神位】
{gong_txt}

【本局断语】{result.get('verdict','')}
【遁法说明】{result.get('dunDesc','')}

【流派声明】{result.get('provenance','')}

请从太乙居宫与阴阳遁、文昌始击的主客关系、主客算的强弱对比、主客将所在宫位四个维度，
结合求测者所问之事，给出**倾向性**的参考解读。务必体现流派不确定性，不作确定性断言。"""


taiyi_prompt = PromptTemplate(
    module="taiyi",
    version="1.0",
    system=SYSTEM,
    build_user=_build_user,
    temperature=0.7,
    max_tokens=2048,
)
