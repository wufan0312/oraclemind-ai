# 玄镜 OracleMind · AI 服务部署指南（oraclemind-ai）

> 与后端（oraclemind-backend）一致：**Vercel 2026 原生支持 ASGI，直接用 ASGI 入口，无需 Mangum。**
> 本服务作为**独立 Vercel 项目**（rootDirectory = `oraclemind-ai`）。

---

## 1. 部署形态

| 文件 | 作用 |
|------|------|
| `api/index.py` | 原生 ASGI 入口：`from src.server import app`，Vercel 直接加载 `app` 变量 |
| `pyproject.toml` | `[tool.vercel] entrypoint = "api/index:app"` 锁定入口 |
| `requirements.txt` | 精选运行时依赖（供 Vercel 安装，切勿用 `pip freeze` 整份导出） |
| `vercel.json` | 函数 `maxDuration=60 / memory=1024`，`excludeFiles` 剔除 `.venv/tests/scripts/data/chroma` 等 |
| `.env.example` | 配置模板 + Vercel/生产追加注记 |

启动逻辑沿用本地：`src/server.py` 在模块加载时构建 `app`，`on_startup` 仅打日志。
**没有需要 Alembic 之类的建表步骤**（本服务无独立数据库，缓存走 Redis/内存，RAG 走 Chroma）。

---

## 2. 路由前缀（无双 /api，无需改造）

所有 router 在 `src/routes/*.py` 中已注册 `/api/v1...` 前缀：

- `interpret` → `/api/v1/interpret`、`/api/v1/interpret/stream`、`/api/v1/retrieve`、`/api/v1/modules` …
- `agent` → `/api/v1/agent/ming`、`/api/v1/agent/home` …
- `astrology` → `/api/v1/astrology/natal` …
- `poster` → `/api/v1/poster`、`/api/v1/poster/generate` …
- 健康检查 → `/health`（根路径，Vercel 下即 `https://<ai-project>.vercel.app/health`）

Vercel 把 `/api/*` 交给 `api/index.py` 时携带完整路径（含 `/api`），与本地一致。
前端 `NEXT_PUBLIC_AI_API_BASE` 填**域名根路径、不含 /api**（如 `https://<ai-project>.vercel.app`），
请求路径里的 `/api/v1/...` 由 `src/lib/api.ts` 携带 —— **不要**在 env 里写 `/api`，否则会双 `/api`。

---

## 3. ⚠️ chromadb 体积与持久化（部署前必读）

本服务若开启 RAG（`RETRIEVAL_ENABLED=true`，默认开），会用到 `chromadb`。这是**与后端最大的不同点**，必须正视：

### 3.1 体积风险（Vercel 500MB 上限）
`chromadb` 的传递依赖会拉入：
- `kubernetes` ~84MB
- `onnxruntime` ~45MB
- `chromadb_rust_bindings` ~61MB
- `numpy` + `numpy.libs` ~55MB
- 叠加 `langchain` / `langgraph` / `grpc` / `openai` / `huggingface_hub` 等

合计极易**逼近甚至超过 Vercel 函数 500MB 包体上限**。后果是部署失败或函数无法加载。

**缓解手段（任选）：**
1. **推荐：AI 服务不放 Vercel。** 部署到支持持久盘、无 500MB 硬限制的主机
   （Railway / Render / Fly.io / 自有 VM），`.env` 配好即可，无需改代码。前端 `NEXT_PUBLIC_AI_API_BASE` 指向该域名。
2. **关闭 RAG（已默认）：** 在 Vercel 环境变量设 `RETRIEVAL_ENABLED=false`（`.env.production` 已置）。
   **`chromadb` 已从 `requirements.txt` 移出**（改放 `requirements-rag.txt`）—— 因为其传递依赖
   实测 400MB+（kubernetes + onnxruntime + chromadb_rust_bindings + numpy），会撑爆 Vercel 函数
   体积上限（Hobby 未压缩约 250MB），表现为部署/函数加载失败 `FUNCTION_INVOCATION_FAILED`。
   代码里 `import chromadb` 本就是惰性 + `try/except`（`src/services/retrieval.py:get_collection()`），
   缺包时 RAG 静默降级为空检索。
   → **Vercel 默认部署已天然不装 chromadb，无需改 Install Command。**
   本地 / 需要 RAG 时：`pip install -r requirements-rag.txt`。
3. **精简：** 用 `vercel.json` 的 `excludeFiles` 剔除非运行文件（已配置），并在 Vercel 选 Pro 计划放宽限制。

### 3.2 持久化风险（只读文件系统）
Vercel 函数运行时代码目录**只读**，仅 `/tmp` 可写。涉及写盘的两处：
- **缓存/预算落盘**（`data/budget_store.json`、`data/cache_store.json`）：
  代码已用 `try/except` 静默降级为内存缓存，**不会崩**，但跨实例/冷启动命中率下降。
  → 生产建议配 `REDIS_URL=rediss://<upstash>`（Upstash）走 Redis，避免依赖本地落盘。
- **Chroma 向量库**（`CHROMA_PERSIST_DIR`，默认 `./data/chroma`）：
  默认路径在只读目录，**首请求建库会失败**。
  → Vercel 上必须设 `CHROMA_PERSIST_DIR=/tmp/oraclemind_chroma`。

---

## 4. 部署清单（Vercel）

1. 在 Vercel 新建项目，Repository 选本仓库，`Root Directory` = `oraclemind-ai`。
2. 构建/运行时环境变量（Project Settings → Environment Variables）：
   - `AI_API_KEY`（智谱 GLM-4-Flash 密钥，**必填**，否则 llm_available=false）
   - `AI_PROVIDER=zhipu`、`AI_MODEL_CHAT=glm-4-flash`
   - `APP_ENV=production`
   - `CORS_ORIGINS=https://<前端项目>.vercel.app`（多个用逗号，勿留 localhost）
   - `REDIS_URL=rediss://<upstash-host>`（推荐；不配则退回内存缓存）
   - 若保留 RAG：`CHROMA_PERSIST_DIR=/tmp/oraclemind_chroma`
   - 若关 RAG：`RETRIEVAL_ENABLED=false`
3. （可选）Build Command 留空即可 —— Vercel 会用 `requirements.txt` 安装依赖并加载 ASGI 入口。
4. 部署后验证：`GET https://<ai-project>.vercel.app/health` 应返回 200，且 `llm_available: true`、`cache_backend` 为 `redis` 或 `memory`。

---

## 5. 本地运行（不变）

```bash
cd oraclemind-ai
cp .env.example .env   # 填 AI_API_KEY
python scripts/ai_server.py start     # 默认端口 8021，单进程、无 reload
python scripts/ai_server.py status    # 健康检查
```
启停仍走 `scripts/ai_server.py`（脱离控制台 + pidfile + 端口扫描兜底），避免 orphan 监听。
