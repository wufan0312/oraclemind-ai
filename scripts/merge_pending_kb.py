"""将待审知识库条目幂等合并入生产库（oraclemind-ai-py）

用法：
    python scripts/merge_pending_kb.py            # 仅预览，不写库、不 ingest
    python scripts/merge_pending_kb.py --apply    # 合入 src/knowledge/*.jsonl 并重新 ingest

设计：
  - 仅处理 scripts/kb_pending_review.jsonl 中 pending_review=true 的条目
  - 按 module 归并到 src/knowledge/{module}_kb.jsonl
  - 以 md5(text) 去重，已存在则跳过（不重复入库）
  - 新条目写入时去掉 pending_review 标记，加 merged_at 时间戳
  - --apply 后用 subprocess 调 ingest_kb.py 重新入库（upsert 幂等，可反复跑）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PENDING = ROOT / "scripts" / "kb_pending_review.jsonl"
KB_DIR = ROOT / "src" / "knowledge"


def md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:16]


def load_pending() -> list[dict]:
    out = []
    if not PENDING.exists():
        return out
    for line in PENDING.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        if d.get("pending_review") is not True:
            continue
        out.append(d)
    return out


def existing_md5(path: Path) -> set[str]:
    s: set[str] = set()
    if not path.exists():
        return s
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("text"):
            s.add(md5(d["text"]))
    return s


def main() -> None:
    ap = argparse.ArgumentParser(description="将待审 KB 条目幂等合入生产库")
    ap.add_argument("--apply", action="store_true", help="真正写入 knowledge/ 并重新 ingest（不加则仅预览）")
    args = ap.parse_args()

    pending = load_pending()
    by_mod: dict[str, list[dict]] = {}
    for d in pending:
        by_mod.setdefault(d["module"], []).append(d)

    print(f"待审条目总数: {len(pending)}  | 覆盖模块: {len(by_mod)}")

    plan: list[tuple[str, Path, list[dict], list[dict]]] = []
    for mod, items in sorted(by_mod.items()):
        kb_file = KB_DIR / f"{mod}_kb.jsonl"
        have = existing_md5(kb_file)
        added, skipped = [], []
        for d in items:
            (skipped if md5(d["text"]) in have else added).append(d)
        plan.append((mod, kb_file, added, skipped))

    # 预览
    for mod, kb_file, added, skipped in plan:
        print(f"\n[{mod}] -> {kb_file.name}")
        print(f"    新增 {len(added)}  跳过(已存在) {len(skipped)}")
        for d in added:
            print(f"    + [{d.get('source', '')}] {d['text'][:28]}…")
    total_add = sum(len(a) for _, _, a, _ in plan)
    print(f"\n预计净新增: {total_add} 条")

    if not args.apply:
        print("\n(预览模式，未写入。加 --apply 执行合入 + 重新 ingest)")
        return

    # 写入 production
    ts = datetime.now().isoformat(timespec="seconds")
    for mod, kb_file, added, skipped in plan:
        if not added:
            continue
        lines = kb_file.read_text(encoding="utf-8").splitlines() if kb_file.exists() else []
        for d in added:
            rec = {
                "text": d["text"],
                "source": d.get("source", "未知"),
                "chapter": d.get("chapter", ""),
                "module": mod,
                "merged_at": ts,
            }
            lines.append(json.dumps(rec, ensure_ascii=False))
        kb_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"已写入 [{mod}] {len(added)} 条 -> {kb_file.name}")

    # 重新入库（ingest_kb 对全 knowledge/ 做 upsert，幂等）
    print("\n重新 ingest 知识库 ...")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "ingest_kb.py")], cwd=str(ROOT))
    if r.returncode != 0:
        print("ingest 失败，请检查 embedding 配置")
        sys.exit(r.returncode)
    print("合入完成 ✅")


if __name__ == "__main__":
    main()
