# 玄镜 OracleMind · AI 服务层

这是 OracleMind（玄镜）的 AI 服务层。

OracleMind 是一套已上线的生产级 AI 平台，前端、AI 服务、后端三端解耦。本仓库负责 LangGraph 多智能体编排、ChromaDB RAG 检索、SSE 流式输出，以及最核心的——**把不稳定的 LLM 输出做成稳定产品能力**。

如果你在找「能独立交付 AI 应用、并且解决过 LLM 工程化问题」的人，这个仓库里的代码和设计文档，就是我的答案。

---

## 这块解决什么问题

把 LLM 直接丢进生产环境，问题从来不止「会不会写 prompt」：

- **输出格式不稳定**：同样一个问题，模型有时返回干净 JSON，有时裹一层 markdown 代码块，有时把字段名写错、漏字段。下游一旦按预期解析，当场崩。
- **多智能体工具调用可能死循环**：让模型「自己决定要不要再调一次工具」，它真能一直调下去——跑空、烧钱、还不给结论。
- **RAG 检索阈值不好定**：阈值调高，相关但表述角度偏的条目被砍掉，解读丢失唯一参考；调低，又把不相关的塞进 prompt，把模型带偏。
- **API 成本容易失控**：没有熔断，一个晚上跑飞就能把整月预算烧光，而且你往往第二天才发现。

这四个问题，靠「换个更强的模型」一个都解决不了。它们本质上是**工程问题**，不是模型问题。

---

## 核心设计决策

### 1. 为什么用 LangGraph，而不自己写循环

我确实可以手搓一个 `while` 循环去驱动 ReAct。但 LangGraph 的价值不在「能编排」，而在于它把「agent 该何时停」这件事**标准化**了——内置的 max steps 是我方死循环防护的第一道闸。

更关键的是边界划分：**模型只负责生成候选解读，确定性计算一律不碰模型**。八字 / 紫微 / 六爻 / 梅花 / 奇门排盘、数字命理、本命盘实算，全部是后端 FastAPI 的确定性算法，被封装成 tool 由 agent 调用（`bazi_paipan` / `ziwei_paipan` / `name_*`）。模型拿到的，是已经算好的结构化结果，不是让它「算一下」。

> 这件事决定了整个仓库的稳定性上限：输出准不准、快不快，取决于代码和历法库，不取决于模型今天心情如何。知道什么时候**不该用模型**，比会用 LangGraph 重要得多。

### 2. 三层降级链，分别降什么、触发条件是什么

```
L1  供应商故障切换   主 zhipu(glm-4-flash) → 备 aliyun(qwen-max)        触发：主供应商异常 / 30s 总超时
L2  输出契约校验     LLM 结果过业务契约；不通过回灌修正一次，仍不过 → degraded=True   触发：JSON 不合法 / 缺必需字段
L3  服务级优雅降级   AI_API_KEY 未配 / 单日预算超限 / Agent 运行异常 → 发降级文案而非 500   触发：配置缺失 / 预算熔断 / 异常捕获
```

- **L1（`services/provider.py`）**：主供应商失败先打 warning，再切备用；备用也没有才抛错。这一层只在「模型还能用」时兜。
- **L2（`harness/boundary/`）**：这是我认为最有价值的一层。LLM 返回后过一遍业务契约（`MODULE_SCHEMAS`），不合法不直接丢给用户，而是把结果**回灌再生成一次**；仍不合法，标记 `degraded=True`，**调用方走本地规则兜底**——坏输出永远不会进用户视野。
- **L3（agent 层）**：连模型都不该调的场景——API Key 没配、今天预算烧完了、agent 跑了异常——直接发一段降级文案，请求体面结束，而不是前端白屏。

前端还有第四层：后端 / AI 不可达时静默降级到本地算法，保证断网也能用。

### 3. 成本熔断的阈值怎么定的

`BUDGET_DAY_YUAN` 默认 **5.0 元 / 天**（可配）。计价按智谱 glm-4-flash：输入 0.001 元/1k token、输出 0.002 元/1k token，每次调用按 token 估算累加。

