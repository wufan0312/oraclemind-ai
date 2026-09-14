"""解梦 Prompt"""

from __future__ import annotations

from src.prompts.shared import (
    COMPLIANCE_BLOCK, OUTPUT_FORMAT,
    INJECTION_GUARD, TONE_BLOCK, role_block, PromptTemplate,
    INPUT_DISCLAIMER,
)

SYSTEM = f"""{role_block("你是精通周公解梦与心理分析的解梦师，擅长传统解梦与荣格潜意识分析。")}
{INPUT_DISCLAIMER}
{OUTPUT_FORMAT}
{COMPLIANCE_BLOCK}
{INJECTION_GUARD}
{TONE_BLOCK}"""


def _build_user(result: dict, focus: str | None = None) -> str:
    dream = result.get("dream", "")
    context = result.get("context", "")
    perspective = result.get("perspective", "")

    perspective_hint = ""
    if perspective == "jung":
        perspective_hint = "（荣格原型视角）"
    elif perspective == "freud":
        perspective_hint = "（弗洛伊德潜意识视角）"
    elif perspective == "cognitive":
        perspective_hint = "（认知行为视角）"
    elif perspective == "fortune":
        perspective_hint = "（东方运势命理视角）"

    return f"""请解读以下梦境{perspective_hint}：

梦境描述：{dream}
近期背景：{context or '未提供'}

请从核心意象、传统解梦、心理分析、建设性建议四维度解读。"""


dream_prompt = PromptTemplate(
    module="dream",
    version="2.0",
    system=SYSTEM,
    build_user=_build_user,
    temperature=0.75,
    max_tokens=2048,
)

# ======== 4 视角（前端「换个角度再看」） ========
# 与 TS 版 PERSPECTIVE_MAP 对齐：system 前缀注入 + user 规则块
PERSPECTIVE_MAP: dict[str, dict[str, str]] = {
    "jung": {
        "title": "荣格原型心理学",
        "systemPrefix": "【视角锁定：荣格学派】本轮请以卡尔·荣格分析心理学为核心框架（集体无意识 / 原型 Shadow / Anima·Animus / Self 个体化 / 梦的补偿功能），减少周公解梦传统释义与弗洛伊德性欲化解读。",
        "userRule": "重点识别梦境原型（阴影/人格面具/阿尼玛/自性/智慧老人）、梦对梦者的补偿性功能、意象中的对立张力（情结）。不要输出吉凶分类。",
    },
    "freud": {
        "title": "弗洛伊德精神分析",
        "systemPrefix": "【视角锁定：弗洛伊德精神分析】本轮请以西格蒙德·弗洛伊德经典精神分析为核心框架（潜意识压抑 / 愿望满足 / 显梦vs隐梦 / 凝缩与移置 / 童年经验）。减少周公解梦传统释义与荣格原型语言。",
        "userRule": "重点做显梦→隐梦翻译（凝缩/移置/象征）、被压抑愿望的满足、童年早期经验溯源。不要输出吉凶分类。",
    },
    "cognitive": {
        "title": "认知行为 (CBT)",
        "systemPrefix": "【视角锁定：认知行为学派】本轮请以现代认知科学与 CBT 为核心框架（情绪认知评估 / 记忆巩固 / 梦境对日间线索的加工 / 认知偏差 / 应对资源）。减少玄学/命理/深潜潜意识语言。",
        "userRule": "重点对应日间认知与情绪线索、梦境如何加工清醒时未处理的信息、识别认知偏差、提供认知重评与应对练习。不要输出吉凶分类。",
    },
    "fortune": {
        "title": "东方运势命理",
        "systemPrefix": "【视角锁定：东方运势命理】本轮请以《周公解梦》传统象征 + 五行/干支/易理征兆框架为主，结合吉凶应期。减少西方心理学语言，突出命理征兆与宜忌。",
        "userRule": "重点对应周公解梦传统释义、五行取象、近 1~3 月可能应验之事、宜/忌/建议；吉凶结论清晰（吉/平/需注意），用词温和避免恐吓。",
    },
}

# 多轮对话专用的自然语言输出指令（替换 dream_prompt.system 中的 JSON OUTPUT_FORMAT）
CHAT_NL_OUTPUT = """【输出格式（多轮对话专用）】
- 必须输出自然语言文本，禁止输出任何 JSON 结构！
- 使用 Markdown：段落用换行，重点用 **加粗**，列表用 - 前缀
- 语气温和、专业、亲切，像一位朋友在耐心解读
- 直接回答用户的问题，不要输出代码块、JSON 对象或结构化标记"""
