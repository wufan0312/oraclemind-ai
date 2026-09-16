"""玄镜 OracleMind · JSON 修复工具集

为 formatter.py 与 structured.py 共享的 LLM 输出修复链：
- repair_json：处理中文引号 / 控制字符 / 尾随逗号 / 字符串内换行 / 未转义引号
- close_truncated_json：按状态机补全被 max_tokens 截断的 JSON

所有函数均纯函数、无副作用，返回 None 表示修复失败（调用方降级即可）。
"""

from __future__ import annotations

import json
import re


def fix_newlines_in_strings(text: str) -> str:
    """修复 JSON 字符串内未转义的换行符。

    合法 JSON 字符串内不允许出现裸 \\n / \\r，但 LLM 经常直接换行。
    通过状态机识别 in_string 状态，把字符串内换行替换为 \\n / \\r 转义。
    """
    result = []
    in_string = False
    escape_next = False
    i = 0
    while i < len(text):
        ch = text[i]
        if escape_next:
            result.append(ch)
            escape_next = False
            i += 1
            continue
        if ch == "\\":
            result.append(ch)
            escape_next = True
            i += 1
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            i += 1
            continue
        if in_string and ch in ("\n", "\r"):
            result.append("\\n" if ch == "\n" else "\\r")
            i += 1
            continue
        result.append(ch)
        i += 1
    return "".join(result)


def fix_escaped_quotes(text: str) -> str:
    """修复 JSON 字符串内未正确转义的双引号。

    LLM 偶发输出 {"text": "他说"你好"然后走了"}，正确应为
    {"text": "他说\\"你好\\"然后走了"}。策略：在字符串内部遇到 "
    时，看后续非空白字符是否为 JSON 结构符（: , } ]）—— 是则视为字符串
    结束，否则视为字符串内部引号并补转义。
    """
    result = []
    in_string = False
    escape_next = False
    i = 0
    while i < len(text):
        ch = text[i]
        if escape_next:
            result.append(ch)
            escape_next = False
            i += 1
            continue
        if ch == "\\":
            result.append(ch)
            escape_next = True
            i += 1
            continue
        if ch == '"':
            if not in_string:
                in_string = True
                result.append(ch)
            else:
                rest = text[i + 1:].lstrip()
                if rest and rest[0] in (":", ",", "}", "]", "\n", "\r"):
                    in_string = False
                    result.append(ch)
                elif not rest:
                    in_string = False
                    result.append(ch)
                else:
                    result.append('\\"')
            i += 1
            continue
        result.append(ch)
        i += 1
    return "".join(result)


def repair_json(text: str) -> str | None:
    """尝试修复 LLM 返回的不规范 JSON。

    修复链：中文引号 → 控制字符 → 尾随逗号 → 字符串内换行 → 未转义引号。
    修复后整体可解析才返回，否则返回 None 让调用方降级。
    """
    if not text or not text.strip():
        return None

    repaired = text.strip()

    # 1. 替换中文引号为英文引号
    repaired = repaired.replace("\u201c", '"').replace("\u201d", '"')
    repaired = repaired.replace("\u2018", "'").replace("\u2019", "'")

    # 2. 清理不可见控制字符（保留 \n \t \r 等合法转义）
    cleaned_chars = []
    for ch in repaired:
        code = ord(ch)
        if code < 32 and ch not in ("\n", "\t", "\r"):
            continue
        cleaned_chars.append(ch)
    repaired = "".join(cleaned_chars)

    # 3. 移除对象/数组中的尾随逗号
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)

    # 4. 移除字符串内部的未转义换行符
    repaired = fix_newlines_in_strings(repaired)

    # 5. 修复字符串内未转义的引号
    repaired = fix_escaped_quotes(repaired)

    try:
        json.loads(repaired)
        return repaired
    except json.JSONDecodeError:
        pass

    return None


def close_truncated_json(text: str) -> str | None:
    """补全被 max_tokens 截断的 JSON：按扫描状态机闭合未结束的字符串与括号栈。

    GLM-4-Flash 偶发在 token 预算边界把 JSON 掐断（cards/timing 写一半），
    此时 json.loads 与 repair_json（只修引号/换行/尾逗号）都救不了。
    补全后仅在整体可解析时返回，否则返回 None。
    """
    if not text or not text.strip():
        return None
    stack: list[str] = []
    in_string = False
    escape_next = False
    for ch in text:
        if in_string:
            if escape_next:
                escape_next = False
            elif ch == "\\":
                escape_next = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    closed = text
    if in_string:
        if escape_next:
            closed = closed[:-1]  # 去掉悬空的转义符再闭合
        closed += '"'
    # 去掉尾部悬挂的逗号/冒号/空白，再按栈逆序闭合
    closed = re.sub(r"[,:\s]+$", "", closed)
    for br in reversed(stack):
        closed += "}" if br == "{" else "]"
    try:
        json.loads(closed)
        return closed
    except json.JSONDecodeError:
        return None
