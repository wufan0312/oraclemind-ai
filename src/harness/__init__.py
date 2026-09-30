"""玄镜 OracleMind · Harness 工程化层（Boundary 强校验等）。

对齐「玄镜 Harness 化改造清单」P0：把裸模型调用装进可治理的系统。
- boundary: 结构化输出强校验 + 重试回路 + LangGraph 接线
- boundary.integration: 包装 src.services.structured.generate_structured 的薄封装
"""
from src.harness.boundary.validator import BoundaryValidator
from src.harness.boundary.schemas import (
    MODULE_SCHEMAS,
    INTERPRET_OUTPUT_SCHEMA,
    REPORT_WRITER_SCHEMA,
)
from src.harness.observability import install_observability, route_trace

__all__ = [
    "BoundaryValidator",
    "MODULE_SCHEMAS",
    "INTERPRET_OUTPUT_SCHEMA",
    "REPORT_WRITER_SCHEMA",
    "install_observability",
    "route_trace",
]
