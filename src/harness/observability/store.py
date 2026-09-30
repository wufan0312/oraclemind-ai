"""玄镜 OracleMind · Observability 决策树追踪 —— 存储层

热存储（hot）：优先 Redis（config.redis_url，与 budget.py 同款降级风格），
带内存兜底（无 Redis / Redis 不可用时），保证本地开发与单测可完整运行。
冷存储（cold，可选）：若配置了 TRACE_PG_DSN，best-effort 写入 PG（asyncpg 连接池，
可选依赖），用于长期留存与离线分析；未配置时仅保留热存储。

修复（2026-09-29）：
- 内存兜底改为写时 LRU 超额清理，避免只增不减导致线上 OOM（原仅在 load 时惰性过期）。
- PG 冷存储改为模块级连接池（懒建一次）+ 启动时建表一次，避免每条 trace 新建连接 + 重复 DDL。

所有异常均吞掉 —— 观测数据丢失绝不能拖垮主业务。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Optional

from src.config import config

import logging  # noqa: E402

logger = logging.getLogger(__name__)

# 内存兜底默认容量上限；超出后清理过期项，仍超出则清理最旧（FIFO）。
DEFAULT_MEM_MAX = 2000


class TraceStore:
    def __init__(self) -> None:
        self.ttl = config.trace_ttl_seconds
        self.redis = self._connect_redis()
        self.pg_dsn = config.trace_pg_dsn
        self.pg_table = config.trace_pg_table
        self._mem_max = getattr(config, "trace_mem_max", DEFAULT_MEM_MAX) or DEFAULT_MEM_MAX
        # 内存兜底：key -> (payload_json, expire_ts)
        self._mem: dict[str, tuple[str, float]] = {}
        self._mem_lock = threading.Lock()
        # PG 连接池：模块级懒建，整个进程复用；初始化失败则永久关闭冷存储避免反复重试
        self._pg_init_failed = False

    def _connect_redis(self):
        if not config.redis_url:
            return None
        try:
            import redis

            r = redis.from_url(
                config.redis_url, decode_responses=True, socket_connect_timeout=2
            )
            r.ping()
            return r
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[observability] Redis 不可用，trace 走内存兜底: {e}")
            return None

    async def save(self, tracer) -> None:
        """持久化一次 trace（热存储 + 可选冷存储）。"""
        tracer.finish()
        try:
            payload = json.dumps(tracer.to_dict(), ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return

        # 内存兜底（写时 LRU 超额清理）
        with self._mem_lock:
            self._mem[tracer.trace_id] = (payload, time.time() + self.ttl)
            self._evict_mem()

        # Redis 热存储
        if self.redis is not None:
            try:
                await asyncio.to_thread(
                    self.redis.setex, f"trace:{tracer.trace_id}", self.ttl, payload
                )
            except Exception:  # noqa: BLE001
                pass

        # PG 冷存储（可选）
        if self.pg_dsn and not self._pg_init_failed:
            try:
                await self._save_pg(tracer.trace_id, payload)
            except Exception:  # noqa: BLE001
                pass

    def _evict_mem(self) -> None:
        """写时超额清理：先清过期，仍超则清最旧（FIFO）。需在持有 _mem_lock 时调用。"""
        if len(self._mem) <= self._mem_max:
            return
        now = time.time()
        expired = [k for k, (_v, exp) in self._mem.items() if exp <= now]
        for k in expired:
            self._mem.pop(k, None)
        while len(self._mem) > self._mem_max:
            self._mem.popitem(last=False)

    async def _get_pool(self):
        """懒建模块级 PG 连接池（含建表），整个进程复用。失败则标记并关闭冷存储。"""
        global _pg_pool
        if _pg_pool is not None:
            return _pg_pool
        if self._pg_init_failed:
            return None
        async with _pg_lock:
            if _pg_pool is not None:
                return _pg_pool
            try:
                import asyncpg  # 可选依赖

                pool = await asyncpg.create_pool(self.pg_dsn, min_size=1, max_size=5)
                async with pool.acquire() as conn:
                    await conn.execute(
                        f"CREATE TABLE IF NOT EXISTS {self.pg_table} ("
                        "trace_id text primary key, payload jsonb, created_at timestamptz default now())"
                    )
                _pg_pool = pool
                logger.info("[observability] PG trace 连接池已建立")
            except Exception as e:  # noqa: BLE001
                self._pg_init_failed = True
                logger.warning(f"[observability] PG trace 连接池建立失败，关闭冷存储: {e}")
            return _pg_pool

    async def _save_pg(self, trace_id: str, payload: str) -> None:
        pool = await self._get_pool()
        if pool is None:
            return
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    f"INSERT INTO {self.pg_table}(trace_id, payload) VALUES($1,$2) "
                    f"ON CONFLICT(trace_id) DO UPDATE SET payload=$2",
                    trace_id,
                    json.loads(payload),
                )
        except Exception as e:  # noqa: BLE001
            # best-effort：单条失败不致命，异常吞掉避免拖垮主业务
            logger.debug(f"[observability] PG trace 写入失败(trace={trace_id}): {e}")

    def load(self, trace_id: str) -> Optional[dict]:
        """读取 trace（内存优先 → Redis）。无则返回 None。"""
        with self._mem_lock:
            item = self._mem.get(trace_id)
            if item:
                val, exp = item
                if exp > time.time():
                    try:
                        return json.loads(val)
                    except Exception:  # noqa: BLE001
                        pass
                self._mem.pop(trace_id, None)
        if self.redis is not None:
            try:
                v = self.redis.get(f"trace:{trace_id}")
                if v:
                    return json.loads(v)
            except Exception:  # noqa: BLE001
                pass
        return None


# 模块级单例与 PG 连接池（整个进程复用，避免每条 trace 新建连接）
_pg_pool: object = None
_pg_lock = asyncio.Lock()

trace_store = TraceStore()
