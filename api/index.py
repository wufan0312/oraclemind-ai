"""Vercel 入口：直接把 FastAPI ASGI 应用暴露给 Vercel 的 Python 运行时。

部署形态（AI 服务作为独立 Vercel 项目，rootDirectory=oraclemind-ai-py）：
- 本文件位于 api/index.py，Vercel 自动将其检测为 Python ASGI Function 入口（无需在 pyproject 的 [tool.vercel] 写 entrypoint）。
- Vercel 2026 原生支持 ASGI，**无需 Mangum**：它直接加载本模块的 `app` 变量并以 ASGI 协议驱动，
  本地跑的 FastAPI 应用「原样」部署上线。
- 前端调用 https://<ai-project>.vercel.app/api/v1/... 即命中本应用。

路由前缀说明（避免双 /api）：
- Vercel 把 `/api/*` 请求路由到 api/index.py，并把完整路径（含 /api）交给 FastAPI。
- 本应用各 router 注册的也是 `/api/v1/...` 前缀（见 src/routes/*.py: router = APIRouter(prefix="/api/v1...")），
  因此 /api/v1/interpret 这类路径在 Vercel 上与本地完全一致，无需任何前缀改造，也不会出现双 /api。
- 前端 NEXT_PUBLIC_AI_API_BASE 填「域名根路径，不含 /api」，路径里的 /api/v1 由前端携带。

注意：本服务依赖链含 chromadb（RAG 检索增强），其传递依赖较重
（kubernetes / onnxruntime / chromadb_rust_bindings / numpy 合计数百 MB），
且需要可写持久盘（CHROMA_PERSIST_DIR）。详见 DEPLOYMENT.md。
"""

from src.server import app

# Vercel 加载的 ASGI 应用入口（顶层 app 变量）。
__all__ = ["app"]
