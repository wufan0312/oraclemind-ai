"""P0 Boundary 单测：ai-py 强校验 + 重试回路 + LangGraph 接线 + integration 回灌 + backend tool-gateway。

运行（受管 venv 已装 jsonschema）：
    cd oraclemind-ai && PYTHONPATH=. python tests/test_harness_boundary.py
"""
import importlib.util
import os
import sys
import types

# ============================ ai-py harness ============================
from src.harness.boundary.validator import BoundaryValidator
from src.harness.boundary.schemas import MODULE_SCHEMAS
from src.harness.boundary.retry import validate_with_retry, RetryConfig
from src.harness.boundary.langgraph_nodes import make_boundary_nodes, BoundaryState
from src.harness.boundary.integration import generate_structured_with_boundary

GOOD_INTERPRET = {
    "ok": True,
    "summary": "整体运势平稳，事业有向上趋势。",
    "aspects": [{"title": "性格倾向", "text": "温和坚毅"}],
    "advice": ["保持节奏"],
    "outlook": "近期宜稳。",
}

GOOD_REPORT = {
    "ok": True,
    "summary": "多术数交叉显示事业向上。",
    "keyFindings": ["八字与紫微共振事业旺"],
    "divergences": [],
    "advice": [{"title": "立即行动", "items": ["深耕技能"]}],
    "outlook": "短期稳中有进。",
    "timeline": [{"period": "2026 秋", "overview": "夯实基础"}],
    "consensus": [{"label": "事业运", "score": 85, "reason": "八字官星得令"}],
    "cards": [{"icon": "📈", "name": "近期趋势", "score": "平稳", "desc": "..."}],
}


def test_registry_covers_modules():
    v = BoundaryValidator()
    for m in [
        "bazi", "ziwei", "liuyao", "meihua", "qimen", "liuren", "taiyi",
        "numerology", "tarot", "dream", "fengshui", "angel", "report",
    ]:
        assert m in v.modules, f"缺模块/契约 {m}"


def test_valid_interpret_passes():
    v = BoundaryValidator()
    assert v.validate("bazi", GOOD_INTERPRET).ok


def test_invalid_interpret_missing_summary():
    v = BoundaryValidator()
    bad = dict(GOOD_INTERPRET)
    del bad["summary"]
    res = v.validate("bazi", bad)
    assert not res.ok
    assert "summary" in res.human_message()


def test_report_consensus_score_type():
    v = BoundaryValidator()
    bad = dict(GOOD_REPORT)
    bad["consensus"] = [{"label": "事业运", "score": "高"}]  # score 必须是 number
    res = v.validate("report", bad)
    assert not res.ok


def test_unknown_contract_rejected():
    v = BoundaryValidator()
    res = v.validate("not_real", {"summary": "x"})
    assert not res.ok
    assert "未注册" in res.human_message()


def test_retry_loop_fixes_output():
    v = BoundaryValidator()
    calls = {"n": 0}

    def on_invalid(result, attempt):
        calls["n"] += 1
        return GOOD_INTERPRET

    bad = dict(GOOD_INTERPRET)
    del bad["summary"]
    res, attempts = validate_with_retry(v, "bazi", bad, on_invalid=on_invalid)
    assert res.ok
    assert attempts == 1


def test_retry_exhausted_returns_failure():
    v = BoundaryValidator()

    def on_invalid(result, attempt):
        b = dict(GOOD_INTERPRET)
        del b["summary"]  # 故意持续返回无效
        return b

    bad = dict(GOOD_INTERPRET)
    del bad["summary"]
    res, attempts = validate_with_retry(
        v, "bazi", bad, on_invalid=on_invalid, config=RetryConfig(max_retries=2)
    )
    assert not res.ok
    assert attempts == 2


