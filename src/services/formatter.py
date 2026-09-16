"""玄镜 OracleMind · JSON → Markdown 格式化器

解析 LLM 返回的 JSON 结构，转换为前端可渲染的 Markdown 文本。
永远不返回原始 JSON —— 即使解析完全失败也会尝试启发式格式化。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from src.services.json_utils import (
    close_truncated_json as _close_truncated_json,
    repair_json as _try_repair_json,
)

logger = logging.getLogger(__name__)

# 梅花易数专用的 aspect 标题映射
MEIHUA_TITLES = {"体用生克", "互卦参断", "变卦趋向", "吉凶判断", "体用", "互卦", "变卦", "吉凶"}

# 检测输出是否仍然像原始 JSON 的特征
_JSON_SIGNATURE_PATTERNS = [
    re.compile(r'^\s*\{'),  # 以 { 开头
    re.compile(r'^\s*\['),  # 以 [ 开头
    re.compile(r'"ok"\s*:\s*(true|false)', re.IGNORECASE),
    re.compile(r'"aspects"\s*:'),
    re.compile(r'"summary"\s*:'),
    re.compile(r'"advice"\s*:'),
    re.compile(r'"outlook"\s*:'),
    re.compile(r'"title"\s*:\s*"'),
    re.compile(r'"text"\s*:\s*"'),
]


def format_interpret_json(raw: str, module: str | None = None, focus: str | None = None) -> str:
    """将 LLM 返回的 JSON 解读结果格式化为 Markdown

    Args:
        raw: LLM 返回的原始文本（预期为 JSON）
        module: 模块名（如 'meihua', 'bazi'），用于处理模块特有字段
        focus: 子焦点（如紫微的 'sihua' / 'stars'），用于按板块决定是否渲染特定段落

    Returns:
        Markdown 格式文本（永远不返回原始 JSON）
    """
    if not raw or not raw.strip():
        return "（AI 暂无解读）"

    # 清理 markdown 代码块标记
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

    # 尝试提取 JSON
    json_str = _extract_json(cleaned)
    data = None
    parse_success = False

    if json_str is not None:
        # JSON 提取成功 → 尝试解析
        try:
            data = json.loads(json_str)
            parse_success = True
        except json.JSONDecodeError:
            # 尝试修复后再解析
            repaired = _try_repair_json(json_str)
            if repaired is not None:
                try:
                    data = json.loads(repaired)
                    parse_success = True
                except json.JSONDecodeError as e2:
                    logger.warning(f"[{module}] JSON 修复后仍解析失败: {e2}")
            else:
                logger.warning(f"[{module}] JSON 解析失败且修复无效")
    else:
        # 无法提取 JSON → 检查是否像 JSON
        if cleaned.lstrip().startswith(("{", "[")):
            # 先走常规修复；仍失败则按「被 max_tokens 截断」补全闭合后再试
            repaired = _try_repair_json(cleaned)
            if repaired is None:
                repaired = _close_truncated_json(cleaned)
            if repaired is not None:
                try:
                    data = json.loads(repaired)
                    parse_success = True
                except json.JSONDecodeError:
                    pass

    # 如果 JSON 解析成功
    if parse_success and data is not None:
        # 非 dict → 通用格式化
        if not isinstance(data, dict):
            result = _format_generic_json(data)
            return _final_safety_check(result, cleaned, module)

        # 检查 ok 字段
        if data.get("ok") is False:
            reason = data.get("reason", "解读失败")
            return f"⚠️ **解读不可用**\n\n{reason}"

        parts: list[str] = []

        # 0. 塔罗结论（宜 / 中性 / 不宜 + 能量分）——放在最前面，先给结论。
        # 用户最高频的问题是「该不该 / 要不要」，此前六段全是描述、没有倾向，体感「不准」。
        # 输出格式固定为「**倾向**：X　**能量分**：N/100」，前端据此渲染结论卡（提取失败则降级为普通文本）。
        if module == "tarot":
            verdict_part = _format_tarot_verdict(data)
            if verdict_part:
                parts.append(verdict_part)

        # 1. 概述
        summary = data.get("summary", "")
        if summary:
            parts.append(f"**整体概述**\n\n{_normalize_field_value(summary)}")

        # 2. 各维度 —— 兼容多种格式
        aspects = data.get("aspects", [])
        if aspects and isinstance(aspects, list):
            parts.extend(_format_aspects_list(aspects))

        # 梅花易数专用字段兼容
        if module == "meihua":
            meihua_parts = _format_meihua_specific(data)
            if meihua_parts:
                parts.extend(meihua_parts)

        # 塔罗专用字段：逐牌解读 / 牌阵主线 / 时间窗口 / 风险提醒
        if module == "tarot":
            tarot_parts = _format_tarot_specific(data)
            if tarot_parts:
                parts.extend(tarot_parts)

        # 3. 建议（紫微·四化飞星/十四主星 两个子板块不展示建设性建议）
        advice = data.get("advice", [])
        if advice and isinstance(advice, list) and focus not in ("sihua", "stars"):
            advice_lines = []
            for a in advice:
                if a:
                    normalized = _normalize_field_value(str(a))
                    for line in normalized.split("\n\n"):
                        line = line.strip()
                        if line:
                            advice_lines.append(f"- {line}")
            if advice_lines:
                parts.append(f"**建设性建议**\n\n" + "\n".join(advice_lines))

        # 4. 近期趋势（紫微·四化飞星/十四主星 两个子板块不展示近期趋势）
        outlook = data.get("outlook", "")
        if outlook and focus not in ("sihua", "stars"):
            parts.append(f"**近期趋势**\n\n{_normalize_field_value(outlook)}")

        # 5. 兜底：如果没有识别到任何标准字段，尝试通用格式化
        if not parts:
            logger.warning(f"[{module}] 未识别到标准字段，尝试通用 JSON 格式化")
            generic = _format_known_fields(data)
            if generic:
                parts.extend(generic)

        if not parts:
            # 最后兜底：遍历所有 key-value
            logger.warning(f"[{module}] 格式化后无内容，遍历所有字段")
            for k, v in data.items():
                if k in ("ok", "reason", "module", "focus", "version"):
                    continue
                if v:
                    sv = _normalize_field_value(v) if isinstance(v, (str, list, dict)) else str(v)
                    if sv and sv.strip():
                        parts.append(f"**{_key_label(k)}**\n\n{sv}")

        if not parts:
            logger.warning(f"[{module}] 全部格式化失败，返回启发式结果")
            result = _heuristic_format(cleaned)
        else:
            result = "\n\n".join(parts)

        return _final_safety_check(result, cleaned, module)

    # JSON 完全无法解析 → 启发式格式化
    # 附上原始输出片段，便于定位是截断、引号转义还是别的形态导致解析失败
    logger.warning(f"[{module}] JSON 完全无法解析，使用启发式格式化; raw[:300]={cleaned[:300]!r}")
    result = _heuristic_format(cleaned)
    return _final_safety_check(result, cleaned, module)


_SYMBOL_ONLY_LINE = re.compile(r"^[ \t]*['\"\[\]{}(),]['\"\[\]{}(), \t]*$", re.M)
_BARE_KEY_LINE = re.compile(
    r"^[ \t]*\*{0,2}(?:outlook|summary|advice|consensus|cards|timeline|key_?findings|divergences|reason)\*{0,2}[ \t]*[:：]?[ \t]*$",
    re.I | re.M,
)


# 模型偶发把 JSON 字段名当成 aspect 标题输出（如 title="title"），这些英文词在任何中文语境里
# 都不应作为自然语言标题出现；渲染前识别并跳过/清洗。
_RESERVED_TITLE_TOKENS = frozenset({
    "title", "text", "summary", "advice", "outlook", "consensus", "cards", "timeline",
    "key findings", "key_findings", "divergence", "divergences", "reason", "score",
    "label", "name", "type", "content", "ok", "module", "focus", "version", "items",
})


def _is_reserved_title(title: Any) -> bool:
    """判断标题是否为字段名占位符（忽略大小写、首尾空格、加粗标记）"""
    if not isinstance(title, str):
        return False
    t = title.strip().lower().strip('*')
    return t in _RESERVED_TITLE_TOKENS


def _strip_structural_residue(text: str) -> str:
    """清除「markdown 为主体、局部混入 JSON/字段名残留」的碎片。

    _looks_like_raw_json 只能拦截整体就是 JSON 的输出；当模型输出 markdown 为主、
    仅局部粘连 '}/'] /字面 \\n / **Text**: 字段名时（用户可见的「Text: / Title: / ']」），
    由本函数兜底摘除。规则均为窄匹配：只匹配英文键名标签、JSON 对象、引号紧邻括号等
    正常中文正文不可能出现的形态，不会误伤内容。前端 markdown.ts 的 sanitizeAiText
    保持同一套规则，两处必须同步修改。
    """
    if not text:
        return text
    s = text
    # ① 字面转义符 → 真换行（模型把 \n 当正文输出时，段落/列表粘成一行）
    s = s.replace('\\r\\n', '\n').replace('\\n', '\n').replace('\\t', '  ')
    # ② Python repr 风格 '中文短语' → 「中文短语」（要求中文开头，英文缩写不受影响）
    s = re.sub(r"'([\u4e00-\u9fff][^'\n]{0,60}?)'", r'「\1」', s)
    # ③ 字段名标签（多行；可选列表符 / 加粗标记包裹）：**Text**: / - **Title**: /
    #    **Outlook**: 等 → 剥掉标签、保留该行剩余正文（如「格局印证」）
    s = re.sub(
        r'(?m)^[ \t]*(?:[-*]\s+)?\*{0,2}(?:text|title|outlook|summary|advice|consensus|'
        r'cards|timeline|key[ _]?findings|divergences?|reason|score|label|name|type|content)'
        r'\*{0,2}\s*[:：]\s*',
        '', s, flags=re.I)
    # ③.5 独立成行的字段名占位符：模型偶发把 title 字段值写成 "title"，导致整行只剩
    # "title" / "**title**" / "- title"；正常中文正文不可能整行只有这些英文词，直接删除。
    s = re.sub(
        r'(?m)^[ \t]*(?:[-*]\s+)?\*{0,2}(?:text|title|outlook|summary|advice|consensus|'
        r'cards|timeline|key[ _]?findings|divergences?|reason|score|label|name|type|content)'
        r'\*{0,2}[ \t]*$',
        '', s, flags=re.I)
    # ④ JSON 风格对象 {...}（如 {"score": 85}）→ 删除
    s = re.sub(r'\{[^{}]*\}', '', s)
    # ⑤ 纯符号行（仅含引号/括号/逗号/空白）→ 删除
    s = _SYMBOL_ONLY_LINE.sub('', s)
    # ⑥ 引号/括号粘连残片（如 '] / '], / "}, ）→ 删除
    s = re.sub(r"['\"\]\}][ \t]*,?[ \t]*['\"\]\}]*", '', s)
    # ⑦ 多余空行收敛
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()


def _final_safety_check(result: str, original: str, module: str | None) -> str:
    """最终安全检查：确保输出不包含原始 JSON 代码

    如果结果仍然看起来像 JSON，尝试用 _heuristic_format 重新处理。
    无论走哪条分支，最后都过一遍 _strip_structural_residue 摘除局部残留。
    """
    if not result or not result.strip():
        return "（AI 解读格式异常，请稍后重试）"

    # 检查结果是否仍然像原始 JSON
    if _looks_like_raw_json(result):
        logger.warning(f"[{module}] 输出检测为原始 JSON，重新格式化")
        # 尝试用启发式格式化重新处理原始输入
        result2 = _heuristic_format(original)
        if result2 and result2.strip() and not _looks_like_raw_json(result2):
            return _strip_structural_residue(result2)
        # 如果仍然不行，返回清理后的安全文本
        return _strip_structural_residue(_safe_cleanup(result))

    return _strip_structural_residue(result)


def _looks_like_raw_json(text: str) -> bool:
    """检测文本是否看起来像原始 JSON 代码"""
    stripped = text.strip()
    if not stripped:
        return False

    # 检查是否以 { 或 [ 开头
    if stripped.startswith("{") or stripped.startswith("["):
        # 进一步验证：是否包含 JSON 特征
        json_features = 0
        if re.search(r'"[^"]+"\s*:', stripped):
            json_features += 1
        if re.search(r'\{[^}]*"[^"]*"', stripped):
            json_features += 1
        if re.search(r'\[[^\]]*\]', stripped):
            json_features += 1
        # 同时包含 { 和 }，且至少有一个 JSON 特征
        if "{" in stripped and "}" in stripped and json_features >= 1:
            return True

    # 检查是否包含多个 JSON 特征键
    feature_count = 0
    for pattern in _JSON_SIGNATURE_PATTERNS:
        if pattern.search(stripped):
            feature_count += 1
    if feature_count >= 2:
        return True

    # 检查是否有大量转义字符和引号（可能是 JSON 字符串）
    if stripped.count('\\"') > 3 and stripped.count('"') > 5:
        return True

    return False


def _safe_cleanup(text: str) -> str:
    """安全清理：从可能的 JSON 中提取可读文本"""
    # 尝试用 _heuristic_format 清理
    result = _heuristic_format(text)
    if result and result.strip() and not _looks_like_raw_json(result):
        return result

    # 最后兜底：移除 JSON 结构字符，保留可读内容
    cleaned = re.sub(r'[{}\[\]]+', ' ', text)
    cleaned = re.sub(r'"([^"]*)"\s*:\s*"([^"]*)"', r'\1: \2', cleaned)
    cleaned = re.sub(r'"', '', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    if cleaned and len(cleaned) > 10:
        return cleaned

    return "（AI 解读格式异常，请稍后重试）"


def _format_aspects_list(aspects: list) -> list[str]:
    """格式化 aspects 列表，兼容多种元素类型"""
    parts: list[str] = []
    for item in aspects:
        if isinstance(item, dict):
            # 提取 title + text
            title = item.get("title", "")
            text = item.get("text", "")
            if title and text and not _is_reserved_title(title):
                formatted_text = _normalize_field_value(text)
                parts.append(f"**{title}**\n\n{formatted_text}")
            elif text:
                # title 是字段名占位符或缺失时，退化为无标题正文
                parts.append(_normalize_field_value(text))
            else:
                # dict 有其他结构，尝试通用格式化；键若为字段名占位符也跳过
                for k, v in item.items():
                    if _is_reserved_title(k):
                        continue
                    val_str = _normalize_field_value(v) if v else ""
                    if val_str:
                        parts.append(f"**{k}**\n\n{val_str}")
        elif isinstance(item, str):
            # 纯字符串 item
            stripped = item.strip()
            if stripped:
                # 尝试解析为 JSON
                if stripped.startswith("{") or stripped.startswith("["):
                    try:
                        parsed = json.loads(stripped)
                        if isinstance(parsed, dict):
                            title = parsed.get("title", "")
                            text = parsed.get("text", "")
                            if title and text and not _is_reserved_title(title):
                                parts.append(f"**{title}**\n\n{_normalize_field_value(text)}")
                                continue
                            elif text:
                                parts.append(_normalize_field_value(text))
                                continue
                        elif isinstance(parsed, list):
                            for sub in parsed:
                                if isinstance(sub, dict):
                                    title = sub.get("title", "")
                                    text = sub.get("text", "")
                                    if title and text and not _is_reserved_title(title):
                                        parts.append(f"**{title}**\n\n{_normalize_field_value(text)}")
                                        continue
                                    elif text:
                                        parts.append(_normalize_field_value(text))
                                        continue
                    except json.JSONDecodeError:
                        # JSON 解析失败 → 尝试修复后再解析
                        repaired = _try_repair_json(stripped)
                        if repaired:
                            try:
                                parsed = json.loads(repaired)
                                if isinstance(parsed, dict):
                                    title = parsed.get("title", "")
                                    text = parsed.get("text", "")
                                    if title and text and not _is_reserved_title(title):
                                        parts.append(f"**{title}**\n\n{_normalize_field_value(text)}")
                                        continue
                                    elif text:
                                        parts.append(_normalize_field_value(text))
                                        continue
                            except json.JSONDecodeError:
                                pass
                        # 仍然失败 → 清理 JSON 字符后返回
                        cleaned = _clean_json_string(stripped)
                        if cleaned and cleaned.strip():
                            parts.append(cleaned)
                        continue
                parts.append(stripped)
        elif isinstance(item, list):
            # 嵌套列表
            for sub in item:
                if isinstance(sub, dict):
                    title = sub.get("title", "")
                    text = sub.get("text", "")
                    if title and text and not _is_reserved_title(title):
                        parts.append(f"**{title}**\n\n{_normalize_field_value(text)}")
                    elif text:
                        parts.append(_normalize_field_value(text))
                elif isinstance(sub, str):
                    cleaned = _clean_json_string(sub)
                    if cleaned:
                        parts.append(cleaned)
        else:
            # 其他类型
            s = str(item).strip()
            if s:
                parts.append(s)
    return parts


def _clean_json_string(text: str) -> str:
    """清理可能包含 JSON 结构的字符串，提取可读内容"""
    if not text:
        return ""
    stripped = text.strip()

    # 尝试修复并解析
    repaired = _try_repair_json(stripped)
    if repaired:
        try:
            parsed = json.loads(repaired)
            if isinstance(parsed, dict):
                title = parsed.get("title", "")
                text_val = parsed.get("text", "")
                if title and text_val:
                    return f"**{title}**：{text_val}"
                # 尝试通用格式化
                return _format_complex_value(parsed)
            elif isinstance(parsed, list):
                return _format_complex_value(parsed)
        except json.JSONDecodeError:
            pass

    # 无法解析 → 清理 JSON 结构字符
    # 移除 { } [ ] " 和转义字符
    cleaned = re.sub(r'[{}\[\]]+', ' ', stripped)
    cleaned = re.sub(r'\\"', '"', cleaned)
    cleaned = re.sub(r'"([^"]*)"\s*:\s*"([^"]*)"', r'\1: \2', cleaned)
    cleaned = re.sub(r'"', '', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def _format_meihua_specific(data: dict) -> list[str]:
    """梅花易数专用字段格式化"""
    parts: list[str] = []
    meihua_fields = {
        "体用生克": "体用生克",
        "互卦参断": "互卦参断",
        "变卦趋向": "变卦趋向",
        "吉凶判断": "吉凶判断",
        "体用": "体用分析",
        "互卦": "互卦分析",
        "变卦": "变卦分析",
        "吉凶": "吉凶分析",
    }
    for key, label in meihua_fields.items():
        if key in data and key != "aspects":
            val = data[key]
            if val and isinstance(val, (str, dict, list)):
                formatted = _normalize_field_value(val)
                if formatted:
                    parts.append(f"**{label}**\n\n{formatted}")
    return parts


def _format_tarot_verdict(data: dict) -> str:
    """塔罗结论：倾向（宜 / 中性 / 不宜）+ 能量分。

    返回空串表示字段缺失或不合法，调用方会跳过（不渲染结论卡）。
    """
    verdict = str(data.get("verdict") or "").strip()
    if verdict not in ("宜", "中性", "不宜"):
        return ""
    try:
        score = int(data.get("score"))
    except (TypeError, ValueError):
        return ""
    score = max(0, min(100, score))
    return f"**🔮 结论**\n\n**倾向**：{verdict}　**能量分**：{score}/100"


def _format_tarot_specific(data: dict) -> list[str]:
    """塔罗专用字段格式化：逐牌解读 + 牌阵主线 / 时间窗口 / 风险提醒"""
    parts: list[str] = []

    cards = data.get("cards", [])
    if cards and isinstance(cards, list):
        lines: list[str] = []
        for item in cards:
            if not isinstance(item, dict):
                if isinstance(item, str) and item.strip():
                    lines.append(item.strip())
                continue
            pos = str(item.get("pos", "") or "").strip()
            name = str(item.get("name", "") or "").strip()
            is_rev = bool(item.get("isRev"))
            text = _normalize_field_value(item.get("text", "")).strip()
            if not text:
                continue
            label_parts = [p for p in [pos, f"{'逆位' if is_rev else '正位'}·{name}" if name else ""] if p]
            label = " · ".join(label_parts)
            lines.append(f"**{label}**：{text}" if label else text)
        if lines:
            parts.append("**逐牌解读**\n\n" + "\n\n".join(lines))

    # 时间窗口：3.1 起为三段结构（近期 / 中期 / 远期），模型若仍返回字符串则原样兜底
    timing = data.get("timing")
    if timing:
        s = _format_tarot_timing(timing).strip()
        if s:
            parts.append(s)

    for key, label in (("synthesis", "牌阵主线"), ("risk", "风险提醒")):
        val = data.get(key)
        if val:
            s = _normalize_field_value(val).strip()
            if s:
                parts.append(f"**{label}**\n\n{s}")

    return parts


# 时间窗口三段：标签与 JSON 字段的映射（顺序即展示顺序）
_TIMING_LABELS = (
    ("near", "近期（2-4 周）"),
    ("mid", "中期（1-3 个月）"),
    ("far", "远期（3 个月以上）"),
)


def _format_tarot_timing(val) -> str:
    """时间窗口格式化：三段结构逐条列出；字符串（旧格式 / 模型不守约）原样渲染。"""
    if not isinstance(val, dict):
        s = _normalize_field_value(val).strip()
        return f"**时间窗口**\n\n{s}" if s else ""

    lines: list[str] = []
    for key, label in _TIMING_LABELS:
        text = _normalize_field_value(val.get(key, "")).strip()
        if text:
            lines.append(f"- **{label}**：{text}")
    if not lines:
        return ""
    return "**时间窗口**\n\n" + "\n".join(lines)


def _format_known_fields(data: dict) -> list[str]:
    """通用字段格式化：遍历 dict 中的所有 key-value"""
    parts: list[str] = []
    skip_keys = {"ok", "reason", "module", "focus", "version"}
    for key, val in data.items():
        if key in skip_keys:
            continue
        if val is None or val == "" or val == []:
            continue
        if isinstance(val, (str, int, float, bool)):
            s = str(val).strip()
            if s:
                parts.append(f"**{_key_label(key)}**\n\n{_normalize_field_value(s)}")
        elif isinstance(val, list):
            formatted = _format_complex_value(val)
            if formatted and formatted.strip():
                parts.append(f"**{_key_label(key)}**\n\n{formatted}")
        elif isinstance(val, dict):
            title = val.get("title", "")
            text = val.get("text", "")
            if title and text and not _is_reserved_title(title):
                parts.append(f"**{title}**\n\n{_normalize_field_value(text)}")
            elif text:
                parts.append(_normalize_field_value(text))
            else:
                formatted = _format_complex_value(val)
                if formatted and formatted.strip():
                    parts.append(f"**{_key_label(key)}**\n\n{formatted}")
    return parts


def _format_generic_json(data: Any) -> str:
    """格式化非 dict 的 JSON（如 list）"""
    if isinstance(data, list):
        items = []
        for item in data:
            if isinstance(item, dict):
                title = item.get("title", "")
                text = item.get("text", "")
                if title and text and not _is_reserved_title(title):
                    items.append(f"**{title}**：{text}")
                elif text:
                    items.append(str(text))
                else:
                    items.append(json.dumps(item, ensure_ascii=False))
            else:
                items.append(str(item))
        return "\n\n".join(items)
    return json.dumps(data, ensure_ascii=False, indent=2)


def _try_parse_ndjson(text: str) -> list[dict] | None:
    """尝试解析换行分隔的多个 JSON 对象（NDJSON），要求每行都是合法 dict"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    items: list[dict] = []
    for ln in lines:
        # 允许行尾逗号
        ln = ln.rstrip(",")
        try:
            parsed = json.loads(ln)
        except json.JSONDecodeError:
            repaired = _try_repair_json(ln)
            if not repaired:
                return None
            try:
                parsed = json.loads(repaired)
            except json.JSONDecodeError:
                return None
        if not isinstance(parsed, dict):
            return None
        items.append(parsed)
    return items


