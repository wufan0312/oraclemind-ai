"""梅花易数 Prompt"""

from __future__ import annotations

import json

from src.prompts.shared import (
    COMPLIANCE_BLOCK, INPUT_DISCLAIMER, OUTPUT_FORMAT,
    INJECTION_GUARD, REFERENCE_GUIDE, TONE_BLOCK, role_block,
    PromptTemplate,
)

SYSTEM = f"""{role_block("你是精通梅花易数的命理师，擅长体用生克、互变卦分析。")}
{INPUT_DISCLAIMER}
{OUTPUT_FORMAT}
{COMPLIANCE_BLOCK}
{INJECTION_GUARD}
{REFERENCE_GUIDE}
{TONE_BLOCK}"""


def _build_user(result: dict, focus: str | None = None) -> str:
    """梅花易数完整盘面注入（M11 补全：互变/旺衰/类象/四卦矩阵/应期/卦辞）。"""
    bg = result.get("benGua") if isinstance(result.get("benGua"), dict) else {}
    bian = result.get("bianGua") if isinstance(result.get("bianGua"), dict) else {}
    hu = result.get("huGua") if isinstance(result.get("huGua"), dict) else {}
    cuo = result.get("cuoGua") if isinstance(result.get("cuoGua"), dict) else {}
    zong = result.get("zongGua") if isinstance(result.get("zongGua"), dict) else {}
    ti = result.get("ti") if isinstance(result.get("ti"), dict) else {}
    yong = result.get("yong") if isinstance(result.get("yong"), dict) else {}
    ty = result.get("tiYong") if isinstance(result.get("tiYong"), dict) else {}
    tiw = result.get("tiWang") if isinstance(result.get("tiWang"), dict) else {}
    yw = result.get("yongWang") if isinstance(result.get("yongWang"), dict) else {}
    ws = result.get("wangshuai") if isinstance(result.get("wangshuai"), dict) else {}
    rm = result.get("relationMatrix") if isinstance(result.get("relationMatrix"), list) else []
    yq = result.get("yingqi") if isinstance(result.get("yingqi"), dict) else {}

    def _lx(tv: dict) -> str:
        d = (tv or {}).get("leiXiang") or {}
        if not d:
            return ""
        keys = ("renwu", "fangwei", "shenghti", "wu", "shuzi", "tianshi")
        return "；".join(f"{k}:{d[k]}" for k in keys if d.get(k))

    lines = [
        "请解读以下梅花易数排盘结果（已计算好，请勿重新推算）：",
        "",
        f"本卦：{bg.get('name','')}（{bg.get('symbol','')}）{bg.get('desc','')}　卦辞：{bg.get('guaCi','') or '—'}",
        f"变卦：{bian.get('name','') or '无'}（{bian.get('symbol','')}）{bian.get('desc','')}　卦辞：{bian.get('guaCi','') or '—'}",
        f"互卦：{hu.get('name','') or '无'}（{hu.get('symbol','')}）{hu.get('desc','')}",
        f"错卦：{cuo.get('name','') or '无'}（{cuo.get('symbol','')}）{cuo.get('desc','')}",
        f"综卦：{zong.get('name','') or '无'}（{zong.get('symbol','')}）{zong.get('desc','')}",
        "",
        f"体卦：{ti.get('name','')}（{ti.get('wuxing','')}·{ti.get('meaning','')}·{ti.get('virtue','')}）　类象：{_lx(ti)}",
        f"用卦：{yong.get('name','')}（{yong.get('wuxing','')}·{yong.get('meaning','')}·{yong.get('virtue','')}）　类象：{_lx(yong)}",
        f"动爻：第{result.get('dongYao',0)}爻　爻题：{result.get('dongYaoTitle','') or '—'}　爻辞：{result.get('dongYaoCi','') or '—'}",
        f"体用关系：{ty.get('relation','')}　吉凶：{ty.get('ji','')}（原始：{ty.get('rawJi','') or '同'}，旺衰修正：{'是' if ty.get('jiAdjusted') else '否'}）",
        "",
        "【体用旺衰】",
    ]
    if tiw:
        lines.append(
            f"体卦：月令{tiw.get('yueWang','')}／日辰{tiw.get('dayRelation','')}／综合气势{tiw.get('strength','')}；"
            f"用卦：月令{yw.get('yueWang','')}／日辰{yw.get('dayRelation','')}／综合气势{yw.get('strength','')}"
        )
        lines.append(
            f"月令{ws.get('monthZhi','')}　日辰{ws.get('dayGan','')}{ws.get('dayZhi','')}（{ws.get('dayWuxing','')}）"
            f"　旬空{ws.get('xunkong','')}　体空：{ws.get('tiEmpty')}　用空：{ws.get('yongEmpty')}"
        )
    else:
        lines.append("（无旺衰数据）")

    if rm:
        lines.append("")
        lines.append("【体用互变四卦关系矩阵】")
        for r in rm:
            lines.append(f"{r.get('from','')}→{r.get('to','')}：{r.get('ji','')}（{r.get('detail','')}）")

    if yq:
        lines.append("")
        lines.append("【应期推断】")
        lines.append(yq.get("summary", ""))
        for p in yq.get("points", []):
            lines.append(f"· {p.get('label','')}：{p.get('text','')}")

    lines.append("")
    lines.append("请从体用生克、互变参断、万物类象、应期远近四个维度，结合所问之事，给出温和、专业、可执行的解读。")
    return "\n".join(lines)


meihua_prompt = PromptTemplate(
    module="meihua",
    version="2.0",
    system=SYSTEM,
    build_user=_build_user,
    temperature=0.75,
    max_tokens=2048,
)
