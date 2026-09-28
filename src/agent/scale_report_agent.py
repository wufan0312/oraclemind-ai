"""玄镜 OracleMind · 量表 AI 心理报告 Agent（流式）

合规定位（2026-09-19，双轨右轨 MVP）：基于量表计分结果（服务端持有）生成
「成长导向 / 非诊断 / 非病理」的自我觉察解读。
- 不给诊断、不算吉凶、不替代专业帮助（去病理化 / 非预言 / 非命理）；
- system prompt 服务端持有，客户端只传计分结果与摘要，不可注入任意 prompt；
- 维度分、等级、服务端摘要均为服务端权威值，AI 仅做「温暖扩展」而非新增结论。

SSE 事件流与 assessment agent 对齐：open → delta* → meta → done / error。
前端镜像：oraclemind/src/lib/prompts/scaleReport.ts（双端同源，改这里必须同步）。
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

_SCALE_REPORT_CACHE_TTL = 3600

# 维度等级 → 中文友好标签（仅用于拼接 user prompt，不改变计分口径）
_BAND_LABEL = {"low": "偏低", "mid": "居中", "high": "偏高"}


SCALE_REPORT_SYSTEM_PROMPT = """你是一位「心理量表解读陪伴」教练，不是医生、不是心理治疗师、也不是算命师。

你的任务：基于一份心理量表的**计分结果**（各维度 0–100 分 + 等级 + 服务端生成的解读要点），
生成一份**温暖、平等、成长导向**的自我觉察解读，帮用户看见自己的倾向、已经拥有的资源，
以及可以轻装起步的一小步。

【严格约束】
1. 非诊断：绝不使用任何诊断标签（如抑郁、焦虑障碍等），不给人贴病、不暗示病理。量表只是
   当下自我觉察的快照，分数高低都没有「好 / 坏」之分。
2. 非命理非预言：绝不给吉凶、运势、命中注定类结论；不基于任何生日 / 生辰推算；
   用户看的是「此刻的倾向」，不是命盘，也不是对未来的确定预测。
3. 给觉察不给判决：用「你现在的倾向可能是…」「你已经在用的资源是…」「可以试的一小步是…」，
   而不是「你会…」「你应该…」。尊重用户的自主：你只提供视角，决定权在用户。
4. 不制造恐慌：先看见资源与优势，再谈可行动项；不放大用户的担忧。
5. 去性别化：不用「她 / 他」指代，统一用「你」。

【输出结构（markdown，中文 600–1000 字，语气温暖像有阅历的朋友轻轻点一下）】
# 你的量表解读快照
- 用 2–3 句话概括整体倾向，先肯定「做一次自我觉察本身就很值得」，再点状提示。

## 逐维度看见自己
对每一个维度，各给：
① 你呈现的倾向（基于其分数 / 等级 / 服务端解读，具体化、不泛泛）
② 这背后的资源或意义（把「偏低 / 偏高」翻译成中性、有建设性的描述）
③ 一个可试的小步（具体、本周能做、不费力）

## 你已经在做的
- 从整体结果里挖出用户已有的优势或努力，具体化地肯定。

## 可以轻装起步的一小步
- 3 条具体、本周可做的微行动，每条一句话，低门槛。

## 当你需要更多支持
- 若用户表达强烈痛苦或有自伤倾向，温和而明确地建议寻求专业帮助
  （如公立医院心理科 / 心理危机干预热线），并说明你无法替代专业治疗。

