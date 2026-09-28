"""玄镜 OracleMind · 预算熔断 + 缓存 + 限流

设计原则：
- Redis 为「可选增强」：配了且连得上 → 用 Redis（多进程 / 横向扩展共享）；
  未配、或连接失败 → **自动降级到内存兜底**，绝不让缓存/预算/限流拖垮主流程。
- 内存兜底做扎实，使单实例 / 本地开发**完全不需要 Redis 也能完整运行**：
    * 缓存：自带 TTL，避免长时间运行无限增长；
    * 预算：落地到本地 JSON 文件，重启不丢、当日累计准确；
    * 限流：内存令牌桶，按 AI_RATE_LIMIT_RPS 放行（单进程内有效）。
"""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional, TypeVar

from src.config import config

logger = logging.getLogger(__name__)

# 全站去缓存命中总开关：.env 设 DISABLE_CACHE=1 时，所有计算结果（占星/解读/合盘等）
# 不再读 Redis / 内存缓存，每次都重新计算并重新调用 LLM，保证拿到最新结果。
DISABLE_CACHE = os.getenv('DISABLE_CACHE', '0') == '1'
if DISABLE_CACHE:
    logger.warning('[cache] DISABLE_CACHE=1 —— 所有计算结果缓存已禁用，每次请求都会重新计算')

try:
    import redis

    _redis: Optional[redis.Redis] = None
    if config.redis_url:
        _redis = redis.from_url(config.redis_url, decode_responses=True, socket_connect_timeout=2)
except ImportError:
    _redis = None

# Redis 连接失败后置位：之后所有操作直接走内存，不再反复重试（避免每次请求都卡连接超时）
_redis_failed = False

# 启动时主动 ping 一次（最多 2s）：让 /health 的 cache_backend 从一开始就反映真实状态，
# 而不是等到第一次缓存操作失败才翻转。
if _redis is not None:
    try:
        _redis.ping()
    except Exception as e:
        _redis_failed = True
        logger.warning(f"Redis 不可用（{e}），启动即降级到内存兜底（缓存/预算/限流仍可用）")

_R = TypeVar("_R")


def _with_redis(redis_fn: Callable[[redis.Redis], _R], memory_fn: Callable[[], _R]) -> _R:
    """优先 Redis；未配置或连接失败后降级到内存兜底。"""
    global _redis_failed
    if _redis is None or _redis_failed:
        return memory_fn()
    try:
        return redis_fn(_redis)
    except Exception as e:  # 连接拒绝 / 超时 / 任何 Redis 异常
        _redis_failed = True
        logger.warning(f"Redis 不可用（{e}），已降级到内存兜底（缓存/预算/限流仍可用）")
        return memory_fn()


def cache_backend_name() -> str:
    if _redis is None:
        return "memory(ttl)"
    if _redis_failed:
        return "memory(ttl)[redis-down]"
    return "redis"


# ============================ 预算熔断 ============================

def _today_key() -> str:
    return f"budget:{datetime.now().strftime('%Y%m%d')}"


# 预算落地本地文件：重启不丢、单实例当日累计准确（多进程共享才需 Redis）
_BUDGET_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "budget_store.json"
_budget_lock = threading.Lock()


def _load_budget() -> dict[str, float]:
    try:
        if _BUDGET_FILE.exists():
            with open(_BUDGET_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_budget() -> None:
    try:
        _BUDGET_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _BUDGET_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_local_budget, f, ensure_ascii=False)
        tmp.replace(_BUDGET_FILE)
    except Exception:
        pass


_local_budget: dict[str, float] = _load_budget()


def _get_spent() -> float:
    def mem() -> float:
        return _local_budget.get(_today_key(), 0.0)

    def rdb(r: redis.Redis) -> float:
        v = r.get(_today_key())
        return float(v) if v else 0.0

    return _with_redis(rdb, mem)


