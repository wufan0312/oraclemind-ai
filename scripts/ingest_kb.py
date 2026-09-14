"""知识库导入脚本 — 将命理典籍导入 Chroma 向量数据库

用法：
    python scripts/ingest_kb.py              # 导入 src/knowledge 下所有 .jsonl
    python scripts/ingest_kb.py --file data/bazi_refs.jsonl

语料以 JSONL 形式维护在 src/knowledge/ 下（一行一条，可版本管理）：
    {"text": "原文内容", "source": "滴天髓", "chapter": "通神论", "module": "bazi"}

文档 ID 取正文摘要，因此**重复导入是幂等的** —— 同一条语料不会存出多份，
改完语料重跑一次即可覆盖旧内容。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from pathlib import Path

# 将项目根目录加入 path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services.retrieval import get_collection

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def ingest_jsonl(file_path: str):
    """从 JSONL 文件导入典籍

    JSONL 格式（每行一条）：
    {"text": "原文内容", "source": "滴天髓", "chapter": "通神论", "module": "bazi"}
    """
    coll = get_collection()
    if coll is None:
        logger.error("Chroma 集合初始化失败，请检查 embedding 配置")
        return

    count = 0
    batch_ids = []
    batch_texts = []
    batch_metas = []

    with open(file_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            text = obj.get("text", "").strip()
            if not text:
                continue

            # 稳定 ID：同一条语料反复导入仍是同一条记录（upsert 覆盖而非堆积）
            doc_id = hashlib.md5(text.encode("utf-8")).hexdigest()[:16]
            meta = {
                "source": obj.get("source", "未知"),
                "chapter": obj.get("chapter", ""),
                "module": obj.get("module", "general"),
            }

            batch_ids.append(doc_id)
            batch_texts.append(text)
            batch_metas.append(meta)
            count += 1

            # 每 100 条批量写入
            if len(batch_ids) >= 100:
                coll.upsert(ids=batch_ids, documents=batch_texts, metadatas=batch_metas)
                logger.info(f"已导入 {count} 条...")
                batch_ids, batch_texts, batch_metas = [], [], []

    # 剩余
    if batch_ids:
        coll.upsert(ids=batch_ids, documents=batch_texts, metadatas=batch_metas)

    logger.info(f"导入完成，共 {count} 条典籍")


def ingest_demo():
    """导入演示数据（无 JSONL 文件时使用）"""
    coll = get_collection()
    if coll is None:
        logger.error("Chroma 集合初始化失败")
        return

    demo_data = [
        {"text": "戊子日柱，俗云人间没有穷戊子，戊土坐子水正财，财星自旺，多主富足。",
         "source": "三命通会", "chapter": "日柱断", "module": "bazi"},
        {"text": "戊癸合化火，为中正之合，主信厚威严，主人聪明有德。",
         "source": "滴天髓", "chapter": "天干五合", "module": "bazi"},
        {"text": "子丑合土，为泥合，主暗昧不明，凡事多阻。",
         "source": "渊海子平", "chapter": "地支六合", "module": "bazi"},
        {"text": "杂气印绶格，辰戌丑未四库全，印星藏于库中，逢冲则发。",
         "source": "子平真诠", "chapter": "杂气格", "module": "bazi"},
        {"text": "紫微在子宫，为水形，主性格温和、聪明、有主见。",
         "source": "紫微斗数全书", "chapter": "星曜赋", "module": "ziwei"},
        {"text": "六爻用神旺相得生扶，事有可成；休囚逢冲克，事难成。",
         "source": "卜筮正宗", "chapter": "用神赋", "module": "liuyao"},
        {"text": "梅花易数体用相生则吉，体克用则吉，用克体则凶。",
         "source": "梅花易数", "chapter": "体用论", "module": "meihua"},
        {"text": "奇门遁甲开休生三吉门，死惊伤三凶门，景杜中平。",
         "source": "烟波钓叟歌", "chapter": "八门诀", "module": "qimen"},
    ]

    ids = [f"demo_{i}" for i in range(len(demo_data))]
    texts = [d["text"] for d in demo_data]
    metas = [{"source": d["source"], "chapter": d["chapter"], "module": d["module"]} for d in demo_data]

    coll.upsert(ids=ids, documents=texts, metadatas=metas)
    logger.info(f"演示数据导入完成，共 {len(demo_data)} 条")


def main():
    parser = argparse.ArgumentParser(description="将命理典籍导入 Chroma 向量数据库")
    parser.add_argument("--file", type=str, help="JSONL 文件路径")
    args = parser.parse_args()

    if args.file:
        ingest_jsonl(args.file)
        return

    # 默认：扫描 src/knowledge 下所有 .jsonl，按文件名排序逐个导入
    kb_dir = Path(__file__).parent.parent / "src" / "knowledge"
    files = sorted(kb_dir.glob("*.jsonl"))
    if not files:
        default = Path("data/kb.jsonl")
        if default.exists():
            ingest_jsonl(str(default))
        else:
            logger.info("未找到知识库文件，导入演示数据...")
            ingest_demo()
        return

    for f in files:
        logger.info(f"=== 导入 {f.name} ===")
        ingest_jsonl(str(f))


if __name__ == "__main__":
    main()
