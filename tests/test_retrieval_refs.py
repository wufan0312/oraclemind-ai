"""format_refs —— 书名号归一化 + 去重（P3-5）。

覆盖四种脏写法与两种去重口径：
  书名号：裸名 / 成对 / 缺前 / 缺后 / 重复包裹 / 尖括号别名 / 空值
  去重：  同书同章留最高分、正文重复丢弃、编号连续
"""

from __future__ import annotations

import pytest

from src.services.retrieval import format_refs, normalize_source


# ===== 书名号归一化 =====

@pytest.mark.parametrize(
    "raw,expect",
    [
        ("葬书", "《葬书》"),
        ("《葬书》", "《葬书》"),          # 已带成对书名号 —— 不能再包一层
        ("葬书》", "《葬书》"),            # 缺前括号（旧实现会产出《葬书》》）
        ("《葬书", "《葬书》"),            # 缺后括号
        ("《《葬书》》", "《葬书》"),      # 重复包裹
        ("  《葬书》  ", "《葬书》"),      # 前后空白
        ("<葬书>", "《葬书》"),            # 尖括号别名
        ("", "《未知》"),                  # 空值兜底
        (None, "《未知》"),
    ],
)
def test_normalize_source(raw, expect):
    assert normalize_source(raw) == expect


# ===== 去重 =====

def _doc(source, chapter="", text="正文", score=0.5):
    return {"source": source, "chapter": chapter, "text": text, "score": score}


def test_dedup_same_book_same_chapter_keeps_highest_score():
    docs = [
        _doc("葬书", "内篇", "低分片段", score=0.3),
        _doc("《葬书》", "内篇", "高分片段", score=0.9),  # 书名号写法不同，归一化后同一条
        _doc("葬书", "内篇", "中分片段", score=0.6),
    ]
    out = format_refs(docs)
    assert out.count("【参考资料") == 1
    assert "高分片段" in out
    assert "低分片段" not in out
    assert "中分片段" not in out
    # 编号从 [1] 起且连续
    assert "[1]" in out and "[2]" not in out


def test_chapter_partitions_same_book():
    docs = [
        _doc("葬书", "内篇", "甲", score=0.9),
        _doc("葬书", "外篇", "乙", score=0.8),
    ]
    out = format_refs(docs)
    assert "[1]" in out and "[2]" in out
    assert "甲" in out and "乙" in out


def test_dedup_identical_text():
    docs = [
        _doc("葬书", "内篇", "完全一样的话", score=0.9),
        _doc("青囊经", "上卷", "完全一样的话", score=0.8),  # 不同书但正文重复
    ]
    out = format_refs(docs)
    assert out.count("完全一样的话") == 1


def test_empty_and_all_invalid():
    assert format_refs([]) == ""
    assert format_refs([_doc("葬书", text="   ")]) == ""   # 空正文被丢弃


def test_ref_line_format():
    out = format_refs([_doc("葬书", "内篇", "气乘风则散", score=0.9)])
    lines = out.strip().split("\n")
    assert lines[0] == "【参考资料 · 检索增强】"
    assert lines[1].startswith("[1] 《葬书》· 内篇")
    assert "气乘风则散" in out


def test_no_chapter_omits_separator():
    out = format_refs([_doc("葬书", "", "正文", score=0.9)])
    assert "[1] 《葬书》\n" in out
