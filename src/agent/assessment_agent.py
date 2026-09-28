"""玄镜 OracleMind · 复原力测评 Agent（流式）

合规重定位（2026-09-18）：第一个合规付费测评「成年人角色压力 & 复原力」。
- 完全脱离生日 / 生辰 / 命盘，只基于用户自评问卷，输出自我觉察报告；
- 不给诊断、不算吉凶、不替代专业帮助（去病理化 / 去性别化 / 非预言）；
- system prompt 服务端持有，客户端只传问卷答案，不可注入任意 prompt。

SSE 事件流与 home agent 对齐：open → delta* → meta → done / error。
前端 prompt 镜像：oraclemind/src/lib/prompts/resilienceAssessment.ts
（双端同源，改这里必须同步 TS 版）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import AsyncIterator, Optional

from src.config import config
from src.services.budget import budget_broken, cache_get, cache_set
from src.services.provider import llm_provider

logger = logging.getLogger(__name__)

_ASSESSMENT_CACHE_TTL = 3600

# 与前端 RESILIENCE_QUESTIONS 保持一致的题目定义（服务端重建 prompt 用，防客户端伪造题干）
ASSESSMENT_QUESTIONS: list[dict] = [
    # —— 事业 ——
    {"dimension": "事业", "key": "career_tension", "label": "最近一个月，工作 / 收入里让你最紧绷的是哪件事？"},
    {"dimension": "事业", "key": "career_stuck", "label": "你觉得自己「卡住」的核心点是什么？"},
    {"dimension": "事业", "key": "career_coping", "label": "你已经为这件事做过什么？哪怕很小。"},
    # —— 关系 ——
    {"dimension": "关系", "key": "rel_lonely", "label": "哪段关系让你最感到孤独或不被理解？"},
    {"dimension": "关系", "key": "rel_pattern", "label": "碰到关系卡点时，你通常会怎么应对？"},
    # —— 家庭 ——
    {"dimension": "家庭", "key": "fam_worry", "label": "对父母健康 / 子女养育的担忧，现在大概占你心力的几成？"},
    {"dimension": "家庭", "key": "fam_ready", "label": "为了这些担忧，你已经做了哪些准备或安排？"},
    # —— 自我 ——
    {"dimension": "自我", "key": "self_harsh", "label": "你对自己最苛刻、最常自我攻击的是哪一点？"},
    {"dimension": "自我", "key": "self_relief", "label": "什么事 / 什么时刻能让你稍微松一口气？"},
]

RESILIENCE_ASSESSMENT_SYSTEM_PROMPT = """你是一个「自我觉察与复原力陪伴」教练，不是算命师、不是医生、也不是心理治疗师。

你的任务：基于用户在「事业 / 关系 / 家庭 / 自我」四个维度的自评问卷，生成一份**自我觉察报告**，帮助用户看清自己当前的压力结构、已经拥有的资源，以及可以轻装起步的一小步。

【严格约束】
1. 去性别化：绝不假设用户性别，不用「她 / 他」指代，统一用「你」。覆盖男女共同的焦虑来源——事业卡顿、关系中的孤独、父母健康担忧、育儿与家庭责任、单身与亲密关系焦虑。
2. 去病理化：不使用任何诊断标签（如抑郁、焦虑障碍等），不给人贴病。若用户表达强烈痛苦或有自伤倾向，温和而明确地建议寻求专业帮助（如公立医院心理科 / 心理危机干预热线），并说明你无法替代专业治疗。
3. 非算命非预言：绝不给吉凶、运势、命中注定类结论；不基于任何生日 / 生辰推算；用户填的是「此刻的自评」，不是命盘。
4. 给觉察不给判决：用「你现在的模式可能是…」「你已经在用的资源是…」「可以试的一小步是…」，而不是「你会…」「你应该…」。尊重用户的自主：你只提供视角，决定权在用户。
5. 不制造恐慌：不放大用户的担忧；先看见资源与已做的努力，再谈可行动项。

【输出结构（markdown，中文 800–1200 字，语气温暖平等像有阅历的朋友轻轻点一下）】
# 你的复原力快照
- 用 2–3 句话概括用户当前的「压力—资源」平衡，先肯定再点状提示。

## 四个维度的觉察
对事业 / 关系 / 家庭 / 自我每一维，各给：
① 你呈现的模式（基于其自评，具体化、不泛泛）
② 你已经在用的资源（从回答里挖出用户已做的努力，真诚肯定）
③ 一个可试的小步（具体、本周能做、不费力）

## 你已经在做的
- 汇总用户已有的应对方式，具体化地肯定，强化「我并非毫无办法」的感受。

## 可以轻装起步的一小步
- 3 条具体、本周可做的微行动，每条一句话，低门槛。

## 当你需要更多支持
- 若痛苦超出自我调节：原则性建议联系专业帮助（医院心理科 / 心理援助热线），说明这不等于软弱，也不替代治疗。