def _normalize_field_value(value: str | list | dict) -> str:
    """将字段值规范化为可读文本"""
    if isinstance(value, (list, dict)):
        return _format_complex_value(value)

    if not isinstance(value, str):
        return str(value)

    # 尝试检测字符串中是否包含 JSON 数组或对象
    stripped = value.strip()
    if stripped.startswith("[") or stripped.startswith("{"):
        # 先尝试直接解析
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, (list, dict)):
                return _format_complex_value(parsed)
        except json.JSONDecodeError:
            # 尝试修复后解析
            repaired = _try_repair_json(stripped)
            if repaired:
                try:
                    parsed = json.loads(repaired)
                    if isinstance(parsed, (list, dict)):
                        return _format_complex_value(parsed)
                except json.JSONDecodeError:
                    pass

    # 检查是否包含转义的 JSON (e.g., {\"key\":\"value\"})
    if '\\"' in stripped and (stripped.startswith('{') or stripped.startswith('[')):
        # 尝试反转义
        unescaped = stripped.replace('\\"', '"').replace("\\/", "/")
        try:
            parsed = json.loads(unescaped)
            if isinstance(parsed, (list, dict)):
                return _format_complex_value(parsed)
        except json.JSONDecodeError:
            pass

    # 处理 LLM 在「格局印证」中常见的 NDJSON（每行一个引文对象）
    if stripped.startswith("{"):
        ndjson_items = _try_parse_ndjson(stripped)
        if ndjson_items:
            return _format_complex_value(ndjson_items)

    return value


