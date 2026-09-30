"""玄镜 OracleMind · 综合行动建议编排

对应 TS 版 src/services/summary.ts —— 融合多术数排盘，缓存 + 预算熔断 +
结构化 JSON 生成 + 本地降级。返回 { data, disclaimer, meta }。

【契约守卫】LLM 输出不保证守规矩（可能少字段、越界、类型错乱），
因此本模块在落库/返回前统一走 _normalize()，强制对齐前端渲染契约。
最危险的退化是 score 为 NaN —— 前端 width:NaN% 会被浏览器判为无效值，
进度条回落 auto 撑满 100%，用户看到一条"满格运势条"实为数据缺失。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

from src.config import config
from src.prompts.shared import DISCLAIMER
from src.prompts.summary import summary_prompt
from src.services.budget import (
    budget_broken, record_cost, estimate_cost,
    cache_get, cache_set, make_cache_key,
)
from src.services.structured import generate_structured
from src.harness.quality.hook import maybe_quality_check

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 归一化契约常量（与 prompts/summary.py 的 SYSTEM 约定严格对齐，二者必须同步修改）
# ---------------------------------------------------------------------------
CONSENSUS_LABELS: tuple[str, ...] = ("事业运", "财运", "感情运", "健康", "学业/成长", "人际/贵人")
CARD_META: tuple[tuple[str, str], ...] = (
    ("📈", "近期趋势"),
    ("🎯", "关键决策期"),
    ("⚠️", "风险提示"),
    ("💎", "天赋优势"),
)
ADVICE_TITLES: tuple[str, ...] = ("⚡ 立即行动", "📅 短期（1-3月）", "🚀 中长期（2027+）")

SCORE_MIN, SCORE_MAX = 60, 95          # 运势强度：prompt 约定 60~95
AGREEMENT_MIN, AGREEMENT_MAX = 0, 100  # 共识度：0 表示"未计算"（本地降级态）


def _clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    """把任意输入钳制成 [lo, hi] 区间内的整数。

    LLM 可能返回 "85" / 85.6 / None / NaN / inf / "高分"，任一非法值都必须安全回落，
    否则前端 `width: NaN%` 会被浏览器忽略、进度条撑满 100%（静默的错误满分）。

    Args:
        value: 任意来源的原始值
        lo: 下界（含）
        hi: 上界（含）
        default: 无法解析时的回落值

    Returns:
        位于 [lo, hi] 内的整数
    """
    try:
        n = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lo, min(hi, n))


def _index_by(items: Any, key: str) -> dict[str, dict]:
    """按指定字段把 list 索引成 dict，便于按契约标签取值。

    Args:
        items: 任意来源的候选列表
        key: 用作索引键的字段名（如 "label" / "name"）

    Returns:
        {key值: 原始dict}；输入非列表或元素不合法时返回空 dict
    """
    out: dict[str, dict] = {}
    if not isinstance(items, list):
        return out
    for it in items:
        if isinstance(it, dict) and isinstance(it.get(key), str):
            out[it[key]] = it
    return out


def _fallback(result: dict) -> dict:
    """本地降级：按聚合结果生成通用结构化建议（AI 不可用时的兜底）。"""
    modules = result.get("modules", []) if isinstance(result, dict) else []
    names = [m.get("name", "") for m in modules if isinstance(m, dict)]
    count = len(names)
    return {
        "ok": True,
        "summary": f"已融合 {count} 种术数（{'、'.join(names) or '未知'}）的排盘结果进行综合分析。AI 解读暂不可用，以下为本地规则生成的通用建议，请结合自身情况理性参考。",
        "advice": [
            {"title": "⚡ 立即行动", "items": ["整理当前最紧迫的一件事，今天先迈出一小步", "记录今日心境与关键决策，便于后续复盘", "保持规律作息，先安顿身心再谈规划"]},
            {"title": "📅 短期（1-3月）", "items": ["围绕核心目标深耕一项可落地的技能", "主动修复一段重要关系或解除一个心结", "建立简单的情绪 / 目标记录习惯"]},
            {"title": "🚀 中长期（2027+）", "items": ["把握自身运势向上的窗口期，考虑进阶 / 转轨", "做稳健的中长期财务与职业规划", "定期关注健康，防患于未然"]},
        ],
        "outlook": "近期宜稳不宜激进，先夯实基础；重大变动建议择机而行。",
        # agreement=0 是刻意设计的哨兵值：本地规则并未真正计算多术数一致性，
        # 前端遇到 0 应隐藏共识度标签，避免把兜底数据包装成"已交叉验证"。
        "consensus": [
            {"label": "事业运", "score": 80, "agreement": 0},
            {"label": "财运", "score": 76, "agreement": 0},
            {"label": "感情运", "score": 72, "agreement": 0},
            {"label": "健康", "score": 85, "agreement": 0},
            {"label": "学业/成长", "score": 78, "agreement": 0},
            {"label": "人际/贵人", "score": 82, "agreement": 0},
        ],
        "cards": [
            {"icon": "📈", "name": "近期趋势", "score": "→ 平稳", "desc": "当前处于蓄力期，宜深耕积累，不宜贸然变动。"},
            {"icon": "🎯", "name": "关键决策期", "score": "待定", "desc": "需结合更多排盘信息确定最佳行动窗口期。"},
            {"icon": "⚠️", "name": "风险提示", "score": "中", "desc": "注意情绪管理，避免冲动决策。"},
            {"icon": "💎", "name": "天赋优势", "score": "待发掘", "desc": "结合命盘格局，发掘自身独特天赋。"},
        ],
        "timeline": [
            {"period": "当下", "overview": "夯实基础、安顿身心，先从最紧迫的一件小事做起。"},
            {"period": "短期", "overview": "围绕核心目标深耕一项技能，主动修复重要关系。"},
            {"period": "中长期", "overview": "把握运势向上窗口期，做稳健的财务与职业规划。"},
        ],
        # 降级态不编造具体共振/矛盾，仅作温和引导，避免把兜底包装成"已交叉验证"
        "keyFindings": [
            f"已融合 {count} 种术数排盘，初步呈现多盘共振的趋势方向。",
            "如需针对具体所问之事深入，可在下方追问区向小玄继续探询。",
        ],
        "divergences": [],
    }


def _normalize(data: dict) -> tuple[dict, list[str]]:
    """把 LLM 的原始结构强行对齐前端渲染契约。

    修复目标：LLM 少字段 / 越界 / 类型错乱时，前端仍拿到可渲染且语义正确的数据，
    而不是静默渲染成"满格运势条"或空白区块。

    Args:
        data: LLM 解析后的原始 dict（或 {"ok": false, "reason": ...}）

    Returns:
        (归一化后的 dict, 被修补字段清单)。清单写入 meta.patched 用于观测
        「该 prompt 版本有多经常不守规矩」，为后续 prompt 调优提供依据。
    """
    if not isinstance(data, dict) or data.get("ok") is False:
        # ok:false 是 prompt 允许的合法失败分支，原样透传给前端显式处理
        return data, []

    patched: list[str] = []
    fb = _fallback({})

    # --- consensus：固定 6 项、顺序固定、score/agreement 双重钳制 ---
    got = _index_by(data.get("consensus"), "label")
    consensus: list[dict[str, Any]] = []
    for i, label in enumerate(CONSENSUS_LABELS):
        raw = got.get(label, {})
        if label not in got:
            patched.append(f"consensus.{label}")
        consensus.append({
            "label": label,
            "score": _clamp_int(raw.get("score"), SCORE_MIN, SCORE_MAX, fb["consensus"][i]["score"]),
            "agreement": _clamp_int(raw.get("agreement"), AGREEMENT_MIN, AGREEMENT_MAX, 0),
            "reason": str(raw.get("reason") or "")[:60],
        })
    data["consensus"] = consensus

    # --- cards：固定 4 张，icon/name 由契约决定 ---
    # 不接受 LLM 改写 name —— 前端配色靠 name 精确匹配，被改写会导致配色静默降级
    gotc = _index_by(data.get("cards"), "name")
    cards: list[dict[str, str]] = []
    for i, (icon, name) in enumerate(CARD_META):
        raw = gotc.get(name, {})
        if name not in gotc:
            patched.append(f"cards.{name}")
        cards.append({
            "icon": icon,
            "name": name,
            "score": str(raw.get("score") or fb["cards"][i]["score"])[:8],
            "desc": str(raw.get("desc") or fb["cards"][i]["desc"])[:80],
        })
    data["cards"] = cards

    # --- advice：固定 3 栏，标题由契约决定 ---
    adv_raw = [a for a in (data.get("advice") or []) if isinstance(a, dict)]
    if len(adv_raw) != len(ADVICE_TITLES):
        patched.append(f"advice.count={len(adv_raw)}")
    advice: list[dict[str, Any]] = []
    for i, title in enumerate(ADVICE_TITLES):
        src = adv_raw[i] if i < len(adv_raw) else {}
        items = [str(x) for x in (src.get("items") or []) if x][:4]
        if not items:
            items = list(fb["advice"][i]["items"])
            patched.append(f"advice[{i}].items")
        advice.append({"title": title, "items": items})
    data["advice"] = advice

    # --- timeline：3~4 段；段数不足时整体回落兜底（半截时间轴比没有更易误导） ---
    tl = [t for t in (data.get("timeline") or []) if isinstance(t, dict) and t.get("period")]
    if 3 <= len(tl) <= 4:
        data["timeline"] = [
            {"period": str(t.get("period"))[:16], "overview": str(t.get("overview") or "")[:80]}
            for t in tl
        ]
    else:
        patched.append(f"timeline.count={len(tl)}")
        data["timeline"] = fb["timeline"]

    # --- keyFindings：3~5 条跨术数共振发现，去空去重限长 ---
    kf = [str(x) for x in (data.get("keyFindings") or []) if x and str(x).strip()][:5]
    seen_kf: set[str] = set()
    uniq_kf: list[str] = []
    for t in kf:
        if t not in seen_kf:
            seen_kf.add(t)
            uniq_kf.append(t[:60])
    data["keyFindings"] = uniq_kf

    # --- divergences：术数分歧与调和，每条限长 ---
    dv_raw = [d for d in (data.get("divergences") or []) if isinstance(d, dict) and d.get("desc")]
    data["divergences"] = [
        {
            "desc": str(d.get("desc"))[:60],
            "modules": [str(m) for m in (d.get("modules") or []) if m][:4],
            "resolution": str(d.get("resolution") or "")[:60],
        }
        for d in dv_raw[:3]
    ]

    # --- 文本字段：限长，防止 LLM 失控输出刷爆版面 ---
    data["summary"] = str(data.get("summary") or "")[:400]
    data["outlook"] = str(data.get("outlook") or "")[:200]
    data["ok"] = True
    return data, patched


def _build(data: dict, rid: str, version: str, start: float, degraded: bool,
           reason: Optional[str], tokens: Optional[dict], provider: Optional[str],
           model: Optional[str], cost: Optional[float] = None, cache_hit: bool = False,
           patched: Optional[list[str]] = None) -> dict:
    return {
        "data": data,
        "disclaimer": DISCLAIMER,
        "meta": {
            "requestId": rid,
            "module": "summary",
            "promptVersion": version,
            "provider": provider or ("local-rules" if degraded else config.ai_provider),
            "model": model or ("local-rules" if degraded else config.ai_model_chat),
            "cacheHit": cache_hit,
            "degraded": degraded,
            "degradedReason": reason,
            "patched": patched or [],
            "tokens": tokens,
            "costYuan": cost if cost is not None else (estimate_cost(tokens) if tokens else 0.0),
            "latencyMs": int((time.time() - start) * 1000),
            "truncated": False,
        },
    }


async def interpret_summary(result: dict, request_id: Optional[str] = None, focus: Optional[str] = None) -> dict:
    """主编排入口：POST /api/v1/summary"""
    rid = request_id or uuid.uuid4().hex[:8]
    start = time.time()
    prompt = summary_prompt

    user_msg = prompt.build_user(result, focus)
    key = make_cache_key("summary", json.dumps(result, ensure_ascii=False, sort_keys=True), focus)

    # 缓存命中直返（缓存内容写入时已归一化，此处再兜一层防历史脏数据）
    cached = cache_get(key)
    if cached:
        try:
            parsed, patched = _normalize(json.loads(cached))
            return _build(parsed, rid, prompt.version, start, False, None, None, "cache", "cache",
                          cache_hit=True, patched=patched)
        except Exception:
            pass

    # 预算熔断 → 降级
    if budget_broken():
        return _build(_fallback(result), rid, prompt.version, start, True, "单日预算超限，熔断降级", None, None, None)

    # LLM 调用（失败重试一次）
    parsed, err, tokens, provider, model = await generate_structured(
        prompt.system, user_msg, temperature=prompt.temperature, max_tokens=prompt.max_tokens, retries=1
    )
    if parsed is not None:
        maybe_quality_check("horoscope_summary", json.dumps(parsed, ensure_ascii=False))
    if err:
        return _build(_fallback(result), rid, prompt.version, start, True, err, tokens, provider, model)

    if parsed is None:
        return _build(_fallback(result), rid, prompt.version, start, True, "LLM_FAIL", tokens, provider, model)

    # 成功：归一化 → 记录成本 → 写缓存
    parsed, patched = _normalize(parsed)
    if patched:
        logger.warning(f"[summary] LLM 输出不合规，已修补 {len(patched)} 处: {patched[:6]}")
    cost = estimate_cost(tokens) if tokens else 0.0
    record_cost(cost)
    cache_set(key, json.dumps(parsed, ensure_ascii=False))

    return _build(parsed, rid, prompt.version, start, False, None, tokens, provider, model, cost,
                  patched=patched)


async def interpret_summary_stream(result: dict, request_id: Optional[str] = None, focus: Optional[str] = None):
    """流式版（SSE 渐进渲染用）。

    复用 interpret_summary 完成缓存 / 预算熔断 / LLM / 降级全流程，计算完成后按
    section 分段 yield SSE 事件，让前端"共识度 → 卡片 → 建议 → 总结"依次点亮，
    而不是像旧版 POST /summary 那样要等全量 JSON 返回后才一次性渲染。

    事件顺序：open → init(ok/reason/降级信息) → consensus(含 reason) → cards → advice →
    summary(含 outlook/timeline) → keyFindings → divergences → meta(完整 meta) → done(disclaimer)。
    """
    rid = request_id or uuid.uuid4().hex[:8]
    yield {"event": "open", "data": json.dumps({"status": "computing"}, ensure_ascii=False)}

    full = await interpret_summary(result, rid, focus)
    data = full.get("data", {}) if isinstance(full, dict) else {}
    meta = full.get("meta", {}) if isinstance(full, dict) else {}
    ok = bool(data.get("ok", True))

    yield {
        "event": "init",
        "data": json.dumps({
            "ok": ok,
            "reason": data.get("reason"),
            "degraded": meta.get("degraded", False),
            "provider": meta.get("provider"),
            "model": meta.get("model"),
            "cacheHit": meta.get("cacheHit", False),
            "degradedReason": meta.get("degradedReason"),
        }, ensure_ascii=False),
    }

    if ok:
        yield {"event": "consensus", "data": json.dumps(data.get("consensus", []), ensure_ascii=False)}
        yield {"event": "cards", "data": json.dumps(data.get("cards", []), ensure_ascii=False)}
        yield {"event": "advice", "data": json.dumps(data.get("advice", []), ensure_ascii=False)}
        yield {
            "event": "summary",
            "data": json.dumps({
                "summary": data.get("summary", ""),
                "outlook": data.get("outlook", ""),
                "timeline": data.get("timeline", []),
            }, ensure_ascii=False),
        }
        # 跨术数共振发现 / 术数分歧与调和（降级态也有温和引导）
        yield {"event": "keyFindings", "data": json.dumps(data.get("keyFindings", []), ensure_ascii=False)}
        yield {"event": "divergences", "data": json.dumps(data.get("divergences", []), ensure_ascii=False)}

    yield {"event": "meta", "data": json.dumps(meta, ensure_ascii=False)}
    yield {
        "event": "done",
        "data": json.dumps({"disclaimer": full.get("disclaimer", "") if isinstance(full, dict) else ""}, ensure_ascii=False),
    }
