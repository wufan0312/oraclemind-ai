"""P0-2 Observability 决策树追踪 —— 单元测试（内存兜底，无需 Redis）

运行：python tests/test_harness_observability.py
"""

import asyncio
import json

from src.harness.observability.tracer import Tracer, set_tracer, reset_tracer
from src.harness.observability.store import trace_store
from src.harness.observability.patch import install_tracing
from src.harness.observability.sse import route_trace


def test_tracer_basic():
    t = Tracer("tr_1", meta={"x": 1})
    t.mark_phase("analyzer", meta={"modules": ["bazi"]})
    t.record_step("paipan", "exec", meta={"ok": True})
    t.record_step("llm", "stream_messages", meta={"out_chars": 42})
    d = t.to_dict()
    assert d["trace_id"] == "tr_1"
    assert len(d["spans"]) == 3
    # llm 应挂到 analyzer 之下（决策树父子关系）
    assert d["spans"][2]["parent_id"] == d["spans"][0]["id"]
    assert d["llm_calls"] == 1
    print("PASS test_tracer_basic")


def test_store_roundtrip():
    t = Tracer("tr_mem")
    t.record_step("llm", "x")
    asyncio.run(trace_store.save(t))
    loaded = trace_store.load("tr_mem")
    assert loaded and loaded["trace_id"] == "tr_mem"
    assert trace_store.load("nonexistent_id") is None
    print("PASS test_store_roundtrip")


def test_patch_records_llm():
    class FakeProvider:
        async def stream_messages(self, messages, **kw):
            yield "hello "
            yield "world"

    prov = FakeProvider()
    install_tracing(prov)

    async def run():
        t = Tracer("tr_patch")
        tok = set_tracer(t)
        out = []
        async for c in prov.stream_messages([{"role": "user", "content": "hi"}]):
            out.append(c)
        reset_tracer(tok)
        return t, "".join(out)

    t, txt = asyncio.run(run())
    assert txt == "hello world"
    llm_spans = [s for s in t.spans if s.kind == "llm"]
    assert llm_spans, "LLM span 应被记录"
    assert llm_spans[0].meta["out_chars"] == len(txt)
    print("PASS test_patch_records_llm")


def test_route_trace_injects_id():
    async def gen():
        yield {"event": "open", "data": json.dumps({})}
        yield {"event": "delta", "data": json.dumps({"chunk": "x"})}
        yield {"event": "done", "data": json.dumps({})}

    async def run():
        out = []
        async for ev in route_trace(gen(), meta={"module": "test"}):
            out.append(ev)
        return out

    out = asyncio.run(run())
    assert out[0]["event"] == "open"
    d = json.loads(out[0]["data"])
    assert "traceId" in d and d["traceUrl"].startswith("/api/v1/trace/")
    loaded = trace_store.load(d["traceId"])
    assert loaded and loaded["meta"]["module"] == "test"
    print("PASS test_route_trace_injects_id")


def test_patch_error_recorded():
    class FailingProvider:
        async def stream_messages(self, messages, **kw):
            raise RuntimeError("boom")
            yield  # 让函数成为 async generator（编译期判定），首次迭代即抛错

    prov = FailingProvider()
    install_tracing(prov)

    async def run():
        t = Tracer("tr_err")
        tok = set_tracer(t)
        try:
            async for _ in prov.stream_messages([]):
                pass
        except RuntimeError:
            pass
        reset_tracer(tok)
        return t

    t = asyncio.run(run())
    err_spans = [s for s in t.spans if s.status == "error"]
    assert err_spans, "异常应被记录为 error 节点"
    print("PASS test_patch_error_recorded")


if __name__ == "__main__":
    test_tracer_basic()
    test_store_roundtrip()
    test_patch_records_llm()
    test_route_trace_injects_id()
    test_patch_error_recorded()
    print("\n✅ ALL OBSERVABILITY TESTS PASSED")
