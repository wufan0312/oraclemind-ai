"""综合行动建议 Prompt（结构化输出）

对应 TS 版 src/prompts/summary.ts —— 融合八字/紫微/六爻/梅花/奇门多术数排盘，
给出综合白话总结与分阶段、可执行的行动建议。由独立 /api/v1/summary 路由驱动。
"""

from __future__ import annotations

from typing import Any
from datetime import datetime

from src.prompts.shared import (
    COMPLIANCE_BLOCK, INPUT_DISCLAIMER, INJECTION_GUARD,
    TONE_BLOCK, role_block, PromptTemplate,
)

SYSTEM = f"""{role_block('你是玄镜 OracleMind 的综合分析师，擅长融合八字、紫微、六爻、梅花、奇门、大六壬、太乙神数、数字命理、塔罗、星座等多种术数排盘，提炼多术数共识，给出通俗易懂的白话总结与分阶段、可执行的行动建议。')}
{INPUT_DISCLAIMER}
【输出格式】
必须输出合法 JSON（不要输出任何 JSON 以外的文字、不要 markdown 代码块包裹）：
{{
  "ok": true,
  "summary": "基于多术数共识的白话整体概述，≤120字；只给整体判断，不要复述 timeline 里的具体时间节点（如 2026秋/冬、2027春）",
  "advice": [
    {{"title":"⚡ 立即行动","items":["可立即执行的具体建议1","建议2","建议3"]}},
    {{"title":"📅 短期（1-3月）","items":["1-3个月内宜做的事1","事2"]}},
    {{"title":"🚀 中长期（2027+）","items":["中长期规划建议1","建议2"]}}
  ],
  "outlook": "近期趋势提示，≤80字；不要复述 timeline 里的具体时间节点（如 2026秋/冬、2027春）",
  "consensus": [
    {{"label":"事业运","score":85,"agreement":90,"reason":"八字官星得令、紫微官禄宫化权，两术数一致看好职场话语权"}},
    {{"label":"财运","score":80,"agreement":75,"reason":"六爻妻财持世但被日辰所克，短期见财但守财需谨慎"}},
    {{"label":"感情运","score":75,"agreement":60,"reason":"八字夫妻宫平稳、塔罗感情牌偏保守，整体中性偏稳"}},
    {{"label":"健康","score":88,"agreement":85,"reason":"五行日主中和、紫微疾厄宫无煞，基础体质尚可"}},
    {{"label":"学业/成长","score":78,"agreement":80,"reason":"文昌入命、数字命理主运偏进取，适合持续学习"}},
    {{"label":"人际/贵人","score":82,"agreement":70,"reason":"天乙贵人临命、奇门开门生宫，易得长辈提携"}}
  ],
  "cards": [
    {{"icon":"📈","name":"近期趋势","score":"↑ 上升","desc":"基于排盘的趋势判断，≤40字"}},
    {{"icon":"🎯","name":"关键决策期","score":"时间标签","desc":"最佳行动窗口期，≤40字"}},
    {{"icon":"⚠️","name":"风险提示","score":"中","desc":"主要风险及规避建议，≤40字"}},
    {{"icon":"💎","name":"天赋优势","score":"高","desc":"核心天赋与适合方向，≤40字"}}
  ],
  "timeline": [
    {{"period":"2026 秋","overview":"该阶段总览，≤50字"}},
    {{"period":"2026 冬","overview":"该阶段总览，≤50字"}},
    {{"period":"2027 春","overview":"该阶段总览，≤50字"}}
  ],
  "keyFindings": [
    "跨术数共振发现1（≤50字，指出哪几个术数一致、共同指向什么结论）",
    "跨术数共振发现2",
    "跨术数共振发现3"
  ],
  "divergences": [
    {{"desc":"术数间矛盾描述（≤50字）","modules":["bazi","liuyao"],"resolution":"调和建议（≤50字）"}}
  ]
}}
要求：
- advice 必须恰好 3 栏，顺序固定为「立即行动 / 短期（1-3月） / 中长期（2027+）」；
- 每条 items 2~4 条，须具体、可执行、避免空话套话；items 中严禁照搬 timeline 的「2026 秋/冬、2027 春」等节点文字；
- 建议须紧扣多术数共同指向的趋势，不要凭空编造；
- consensus 必须恰好 6 项，label 固定为「事业运/财运/感情运/健康/学业/成长/人际/贵人」，每项含两个**语义不同**的指标，切勿混为一谈：
  - `score`（60~95 整数）= **运势强度**：该维度当前运势有多好，越高越好；
  - `agreement`（0~100 整数）= **共识度**：参与融合的各术数在该维度上的结论一致程度，100=全体指向同一结论，0=各术数互相矛盾。
  例：事业运 score=85 但 agreement=50，表示"整体看好，但各术数分歧大，结论不确定"——这种情况下须在 summary 或风险提示中明确点出分歧，不要给出斩钉截铁的判断；
- `agreement` 必须基于实际参与融合的术数数量与分歧程度估算，不要一律填 100，也不要与 score 取相同值；
- cards 必须恰好 4 张，顺序固定为「近期趋势/关键决策期/风险提示/天赋优势」，icon 固定不变，score 和 desc 按排盘结果填写；
- timeline 提供 3~4 个时间节点，period 为时间标签（如「2026 秋」「2026 冬」「2027 春」，**必须晚于【当前时间】，不得给出过去年份**）、overview 为该阶段一句话总览（≤50字），须与 advice 三阶段呼应、可落地；timeline 是独立展示区块，**严禁在 summary、outlook、advice 的 items 中再次复述 timeline 的具体 period/overview 内容**，避免前端重复展示；
- consensus 每一项必须包含 reason（≤50字）：说明该维度分数为何如此，明确指出哪几个术数一致指向此结论，不得空泛；前端会逐维展示 reason 作为「分维依据」；
- keyFindings 提供 3~5 条「跨术数共振发现」：每条指出哪几个术数一致、共同指向什么结论（≤50字），这是综合运势的核心价值，务必具体、引用真实排盘字段，禁止空话套话；
- divergences 如实记录各术数间的矛盾（如事业运 score 高但某术数看衰），并对每条给出 resolution 调和建议（≤50字）；若无矛盾输出空数组 []，不得编造矛盾；
- 数字命理/塔罗/星座为「跨页近期测算结论」（result 中仅有 summary 摘要文本），融合时仅作趋势旁证，不重新推算，不得与正统排盘字段混淆；
- 大六壬看「当下事机」（四课三传、初传天将、空亡），太乙神数看「年内大势」（积年行宫、主客算），二者与奇门合称三式：奇门主方略、六壬主人事、太乙主天时，融合时按此分工取用；
- 太乙流派分歧极大（文昌/始击/主客算各本不同），仅作趋势旁证，不得据其单独立断，也不得声称其为唯一权威结论；
- 若关键字段缺失无法解读，输出 {{"ok": false, "reason": "简短原因"}}。
{COMPLIANCE_BLOCK}
{INJECTION_GUARD}
{TONE_BLOCK}"""