def test_langgraph_nodes_wiring():
    v = BoundaryValidator()
    check, route, _ = make_boundary_nodes(v, max_retries=2)

    s1: BoundaryState = {"module": "bazi", "raw_output": GOOD_INTERPRET, "retries_left": 2}
    out1 = check(s1)
    assert route(out1) == "valid"

    bad = dict(GOOD_INTERPRET)
    del bad["summary"]
    s2: BoundaryState = {"module": "bazi", "raw_output": bad, "retries_left": 2}
    out2 = check(s2)
    assert route(out2) == "retry"
    assert out2["retries_left"] == 1

    s3: BoundaryState = {"module": "bazi", "raw_output": bad, "retries_left": 1}
    out3 = check(s3)
    assert route(out3) == "degraded"


def test_integration_retry_on_boundary_fail():
    """验证 generate_structured_with_boundary 在契约不通过时回灌重试一次。"""
    fake = types.ModuleType("src.services.structured")
    call = {"n": 0}

    async def fake_generate(system, user, **kw):
        call["n"] += 1
        if call["n"] == 1:
            # 首轮返回不符合契约（缺 summary），触发 Boundary 回灌
            return ({"ok": True}, None, {}, "p", "m")
        return (GOOD_INTERPRET, None, {}, "p", "m")

    fake.generate_structured = fake_generate
    svc = types.ModuleType("src.services")
    svc.structured = fake
    sys.modules["src.services"] = svc
    sys.modules["src.services.structured"] = fake
    try:
        import asyncio

        parsed, err, degraded = asyncio.run(
            generate_structured_with_boundary("bazi", "sys", "usr", max_boundary_retries=1)
        )
        assert parsed is not None and not degraded
        assert call["n"] == 2  # 首轮无效 + Boundary 回灌一次
    finally:
        sys.modules.pop("src.services.structured", None)
        sys.modules.pop("src.services", None)


# ============================ backend tool-gateway ============================
def _load_backend_gateway():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.normpath(
        os.path.join(here, "..", "..", "oraclemind-backend", "app", "harness", "tool_gateway.py")
    )
    spec = importlib.util.spec_from_file_location("bk_tool_gateway", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bk_tool_gateway"] = mod  # 注册后 dataclass 才能解析 __module__
    spec.loader.exec_module(mod)
    return mod


def test_backend_gateway_blocks_unregistered():
    gw_mod = _load_backend_gateway()
    gw = gw_mod.ToolGateway()
    try:
        gw.dispatch("evil", {"x": 1})
        raise AssertionError("应拒绝未注册工具")
    except gw_mod.ToolGatewayError:
        pass


def test_backend_gateway_validates_params():
    gw_mod = _load_backend_gateway()
    gw = gw_mod.ToolGateway()
    gw.register(
        gw_mod.ToolSpec(
            name="bazi_calc",
            handler=lambda birth: f"calc:{birth}",
            param_schema={"type": "object", "properties": {"birth": {"type": "string"}}, "required": ["birth"]},
        )
    )
    try:
        gw.dispatch("bazi_calc", {})
        raise AssertionError("缺参应被拦截")
    except gw_mod.ToolGatewayError as e:
        assert "入参校验失败" in str(e)
    assert gw.dispatch("bazi_calc", {"birth": "1990-01-01"}) == "calc:1990-01-01"


def test_backend_gateway_requires_approval():
    gw_mod = _load_backend_gateway()
    gw = gw_mod.ToolGateway()
    gw.register(
        gw_mod.ToolSpec(
            name="paid_report",
            handler=lambda user_id: f"paid:{user_id}",
            param_schema={"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]},
            risk="high",
            requires_approval=True,
        )
    )
    try:
        gw.dispatch("paid_report", {"user_id": "u1"})
        raise AssertionError("未审批应被拦截")
    except gw_mod.ToolGatewayError as e:
        assert "approval" in str(e)
    assert gw.dispatch("paid_report", {"user_id": "u1"}, approved=True) == "paid:u1"


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"ALL PASS ({len(fns)} tests)")
