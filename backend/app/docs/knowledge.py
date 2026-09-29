"""L3 知识库：文档导入与检索（ADR-002）。

- 文档表 docs（版本 + 内容哈希）与块表 doc_chunks（heading_path + FTS5）
- 向量进 Chroma collection "l3_chunks"（与 kbase_memories 隔离）
- 检索双路并行：FTS5(BM25) + 向量，归一化合并去重
- 导入幂等：内容哈希未变化直接跳过（不重复 embedding）
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
import uuid
from pathlib import Path
from sqlalchemy import Float, Integer, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import settings
from app.docs.chunker import Chunk, chunk_markdown, collect_warnings
from app.memory.embedding import EmbeddingClient, NullEmbeddingClient
from app.memory.types import KnowledgeHit

log = logging.getLogger(__name__)


class DocBase(DeclarativeBase):
    pass


class DocRow(DocBase):
    __tablename__ = "docs"

    doc_id: Mapped[str] = mapped_column(String(256), primary_key=True)  # 相对路径
    title: Mapped[str] = mapped_column(String(512), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    source_path: Mapped[str] = mapped_column(String(1024), default="")
    created_at: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[float] = mapped_column(Float, default=0.0)


class ChunkRow(DocBase):
    __tablename__ = "doc_chunks"

    chunk_id: Mapped[str] = mapped_column(String(320), primary_key=True)  # f"{doc_id}#{seq}"
    doc_id: Mapped[str] = mapped_column(String(256), default="")
    seq: Mapped[int] = mapped_column(Integer, default=0)
    heading_path: Mapped[str] = mapped_column(String(1024), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)


class KnowledgeBase:
    """L3 知识库服务。可独立引擎，也可复用 MemoryService 的引擎（传 db_url 一致即同库）。"""

    def __init__(self, db_url: str | None = None, embedding: EmbeddingClient | None = None) -> None:
        resolved = db_url or settings.memory_db_url
        self._engine = create_async_engine(resolved, echo=False)
        self._session = async_sessionmaker(self._engine, class_=AsyncSession, expire_on_commit=False)
        self._embed = embedding if embedding is not None else _shared_embedding_client()
        self._chroma_col = None

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(DocBase.metadata.create_all)
            await conn.exec_driver_sql(
                "CREATE VIRTUAL TABLE IF NOT EXISTS doc_chunks_fts USING fts5("
                "chunk_id UNINDEXED, heading_path, content)"
            )
        await self._init_chroma()

    async def _init_chroma(self) -> None:
        if isinstance(self._embed, NullEmbeddingClient):
            return
        try:
            import chromadb
            from chromadb.config import Settings as ChromaSettings

            persist = Path(settings.chroma_persist_dir)
            persist.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(
                path=str(persist), settings=ChromaSettings(anonymized_telemetry=False)
            )
            self._chroma_col = client.get_or_create_collection(
                "l3_chunks", metadata={"hnsw:space": "cosine"}
            )
            log.info("L3 chroma ready: vectors=%d", self._chroma_col.count())
        except Exception:  # noqa: BLE001
            log.exception("L3 chroma init failed, vector leg disabled")
            self._chroma_col = None

    # ---- 导入 ----

    async def import_document(self, path: Path, root: Path | None = None) -> dict:
        """导入单篇 Markdown 文件。文件版薄壳：读文件后走 import_text（ADR-011）。"""
        text = path.read_text(encoding="utf-8")
        doc_id = str(path.relative_to(root).as_posix()) if root else path.name
        return await self.import_text(doc_id, text, source_path=str(path))

    async def import_text(self, doc_id: str, text: str, *, source_path: str = "") -> dict:
        """按 doc_id 导入文本（ADR-011 核心入口）。哈希未变跳过；变化则失效旧块重建。"""
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        title = _extract_title(text)

        async with self._session() as s:
            row = await s.get(DocRow, doc_id)
            if row is not None and row.content_hash == content_hash:
                # 内容未变也可能缺向量（首轮索引失败/未启用 embedding），补齐后再跳过
                await self._ensure_vectors(row, text)
                return {"doc": doc_id, "action": "skipped", "chunks": 0, "warnings": []}
            if row is None:
                row = DocRow(doc_id=doc_id, title=title, content_hash=content_hash,
                             source_path=source_path, created_at=time.time())
                s.add(row)
                action = "created"
            else:
                row.version += 1
                row.title = title
                row.content_hash = content_hash
                action = "updated"
            row.updated_at = time.time()
            version = row.version

            # 旧块失效（FTS 行与 chunk 行）
            old = (await s.execute(select(ChunkRow).where(ChunkRow.doc_id == doc_id))).scalars().all()
            old_fts_ids = [c.chunk_id for c in old]
            for c in old:
                await s.delete(c)
            for cid in old_fts_ids:
                await s.execute(
                    sa_text("DELETE FROM doc_chunks_fts WHERE chunk_id = :cid"),
                    {"cid": cid},
                )

            chunks = chunk_markdown(text)
            for seq, ch in enumerate(chunks):
                cr = ChunkRow(
                    chunk_id=f"{doc_id}#{seq}", doc_id=doc_id, seq=seq,
                    heading_path=ch.heading_path, content=ch.content,
                    position=ch.start_offset, version=version,
                )
                s.add(cr)
                await s.execute(
                    sa_text("INSERT INTO doc_chunks_fts (chunk_id, heading_path, content) VALUES (:cid, :hp, :ct)"),
                    {"cid": cr.chunk_id, "hp": ch.heading_path, "ct": ch.content},
                )
            await s.commit()

        if self._chroma_col is not None:
            await self._index_vectors(doc_id, chunks, version, old_fts_ids)
        return {
            "doc": doc_id, "action": action, "chunks": len(chunks),
            "warnings": collect_warnings(chunks),
        }

    async def delete_document(self, doc_id: str) -> bool:
        """镜像删除（ADR-011）：文档缺席时删其行、块、FTS 行与向量。返回是否确有删除。"""
        async with self._session() as s:
            row = await s.get(DocRow, doc_id)
            if row is None:
                return False
            await s.delete(row)
            old = (await s.execute(select(ChunkRow).where(ChunkRow.doc_id == doc_id))).scalars().all()
            for c in old:
                await s.delete(c)
                await s.execute(
                    sa_text("DELETE FROM doc_chunks_fts WHERE chunk_id = :cid"),
                    {"cid": c.chunk_id},
                )
            await s.commit()

        if self._chroma_col is not None:
            try:
                self._chroma_col.delete(where={"doc_id": doc_id})
            except Exception:  # noqa: BLE001
                log.warning("chroma delete failed for %s (fts/sql legs deleted)", doc_id)
        return True

    async def list_doc_ids(self) -> list[str]:
        """知识库现有全部 doc_id（镜像删除的缺席判定用）。"""
        async with self._session() as s:
            rows = await s.execute(select(DocRow.doc_id))
            return [r[0] for r in rows.fetchall()]

    async def _index_vectors(self, doc_id: str, chunks: list[Chunk], version: int, old_chunk_ids: list[str]) -> None:
        if old_chunk_ids:
            try:
                self._chroma_col.delete(ids=[i for i in old_chunk_ids])
            except Exception:  # noqa: BLE001
                log.warning("chroma delete old chunks failed for %s", doc_id)
        if not chunks:
            return
        try:
            vecs = await self._embed.embed([c.content for c in chunks])
            if not vecs:
                return
            self._chroma_col.upsert(
                ids=[f"{doc_id}#{i}" for i in range(len(chunks))],
                embeddings=vecs,
                documents=[c.content for c in chunks],
                metadatas=[{"doc_id": doc_id, "seq": i, "version": version,
                            "heading_path": c.heading_path} for i, c in enumerate(chunks)],
            )
        except Exception:  # noqa: BLE001
            log.exception("L3 vector index failed for %s (fts leg still works)", doc_id)

    async def _ensure_vectors(self, row: DocRow, text: str) -> None:
        """哈希命中但向量缺失（首轮失败）时补索引。幂等。"""
        if self._chroma_col is None:
            return
        expected = self._chroma_col.get(where={"doc_id": row.doc_id})
        if expected["ids"]:
            return  # 向量已在
        chunks = chunk_markdown(text)
        await self._index_vectors(row.doc_id, chunks, row.version, [])
        log.info("backfilled vectors for %s: %d chunks", row.doc_id, len(chunks))

    # ---- 检索 ----

    async def search(self, query: str, *, limit: int = 8) -> list[KnowledgeHit]:
        """双路并行：FTS5 BM25 + 向量。归一化合并，两路都命中标 both 排前。"""
        if not (query or "").strip():
            return []
        fts_hits, vec_hits = await asyncio.gather(self._search_fts(query, 10), self._search_vector(query, 10))

        # 两路分数尺度不可比（BM25 归一化后仍偏 0.7+，向量常驻 0.4~0.55），
        # 用 rank 融合：每路第 k 名得 1/(60+k)（RRF, k=60 经验值），求和合并。
        merged: dict[str, tuple[float, KnowledgeHit, set[str]]] = {}

        def _key(h: KnowledgeHit) -> str:
            return f"{h.heading_path}|{h.content[:50]}"

        for hits in (fts_hits, vec_hits):
            for rank, hit in enumerate(hits, start=1):
                rrf = 1.0 / (60.0 + rank)
                key = _key(hit)
                if key not in merged:
                    merged[key] = (rrf, hit, {hit.source})
                else:
                    score, k, legs = merged[key]
                    legs.add(hit.source)
                    merged[key] = (score + rrf, k, legs)

        results: list[KnowledgeHit] = []
        for score, k, legs in merged.values():
            k.score = score * 20.0  # 归一化到展示友好区间（榜首约 0.033*2=0.65 量级不直观，改用相对值）
            if len(legs) > 1:
                k.source = "both"
            results.append(k)
        results.sort(key=lambda k: -k.score)
        # 分数映射：把 RRF 总分线性拉伸，第一名 ≈ 1.0
        if results:
            top = results[0].score
            if top > 0:
                for k in results:
                    k.score = round(k.score / top, 3)
        return results[:limit]

    @staticmethod
    def _fts_tokens(query: str) -> list[str]:
        """FTS5 unicode61 分词对中文整句是短语匹配语义——整句带"的/？"
        时零命中。因此查询侧先切 token：ASCII 词整取，连续 CJK 段切 2-gram
        （bigram 免词典、精度接近词级），去重保序。"""
        tokens: list[str] = []
        for seg in re.split(r"\s+", query.strip()):
            if not seg:
                continue
            if seg.isascii():
                tokens.append(seg)
                continue
            cjk = re.sub(r"[^\u4e00-\u9fff]", "", seg)
            if len(cjk) >= 2:
                tokens.extend(cjk[i : i + 2] for i in range(len(cjk) - 1))
            elif cjk:
                tokens.append(cjk)
        seen: set[str] = set()
        return [t for t in tokens if not (t in seen or seen.add(t))]

    async def _search_fts(self, query: str, limit: int) -> list[KnowledgeHit]:
        async with self._session() as s:
            tokens = self._fts_tokens(query)
            match = " OR ".join(f'"{t}"' for t in tokens) or '""'
            try:
                rows = (
                    await s.execute(
                        sa_text(
                            "SELECT c.chunk_id, c.heading_path, c.content, c.doc_id, "
                            "bm25(doc_chunks_fts) AS rank "
                            "FROM doc_chunks_fts f JOIN doc_chunks c ON c.chunk_id = f.chunk_id "
                            "WHERE doc_chunks_fts MATCH :q ORDER BY rank LIMIT :n"
                        ),
                        {"q": match, "n": limit},
                    )
                ).fetchall()
            except Exception:  # noqa: BLE001
                log.warning("fts query failed for %r", query)
                return []
            # bm25 越小越相关，归一化到 0~1（经验：rank 常见 -20 ~ 0）
            hits = []
            for r in rows:
                score = max(0.0, min(1.0, 1.0 + float(r.rank) / 20.0))
                hits.append(KnowledgeHit(
                    doc_title=r.doc_id, heading_path=r.heading_path, content=r.content,
                    doc_version="", source="fts", score=score,
                ))
            return hits

    async def _search_vector(self, query: str, limit: int) -> list[KnowledgeHit]:
        if self._chroma_col is None:
            return []
        try:
            vecs = await self._embed.embed([query])
            if not vecs:
                return []
            res = self._chroma_col.query(
                query_embeddings=[vecs[0]], n_results=min(limit, self._chroma_col.count() or 1),
                include=["distances", "metadatas", "documents"],
            )
            hits = []
            for i in range(len(res["ids"][0])):
                meta = res["metadatas"][0][i]
                hits.append(KnowledgeHit(
                    doc_title=meta.get("doc_id", ""),
                    heading_path=meta.get("heading_path", ""),
                    content=res["documents"][0][i],
                    doc_version=str(meta.get("version", "")),
                    source="vector",
                    score=max(0.0, 1.0 - float(res["distances"][0][i])),
                ))
            return hits
        except Exception:  # noqa: BLE001
            log.exception("L3 vector search failed")
            return []


def _extract_title(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _shared_embedding_client() -> EmbeddingClient:
    from app.memory.embedding import create_embedding_client
    return create_embedding_client()


import asyncio  # noqa: E402
import re  # noqa: E402
from sqlalchemy import text as sa_text  # noqa: E402
