"""
玄镜 Harness Boundary —— LangGraph 集成
落点：oraclemind-ai/src/harness/boundary/langgraph_nodes.py

把 Boundary 校验接成编排回路：
    generate(生成节点) -> boundary_check -> route_boundary
        route "valid"     -> 下游 / END
        route "degraded"  -> degraded_output（降级输出，不阻断主流程）
        route "retry"     -> generate（带 feedback 回灌）

langgraph 延迟导入，核心 validator 不依赖它；本文件仅依赖 typing。

示例接线（home_agent / ming_agent 等 LangGraph ReAct 场景）：
    from langgraph.graph import StateGraph, END
    from src.harness.boundary.langgraph_nodes import make_boundary_nodes

    check, route, _ = make_boundary_nodes(validator, max_retries=2)

    g = StateGraph(BoundaryState)
    g.add_node("generate", generate_node)      # 读取 state["feedback"] 追加到 prompt
    g.add_node("boundary_check", check)
    g.add_node("degraded_output", degraded_node)
    g.set_entry_point("generate")
    g.add_edge("generate", "boundary_check")
    g.add_conditional_edges("boundary_check", route,
                            {"valid": END, "degraded": "degraded_output", "retry": "generate"})
    app = g.compile()
"""
from typing import TypedDict, Optional, Callable, Any, Tuple


class BoundaryState(TypedDict, total=False):
    module: str
    raw_output: dict                 # 模型本轮结构化输出
    validation: dict                 # BoundaryResult.to_dict()
    retries_left: int                # 剩余重试次数
    degraded: bool                   # 是否已进入降级
    feedback: str                    # 回灌给 generate 的错误信息


def make_boundary_nodes(validator, max_retries: int = 2) -> Tuple[Callable, Callable, Callable]:
    """返回一个 (boundary_check, route_boundary, consume_retry) 三元组。"""

    def boundary_check(state: BoundaryState) -> dict:
        module = state.get("module", "")
        data = state.get("raw_output", {}) or {}
        res = validator.validate(module, data)
        incoming = state.get("retries_left", max_retries)
        if res.ok:
            return {"validation": res.to_dict(), "feedback": "", "degraded": False}
        # 无效：消耗一次重试配额
        new_left = max(0, incoming - 1)
        return {
            "validation": res.to_dict(),
            "retries_left": new_left,
            "feedback": res.human_message(),
            "degraded": new_left <= 0,
        }

    def route_boundary(state: BoundaryState) -> str:
        v = state.get("validation", {})
        if v.get("ok"):
            return "valid"
        if state.get("degraded"):
            return "degraded"
        return "retry"

    def consume_retry(state: BoundaryState) -> dict:
        return {"retries_left": max(0, state.get("retries_left", max_retries) - 1)}

    return boundary_check, route_boundary, consume_retry