def _set_spent(val: float) -> None:
    def mem() -> None:
        with _budget_lock:
            _local_budget[_today_key()] = val
            _save_budget()

    def rdb(r: redis.Redis) -> None:
        r.setex(_today_key(), int(timedelta(days=1).total_seconds()), val)

    _with_redis(rdb, mem)


def budget_broken() -> bool:
    """检查是否超出单日预算"""
    return _get_spent() >= config.budget_day_yuan


def estimate_cost(tokens: dict) -> float:
    """根据 token 用量估算成本（元）"""
    p = tokens.get("prompt", 0)
    c = tokens.get("completion", 0)
    return p * config.ai_price_input_per_1k / 1000 + c * config.ai_price_output_per_1k / 1000


def record_cost(cost: float) -> None:
    """记录成本"""
    _set_spent(_get_spent() + cost)


# ============================ 缓存（带 TTL 的内存兜底，可落盘） ============================
# 无 Redis 时：key -> (value, expire_ts)，get 时惰性过期。
# 兜底缓存落盘（data/cache_store.json）：Redis 不可用（如本地开发 / 容器起不来）时，
# 重启服务也能命中缓存，避免 summary 等重结果重启后白白再调一次 LLM（对应 P3-2）。

_CACHE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "cache_store.json"
_cache_lock = threading.Lock()
# 脏标记：仅在内存缓存被改动时才落盘，避免每次 set 全文件写（>500 条时瓶颈明显，对应 P2-4）
_cache_dirty = False
# 后台周期落盘停止信号
_flush_stop = threading.Event()


def _mark_dirty() -> None:
    global _cache_dirty
    _cache_dirty = True


def _load_cache() -> dict[str, tuple[str, float]]:
    try:
        if _CACHE_FILE.exists():
            with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            now = time.time()
            out: dict[str, tuple[str, float]] = {}
            # 落盘格式 {key: [value, expire_ts]}，加载时丢弃已过期项
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, list) and len(v) == 2 and isinstance(v[1], (int, float)) and v[1] > now:
                        out[k] = (str(v[0]), float(v[1]))
            return out
    except Exception:
        pass
    return {}


def _save_cache() -> None:
    """将内存缓存落盘（仅在 _cache_dirty 时执行，避免每次 set 全文件写）。"""
    global _cache_dirty
    try:
        with _cache_lock:
            if not _cache_dirty:
                return
            now = time.time()
            # 落盘时顺手剔除已过期项，避免文件无限增长
            expired = [k for k, (_, exp) in _local_cache.items() if exp <= now]
            for k in expired:
                _local_cache.pop(k, None)
            _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _CACHE_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_local_cache, f, ensure_ascii=False)
            tmp.replace(_CACHE_FILE)
            _cache_dirty = False
    except Exception:
        pass


def _cache_flush_loop() -> None:
    """每 10s 触发一次落盘；进程退出时 atexit 也会落盘最后一次（见模块底部）。

    代价：极端情况下进程在 10s 窗口内被强杀，最近 <10s 的新增缓存会丢失（缓存本就是可重算的兜底，
    丢失仅意味着重启后重新计算一次，不影响正确性）。间隔由 30s 收紧到 10s（BUG-08），
    降低强杀场景下的缓存丢失窗口，代价可忽略。
    """
    while not _flush_stop.wait(10):
        _save_cache()


_local_cache: dict[str, tuple[str, float]] = _load_cache()

# 后台周期落盘：每 30s 将脏缓存写盘一次；进程退出时 atexit 兜底落盘最后一次。
# 仅在本模块被实际导入（即 AI 服务运行）时启动一次。
try:
    _flush_thread = threading.Thread(target=_cache_flush_loop, name="cache-flush", daemon=True)
    _flush_thread.start()
    atexit.register(_save_cache)
except Exception:  # pragma: no cover
    pass


def cache_get(key: str) -> Optional[str]:
    if DISABLE_CACHE:
        return None

    def mem() -> Optional[str]:
        item = _local_cache.get(key)
        if item is None:
            return None
        val, exp = item
        if exp <= time.time():
            _local_cache.pop(key, None)
            _mark_dirty()  # 惰性过期也需落盘，避免旧文件仍含已过期项
            return None
        return val

    def rdb(r: redis.Redis) -> Optional[str]:
        return r.get(key)

    return _with_redis(rdb, mem)


