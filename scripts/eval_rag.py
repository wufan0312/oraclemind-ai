"""玄镜 OracleMind · RAG 检索质量评估脚本（基线）

目的：在没有任何人工标注 gold 的情况下，用「代表性排盘结果 + 期望命中的经典来源 +
关键主题词」构造 golden set，复用**生产检索链路**（_extract_features → retrieve →
MIN_RELEVANCE 过滤），量化当前 RAG 的基线命中率，作为后续「查询构造优化 / 知识库补全」
的可量化参照。

指标：
  - hit_expected_source：检索 top-k（经 MIN_RELEVANCE 过滤）是否含任一「期望来源」。
  - term_recall：检索拼接文本是否覆盖 case 的「关键主题词」（更鲁棒的替代相关性指标）。
  - top_score / min_score：最高/最低相似度，用于观察 MIN_RELEVANCE 阈值是否误删。

用法：
  .venv/Scripts/python.exe scripts/eval_rag.py
可选导出：
  .venv/Scripts/python.exe scripts/eval_rag.py --json outputs/markdown/rag-eval-baseline.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 让脚本能 import src 包（脚本位于 <project>/scripts/ 下，parent.parent 即项目根）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.services.retrieval import (  # noqa: E402
    MIN_RELEVANCE,
    _extract_features,
    normalize_source,
    retrieve,
)


# ---------------------------------------------------------------------------
# Golden set：每个模块 2 个代表性 case。
#   result       —— 模拟前端/排盘服务传入的排盘结果（字段对齐 _extract_features 取值口径）
#   expected     —— 该 case 最该命中的经典来源（normalize_source 后形态，如《滴天髓》）
#   key_terms    —— 期望检索文本应覆盖的主题词（term_recall 用，与来源无关，更稳）
# ---------------------------------------------------------------------------
GOLDEN = [
    # ---------------- 八字 ----------------
    {
        "module": "bazi",
        "name": "日主甲木旺",
        "result": {
            "dayMaster": "甲", "dayMasterWuxing": "木",
            "pillars": [{"label": "日柱", "gan": "甲", "zhi": "子", "note": "比肩"}],
            "yongshen": {"xi": ["金"], "ji": ["水"]},
            "wuxing": [{"label": "木", "pct": 55}],
        },
        "expected": ["《滴天髓》", "《穷通宝鉴》"],
        "key_terms": ["甲", "木", "旺衰", "日主", "喜用"],
    },
    {
        "module": "bazi",
        "name": "日主戊土弱喜火",
        "result": {
            "dayMaster": "戊", "dayMasterWuxing": "土",
            "pillars": [{"label": "日柱", "gan": "戊", "zhi": "午", "note": "正印"}],
            "yongshen": {"xi": ["火"], "ji": ["水"]},
            "wuxing": [{"label": "土", "pct": 20}],
        },
        "expected": ["《穷通宝鉴》", "《滴天髓》"],
        "key_terms": ["戊", "土", "弱", "喜", "用神"],
    },
    # ---------------- 紫微 ----------------
    {
        "module": "ziwei",
        "name": "紫微在命宫",
        "result": {
            "ziwei": "紫微在命宫",
            "palaces": [{"name": "命宫", "star": "紫微", "highlight": True}],
            "patterns": [{"name": "紫微居命"}],
        },
        "expected": ["《紫微斗数全书》", "《骨髓赋》"],
        "key_terms": ["紫微", "命宫", "格局"],
    },
    {
        "module": "ziwei",
        "name": "日月并明格局",
        "result": {
            "ziwei": "日月同宫",
            "patterns": [{"name": "日月并明"}],
        },
        "expected": ["《骨髓赋》", "《斗数宣微》"],
        "key_terms": ["日月", "格局", "紫微"],
    },
    # ---------------- 六爻 ----------------
    {
        "module": "liuyao",
        "name": "本卦乾用神妻财",
        "result": {
            "benGua": {"name": "乾为天", "gong": "乾"},
            "yongshen": {"name": "妻财"},
        },
        "expected": ["《增删卜易》", "《黄金策》"],
        "key_terms": ["乾", "用神", "妻财", "本卦"],
    },
    {
        "module": "liuyao",
        "name": "本卦坤问财",
        "result": {
            "benGua": {"name": "坤为地", "gong": "坤"},
            "yongshen": {"name": "妻财"},
        },
        "expected": ["《卜筮正宗》", "《火珠林》"],
        "key_terms": ["坤", "用神", "财"],
    },
    # ---------------- 梅花 ----------------
    {
        "module": "meihua",
        "name": "体乾用坤比和",
        "result": {
            "ti": {"name": "乾", "wuxing": "金"},
            "yong": {"name": "坤", "wuxing": "土"},
            "tiYong": {"relation": "比和"},
        },
        "expected": ["《梅花易数》", "《周易》"],
        "key_terms": ["体", "用", "乾", "坤", "比和"],
    },
    {
        "module": "meihua",
        "name": "体离用坎相克",
        "result": {
            "ti": {"name": "离", "wuxing": "火"},
            "yong": {"name": "坎", "wuxing": "水"},
            "tiYong": {"relation": "相克"},
        },
        "expected": ["《周易》", "《梅花易数》"],
        "key_terms": ["离", "坎", "克", "体", "用"],
    },
    # ---------------- 奇门 ----------------
    {
        "module": "qimen",
        "name": "阳遁值符天蓬",
        "result": {
            "type": "阳遁",
            "valueFu": "天蓬",
            "valueShi": "休门",
            "palaces": [{"dir": "坎", "star": "天蓬", "door": "休门"}],
        },
        "expected": ["《烟波钓叟歌》", "《奇门遁甲秘笈大全》"],
        "key_terms": ["奇门", "值符", "值使", "遁"],
    },
    # ---------------- 解梦 ----------------
    {
        "module": "dream",
        "name": "梦见掉牙",
        "result": {"dream": "梦见牙齿松动掉落"},
        "expected": ["《周公解梦》"],
        "key_terms": ["梦", "牙", "掉"],
    },
    {
        "module": "dream",
        "name": "梦见飞翔",
        "result": {"dream": "梦见自己在云端飞翔"},
        "expected": ["《周公解梦》", "《荣格分析心理学》"],
        "key_terms": ["飞", "梦"],
    },
    # ---------------- 风水 ----------------
    {
        "module": "fengshui",
        "name": "财位武曲",
        "result": {
            "dimension": "财位",
            "stars": [{"pos": "东南", "starName": "武曲"}],
        },
        "expected": ["《阳宅十书》", "《八宅明镜》"],
        "key_terms": ["财", "方位", "宅"],
    },
    {
        "module": "fengshui",
        "name": "玄空飞星",
        "result": {
            "dimension": "玄空飞星",
            "stars": [{"pos": "中宫", "starName": "五黄"}],
        },
        "expected": ["《沈氏玄空学》"],
        "key_terms": ["飞星", "玄空"],
    },
    # ---------------- 塔罗 ----------------
    {
        "module": "tarot",
        "name": "感情三牌",
        "result": {
            "spreadName": "三张牌",
            "question": "他会回来吗",
            "cards": [
                {"name": "恋人", "isRev": False, "pos": "过去"},
                {"name": "高塔", "isRev": True, "pos": "现在"},
                {"name": "星星", "isRev": False, "pos": "未来"},
            ],
        },
        "expected": ["《玄镜塔罗解读手册》"],
        "key_terms": ["恋人", "塔", "星星", "牌"],
    },
    # ---------------- 数字命理 ----------------
    {
        "module": "numerology",
        "name": "生命灵数7",
        "result": {
            "lifePath": 7,
            "data": {"name": "小凡", "element": "水", "lesson": "内省"},
            "core": {
                "expression": 3, "soulUrge": 5, "personality": 2, "maturity": 9,
                "challenge": {"c1": 4, "c2": 2, "c3": 1, "c4": 3},
            },
            "missing": [2, 4],
            "years": [{"py": 2026}],
        },
        "expected": ["《生命灵数体系》", "《玄镜数字命理》"],
        "key_terms": ["生命灵数", "7", "缺数", "表达"],
    },
    {
        "module": "numerology",
        "name": "生命灵数1",
        "result": {
            "lifePath": 1,
            "data": {"name": "阿明", "element": "火", "lesson": "开创"},
            "core": {"expression": 1, "soulUrge": 1, "personality": 1, "maturity": 2,
                     "challenge": {"c1": 0, "c2": 0, "c3": 0, "c4": 0}},
            "missing": [5, 7],
            "years": [{"py": 2026}],
        },
        "expected": ["《毕达哥拉斯数字学》", "《生命灵数体系》"],
        "key_terms": ["生命灵数", "1", "缺数"],
    },
    # ---------------- 星座 ----------------
    {
        "module": "horoscope",
        "name": "日狮月蟹升秤",
        "result": {
            "sunSign": {"sign": "狮子"},
            "moonSign": {"sign": "巨蟹"},
            "ascendant": {"sign": "天秤"},
            "midheaven": {"sign": "巨蟹"},
            "planets": [
                {"label": "火星", "sign": "白羊", "dignity": "庙", "retrograde": True},
            ],
            "elements": {"火": 4, "水": 3},
            "aspects": [{"type": "合", "p1": "日", "p2": "水"}],
        },
        "expected": ["《心理占星学》", "《托勒密《占星四书》》"],
        "key_terms": ["太阳", "月亮", "上升", "狮子", "巨蟹"],
    },
    # ---------------- 大六壬 ----------------
    {
        "module": "liuren",
        "name": "元首课三传",
        "result": {
            "yueJiangName": "亥", "yueJiang": "亥", "zhanShi": "子",
            "riGanZhi": "甲子",
            "sanChuan": {"keTi": "元首课", "method": "元首", "chu": "子", "zhong": "戌", "mo": "申"},
            "tianJiang": "贵人",
            "guiRen": {"zhi": "丑"},
            "kongWang": ["午", "未"],
        },
        "expected": ["《六壬毕法赋》", "《大六壬大全》"],
        "key_terms": ["六壬", "三传", "课", "贵人"],
    },
    # ---------------- 太乙 ----------------
    {
        "module": "taiyi",
        "name": "阳遁十二局",
        "result": {
            "type": "阳遁", "ju": 12, "dun": "阳",
            "taiYiGong": {"pos": "一宫", "fang": "乾"},
            "zhuSuan": 15, "keSuan": 22,
            "wenChang": {"shen": "武曲"}, "shiJi": {"shen": "文曲"},
            "zhuDaJiang": {"pos": "三宫"}, "keDaJiang": {"pos": "七宫"},
            "jiNian": 2026,
        },
        "expected": ["《太乙式经》", "《太乙统宗宝鉴》"],
        "key_terms": ["太乙", "局", "算"],
    },
]


def _base_source(s: str) -> str:
    """取来源主名：去掉《》后按 '/' 拆，保留第一段（如 '《周公解梦》/东方五行取象'→'周公解梦'）。

    原因：KB 的 source 常带子流派后缀，检索命中应看「主来源」是否对齐，避免把
    '《周公解梦》/东方五行取象' 误判为未命中 '《周公解梦》'。
    """
    s = normalize_source(s).strip("《》")
    return s.split("/")[0].strip()


def evaluate():
    rows = []
    for case in GOLDEN:
        mod = case["module"]
        q = _extract_features(mod, case["result"])
        docs = retrieve(q, where={"module": mod})
        # 复刻生产 retrieve_for_module 的 MIN_RELEVANCE 过滤
        docs = [d for d in docs if float(d.get("score", 1.0)) >= MIN_RELEVANCE]

        retrieved_sources = [normalize_source(d.get("source", "")) for d in docs]
        retrieved_bases = [_base_source(d.get("source", "")) for d in docs]
        expected_bases = [_base_source(e) for e in case["expected"]]
        # 前缀匹配：检索到的主来源命中任一期望主来源即算命中
        hit_src = any(
            rb.startswith(eb) or eb.startswith(rb)
            for rb in retrieved_bases for eb in expected_bases
        )

        merged = " ".join(d.get("text", "") for d in docs)
        term_hits = [t for t in case["key_terms"] if t in merged]
        term_recall = len(term_hits) / len(case["key_terms"]) if case["key_terms"] else 0.0

        scores = [float(d.get("score", 0.0)) for d in docs]
        top_score = max(scores) if scores else 0.0
        min_score = min(scores) if scores else 0.0

        rows.append({
            "module": mod,
            "name": case["name"],
            "query_len": len(q),
            "retrieved": len(docs),
            "hit_expected_source": hit_src,
            "term_recall": round(term_recall, 3),
            "top_score": round(top_score, 3),
            "min_score": round(min_score, 3),
            "retrieved_sources": retrieved_sources,
            "expected": case["expected"],
        })
    return rows


def summarize(rows):
    total = len(rows)
    hit = sum(1 for r in rows if r["hit_expected_source"])
    term_avg = sum(r["term_recall"] for r in rows) / total if total else 0.0
    empty = sum(1 for r in rows if r["retrieved"] == 0)
    by_mod = {}
    for r in rows:
        m = r["module"]
        d = by_mod.setdefault(m, {"n": 0, "hit": 0, "term": 0.0})
        d["n"] += 1
        d["hit"] += 1 if r["hit_expected_source"] else 0
        d["term"] += r["term_recall"]
    per_mod = {m: {"hit_rate": round(v["hit"] / v["n"], 3),
                   "term_recall": round(v["term"] / v["n"], 3),
                   "n": v["n"]} for m, v in by_mod.items()}
    return {
        "total_cases": total,
        "hit_rate": round(hit / total, 3),
        "avg_term_recall": round(term_avg, 3),
        "empty_retrieval_cases": empty,
        "min_relevance_threshold": MIN_RELEVANCE,
        "per_module": per_mod,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="导出 JSON 路径", default=None)
    args = ap.parse_args()

    rows = evaluate()
    summary = summarize(rows)

    print(f"RAG 基线评估 ｜ 阈值 MIN_RELEVANCE={MIN_RELEVANCE}")
    print("=" * 78)
    print(f"{'模块':<10}{'case':<14}{'检索':>4}{'命中来源':>8}{'词召回':>8}{'top':>7}{'min':>7}")
    print("-" * 78)
    for r in rows:
        print(f"{r['module']:<10}{r['name']:<14}{r['retrieved']:>4}"
              f"{'✓' if r['hit_expected_source'] else '✗':>8}"
              f"{r['term_recall']:>8}{r['top_score']:>7}{r['min_score']:>7}")
    print("=" * 78)
    print(f"总体命中率(hit_rate)   : {summary['hit_rate']}")
    print(f"平均词召回(term_recall): {summary['avg_term_recall']}")
    print(f"空检索 case 数          : {summary['empty_retrieval_cases']} / {summary['total_cases']}")
    print("各模块:")
    for m, v in summary["per_module"].items():
        print(f"  {m:<10} hit={v['hit_rate']}  term={v['term_recall']}  n={v['n']}")

    if args.json:
        out = {"summary": summary, "rows": rows}
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已导出: {args.json}")


if __name__ == "__main__":
    main()
