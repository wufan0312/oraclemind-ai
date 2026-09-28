"""玄镜 OracleMind · 综合报告 Agent（流式）

移植自 TS 版 src/agent/reportAgent.ts + reportGraph.ts：
- 规则化 analyzer（零 LLM 成本）按问题关键词选术数
- paipan 节点并行回调后端（确定性计算）：八字/紫微/奇门/六爻/梅花/数字命理
- synthesizer / reviewer / writer 三阶段 LLM 综合 + 反思 + 终稿
- 条件边：reviewer 判定需重综合且重试<2 时回到 synthesizer（防死循环）
- SSE 事件流：open → phase* → delta* → meta(report) → done / error

与 ming/dream 区别：输出结构化 JSON（SummaryOutput 兼容），delta 展示思考过程，最终 report 在 meta。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, AsyncIterator, Optional

import aiohttp
import hashlib

from src.config import config
from src.services.budget import cache_get, cache_set
from src.services.provider import llm_provider
from src.harness.observability.tracer import mark_phase

logger = logging.getLogger(__name__)

# ============================ 输出安全护栏 ============================
# 服务端内容过滤：拦截风水/算命场景下的恐吓性、诱导消费与改运话术。
# 前端 disclaimer 不拦截内容，真正的内容闸口必须落在服务端输出层。
_FENGSHUI_BLOCKLIST = [
    "血光", "伤丁", "绝嗣", "破财", "灾祸", "横祸", "凶灾", "开光", "法事",
    "化煞", "化解", "消灾", "改命", "改运", "转运", "趋吉避凶", "趋吉",
    "五帝铜钱", "铜葫芦", "铜麒麟", "貔貅", "金蟾", "泰山石敢当", "泰山石",
    "八卦镜", "风水轮", "凸面镜", "铜风铃", "黄水晶", "聚宝盆", "水晶洞",
    "招财", "催旺", "镇宅", "泄煞", "辟邪", "挡煞", "化病",
]
_BLOCK_RE = re.compile("|".join(re.escape(w) for w in _FENGSHUI_BLOCKLIST))


def _sanitize(text: str) -> str:
    """对单段文本做关键词脱敏，命中黑名单词替换为〔已过滤〕。"""
    if not text:
        return text
    return _BLOCK_RE.sub("〔已过滤〕", text)


def _sanitize_obj(o: Any) -> Any:
    """递归脱敏 report 等结构化对象中的字符串字段。"""
    if isinstance(o, str):
        return _sanitize(o)
    if isinstance(o, list):
        return [_sanitize_obj(x) for x in o]
    if isinstance(o, dict):
        return {k: _sanitize_obj(v) for k, v in o.items()}
    return o


# 同 key 并发请求去重锁：避免缓存未写入前多个相同请求同时跑 LLM
_report_locks: dict[str, asyncio.Lock] = {}

REPORT_DISCLAIMER = (
    "本报告由玄镜 AI 综合命理分析师生成，融合多术数交叉验证，"
    "仅供文化参考，不构成专业决策建议。"
)

MODULE_LABELS: dict[str, str] = {
    "bazi": "八字四柱",
    "ziwei": "紫微斗数",
    "qimen": "奇门遁甲",
    "liuyao": "六爻",
    "meihua": "梅花易数",
    # 三式补齐
    "liuren": "大六壬",
    "taiyi": "太乙神数",
    "numerology": "数字命理",
}

PAIPAN_URLS: dict[str, str] = {
    "bazi": "/api/v1/bazi/paipan",
    "ziwei": "/api/v1/ziwei/paipan",
    "qimen": "/api/v1/qimen/paipan",
    "liuyao": "/api/v1/gua/liuyao",
    "meihua": "/api/v1/gua/meihua",
    "liuren": "/api/v1/liuren/paipan",
    "taiyi": "/api/v1/taiyi/paipan",
    "numerology": "/api/v1/numerology/paipan",
}

_REPORT_SYNTHESIZER_PROMPT = """你是玄镜·综合命理分析师，精通八字、紫微、奇门、六爻、梅花等多术数的交叉验证。你将收到多个术数的排盘结果，需要综合分析，找出共识与分歧。

【输入说明】
你会收到：
- 用户问题（question）
- 各术数排盘精简结果（按术数分组，每术数含关键字段 + analysis）
- 跨页测算结论（塔罗/星座/数字命理等，用户在其他页面已完成测算，与排盘结果同权参与交叉验证）
- 部分术数可能排盘失败（errors），忽略失败的，基于可用术数分析

