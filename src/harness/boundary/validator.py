"""
玄镜 Harness Boundary —— 结构化强校验器
落点：oraclemind-ai/src/harness/boundary/validator.py
依赖：jsonschema (Draft7)
"""
from typing import Dict, List, Optional

from jsonschema import Draft7Validator

from .schemas import MODULE_SCHEMAS


class BoundaryViolation:
    """单条校验违例（路径 + 原因）。"""

    __slots__ = ("path", "message", "validator", "schema_path")

    def __init__(self, path, message, validator=None, schema_path=None):
        self.path = path
        self.message = message
        self.validator = validator
        self.schema_path = schema_path

    def __str__(self) -> str:
        loc = "/".join(str(p) for p in self.path) if self.path else "(root)"
        return f"[{loc}] {self.message}"


class BoundaryResult:
    """一次校验的结论，可直接序列化为 dict 或压成回灌 prompt 的反馈。"""

    def __init__(self, module: str, ok: bool, violations: List[BoundaryViolation]):
        self.module = module
        self.ok = ok
        self.violations = violations

    def __bool__(self) -> bool:
        return self.ok

    def to_dict(self) -> dict:
        return {
            "module": self.module,
            "ok": self.ok,
            "violations": [
                {
                    "path": list(v.path),
                    "message": v.message,
                    "validator": v.validator,
                }
                for v in self.violations
            ],
        }

    def human_message(self, max_errors: int = 5) -> str:
        """把错误压成可回灌 prompt 的简短反馈（给模型二次生成用）。"""
        if self.ok:
            return ""
        lines = [f"你返回的结构不符合 {self.module} 输出契约，请按契约修正后重新输出 JSON："]
        for v in self.violations[:max_errors]:
            loc = "/".join(map(str, v.path)) or "根"
            lines.append(f"- 字段 {loc}: {v.message}")
        if len(self.violations) > max_errors:
            lines.append(f"- …还有 {len(self.violations) - max_errors} 处问题")
        return "\n".join(lines)


class BoundaryValidator:
    """模块/契约 → JSON Schema 注册表 + 强校验。"""

    def __init__(self, schemas: Optional[Dict[str, dict]] = None):
        self._schemas: Dict[str, dict] = schemas if schemas is not None else MODULE_SCHEMAS
        self._validators: Dict[str, Draft7Validator] = {
            k: Draft7Validator(v) for k, v in self._schemas.items()
        }

    @property
    def modules(self) -> List[str]:
        return list(self._validators.keys())

    def schema_for(self, module: str) -> dict:
        """返回某模块/契约 Schema（供注入 system prompt / 工具描述）。"""
        try:
            return self._schemas[module]
        except KeyError:
            raise KeyError(f"未注册模块/契约: {module!r}，已注册: {self.modules}")

    def validate(self, module: str, data) -> BoundaryResult:
        if not isinstance(data, dict):
            return BoundaryResult(module, False, [BoundaryViolation([], "输入不是 JSON 对象")])
        if module not in self._validators:
            return BoundaryResult(module, False, [BoundaryViolation([], f"未注册模块/契约 {module!r}")])
        violations: List[BoundaryViolation] = []
        # 用字符串 key 排序，避免 int/str 路径混排导致 sort 抛 TypeError
        errors = sorted(
            self._validators[module].iter_errors(data),
            key=lambda e: "/".join(map(str, e.absolute_path)),
        )
        for err in errors:
            violations.append(
                BoundaryViolation(
                    list(err.absolute_path),
                    err.message,
                    err.validator,
                    list(err.absolute_schema_path),
                )
            )
        return BoundaryResult(module, len(violations) == 0, violations)