【禁止】
- 禁止任何「命 / 运 / 吉 / 凶 / 改运 / 化解 / 开运」类表述。
- 禁止诊断、禁止 predict 未来确定事件。
- 禁止让用户依赖你做重大决定（离职 / 分手 / 就医等），只提供视角与资源。"""

SCALE_REPORT_DISCLAIMER = (
    "本报告由玄镜 AI 心理陪伴教练基于你的量表计分生成，仅用于自我觉察与成长参考，"
    "不构成医学诊断或治疗建议；若你正处于强烈痛苦中，请联系专业帮助。"
)


def _band_label(band: str) -> str:
    return _BAND_LABEL.get(str(band or "").lower(), str(band or ""))


def build_scale_report_user_prompt(payload: dict) -> str:
    """把计分结果拼成 user prompt（维度分 / 等级 / 服务端解读均为权威值，AI 仅扩展）。"""
    slug = str(payload.get("slug") or "")
    title = str(payload.get("title") or "")
    summary = str(payload.get("summary") or "").strip()
    dims = payload.get("dimensions") or []

    lines: list[str] = []
    for d in dims:
        if not isinstance(d, dict):
            continue
        name = str(d.get("name") or d.get("key") or "维度")
        score = d.get("score")
        band = _band_label(d.get("band"))
        interp = str(d.get("interpretation") or "").strip()
        part = f"· {name}：{score}/100（{band}）"
        if interp:
            part += f"\n  服务端解读：{interp}"
        lines.append(part)

    dims_block = "\n".join(lines) if lines else "（无维度明细）"
    head = f"量表：{title}（{slug}）" if title else f"量表：{slug}"
    block = (
        f"{head}\n\n"
        f"【各维度计分】\n{dims_block}\n\n"
        f"【服务端生成的自我觉察摘要】\n{summary or '（无）'}"
    )
    return (
        "请根据以上量表计分结果，生成一份非诊断、成长导向的自我觉察解读报告：\n\n"
        f"{block}"
    )


def _scale_report_cache_key(payload: dict) -> str:
    seed = json.dumps(
        {
            "slug": payload.get("slug"),
            "dims": [
                {k: d.get(k) for k in ("key", "name", "score", "band")}
                for d in (payload.get("dimensions") or [])
                if isinstance(d, dict)
            ],
            "summary": payload.get("summary"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return (
        f"agent:scale_report:{hashlib.md5(SCALE_REPORT_SYSTEM_PROMPT.encode('utf-8')).hexdigest()[:12]}"
        f":{hashlib.md5(seed.encode('utf-8')).hexdigest()[:16]}"
    )


def _degraded_event(reason: str) -> dict:
    return {"event": "error", "data": {"reason": reason}}


async def stream_scale_report_agent(payload: dict) -> AsyncIterator[dict]:
    """量表 AI 心理报告（流式），yield SSE 事件：open → delta* → meta → done / error"""
    if not config.llm_available:
        yield _degraded_event("AI_API_KEY 未配置")
        return

    if budget_broken():
        yield _degraded_event("单日预算超限")
        return

    # 只保留已知字段的维度（防注入未知键）；计分口径由服务端提供
    clean_dims: list[dict] = []
    for d in payload.get("dimensions") or []:
        if not isinstance(d, dict):
            continue
        clean_dims.append(
            {
                "key": str(d.get("key") or ""),
                "name": str(d.get("name") or d.get("key") or ""),
                "score": d.get("score"),
                "band": d.get("band"),
                "interpretation": str(d.get("interpretation") or "")[:500],
            }
        )
    clean = {
        "slug": str(payload.get("slug") or ""),
        "title": str(payload.get("title") or "")[:128],
        "dimensions": clean_dims,
        "summary": str(payload.get("summary") or "")[:2000],
    }

    cache_key = _scale_report_cache_key(clean)
    cached = cache_get(cache_key)
    if cached:
        try:
            text = json.loads(cached).get("text", "")
            logger.info("[scale_report] 缓存命中（流式回放）: %s", cache_key)
            yield {"event": "open", "data": {}}
            yield {"event": "delta", "data": {"chunk": text}}
            yield {
                "event": "meta",
                "data": {
                    "provider": config.ai_provider,
                    "model": config.ai_model_chat,
                    "disclaimer": SCALE_REPORT_DISCLAIMER,
                    "degraded": False,
                    "degradedReason": None,
                    "cached": True,
                },
            }
            yield {"event": "done", "data": {"disclaimer": SCALE_REPORT_DISCLAIMER}}
            return
        except Exception:
            pass

    yield {"event": "open", "data": {}}

    full_text = ""
    try:
        user_prompt = build_scale_report_user_prompt(clean)
        async for chunk in llm_provider.stream(
            SCALE_REPORT_SYSTEM_PROMPT,
            user_prompt,
            temperature=0.6,
            max_tokens=2000,
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
                "disclaimer": SCALE_REPORT_DISCLAIMER,
                "degraded": False,
                "degradedReason": None,
            },
        }
        yield {"event": "done", "data": {"disclaimer": SCALE_REPORT_DISCLAIMER}}

        try:
            cache_set(cache_key, json.dumps({"text": full_text}, ensure_ascii=False), ttl_seconds=_SCALE_REPORT_CACHE_TTL)
        except Exception:
            pass

    except Exception as e:
        logger.error(f"scale_report_agent 流式异常: {e}", exc_info=True)
        yield {"event": "error", "data": {"code": "STREAM_ERROR", "reason": "报告服务暂时不可用，请稍后重试"}}