【你的任务】
输出严格 JSON：
```json
{
  "overallSummary": "≤150字整体概述，融合各术数核心结论",
  "consensus": [
    { "label": "事业运", "score": 85, "reason": "八字日主旺相+紫微官禄宫吉星+奇门开门，三盘共振主事业向上" },
    ... 共6维度：事业运/财运/感情运/健康/学业成长/人际贵人
  ],
  "keyFindings": [
    "发现1：如「八字与紫微均提示2027年事业转折」",
    "发现2：如「奇门九星天辅星+开门利文书学业」"
  ],
  "divergences": [
    { "desc": "八字示财运平稳，梅花体克用示短期破财", "modules": ["bazi","meihua"], "resolution": "以应期远近调和：长期看八字平稳，短期1月内防小额支出" }
  ]
}
```

【分析原则】
1. consensus 的 score 在 70-95 区间，多盘共振取高值，分歧取低值
2. keyFindings 提炼 3-5 条跨术数的共振发现
3. divergences 如实记录矛盾，并给出 resolution 调和建议
4. 若仅 1 种术数可用，consensus 评分偏中性（75-82），divergences 为空
5. 不得编造数据，只基于输入的排盘结果分析"""

_REPORT_REVIEWER_PROMPT = """你是玄镜·报告审稿人。请审视综合分析的逻辑自洽性与数据一致性。

【输入说明】
你会收到 synthesizer 的综合分析 JSON（overallSummary / consensus / keyFindings / divergences）。

【你的任务】
输出严格 JSON：
```json
{
  "hasConflict": false,
  "conflicts": [
    { "desc": "矛盾描述", "severity": "high|medium|low" }
  ],
  "needResynth": false,
  "notes": "审稿说明，如「逻辑自洽，无重大矛盾」或「建议重新综合时关注XX」"
}
```

【判定标准】
- hasConflict=true 当且仅当存在 severity=high 的矛盾（如事业运 score=90 但 keyFindings 说事业受阻）
- needResynth=true 当且仅当 hasConflict=true 且矛盾影响整体结论
- 最多触发 1 次重新综合（防止死循环，由 graph 控制）
- 轻微分歧（divergences 已给 resolution）不算 conflict
- 输出 notes 说明审稿结论"""

_REPORT_WRITER_PROMPT = """你是玄镜·报告撰写师。基于综合分析与审稿结果，输出最终用户可见的结构化报告。

【输入说明】
你会收到：
- 用户问题（question）
- 综合分析（overallSummary / consensus / keyFindings / divergences）
- 审稿结果（conflicts / notes）
- 已成功排盘的术数列表（successModules）

【你的任务】
输出严格 JSON（与现有 SummaryOutput 兼容，前端可直接渲染卡片）：
```json
{
  "ok": true,
  "summary": "200-300字白话整体概述，面向用户，温和专业。必须引用至少2-3个术数的具体发现作为依据，不能泛泛而谈。例如：八字日主庚金得月令帮扶主事业有冲劲；紫微官禄宫见化权主职场有话语权；六爻官鬼持世变出父母爻主近期有文书/合约类变动。禁止输出'整体运势积极，事业和感情表现良好'这类空洞表述。",
  "keyFindings": ["跨术数共振发现1（50字内，说明哪几个术数一致，指向什么结论）", "发现2", "发现3"],
  "divergences": [
    { "desc": "矛盾描述（如八字看财运稳，六爻看短期破财）", "modules": ["bazi", "liuyao"], "resolution": "如何调和，给出具体建议" }
  ],
  "advice": [
    { "title": "⚡ 立即行动", "items": ["可落地建议1", "建议2", "建议3"] },
    { "title": "📅 短期（1-3月）", "items": ["建议1", "建议2", "建议3"] },
    { "title": "🚀 中长期（2027+）", "items": ["建议1", "建议2", "建议3"] }
  ],
  "outlook": "80-120字近期趋势提示，必须结合当前流年/流月给出具体方向",
  "timeline": [
    { "period": "2026 秋", "overview": "40-60字该阶段一句话总览，紧扣当前流月" },
    { "period": "2026 冬", "overview": "..." },
    { "period": "2027 春", "overview": "..." },
    { "period": "2027 秋", "overview": "..." }
  ],
  "consensus": [
    { "label": "事业运", "score": 85, "reason": "八字官星得令、紫微官禄宫化权，两术数一致看好职场话语权" },
    { "label": "财运", "score": 76, "reason": "六爻妻财持世但被日辰所克，短期见财但守财需谨慎" },
    { "label": "感情运", "score": 72, "reason": "八字夫妻宫平稳、塔罗感情牌偏保守，整体中性偏稳" },
    { "label": "健康", "score": 85, "reason": "五行日主中和、紫微疾厄宫无煞，基础体质尚可" },
    { "label": "学业/成长", "score": 78, "reason": "文昌入命、数字命理主运偏进取，适合持续学习" },
    { "label": "人际/贵人", "score": 82, "reason": "天乙贵人临命、奇门开门生宫，易得长辈提携" }
  ],
  "cards": [
    { "icon": "📈", "name": "近期趋势", "score": "→ 平稳", "desc": "..." },
    { "icon": "🎯", "name": "关键决策期", "score": "2027春", "desc": "..." },
    { "icon": "⚠️", "name": "风险提示", "score": "中", "desc": "..." },
    { "icon": "💎", "name": "天赋优势", "score": "发掘", "desc": "..." }
  ]
}
```

