"""玄镜 OracleMind · 首页通用命理助手 Agent（LangGraph ReAct）

复用起名 Agent 的 ReAct 骨架，工具集扩展为：
- 八字排盘（bazi_paipan）
- 紫微斗数排盘（ziwei_paipan）
- 起名（name_generate）/ 名字详批（name_detail）

定位为「玄镜通用命理助手」：能回答任意命理/玄学问题，涉及具体生辰时
自动调用排盘工具，不依赖生辰的问题（塔罗/星座/数字命理/风水/解梦）基于
知识直接作答。支持多轮 history。
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, AsyncIterator, Optional

from src.config import config
from src.services.budget import budget_broken, cache_get, cache_set
from src.agent.prompts import HOME_AGENT_SYSTEM_PROMPT, HOME_AGENT_DISCLAIMER

logger = logging.getLogger(__name__)

# 首页助手结果缓存：相同（问题 + 历史 + 出生）直接复用，不再重跑 LLM
_HOME_CACHE_TTL = 3600

# 每轮请求关联的 birth_hint，供排盘工具做「无生辰禁止编造」硬拦截
_current_birth_hint: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "current_birth_hint", default=None
)


def _has_complete_birth_hint(hint: Optional[dict]) -> bool:
    """判断 birth_hint 是否包含完整的公历生辰（年月日时辰性别五项缺一不可）。"""
    if not hint:
        return False
    return bool(
        hint.get("year")
        and hint.get("month")
        and hint.get("day")
        and hint.get("timeText")
        and hint.get("gender")
    )


def _is_complete_paipan_input(year: int, month: int, day: int, time_text: str, gender: str) -> bool:
    """判断排盘工具入参本身是否完整（AI 已从自然语言提取出有效生辰）。"""
    return bool(year and month and day and time_text and gender)


# ==================== 结构化生辰归一化（时辰/性别文本兜底） ====================
# 与前端 src/lib/birthHintExtractor.ts 的解析规则保持一致，双端互为防线：
# 前端失手（历史坑：/\b女\b/ 对中文永不匹配，导致 gender 恒空）时，
# 后端仍能从用户原话里把时辰/性别捞回来。
_SHICHEN = ["子时", "丑时", "寅时", "卯时", "辰时", "巳时",
            "午时", "未时", "申时", "酉时", "戌时", "亥时"]
_ZHI_TIME_RE = re.compile(r"(子|丑|寅|卯|辰|巳|午|未|申|酉|戌|亥)\s*[时時]")
_TIME_RANGE_RE = re.compile(r"(\d{1,2})\s*[-~—－至到]\s*(\d{1,2})\s*[点时時]")
_TIME_SINGLE_RE = re.compile(r"(\d{1,2})\s*[点时時]")
_YMD_RE = re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")
_LUNAR_WORD_RE = re.compile(r"农历|阴历|旧历|農曆")
_SOLAR_WORD_RE = re.compile(r"公历|阳历|新历|国历|西历")


def _shichen_from_hour(hour: int) -> str:
    """小时 → 时辰（按区间起点取整）：23/0→子, 1→丑, 3→寅 ... 21→亥"""
    hh = ((hour % 24) + 24) % 24
    return _SHICHEN[((hh + 1) % 24) // 2]


def _parse_time_text(text: str) -> Optional[str]:
    """文本 → 时辰。区间/单点必须带「点/时」后缀，避免把日期 03-12 误判成 3点到12点。"""
    m = _ZHI_TIME_RE.search(text)
    if m:
        return f"{m.group(1)}时"
    m = _TIME_RANGE_RE.search(text)
    if m:
        h = int(m.group(1))
        if 0 <= h <= 23:
            return _shichen_from_hour(h)
    m = _TIME_SINGLE_RE.search(text)
    if m:
        h = int(m.group(1))
        if 0 <= h <= 23:
            return _shichen_from_hour(h)
    return None


def _parse_gender(text: str) -> Optional[str]:
    """文本 → 性别。禁用 \\b 词边界（对中文恒不匹配），改按字符位置判断。"""
    s = re.sub(r"男女|女男", "", text)
    f = s.find("女")
    m = s.find("男")
    if f >= 0 and m >= 0:
        return "女" if f < m else "男"
    if f >= 0:
        return "女"
    if m >= 0:
        return "男"
    return None


def _normalize_birth_hint(
    birth_hint: Optional[dict],
    message: str,
    history: Optional[list] = None,
) -> Optional[dict]:
    """合并前端结构化生辰与用户原话解析结果。

    分工原则：**公历年月日优先信前端**（它已完成农历→公历换算）；前端没给时，
    若用户原话里的日期不是农历，就当公历直接采用；若是农历，则记为
    ``hint["lunar"]``，交由后端排盘服务做权威换算（见 ``_prefetch_paipan``）。
    **时辰/性别**前端缺失时用文本兜底补齐。

    无任何可用年月日 → 返回 None，不做排盘，也不生成 CTA 回填。
    """
    hint: dict = dict(birth_hint) if birth_hint else {}

    texts = [message or ""]
    for h in (history or []):
        if h.get("role") == "user" and h.get("content"):
            texts.append(str(h["content"]))
    blob = "\n".join(t for t in texts if t)

    if not hint.get("timeText"):
        parsed_t = _parse_time_text(blob)
        if parsed_t:
            hint["timeText"] = parsed_t
    if not hint.get("gender"):
        parsed_g = _parse_gender(blob)
        if parsed_g:
            hint["gender"] = parsed_g

    has_ymd = bool(hint.get("year") and hint.get("month") and hint.get("day"))
    if not has_ymd:
        # 前端没给公历 → 尝试从原话里补。农历声明时只记 lunar，交后端换算（本地不做历法）
        m = _YMD_RE.search(blob)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if _LUNAR_WORD_RE.search(blob) and not _SOLAR_WORD_RE.search(blob):
                hint["lunar"] = {"year": y, "month": mo, "day": d}
            else:
                hint["year"], hint["month"], hint["day"] = y, mo, d

    if not (hint.get("year") and hint.get("month") and hint.get("day")) and not hint.get("lunar"):
        return None
    return hint


def _paipan_ready(hint: Optional[dict]) -> bool:
    """是否具备排盘条件：公历五项齐全，或「农历分量 + 时辰 + 性别」齐全。"""
    if not hint:
        return False
    if _has_complete_birth_hint(hint):
        return True
    lunar = hint.get("lunar") or {}
    return bool(
        lunar.get("year") and lunar.get("month") and lunar.get("day")
        and hint.get("timeText") and hint.get("gender")
    )


# ==================== 服务端主动排盘（不赌 LLM 是否愿意调工具） ====================
_PAIPAN_OBS_HEADER = "【系统已完成八字排盘 · 以下为真实数据】"
_PAIPAN_OBS_TAIL = (
    "请直接基于以上真实排盘数据回答用户的问题：不要再向用户索要出生信息，"
    "也不要再次调用排盘工具。若数据里没有的字段，就说「无法确定」，不要编造。"
)


def _compact_paipan_summary(raw: str) -> str:
    """把后端全量排盘 JSON 压成精简摘要。

    全量 JSON 含四柱/十神/五行/大运/流年/神煞等上万字符，直接注入会挤爆上下文；
    这里只保留解读必需的字段，控制在千字符级。
    """
    try:
        d = json.loads(raw)
    except Exception:
        return raw[:1200]
    if not isinstance(d, dict):
        return raw[:1200]

    parts: list = []
    solar = d.get("solar")
    if solar:
        parts.append(f"公历 {solar}" + (f"（农历 {d['lunar']}）" if d.get("lunar") else ""))
    if d.get("timeText"):
        parts.append(f"时辰 {d['timeText']}")
    if d.get("shengxiao"):
        parts.append(f"生肖 {d['shengxiao']}")
    if d.get("dayMaster"):
        parts.append(f"日主 {d['dayMaster']}{d.get('dayMasterWuxing', '')}")

    pillars = d.get("pillars") or []
    if pillars:
        parts.append("四柱 " + " ".join(f"{p.get('gan', '')}{p.get('zhi', '')}" for p in pillars))

    wuxing = d.get("wuxing") or []
    if wuxing:
        parts.append("五行占比 " + "、".join(f"{w.get('label')}{w.get('pct')}%" for w in wuxing))
    if d.get("lacking"):
        parts.append("五行缺 " + "、".join(str(x) for x in d["lacking"]))

    ys = d.get("yongshen") or {}
    if ys.get("xi"):
        parts.append("喜用神 " + "、".join(str(x) for x in ys["xi"]))
    if ys.get("ji"):
        parts.append("忌神 " + "、".join(str(x) for x in ys["ji"]))

    ss = d.get("shiShen") or []
    if ss:
        parts.append("十神 " + "、".join(f"{s.get('name')}{s.get('val')}" for s in ss))

    dy = d.get("dayun") or []
    if dy:
        parts.append("大运 " + "、".join(
            f"{x.get('age')}{x.get('gan')}" + (f"（{x['note']}）" if x.get("note") else "")
            for x in dy[:8]
        ))

    ln = d.get("liunian") or []
    if ln:
        parts.append("流年 " + "、".join(
            f"{x.get('yr')}{x.get('gan')}" + (f"·{x['note']}" if x.get("note") else "")
            for x in ln[:5]
        ))

    qy = d.get("qiyun") or {}
    if qy.get("date"):
        parts.append(f"起运 {qy['date']}" + (f"（{qy['after']}）" if qy.get("after") else ""))

    if d.get("analysis"):
        parts.append(f"八字分析 {str(d['analysis'])[:300]}")

    return "\n".join(parts) if parts else raw[:1200]


async def _prefetch_paipan(hint: Optional[dict]):
    """结构化生辰齐全时，服务端主动排盘并把真实结果交给 LLM。

    为什么不交给 LLM 自己决定：GLM-4-Flash 的 ReAct 决策不稳定 —— 用户已给出完整
    生辰，模型仍可能直接回「很抱歉，我无法获取您的信息」，既不调工具也不排盘
    （2026-09-11 线上实测）。这里把「该不该排盘」变成确定性工程判断。

    仅有农历分量时，把 lunar 交给后端排盘服务换算（本地不实现历法），
    并从返回的 ``solar`` 回填 ``hint`` 的年月日，供 CTA 回填使用。

    Returns: (observation_text | None, paipan_input | None)
    """
    if not _paipan_ready(hint):
        return None, None

    lunar = hint.get("lunar") if not _has_complete_birth_hint(hint) else None
    year = int(hint.get("year") or (lunar or {}).get("year") or 0)
    month = int(hint.get("month") or (lunar or {}).get("month") or 0)
    day = int(hint.get("day") or (lunar or {}).get("day") or 0)
    time_text = str(hint.get("timeText") or "")
    gender = str(hint.get("gender") or "")

    try:
        from src.agent.tools.bazi_paipan import bazi_paipan

        raw = await asyncio.to_thread(
            bazi_paipan, year, month, day, time_text, gender, lunar,
        )
    except Exception as e:
        # 排盘服务不可用 → 不注入、不假装有数据；让 LLM 走原有「无资料」路径
        logger.warning("[home] 服务端预排盘失败，本轮不注入排盘数据: %s", e)
        return None, None

    # 农历换算结果回填：让 hint 变成结构完整的公历生辰（CTA / 缓存键都依赖它）
    try:
        parsed = json.loads(raw)
        solar = str(parsed.get("solar") or "")
        if solar:
            y, mo, d = (int(x) for x in solar.split("-")[:3])
            hint["year"], hint["month"], hint["day"] = y, mo, d
            hint.pop("lunar", None)
    except Exception:
        pass

    summary = _compact_paipan_summary(raw)
    paipan_input = {
        "year": year, "month": month, "day": day,
        "time_text": time_text, "gender": gender,
    }
    return f"{_PAIPAN_OBS_HEADER}\n{summary}\n{_PAIPAN_OBS_TAIL}", paipan_input


@lru_cache(maxsize=1)
def _home_prompt_hash() -> str:
    """对首页助手 system prompt + 免责声明取内容哈希：prompt 一改，旧缓存 key 自然失配、自动失效，
    不再依赖手写版本号（历史多次漏 bump vN 导致旧缓存绕过新 prompt 回放坏结果，对应缺陷报告 P2·home 缓存 key 自动失效）。
    """
    h = hashlib.md5()
    h.update(HOME_AGENT_SYSTEM_PROMPT.encode("utf-8"))
    h.update(b"||")
    h.update(HOME_AGENT_DISCLAIMER.encode("utf-8"))
    return h.hexdigest()[:12]


def _home_cache_key(message, history, birth_hint) -> str:
    raw = json.dumps(
        {"m": message, "h": history or [], "b": birth_hint or {}},
        ensure_ascii=False, sort_keys=True,
    )
    # 版本前缀改为 prompt 内容哈希：HOME_AGENT_SYSTEM_PROMPT / DISCLAIMER 任一改动，
    # 旧缓存 key 即失配而自动失效，彻底消除「忘了 bump vN → 旧缓存绕过新 prompt」的回归。
    return f"agent:home:{_home_prompt_hash()}:{hashlib.md5(raw.encode('utf-8')).hexdigest()[:16]}"


# 全局 Agent 单例
_agent = None


def _build_model():
    """构建 ChatOpenAI 实例"""
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=config.ai_model_chat,
        api_key=config.ai_api_key,
        base_url=config.ai_base_url,
        temperature=0.7,
        max_tokens=2048,
        streaming=True,
    )


def _build_tools():
    """构建 LangChain Tool 列表"""
    from langchain_core.tools import tool

    from src.agent.tools.bazi_paipan import bazi_paipan
    from src.agent.tools.ziwei_paipan import ziwei_paipan
    from src.agent.tools.name_generate import name_generate
    from src.agent.tools.name_detail import name_detail

    @tool
    def bazi_paipan_tool(year: int, month: int, day: int, time_text: str, gender: str) -> str:
        """调用八字排盘服务，获取四柱八字、五行、喜用神、十神等信息。

        硬性规则：用户未在对话中提供完整出生信息（年月日时辰性别）时，禁止调用本工具，
        不得编造参数。此时应返回拒绝信息，引导用户填写完整生辰。
        若系统已通过【已知访客信息】提供结构化生辰，优先采用该信息，覆盖模型自行解析的参数。

        Args:
            year: 出生年（公历）
            month: 出生月（公历）
            day: 出生日（公历）
            time_text: 时辰文本（如"辰时"）
            gender: 性别（"男"/"女"）
        """
        hint = _current_birth_hint.get()
        if _has_complete_birth_hint(hint):
            year = int(hint["year"])
            month = int(hint["month"])
            day = int(hint["day"])
            time_text = str(hint["timeText"])
            gender = str(hint["gender"])
        elif not _is_complete_paipan_input(year, month, day, time_text, gender):
            return "【排盘被拒绝】用户尚未提供完整出生信息（公历年月日、时辰、性别）。根据平台规则，不得编造生辰进行排盘，请先引导用户填写完整信息。"
        return bazi_paipan(year, month, day, time_text, gender)

    @tool
    def ziwei_paipan_tool(year: int, month: int, day: int, time_text: str, gender: str) -> str:
        """调用紫微斗数排盘服务，获取十二宫命盘、主星、四化、大运等信息。

        硬性规则：用户未在对话中提供完整出生信息（年月日时辰性别）时，禁止调用本工具，
        不得编造参数。此时应返回拒绝信息，引导用户填写完整生辰。
        若系统已通过【已知访客信息】提供结构化生辰，优先采用该信息，覆盖模型自行解析的参数。

        Args:
            year: 出生年（公历）
            month: 出生月（公历）
            day: 出生日（公历）
            time_text: 时辰文本（如"辰时"）
            gender: 性别（"男"/"女"）
        """
        hint = _current_birth_hint.get()
        if _has_complete_birth_hint(hint):
            year = int(hint["year"])
            month = int(hint["month"])
            day = int(hint["day"])
            time_text = str(hint["timeText"])
            gender = str(hint["gender"])
        elif not _is_complete_paipan_input(year, month, day, time_text, gender):
            return "【排盘被拒绝】用户尚未提供完整出生信息（公历年月日、时辰、性别）。根据平台规则，不得编造生辰进行排盘，请先引导用户填写完整信息。"
        return ziwei_paipan(year, month, day, time_text, gender)

    @tool
    def name_generate_tool(surname: str, gender: str, wuxing_xi: str = "", count: int = 5) -> str:
        """根据姓氏、性别、喜用神五行生成候选名字。

        Args:
            surname: 姓氏（如"李"）
            gender: 性别（"男"/"女"）
            wuxing_xi: 喜用神五行（如"水"，可选）
            count: 生成数量（默认5个）
        """
        return name_generate(surname, gender, wuxing_xi, count)

    @tool
    def name_detail_tool(name: str) -> str:
        """查询名字的笔画、五行、三才五格等详细信息。

        Args:
            name: 完整名字（不含姓，如"子涵"）
        """
        return name_detail(name)

    return [bazi_paipan_tool, ziwei_paipan_tool, name_generate_tool, name_detail_tool]


def _get_agent():
    """获取/初始化 Agent（惰性单例）"""
    global _agent
    if _agent is not None:
        return _agent

    from langchain.agents import create_agent

    model = _build_model()
    tools = _build_tools()
    _agent = create_agent(
        model=model,
        tools=tools,
        system_prompt=HOME_AGENT_SYSTEM_PROMPT,
    )
    logger.info("首页通用命理助手 ReAct Agent 已初始化")
    return _agent


# 各模块的 CTA 文案与路由（首页小玄引导跳转用）
# 措辞合规（2026-09-20）：对用户去「算命/排盘/命盘」化，统一走「觉察/探索/自我了解」框架
_CTA_TARGETS = {
    "tarot":      ("/tarot",      "想抽张牌理理思路？去塔罗页看看 →"),
    "horoscope":  ("/horoscope",  "想了解你的本命星盘？去星座页看看 →"),
    "numerology": ("/numerology", "想知道你的生命灵数？去数字密码页 →"),
    "bugua":      ("/bugua",      "想生成专属的觉察档案？去卜卦页看看 →"),
    "scales":     ("/scales",     "想更了解自己的性格优势？去测评页测一测 →"),
}

# 无实质诉求（随便看看/逛）时的探索向文案：不带预填问题，目标页自动开「整体指引」局
_CTA_EXPLORE_LABELS = {
    "tarot":      ("/tarot",      "随便逛逛？抽一副今日指引牌 →"),
    "horoscope":  ("/horoscope",  "随便看看？去星座页看今日星象 →"),
    "numerology": ("/numerology", "随便看看？去看看你的生命灵数 →"),
    "bugua":      ("/bugua",      "随便看看？去卜卦页看今日指引 →"),
    "scales":     ("/scales",     "随便看看？测一测你的性格优势 →"),
}

# 意图关键词：具体模块词优先，宽情感/运势词兜底（避免「运势」等泛词误抢具体模块）
_CTA_INTENT_PATTERNS = [
    (re.compile(r"塔罗|牌阵|抽[张个]牌|恋人|关系走向|缘分"), "tarot"),
    (re.compile(r"数字|生命灵数|灵数|九宫|流年|数字密码"), "numerology"),
    (re.compile(r"八字|卜卦|排盘|起名|合婚|紫微"), "bugua"),
    (re.compile(r"星座|星盘|上升|本命盘|行星|太阳返照|合盘"), "horoscope"),
    # 测评类诉求 → 自家测评页（禁外链，见 prompts.py 5.2）；放在宽兜底之前，具体词优先
    (re.compile(r"测评|在线测试|性格测试|人格测试|优势测试|优势测评|心理测试|MBTI|DISC|大五"), "scales"),
    # 宽兜底：无具体模块词时按语义归并
    (re.compile(r"感情|关系|在一起|分手|复合|暧昧"), "tarot"),
    (re.compile(r"运势|运程"), "horoscope"),
    # 无目的闲逛兜底（用户明确说「随便看看/逛逛」）：推荐意图必须稳定产出 CTA，
    # 不能赌 LLM 回复里是否提到模块关键词。放最后——回复中出现具体模块词时优先命中上面的。
    (re.compile(r"随便看看|随便逛逛|随便转转|有什么推荐"), "tarot"),
]


# 元信息 / 寒暄 / 关于功能本身的提问：这些不算「真实诉求」，合成 prefill 时应剔除，
# 否则会出现「原来还能卜卦呀，是免费的吗？」这类话被带去占卜页当问题。
_META_MSG_PATTERNS = [
    re.compile(r"^(你好|您好|hi|hello|在吗|嗨|哈喽)\b", re.I),
    re.compile(r"^(好的|好吧|谢谢|感谢|嗯+|哦+|啊+|明白|知道了|可以的|收到|了解|嗯嗯)\s*[！!。.～~]*$"),
    re.compile(r"(免费|多少钱|收费|怎么用|如何使用|如何.*用|在哪里|在哪|怎么.*卜卦|怎么.*起卦|怎么开始|教程|使用说明|怎么玩|怎么操作)"),
    # 无目的闲逛 / 能力询问：用户没有实质性诉求，宁可不带 prefill 也不许编造
    re.compile(r"(随便看看|随便逛逛|随便转转|没有目的|没啥目的|没什么目的|不知道看什么|不知道问什么)"),
    re.compile(r"(有什么推荐|推荐一下|推荐个|介绍介绍|介绍一下)"),
    re.compile(r"(能做什么|能干什么|可以做哪些|能帮我做哪些|有哪些功能|有什么功能|都会什么|都会啥)"),
]

def _is_meta_message(msg: str) -> bool:
    """判断是否为寒暄/致谢/关于功能本身的元信息，而非真实诉求。"""
    m = (msg or "").strip()
    if len(m) <= 2:
        return True
    for p in _META_MSG_PATTERNS:
        if p.search(m):
            return True
    return False


def _heuristic_prefill(user_msgs: list) -> str:
    """无 LLM 时的兜底：剔除元信息/寒暄，取前两条实质性诉求合并（过长则只取首条）。

    全部是对话元信息（无实质诉求）→ 返回空串，**禁止**拿原话凑数
    （旧实现 return user_msgs[-1] 会把「随便看看，有什么推荐吗」整句带去当占卜问题）。
    """
    substantive = [m for m in user_msgs if not _is_meta_message(m)]
    if not substantive:
        return ""
    if len(substantive) == 1:
        return substantive[0]
    merged = "；".join(substantive[:2])
    return merged if len(merged) <= 40 else substantive[0]


async def _synthesize_prefill(history: Optional[list], current_message: str) -> str:
    """从完整对话历史里提炼用户真实诉求，作为 CTA 带往目标页的预填问题。

    单轮对话 → 原话是元信息/闲逛则返回空串，否则返回原话。
    多轮对话 → 直接走启发式（剔除寒暄/元信息，合并前两条实质性诉求）。
    **不再额外调用 LLM 综合**（首页 CTA 预填无需精确一句话综合，省下每次请求
    1 次 LLM 开销，详见 BUG-02 性能优化；启发式质量足够，且规避 GLM-4-Flash
    对「无诉求输出 NONE」依从不稳而把元信息当诉求返回的问题）。
    """
    user_msgs: list = []
    for h in (history or []):
        if h.get("role") == "user" and h.get("content"):
            user_msgs.append(h["content"])
    if current_message:
        user_msgs.append(current_message)
    user_msgs = [m for m in user_msgs if m and m.strip()]

    if len(user_msgs) <= 1:
        msg = user_msgs[0] if user_msgs else (current_message or "")
        return "" if _is_meta_message(msg) else msg

    return _heuristic_prefill(user_msgs)


def _resolve_cta_birth_hint(paipan_input: dict | None) -> dict | None:
    """生成 CTA 回填用的 birthHint：优先使用系统已确认的结构化生辰（可能已被前端提取并修正农历），
    否则退而求其次使用排盘工具入参。
    """
    hint = _current_birth_hint.get()
    if _has_complete_birth_hint(hint):
        return {
            "year": int(hint["year"]),
            "month": int(hint["month"]),
            "day": int(hint["day"]),
            "timeText": str(hint["timeText"]),
            "gender": str(hint["gender"]),
        }
    if not paipan_input:
        return None
    year = paipan_input.get("year")
    month = paipan_input.get("month")
    day = paipan_input.get("day")
    if not (year and month and day):
        return None
    return {
        "year": int(year),
        "month": int(month),
        "day": int(day),
        "timeText": paipan_input.get("time_text") or paipan_input.get("timeText") or "",
        "gender": paipan_input.get("gender") or "",
    }


async def _decide_cta(
    message: str,
    full_text: str,
    used_paipan: bool,
    history: Optional[list] = None,
    paipan_input: dict | None = None,
):
    """根据对话意图决定首页小玄的引导 CTA（target + label + prefill + birthHint）。

    - used_paipan（调用了八字/紫微排盘工具）→ 强引导去卜卦
    - 否则按 LLM 回复 + 用户问题中的关键词命中具体模块
    - 都没命中 → 返回 None（不强行推送，避免打扰）
    prefill 分两态：
    - 用户有实质诉求 → 综合后的真实意图（剔除寒暄/元信息），目标页预填问题；
    - 用户无诉求（随便看看/问功能）→ prefill 置空 + 探索向 label，
      目标页自动开「整体指引」局，禁止编造「随便看看推荐」这类空话当问题。
    """
    async def _make_cta(key: str) -> dict:
        prefill = (await _synthesize_prefill(history, message)).strip()
        cta: dict = {"target": "", "label": "", "prefill": prefill}
        if prefill:
            target, label = _CTA_TARGETS[key]
            cta.update({"target": target, "label": label})
        else:
            target, label = _CTA_EXPLORE_LABELS[key]
            cta.update({"target": target, "label": label})
        # 若本轮已排盘，把结构化生辰带给目标页，避免用户重复填写
        birth_hint_for_cta = _resolve_cta_birth_hint(paipan_input)
        if birth_hint_for_cta:
            cta["birthHint"] = birth_hint_for_cta
        return cta

    if used_paipan:
        return await _make_cta("bugua")

    combined = f"{message or ''}\n{full_text or ''}"
    for pat, key in _CTA_INTENT_PATTERNS:
        if pat.search(combined):
            return await _make_cta(key)
    return None


# ---------------------------------------------------------------------------
# 外链硬清洗（输出层服务端兜底）
# prompts.py 5.2 已在指令层禁止 AI 推荐外部网站，但 GLM-4-Flash 对指令的依从并非
# 100%（2026-09-20 用户实测：AI 仍给出 personalitytest.com / hollandcodes.com /
# gallupstrengthscenter.com 等外链）。这里在输出层做**硬兜底**：任何外部 URL 一律
# 抹除，命中时补一句站内引导，使「测评类请求只推自家工具」不再依赖模型的自觉性。
# ---------------------------------------------------------------------------
_MD_LINK_RE = re.compile(r"\[([^\[\]\n]*?)\]\(\s*(?:https?://|www\.)[^)\s]*\s*\)")
# URL 合法字符集：中文/空格/括号等一律视为 URL 终止符（防「URL 后紧跟中文被一起吞掉」）
_URL_BODY = r"[A-Za-z0-9\-._~:/?#@!$&*+,;=%\[\]]*"
# 裸 URL，连同紧邻的成对/单侧括号一起抹除（避免掏空后残留 "()" 碎片）
_WRAPPED_URL_RE = re.compile(rf"[（(【\[]?\s*(?:https?://|www\.){_URL_BODY}\s*[)）】\]]?")
_EMPTY_PAREN_RE = re.compile(r"[（(]\s*[)）]")
# 注意：这里与下面两处都必须用「行内空白」[ \t] 而非 \s —— MULTILINE 下 \s 会
# 连换行一起吃掉，把「进行：\n\n- …」压成「进行\n- …」，破坏段落结构
_DANGLING_OPEN_RE = re.compile(r"[（(【\[]+[ \t]*(?=\n|$)", re.M)
_EXT_LINK_NOTICE = (
    "\n\n顺带一提，上面这几类测评玄镜站内就有现成的（性格优势觉察 / 大五人格自评，"
    "几分钟测完还能生成专属解读报告），不用去外面找～"
)
# 流式缓冲尾部保留字符数：防 "ht|tps://…" 这类 URL 起始标记被 chunk 边界切碎
_LINK_TAIL_KEEP = 10
# 切点回退时向前寻找自然边界的最大回溯距离（兜底，防超长无空白段落退化为全缓冲）
_LINK_LOOKBACK = 80
# URL 起始标记及其可能的不完整前缀（用于识别「正在成形」的 URL）
_URL_START_MARKS = ("http://", "https://", "www.")
_URL_PREFIX_SCAN = 8  # 覆盖最长标记 "https://"


def _is_url_prefix(seg: str) -> bool:
    """seg 是否可能是某 URL 起始标记的不完整前缀（如 "h" / "ht" / "https:/"）。"""
    return bool(seg) and any(mark.startswith(seg) for mark in _URL_START_MARKS)


def _url_prefix_pos(text: str) -> int:
    """返回 text 中「可能是 URL 起始标记不完整前缀」的最早位置（无则 len(text)）。

    只需检查缓冲区**开头**与**尾部窗口**：前者是上一轮 chunk 切分留下的半截标记，
    后者是正在成形的标记；中间位置一旦凑齐完整标记即由 ``_WRAPPED_URL_RE`` 兜住。
    """
    if _is_url_prefix(text[:_URL_PREFIX_SCAN].lower()):
        return 0
    for i in range(max(0, len(text) - _URL_PREFIX_SCAN), len(text)):
        if _is_url_prefix(text[i:i + _URL_PREFIX_SCAN].lower()):
            return i
    return len(text)


def _strip_external_links(text: str) -> tuple[str, bool]:
    """抹除文本中的外部 URL（含 markdown 链接与括号包裹的裸链）。

    Returns: (清洗后文本, 是否发生清洗)
    """
    if not text or ("http" not in text and "www." not in text):
        return text, False
    out = _MD_LINK_RE.sub(lambda m: m.group(1), text)   # [标题](url) → 标题
    out = _WRAPPED_URL_RE.sub("", out)                  # 裸 URL / 畸形包裹的 URL
    out = _EMPTY_PAREN_RE.sub("", out)                  # 掏空后的空括号
    out = _DANGLING_OPEN_RE.sub("", out)                # 行尾/文尾孤立开括号
    out = re.sub(r"[：:][ \t]*$", "", out, flags=re.M)  # 行尾孤立冒号（「以下是链接：」）
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return (out, True) if out != text else (text, False)


def _flush_link_safe(pending: str, final: bool = False) -> tuple[str, str, bool]:
    """从流式缓冲中切出可安全输出的前缀（不切断可能正在成形的 URL）。

    安全性依据：
      ① 保留极短尾部——防 ``"ht"`` + ``"tps://…"`` 这类起始标记被 chunk 边界切碎后
         「后半段失去 http 前缀」而绕过清洗；
      ② 切点绝不落在任何 URL 内部，若落在某个 URL 上则整体回退到该 URL 起点
         （连同其前置括号一起留到下一轮），避免把 URL 切成两半；
      ③ 对可输出段统一做 ``_strip_external_links`` 清洗。

    Returns: (可输出文本[已清洗], 残留缓冲, 本轮是否命中清洗)
    """
    if not pending:
        return "", "", False
    if final:
        out, hit = _strip_external_links(pending)
        return out, "", hit

    cut = max(0, len(pending) - _LINK_TAIL_KEEP)

    # 危险点①：缓冲区尾部/开头正在成形的 URL 起始标记（如 "…](ht" / "ht" + "tps://…"）
    danger = _url_prefix_pos(pending)
    # 危险点②：已成形但尚未结束的 URL —— 绝不能把 URL 切成两半（后半段会失去
    # "http://" 前缀而绕过清洗）。只在首个越界的 URL 处回退一次即可。
    for m in _WRAPPED_URL_RE.finditer(pending):
        if m.end() > cut:
            danger = min(danger, m.start())
            break

    if danger < len(pending):
        # 回退到最近的自然边界（换行/空白），保证 markdown 链接前缀 "[标题](" 整块留到
        # 下一轮 —— 这样流式切片能完整命中 _MD_LINK_RE，与一次性清洗结果完全一致。
        cut = danger
        floor = max(0, danger - _LINK_LOOKBACK)
        while cut > floor and pending[cut - 1] not in "\n \t":
            cut -= 1

    if cut <= 0:
        return "", pending, False
    head, rest = pending[:cut], pending[cut:]
    out, hit = _strip_external_links(head)
    return out, rest, hit


def _build_degraded(message: str) -> str:
    return f"> 小玄助手暂时不可用：{message}\n\n建议您：\n- 稍后重试\n- 或前往对应模块（卜卦/解梦/塔罗/星座）单独查询"


def _build_user_message(
    message: str,
    history: Optional[list] = None,
    birth_hint: Optional[dict] = None,
    paipan_observation: Optional[str] = None,
) -> list:
    """构造发送给 Agent 的 messages 列表（含历史、出生信息提示、服务端预排盘结果）"""
    messages: list = []
    if history:
        for h in history:
            role = h.get("role")
            content = h.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})

    # 注入真实当前日期，强制模型按真实年份理解「今年/明年/去年」，避免把今年当成训练截止年
    now = datetime.now(timezone.utc).astimezone()
    date_context = (
        f"【当前日期】{now.year}年{now.month}月{now.day}日。"
        f"注意：用户说「今年」指{now.year}年，「去年」指{now.year - 1}年，「明年」指{now.year + 1}年。"
    )

    user_message = f"{date_context}\n\n{message}"
    if birth_hint and (birth_hint.get("year") or birth_hint.get("gender")):
        b = birth_hint
        user_message += "\n\n【已知访客信息】"
        if b.get("year"):
            user_message += f" 出生：{b['year']}年{b.get('month','')}月{b.get('day','')}日"
        if b.get("timeText"):
            user_message += f" {b['timeText']}"
        if b.get("gender"):
            user_message += f" {b['gender']}"

    # 服务端已完成排盘：把真实结果前置注入，模型只需基于数据解读，
    # 无需（也不允许）再调工具或向用户索要生辰。
    if paipan_observation:
        user_message += f"\n\n{paipan_observation}"

    messages.append({"role": "user", "content": user_message})
    return messages


async def run_home_agent(
    message: str,
    history: Optional[list] = None,
    birth_hint: Optional[dict] = None,
) -> tuple[str, bool, Optional[str]]:
    """运行首页通用命理助手（非流式）

    Returns: (text, degraded, degraded_reason)
    """
    if not config.llm_available:
        return _build_degraded("AI_API_KEY 未配置"), True, "AI_API_KEY 未配置"

    if budget_broken():
        return _build_degraded("单日预算超限"), True, "单日预算超限"

    birth_hint = _normalize_birth_hint(birth_hint, message, history)
    cache_key = _home_cache_key(message, history, birth_hint)
    cached = cache_get(cache_key)
    if cached:
        try:
            logger.info("[home] 缓存命中: %s", cache_key)
            return json.loads(cached)["text"], False, None
        except Exception:
            pass

    token = _current_birth_hint.set(birth_hint)
    try:
        agent = _get_agent()
        # 生辰齐全 → 服务端直接排盘，把真实结果注入，不依赖 LLM 是否愿意调工具
        paipan_observation, _ = await _prefetch_paipan(birth_hint)
        result = await agent.ainvoke(
            {"messages": _build_user_message(message, history, birth_hint, paipan_observation)}
        )

        messages = result.get("messages", [])
        text = ""
        for msg in reversed(messages):
            if hasattr(msg, "content") and getattr(msg, "type", None) == "ai":
                text = msg.content
                break

        if not text:
            return "> 小玄助手未返回内容，请重试。", True, "Agent 未输出内容"

        # 输出层硬清洗：抹除外部 URL（prompts 5.2 的服务端兜底），命中则补站内引导
        text, link_hit = _strip_external_links(text)
        if link_hit:
            text += _EXT_LINK_NOTICE

        if HOME_AGENT_DISCLAIMER not in text:
            text += f"\n\n---\n*{HOME_AGENT_DISCLAIMER}*"

        # 非流式不产 CTA，cta 记为 None（流式命中时复用同一 key 取不到 CTA，符合预期）
        cache_set(cache_key, json.dumps({"text": text, "cta": None}, ensure_ascii=False), ttl_seconds=_HOME_CACHE_TTL)
        return text, False, None

    except Exception as e:
        reason = str(e)
        return _build_degraded(f"Agent 运行异常：{reason}"), True, reason
    finally:
        _current_birth_hint.reset(token)


async def stream_home_agent(
    message: str,
    history: Optional[list] = None,
    birth_hint: Optional[dict] = None,
) -> AsyncIterator[dict]:
    """运行首页通用命理助手（流式），yield SSE 事件

    Events: open, tool_start, tool_end, delta, meta, done, error
    """
    if not config.llm_available:
        yield {"event": "error", "data": {"reason": "AI_API_KEY 未配置"}}
        return

    if budget_broken():
        yield {"event": "error", "data": {"reason": "单日预算超限"}}
        return

    birth_hint = _normalize_birth_hint(birth_hint, message, history)
    cache_key = _home_cache_key(message, history, birth_hint)
    cached = cache_get(cache_key)
    if cached:
        try:
            parsed = json.loads(cached)
            logger.info("[home] 缓存命中（流式回放）: %s", cache_key)
            yield {"event": "open", "data": {}}
            yield {"event": "delta", "data": {"chunk": parsed.get("text", "")}}
            meta_data = {
                "provider": config.ai_provider,
                "model": config.ai_model_chat,
                "disclaimer": HOME_AGENT_DISCLAIMER,
                "degraded": False,
                "degradedReason": None,
                "cached": True,
            }
            cta = parsed.get("cta")
            if cta:
                meta_data["cta"] = cta
            yield {"event": "meta", "data": meta_data}
            yield {"event": "done", "data": {"disclaimer": HOME_AGENT_DISCLAIMER}}
            return
        except Exception:
            pass

    yield {"event": "open", "data": {}}

    used_paipan = False  # 是否调用了排盘类工具
    full_text = ""       # 累积 LLM 完整回复（已过外链清洗），用于 meta 阶段意图识别与缓存
    paipan_input: dict | None = None  # 最近一次排盘工具的入参，用于 CTA 回填
    link_buffer = ""     # 外链清洗的流式缓冲（避免 URL 跨 chunk 漏出）
    link_hit = False     # 本轮是否清洗掉了外部链接
    token = _current_birth_hint.set(birth_hint)

    # 生辰齐全 → 服务端先自己把盘排了（确定性判断，不赌 LLM 的 ReAct 决策）。
    # 这样既保证模型有真实数据可依，也保证 CTA 100% 带上 birthHint。
    paipan_observation, prefetched_input = await _prefetch_paipan(birth_hint)
    if prefetched_input:
        used_paipan = True
        paipan_input = prefetched_input
        yield {
            "event": "tool_start",
            "data": {"tool": "bazi_paipan_tool（系统自动）", "input": str(prefetched_input)},
        }

    try:
        agent = _get_agent()

        # 流式边界去重：极少数情况下相邻两次 on_chat_model_stream 会带回完全相同
        # 的 chunk（langchain 流式累积 + 网络重连等边界情况），这里直接跳过
        last_chunk_text = ""

        async for event in agent.astream_events(
            {"messages": _build_user_message(message, history, birth_hint, paipan_observation)},
            version="v2",
        ):
            evt_type = event.get("event", "")
            name = event.get("name", "")
            data = event.get("data", {})

            if evt_type == "on_chat_model_stream":
                chunk = data.get("chunk")
                text = getattr(chunk, "content", "") if chunk else ""
                if not text:
                    continue
                if text == last_chunk_text:
                    continue
                last_chunk_text = text
                # 外链硬清洗：先入缓冲再按安全边界切出，防 URL 跨 chunk 漏出
                link_buffer += text
                safe, link_buffer, hit = _flush_link_safe(link_buffer)
                if hit:
                    link_hit = True
                if safe:
                    full_text += safe
                    yield {"event": "delta", "data": {"chunk": safe}}

            elif evt_type == "on_tool_start":
                if "paipan" in name:
                    used_paipan = True
                    try:
                        raw_input = data.get("input", {})
                        # LangGraph 有时把 input 包在 dict，有时直接是 dict
                        paipan_input = raw_input if isinstance(raw_input, dict) else {}
                    except Exception:
                        paipan_input = None
                yield {"event": "tool_start", "data": {"tool": name, "input": str(data.get("input", ""))[:500]}}

            elif evt_type == "on_tool_end":
                output = data.get("output", "")
                if hasattr(output, "content"):
                    output = output.content
                yield {"event": "tool_end", "data": {"tool": name, "output": str(output)[:1000]}}

        # 冲净清洗缓冲（含最后一段未以换行结尾的文本）
        tail, link_buffer, hit = _flush_link_safe(link_buffer, final=True)
        if hit:
            link_hit = True
        if tail:
            full_text += tail
            yield {"event": "delta", "data": {"chunk": tail}}
        # 命中外链清洗 → 末尾补一句站内引导（评测类场景把用户留在站内）
        if link_hit:
            full_text += _EXT_LINK_NOTICE
            yield {"event": "delta", "data": {"chunk": _EXT_LINK_NOTICE}}

        meta_data: dict = {
            "provider": config.ai_provider,
            "model": config.ai_model_chat,
            "disclaimer": HOME_AGENT_DISCLAIMER,
            "degraded": False,
            "degradedReason": None,
        }
        # 根据对话意图智能引导到对应模块（卜卦/塔罗/星座/数字密码），
        # 并把用户的问题带过去，目标页会自动预填
        cta = await _decide_cta(message, full_text, used_paipan, history, paipan_input)
        if cta:
            meta_data["cta"] = cta
        # 流式结果缓存（含 CTA，命中回放时跳过 _decide_cta 的二次 LLM 合成）
        try:
            cache_set(cache_key, json.dumps({"text": full_text, "cta": cta}, ensure_ascii=False), ttl_seconds=_HOME_CACHE_TTL)
        except Exception:
            pass
        yield {"event": "meta", "data": meta_data}
        yield {"event": "done", "data": {"disclaimer": HOME_AGENT_DISCLAIMER}}

    except Exception as e:
        logger.error(f"home_agent 流式异常: {e}", exc_info=True)
        yield {"event": "error", "data": {"code": "STREAM_ERROR", "message": "小玄助手暂时不可用，请稍后重试"}}
    finally:
        _current_birth_hint.reset(token)
