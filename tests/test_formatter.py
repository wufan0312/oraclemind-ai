"""玄镜 OracleMind · JSON → Markdown 格式化器回归测试

守护 `format_interpret_json` 的「永不返回原始 JSON」承诺：
- LLM 在「格局印证」中输出的 NDJSON 引文（每行一个 JSON 对象）必须被渲染为可读 markdown；
- 单条引文对象（source/content/comment）同样被正确格式化；
- 常规 title/text aspects 保持原有行为；
- 顶层 JSON 非法时，启发式兜底仍须尽量可读。
"""

from __future__ import annotations

import json

import pytest

from src.services.formatter import format_interpret_json


# ---------------------------------------------------------------------------
# 格局印证：NDJSON 引文
# ---------------------------------------------------------------------------

CITATION_NDJSON = """{"source":"《三命通会》· 日柱断","content":"戊子日柱，俗云人间没有穷戊子，戊土坐子水正财，财星自旺，多主富足。","comment":"戊土日主，财星自旺，确实主富足。"}
{"source":"《滴天髓》· 天干五合","content":"戊癸合化火，为中正之合，主信厚威严，主人聪明有德。","comment":"戊土日主，与癸水合化火，主聪明有德，但需注意火旺可能带来的冲动。"}
{"source":"《子平真诠》· 杂气格","content":"杂气印绶格，辰戌丑未四库全，印星藏于库中，逢冲则发。","comment":"日主戊土，与月柱甲木七杀相冲，可能引发印星之旺，有利于事业发展。"}"""


def test_format_interpret_ndjson_citations():
    raw = {
        "ok": True,
        "summary": "整体概述",
        "aspects": [
            {"title": "性格倾向", "text": "性格温和。"},
            {"title": "格局印证", "text": CITATION_NDJSON},
        ],
        "advice": ["建议一"],
        "outlook": "近期平稳",
    }
    text = format_interpret_json(json.dumps(raw, ensure_ascii=False), module="bazi")
    assert "整体概述" in text
    assert "性格倾向" in text
    assert "《三命通会》· 日柱断" in text
    assert "戊子日柱" in text
    assert "戊土日主，财星自旺" in text
    # 输出中不应保留原始 JSON 结构字符
    assert '"source"' not in text
    assert '"content"' not in text
    # 应为 markdown 列表条目
    assert text.count("- **《三命通会》· 日柱断**：") == 1
    assert text.count("- **《滴天髓》· 天干五合**：") == 1
    assert text.count("- **《子平真诠》· 杂气格**：") == 1


def test_format_interpret_single_citation_object():
    raw = {
        "ok": True,
        "aspects": [
            {"title": "格局印证", "text": '{"source":"《渊海子平》· 地支六合","content":"子丑合土，为泥合，主暗昧不明，凡事多阻。","comment":"日主戊土，与时柱癸水正财相合，可能带来暗昧不明之事，需谨慎处理。"}'},
        ],
    }
    text = format_interpret_json(json.dumps(raw, ensure_ascii=False), module="bazi")
    assert "- **《渊海子平》· 地支六合**：" in text
    assert "子丑合土" in text
    assert '"source"' not in text


def test_format_interpret_citation_array():
    raw = {
        "ok": True,
        "aspects": [
            {
                "title": "格局印证",
                "text": json.dumps(
                    [
                        {"source": "书一", "content": "原文一", "comment": "白话一"},
                        {"source": "书二", "content": "原文二", "comment": "白话二"},
                    ],
                    ensure_ascii=False,
                ),
            },
        ],
    }
    text = format_interpret_json(json.dumps(raw, ensure_ascii=False), module="bazi")
    assert "- **《书一》**：原文一 —— 白话一" in text
    assert "- **《书二》**：原文二 —— 白话二" in text


# ---------------------------------------------------------------------------
# 常规 aspects 与异常分支
# ---------------------------------------------------------------------------


def test_format_interpret_normal_aspects_preserved():
    raw = {
        "ok": True,
        "summary": "summary text",
        "aspects": [
            {"title": "性格倾向", "text": "text1"},
            {"title": "事业财运", "text": "text2"},
        ],
        "advice": ["a1", "a2"],
        "outlook": "outlook text",
    }
    text = format_interpret_json(json.dumps(raw, ensure_ascii=False), module="bazi")
    assert "**整体概述**" in text
    assert "**性格倾向**" in text
    assert "**事业财运**" in text
    assert "**建设性建议**" in text
    assert "**近期趋势**" in text


def test_format_interpret_ok_false():
    text = format_interpret_json('{"ok": false, "reason": "字段缺失"}', module="bazi")
    assert "解读不可用" in text
    assert "字段缺失" in text


def test_format_interpret_malformed_json_falls_back():
    text = format_interpret_json("这根本不是 JSON", module="bazi")
    # 完全非 JSON 输入走启发式兜底，给出友好提示而不是裸文本
    assert "AI 解读格式异常" in text or text == "这根本不是 JSON"


def test_format_interpret_json_code_block_stripped():
    raw = '{"ok": true, "aspects": [{"title": "格局印证", "text": " plain text "}]}'
    text = format_interpret_json(f"```json\n{raw}\n```", module="bazi")
    assert "plain text" in text
    assert "```" not in text