【撰写原则】
1. summary 与 outlook 面向用户白话，不出现术数术语堆砌
2. advice 三栏必须可落地（行动建议，非空泛话）
3. consensus score 直接采用综合分析的评分（6维度保持一致）
4. cards 4 张：近期趋势/关键决策期/风险提示/天赋优势，desc 简洁
5. 若有 conflicts，在 cards 的「风险提示」中温和体现，不恐吓
6. ok 恒为 true（只要有任一术数可用）
7. 不得编造数据，所有结论源自综合分析
8. consensus 每一维都必须填 reason（50 字内），说明该维度分数的具体依据——指出哪几个术数一致指向此结论，不能空泛。前端会逐维展示 reason 作为「分维解读」
9. timeline 必须输出 3~4 个时间节点（从当前流月起向后推进，间隔 3~6 个月），period 为时间标签（如「2026 秋」「2027 春」），overview 为该阶段一句话总览（≤60字）。须与 advice 三阶段（立即/短期/中长期）呼应、可落地，让用户能按时间轴对照执行"""


# report agent 专属 prompt 内容哈希：prompt 模板改动后旧缓存自动失效，避免返回过期格式
_REPORT_PROMPT_HASH = hashlib.md5(
    (_REPORT_SYNTHESIZER_PROMPT + _REPORT_REVIEWER_PROMPT + _REPORT_WRITER_PROMPT).encode("utf-8")
).hexdigest()[:10]

# ============================ 工具函数 ============================

# timeline 兜底：LLM 未给出合规时间轴时的保守三阶段（与 advice 三栏呼应)
_FALLBACK_TIMELINE = [
    {"period": "当下", "overview": "夯实基础、安顿身心，先从最紧迫的一件小事做起。"},
    {"period": "短期", "overview": "围绕核心目标深耕一项技能，主动修复重要关系。"},
    {"period": "中长期", "overview": "把握运势向上窗口期，做稳健的财务与职业规划。"},
]


def _normalize_timeline(raw: object) -> list[dict]:
    """把 LLM 的 timeline 对齐前端渲染契约：3~4 段，每段含 period + overview。

    段数不足或字段缺失时整体回落兜底 —— 半截时间轴比没有更容易误导用户。
    """
    if not isinstance(raw, list):
        return list(_FALLBACK_TIMELINE)
    items = [
        t for t in raw
        if isinstance(t, dict) and str(t.get("period") or "").strip()
    ]
    if not (3 <= len(items) <= 4):
        return list(_FALLBACK_TIMELINE)
    return [
        {
            "period": str(t.get("period")).strip()[:16],
            "overview": str(t.get("overview") or "").strip()[:80],
        }
        for t in items
    ]


def _parse_json(text: str) -> Optional[dict]:
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    raw = m.group(1) if m else text
    try:
        return json.loads(raw.strip())
    except Exception:
        s, e = raw.find("{"), raw.rfind("}")
        if s >= 0 and e > s:
            try:
                return json.loads(raw[s : e + 1])
            except Exception:
                return None
    return None


def _friendly_synth(synthesis: dict, success: list, cross_labels: list) -> str:
    """把 synthesizer 的结构化 JSON 转成推演过程可读摘要（不暴露原始 JSON）。"""
    parts: list[str] = []
    fused = f"已融合 {len(success)} 种术数排盘" + (
        f"与 {len(cross_labels)} 项跨页测算" if cross_labels else ""
    )
    parts.append(f"{fused}，提取综合概述与关键发现。")
    ov = (synthesis.get("overallSummary") or "").strip()
    if ov:
        parts.append(ov[:120] + ("…" if len(ov) > 120 else ""))
    kf = synthesis.get("keyFindings") or []
    if isinstance(kf, list) and kf:
        diverged = "存在术数分歧，已在后续审稿中标记" if synthesis.get("divergences") else "各术数结论大体一致"
        parts.append(f"关键发现 {len(kf)} 条；{diverged}。")
    return "\n".join(parts)


def _friendly_review(review: dict) -> str:
    """把 reviewer 的结构化 JSON 转成推演过程可读摘要。"""
    if review.get("hasConflict"):
        conflicts = review.get("conflicts") or []
        note = f"（{review['notes']}）" if review.get("notes") else ""
        return f"审稿完成：发现 {len(conflicts)} 处术数结论冲突，已标记待复核。{note}"
    return "审稿完成：各术数结论一致，无需重综合。"


def _friendly_writer() -> str:
    """writer 阶段完成后的推演过程摘要。"""
    return "已撰写最终结构化报告，正在汇总六维评分与建议…"


def select_modules(question: str) -> list[str]:
    """根据问题关键词选术数（规则化，零 LLM 成本）"""
    q = (question or "").lower()
    has = lambda kws: any(k in q for k in kws)
    if has(["事业", "工作", "升迁", "求职", "创业", "职业", "job", "career"]):
        return ["bazi", "ziwei", "qimen"]
    if has(["财", "投资", "金钱", "收益", "财路"]):
        return ["bazi", "ziwei"]
    if has(["感情", "婚姻", "桃花", "恋爱", "复合", "love"]):
        return ["ziwei", "bazi"]
    if has(["择时", "方位", "出行", "搬家", "动土", "开业", "择日", "吉时"]):
        return ["qimen", "meihua"]
    if has(["能不能", "是否", "可以吗", "会吗", "行不行", "吉凶"]):
        return ["liuyao", "meihua"]
    if has(["学业", "考试", "升学", "考公", "考研", "study", "exam"]):
        return ["ziwei", "meihua"]
    if has(["健康", "疾病", "身体", "病"]):
        return ["bazi", "ziwei"]
    if has(["整体", "运势", "今年", "今年运", "综合", "命理"]):
        # numerology 排盘接口已就绪，综合类问题让它常态参与（此前数字命理只能靠跨页池带入）
        return ["bazi", "ziwei", "qimen", "liuyao", "numerology"]
    return ["bazi", "ziwei", "qimen"]


def _slim_result(module: str, data: Any) -> Any:
    """各术数结果精简（避免 LLM 输入过长）"""
    if not isinstance(data, dict):
        return data
    if module == "bazi":
        return {
            "dayMaster": data.get("dayMaster"),
            "dayMasterWuxing": data.get("dayMasterWuxing"),
            "shengxiao": data.get("shengxiao"),
            "pillars": data.get("pillars"),
            "wuxing": data.get("wuxing"),
            "yongshen": data.get("yongshen"),
            "currentDayun": (data.get("dayun")[0] if isinstance(data.get("dayun"), list) and data.get("dayun") else None),
            "recentLiunian": (data.get("liunian")[:4] if isinstance(data.get("liunian"), list) else []),
            "analysis": data.get("analysis"),
        }
    if module == "ziwei":
        return {
            "mingGong": data.get("mingGong"),
            "wuxingJu": data.get("wuxingJu"),
            "mainStars": data.get("mainStars"),
            "twelveGongs": (data.get("twelveGongs")[:6] if isinstance(data.get("twelveGongs"), list) else []),
            "sihua": data.get("sihua"),
            "analysis": data.get("analysis"),
        }
    if module == "qimen":
        return {
            "ju": data.get("ju"),
            "yinYangDun": data.get("yinYangDun"),
            "jiuGong": data.get("jiuGong"),
            "sanQi": data.get("sanQi"),
            "action": data.get("action"),
            "analysis": data.get("analysis"),
        }
    if module == "liuyao":
        return {
            "benGua": data.get("benGua"),
            "bianGua": data.get("bianGua"),
            "yongShen": data.get("yongShen"),
            "liuQin": data.get("liuQin"),
            "shiYing": data.get("shiYing"),
            "dongYao": data.get("dongYao"),
            "analysis": data.get("analysis"),
        }
    if module == "meihua":
        return {
            "benGua": data.get("benGua"),
            "huGua": data.get("huGua"),
            "bianGua": data.get("bianGua"),
            "tiYong": data.get("tiYong"),
            "shengKe": data.get("shengKe"),
            "analysis": data.get("analysis"),
        }
    if module == "liuren":
        san = data.get("sanChuan") if isinstance(data.get("sanChuan"), dict) else {}
        return {
            "yueJiang": data.get("yueJiang"),
            "zhanShi": data.get("zhanShi"),
            "riGanZhi": data.get("riGanZhi"),
            "siKe": data.get("siKe"),
            "sanChuan": {"keTi": san.get("keTi"), "method": san.get("method"),
                         "items": san.get("items")},
            "kongWang": data.get("kongWang"),
            "analysis": data.get("analysis"),
        }
    if module == "taiyi":
        return {
            "ganZhi": data.get("ganZhi"),
            "ju": data.get("ju"),
            "dun": data.get("dun"),
            "taiYiGong": data.get("taiYiGong"),
            "wenChang": data.get("wenChang"),
            "shiJi": data.get("shiJi"),
            "zhuSuan": data.get("zhuSuan"),
            "keSuan": data.get("keSuan"),
            "verdict": data.get("verdict"),
            "analysis": data.get("analysis"),
        }
    if module == "numerology":
        return {
            "lifePath": data.get("lifePath"),
            "expression": data.get("expression"),
            "soulUrge": data.get("soulUrge"),
            "analysis": data.get("analysis"),
        }
    return data


async def paipan_executor(modules: list[str], birth: dict) -> dict:
    """并行执行多术数排盘，回调后端 PAIPAN_API_BASE"""
    base = config.paipan_api_base.rstrip("/")
    base_body = {
        "year": birth.get("year"),
        "month": birth.get("month"),
        "day": birth.get("day"),
        "hour": birth.get("hour"),
        "timeText": birth.get("timeText", ""),
        "gender": birth.get("gender", ""),
        "question": birth.get("question", ""),
    }
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}

    async def _one(m: str):
        # 数字命理模块按「农历口径」排盘（与前端数字命理页 / 报告命主卡一致）；
        # 前端带 lunarYear/lunarMonth/lunarDay 则用之，否则沿用公历（兼容老客户端）。
        if m == "numerology":
            ly = birth.get("lunarYear")
            lm = birth.get("lunarMonth")
            ld = birth.get("lunarDay")
            if ly is not None and lm is not None and ld is not None:
                body = {**base_body, "year": ly, "month": lm, "day": ld}
            else:
                body = base_body
        else:
            body = base_body
        url = base + PAIPAN_URLS[m]
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, json=body, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status != 200:
                        txt = await resp.text()
                        raise RuntimeError(f"HTTP {resp.status}: {txt[:120]}")
                    return m, await resp.json()
        except Exception as e:  # noqa: BLE001
            return m, e

    import asyncio
    out = await asyncio.gather(*[_one(m) for m in modules])
    for m, val in out:
        if isinstance(val, Exception):
            errors[m] = str(val)
        else:
            results[m] = _slim_result(m, val)

    return {"results": results, "errors": errors, "success": list(results.keys())}


def _build_degraded_report(reason: str, modules: list[str] = []) -> dict:
    return {
        "ok": True,
        "summary": f"综合报告暂不可用：{reason}。"
        + (f"已尝试排盘：{ '、'.join(MODULE_LABELS.get(m, m) for m in modules) }" if modules else ""),
        "keyFindings": ["当前为降级输出，未进行多术数交叉验证。"],
        "divergences": [],
        "advice": [
            {"title": "⚡ 立即行动", "items": ["稍后重试，或拆解为单一术数（八字/紫微）单独查看"]},
            {"title": "📅 短期（1-3月）", "items": ["围绕核心目标深耕一项可落地的技能"]},
            {"title": "🚀 中长期（2027+）", "items": ["把握运势向上窗口期，做稳健规划"]},
        ],
        "outlook": "近期宜稳不宜激进，先夯实基础。",
        "consensus": [
            {"label": "事业运", "score": 78},
            {"label": "财运", "score": 76},
            {"label": "感情运", "score": 72},
            {"label": "健康", "score": 80},
            {"label": "学业/成长", "score": 75},
            {"label": "人际/贵人", "score": 78},
        ],
        "cards": [
            {"icon": "📈", "name": "近期趋势", "score": "→ 待测", "desc": "AI 不可用，已切换本地规则。"},
            {"icon": "🎯", "name": "关键决策期", "score": "待定", "desc": "需结合排盘信息确定。"},
            {"icon": "⚠️", "name": "风险提示", "score": "中", "desc": "注意情绪管理，避免冲动决策。"},
            {"icon": "💎", "name": "天赋优势", "score": "待发掘", "desc": "结合命盘格局发掘天赋。"},
        ],
    }


# ============================ 流水线（SSE 生成器） ============================
def _sanitize_cross_readings(raw: Any) -> list[dict]:
    """清洗前端传来的跨页测算结论（塔罗/星座/数字命理）。

    Args:
        raw: 前端共享池原始列表 [{type, label, summary}]，可能为 None/脏数据。

    Returns:
        最多 6 条、每条 summary 截断到 300 字的干净列表（防 prompt 注入超长）。
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("type") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if not label or not summary:
            continue
        # 同 label 去重，保留先出现的（前端已按 type 去重，这里兜底）
        if any(x["label"] == label for x in out):
            continue
        out.append({"label": label, "type": str(item.get("type") or label), "summary": summary[:300]})
        if len(out) >= 6:
            break
    return out