为什么是 5 元：这是一个仍在迭代的小流量产品，不是要跑满额度——设一个低水位，是为了「**超了就立刻全量降级**，宁可少解读，不要半夜跑飞」。累计按天滚动（`setex 1天`），生产走 Redis；本地 / 容器起不来时落内存 + JSON 文件兜底，重启不丢、当日累计准确。

### 4. RAG 检索阈值调高调低的对比结果

`MIN_RELEVANCE = 0.15`（cosine 相似度下限）。我把它从 0.25 降了下来，有实测依据：

- **调高（≥0.25）的坑**：本仓库单模块语料只有 14~20 条（极小语料）。某条唯一相关的典籍，因为表述角度不同、相似度略低于阈值（比如 0.20）会被直接删掉，解读反而丢了唯一参考——实测曾把「全部正位」条目检索进含逆位的牌阵。
- **调低（0.15）的结果**：保底让「稀疏 / 角度偏」的查询不误删唯一相关条；而小语料下 `top_k` 候选本就稀薄，降阈值并不会让无关条混进来。基线评估显示命中条目 min_score 区间 0.45~0.62，远高于阈值。

结论：**检索宁少勿杂，但阈值必须低于「小语料下唯一相关条的相似度」**。阈值之上还要做去重（同书同章只留最高分、正文前 300 字相同直接丢），再进 prompt。

---

## 怎么跑起来

### 环境要求

- Python 3.12+
- 运行时依赖是**精选**的（`requirements.txt`）；切勿 `pip freeze` 全量导出。
- RAG 需要的 `chromadb` 体积巨大（kubernetes + onnxruntime + chromadb_rust_bindings + numpy 合计 400MB+），**故意不进 `requirements.txt`**，需要时才 `pip install -r requirements-rag.txt`。默认 `RETRIEVAL_ENABLED=true`；Vercel 部署时建议关掉或把本服务部署到支持持久盘的主机（Railway / Render / Fly.io），避开 Vercel 函数 500MB 上限。

### 安装与启动

```bash
cd oraclemind-ai
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env        # 至少填 AI_API_KEY
python scripts/ai_server.py start    # 默认端口 8021，单进程、无 reload、启动后自动探活
python scripts/ai_server.py status   # 查看运行状态（pidfile + 端口 + 健康检查）
```

> 启动脚本刻意**不开 `uvicorn --reload`**：reload 会 fork 出 worker 持有监听套接字，父进程异常退出就变成「孤儿监听」、端口被占且杀不死。脚本用 pidfile + 端口扫描双重清理，从根上杜绝端口漂移。

### 环境变量示例

```bash
# 必填
AI_API_KEY=你的智谐密钥
AI_PROVIDER=zhipu
AI_MODEL_CHAT=glm-4-flash
AI_BASE_URL=https://open.bigmodel.cn/api/paas/v4

# 可选：备用供应商（主供应商故障时切换）
AI_PROVIDER_FALLBACK=aliyun
AI_MODEL_FALLBACK=qwen-max
AI_API_KEY_FALLBACK=你的阿里百炼密钥

# 成本控制 / 缓存
BUDGET_DAY_YUAN=5.0
REDIS_URL=rediss://<upstash-host>     # 不配则降级内存缓存

# 检索增强
RETRIEVAL_ENABLED=true                # Vercel 部署建议 false
RETRIEVAL_TOPK=6

# 跨域 / 排盘服务
CORS_ORIGINS=https://oraclemind-frontend.vercel.app
PAIPAN_API_BASE=http://localhost:8000  # 指向后端排盘服务
```

### 部署（Vercel）

本服务是独立 Vercel 项目，`Root Directory = oraclemind-ai`，用原生 ASGI 入口 `api/index.py`（`from src.server import app`），无需 Mangum。部署后访问 `GET /health` 确认 `llm_available` 与 `cache_backend` 真实状态。chromadb 体积与只读文件系统坑位详见 `DEPLOYMENT.md`。
