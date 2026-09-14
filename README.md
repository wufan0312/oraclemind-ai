# 玄镜 OracleMind · AI 服务（Python 版）

基于 Python + LangChain + LangGraph + Chroma 重构的 AI 解读服务。

## 技术栈

| 层 | 技术 | 说明 |
|----|------|------|
| Web 框架 | FastAPI + SSE | 异步 HTTP 服务，SSE 流式输出 |
| LLM 编排 | LangChain | 统一 LLM 调用接口 |
| Agent | LangGraph + ReAct | 多步推理 Agent |
| RAG | Chroma | 向量数据库检索增强 |
| 类型校验 | Pydantic v2 | 请求/响应类型安全 |
| 缓存 | Redis | 幂等缓存 |

## 项目结构

```
oraclemind-ai-py/
├── src/
│   ├── __init__.py
│   ├── config.py              # 配置层
│   ├── types.py               # Pydantic 类型定义
│   ├── server.py              # FastAPI 入口
│   ├── routes/
│   │   ├── interpret.py       # 解读路由（含 SSE 流式）
│   │   ├── agent.py           # Agent 路由（起名 ReAct）
│   │   ├── astrology.py       # 占星路由
│   │   └── poster.py          # 海报路由
│   ├── services/
│   │   ├── provider.py        # LLM 供应商适配
│   │   ├── interpret.py       # 解读编排
│   │   ├── retrieval.py       # RAG 检索（Chroma）
│   │   ├── budget.py          # 预算熔断
│   │   └── cache.py           # Redis 缓存
│   ├── prompts/
│   │   ├── shared.py          # 共享 prompt 常量
│   │   ├── bazi.py            # 八字 prompt
│   │   ├── ziwei.py           # 紫微 prompt
│   │   ├── liuyao.py          # 六爻 prompt
│   │   ├── meihua.py          # 梅花 prompt
│   │   ├── qimen.py           # 奇门 prompt
│   │   ├── numerology.py     # 数字命理 prompt
│   │   ├── tarot.py           # 塔罗 prompt
│   │   ├── dream.py           # 解梦 prompt
│   │   ├── fengshui.py        # 风水 prompt
│   │   └── registry.py        # 模板注册表
│   ├── agent/
│   │   ├── ming_agent.py      # 起名 ReAct Agent
│   │   └── tools/
│   │       ├── bazi_paipan.py # 八字排盘工具
│   │       ├── name_generate.py # 起名工具
│   │       └── name_detail.py # 名字详批工具
│   └── knowledge/
│       └── ingest.py         # 知识库导入脚本
├── scripts/
│   └── ingest_kb.py           # 向 Chroma 导入典籍
├── data/chroma/               # Chroma 持久化目录
├── pyproject.toml
└── .env
```

## 快速启动

```bash
cd oraclemind-ai-py

# 安装依赖
pip install -e .

# 导入知识库到 Chroma（首次）
python scripts/ingest_kb.py

# 启动服务
python -m uvicorn src.server:app --host 0.0.0.0 --port 8001 --reload
```

## API 路由

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/interpret` | 单模块解读 |
| POST | `/api/v1/interpret/stream` | 流式解读（SSE） |
| POST | `/api/v1/retrieve` | 纯检索（RAG） |
| GET  | `/api/v1/modules` | 支持的模块列表 |
| POST | `/api/v1/summary` | 综合行动建议 |
| POST | `/api/v1/tarot/daily` | 每日塔罗 |
| POST | `/api/v1/agent/ming` | 起名 Agent |
| POST | `/api/v1/agent/ming/stream` | 起名 Agent（SSE） |
| POST | `/api/v1/astrology/*` | 占星解读 |
| POST | `/api/v1/poster/*` | 海报生成 |
| GET  | `/health` | 健康检查 |
