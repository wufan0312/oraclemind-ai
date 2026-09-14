"""数字命理 Prompt（v3.1）

v3.0 的问题：沿用了 shared.OUTPUT_FORMAT（通用 aspects：性格倾向/事业财运/情感人际/格局印证），
    导致本模块特化的分节要求被 system 覆盖，且「格局印证」会诱导模型为西方数字命理编造
    东方典籍引文（如「《滴天髓》：命带大师数」）—— 属体系错配的幻觉。

v3.1 修正：
- 参照 ziwei 的做法，用 build_system 覆盖默认 OUTPUT_FORMAT，改为数字命理专属 aspects
- 明确体系边界：禁止以「《书名》：金句」形式引用东方典籍，结论必须基于给定数字推导
- 新增合盘模式（focus 以 'synastry' 开头）专属输出结构
- 本命模式补喂大师数标注、九宫格洛书统计与天赋连线、四项挑战数、核心数字
"""

from __future__ import annotations

import json

from src.prompts.shared import (
    COMPLIANCE_BLOCK, INPUT_DISCLAIMER,
    INJECTION_GUARD, role_block, PromptTemplate,
)

# ===== 本命专属输出格式（覆盖通用 OUTPUT_FORMAT）=====
NATAL_OUTPUT_FORMAT = """【输出格式】
必须输出合法 JSON（不要输出任何 JSON 以外的文字、不要 markdown 代码块包裹）：
{
  "ok": true,
  "summary": "整体概述，≤80字",
  "aspects": [
    {"title":"天赋特质","text":"结合生命灵数与表现数说明，≤130字"},
    {"title":"人生课题","text":"结合四项挑战数（尤其第三挑战）与缺失数字说明，≤140字"},
    {"title":"事业方向","text":"≤120字"},
    {"title":"情感关系","text":"≤120字"},
    {"title":"姓名能量","text":"表现数/内驱数/人格数三者如何相互作用；若输入未提供核心数字，则写「填写中文姓名后可解锁表现数、内驱数、人格数与成熟数」，≤130字"},
    {"title":"跨越你的挑战数","text":"针对四项挑战各给一条具体可执行的做法，以 markdown 列表呈现，≤170字"}
  ],
  "advice": ["建设性建议1","建议2","建议3"],
  "outlook": "结合今年流年数的近期提示，≤80字"
}
若无法解读：{"ok": false, "reason": "简短原因"}"""

# ===== 合盘专属输出格式 =====
SYNASTRY_OUTPUT_FORMAT = """【输出格式】
必须输出合法 JSON（不要输出任何 JSON 以外的文字、不要 markdown 代码块包裹）：
{
  "ok": true,
  "summary": "这组组合的整体概述，≤80字",
  "aspects": [
    {"title":"这组组合的第一印象","text":"≤120字"},
    {"title":"契合度从哪来","text":"结合给定的三个维度具体说明，不要复述数字，≤150字"},
    {"title":"最容易卡住的地方","text":"给出具体的沟通或分工建议，≤140字"},
    {"title":"相处建议","text":"一条最值得落地的建议，≤120字"}
  ],
  "advice": ["给甲方的建议","给乙方的建议","给这组关系的建议"],
  "outlook": "近期相处提示，≤80字"
}
若无法解读：{"ok": false, "reason": "简短原因"}"""

# ===== 体系边界：数字命理属西方毕达哥拉斯体系，与东方典籍无对应关系 =====
SYSTEM_BOUNDARY = """【体系边界】
数字命理（Numerology）属西方毕达哥拉斯体系，与八字、紫微等东方命理典籍【没有】对应关系。
- 若输入中出现「参考资料 · 检索增强」块，仅供背景参考，【禁止】引用、【禁止】改写、【禁止】扩写其中任何内容；
- 【禁止】以「《书名·篇目》：原文金句」的形式编造任何典籍引文——数字命理不存在此类古典文献；
- 所有结论必须由给定的数字结果（生命灵数、核心数字、挑战数、九宫格与天赋连线、流年）直接推导得出。"""

# 语气：本模块取代 shared.TONE_BLOCK（其 ≤400 字的总量约束对 6 段结构过紧）
TONE = "【语气风格】\n平和、克制、温暖，不夸大、不渲染焦虑。每个 aspect 控制在规定的字数内，解读总字数 ≤ 650 字。"