def _is_citation(obj: dict) -> bool:
    """判断对象是否为「典籍引文」结构（source + content/text/comment）"""
    if not isinstance(obj, dict):
        return False
    has_source = bool(obj.get("source", "").strip())
    has_body = bool(
        obj.get("content", "").strip()
        or obj.get("text", "").strip()
        or obj.get("comment", "").strip()
    )
    return has_source and has_body


def _format_citation(obj: dict) -> str:
    """将典籍引文结构格式化为可读 markdown"""
    source = str(obj.get("source", "")).strip()
    content = str(obj.get("content", obj.get("text", ""))).strip()
    comment = str(obj.get("comment", "")).strip()

    # source 可能已带《》，避免重复书名号
    source_md = source if source.startswith("《") else f"《{source}》"
    body = content
    if comment and comment != content:
        body = f"{content} —— {comment}" if content else comment
    return f"- **{source_md}**：{body}"


def _format_complex_value(value: list | dict) -> str:
    """将复杂值（list/dict）格式化为 Markdown 文本"""
    if isinstance(value, list):
        items = []
        for item in value:
            if isinstance(item, dict) and _is_citation(item):
                items.append(_format_citation(item))
            elif isinstance(item, dict):
                title = item.get("title", "")
                text = item.get("text", "")
                if title and text and not _is_reserved_title(title):
                    items.append(f"**{title}**：{text}")
                elif text:
                    items.append(text)
                else:
                    items.append(json.dumps(item, ensure_ascii=False))
            elif isinstance(item, str):
                items.append(item)
            else:
                items.append(json.dumps(item, ensure_ascii=False))
        return "\n\n".join(items)

    if isinstance(value, dict):
        if _is_citation(value):
            return _format_citation(value)
        title = value.get("title", "")
        text = value.get("text", "")
        if title and text and not _is_reserved_title(title):
            return f"**{title}**：{text}"
        if text:
            return str(text)
        return json.dumps(value, ensure_ascii=False)

    return str(value)


