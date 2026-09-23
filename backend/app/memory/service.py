"""SQLite 实现的 MemoryService（ADR-002）。

L1 Session 用 SQLAlchemy + SQLite 持久化（含 pending 状态，重启可恢复）。
L2 KBase、L3 知识库先给空实现（返回空/占位），接口就位，后续接 ChromaDB/FTS5。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from sqlalchemy.engine import make_url
from sqlalchemy import Float, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import settings
from app.memory.types import (
    ChatMessage,
    KnowledgeHit,
    MemoryDraft,
    MemoryHit,
    MemoryStatus,
    PendingState,
    SessionContext,
)


class Base(DeclarativeBase):
    pass


class SessionRow(Base):
    __tablename__ = "sessions"

    chat_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), default="")
    conversation_id: Mapped[str] = mapped_column(String(128), default="")
    messages_json: Mapped[str] = mapped_column(Text, default="[]")
    pending_json: Mapped[str] = mapped_column(Text, default="")
    pending_memory_json: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    active_task_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    last_route_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[float] = mapped_column(Float, default=0.0)


class MemoryRow(Base):
    __tablename__ = "kbase_memories"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    statement: Mapped[str] = mapped_column(Text, default="")
    memory_type: Mapped[str] = mapped_column(String(64), default="")
    rationale: Mapped[str] = mapped_column(Text, default="")
    keywords_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(32), default=MemoryStatus.PENDING.value)
    source_session_id: Mapped[str] = mapped_column(String(64), default="")
    source_task_id: Mapped[str] = mapped_column(String(64), default="")
    created_by: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[float] = mapped_column(Float, default=0.0)


def _parse_keywords(keywords_json: str) -> list[str]:
    try:
        kws = json.loads(keywords_json or "[]")
        return [str(k) for k in kws if k]
    except (ValueError, TypeError):
        return []


def _session_from_row(row: SessionRow) -> SessionContext:
    msgs = [ChatMessage(**m) for m in json.loads(row.messages_json or "[]")]
    pending = PendingState(**json.loads(row.pending_json)) if row.pending_json else None
    pending_memory = (
        PendingState(**json.loads(row.pending_memory_json)) if row.pending_memory_json else None
    )
    return SessionContext(
        session_id=row.session_id,
        chat_id=row.chat_id,
        conversation_id=row.conversation_id,
        messages=msgs,
        pending=pending,
        pending_memory=pending_memory,
        summary=row.summary or "",
        active_task_ids=json.loads(row.active_task_ids_json or "[]"),
        last_route=json.loads(row.last_route_json or "{}"),
        updated_at=row.updated_at,
    )


class SQLiteMemoryService:
    """默认实现。L1 全量实现；L2 草稿/确认落库但检索暂为关键词兜底；L3 暂空。"""

    def __init__(self, db_url: str | None = None) -> None:
        resolved_url = db_url or settings.memory_db_url
        parsed = make_url(resolved_url)
        if parsed.get_backend_name() == "sqlite" and parsed.database not in (None, ":memory:"):
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_async_engine(resolved_url, echo=False)
        self._session = async_sessionmaker(self._engine, class_=AsyncSession, expire_on_commit=False)

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # create_all 不给已有表加列；轻量迁移补齐 Phase 0 新增列
            for ddl in (
                "ALTER TABLE sessions ADD COLUMN pending_memory_json Text DEFAULT ''",
                "ALTER TABLE sessions ADD COLUMN summary Text DEFAULT ''",
            ):
                try:
                    await conn.exec_driver_sql(ddl)
                except Exception:  # 列已存在
                    pass

    # ---- L1 ----

    async def load_session(self, chat_id: str) -> SessionContext:
        async with self._session() as s:
            row = await s.get(SessionRow, chat_id)
            if row is None:
                return SessionContext.new(chat_id)
            return _session_from_row(row)

    async def save_session(self, session: SessionContext) -> None:
        session.updated_at = time.time()
        async with self._session() as s:
            row = await s.get(SessionRow, session.chat_id)
            if row is None:
                row = SessionRow(chat_id=session.chat_id)
                s.add(row)
            row.session_id = session.session_id
            row.conversation_id = session.conversation_id
            row.messages_json = json.dumps(
                [vars(m) for m in session.messages], ensure_ascii=False
            )
            row.pending_json = json.dumps(vars(session.pending), ensure_ascii=False) if session.pending else ""
            row.pending_memory_json = (
                json.dumps(vars(session.pending_memory), ensure_ascii=False)
                if session.pending_memory else ""
            )
            row.summary = session.summary
            row.active_task_ids_json = json.dumps(session.active_task_ids, ensure_ascii=False)
            row.last_route_json = json.dumps(session.last_route, ensure_ascii=False)
            row.updated_at = session.updated_at
            await s.commit()

    # ---- L2 ----

    async def recall_kbase(self, query: str, *, limit: int = 5) -> list[MemoryHit]:
        """分级召回（ADR-002）：confirmed 条目数 ≤ 阈值时全量注入（小库阶段
        召回环节整体消失，规避中文子串匹配漏召回）；超过阈值才按关键词过滤。

        关键词召回的中文整句匹配局限已在真实环境验证（"提交代码" vs
        "代码合并前…"漏召回），向量检索在 Phase C 替换本实现。
        """
        q = (query or "").strip()
        async with self._session() as s:
            rows = (
                await s.execute(
                    select(MemoryRow).where(MemoryRow.status == MemoryStatus.CONFIRMED.value)
                )
            ).scalars().all()
        confirmed = [
            MemoryHit(
                id=r.id, title=r.title, statement=r.statement,
                memory_type=r.memory_type, rationale=r.rationale,
                status=MemoryStatus.CONFIRMED,
            )
            for r in rows
        ]
        if len(confirmed) <= settings.kbase_full_load_threshold:
            return confirmed[:limit]
        if not q:
            return []
        hits = []
        for r in rows:
            hay = f"{r.title}\n{r.statement}"
            keywords = _parse_keywords(r.keywords_json)
            if q in hay or any(tok and tok in hay for tok in q.split()) or any(k and k in q for k in keywords):
                hits.append(
                    MemoryHit(
                        id=r.id, title=r.title, statement=r.statement,
                        memory_type=r.memory_type, rationale=r.rationale,
                        status=MemoryStatus.CONFIRMED,
                    )
                )
        return hits[:limit]

    async def create_memory_draft(self, draft: MemoryDraft) -> str:
        import uuid

        mid = str(uuid.uuid4())
        async with self._session() as s:
            s.add(
                MemoryRow(
                    id=mid, title=draft.title, statement=draft.statement,
                    memory_type=draft.memory_type, rationale=draft.rationale,
                    keywords_json=json.dumps(draft.keywords, ensure_ascii=False),
                    status=MemoryStatus.PENDING.value,
                    source_session_id=draft.source_session_id,
                    source_task_id=draft.source_task_id,
                    created_at=time.time(), updated_at=time.time(),
                )
            )
            await s.commit()
        return mid

    async def confirm_memory(self, memory_id: str, actor_id: str) -> None:
        async with self._session() as s:
            row = await s.get(MemoryRow, memory_id)
            if row is not None:
                row.status = MemoryStatus.CONFIRMED.value
                row.created_by = actor_id
                row.updated_at = time.time()
                await s.commit()

    async def get_memory(self, memory_id: str) -> MemoryHit | None:
        async with self._session() as s:
            r = await s.get(MemoryRow, memory_id)
            if r is None:
                return None
            return MemoryHit(
                id=r.id, title=r.title, statement=r.statement,
                memory_type=r.memory_type, rationale=r.rationale,
                status=MemoryStatus(r.status),
            )

    async def delete_memory(self, memory_id: str) -> bool:
        """条目级 remove（ADR-002 写入治理）。拒绝/过期的 pending 草稿物理删除。"""
        async with self._session() as s:
            row = await s.get(MemoryRow, memory_id)
            if row is None:
                return False
            await s.delete(row)
            await s.commit()
            return True

    # ---- L3 ----

    async def search_knowledge(self, query: str, *, limit: int = 8) -> list[KnowledgeHit]:
        """第一阶段空实现：正式知识库检索待 ChromaDB+FTS5 接入。"""
        return []