# 洛书排列顺序（与前端 LO_SHU_ORDER 一致）
LO_SHU_ORDER = [4, 9, 2, 3, 5, 7, 8, 1, 6]

# 八条天赋连线（与前端 TALENT_LINES 一致）：组成数字全部出现即成立
TALENT_LINES: list[tuple[list[int], str]] = [
    ([4, 9, 2], "思维线"),
    ([3, 5, 7], "情感线"),
    ([8, 1, 6], "行动线"),
    ([4, 3, 8], "意志线"),
    ([9, 5, 1], "智慧线"),
    ([2, 7, 6], "艺术线"),
    ([4, 5, 6], "规划线"),
    ([2, 5, 8], "平衡线"),
]

# 挑战数 0~8 释义（与前端 CHALLENGE_DATA 一致）
CHALLENGE_MEANING: dict[int, str] = {
    0: "无碍（能量自然流动，但缺少张力推力）",
    1: "自我 vs 他人（在坚持自己与在意他人眼光间摇摆）",
    2: "亲密 vs 依赖（害怕亲密又害怕孤单）",
    3: "表达 vs 自我怀疑（想说却不敢说，或一说就停不下来）",
    4: "秩序 vs 束缚（缺乏耐心，或被规则捆死）",
    5: "自由 vs 承诺（临近承诺就想逃，逃开后又空虚）",
    6: "完美 vs 接纳（标准过高，容易失望挑剔）",
    7: "信任 vs 怀疑（过度多疑，或轻信后受伤）",
    8: "掌控 vs 焦虑（对金钱权力地位有深层焦虑）",
}

CORE_LABELS = [
    ("expression", "表现数", "天生带来的才能与潜能"),
    ("soulUrge", "内驱数", "内心真正渴望什么"),
    ("personality", "人格数", "给外界的第一印象"),
    ("maturity", "成熟数", "中年后真正走向的人生方向"),
]

CHALLENGE_ROWS = [
    ("c1", "第一挑战", "早年 0-35 岁"),
    ("c2", "第二挑战", "中年 35-55 岁"),
    ("c3", "第三挑战", "贯穿一生的底层课题"),
    ("c4", "第四挑战", "面对世界的方式"),
]

# 默认 system（本命）—— focus 非 synastry 时使用
SYSTEM_NATAL = f"""{role_block("你是精通毕达哥拉斯数字命理的咨询师，擅长生命灵数、核心数字（表现数/内驱数/人格数/成熟数）、挑战数与九宫格天赋连线分析。")}
{INPUT_DISCLAIMER}
{NATAL_OUTPUT_FORMAT}
{SYSTEM_BOUNDARY}
{COMPLIANCE_BLOCK}
{INJECTION_GUARD}
{TONE}"""

SYSTEM_SYNASTRY = f"""{role_block("你是精通毕达哥拉斯数字命理的咨询师，擅长用生命灵数、核心数字与九宫格能量补位分析两人的契合度与相处模式。")}
{INPUT_DISCLAIMER}
{SYNASTRY_OUTPUT_FORMAT}
{SYSTEM_BOUNDARY}
{COMPLIANCE_BLOCK}
{INJECTION_GUARD}
{TONE}"""


def _fmt_master(n) -> str:
    """大师数标注：11 / 22 / 33 需点明「不化简、能量翻倍」。"""
    try:
        v = int(n)
    except (TypeError, ValueError):
        return str(n)
    if v in (11, 22, 33):
        roots = {11: 2, 22: 4, 33: 6}
        return f"{v}（大师数，根数 {roots[v]}，能量翻倍、不化简）"
    return str(v)


def _count_of(counts: dict, n: int) -> int:
    """九宫格计数兼容字符串键（JSON）与整数键。"""
    if not isinstance(counts, dict):
        return 0
    v = counts.get(str(n), counts.get(n, 0))
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _fmt_grid(counts: dict | None) -> str:
    """九宫格统计 + 天赋连线判定（洛书序）。"""
    if not isinstance(counts, dict) or not counts:
        return "（未提供九宫格数据）"
    grid = " ".join(f"{n}:{_count_of(counts, n)}次" for n in LO_SHU_ORDER)
    active, inactive = [], []
    for nums, label in TALENT_LINES:
        seg = f"{label}（{'-'.join(map(str, nums))}）"
        (active if all(_count_of(counts, x) > 0 for x in nums) else inactive).append(seg)
    lines = [f"洛书序统计：{grid}", f"已点亮的天赋连线：{'、'.join(active) if active else '无'}"]
    if inactive:
        lines.append(f"未凑齐的连线：{'、'.join(inactive)}")
    return "\n".join(lines)