_REPORT_CACHE_TTL = 3600 * 6  # 报告推演极贵（多术数排盘 + 三阶段 LLM），长缓存 6h

# 阶段产物缓存 TTL（#14 轻量续传）：比完整报告短，避免用过期的排盘/综合结论。
# 取值 2h —— 覆盖「推演中断后回来重跑」的典型间隔（分钟~小时级）。
_STAGE_CACHE_TTL = 3600 * 2


def _stage_cache_key(stage: str, question: str, birth: dict, modules: list[str], variant: int) -> str:
    """阶段缓存键：stage + 问题 + 命盘 + 术数集合。

    variant 不进 paipan 的键（排盘与表述无关），但进 synthesizer 的键
    （换个说法要求换切入点，综合分析也会随之变化）。
    """
    payload: dict = {"b": birth or {}, "m": sorted(modules)}
    if stage != "paipan":
        payload["q"] = question
        payload["v"] = variant
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return f"stage:{stage}:v1:{hashlib.md5(raw.encode('utf-8')).hexdigest()[:16]}"


def _stage_cache_get(stage: str, question: str, birth: dict, modules: list[str], variant: int) -> Any:
    """读取阶段产物；未命中/损坏一律返回 None（缓存只做加速，不参与正确性）。"""
    try:
        raw = cache_get(_stage_cache_key(stage, question, birth, modules, variant))
        if not raw:
            return None
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def _stage_cache_set(
    stage: str, question: str, birth: dict, modules: list[str], variant: int, value: Any
) -> None:
    """写入阶段产物。缓存失败不影响主流程。"""
    try:
        cache_set(
            _stage_cache_key(stage, question, birth, modules, variant),
            json.dumps(value, ensure_ascii=False),
            ttl_seconds=_STAGE_CACHE_TTL,
        )
    except Exception:  # noqa: BLE001
        pass


