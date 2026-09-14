"""玄镜 OracleMind · Prompt 模板注册表

对应 TS 版 src/prompts/index.ts
"""

from __future__ import annotations

from src.prompts.shared import PromptTemplate
from src.prompts.bazi import bazi_prompt, bazi_refs_explain_prompt
from src.prompts.ziwei import ziwei_prompt
from src.prompts.liuyao import liuyao_prompt
from src.prompts.meihua import meihua_prompt
from src.prompts.qimen import qimen_prompt
from src.prompts.liuren import liuren_prompt
from src.prompts.taiyi import taiyi_prompt
from src.prompts.numerology import numerology_prompt
from src.prompts.tarot import tarot_prompt
from src.prompts.dream import dream_prompt
from src.prompts.fengshui import fengshui_prompt
from src.prompts.angel import angel_prompt

_REGISTRY: dict[str, PromptTemplate] = {
    "bazi": bazi_prompt,
    "ziwei": ziwei_prompt,
    "liuyao": liuyao_prompt,
    "meihua": meihua_prompt,
    "qimen": qimen_prompt,
    # 三式补齐（P2-1 / P2-2）
    "liuren": liuren_prompt,
    "taiyi": taiyi_prompt,
    "numerology": numerology_prompt,
    "tarot": tarot_prompt,
    "dream": dream_prompt,
    "fengshui": fengshui_prompt,
    # 天使数字（P2-3）
    "angel": angel_prompt,
}


def get_prompt(module: str) -> PromptTemplate:
    tpl = _REGISTRY.get(module)
    if not tpl:
        raise ValueError(f"不支持的解读模块: {module}")
    return tpl


def get_refs_explain_prompt(module: str) -> PromptTemplate:
    """获取「命理原文白话解读」Prompt（focus='refs'）"""
    if module == "bazi":
        return bazi_refs_explain_prompt
    return get_prompt(module)


def list_modules() -> list[str]:
    return list(_REGISTRY.keys())