def _whitelist_project(module: str, result: Any) -> str:
    """白名单字段投影：每个术数只挑前端已展示的关键字段，避免回传原始大对象。"""
    if not result or not isinstance(result, dict):
        return "（无可用结果）"
    r = result
    if module == "bazi":
        pillars = " ".join(f"{p.get('gan','')}{p.get('zhi','')}" for p in r.get("pillars", []) if isinstance(p, dict)) or "-"
        shishen = "、".join(f"{s.get('name','')}({s.get('val',0)})" for s in r.get("shiShen", []) if isinstance(s, dict)) or "-"
        wx = r.get("wuxing")
        wx_str = wx if isinstance(wx, str) else (json.dumps(wx, ensure_ascii=False)[:60] if wx else "-")
        return "；".join([
            f"日主/五行: {r.get('dayMaster','-')}{('('+r['dayMasterWuxing']+')') if r.get('dayMasterWuxing') else ''}",
            f"四柱: {pillars}",
            f"十神: {shishen}",
            f"用神: {r.get('yongshen','-')}",
            f"五行: {wx_str}",
        ])
    if module == "ziwei":
        palaces = r.get("palaces", []) if isinstance(r.get("palaces"), list) else []
        ming_star = next((p.get("star", "-") for p in palaces if isinstance(p, dict) and p.get("name") == "命宫"), "-")
        sihua = r.get("sihua")
        sihua_str = "、".join(str(x.get("name", x)) for x in sihua if isinstance(x, dict)) if isinstance(sihua, list) else (sihua or "-")
        return "；".join([
            f"命宫: {r.get('mingGong','-')}",
            f"身宫: {r.get('shenGong','-')}",
            f"五行局: {r.get('wuxingJu','-')}",
            f"紫微: {r.get('ziwei','-')}",
            f"命宫主星: {ming_star}",
            f"四化: {sihua_str}",
        ])
    if module == "liuyao":
        ben = r.get("benGua") if isinstance(r.get("benGua"), dict) else {}
        lines = r.get("lines", []) if isinstance(r.get("lines"), list) else []
        liuqin = "、".join(l.get("shishen") for l in lines if isinstance(l, dict) and l.get("shishen")) or "-"
        return "；".join([
            f"本卦: {ben.get('name','-')}",
            f"变卦: {(r.get('bianGua') or {}).get('name','-')}",
            f"上卦/下卦: {ben.get('upper','-')} / {ben.get('lower','-')}",
            f"世应: 世={ben.get('shiPos','-')} 应={ben.get('yingPos','-')}",
            f"六亲: {liuqin}",
            f"用神: {r.get('yongshen','-')}",
        ])
    if module == "meihua":
        ben = r.get("benGua") if isinstance(r.get("benGua"), dict) else {}
        return "；".join([
            f"上卦: {ben.get('upper',{}).get('name','-') if isinstance(ben.get('upper'),dict) else ben.get('upper','-')}",
            f"下卦: {ben.get('lower',{}).get('name','-') if isinstance(ben.get('lower'),dict) else ben.get('lower','-')}",
            f"本卦: {ben.get('name','-')}",
            f"体用: 体={r.get('ti','-')} 用={r.get('yong','-')}",
            f"互卦: {(r.get('huGua') or {}).get('name','-')}",
            f"变卦: {(r.get('bianGua') or {}).get('name','-')}",
        ])
    if module == "qimen":
        palaces = r.get("palaces", []) if isinstance(r.get("palaces"), list) else []
        kai_men = next((p.get("dir", "-") for p in palaces if isinstance(p, dict) and p.get("door") == "开门"), "-")
        return "；".join([
            f"局数: {r.get('type','-')}",
            f"值符: {r.get('valueFu','-')}",
            f"值使: {r.get('valueShi','-')}",
            f"开门方位: {kai_men}",
        ])
    if module == "liuren":
        san = r.get("sanChuan") if isinstance(r.get("sanChuan"), dict) else {}
        items = san.get("items", []) if isinstance(san.get("items"), list) else []
        chuan_str = " → ".join(f"{c.get('gan','')}{c.get('zhi','')}({c.get('jiang','-')})"
                               for c in items if isinstance(c, dict)) or "-"
        sike = r.get("siKe", []) if isinstance(r.get("siKe"), list) else []
        sike_str = " ".join(f"{k.get('lower','-')}上{k.get('upper','-')}" for k in sike
                            if isinstance(k, dict)) or "-"
        # 伏吟 / 反吟：两者可同时不成立，此时显式标「无」，避免留下空标签
        fy_flags = "".join([
            "伏吟" if r.get("fuYin") else "",
            "反吟" if r.get("fanYin") else "",
        ]) or "无"
        return "；".join([
            f"月将/占时: {r.get('yueJiang','-')}{('('+r['yueJiangName']+')') if r.get('yueJiangName') else ''} / {r.get('zhanShi','-')}",
            f"日辰: {r.get('riGanZhi','-')}",
            f"四课: {sike_str}",
            f"课体: {san.get('keTi','-')}（{san.get('method','-')}）",
            f"三传: {chuan_str}",
            f"空亡: {'、'.join(r.get('kongWang') or []) or '-'}",
            f"伏吟/反吟: {fy_flags}",
        ])
    if module == "taiyi":
        ty = r.get("taiYiGong") if isinstance(r.get("taiYiGong"), dict) else {}
        return "；".join([
            f"年干支: {r.get('ganZhi','-')}",
            f"积年/局数: {r.get('jiNian','-')} / 第{r.get('ju','-')}局（{r.get('dun','-')}）",
            f"太乙居宫: {ty.get('gong','-')}宫（{ty.get('pos','-')}·{ty.get('fang','-')}）",
            f"文昌/始击: {(r.get('wenChang') or {}).get('shen','-')} / {(r.get('shiJi') or {}).get('shen','-')}",
            f"主算/客算: {r.get('zhuSuan','-')} / {r.get('keSuan','-')}",
            f"主客大将: 主{(r.get('zhuDaJiang') or {}).get('pos','-')} 客{(r.get('keDaJiang') or {}).get('pos','-')}",
            f"断语: {r.get('verdict','-')}",
            "注: 太乙流派分歧大，仅作趋势旁证，不得作为唯一依据",
        ])
    # 五行能量：由八字派生的独立维度，前端单独成 tab，融合时需显式引用
    if module == "wuxing":
        return "；".join([
            f"五行分布: {r.get('wuxing', '-')}",
            f"个数统计: {r.get('wuxingCount', '-')}",
            f"缺失: {r.get('lacking', '-')}",
            f"用神: {r.get('yongshen', '-')}",
        ])
    # 跨页近期测算结论：result 仅含 summary 摘要文本（非完整排盘），直接投影摘要
    if module in ("numerology", "tarot", "horoscope"):
        summary = r.get("summary") if isinstance(r, dict) else None
        return f"近期测算结论摘要：{summary or '（无摘要）'}"
    return json.dumps(r, ensure_ascii=False)[:400]


