"""L3 知识库文档自动同步（ADR-011）。

- SyncSource 抽象：同步循环只面向接口，不感知文档来自磁盘还是云端 API
- LocalDocsSource：第一期唯一实现，扫本地 docs 目录 *.md（doc_id 为相对路径）
- KBSyncer：lifespan 后台任务，启动先跑一轮再按间隔轮询；哈希幂等导入 +
  镜像删除（本轮缺席的 doc 删块删向量）
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.config import settings
from app.docs.knowledge import KnowledgeBase

log = logging.getLogger(__name__)


class SyncSource:
    """文档来源接口（ADR-011）。子类实现目录扫描或云 API 拉取。

    list_docs 返回的 doc_id 是来源内标识（如相对路径）；写入 KB 时带来源
    前缀作命名空间：无前缀源（本地目录）KB doc_id 即来源内标识，带前缀源
    （如飞书）KB doc_id 为 f"{name}/{doc_id}"。
    """

    name: str = ""  # 来源标识；空表示本地源（KB doc_id 不加前缀）

    async def list_docs(self) -> list[tuple[str, str]]:
        """本轮可见文档列表 [(doc_id, title)]。"""
        raise NotImplementedError

    async def fetch(self, doc_id: str) -> str:
        """按 doc_id 取文档全文文本。"""
        raise NotImplementedError


class LocalDocsSource(SyncSource):
    """本地目录源：递归扫 *.md，doc_id 为相对根目录的 POSIX 路径（与 CLI 兼容）。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    async def list_docs(self) -> list[tuple[str, str]]:
        from app.docs.knowledge import _extract_title

        out: list[tuple[str, str]] = []
        for f in sorted(self.root.rglob("*.md")):
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                log.warning("kbsync unreadable file skipped: %s", f)
                continue
            out.append((f.relative_to(self.root).as_posix(), _extract_title(text)))
        return out

    async def fetch(self, doc_id: str) -> str:
        return (self.root / doc_id).read_text(encoding="utf-8")


class KBSyncer:
    """知识库同步循环：每轮 list → 逐篇哈希幂等导入 → 镜像删除缺席文档。"""

    def __init__(self, sources: list[SyncSource], kb: KnowledgeBase) -> None:
        self._sources = sources
        self._kb = kb
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="kbsyncer")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        interval = max(5, settings.kbsync_interval_seconds)
        while not self._stop.is_set():
            try:
                await self.sync_once()
            except Exception:  # noqa: BLE001 —— 单轮失败不影响下一轮与主链路
                log.exception("kbsync round failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def sync_once(self) -> dict:
        """一轮全量同步。返回统计（供 E2E 断言与日志）。"""
        total = {"created": 0, "updated": 0, "skipped": 0, "failed": 0, "deleted": 0, "chunks": 0}
        claimed: set[str] = set()
        for src in self._sources:
            for doc_id, _title in await src.list_docs():
                # 带前缀来源的 KB doc_id 为 f"{src.name}/{doc_id}"（ADR-011 命名空间）
                full_id = f"{src.name}/{doc_id}" if src.name else doc_id
                try:
                    text = await src.fetch(doc_id)
                    stat = await self._kb.import_text(full_id, text, source_path=full_id)
                except Exception:  # noqa: BLE001 —— 单篇失败不阻断本轮其余文档
                    log.exception("kbsync import failed: %s/%s", src.name, doc_id)
                    total["failed"] += 1
                    # 列举到但拉取/导入失败：计入 claimed 保住 KB 旧内容，
                    # 防源端临时故障触发镜像误删（旧数据好过没数据）
                    claimed.add(full_id)
                    continue
                claimed.add(full_id)
                total[stat["action"]] = total.get(stat["action"], 0) + 1
                total["chunks"] += stat["chunks"]
                if stat["action"] != "skipped":
                    log.info("kbsync %s %s -> %d blocks", stat["action"], full_id, stat["chunks"])
        # 镜像删除：本轮没有任何来源声称的已有文档
        for doc_id in await self._kb.list_doc_ids():
            if doc_id in claimed:
                continue
            if await self._kb.delete_document(doc_id):
                total["deleted"] += 1
                log.info("kbsync deleted missing doc: %s", doc_id)
        log.info("kbsync round done: %s", total)
        return total


def build_default_syncer(kb: KnowledgeBase) -> KBSyncer | None:
    """按配置装配默认同步器：本地 docs 目录源。None 表示 kbsync 关闭。"""
    if not settings.kbsync_enabled:
        return None
    root = Path(settings.docs_root)
    if not root.is_dir():
        log.warning("kbsync docs root missing, syncer disabled: %s", root)
        return None
    return KBSyncer([LocalDocsSource(root)], kb)
