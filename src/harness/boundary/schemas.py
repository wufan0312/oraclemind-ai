"""
玄镜 OracleMind · Harness Boundary 层：AI 结构化输出 JSON Schema 契约
落点：oraclemind-ai/src/harness/boundary/schemas.py

设计依据（对齐真实业务，而非拍脑袋字段）：
- 单模块解读输出契约 = src/prompts/shared.OUTPUT_FORMAT
    { ok, summary, aspects:[{title,text}], advice:[str], outlook }
  该契约被 bazi/ziwei/liuyao/meihua/qimen/liuren/taiyi/numerology/tarot/dream/fengshui/angel
  等所有单模块解读 prompt 共用（见 src/prompts/registry.py）。
- 综合报告 writer 契约 = src/agent/report_agent.py 的 _REPORT_WRITER_PROMPT
    { ok, summary, keyFindings:[], divergences:[{desc,modules,resolution}],
      advice:[{title,items:[]}], outlook, timeline:[{period,overview}],
      consensus:[{label,score,reason}], cards:[{icon,name,score,desc}] }

校验策略：required 仅放关键字段（ok/summary），其余 properties 宽松 +
additionalProperties=true，避免误杀模型合理变化（ai-py 的 model 是 GLM-4-Flash，
格式偶发波动，且现有 textDedup 已处理孤立编号等残留）。
"""
from typing import Any, Dict

# ----------------------- 单模块解读统一契约（OUTPUT_FORMAT） -----------------------
INTERPRET_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean", "description": "是否成功解读"},
        "module": {"type": "string", "description": "模块标识（可选，部分 prompt 会回显）"},
        "summary": {"type": "string", "minLength": 1, "description": "整体概述，≤80字"},
        "aspects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "minLength": 1},
                    "text": {"type": "string"},
                },
                "required": ["title"],
            },
        },
        "advice": {"type": "array", "items": {"type": "string"}},
        "outlook": {"type": "string"},
        "disclaimer": {"type": "string"},
    },
    "required": ["ok", "summary"],
    "additionalProperties": True,
}

# ----------------------- 综合报告 writer 契约 -----------------------
REPORT_WRITER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "summary": {"type": "string", "minLength": 1, "description": "200-300字白话概述"},
        "keyFindings": {"type": "array", "items": {"type": "string"}},
        "divergences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "desc": {"type": "string"},
                    "modules": {"type": "array", "items": {"type": "string"}},
                    "resolution": {"type": "string"},
                },
                "required": ["desc"],
            },
        },
        "advice": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "minLength": 1},
                    "items": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title"],
            },
        },
        "outlook": {"type": "string"},
        "timeline": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "period": {"type": "string", "minLength": 1},
                    "overview": {"type": "string"},
                },
                "required": ["period"],
            },
        },
        "consensus": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "minLength": 1},
                    "score": {"type": "number", "minimum": 0, "maximum": 100},
                    "reason": {"type": "string"},
                },
                "required": ["label", "score"],
            },
        },
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "icon": {"type": "string"},
                    "name": {"type": "string"},
                    "score": {"type": "string"},
                    "desc": {"type": "string"},
                },
                "required": ["name"],
            },
        },
    },
    "required": ["summary"],
    "additionalProperties": True,
}

# 12 个解读模块（见 src/prompts/registry.py）默认都复用统一契约；
# 若某模块未来有特化输出（如 tarot 返回 cards 数组），在此 override 对应键即可。
_INTERPRET_MODULES = [
    "bazi", "ziwei", "liuyao", "meihua", "qimen", "liuren", "taiyi",
    "numerology", "tarot", "dream", "fengshui", "angel",
]

MODULE_SCHEMAS: Dict[str, Dict[str, Any]] = {
    **{m: INTERPRET_OUTPUT_SCHEMA for m in _INTERPRET_MODULES},
    "report": REPORT_WRITER_SCHEMA,
}

# 可选：backend 排盘结果契约（确定性数据）。
# 默认不接入 BoundaryValidator.validate()，以免误杀合法排盘；
# 如需要对排盘入参/出参做契约校验，可在此补充并按需 import。
PAIPAN_SCHEMAS: Dict[str, Dict[str, Any]] = {}
