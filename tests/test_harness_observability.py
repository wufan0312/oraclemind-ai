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


def test_patch_wraps_chat():
    """回归：chat 埋点必须闭包捕获 _orig_chat。

    历史 bug：patch.py 只捕获了 _orig_stream，traced_chat 里裸写 _orig_chat
    → CPython 按全局名解析 → NameError: name '_orig_chat' is not defined。
    因 TRACE_ENABLED 默认 True，线上所有 provider.chat() 调用全挂
    （summary/stream、interpret、poster 一起降级）。原测试只覆盖
    stream_messages，故漏网。
    """
    class FakeProvider:
        async def stream_messages(self, messages, **kw):
            yield "s"

        async def chat(self, system_prompt, user_prompt, **kw):
            return {
                "content": "hello there",
                "tokens": {"prompt": 1, "completion": 2},
                "provider": "zhipu",
                "model": "glm-4-flash",
            }

    prov = FakeProvider()
    install_tracing(prov)

    async def run():
        # (a) 无 Tracer：必须原样透传（就是这里曾抛 NameError）
        passthrough = await prov.chat("sys", "usr")
        # (b) 有 Tracer：透传 + 记录 llm span
        t = Tracer("tr_chat")
        tok = set_tracer(t)
        traced = await prov.chat("sys", "usr", temperature=0.5, timeout_ms=1234)
        reset_tracer(tok)
        return passthrough, traced, t

    passthrough, traced, t = asyncio.run(run())
    assert passthrough["content"] == "hello there", "无 Tracer 时应原样返回"
    assert traced["content"] == "hello there"
    assert traced["provider"] == "zhipu"
    llm_spans = [s for s in t.spans if s.kind == "llm"]
    assert len(llm_spans) == 1, "chat 应记录 1 个 llm span"
    assert llm_spans[0].meta["out_chars"] == len("hello there")
    assert llm_spans[0].meta["tokens"] == {"prompt": 1, "completion": 2}
    print("PASS test_patch_wraps_chat")


def test_patch_chat_error_recorded():
    class ChatFailingProvider:
        async def stream_messages(self, messages, **kw):
            yield "s"

        async def chat(self, system_prompt, user_prompt, **kw):
            raise RuntimeError("chat boom")

    prov = ChatFailingProvider()
    install_tracing(prov)

    async def run():
        t = Tracer("tr_chat_err")
        tok = set_tracer(t)
        try:
            await prov.chat("s", "u")
        except RuntimeError:
            pass
        reset_tracer(tok)
        return t

    t = asyncio.run(run())
    err_spans = [s for s in t.spans if s.status == "error"]
    assert err_spans, "chat 异常应记录为 error 节点"
    assert "chat boom" in (err_spans[0].error or "")
    print("PASS test_patch_chat_error_recorded")


def test_patch_provider_without_chat():
    """精简 provider（无 chat 属性）不应报错，且 stream_messages 正常包装。"""
    class StreamOnlyProvider:
        async def stream_messages(self, messages, **kw):
            yield "only"

    prov = StreamOnlyProvider()
    install_tracing(prov)

    async def run():
        out = []
        async for c in prov.stream_messages([]):
            out.append(c)
        return out

    assert asyncio.run(run()) == ["only"]
    assert not hasattr(prov, "chat"), "本就没有 chat 的 provider 不该被凭空加上"
    print("PASS test_patch_provider_without_chat")


def test_patch_idempotent():
    """重复 install_tracing 必须幂等，否则 span 会重复计数。"""
    class FakeProvider:
        async def stream_messages(self, messages, **kw):
            yield "a"

        async def chat(self, system_prompt, user_prompt, **kw):
            return {"content": "b"}

    prov = FakeProvider()
    install_tracing(prov)
    first_stream, first_chat = prov.stream_messages, prov.chat

    install_tracing(prov)  # 第二次调用应当 no-op
    assert prov.stream_messages is first_stream, "重复安装不该再次包裹"
    assert prov.chat is first_chat

    async def run():
        t = Tracer("tr_idem")
        tok = set_tracer(t)
        out = []
        async for c in prov.stream_messages([]):
            out.append(c)
        r = await prov.chat("s", "u")
        reset_tracer(tok)
        return t, out, r

    t, out, r = asyncio.run(run())
    assert out == ["a"] and r["content"] == "b"
    assert len([s for s in t.spans if s.kind == "llm"]) == 2, "stream + chat 各 1 个，未重复"
    print("PASS test_patch_idempotent")


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
    test_patch_wraps_chat()
    test_patch_chat_error_recorded()
    test_patch_provider_without_chat()
    test_patch_idempotent()
    test_route_trace_injects_id()
    test_patch_error_recorded()
    print("\n✅ ALL OBSERVABILITY TESTS PASSED")