def _build_user(result: dict, focus: str | None = None) -> str:
    if not result or not isinstance(result.get("modules"), list) or len(result["modules"]) == 0:
        return "输入数据不完整：未收到任何术数排盘结果。"
    lines: list[str] = []
    # 注入当前时间锚点：timeline/outlook 须从当下向后推演，避免 LLM 给出过去年份
    lines.append(f"【当前时间】{datetime.now().strftime('%Y年%m月')}")
    if result.get("question"):
        lines.append(f"【占卜问题】{result['question']}")
    lines.append("【多术数排盘结果（已由确定性算法算好，请勿重新推算）】")
    for m in result["modules"]:
        lines.append(f"\n■ {m.get('name', m.get('module', ''))}（{m.get('module', '')}）")
        lines.append(_whitelist_project(m.get("module", ""), m.get("result")))
    lines.append("\n请基于以上全部排盘结果，找出多术数共识，输出综合白话总结、四张卡片、共识度与 3~4 段时间节点的时间轴 JSON。")
    if focus == "love":
        lines.append("\n【聚焦】用户重点关注感情/姻缘，请在 summary、advice、consensus 中适当强化「感情运」「人际/贵人」维度，其余维度仍须覆盖。")
    elif focus == "career":
        lines.append("\n【聚焦】用户重点关注事业/财运，请在 summary、advice、consensus 中适当强化「事业运」「财运」维度，其余维度仍须覆盖。")
    elif focus:
        lines.append(f"\n【聚焦】用户重点关注：{focus}，请在整体建议中适当倾斜该方向，其余维度仍须覆盖。")
    return "\n".join(lines)


# 避免在未 import json 时引用
import json  # noqa: E402

summary_prompt = PromptTemplate(
    module="summary",
    version="summary_interpret_v7",
    system=SYSTEM,
    build_user=_build_user,
    temperature=0.7,
    max_tokens=2400,
)