def cache_set(key: str, value: str, ttl_seconds: int = 3600) -> None:
    if DISABLE_CACHE:
        return

    def mem() -> None:
        _local_cache[key] = (value, time.time() + ttl_seconds)
        # 仅标记脏，由后台 30s flush 线程（或进程退出 atexit）统一落盘，
        # 避免 >500 条时每次 set 都全文件写（对应 P2-4）
        _mark_dirty()

    def rdb(r: redis.Redis) -> None:
        r.setex(key, ttl_seconds, value)

    _with_redis(rdb, mem)


@lru_cache(maxsize=None)
def _prompt_fmt_hash(module: str) -> str:
    """对「影响该 module 输出格式」的源文件取内容哈希（缺陷报告 P1-10）。

    覆盖三类文件：
      1. ``src/prompts/<module>.py`` —— 该模块专属 prompt 模板
      2. ``src/prompts/shared.py``   —— 跨模块共用的提示片段 / 免责声明
      3. ``src/services/formatter.py`` —— LLM JSON → Markdown 的渲染结构

    任一文件一旦改动，哈希随之变化，该 module 的旧缓存**自动整体失效**，
    不再依赖开发者手动 bump ``_CACHE_VER``（历史上 14 次升级全是事后补 bump，
    漏一次就会让旧缓存绕过新 formatter 直接返回错误格式）。

    仅在进程内计算一次（lru_cache）；源文件缺失（如打包部署）时跳过该文件，
    不会因取哈希失败而中断解读。
    """
    base = Path(__file__).resolve().parents[1]  # .../src
    candidates = (
        base / "prompts" / f"{module}.py",
        base / "prompts" / "shared.py",
        base / "services" / "formatter.py",
    )
    h = hashlib.md5()
    for p in candidates:
        try:
            h.update(p.name.encode())
            h.update(p.read_bytes())
        except OSError:
            continue
    return h.hexdigest()[:12]


