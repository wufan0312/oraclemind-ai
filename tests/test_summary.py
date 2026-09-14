"""玄镜 OracleMind · 综合运势(summary) 回归测试

守护 `summary_interpret_v3` prompt 的「输出契约」不被破坏：
- _normalize 必须强制 6 维共识度 / 4 张卡片 / 3 栏建议 / 3~4 段时间轴，且 score 钳制 [60,95]；
- _clamp_int 对任意脏输入（None / NaN / 越界 / 非数字字符串）安全回落；
- ok:false 合法失败分支原样透传；
- interpret_summary_stream 的事件顺序与分段结构稳定（前端渐进渲染依赖此契约）。

不依赖网络 / LLM：generate_structured 与缓存/预算均被 monkeypatch。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from src.services.summary import (
    _build,
    _clamp_int,
    _fallback,
    _normalize,
    interpret_summary_stream,
)


# ---------------------------------------------------------------------------
# _clamp_int
# ---------------------------------------------------------------------------

def test_clamp_int_normal():
    assert _clamp_int(85, 60, 95, 78) == 85
    assert _clamp_int("85", 60, 95, 78) == 85
    assert _clamp_int(85.6, 60, 95, 78) == 85


def test_clamp_int_overflow_underflow():
    assert _clamp_int(200, 60, 95, 78) == 95
    assert _clamp_int(-3, 60, 95, 78) == 60


def test_clamp_int_dirty_inputs_fallback():
    assert _clamp_int(None, 60, 95, 78) == 78
    assert _clamp_int("高分", 60, 95, 78) == 78
    assert _clamp_int(float("nan"), 60, 95, 78) == 78
    assert _clamp_int(float("inf"), 60, 95, 78) == 78  # inf 无法钳制，回落默认


# ---------------------------------------------------------------------------
# _fallback（降级结构完整性）
# ---------------------------------------------------------------------------

def test_fallback_shape():
    fb = _fallback({})
    assert fb["ok"] is True
    assert len(fb["consensus"]) == 6
    assert len(fb["cards"]) == 4
    assert len(fb["advice"]) == 3
    # 本地规则未真正计算一致性，agreement 必须为 0 哨兵值
    assert all(c.get("agreement") == 0 for c in fb["consensus"])
    assert 3 <= len(fb["timeline"]) <= 4


# ---------------------------------------------------------------------------
# _normalize（核心契约守卫）
# ---------------------------------------------------------------------------

def test_normalize_enforces_fixed_consensus():
    raw = {"ok": True, "summary": "s", "advice": [], "outlook": "o", "consensus": [], "cards": []}
    out, patched = _normalize(raw)
    assert len(out["consensus"]) == 6
    labels = [c["label"] for c in out["consensus"]]
    assert labels == ["事业运", "财运", "感情运", "健康", "学业/成长", "人际/贵人"]
    # 空 consensus 触发全部修补
    assert any(p.startswith("consensus.") for p in patched)


def test_normalize_clamps_score_and_fills_defaults():
    raw = {
        "ok": True,
        "summary": "s",
        "outlook": "o",
        "consensus": [
            {"label": "事业运", "score": 200, "agreement": 120},
            {"label": "财运", "score": -5},
        ],
        "cards": [],
        "advice": [],
    }
    out, _ = _normalize(raw)
    by_label = {c["label"]: c for c in out["consensus"]}
    assert by_label["事业运"]["score"] == 95        # 越界上限
    assert by_label["事业运"]["agreement"] == 100     # agreement 上限
    assert by_label["财运"]["score"] == 60            # 越界下限
    assert by_label["财运"]["agreement"] == 0         # 缺失补 0


def test_normalize_cards_contract_names_fixed():
    # AI 改写卡片 name 必须被忽略，强制回退契约名（前端配色依赖 name 精确匹配）
    raw = {
        "ok": True, "summary": "s", "outlook": "o",
        "consensus": [],
        "cards": [{"icon": "X", "name": "乱写的名字", "score": "9", "desc": "y"}],
        "advice": [],
    }
    out, patched = _normalize(raw)
    names = [c["name"] for c in out["cards"]]
    assert names == ["近期趋势", "关键决策期", "风险提示", "天赋优势"]
    assert any(p.startswith("cards.") for p in patched)


def test_normalize_ok_false_passthrough():
    raw = {"ok": False, "reason": "排盘字段缺失"}
    out, patched = _normalize(raw)
    assert out is raw
    assert patched == []


def test_normalize_timeline_out_of_range_falls_back():
    raw = {
        "ok": True, "summary": "s", "outlook": "o",
        "consensus": [], "cards": [], "advice": [],
        "timeline": [{"period": "只一段", "overview": "x"}],  # 不足 3 段
    }
    out, patched = _normalize(raw)
    assert 3 <= len(out["timeline"]) <= 4
    assert any(p.startswith("timeline.count") for p in patched)


# ---------------------------------------------------------------------------
# interpret_summary_stream（事件契约，前端渐进渲染依赖）
# ---------------------------------------------------------------------------

def _fake_generate(*_args, **_kwargs):
    async def _gen():
        return (
            {
                "ok": True,
                "summary": "整体向好",
                "outlook": "近期宜稳",
                "consensus": [
                    {"label": "事业运", "score": 88, "agreement": 70},
                ],
                "cards": [{"icon": "📈", "name": "近期趋势", "score": "→ 升", "desc": "d"}],
                "advice": [{"title": "⚡ 立即行动", "items": ["做一事"]}],
                "timeline": [{"period": "当下", "overview": "夯实基础"}],
            },
            None,
            {"prompt": 10, "completion": 20},
            "mock-provider",
            "mock-model",
        )
    return _gen()


@pytest.fixture
def patched_summary(monkeypatch):
    """把 LLM / 缓存 / 预算 / 成本全部替换为无副作用桩，使测试不依赖外部。"""
    monkeypatch.setattr("src.services.summary.generate_structured", _fake_generate)
    monkeypatch.setattr("src.services.summary.cache_get", lambda _k: None)
    monkeypatch.setattr("src.services.summary.cache_set", lambda *_a, **_k: None)
    monkeypatch.setattr("src.services.summary.budget_broken", lambda: False)
    monkeypatch.setattr("src.services.summary.record_cost", lambda *_a, **_k: None)


def test_stream_event_sequence_and_shape(patched_summary):
    result = {"question": "", "modules": [{"module": "bazi", "name": "八字", "result": {}}]}
    events = asyncio.run(_collect(interpret_summary_stream(result, "t1")))

    names = [e["event"] for e in events]
    assert names == ["open", "init", "consensus", "cards", "advice", "summary", "meta", "done"]

    init = next(e["data"] for e in events if e["event"] == "init")
    assert init["ok"] is True

    consensus = next(e["data"] for e in events if e["event"] == "consensus")
    assert len(consensus) == 6  # 归一化补齐到 6

    cards = next(e["data"] for e in events if e["event"] == "cards")
    assert len(cards) == 4

    advice = next(e["data"] for e in events if e["event"] == "advice")
    assert len(advice) == 3

    summary = next(e["data"] for e in events if e["event"] == "summary")
    assert summary["summary"] == "整体向好"
    assert len(summary["timeline"]) == 3  # 单段输入被补齐到 3


def test_stream_ok_false_no_sections(patched_summary):
    async def _fail(*_a, **_k):
        return ({"ok": False, "reason": "排盘字段缺失"}, None, None, None, None)

    monkeypatch_fix = _fail

    result = {"question": "", "modules": []}
    # 临时覆盖 generate_structured 为失败桩
    import src.services.summary as sm
    orig = sm.generate_structured
    sm.generate_structured = monkeypatch_fix
    try:
        events = asyncio.run(_collect(sm.interpret_summary_stream(result, "t2")))
    finally:
        sm.generate_structured = orig

    names = [e["event"] for e in events]
    assert "consensus" not in names  # ok:false 不推送分段
    init = next(e["data"] for e in events if e["event"] == "init")
    assert init["ok"] is False
    assert init["reason"] == "排盘字段缺失"


async def _collect(gen):
    out = []
    async for evt in gen:
        # 模拟 route 把 data 透传给 EventSourceResponse 后，客户端 JSON.parse 的结果
        data = evt.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                pass
        out.append({"event": evt["event"], "data": data})
    return out
