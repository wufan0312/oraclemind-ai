"""玄镜 OracleMind · AI 服务 FastAPI 入口"""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from uvicorn import run as uvicorn_run

from src.config import config
from src.services.budget import rate_limiter_allow, cache_backend_name

# 配置日志
logging.basicConfig(
    level=getattr(logging, config.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# 创建 FastAPI 应用
_app_kwargs: dict = {}
if config.is_production:
    # 生产环境关闭交互式文档，避免暴露全部 API 结构与参数
    _app_kwargs.update(docs_url=None, redoc_url=None, openapi_url=None)

app = FastAPI(
    title="玄镜 OracleMind · AI 服务",
    description="Python + LangChain + LangGraph + RAG + Agent + ReAct + Chroma",
    version="0.1.0",
    **_app_kwargs,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    # 用 cors_origin_list（配置值 + 内置线上前端域名的并集），兜底面板漏配/配错
    allow_origins=config.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
from src.routes.interpret import router as interpret_router
from src.routes.agent import router as agent_router
from src.routes.astrology import router as astrology_router
from src.routes.poster import router as poster_router

app.include_router(interpret_router)
app.include_router(agent_router)
app.include_router(astrology_router)
app.include_router(poster_router)

# Harness 工程化 · Observability 决策树追踪（P0-2）：装 LLM 埋点 + /trace/{id} 端点
from src.harness import install_observability

install_observability(app)


# 限流中间件（内存令牌桶，单进程；多 worker 才需 Redis）
_RATE_LIMIT_WHITELIST = ("/", "/health", "/docs", "/redoc", "/openapi.json")


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.url.path in _RATE_LIMIT_WHITELIST:
        return await call_next(request)
    if not rate_limiter_allow():
        return JSONResponse(
            status_code=429,
            content={"error": "RATE_LIMITED", "message": "请求过于频繁，请稍后再试"},
        )
    return await call_next(request)


# 可选 API Key 鉴权：配置了 AI_SERVICE_API_KEY 时强制校验 X-API-Key；
# 未配置（开发模式）放行，便于本机前端直接调用。
_AUTH_WHITELIST = ("/", "/health", "/docs", "/redoc", "/openapi.json")


# 审计中间件（SEC-13）：最外层包裹全部请求，记录 request_id / 客户端 IP / 端点 / 耗时 / 状态码。
# 不记录请求体（避免泄露出生信息、命理问题等隐私）。审计日志单独 logger "audit"，便于分流采集。
_audit_logger = logging.getLogger("audit")


@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    client = request.client.host if request.client else "?"
    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        # 异常由全局处理器兜底转 500；审计层只记录、不吞异常
        raise
    dur_ms = (time.monotonic() - start) * 1000
    _audit_logger.info(
        f"rid={rid} {request.method} {request.url.path} "
        f"status={response.status_code} {dur_ms:.0f}ms ip={client}"
    )
    response.headers["X-Request-ID"] = rid
    return response


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not config.ai_service_api_key:
        return await call_next(request)
    if request.url.path in _AUTH_WHITELIST:
        return await call_next(request)
    provided = request.headers.get("X-API-Key")
    if provided and provided == config.ai_service_api_key:
        return await call_next(request)
    logger.warning(f"鉴权失败: path={request.url.path} ip={request.client.host if request.client else '?'}")
    return JSONResponse(
        status_code=401,
        content={"error": "UNAUTHORIZED", "message": "缺少或无效的 API Key"},
    )


# 全局异常处理
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"未处理异常: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": "SERVER_ERROR",
            "message": "服务暂时不可用，请稍后重试",
        },
    )


# 根路径：给部署预览页 / 人工探活一个可读的 200 响应。
# 不定义它时，访问 / 会落到 FastAPI 的默认 404（{"detail":"Not Found"}），
# 容易被误读成「函数又崩了」—— 见 VERCEL_DEPLOY.md 6.6。
@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "oraclemind-ai-py",
        "name": "玄镜 OracleMind · AI 服务",
        "version": "0.1.0",
        "message": "服务已就绪。健康检查：GET /health；业务接口：/api/v1/*。",
    }


# 健康检查
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "oraclemind-ai-py",
        "version": "0.1.0",
        "llm_available": config.llm_available,
        "llm_fallback_configured": config.llm_fallback_configured,
        "retrieval_enabled": config.retrieval_enabled,
        "rate_limit": f"memory:{config.rate_limit_rps}/s",
        "cache_backend": cache_backend_name(),
    }


@app.on_event("startup")
async def on_startup():
    logger.info(
        f"玄镜 AI 服务启动 | port={config.port} | "
        f"provider={config.ai_provider} | model={config.ai_model_chat} | "
        f"llm_available={config.llm_available} | retrieval={config.retrieval_enabled}"
    )


def main():
    uvicorn_run(
        "src.server:app",
        host=config.ai_bind_host,
        port=config.port,
        # reload 默认关闭（config.ai_reload，受 AI_RELOAD 控制）：
        # 开启会派生 reloader+worker 双进程，worker 易变成杀不死的「孤儿监听」占死端口。
        reload=config.ai_reload,
        log_level=config.log_level,
    )


if __name__ == "__main__":
    main()
