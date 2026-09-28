"""玄镜 OracleMind · Observability 决策树追踪 —— 存储层

热存储（hot）：优先 Redis（config.redis_url，与 budget.py 同款降级风格），
带内存兜底（无 Redis / Redis 不可用时），保证本地开发与单测可完整运行。
冷存储（cold，可选）：若配置了 TRACE_PG_DSN，best-effort 写入 PG（asyncpg 可选依赖），
用于长期留存与离线分析；未配置时仅保留热存储，不强制引入新依赖。

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


class TraceStore:
    def __init__(self) -> None:
        self.ttl = config.trace_ttl_seconds
        self.redis = self._connect_redis()
        self.pg_dsn = config.trace_pg_dsn
        self.pg_table = config.trace_pg_table
        # 内存兜底：key -> (payload_json, expire_ts)
        self._mem: dict[str, tuple[str, float]] = {}
        self._mem_lock = threading.Lock()

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

        # 内存兜底
        with self._mem_lock:
            self._mem[tracer.trace_id] = (payload, time.time() + self.ttl)

        # Redis 热存储
        if self.redis is not None:
            try:
                await asyncio.to_thread(
                    self.redis.setex, f"trace:{tracer.trace_id}", self.ttl, payload
                )
            except Exception:  # noqa: BLE001
                pass

        # PG 冷存储（可选）
        if self.pg_dsn:
            await self._save_pg(tracer.trace_id, payload)

    async def _save_pg(self, trace_id: str, payload: str) -> None:
        try:
            import asyncpg  # 可选依赖
        except ImportError:
            return
        try:
            conn = await asyncpg.connect(self.pg_dsn)
            try:
                await conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {self.pg_table} ("
                    "trace_id text primary key, payload jsonb, created_at timestamptz default now())"
                )
                await conn.execute(
                    f"INSERT INTO {self.pg_table}(trace_id, payload) VALUES($1,$2) "
                    f"ON CONFLICT(trace_id) DO UPDATE SET payload=$2",
                    trace_id,
                    json.loads(payload),
                )
            finally:
                await conn.close()
        except Exception:  # noqa: BLE001
            pass

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


# 模块级单例：import 时即建立（Redis 连接失败会自动降级内存）。
trace_store = TraceStore()