def _report_cache_key(question: str, birth: dict, cross_readings: Optional[list[dict]], variant: int) -> str:
    """缓存键：问题 + 出生 + 跨页结论 + variant + prompt 哈希。variant>0（换个说法）视为不同请求。"""
    raw = json.dumps(
        {
            "q": question,
            "b": birth or {},
            # 排除报告自身结论（type=='report'）：否则报告推演完回写跨页池后，
            # 下次进入 cross_readings 含自身结论、缓存键随之变化，导致永远重推。
            "c": [x for x in _sanitize_cross_readings(cross_readings) if x.get("type") != "report"],
            "v": variant,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"report:v2:{hashlib.md5(raw.encode('utf-8')).hexdigest()[:16]}:{_REPORT_PROMPT_HASH}"


def _patch_meta_cache_hit(ev: dict, cache_hit: bool) -> dict:
    """在 meta 事件中注入 cacheHit 字段，用于前端/日志观测缓存命中情况。"""
    if ev.get("event") != "meta":
        return ev
    try:
        raw = ev.get("data", {})
        # 录制帧中的 data 可能是已解析的 dict，也可能是 JSON 字符串（回放时）
        data = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(data, dict):
            data["cacheHit"] = cache_hit
            return {**ev, "data": json.dumps(data, ensure_ascii=False) if isinstance(raw, str) else data}
    except Exception:  # noqa: BLE001
        pass
    return ev


async def run_report_agent(
    question: str, birth: dict, cross_readings: Optional[list[dict]] = None, variant: int = 0
) -> AsyncIterator[dict]:
    """流式运行综合报告 Agent（带结果缓存 + 同 key 并发去重）。

    相同参数命中时直接回放已录制的完整事件流（含排盘 phase 与三阶段推演 delta），
    不再重跑任何 LLM 与排盘，节省大量 token。
    若同 key 有请求正在生成中，后续请求会等待其完成并复用结果，避免缓存穿透。
    """
    key = _report_cache_key(question, birth, cross_readings, variant)
    cached = cache_get(key)
    if cached:
        try:
            frames = json.loads(cached)
            if isinstance(frames, list) and frames:
                for ev in frames:
                    yield _patch_meta_cache_hit(ev, True)
                logger.info("[report] 缓存命中回放: %s", key)
                return
        except Exception:  # noqa: BLE001
            logger.warning("[report] 缓存回放失败，重新生成: %s", key)

    lock = _report_locks.setdefault(key, asyncio.Lock())
    if lock.locked():
        logger.info("[report] 同 key 请求正在生成，等待复用: %s", key)
    async with lock:
        # 等待期间可能已有其他请求写入缓存，再次检查
        cached2 = cache_get(key)
        if cached2:
            try:
                frames = json.loads(cached2)
                if isinstance(frames, list) and frames:
                    for ev in frames:
                        yield _patch_meta_cache_hit(ev, True)
                    logger.info("[report] 等待后缓存命中回放: %s", key)
                    return
            except Exception:  # noqa: BLE001
                pass

        recorded: list[dict] = []
        async for ev in _run_report_agent_inner(question, birth, cross_readings, variant):
            recorded.append(ev)
            yield _patch_meta_cache_hit(ev, False)

        try:
            cache_set(key, json.dumps(recorded, ensure_ascii=False), ttl_seconds=_REPORT_CACHE_TTL)
            logger.info("[report] 推演结果已缓存: %s", key)
        except Exception:  # noqa: BLE001
            pass


async def _run_report_agent_inner(
    question: str, birth: dict, cross_readings: Optional[list[dict]] = None, variant: int = 0
) -> AsyncIterator[dict]:
    """内部实现：见 run_report_agent 文档。"""
    cross = _sanitize_cross_readings(cross_readings)
    cross_types = {c["type"] for c in cross}

    # 1. analyzer（规则化选术数，零成本）
    modules = select_modules(question)
    mark_phase("analyzer", meta={"modules": modules, "crossLabels": cross_labels})
    # 跨页已有数字命理结论时不再重复排盘：跨页结论含姓名核心数字，信息比无姓名的排盘更全
    if "numerology" in cross_types:
        modules = [m for m in modules if m != "numerology"]
    module_labels = [MODULE_LABELS.get(m, m) for m in modules]

    # 排盘已覆盖的术数，跳过同名跨页结论（否则前端 chips 会出现两条「数字命理」）
    cross = [c for c in cross if c["label"] not in module_labels]
    cross_labels = [c["label"] for c in cross]

    yield {"event": "open", "data": {}}

    try:
        yield {"event": "phase", "data": {
            "phase": "analyzer",
            "selectedModules": modules,
            "selectedLabels": [MODULE_LABELS.get(m, m) for m in modules] + cross_labels,
        }}

        # 2. paipan（并行排盘）—— 命中阶段缓存则直接复用（#14 中断后续传）
        cached_paipan = _stage_cache_get("paipan", question, birth, modules, variant)
        resumed_paipan = isinstance(cached_paipan, dict) and "results" in cached_paipan
        paipan = cached_paipan if resumed_paipan else await paipan_executor(modules, birth)
        if not resumed_paipan:
            _stage_cache_set("paipan", question, birth, modules, variant, paipan)
        success = paipan["success"]
        mark_phase("paipan", meta={
            "success": success,
            "errors": list(paipan["errors"].keys()),
            "resumed": resumed_paipan,
        })
        yield {"event": "phase", "data": {
            "phase": "paipan",
            "success": success,
            "errors": list(paipan["errors"].keys()),
            # 跨页结论不经过排盘（各页已完成测算），直接计入融合清单
            "successLabels": [MODULE_LABELS.get(m, m) for m in success] + cross_labels,
            # 续传标记：前端据此提示「复用上次排盘结果」，避免用户以为卡住
            "resumed": resumed_paipan,
        }}

        if not success:
            report = _build_degraded_report("所有术数排盘失败", modules)
            yield {"event": "meta", "data": {
                "report": report, "disclaimer": REPORT_DISCLAIMER,
                "degraded": True, "degradedReason": "排盘失败",
            }}
            yield {"event": "done", "data": {}}
            return

        results = paipan["results"]
        errors = paipan["errors"]

        # 3~5. synthesizer → reviewer → (可选重综合) → writer
        synthesis: Optional[dict] = None
        review: Optional[dict] = None
        revision = 0

        while True:
            # synthesizer（命中阶段缓存则跳过 LLM，#14 续传）
            cached_synth = _stage_cache_get("synthesizer", question, birth, modules, variant)
            resumed_synth = isinstance(cached_synth, dict) and bool(cached_synth)
            mark_phase("synthesizer", meta={"resumed": resumed_synth, "revision": revision})
            yield {"event": "phase", "data": {"phase": "synthesizer", "resumed": resumed_synth}}
            synth_blocks = "\n\n".join(
                f"### {MODULE_LABELS.get(m, m)}（{m}）\n{json.dumps(results[m], ensure_ascii=False)}"
                for m in success
            )
            # 跨页测算结论（塔罗/星座/数字命理）：非排盘产物，但与排盘同权参与交叉验证
            cross_block = ""
            if cross:
                cross_blocks = "\n\n".join(
                    f"### {c['label']}（跨页测算 · {c['type']}）\n{c['summary']}"
                    for c in cross
                )
                cross_block = f"\n\n【跨页测算结论】\n{cross_blocks}"
            err_block = (
                f"\n\n【排盘失败的术数】" + "；".join(f"{k}: {v}" for k, v in errors.items())
                if errors else ""
            )
            synth_user = (
                f"【用户问题】\n{question or '整体运势综合分析'}\n\n"
                f"【各术数排盘结果】\n{synth_blocks}{cross_block}{err_block}\n\n"
                "请基于以上排盘结果与跨页测算结论，输出综合分析 JSON。"
            )
            # 命中阶段缓存直接复用：跳过本次最贵的一次 LLM 调用（#14）
            synth_text = ""
            if resumed_synth:
                synthesis = cached_synth
            else:
                if config.llm_available:
                    async for ch in llm_provider.stream_messages(
                        [{"role": "system", "content": _REPORT_SYNTHESIZER_PROMPT},
                         {"role": "user", "content": synth_user}],
                        # 综合分析须稳定：相同排盘输入应给出一致的 consensus score，避免用户每次刷新分数跳动
                        temperature=0.1, max_tokens=2048,
                    ):
                        synth_text += ch
                synthesis = _parse_json(synth_text) or {
                    "overallSummary": "", "consensus": [], "keyFindings": [], "divergences": [],
                }
                if synthesis.get("consensus") or synthesis.get("keyFindings"):
                    _stage_cache_set("synthesizer", question, birth, modules, variant, synthesis)
            # 思考过程只推人类可读摘要，不再把模型原生 JSON 当 delta 推流（避免界面出现大段 JSON）
            if config.llm_available:
                yield {"event": "delta", "data": {"chunk": _sanitize(_friendly_synth(synthesis, success, cross_labels)) + "\n"}}

            # reviewer
            mark_phase("reviewer", meta={"revision": revision})
            yield {"event": "phase", "data": {"phase": "reviewer"}}
            review_user = (
                f"【综合分析结果】\n{json.dumps(synthesis, ensure_ascii=False)}\n\n请审稿，输出 JSON。"
            )
            review_text = ""
            if config.llm_available:
                async for ch in llm_provider.stream_messages(
                    [{"role": "system", "content": _REPORT_REVIEWER_PROMPT},
                     {"role": "user", "content": review_user}],
                    temperature=0.3, max_tokens=800,
                ):
                    review_text += ch
            review = _parse_json(review_text) or {
                "hasConflict": False, "conflicts": [], "needResynth": False,
                "notes": "解析失败，默认通过",
            }
            if config.llm_available:
                yield {"event": "delta", "data": {"chunk": _sanitize(_friendly_review(review)) + "\n"}}

            revision += 1
            need_resynth = bool(review.get("needResynth")) and revision < 2
            if not need_resynth:
                break

        # writer
        mark_phase("writer", meta={"variant": variant})
        yield {"event": "phase", "data": {"phase": "writer"}}
        writer_user = (
            f"【用户问题】\n{question or '整体运势综合分析'}\n\n"
            f"【已参与融合的术数与测算】\n{'、'.join([MODULE_LABELS.get(m, m) for m in success] + cross_labels) or '无'}\n\n"
            f"【综合分析】\n{json.dumps(synthesis, ensure_ascii=False)}\n\n"
            f"【审稿结果】\n{json.dumps(review, ensure_ascii=False)}\n\n"
            "请输出最终用户可见的结构化报告 JSON。"
        )
        # 「换个说法」：variant>0 时要求换全新切入点与表述结构，避免复述上一版
        if variant and variant > 0:
            writer_user += (
                f"\n\n（这是第 {variant} 次重新生成报告：请换一个全新的切入点与表述结构，"
                f"不要复述上一次的内容，重点从不同的术数维度展开，但结论须与综合分析一致。）"
            )
        writer_text = ""
        if config.llm_available:
            # variant=0 要求输出稳定可缓存；variant>0（换个说法）保留一定多样性
            writer_temp = 0.7 if variant and variant > 0 else 0.1
            async for ch in llm_provider.stream_messages(
                [{"role": "system", "content": _REPORT_WRITER_PROMPT},
                 {"role": "user", "content": writer_user}],
                temperature=writer_temp, max_tokens=2048,
            ):
                writer_text += ch
        report = _parse_json(writer_text)
        if config.llm_available:
            yield {"event": "delta", "data": {"chunk": _sanitize(_friendly_writer()) + "\n"}}
        degraded = False
        degraded_reason: Optional[str] = None
        if report is None:
            # 此前无论何种原因都返回 degraded=False，LLM 不可用时 UI 不提示降级、
            # 还拿「请重试」误导用户（重试同样拿不到 LLM 结果）。这里如实标记。
            llm_down = not config.llm_available
            degraded = True
            degraded_reason = "AI 综合推演不可用" if llm_down else "报告格式化异常"
            report = {
                "ok": True,
                "summary": (
                    "AI 综合推演暂不可用，以下为本地规则生成的保守结论，六维评分仅供参考。"
                    if llm_down else "综合分析已生成，但报告格式化异常，可稍后重试。"
                ),
                "advice": [
                    {"title": "⚡ 立即行动", "items": ["整理当前最紧迫的一件事"]},
                    {"title": "📅 短期（1-3月）", "items": ["围绕核心目标深耕"]},
                    {"title": "🚀 中长期（2027+）", "items": ["把握运势向上的窗口期"]},
                ],
                "outlook": "近期宜稳不宜激进。",
                "consensus": synthesis.get("consensus", []),
                "cards": [],
                "timeline": _FALLBACK_TIMELINE,
            }

        # timeline 归一化：段数不足/字段缺失时整体回落兜底
        # （半截时间轴比没有更易误导，宁可给保守的通用三阶段）
        report["timeline"] = _normalize_timeline(report.get("timeline"))

        report = _sanitize_obj(report)
        yield {"event": "meta", "data": {
            "report": report, "disclaimer": REPORT_DISCLAIMER,
            "degraded": degraded, "degradedReason": degraded_reason,
        }}
        yield {"event": "done", "data": {}}

    except Exception as e:  # noqa: BLE001
        logger.error(f"report agent 异常: {e}", exc_info=True)
        yield {"event": "error", "data": {"code": "REPORT_ERROR", "message": str(e) or "报告生成失败"}}
        yield {"event": "done", "data": {}}
