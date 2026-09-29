"""ADR-011 E2E 验证脚本：知识库文档自动同步。

不依赖真实飞书与真实 LLM/embedding（NullEmbeddingClient 下向量腿自动禁用），
在临时目录构造文档源，直调 sync_once 验证链路行为：
  1. 首轮全量导入（空库 → 全部 created）
  2. 哈希幂等（内容未变 → 全部 skipped，无重复 embedding）
  3. 变更更新（改文件内容 → updated，块重建）
  4. 镜像删除（源文件删除 → KB 内 doc 删除，检索不再命中）
  5. 新增文档（新文件出现 → 下一轮 created）
  6. 前缀命名空间（带前缀的 FakeSource 与本地源互不误删）
  7. import_text 与 CLI 兼容（doc_id 相同不重复建条目，哈希跳过）
  8. 循环行为（start 后自动跑首轮，stop 干净退出）
  9. 单篇失败不阻断（坏文件 failed 计数，其余文档正常导入）
 10. 与 MemoryService 检索协同（同步后 search 命中新内容）

运行：cd backend && python scripts/e2e_kbsync.py
产出：scripts/e2e_kbsync_report.json（可重复验证 artifact）
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_tmp_db = Path("./data/e2e_kbsync_tmp.db")
if _tmp_db.exists():
    _tmp_db.unlink()

os.environ["MEMORY_DB_URL"] = "sqlite+aiosqlite:///./data/e2e_kbsync_tmp.db"
os.environ["CHROMA_PERSIST_DIR"] = "./data/e2e_kbsync_chroma"  # 隔离向量腿，防污染生产 chroma
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9")
os.environ.setdefault("LLM_API_KEY", "e2e-dummy")
os.environ.setdefault("CONSOLE_MODEL", "e2e-dummy")
os.environ.setdefault("SANDBOX_MODEL", "e2e-dummy")
os.environ.setdefault("FEISHU_APP_ID", "cli_e2e")
os.environ.setdefault("FEISHU_APP_SECRET", "e2e-secret")

from app.docs.knowledge import KnowledgeBase          # noqa: E402
from app.docs.syncer import KBSyncer, LocalDocsSource, SyncSource  # noqa: E402
from app.memory.service import SQLiteMemoryService    # noqa: E402

PASS: list[dict] = []
FAIL: list[dict] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    entry = {"name": name, "ok": bool(cond), "detail": detail}
    (PASS if cond else FAIL).append(entry)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  | {detail}" if detail and not cond else ""))


DOC_A_V1 = "# 文档A\n\n## 部署方式\n\n服务使用 Docker 部署，镜像名 opendomino-sandbox。\n"
DOC_A_V2 = "# 文档A\n\n## 部署方式\n\n服务改用 Podman 部署，镜像名 opendomino-podbox。\n"
DOC_B = "# 文档B\n\n## 备份策略\n\n数据库每日凌晨三点全量备份到对象存储。\n"
DOC_C = "# 文档C\n\n## 日志规范\n\n所有服务日志统一 JSON 格式输出到 stdout。\n"


class FakePrefixedSource(SyncSource):
    """带前缀的第二来源（模拟将来的飞书源），验证命名空间互不误删。"""

    name = "feishu"

    def __init__(self) -> None:
        self.docs: dict[str, str] = {"doc1": "# 云文档一\n\n## 要点\n\n云端内容正文。\n"}
        self.fail_ids: set[str] = set()  # fetch 时抛异常，模拟单篇失败

    async def list_docs(self) -> list[tuple[str, str]]:
        return [(k, "") for k in self.docs]

    async def fetch(self, doc_id: str) -> str:
        if doc_id in self.fail_ids:
            raise RuntimeError("simulated fetch failure")
        return self.docs[doc_id]


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="kbsync_e2e_"))
    report: dict = {"cases": []}

    try:
        kb = KnowledgeBase()
        await kb.init()
        src = LocalDocsSource(tmp)
        remote = FakePrefixedSource()
        syncer = KBSyncer([src, remote], kb)

        # ---- 1. 首轮全量导入 ----
        (tmp / "doc_a.md").write_text(DOC_A_V1, encoding="utf-8")
        (tmp / "doc_b.md").write_text(DOC_B, encoding="utf-8")
        stat = await syncer.sync_once()
        check("首轮全量导入 created=3", stat["created"] == 3, f"stat={stat}")
        check("首轮无删除", stat["deleted"] == 0, f"stat={stat}")
        ids = await kb.list_doc_ids()
        check("KB 含本地+前缀源共 3 篇", sorted(ids) == ["doc_a.md", "doc_b.md", "feishu/doc1"], f"ids={ids}")

        # ---- 2. 哈希幂等 ----
        stat = await syncer.sync_once()
        check("二轮全部 skipped", stat["created"] == 0 and stat["updated"] == 0
              and stat["skipped"] == 3, f"stat={stat}")

        # ---- 3. 变更更新 ----
        (tmp / "doc_a.md").write_text(DOC_A_V2, encoding="utf-8")
        stat = await syncer.sync_once()
        check("内容变更触发 updated", stat["updated"] == 1, f"stat={stat}")
        hits = await kb.search("Podman 部署")
        check("检索命中新内容", any("Podman" in h.content for h in hits), f"hits={[(h.doc_title, h.content[:30]) for h in hits]}")
        hits_old = await kb.search("opendomino-sandbox 镜像")
        check("旧内容不再命中 doc_a（仅 doc_b 无关或无命中）",
              all(not ("opendomino-sandbox" in h.content and "doc_a" in h.doc_title) for h in hits_old),
              f"hits={[(h.doc_title, h.content[:30]) for h in hits_old]}")

        # ---- 4. 镜像删除 ----
        (tmp / "doc_b.md").unlink()
        stat = await syncer.sync_once()
        check("缺席文档触发 deleted=1", stat["deleted"] == 1, f"stat={stat}")
        check("删除后 KB 只剩 2 篇", sorted(await kb.list_doc_ids()) == ["doc_a.md", "feishu/doc1"],
              f"ids={await kb.list_doc_ids()}")
        hits = await kb.search("全量备份 对象存储")
        check("已删文档检索不命中", all("doc_b" not in h.doc_title for h in hits),
              f"hits={[(h.doc_title, h.content[:30]) for h in hits]}")

        # ---- 5. 新增文档 ----
        (tmp / "sub").mkdir()
        (tmp / "sub" / "doc_c.md").write_text(DOC_C, encoding="utf-8")
        stat = await syncer.sync_once()
        check("新增文档 created=1（含子目录）", stat["created"] == 1, f"stat={stat}")

        # ---- 6. 前缀命名空间不误删 ----
        # feishu 源本轮仍提供 doc1，不应被镜像删除
        check("带前缀源文档存活", "feishu/doc1" in await kb.list_doc_ids(), f"ids={await kb.list_doc_ids()}")
        remote.docs.clear()  # 前缀源整体清空 → 只删 feishu/ 命名空间
        stat = await syncer.sync_once()
        check("前缀源清空只删前缀命名空间", stat["deleted"] == 1
              and sorted(await kb.list_doc_ids()) == ["doc_a.md", "sub/doc_c.md"],
              f"stat={stat} ids={await kb.list_doc_ids()}")

        # ---- 7. import_text 与 CLI 导入数据兼容 ----
        stat = await kb.import_text("doc_a.md", DOC_A_V2)  # 同 doc_id 同内容
        check("同 doc_id 同内容 CLI 侧 skipped（兼容不重复建条目）", stat["action"] == "skipped", f"stat={stat}")

        # ---- 9. 单篇失败不阻断（假源 fetch 抛异常；KB 中已有该篇时失败不误删）----
        remote.docs["doc1"] = "# 云文档一\n\n## 要点\n\n云端内容正文。\n"
        stat = await syncer.sync_once()  # 先成功同步一轮，让 feishu/doc1 回到 KB
        check("前缀源恢复后重新入库", "feishu/doc1" in await kb.list_doc_ids(),
              f"stat={stat} ids={await kb.list_doc_ids()}")
        remote.fail_ids.add("doc1")
        stat = await syncer.sync_once()
        check("fetch 失败 failed=1 其余 skipped", stat["failed"] == 1 and stat["skipped"] == 2, f"stat={stat}")
        check("失败篇不误删（仍在 KB）", "feishu/doc1" in await kb.list_doc_ids(), f"ids={await kb.list_doc_ids()}")
        remote.fail_ids.clear()
        # 本地源不可读文件：list_docs 层跳过 → 该篇视为缺席被镜像删除
        (tmp / "bad.md").write_bytes(b"\xff\xfe broken utf8 \xff")
        stat = await syncer.sync_once()
        check("坏文件不入 KB（list 层跳过后无残留）", "bad.md" not in await kb.list_doc_ids(),
              f"ids={await kb.list_doc_ids()}")

        # ---- 8. 循环行为：start 自动首轮 + stop 干净退出 ----
        kb2 = KnowledgeBase()
        await kb2.init()
        loop_syncer = KBSyncer([LocalDocsSource(tmp), FakePrefixedSource()], kb2)
        loop_syncer.start()
        await asyncio.sleep(0.5)  # 等首轮完成
        loop_ids = await kb2.list_doc_ids()
        check("start 后自动完成首轮导入", "doc_a.md" in loop_ids and "sub/doc_c.md" in loop_ids,
              f"ids={loop_ids}")
        await loop_syncer.stop()
        check("stop 后任务退出", loop_syncer._task is None or loop_syncer._task.done())

        # ---- 10. 与 MemoryService 检索协同 ----
        mem = SQLiteMemoryService()
        await mem.init()
        hits = await mem.search_knowledge("日志规范 JSON")
        check("MemoryService L3 检索命中同步内容", any("doc_c" in h.doc_title or "JSON" in h.content for h in hits),
              f"hits={[(h.doc_title, h.content[:30]) for h in hits]}")

        await kb._engine.dispose()
        await kb2._engine.dispose()
        await mem._engine.dispose()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    report["cases"] = PASS + FAIL
    report["pass"] = len(PASS)
    report["fail"] = len(FAIL)
    out = Path("./scripts/e2e_kbsync_report.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nreport -> {out.resolve()}")
    print(f"TOTAL: {len(PASS)} pass, {len(FAIL)} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