# 兜底输出的键名 → 中文标签：启发式格式化不再暴露 Verdict/Summary 等英文原始键名
_KEY_LABELS = {
    "verdict": "结论倾向",
    "score": "能量分",
    "summary": "整体概述",
    "synthesis": "牌阵主线",
    "risk": "风险提醒",
    "advice": "建议",
    "outlook": "近期趋势",
    "pos": "牌位",
    "name": "牌名",
    "text": "解读",
    "isrev": "正逆位",
    "title": "标题",
    "reason": "原因",
    "near": "近期（2-4 周）",
    "mid": "中期（1-3 个月）",
    "far": "远期（3 个月以上）",
    "timing": "时间窗口",
}


def _key_label(key: str) -> str:
    """键名转中文标签；映射表外的键原样保留（如模型自拟的中文键名）"""
    k = key.strip().lower()
    return _KEY_LABELS.get(k, key.replace("_", " ").strip() or key)


def _render_heuristic_card(card: dict[str, str]) -> str:
    """把兜底提取到的单张牌 {pos, name, text} 渲染成一行逐牌解读"""
    pos = card.get("pos", "")
    name = card.get("name", "")
    label = " · ".join(p for p in (pos, name) if p)
    text = card.get("text", "")
    return f"**{label}**：{text}" if label else text


