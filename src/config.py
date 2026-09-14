"""玄镜 OracleMind · AI 服务配置层

所有配置项集中于此，读取 .env / 环境变量，禁止在业务代码中硬编码。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

load_dotenv()


def _num(key: str, default: int) -> int:
    v = os.environ.get(key)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except ValueError:
        return int(float(v))


def _float(key: str, default: float) -> float:
    v = os.environ.get(key)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _bool(key: str, default: bool) -> bool:
    v = (os.environ.get(key) or "").strip().lower()
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("false", "0", "no", "off"):
        return False
    return default


def _str(key: str, default: str) -> str:
    return os.environ.get(key) or default


def _list(key: str, default: list[str]) -> list[str]:
    v = os.environ.get(key)
    if not v:
        return default
    return [s.strip() for s in v.split(",") if s.strip()]


def _normalize_provider(name: str) -> str:
    n = name.strip().lower()
    aliases = {
        "智谱": "zhipu", "zhipu": "zhipu", "glm": "zhipu", "bigmodel": "zhipu",
        "通义": "aliyun", "aliyun": "aliyun", "qwen": "aliyun", "通义千问": "aliyun",
        "deepseek": "deepseek", "深度求索": "deepseek",
    }
    return aliases.get(n, n)


class Config:
    """全局配置（单例模式）"""

    # ----- 服务 -----
    port: int = _num("PORT", 8001)
    app_env: str = _str("APP_ENV", "development")
    # uvicorn 代码热重载开关（默认关闭）。
    # 关闭原因：reload 会派生 reloader + worker 两个进程，worker 持有监听套接字；
    # 一旦父进程被强杀或重载异常，worker 会变成「孤儿监听」——端口被占且 taskkill
    # 找不到进程，只能被迫迁移端口。改代码请走 scripts/ai_server.py restart。
    ai_reload: bool = _bool("AI_RELOAD", False)
    log_level: str = _str("LOG_LEVEL", "info")
    cors_origins: list[str] = _list("CORS_ORIGINS", ["http://localhost:3000"])
    # 服务绑定地址：默认仅监听本机回环 127.0.0.1（安全性）；生产经反代转发时同样用回环，
    # 切勿直接暴露 0.0.0.0 到公网。确需跨机访问时再显式设为 0.0.0.0 并配合防火墙/反代。
    ai_bind_host: str = _str("AI_BIND_HOST", "127.0.0.1")
    # 服务间 / 前端调用本服务的 API Key：留空=开发模式不校验；生产必须配置，且调用方需带 X-API-Key 头。
    ai_service_api_key: str = _str("AI_SERVICE_API_KEY", "")
    ai_provider: str = _str("AI_PROVIDER", "zhipu")
    ai_model_chat: str = _str("AI_MODEL_CHAT", "glm-4-flash")
    ai_model_lite: str = _str("AI_MODEL_LITE", "glm-4-flash")
    ai_api_key: str = _str("AI_API_KEY", "")
    ai_base_url: str = _str("AI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")

    # 备用供应商
    # 注意：ai_model_fallback 必须是阿里云百炼（兼容模式）的有效模型标识，
    # 旧的 "qwen3.6-27b" 在通义千问命名规范中不存在，会导致 fallback 调用必败（安全缺陷报告 BUG-01）。
    # 改为通用有效的 qwen-max；如需更低成本可改 qwen-plus / qwen-turbo。
    ai_provider_fallback: str = _normalize_provider(_str("AI_PROVIDER_FALLBACK", "aliyun"))
    ai_model_fallback: str = _str("AI_MODEL_FALLBACK", "qwen-max")
    ai_api_key_fallback: str = _str("AI_API_KEY_FALLBACK", "")
    ai_base_url_fallback: str = _str(
        "AI_BASE_URL_FALLBACK",
        "https://ws-0q1asnif44n77cn8.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    )

    # ----- 请求参数 -----
    ttft_timeout_ms: int = _num("AI_TTFT_TIMEOUT_MS", 10_000)
    total_timeout_ms: int = _num("AI_TOTAL_TIMEOUT_MS", 30_000)
    rate_limit_rps: int = _num("AI_RATE_LIMIT_RPS", 20)
    # 流式响应单 chunk 最大不活跃超时（秒）：超过则该次流式提前结束，避免上游 stall 导致永久挂起
    stream_timeout_seconds: int = _num("STREAM_TIMEOUT_SECONDS", 60)

    # ----- 缓存 -----
    redis_url: str = _str("REDIS_URL", "")

    # ----- 成本控制 -----
    budget_day_yuan: float = _float("BUDGET_DAY_YUAN", 5.0)
    ai_price_input_per_1k: float = _float("AI_PRICE_INPUT_PER_1K", 0.001)
    ai_price_output_per_1k: float = _float("AI_PRICE_OUTPUT_PER_1K", 0.002)

    # ----- 排盘服务 -----
    paipan_api_base: str = _str("PAIPAN_API_BASE", "http://localhost:8000")

    # ----- 检索增强（RAG）-----
    retrieval_enabled: bool = _bool("RETRIEVAL_ENABLED", True)
    retrieval_topk: int = _num("RETRIEVAL_TOPK", 6)

    # ----- Chroma 向量数据库 -----
    chroma_persist_dir: str = _str("CHROMA_PERSIST_DIR", "./data/chroma")
    chroma_collection: str = _str("CHROMA_COLLECTION", "oraclemind_kb")
    embedding_api_key: str = _str("EMBEDDING_API_KEY", "")
    embedding_base_url: str = _str("EMBEDDING_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    embedding_model: str = _str("EMBEDDING_MODEL", "embedding-3")

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def llm_available(self) -> bool:
        return len(self.ai_api_key) > 0

    @property
    def llm_fallback_configured(self) -> bool:
        """备用供应商是否真正就绪（SEC-16/17 可见性修复）。

        llm_available 仅校验主 key 长度，运维容易误以为「配了 fallback 就双保险」，
        但旧默认 ai_model_fallback='qwen3.6-27b' 无效、且 ai_api_key_fallback 常为空，
        主供应商故障时 fallback 实际不可用。此处暴露真实就绪状态供 /health 观测。
        """
        return bool(self.ai_api_key_fallback) and self.ai_model_fallback.lower().startswith("qwen")


config = Config()
