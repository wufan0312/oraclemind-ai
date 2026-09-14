"""玄镜 OracleMind · LLM 供应商适配层

使用 LangChain ChatOpenAI 统一调用智谱/阿里/OpenAI 兼容接口。
支持主/备用供应商故障切换。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Optional

import httpx

from src.config import config

logger = logging.getLogger(__name__)


def _build_chat_model(model_name: Optional[str] = None, streaming: bool = True):
    """构建 LangChain ChatOpenAI 实例（OpenAI 兼容接口）"""
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=model_name or config.ai_model_chat,
        api_key=config.ai_api_key,
        base_url=config.ai_base_url,
        temperature=0.7,
        max_tokens=2048,
        streaming=streaming,
    )


def _build_fallback_model():
    """构建备用供应商模型"""
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=config.ai_model_fallback,
        api_key=config.ai_api_key_fallback,
        base_url=config.ai_base_url_fallback,
        temperature=0.7,
        max_tokens=2048,
        streaming=False,
    )


class LLMProvider:
    """LLM 供应商：主/备用切换 + 超时控制"""

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        timeout_ms: int = 30_000,
    ) -> dict:
        """同步对话，返回 {content, tokens, provider, model}"""
        if not config.llm_available:
            raise RuntimeError("AI_API_KEY 未配置")

        messages = [
            ("system", system_prompt),
            ("user", user_prompt),
        ]

        # 主供应商
        try:
            model = _build_chat_model(streaming=False)
            model = model.bind(temperature=temperature, max_tokens=max_tokens)
            resp = await asyncio.wait_for(
                model.ainvoke(messages),
                timeout=timeout_ms / 1000,
            )
            content = resp.content if hasattr(resp, "content") else str(resp)
            usage = getattr(resp, "usage_metadata", None)
            tokens = {
                "prompt": usage.get("input_tokens", 0) if usage else 0,
                "completion": usage.get("output_tokens", 0) if usage else 0,
            } if usage else None
            return {
                "content": content,
                "tokens": tokens,
                "provider": config.ai_provider,
                "model": config.ai_model_chat,
            }
        except Exception as e:
            # 用 repr + 类型名：TimeoutError 等异常的 str() 为空串，只打 {e} 会丢失根因
            logger.warning(f"主供应商失败[{type(e).__name__}]: {e!r}, 尝试备用供应商...")

        # 备用供应商
        if config.ai_api_key_fallback:
            try:
                model = _build_fallback_model()
                resp = await asyncio.wait_for(
                    model.ainvoke(messages),
                    timeout=timeout_ms / 1000,
                )
                content = resp.content if hasattr(resp, "content") else str(resp)
                return {
                    "content": content,
                    "tokens": None,
                    "provider": config.ai_provider_fallback,
                    "model": config.ai_model_fallback,
                }
            except Exception as e2:
                logger.error(f"备用供应商也失败: {e2}")

        raise RuntimeError(f"所有 LLM 供应商均不可用")

    async def stream(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> AsyncIterator[str]:
        """流式对话，yield 文本分片。

        每个 chunk 经 asyncio.wait_for 加「不活跃超时」：两次数据间隔超过
        stream_timeout_seconds 即视为上游 stall，提前结束流（不抛异常、不断连接），
        避免上游卡死导致本次请求永久挂起（对应缺陷报告 P2·流式超时）。
        """
        if not config.llm_available:
            raise RuntimeError("AI_API_KEY 未配置")

        messages = [
            ("system", system_prompt),
            ("user", user_prompt),
        ]

        model = _build_chat_model(streaming=True)
        model = model.bind(temperature=temperature, max_tokens=max_tokens)

        async for text in self._stream_iter(model.astream(messages)):
            yield text

    async def stream_messages(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> AsyncIterator[str]:
        """流式对话（传入完整 messages 列表，支持多轮历史）"""
        if not config.llm_available:
            raise RuntimeError("AI_API_KEY 未配置")

        model = _build_chat_model(streaming=True)
        model = model.bind(temperature=temperature, max_tokens=max_tokens)

        async for text in self._stream_iter(model.astream(messages)):
            yield text

    async def _stream_iter(self, aiterable) -> AsyncIterator[str]:
        """消费 LLM 流式输出并 yield 文本分片，带单 chunk 不活跃超时保护。"""
        timeout = config.stream_timeout_seconds
        try:
            it = aiterable.__aiter__()
            while True:
                try:
                    chunk = await asyncio.wait_for(it.__anext__(), timeout=timeout)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    logger.warning("流式响应超时（单 chunk 超过 %ss 无数据），提前结束", timeout)
                    return
                text = chunk.content if hasattr(chunk, "content") else str(chunk)
                # 多模态 content 为 list 时仅取文本片段，避免下游 `str += list` 抛错
                if isinstance(text, list):
                    text = "".join(
                        part.get("text", "") if isinstance(part, dict) else str(part)
                        for part in text
                    )
                if text:
                    yield text
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("stream 异常: %s", e)

    async def generate_image(self, prompt: str, *, size: str = "1024x1024", timeout_ms: int = 60_000) -> str:
        """调用智谱 CogView-3-Flash 生成图片，返回图片 URL。

        免费模型，通过 images/generations 端点调用。
        仅支持智谱主供应商（备用供应商不一定有图片能力）。
        """
        if not config.llm_available:
            raise RuntimeError("AI_API_KEY 未配置")

        url = f"{config.ai_base_url}/images/generations"
        payload = {
            "model": "cogview-3-flash",
            "prompt": prompt,
            "size": size,
        }
        headers = {
            "Authorization": f"Bearer {config.ai_api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=timeout_ms / 1000) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            # 智谱返回格式：{ "data": [{ "url": "..." }] }
            return data["data"][0]["url"]


# 全局单例
llm_provider = LLMProvider()
