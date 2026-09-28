"""P2 质量门指标（纯标准库，无外部依赖）。

三类指标对应玄镜 AI 输出的常见退化：
- format_compliance：必含章节是否齐备（格式合规率）
- orphaned_heading_rate：孤立编号标题占比（孤立编号率）
- hallucination_keyword_rate：幻觉 / 越界关键词命中率（幻觉关键词命中率）

其中孤立编号率即前端 textDedup.renumberStandaloneNumberedHeadings 的 Python 化度量：
模型常产出「1. 标题」后无正文、或编号不连续的孤立标题，属 GLM-4-Flash 典型格式漂移。
"""
from __future__ import annotations

import re

# 任意 markdown 标题行：#~###### 前缀，或行首独立编号 "N. 文本"
_HEADING_RE = re.compile(r"^(#{1,6}\s+.*|(\d+)\.\s+\S.*)$")
# 独立编号标题行："N. 文本"（允许 0~6 个 # 前缀）
_NUMBERED_HEADING_RE = re.compile(r"^(#{0,6}\s*)?(\d+)\.\s+\S")


def split_sections(text: str) -> list[tuple[str | None, str]]:
    """按标题切分，返回 [(heading, body), ...]；非标题开头的文本归入 heading=None。"""
    sections: list[tuple[str | None, str]] = []
    cur_heading: str | None = None
    cur_body: list[str] = []
    for ln in text.splitlines():
        if _HEADING_RE.match(ln):
            if cur_heading is not None or cur_body:
                sections.append((cur_heading, "\n".join(cur_body).strip()))
            cur_heading = ln.strip()
            cur_body = []
        else:
            cur_body.append(ln)
    if cur_heading is not None or cur_body:
        sections.append((cur_heading, "\n".join(cur_body).strip()))
    return sections


def format_compliance(text: str, required_sections: list[str]) -> dict:
    """必含章节（子串）齐备性。返回 {compliant, missing}。"""
    missing = [s for s in required_sections if s not in text]
    return {"compliant": len(missing) == 0, "missing": missing}


def orphaned_heading_rate(text: str) -> dict:
    """孤立编号率：有编号标题（N.）的章节中，正文为空（无实质内容）的比例。

    返回 {total, orphaned, rate}。
    """
    total = 0
    orphaned = 0
    for heading, body in split_sections(text):
        if heading is None:
            continue
        if not _NUMBERED_HEADING_RE.match(heading):
            continue
        total += 1
        # 正文为空或仅空白/标点 → 孤立
        if not body or len(re.sub(r"[\s\W]+", "", body)) == 0:
            orphaned += 1
    rate = (orphaned / total) if total else 0.0
    return {"total": total, "orphaned": orphaned, "rate": rate}


def hallucination_keyword_rate(text: str, keywords: list[str]) -> dict:
    """幻觉 / 越界关键词命中率：命中数 / 句数（以句号/换行切分估算规模）。

    返回 {hits:[(kw,count)], total_hits, rate, has_hallucination}。
    """
    hits: list[tuple[str, int]] = []
    for kw in keywords:
        c = text.count(kw)
        if c:
            hits.append((kw, c))
    sentences = [s for s in re.split(r"[。！？\.\n]+", text) if s.strip()]
    denom = max(len(sentences), 1)
    total_hits = sum(c for _, c in hits)
    return {
        "hits": hits,
        "total_hits": total_hits,
        "rate": total_hits / denom,
        "has_hallucination": len(hits) > 0,
    }
