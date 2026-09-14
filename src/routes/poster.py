"""海报生成路由"""

from __future__ import annotations

import hashlib
import json
import logging

from fastapi import APIRouter
from pydantic import BaseModel

from src.services.budget import cache_get, cache_set

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/poster", tags=["poster"])

# 海报生成很贵（含 CogView 画图），相同入参直接复用，避免重复烧 token
_POSTER_CACHE_TTL = 7 * 24 * 3600


def _poster_key(prefix: str, **kw) -> str:
    raw = json.dumps(kw, ensure_ascii=False, sort_keys=True)
    return f"{prefix}:{hashlib.md5(raw.encode('utf-8')).hexdigest()[:16]}"


class PosterRequest(BaseModel):
    keyword: str = ""        # 分享主题关键词（前端 requestPoster 传入）
    interpretation: str = "" # 解读文本
    module: str | None = None
    result: dict | None = None
    style: str = "classic"   # classic / modern / ink


@router.post("")
async def post_poster(req: PosterRequest):
    """生成分享海报（兼容前端 POST /api/v1/poster）

    返回 PosterResult：{ shareText, imagePrompt, imageUrl, imageError, fallback }
    """
    from src.services.provider import llm_provider
    from src.config import config

    if not config.llm_available:
        return {
            "shareText": req.interpretation[:200] if req.interpretation else "命理解读生成中…",
            "imagePrompt": "",
            "imageUrl": None,
            "imageError": None,
            "fallback": True,
        }

    key = _poster_key("poster", keyword=req.keyword, interpretation=req.interpretation,
                      module=req.module, result=req.result, style=req.style)
    cached = cache_get(key)
    if cached:
        try:
            logger.info("[poster] 缓存命中: %s", key)
            return json.loads(cached)
        except Exception:
            pass

    kw = req.keyword or req.module or "命理"
    interp = req.interpretation or (json.dumps(req.result, ensure_ascii=False) if req.result else "")

    sys_text = "你是社交媒体文案专家，擅长将命理排盘结果转化为适合朋友圈/小红书分享的短文案。"
    user_text = f"请为以下命理解读生成一段分享文案（≤200字，含emoji，积极温和）：\n主题：{kw}\n解读：{interp}"

    sys_img = "你是 AI 绘画 prompt 专家，擅长将命理意象转化为画面描述。"
    user_img = f"请为以下主题生成一个 AI 绘画 prompt（英文，≤100词，唯美国风）：\n主题：{kw}\n风格：{req.style}"

    try:
        text_resp = await llm_provider.chat(sys_text, user_text, temperature=0.85, max_tokens=512)
        img_resp = await llm_provider.chat(sys_img, user_img, temperature=0.8, max_tokens=256)

        # 调用 CogView-3-Flash 生成分享配图（免费模型）
        image_url: str | None = None
        image_error: str | None = None
        try:
            image_url = await llm_provider.generate_image(img_resp.get("content", "mystical tarot card, ethereal Chinese ink painting style"))
        except Exception as img_e:
            image_error = str(img_e)
            logger.warning(f"CogView 图片生成失败: {img_e}")

        result = {
            "shareText": text_resp.get("content", ""),
            "imagePrompt": img_resp.get("content", ""),
            "imageUrl": image_url,
            "imageError": image_error,
            "fallback": False,
            "provider": text_resp.get("provider"),
            "model": text_resp.get("model"),
        }
        # 图片成功才缓存，避免把「图片生成失败」锁死（用户需可重试）
        if image_url:
            try:
                cache_set(key, json.dumps(result, ensure_ascii=False), ttl_seconds=_POSTER_CACHE_TTL)
            except Exception:
                pass
        return result
    except Exception as e:
        logger.warning(f"海报生成失败: {e}")
        return {
            "shareText": interp[:200] if interp else "命理解读生成中…",
            "imagePrompt": "",
            "imageUrl": None,
            "imageError": "生成失败，请稍后重试",
            "fallback": True,
        }


@router.post("/generate")
async def post_generate(req: PosterRequest):
    """生成分享海报文案"""
    from src.services.provider import llm_provider
    from src.config import config

    if not config.llm_available:
        return {"degraded": True, "reason": "AI_API_KEY 未配置"}

    key = _poster_key("poster:gen", module=req.module, result=req.result)
    cached = cache_get(key)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass

    system = "你是社交媒体文案专家，擅长将命理排盘结果转化为适合朋友圈/小红书分享的短文案。"
    user = f"请为以下排盘结果生成一段分享文案（≤200字，含emoji）：\n模块：{req.module}\n结果：{req.result}"

    try:
        result = await llm_provider.chat(system, user, temperature=0.8, max_tokens=512)
        out = {
            "text": result["content"],
            "provider": result["provider"],
            "model": result["model"],
        }
        try:
            cache_set(key, json.dumps(out, ensure_ascii=False), ttl_seconds=_POSTER_CACHE_TTL)
        except Exception:
            pass
        return out
    except Exception as e:
        logger.warning(f"海报文案生成失败: {e}")
        return {"degraded": True, "reason": "生成失败，请稍后重试"}


@router.post("/image")
async def post_image(req: PosterRequest):
    """生成图片 prompt（供前端调用图片生成 API）"""
    from src.services.provider import llm_provider
    from src.config import config

    if not config.llm_available:
        return {"degraded": True, "reason": "AI_API_KEY 未配置"}

    key = _poster_key("poster:img", module=req.module, style=req.style)
    cached = cache_get(key)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass

    system = "你是 AI 绘画 prompt 专家，擅长将命理意象转化为画面描述。"
    user = f"请为以下排盘结果生成一个 AI 绘画 prompt（英文，≤100词）：\n模块：{req.module}\n风格：{req.style}"

    try:
        result = await llm_provider.chat(system, user, temperature=0.8, max_tokens=256)
        out = {
            "prompt": result["content"],
            "provider": result["provider"],
        }
        try:
            cache_set(key, json.dumps(out, ensure_ascii=False), ttl_seconds=_POSTER_CACHE_TTL)
        except Exception:
            pass
        return out
    except Exception as e:
        logger.warning(f"海报图片 prompt 生成失败: {e}")
        return {"degraded": True, "reason": "生成失败，请稍后重试"}
