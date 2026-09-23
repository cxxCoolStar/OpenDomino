"""知识库导入 CLI（ADR-002 L3）。

用法：
    cd backend
    python -m app.docs.import_docs ../docs          # 目录递归导入 *.md
    python -m app.docs.import_docs ../docs/handoff.md  # 单文件

行为：内容哈希幂等；变化才重切块重 embedding；输出统计与规范违规报告。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path


async def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m app.docs.import_docs <docs_dir_or_file>")
        return 2
    target = Path(sys.argv[1]).resolve()
    root = target if target.is_dir() else (target.parent.parent if "docs" in target.parts else target.parent)
    # 以 OpenDomino 仓库的 docs 目录为根推导 doc_id（保持相对路径稳定性）
    if target.is_dir():
        root = target
        files = sorted(target.rglob("*.md"))
    else:
        # 找到路径中最后出现的 docs 目录作为根
        parts = target.parts
        if "docs" in parts:
            docs_idx = len(parts) - 1 - parts[::-1].index("docs")
            root = Path(*parts[: docs_idx + 1])
        else:
            root = target.parent
        files = [target]

    from app.docs.knowledge import KnowledgeBase

    kb = KnowledgeBase()
    await kb.init()

    total = {"created": 0, "updated": 0, "skipped": 0, "chunks": 0}
    warnings: list[tuple[str, str]] = []
    for f in files:
        try:
            stat = await kb.import_document(f, root=root)
        except Exception as e:  # noqa: BLE001
            print(f"  [FAILED] {f.name}: {e}")
            continue
        total[stat["action"]] = total.get(stat["action"], 0) + 1
        total["chunks"] += stat["chunks"]
        mark = {"created": "+", "updated": "~", "skipped": "="}[stat["action"]]
        print(f"  [{mark}] {stat['doc']} -> {stat['chunks']} blocks")
        for w in stat["warnings"]:
            warnings.append((stat["doc"], w))

    print(
        f"\ndone: {total['created']} created, {total['updated']} updated, "
        f"{total['skipped']} skipped, {total['chunks']} blocks total"
    )
    if warnings:
        print(f"\n规范违规 {len(warnings)} 处（不影响导入，建议修文档）：")
        for doc, w in warnings[:20]:
            print(f"  - [{doc}] {w}")
    await kb._engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