def make_cache_key(module: str, result_json: str, focus: Optional[str] = None) -> str:
    """构建缓存键

    **P1-10 起缓存键末尾追加 `_prompt_fmt_hash(module)`**：prompt 模板 / formatter
    的内容哈希，改了就自动失效，手动 _CACHE_VER 只作为紧急整体失效的兜底开关。

    缓存版本后缀（_CACHE_VER）为「输出格式/字段破坏性变更」的总开关：
    一旦 formatter 对某些 module/focus 的渲染结构（如是否输出某标题段）发生变更，
    必须 bump 该版本号，使所有旧缓存整体失效，否则旧缓存文本会绕过新 formatter 直接返回。
    v6: 紫微 sihua/stars 不再输出「建设性建议」「近期趋势」（formatter 已按 focus 屏蔽），
        旧 v5 缓存文本含这两段，需整体失效。
    v7: 综合运势 /summary 输出新增 timeline 时间轴字段、融入跨页占卜（数字命理/塔罗/星座），
        旧 v6 缓存无 timeline，需整体失效。
    v8: 综合运势 consensus 新增 agreement（共识度）字段，与 score（运势强度）语义分离；
        归一化层虽会把缺失的 agreement 补成 0（前端隐藏），但旧 v7 缓存未经本轮语义
        校准、其 score 也未按"强度"重新对齐，需整体失效重算。
    v9: 塔罗（tarot）prompt 升级到 v3：输出由通用 aspects（性格倾向/事业财运/…）
        改为塔罗专属 cards/synthesis/timing/risk，并注入前端牌库牌义作为唯一口径。
        旧 v8 缓存是按通用 aspects 渲染的文本，需整体失效重算。
    v10: 塔罗 prompt v3.1 —— timing 由「≤60 字的一句话」改为 near/mid/far 三段结构，
         formatter 渲染成三条列表。旧 v9 缓存里 timing 是字符串，需整体失效重算。
    v11: 数字命理 prompt v3.0 —— 本命模式新增大师数标注、九宫格连线、四项挑战数与
         核心数字（表现/内驱/人格/成熟），输出新增「姓名能量」「跨越挑战数」两节；
         并新增合盘模式（mode=synastry）输出结构。旧 v10 缓存无这些段落，需整体失效重算。
    v12: 数字命理 prompt v3.1 —— v3.0 仍沿用通用 OUTPUT_FORMAT，实际渲染成
         （性格倾向/事业财运/情感人际/格局印证），且会诱导模型编造东方典籍引文。
         v3.1 改用 build_system 覆盖为数字命理专属 aspects。旧 v11 缓存是通用结构，需整体失效重算。
    v13: formatter 新增 _strip_structural_residue（清洗 markdown 中粘连的 JSON 残留：
         **Text**:/Title: 字段名、'}/'] 碎片、字面 \\n）。旧 v12 缓存里已落盘坏输出，
         bump 使其失效重算（2026-09-07 用户实测报告页出现 Text/Title/'] 直出）。
    v14: formatter 两处修复 —— ① 新增 _close_truncated_json（补全被 max_tokens 掐断的
         JSON，GLM-4-Flash 偶发）；② _heuristic_format 兜底中文化（键名映射中文标签、
         cards 逐牌合并），不再输出 **Verdict**/**Pos** 英文键名。旧 v13 缓存里已有
         英文键名坏输出（2026-09-08 塔罗页实测），bump 失效重算。
    """
    _CACHE_VER = "v19"  # v19: 2026-09-20 新增 5.2 测评类请求禁外链只推自家工具 + CTA 新增 scales 目标（手动失效）
    h = hashlib.md5(result_json.encode()).hexdigest()[:16]
    # 末尾的 _prompt_fmt_hash(module)：prompt/format 源文件一改即自动失效（P1-10）
    return f"interpret:{module}:{h}:{focus or 'default'}:{_CACHE_VER}:{_prompt_fmt_hash(module)}"


# ============================ 限流（内存令牌桶，单进程） ============================

class _TokenBucket:
    """简单令牌桶：rate 个/秒，突发容量 = capacity。"""

    def __init__(self, rate: float, capacity: float):
        self.rate = rate
        self.capacity = capacity
        self.tokens = float(capacity)
        self.last = time.monotonic()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.last
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False


_rate_bucket: Optional[_TokenBucket] = None


def _get_bucket() -> _TokenBucket:
    global _rate_bucket
    if _rate_bucket is None:
        rps = max(1, config.rate_limit_rps)
        _rate_bucket = _TokenBucket(rate=rps, capacity=rps)
    return _rate_bucket


def rate_limiter_allow() -> bool:
    """限流放行判断（多 worker 安全）。

    - rate_limit_rps <= 0 视为不限流；
    - 配置了 Redis：用 1 秒固定窗口计数器（多 worker / 横向扩展共享同一计数），
      单次 Redis 抖动只降级到本进程内存令牌桶，不污染全局 Redis 健康标志；
    - 无 Redis / Redis 不可用：单进程内存令牌桶兜底（单实例/本地开发完全够用）。
    """
    if config.rate_limit_rps <= 0:
        return True
    limit = max(1, config.rate_limit_rps)

    # Redis 固定窗口计数：多 worker 横向扩展时共享同一计数
    if _redis is not None and not _redis_failed:
        try:
            window = int(time.time())
            key = f"rl:{window}"
            n = _redis.incr(key)
            if n == 1:
                _redis.expire(key, 2)
            return n <= limit
        except Exception as e:
            logger.warning(f"Redis 限流计数失败，降级内存令牌桶：{e}")
            # 注意：不置 _redis_failed，避免单次限流抖动拖垮整个 Redis 缓存层

    # 无 Redis / Redis 不可用时：单进程内存令牌桶
    return _get_bucket().allow()