def _fmt_core(core: dict | None) -> str:
    """核心数字 + 挑战数。"""
    if not isinstance(core, dict):
        return "（未提供姓名，无核心数字；请直接说明「填写中文姓名后可解锁」）"
    out = []
    for key, label, hint in CORE_LABELS:
        v = core.get(key)
        if v is not None:
            out.append(f"- {label}：{_fmt_master(v)} —— {hint}")
    ch = core.get("challenge")
    if isinstance(ch, dict):
        segs = [
            f"{lbl}（{stage}）= {ch.get(k)} · {CHALLENGE_MEANING.get(ch.get(k), '')}"
            for k, lbl, stage in CHALLENGE_ROWS if ch.get(k) is not None
        ]
        if segs:
            out.append("- 挑战数：" + "；".join(segs))
    return "\n".join(out) if out else "（未提供姓名，无核心数字；请直接说明「填写中文姓名后可解锁」）"


def _build_system(focus: str | None = None) -> str | None:
    """合盘模式（focus 以 'synastry' 开头）切换到专属输出结构。"""
    if (focus or "").strip().startswith("synastry"):
        return SYSTEM_SYNASTRY
    return None  # 使用默认 SYSTEM_NATAL


def _build_user(result: dict, focus: str | None = None) -> str:
    # ===== 合盘模式 =====
    if isinstance(result, dict) and result.get("mode") == "synastry":
        a = result.get("a") or {}
        b = result.get("b") or {}
        syn = result.get("synastry") or {}
        dims = syn.get("dims") or []

        def side(tag: str, p: dict) -> str:
            return (
                f"{tag}：生命灵数 {_fmt_master(p.get('lifePath'))}　生日数 {p.get('birthdayNum')}　"
                f"数字名 {(p.get('data') or {}).get('name', '')}\n"
                f"{tag}核心数字：\n{_fmt_core(p.get('core'))}\n"
                f"{tag}九宫格：{_fmt_grid(p.get('counts'))}\n"
                f"{tag}缺数：{p.get('missing') or '无'}"
            )

        dim_text = "\n".join(
            f"- {d.get('label')}：{d.get('score')}/{d.get('max')} —— {d.get('desc', '')}" for d in dims
        ) or "（未提供维度拆解）"

        return f"""请解读以下数字命理合盘（配对）结果：

{side('甲方', a)}

{side('乙方', b)}

契合度总分：{syn.get('score')} / 100
总评：{syn.get('headline', '')}
维度拆解：
{dim_text}
结构优势：{json.dumps(syn.get('strengths', []), ensure_ascii=False)}
结构摩擦点：{json.dumps(syn.get('frictions', []), ensure_ascii=False)}

注意：契合度分数由确定性算法给出，不要重新打分，也不要质疑它；你的任务是解释这组数字结构在关系里意味着什么。
{focus or ''}"""

    # ===== 本命模式 =====
    core = result.get("core")
    return f"""请解读以下数字命理结果：

生命灵数：{_fmt_master(result.get('lifePath', ''))}
生日数字：{result.get('birthdayNum', '')}
缺失数字：{result.get('missing', [])}
数字详情：{json.dumps(result.get('data', {}), ensure_ascii=False)}

九宫格与天赋连线：
{_fmt_grid(result.get('counts'))}

核心数字与挑战数：
{_fmt_core(core)}

流年（月+日+年份各位数字之和→数字根）：{json.dumps(result.get('years', []), ensure_ascii=False)}

{focus or ''}"""


numerology_prompt = PromptTemplate(
    module="numerology",
    version="3.1",
    system=SYSTEM_NATAL,
    build_user=_build_user,
    build_system=_build_system,
    temperature=0.75,
    max_tokens=2560,
)
