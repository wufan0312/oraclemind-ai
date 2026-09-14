"""玄镜 OracleMind · RAG 检索增强服务（Chroma 向量数据库）

替代 TS 版的关键词加权检索，升级为语义向量检索。
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any, Optional

from src.config import config

logger = logging.getLogger(__name__)

# 全局 Chroma 客户端
_chroma_client = None
_collection = None
# Chroma PersistentClient 初始化不是线程安全的（asyncio.to_thread 会并发进入），
# 用锁保证「单例初始化」只发生一次，避免多 worker 线程同时建客户端写同一目录。
_collection_lock = threading.Lock()

# 检索结果 LRU 缓存：key=(query, top_k, where)，避免对相同查询重复发起 embedding API + chroma 查询。
# 直接命中缓存可省掉一次联网 embedding 调用（成本）和一次同步 I/O（延迟）。
_RETRIEVE_CACHE: "OrderedDict[tuple, list]" = OrderedDict()
_RETRIEVE_CACHE_MAX = 1024

# 相关性下限（cosine 相似度）。语义检索几乎总能凑满 top_k 条，但语料变大、排在末尾的往往
# 只是「沾边」——硬塞进 prompt 会稀释有用信息，甚至把模型带偏（实测曾把「全部正位」条目检索
# 进含逆位的牌阵）。宁可少给，不要给杂。
# 但本仓库单模块语料仅 14~20 条（极小语料）：若该模块唯一相关的一条因表述角度不同、相似度略
# 低于阈值（如 0.20）会被直接删掉，反而让解读丢失唯一参考。基线评估显示命中条 min_score 区间
# 为 0.45~0.62，远高于本阈值，故把 0.25 降到 0.15 既能保底「稀疏/角度偏的查询不误删唯一相关条」，
# 又不会在小语料下引入噪声（top_k 候选集本就稀薄，降阈值不会让无关条混入）。
MIN_RELEVANCE = 0.15


def _get_embedding_function():
    """构建 Embedding 函数（OpenAI 兼容接口）"""
    from langchain_openai import OpenAIEmbeddings

    return OpenAIEmbeddings(
        api_key=config.embedding_api_key or config.ai_api_key,
        base_url=config.embedding_base_url,
        model=config.embedding_model,
    )


def get_collection():
    """获取/初始化 Chroma collection（惰性加载，线程安全）"""
    global _chroma_client, _collection
    if _collection is not None:
        return _collection

    # 双检锁：避免 asyncio.to_thread 并发首调时重复初始化客户端
    with _collection_lock:
        if _collection is not None:
            return _collection
        try:
            import chromadb
            from chromadb.utils import embedding_functions

            _chroma_client = chromadb.PersistentClient(path=config.chroma_persist_dir)

            # Embedding 函数：优先用 OpenAI 兼容接口（智谱/OpenAI），无 key 时用本地默认模型
            embed_key = config.embedding_api_key or config.ai_api_key
            if embed_key:
                ef = embedding_functions.OpenAIEmbeddingFunction(
                    api_key=embed_key,
                    api_base=config.embedding_base_url,
                    model_name=config.embedding_model,
                )
            else:
                # 无 API key 时使用 Chroma 默认 embedding（all-MiniLM-L6-v2，本地运行）
                ef = embedding_functions.DefaultEmbeddingFunction()

            _collection = _chroma_client.get_or_create_collection(
                name=config.chroma_collection,
                embedding_function=ef,
                metadata={"hnsw:space": "cosine"},
            )
            logger.info(f"Chroma 集合已加载: {config.chroma_collection}")
            return _collection
        except Exception as e:
            logger.error(f"Chroma 初始化失败: {e}")
            return None


def _retrieve_cache_key(query: str, k: int, where: Optional[dict]) -> tuple:
    """缓存键：相同查询文本直接复用 embedding + 向量检索结果。"""
    wk = frozenset(where.items()) if where else None
    return (query, k, wk)


def retrieve(
    query: str,
    top_k: Optional[int] = None,
    where: Optional[dict] = None,
) -> list[dict]:
    """语义检索：返回 [{id, text, source, score, metadata}]

    同步 I/O（Chroma 查询 + 每次请求的 embedding API 调用）。本函数本身保持同步，
    调用方应包在 `await asyncio.to_thread(retrieve, ...)` 中以移出事件循环。
    结果按 query 文本做 LRU 缓存，相同的排盘特征查询直接命中，省掉重复 embedding 成本。
    """
    coll = get_collection()
    if coll is None:
        return []

    k = top_k or config.retrieval_topk
    key = _retrieve_cache_key(query, k, where)
    cached = _RETRIEVE_CACHE.get(key)
    if cached is not None:
        _RETRIEVE_CACHE.move_to_end(key)  # LRU 命中即刷新
        return cached

    try:
        results = coll.query(
            query_texts=[query],
            n_results=k,
            where=where,
        )

        docs = []
        for i, doc_id in enumerate(results.get("ids", [[]])[0]):
            text = results["documents"][0][i]
            meta = results.get("metadatas", [None])[0][i] if results.get("metadatas") else {}
            dist = results.get("distances", [None])[0][i] if results.get("distances") else 0.0
            docs.append({
                "id": doc_id,
                "text": text,
                "source": meta.get("source", "未知"),
                "chapter": meta.get("chapter", ""),
                "module": meta.get("module", ""),
                "score": 1.0 - dist,  # cosine distance → similarity
                "metadata": meta,
            })

        # 写入缓存（命中率低时上限保护，避免常驻内存无限增长）
        _RETRIEVE_CACHE[key] = docs
        while len(_RETRIEVE_CACHE) > _RETRIEVE_CACHE_MAX:
            _RETRIEVE_CACHE.popitem(last=False)
        return docs
    except Exception as e:
        logger.error(f"Chroma 检索失败: {e}")
        return []


def normalize_source(source: str) -> str:
    """规范化书名：剥离已有 / 残缺的书名号后，统一只包一层《》。

    语料里 source 写法极不统一：「葬书」「《葬书》」「葬书》」「《葬书」「《《葬书》》」。
    旧实现只判前缀 `《`，于是「葬书」正常、「葬书》」被包成《葬书》》、「《葬书》」
    虽不加前括号却也去不掉残缺后缀。这里用 strip("《》") 一次性去掉首尾所有书名号
    字符（含全角与尖括号别名），再统一补回一对，保证任何写法都归一为《X》。
    """
    s = (source or "").strip().strip("《》<>").strip()
    return f"《{s}》" if s else "《未知》"


def format_refs(docs: list[dict]) -> str:
    """将检索结果格式化为参考资料文本块（书名号归一 + 去重）。

    去重口径（两条，按序生效）：
      1. 同一 (书名, 章节) 只保留**得分最高**的一条 —— 向量库常把一本书的同一章
         切成多个 chunk，全部塞进 prompt 会挤占上下文且让 LLM 误以为是多部典籍；
      2. 正文前 300 字完全相同的片段直接丢弃 —— 兜住 id 不同但内容重复的脏数据。

    编号在去重后重排，保证 [1][2][3] 连续（旧实现先编号后可能被跳过，出现断号）。
    """
    if not docs:
        return ""

    # 先按分值降序，保证「同书同章保留最高分」——docs 原始顺序不保证有序
    ordered = sorted(
        docs,
        key=lambda d: float(d.get("score", 0.0) or 0.0),
        reverse=True,
    )

    lines = ["【参考资料 · 检索增强】"]
    seen_ref: set[tuple[str, str]] = set()
    seen_text: set[str] = set()
    idx = 0

    for doc in ordered:
        source = normalize_source(doc.get("source", ""))
        chapter = (doc.get("chapter") or "").strip()
        text = (doc.get("text") or "").strip()
        if not text:
            continue

        text_key = text[:300]
        ref_key = (source, chapter)
        if ref_key in seen_ref or text_key in seen_text:
            continue
        seen_ref.add(ref_key)
        seen_text.add(text_key)

        idx += 1
        ref = source + (f"· {chapter}" if chapter else "")
        lines.append(f"[{idx}] {ref}\n{text_key}\n")

    # 只有表头说明没有任何有效条目
    if idx == 0:
        return ""
    return "\n".join(lines)


def retrieve_for_module(
    module: str,
    result: dict,
) -> str:
    """按术数模块提取特征并检索，返回格式化的参考资料文本"""
    if not config.retrieval_enabled:
        return ""

    # 根据模块提取检索特征
    query = _extract_features(module, result)
    if not query:
        return ""

    docs = retrieve(query, where={"module": module})
    # 低分过滤：score 缺失时按「相关」处理，避免因字段缺失把全部结果丢掉
    docs = [d for d in docs if float(d.get("score", 1.0)) >= MIN_RELEVANCE]
    return format_refs(docs)


def _extract_features(module: str, result: dict) -> str:
    """从排盘结果中提取检索关键词作为语义查询"""
    features = []

    # 安全取值：确保子字段为 dict/list，避免非 dict 调用 .get 崩溃
    def _d(v):
        return v if isinstance(v, dict) else {}
    def _l(v):
        return v if isinstance(v, list) else []

    if module == "bazi":
        dm = result.get("dayMaster", "")
        dmwx = result.get("dayMasterWuxing", "")
        # 日主核心特征（始终保留）
        if dm or dmwx:
            features.append(f"日主{dm} {dmwx}")
            features.append(f"{dmwx}旺衰 日主{dm}")
        # 四柱干支（修复 typo：gold -> gan）
        pillar_texts = []
        for p in _l(result.get("pillars")):
            if isinstance(p, dict) and p.get("gan"):
                gz = f"{p.get('gan','')}{p.get('zhi','')}"
                pillar_texts.append(gz)
                # 日柱单独加入（典籍中常以日柱为核心）
                if p.get("label") == "日柱":
                    features.append(f"日柱 {gz}")
                # 带十神的干支优先
                if p.get("note"):
                    features.append(f"{p.get('note','')} {gz}")
        if pillar_texts:
            features.append("四柱 " + " ".join(pillar_texts))
        # 喜忌用神
        ys = _d(result.get("yongshen"))
        xi_list = ys.get("xi", []) if isinstance(ys.get("xi"), list) else []
        ji_list = ys.get("ji", []) if isinstance(ys.get("ji"), list) else []
        if xi_list:
            features.append(f"喜用神 {''.join(str(x) for x in xi_list)}")
        if ji_list:
            features.append(f"忌神 {''.join(str(x) for x in ji_list)}")
        # 十神特征
        for ss in _l(result.get("shiShen")):
            if isinstance(ss, dict) and ss.get("name"):
                features.append(f"{ss.get('name')} {ss.get('wuxing','')}")
        # 五行强弱
        for wx in _l(result.get("wuxing")):
            if isinstance(wx, dict) and wx.get("label"):
                pct = wx.get("pct", 0)
                if isinstance(pct, (int, float)) and pct >= 40:
                    features.append(f"{wx['label']}旺")
        # 用神为核心检索关键词，优先级权重加倍
        for _ in range(2):
            if xi_list:
                features.append(f"喜{''.join(str(x) for x in xi_list)}")
            if dm:
                features.append(f"日主{dm}")

    elif module == "ziwei":
        features.append(result.get("ziwei", ""))
        for p in _l(result.get("palaces")):
            if isinstance(p, dict) and p.get("highlight"):
                features.append(f"{p.get('name','')}宫 {p.get('star','')}")
        for pat in _l(result.get("patterns")):
            if isinstance(pat, dict):
                features.append(pat.get("name", ""))

    elif module == "liuyao":
        bg = _d(result.get("benGua"))
        features.append(f"本卦 {bg.get('name','')} {bg.get('gong','')}宫")
        ys = _d(result.get("yongshen"))
        if ys.get("name"):
            features.append(f"用神 {ys['name']}")

    elif module == "meihua":
        ti = _d(result.get("ti"))
        yong = _d(result.get("yong"))
        features.append(f"体卦 {ti.get('name','')} {ti.get('wuxing','')}")
        features.append(f"用卦 {yong.get('name','')} {yong.get('wuxing','')}")
        ty = _d(result.get("tiYong"))
        if ty.get("relation"):
            features.append(ty["relation"])

    elif module == "qimen":
        features.append(result.get("type", ""))
        features.append(f"值符 {result.get('valueFu','')}")
        features.append(f"值使 {result.get('valueShi','')}")
        for p in _l(result.get("palaces")):
            if isinstance(p, dict) and p.get("star"):
                features.append(f"{p.get('dir','')} {p.get('star','')} {p.get('door','')}")

    elif module == "dream":
        features.append(result.get("dream", ""))

    elif module == "fengshui":
        features.append(result.get("dimension", ""))
        for s in _l(result.get("stars")):
            if isinstance(s, dict):
                features.append(f"{s.get('pos','')} {s.get('starName','')}")

    elif module == "tarot":
        # 塔罗：牌阵 + 问题 + 每张牌的正逆位与牌位，作为语义检索特征
        features.append(result.get("spreadName", ""))
        q = result.get("question")
        if isinstance(q, str) and q.strip():
            features.append(q.strip())
        for c in _l(result.get("cards")):
            if isinstance(c, dict) and c.get("name"):
                rev_flag = "逆位" if c.get("isRev") else "正位"
                features.append(f"{rev_flag}{c.get('name')} {c.get('pos','')}")

    elif module == "numerology":
        # 数字命理：生命灵数 + 核心数字（表现/内驱/人格/成熟）+ 挑战数 + 缺数 + 当年流年
        lp = result.get("lifePath")
        if isinstance(lp, int):
            data = _d(result.get("data"))
            features.append(f"生命灵数 {lp}")
            if data.get("name"):
                features.append(f"{data['name']} 生命灵数{lp}")
            if data.get("element"):
                features.append(f"{data['element']}象 生命灵数{lp}")
            if data.get("lesson"):
                features.append(f"生命灵数{lp} 课题 {data['lesson']}")
        core = _d(result.get("core"))
        for k in ("expression", "soulUrge", "personality", "maturity"):
            v = core.get(k)
            if isinstance(v, int):
                features.append(f"{k} {v}")
        ch = _d(core.get("challenge"))
        for k in ("c1", "c2", "c3", "c4"):
            v = ch.get(k)
            if isinstance(v, int):
                features.append(f"挑战数{k} {v}")
        missing = result.get("missing") or []
        if isinstance(missing, list) and missing:
            features.append("缺数 " + " ".join(str(x) for x in missing))
        years = _l(result.get("years"))
        if years and isinstance(years[0], dict) and isinstance(years[0].get("py"), int):
            features.append(f"流年 {years[0]['py']}")
        # 核心数字权重加倍，提升命中
        for _ in range(2):
            if isinstance(lp, int):
                features.append(f"生命灵数{lp}")
            ex = core.get("expression")
            if isinstance(ex, int):
                features.append(f"表现数{ex}")

    elif module == "horoscope":
        # 星座：日/月/升 + 行星落座与庙旺落陷 + 四象分布 + 主要相位
        sun = _d(result.get("sunSign"))
        moon = _d(result.get("moonSign"))
        asc = _d(result.get("ascendant"))
        mc = _d(result.get("midheaven"))
        if sun.get("sign"):
            features.append(f"太阳 {sun['sign']}")
        if moon.get("sign"):
            features.append(f"月亮 {moon['sign']}")
        if asc.get("sign"):
            features.append(f"上升 {asc['sign']}")
        if mc.get("sign"):
            features.append(f"中天 {mc['sign']}")
        for p in _l(result.get("planets")):
            if isinstance(p, dict) and p.get("sign"):
                label = f"{p.get('label','')} {p.get('sign','')}"
                features.append(label)
                dig = p.get("dignity")
                if dig:
                    features.append(f"{label} {dig}")
                if p.get("retrograde"):
                    features.append(f"{label} 逆行")
        elements = _d(result.get("elements"))
        for el, cnt in elements.items():
            if isinstance(cnt, int) and cnt >= 4:
                features.append(f"{el}象旺")
        for a in _l(result.get("aspects")):
            if isinstance(a, dict) and a.get("type"):
                features.append(f"{a.get('p1','')}{a.get('type','')}{a.get('p2','')}")
        # 日/月/升权重加倍
        for _ in range(2):
            if sun.get("sign"):
                features.append(f"太阳 {sun['sign']}")
        if asc.get("sign"):
            features.append(f"上升 {asc['sign']}")

    elif module == "liuren":
        # 大六壬：月将·占时·日辰·课体·三传·天将·贵人·空亡·伏吟反吟
        features.append("大六壬 六壬占法")
        if result.get("yueJiangName"):
            features.append(f"月将 {result.get('yueJiangName')} {result.get('yueJiang','')}")
        if result.get("zhanShi"):
            features.append(f"占时 {result.get('zhanShi')}")
        if result.get("riGanZhi"):
            features.append(f"日辰 {result.get('riGanZhi')}")
        san = _d(result.get("sanChuan"))
        if san.get("keTi"):
            features.append(f"课体 {san['keTi']}")
        if san.get("method"):
            features.append(f"发用 {san['method']}")
        for x in ("chu", "zhong", "mo"):
            if san.get(x):
                features.append(f"三传 {san[x]}")
        tj = result.get("tianJiang")
        if isinstance(tj, str) and tj:
            features.append(f"天将 {tj}")
        gr = _d(result.get("guiRen"))
        if gr.get("zhi"):
            features.append(f"贵人 {gr['zhi']}")
        kw = result.get("kongWang") or []
        if isinstance(kw, list) and kw:
            features.append("空亡 " + " ".join(str(x) for x in kw))
        if result.get("fuYin"):
            features.append("伏吟课 静守迟滞")
        if result.get("fanYin"):
            features.append("反吟课 反复变动")

    elif module == "taiyi":
        # 太乙神数：局数·阴阳遁·太乙居宫·主客算·文昌始击·主客大将·积年
        features.append("太乙神数 太乙式 太乙推算天时")
        if result.get("type"):
            features.append(result.get("type", ""))
        if isinstance(result.get("ju"), int):
            features.append(f"太乙第{result['ju']}局")
        if result.get("dun"):
            features.append(f"太乙 {result.get('dun','')}")
        tg = _d(result.get("taiYiGong"))
        if tg.get("pos"):
            features.append(f"太乙居宫 {tg['pos']} {tg.get('fang','')}")
        if isinstance(result.get("zhuSuan"), (int, float)):
            features.append(f"主算{result['zhuSuan']}")
        if isinstance(result.get("keSuan"), (int, float)):
            features.append(f"客算{result['keSuan']}")
        wc = _d(result.get("wenChang"))
        if wc.get("shen"):
            features.append(f"文昌 {wc['shen']}")
        sj = _d(result.get("shiJi"))
        if sj.get("shen"):
            features.append(f"始击 {sj['shen']}")
        zd = _d(result.get("zhuDaJiang"))
        if zd.get("pos"):
            features.append(f"主大将 {zd['pos']}")
        kd = _d(result.get("keDaJiang"))
        if kd.get("pos"):
            features.append(f"客大将 {kd['pos']}")
        if isinstance(result.get("jiNian"), int):
            features.append(f"太乙积年 {result['jiNian']}")

    # 去重并拼接
    features = list(dict.fromkeys(f for f in features if f))
    return " ".join(features) if features else ""