def _heuristic_format(text: str) -> str:
    """启发式格式化：当 JSON 完全无法解析时，尽可能提取可读内容"""
    if not text or not text.strip():
        return "（AI 暂无解读）"

    stripped = text.strip()
    looks_like_json = stripped.startswith(("{", "["))

    if looks_like_json:
        parts: list[str] = []

        # 尝试 1: 用正则提取 "key": "value" 对
        kv_pattern = re.findall(
            r'"([^"]*)"\s*:\s*"([^"]*)"',
            stripped,
            re.DOTALL,
        )
        if kv_pattern:
            # 扁平 kv 序列里识别「逐牌三元组」：pos 开启新牌，name/text 归入当前牌，
            # 渲染成「**牌位 · 牌名**：解读」；其余键按中文标签分节输出。
            # （旧实现 key.title() 直接渲染 **Verdict**/**Pos** 等英文键名，即用户看到的「JSON 原码」）
            card_buf: dict[str, str] = {}
            for key, value in kv_pattern:
                lk = key.strip().lower()
                v = value.strip()
                if not v:
                    continue
                if lk == "pos":
                    if card_buf:
                        parts.append(_render_heuristic_card(card_buf))
                    card_buf = {"pos": v}
                elif lk in ("name", "text") and card_buf:
                    card_buf[lk] = v
                else:
                    if card_buf:
                        parts.append(_render_heuristic_card(card_buf))
                        card_buf = {}
                    parts.append(f"**{_key_label(key)}**\n\n{v}")
            if card_buf:
                parts.append(_render_heuristic_card(card_buf))

        # 尝试 2: 提取 title/text 结构
        if not parts:
            array_items = re.findall(
                r'\{\s*"title"\s*:\s*"([^"]*)"\s*,\s*"text"\s*:\s*"([^"]*)"',
                stripped,
                re.DOTALL,
            )
            if array_items:
                for title, text_val in array_items:
                    if text_val.strip() and not _is_reserved_title(title):
                        parts.append(f"**{title}**\n\n{text_val.strip()}")
                    elif text_val.strip():
                        parts.append(text_val.strip())

        # 尝试 3: 提取嵌套 JSON 结构
        if not parts:
            # 查找 {"体用生克": "..."} 或 {"体用": {...}} 等结构
            cn_pattern = re.findall(
                r'["\u201c]([^\"]*?)["\u201d]\s*:\s*["\u201c]([^\"]*?)["\u201d]',
                stripped,
                re.DOTALL,
            )
            if cn_pattern:
                for key, value in cn_pattern:
                    if value.strip():
                        parts.append(f"**{key}**\n\n{value.strip()}")

        if parts:
            return "\n\n".join(parts)

    # 清理 JSON 外壳，提取纯文本（先过字段名残留清洗，避免独立成行的 title/text 被保留）
    cleaned_text = _extract_plain_text(_strip_structural_residue(stripped))
    if cleaned_text and len(cleaned_text.strip()) > 10:
        return cleaned_text

    return "（AI 解读格式异常，请稍后重试）"


