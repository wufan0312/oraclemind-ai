"""解读路由"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from src.oracle_types import InterpretRequest, InterpretResponse
from src.services.interpret import interpret, interpret_stream
# 注：format_refs 由 retrieve_for_module 内部调用，此处不再导入（此前为死导入）
from src.services.retrieval import retrieve_for_module
from src.services.tarot_daily import interpret_daily_tarot
from src.services.summary import interpret_summary, interpret_summary_stream
from src.services.provider import llm_provider
from src.prompts.dream import dream_prompt
from src.prompts.shared import OUTPUT_FORMAT, DISCLAIMER
from src.prompts.registry import list_modules, get_prompt
from src.config import config
from src.harness import route_trace
from pydantic import BaseModel, Field
from typing import Any, Optional

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["interpret"])


@router.post("/interpret", response_model=InterpretResponse)
async def post_interpret(req: InterpretRequest):
    """单模块解读"""
    resp = await interpret(req)
    return resp


@router.post("/interpret/stream")
async def post_interpret_stream(req: InterpretRequest):
    """流式解读（SSE）"""
    async def event_generator():
        async for evt in interpret_stream(req):
            yield {
                "event": evt["event"],
                "data": json.dumps(evt.get("data", {}), ensure_ascii=False),
            }

    return EventSourceResponse(route_trace(event_generator(), meta={"module": req.module, "kind": "interpret"}))


@router.post("/retrieve")
async def post_retrieve(req: InterpretRequest):
    """纯检索（RAG），返回检索到的参考资料

    前端期望格式: { enabled, count, sources, text }
    """
    from src.services.retrieval import retrieve as do_retrieve, _extract_features

    if not config.retrieval_enabled:
        return {"enabled": False, "count": 0, "sources": [], "text": ""}

    result = req.result if isinstance(req.result, dict) else {}
    try:
        query = _extract_features(req.module, result)
        if not query:
            return {"enabled": True, "count": 0, "sources": [], "text": ""}

        docs = await asyncio.to_thread(do_retrieve, query, where={"module": req.module})
        if not docs:
            return {"enabled": True, "count": 0, "sources": [], "text": ""}

        # 提取来源
        sources = list(dict.fromkeys(d.get("source", "") for d in docs if d.get("source")))

        # 格式化文本 —— 结构化叙事
        text_parts = []
        for i, doc in enumerate(docs, 1):
            source = doc.get("source", "未知")
            chapter = doc.get("chapter", "")
            # source 可能已自带《》，避免包成《《葬书》》
            ref = source if source.startswith("《") else f"《{source}》"
            ref += f"· {chapter}" if chapter else ""
            snippet = doc.get("text", "")[:300]
            text_parts.append(f"[{i}] {ref}\n{snippet}")

        text = "\n\n".join(text_parts)

        return {
            "enabled": True,
            "count": len(docs),
            "sources": sources,
            "text": text,
        }
    except Exception as e:
        logger.warning(f"retrieve 检索失败: {e}")
        return {"enabled": True, "count": 0, "sources": [], "text": ""}


@router.get("/modules")
async def get_modules():
    """支持的解读模块列表"""
    return {"modules": list_modules()}


# ============================ 塔罗 · 每日 ============================
class TarotDailyCard(BaseModel):
    name: str
    isRev: bool = False
    # 牌位语义（今日能量 / 今日挑战 / 今日行动），由前端生成牌面时附带
    position: str | None = None


class TarotDailyRequest(BaseModel):
    date: str  # YYYY-MM-DD
    theme: dict  # { name: string, isRev?: boolean }
    cards: list[TarotDailyCard]
    requestId: str | None = None


@router.post("/tarot/daily")
async def post_tarot_daily(req: TarotDailyRequest):
    """今日塔罗能量 + 每日三牌（结构化解读数据）"""
    result = {
        "date": req.date,
        "theme": {"name": req.theme.get("name", ""), "isRev": bool(req.theme.get("isRev", False))},
        "cards": [{"name": c.name, "isRev": c.isRev, "position": c.position or ""} for c in req.cards],
    }
    return await interpret_daily_tarot(result, req.requestId)


# ============================ 综合行动建议 ============================
class SummaryModule(BaseModel):
    module: str
    name: str
    result: Any


class SummaryRequest(BaseModel):
    modules: list[SummaryModule]
    question: str | None = None
    requestId: str | None = None
    focus: str | None = None


@router.post("/summary")
async def post_summary(req: SummaryRequest):
    """综合行动建议（融合多术数排盘，结构化输出）"""
    result = {
        "question": req.question or "",
        "modules": [m.model_dump() for m in req.modules],
    }
    return await interpret_summary(result, req.requestId, req.focus)


@router.post("/summary/stream")
async def post_summary_stream(req: SummaryRequest):
    """综合行动建议（SSE 流式）：计算完成后按 section 分段推送，前端渐进渲染"""
    result = {
        "question": req.question or "",
        "modules": [m.model_dump() for m in req.modules],
    }

    async def event_generator():
        # interpret_summary_stream 已把 data 序列化为 JSON 字符串（SSE 契约要求），
        # 这里直接透传，切勿再 json.dumps 一次（否则前端 JSON.parse 会得到字符串而非对象）。
        async for evt in interpret_summary_stream(result, req.requestId, req.focus):
            yield evt

    return EventSourceResponse(route_trace(event_generator(), meta={"module": "summary", "kind": "summary"}))


# ============================ 多轮对话 SSE 流式 ============================
class ChatMessageModel(BaseModel):
    role: str  # 'user' | 'assistant'
    content: str = Field(max_length=5000)


class ChatStreamRequest(BaseModel):
    module: str = "dream"
    dream: str | None = Field(default=None, max_length=5000)
    context: str | None = Field(default=None, max_length=20000)
    history: list[ChatMessageModel] = Field(default_factory=list, max_items=20)
    question: str | None = Field(default=None, max_length=5000)
    perspective: str | None = Field(default=None, max_length=50)
    # 模块专属上下文：module='tarot' 时传牌阵与牌面（{spreadName, question, cards:[{pos,name,isRev,upright,rev}]}）
    tarotCtx: dict | None = None


# 通用「轻量追问」支持的模块（命理 / 占卜各体系 + 综合运势）。
# 这些模块没有专属对话 prompt，统一以「原始解读文本」为上下文做追问。
GENERIC_FOLLOWUP_MODULES = {
    "numerology", "horoscope", "bazi", "ziwei",
    "liuyao", "meihua", "qimen", "wuxing", "summary",
    # 三式补齐：大六壬 / 太乙神数
    "liuren", "taiyi",
    "ceming",
}
MODULE_DISPLAY = {
    "numerology": "数字命理",
    "horoscope": "星座星盘",
    "bazi": "八字命理",
    "ziwei": "紫微斗数",
    "liuyao": "六爻",
    "meihua": "梅花易数",
    "qimen": "奇门遁甲",
    "liuren": "大六壬",
    "taiyi": "太乙神数",
    "wuxing": "五行能量",
    "summary": "综合运势",
    "ceming": "测字起名",
}
# 通用追问的系统人设：不预言具体事件，只作传统文化视角参考。
GENERIC_FOLLOWUP_SYSTEM = (
    "你是一位温和、专业、具有东方玄学底蕴的解读助手，擅长用通俗易懂的中文"
    "解释命盘、卦象、数字命理与星盘，语气平和而笃定。你不预言具体事件，"
    "只作传统文化与心理视角的参考。"
    "【输出约束】这是多轮对话模式，不是排盘接口。你必须始终以自然语言（可适度使用"
    "Markdown 的加粗/列表/分段）直接回答，严禁输出任何 JSON、XML、YAML 或代码块形式的"
    "原始结构数据，也不要使用「{...}」「```json」这类包裹。否则回答会被解析管线整段丢弃，"
    "用户将看不到任何内容。"
)


@router.post("/chat/stream")
async def post_chat_stream(req: ChatStreamRequest):
    """多轮对话（SSE 流式输出）

    入参：
      - module='dream'：{ dream, context?, history, question, perspective? }
      - module='tarot'：{ tarotCtx, history, question }
      - module in GENERIC_FOLLOWUP_MODULES：{ context(原始解读文本), history, question }
    事件流：open → delta* → meta → done（异常时 error 替代 meta+done）
    """
    if req.module not in ("dream", "tarot", "healing", "classics", "meditation", *GENERIC_FOLLOWUP_MODULES):
        raise HTTPException(
            status_code=400,
            detail={"error": "INVALID_MODULE", "message": "chat/stream 仅支持 dream / tarot / 命理追问模块"},
        )
    if not req.question or not req.question.strip():
        raise HTTPException(
            status_code=400,
            detail={"error": "INVALID_QUESTION", "message": "question 必须为非空字符串"},
        )
    if req.module == "dream" and not (req.dream or "").strip():
        raise HTTPException(
            status_code=400,
            detail={"error": "INVALID_DREAM", "message": "dream 必须为非空字符串"},
        )
    if req.module == "tarot" and not req.tarotCtx:
        raise HTTPException(
            status_code=400,
            detail={"error": "INVALID_TAROT_CTX", "message": "module=tarot 时 tarotCtx 必填"},
        )
    if req.module in GENERIC_FOLLOWUP_MODULES and not (req.context or "").strip():
        raise HTTPException(
            status_code=400,
            detail={"error": "INVALID_CONTEXT", "message": "通用追问需传 context（原始解读文本）"},
        )

    from src.prompts.dream import PERSPECTIVE_MAP, CHAT_NL_OUTPUT

    if req.module == "tarot":
        # 塔罗追问：以本次占卜的牌阵与牌面为上下文，回答用户的追问
        from src.prompts.tarot import tarot_prompt, OUTPUT_FORMAT_TAROT

        # 整段替换 JSON 输出约束（与下方的 dream 分支保持一致）。
        # 此前只替换「【输出格式】」标题，JSON schema 整段残留，与「禁止输出任何 JSON 结构」自相矛盾。
        system = tarot_prompt.system.replace(OUTPUT_FORMAT_TAROT, CHAT_NL_OUTPUT)
        ctx = req.tarotCtx or {}
        cards = ctx.get("cards") or []
        card_lines = []
        for c in cards:
            if not isinstance(c, dict):
                continue
            is_rev = bool(c.get("isRev"))
            meaning = c.get("rev") if is_rev else c.get("upright")
            card_lines.append(
                f"- {c.get('pos', '')}：{'逆位' if is_rev else '正位'}·{c.get('name', '')}（牌义：{meaning or '未提供'}）"
            )
        system += (
            f"\n\n【多轮对话模式 · 塔罗追问】\n"
            f"本次占卜：牌阵「{ctx.get('spreadName', '')}」，"
            f"问题「{ctx.get('question') or '未指定'}」。\n"
            f"牌面如下（牌义为唯一口径，禁止改写或重新抽牌）：\n" + ("\n".join(card_lines) or "（无牌面）") + "\n"
            f"用户已看过上面的完整解读，现在针对这次占卜继续追问。请基于牌面与历史对话作答，"
            f"回答用中文，语气温和专业，200 字以内，不要重复整篇解读，只答所问。"
        )
    elif req.module == "dream":
        # 构建 system prompt：剥离 JSON 输出约束，改为自然语言对话模式
        system = dream_prompt.system.replace(OUTPUT_FORMAT, CHAT_NL_OUTPUT)
        ctx = f"\n\n【用户近期背景】{req.context.strip()}" if req.context and req.context.strip() else ""
        system += (
            f"\n\n【多轮对话模式】\n"
            f"你正在与用户进行多轮对话。用户的初始梦境描述为：「{req.dream.strip()}」{ctx}\n"
            f"之前的对话历史已在 messages 中给出。请基于梦境上下文和历史对话，自然地回答用户的最新问题。\n"
            f"回答用中文，语气温和专业。可以使用 Markdown 格式（加粗、列表、段落）。"
            f"不需要输出 JSON 结构，直接输出自然语言文本。"
        )
    elif req.module in ("healing", "classics", "meditation"):
        # 疗愈陪伴对话：小玄 persona，温柔倾听，不预言；context 为可选综合报告背景
        from src.prompts.healing import HEALING_SYSTEM
        ctx = (
            f"\n\n【用户综合命理报告（可选背景，仅用于更贴合的陪伴）】\n{req.context.strip()}"
            if req.context and req.context.strip()
            else ""
        )
        system = HEALING_SYSTEM + (
            "\n\n【疗愈对话模式】\n"
            "你正在以「小玄」的身份与用户进行温柔的疗愈对话。用户可能带着情绪、困惑或心事而来。\n"
            "请先接住对方的情绪，再轻轻引导；以阳明心学、禅意与传统人文视角给予陪伴与启发，"
            "不预言具体事件、不替用户做决定、不诊断疾病。\n"
            "回答用中文，自然亲切，可使用 Markdown（加粗、列表、段落），200 字以内，先共情再轻引。"
            + ctx
        )
    else:
        # 通用命理追问：以「原始解读文本」为唯一上下文，禁止重新排盘或编造新结论。
        # 轻量追问的价值在于「基于已有解读深挖」，而不是再跑一遍排盘。
        display = MODULE_DISPLAY.get(req.module, req.module)
        system = GENERIC_FOLLOWUP_SYSTEM + (
            f"\n\n【多轮对话模式 · {display}追问】\n"
            f"用户此前已获得一份关于「{display}」的 AI 解读（见下方【原始解读】）。\n"
            f"用户现在针对这份解读继续追问。请严格基于原始解读与历史对话作答，"
            f"不要重新排盘、不要编造新的命理结论、不要虚构具体事件。\n"
            f"回答用中文，语气温和专业，200 字以内，只答所问，不要复述整篇解读。\n\n"
            f"【原始解读】\n{req.context.strip()}"
        )
    if req.perspective:
        p = PERSPECTIVE_MAP.get(req.perspective)
        if p:
            system += f"\n\n【本轮视角要求】\n本论解读视角：{p['title']}\n{p['userRule']}"

    # 拼装完整 messages（system + 历史 + 当前问题）
    messages: list[dict] = [{"role": "system", "content": system}]
    for m in req.history:
        if m.content:
            messages.append(
                {"role": "assistant" if m.role == "assistant" else "user", "content": m.content}
            )
    messages.append({"role": "user", "content": req.question})

    async def event_generator():
        yield {"event": "open", "data": json.dumps({}, ensure_ascii=False)}
        try:
            full_text = ""
            if config.llm_available:
                try:
                    async for chunk in llm_provider.stream_messages(
                        messages, temperature=0.9, max_tokens=1024
                    ):
                        if chunk:
                            full_text += chunk
                            yield {
                                "event": "delta",
                                "data": json.dumps({"chunk": chunk}, ensure_ascii=False),
                            }
                except Exception as e:  # 主供应商异常 → 降级文案
                    logger.warning(f"chat/stream LLM 失败: {e}")
                    full_text = ""

            if not full_text.strip():
                # 兜底文案必须真正下发，否则前端只会收到空文本 → 渲染出空气泡
                fallback = "抱歉，AI 服务暂时不可用，请稍后重试。"
                full_text = fallback
                yield {
                    "event": "delta",
                    "data": json.dumps({"chunk": fallback}, ensure_ascii=False),
                }

            yield {
                "event": "meta",
                "data": json.dumps({"disclaimer": DISCLAIMER}, ensure_ascii=False),
            }
            yield {"event": "done", "data": json.dumps({}, ensure_ascii=False)}
        except Exception as e:
            logger.error(f"chat/stream 异常: {e}")
            yield {
                "event": "error",
                "data": json.dumps(
                    {"code": "STREAM_ERROR", "message": "对话服务暂时不可用，请稍后重试"},
                    ensure_ascii=False,
                ),
            }
            yield {"event": "done", "data": json.dumps({}, ensure_ascii=False)}

    return EventSourceResponse(route_trace(event_generator(), meta={"module": "chat", "chat_module": req.module, "kind": "chat"}))