【禁止】
- 禁止任何「命 / 运 / 吉 / 凶 / 改运 / 化解 / 开运」类表述。
- 禁止诊断、禁止 predict 未来确定事件。
- 禁止让用户依赖你做重大决定（离职 / 分手 / 就医等），只提供视角与资源。"""

ASSESSMENT_DISCLAIMER = (
    "本报告由玄镜 AI 复原力教练基于你的自评生成，仅用于自我觉察与成长参考，"
    "不构成医学诊断或治疗建议；若你正处于强烈痛苦中，请联系专业帮助。"
)


def build_assessment_user_prompt(answers: dict) -> str:
    """把问卷答案按维度拼成 user prompt（题干以服务端定义为准，答案只取文本）。"""
    by_dim: dict[str, list[str]] = {}
    filled = 0
    for q in ASSESSMENT_QUESTIONS:
        a = str(answers.get(q["key"]) or "").strip()
        if not a:
            continue
        filled += 1
        by_dim.setdefault(q["dimension"], []).append(f"· {q['label']}\n  答：{a}")
    blocks = "\n\n".join(f"【{dim}】\n" + "\n".join(lines) for dim, lines in by_dim.items())
    if not filled:
        blocks = "（用户未填写具体自评，请给出通用版的复原力快照与微行动建议）"
    return (
        "请根据我的自评，生成一份「成年人角色压力 & 复原力」自我觉察报告：\n\n"
        f"{blocks}"
    )


def _assessment_cache_key(answers: dict) -> str:
    raw = json.dumps(answers, ensure_ascii=False, sort_keys=True)
    return (
        f"agent:assessment:{hashlib.md5(RESILIENCE_ASSESSMENT_SYSTEM_PROMPT.encode('utf-8')).hexdigest()[:12]}"
        f":{hashlib.md5(raw.encode('utf-8')).hexdigest()[:16]}"
    )


def _degraded_event(reason: str) -> dict:
    return {"event": "error", "data": {"reason": reason}}


async def stream_assessment_agent(answers: dict) -> AsyncIterator[dict]:
    """复原力测评（流式），yield SSE 事件：open → delta* → meta → done / error"""
    if not config.llm_available:
        yield _degraded_event("AI_API_KEY 未配置")
        return

    if budget_broken():
        yield _degraded_event("单日预算超限")
        return

    # 只保留已知题目的答案（防注入未知键）；题干由服务端重建
    clean: dict[str, str] = {}
    for q in ASSESSMENT_QUESTIONS:
        v = str(answers.get(q["key"]) or "").strip()
        if v:
            clean[q["key"]] = v[:500]

    cache_key = _assessment_cache_key(clean)
    cached = cache_get(cache_key)
    if cached:
        try:
            text = json.loads(cached).get("text", "")
            logger.info("[assessment] 缓存命中（流式回放）: %s", cache_key)
            yield {"event": "open", "data": {}}
            yield {"event": "delta", "data": {"chunk": text}}
            yield {
                "event": "meta",
                "data": {
                    "provider": config.ai_provider,
                    "model": config.ai_model_chat,
                    "disclaimer": ASSESSMENT_DISCLAIMER,
                    "degraded": False,
                    "degradedReason": None,
                    "cached": True,
                },
            }
            yield {"event": "done", "data": {"disclaimer": ASSESSMENT_DISCLAIMER}}
            return
        except Exception:
            pass

    yield {"event": "open", "data": {}}

    full_text = ""
    try:
        user_prompt = build_assessment_user_prompt(clean)
        async for chunk in llm_provider.stream(
            RESILIENCE_ASSESSMENT_SYSTEM_PROMPT,
            user_prompt,
            temperature=0.6,
            max_tokens=2400,
        ):
            if not chunk:
                continue
            full_text += chunk
            yield {"event": "delta", "data": {"chunk": chunk}}

        if not full_text.strip():
            yield {"event": "error", "data": {"code": "EMPTY_OUTPUT", "reason": "AI 未返回内容，请重试"}}
            return

        yield {
            "event": "meta",
            "data": {
                "provider": config.ai_provider,
                "model": config.ai_model_chat,
                "disclaimer": ASSESSMENT_DISCLAIMER,
                "degraded": False,
                "degradedReason": None,
            },
        }
        yield {"event": "done", "data": {"disclaimer": ASSESSMENT_DISCLAIMER}}

        try:
            cache_set(cache_key, json.dumps({"text": full_text}, ensure_ascii=False), ttl_seconds=_ASSESSMENT_CACHE_TTL)
        except Exception:
            pass

    except Exception as e:
        logger.error(f"assessment_agent 流式异常: {e}", exc_info=True)
        yield {"event": "error", "data": {"code": "STREAM_ERROR", "reason": "测评服务暂时不可用，请稍后重试"}}