def _extract_plain_text(text: str) -> str:
    """从可能包含 JSON 结构的文本中提取纯可读文本"""
    # 移除 JSON 结构字符但保留内容
    # 1. 移除 { } [ ]
    cleaned = re.sub(r'[{}\[\]]+', ' ', text)
    # 2. 处理 "key": "value" → key: value
    cleaned = re.sub(r'"([^"]*)"\s*:\s*"([^"]*)"', r'\1: \2', cleaned)
    # 3. 处理剩余的引号
    cleaned = re.sub(r'"', '', cleaned)
    # 4. 清理转义
    cleaned = cleaned.replace('\\n', '\n').replace('\\t', ' ')
    # 5. 清理多余空白
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def _extract_json(text: str) -> str | None:
    """从文本中提取 JSON（处理 LLM 可能添加的前后缀文字）"""
    # 1. 直接尝试
    try:
        json.loads(text.strip())
        return text.strip()
    except json.JSONDecodeError:
        pass

    # 2. 去除 markdown 代码块
    cleaned = re.sub(r"```(?:json)?\s*", "", text)
    cleaned = cleaned.strip()
    try:
        json.loads(cleaned)
        return cleaned
    except json.JSONDecodeError:
        pass

    # 3. 提取第一个 { 到最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    # 4. 提取第一个 [ 到最后一个 ]
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    return None
