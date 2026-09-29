"""SQLite 实现的 MemoryService（ADR-002）。

L1 Session 用 SQLAlchemy + SQLite 持久化（含 pending 状态，重启可恢复）。
L2 KBase 草稿/确认落库；召回三级递进（Phase C）：
  1. 向量召回（EMBEDDING_MODEL 已配置且条目数 > kbase_full_load_threshold）
  2. 关键词召回（未配置 embedding）
  3. 全量注入（confirmed 条目数 ≤ kbase_full_load_threshold，召回环节消失）
L3 知识库经 search_knowledge 委托 KnowledgeBase（FTS5 + 向量双路，见 app/docs/knowledge.py）。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from sqlalchemy.engine import make_url
from sqlalchemy import Float, String, Text, delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import settings
from app.memory.repo_types import Repo, new_repo_id

log = logging.getLogger(__name__)
from app.memory.embedding import (
    EmbeddingClient,
    NullEmbeddingClient,
    create_embedding_client,
)
from app.memory.types import (
    ChatMessage,
    DistillState,
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
    supersedes_id: Mapped[str] = mapped_column(String(64), default="")   # ADR-009：替代的旧条目
    source_type: Mapped[str] = mapped_column(String(32), default="")     # manual | correction | overheard
    source_chat_id: Mapped[str] = mapped_column(String(128), default="") # 来源群（overheard 溯源）
    created_by: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[float] = mapped_column(Float, default=0.0)


class RepoRow(Base):
    """仓库实体（M:N 的 M 端）：全局唯一，档案只存一份。凭据不在此表（走部署环境）。"""

    __tablename__ = "repos"

    repo_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    url: Mapped[str] = mapped_column(String(512), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    default_test_cmd: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[float] = mapped_column(Float, default=0.0)


class GroupRepoBindingRow(Base):
    """群-仓库关系（M:N 的 N 端）：(chat_id, repo_id) 唯一；群级 test_cmd 可覆盖实体默认值。"""

    __tablename__ = "group_repo_bindings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(128), index=True)
    repo_id: Mapped[str] = mapped_column(String(64), index=True)
    test_cmd_override: Mapped[str] = mapped_column(Text, default="")
    bound_by: Mapped[str] = mapped_column(String(128), default="")
    bound_at: Mapped[float] = mapped_column(Float, default=0.0)


class ObservedMessageRow(Base):
    """旁听消息（ADR-008）：非 @ 群消息与回拉历史，全量落库、存而不检索。

    message_id 主键天然幂等——通道去重集合重启丢失后，回拉/重放靠 INSERT OR REPLACE 兜底。
    received_at 为本地接收时间（ADR-009 蒸馏空闲判定与写前校验用，与飞书消息时间戳解耦）。
    """

    __tablename__ = "observed_messages"

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[str] = mapped_column(String(128), default="")
    user_name: Mapped[str] = mapped_column(String(128), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    timestamp: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    received_at: Mapped[float] = mapped_column(Float, default=0.0)


class DistillStateRow(Base):
    """旁听蒸馏游标（ADR-009）：per-chat 水位线与冷却锚点。"""

    __tablename__ = "distill_state"

    chat_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    watermark_ts: Mapped[float] = mapped_column(Float, default=0.0)
    last_success_at: Mapped[float] = mapped_column(Float, default=0.0)
    last_failure_at: Mapped[float] = mapped_column(Float, default=0.0)


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
    """默认实现。L1 全量实现；L2 草稿/确认落库 + 分级召回；L3 委托 KnowledgeBase（FTS5+向量双路）。"""

    def __init__(self, db_url: str | None = None, embedding: EmbeddingClient | None = None) -> None:
        resolved_url = db_url or settings.memory_db_url
        parsed = make_url(resolved_url)
        if parsed.get_backend_name() == "sqlite" and parsed.database not in (None, ":memory:"):
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_async_engine(resolved_url, echo=False)
        self._session = async_sessionmaker(self._engine, class_=AsyncSession, expire_on_commit=False)
        self._embed = embedding if embedding is not None else create_embedding_client()
        self._chroma = None       # 惰性初始化（init() 中）
        self._chroma_col = None
        self._knowledge: KnowledgeBase | None = None  # L3 惰性初始化（首次 search）

    async def aclose(self) -> None:
        """释放引擎与向量库句柄；Windows 下不释放会导致 SQLite/Chroma 文件无法删除。"""
        if self._chroma is not None:
            self._chroma = None
            self._chroma_col = None
        await self._engine.dispose()

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # create_all 不给已有表加列；轻量迁移补齐新增列（ADR-009）
            for ddl in (
                "ALTER TABLE sessions ADD COLUMN pending_memory_json Text DEFAULT ''",
                "ALTER TABLE sessions ADD COLUMN summary Text DEFAULT ''",
                "ALTER TABLE observed_messages ADD COLUMN received_at Float DEFAULT 0",
                "ALTER TABLE kbase_memories ADD COLUMN supersedes_id String(64) DEFAULT ''",
                "ALTER TABLE kbase_memories ADD COLUMN source_type String(32) DEFAULT ''",
                "ALTER TABLE kbase_memories ADD COLUMN source_chat_id String(128) DEFAULT ''",
            ):
                try:
                    await conn.exec_driver_sql(ddl)
                except Exception:  # 列已存在
                    pass
        await self._init_chroma()

    async def _init_chroma(self) -> None:
        """embedding 未配置时跳过；ChromaDB 持久化目录独立于 SQLite。"""
        if isinstance(self._embed, NullEmbeddingClient):
            return
        try:
            import chromadb
            from chromadb.config import Settings as ChromaSettings

            persist = Path(settings.chroma_persist_dir)
            persist.mkdir(parents=True, exist_ok=True)
            self._chroma = chromadb.PersistentClient(
                path=str(persist), settings=ChromaSettings(anonymized_telemetry=False)
            )
            self._chroma_col = self._chroma.get_or_create_collection(
                "kbase_memories", metadata={"hnsw:space": "cosine"}
            )
            log.info("chroma ready: dir=%s vectors=%d", persist, self._chroma_col.count())
        except Exception:  # noqa: BLE001 —— 向量库故障不阻塞记忆层
            log.exception("chroma init failed, vector recall disabled")
            self._chroma = None
            self._chroma_col = None

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
        """三级召回（ADR-002 Phase C）：

        1. confirmed 条目 ≤ kbase_full_load_threshold → 全量注入（召回环节消失）；
        2. 超阈值且 embedding 已配置 → ChromaDB 向量召回（只取 confirmed）；
        3. 超阈值且未配置 embedding → 关键词召回（中文整句匹配有局限，Phase C 前的兜底）。
        """
        q = (query or "").strip()
        async with self._session() as s:
            rows = (
                await s.execute(
                    select(MemoryRow).where(MemoryRow.status == MemoryStatus.CONFIRMED.value)
                )
            ).scalars().all()

        def _hit(r: MemoryRow, score: float = 0.0) -> MemoryHit:
            return MemoryHit(
                id=r.id, title=r.title, statement=r.statement,
                memory_type=r.memory_type, rationale=r.rationale,
                score=score, status=MemoryStatus.CONFIRMED,
            )

        if len(rows) <= settings.kbase_full_load_threshold:
            return [_hit(r) for r in rows][:limit]

        if self._chroma_col is not None and q:
            try:
                vecs = await self._embed.embed([q])
                if vecs:
                    res = self._chroma_col.query(
                        query_embeddings=[vecs[0]],
                        n_results=min(limit, self._chroma_col.count()) or 1,
                        include=["distances", "metadatas"],
                    )
                    ids = res["ids"][0]
                    distances = res["distances"][0]
                    by_id = {r.id: r for r in rows}
                    hits: list[MemoryHit] = []
                    for mid, dist in zip(ids, distances):
                        # Chroma 只存 confirmed（写入/删除同步），但防御性再查一次 SQLite
                        if mid in by_id:
                            hits.append(_hit(by_id[mid], score=1.0 - float(dist)))
                    return hits
            except Exception:  # noqa: BLE001 —— 向量召回失败降级关键词
                log.exception("vector recall failed, falling back to keyword")

        if not q:
            return []
        hits = []
        for r in rows:
            hay = f"{r.title}\n{r.statement}"
            keywords = _parse_keywords(r.keywords_json)
            if q in hay or any(tok and tok in hay for tok in q.split()) or any(k and k in q for k in keywords):
                hits.append(_hit(r))
        return hits[:limit]

    async def create_memory_draft(self, draft: MemoryDraft) -> str:
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
                    supersedes_id=draft.supersedes_id,
                    source_type=draft.source_type,
                    source_chat_id=draft.source_chat_id,
                    created_at=time.time(), updated_at=time.time(),
                )
            )
            await s.commit()
        return mid

    async def confirm_memory(self, memory_id: str, actor_id: str) -> None:
        """确认条目；带 supersedes_id 时将旧条目转 archived（ADR-009 过时内容处理）。

        supersedes_id 指向的旧条目缺失或已非 confirmed 时丢弃替代语义按普通新条目
        处理（重复确认幂等：旧条目已是 archived 则不再满足条件）。归档成功后保留
        本条目的 supersedes_id 反向指针，保留「这条替代了哪条」的审计链。
        """
        async with self._session() as s:
            row = await s.get(MemoryRow, memory_id)
            if row is not None:
                row.status = MemoryStatus.CONFIRMED.value
                row.created_by = actor_id
                row.updated_at = time.time()
                if row.supersedes_id:
                    old = await s.get(MemoryRow, row.supersedes_id)
                    if old is not None and old.status == MemoryStatus.CONFIRMED.value:
                        old.status = MemoryStatus.ARCHIVED.value
                        old.updated_at = time.time()
                else:
                    row.supersedes_id = ""
                await s.commit()
        await self._index_memory_vector(memory_id)

    async def _index_memory_vector(self, memory_id: str) -> None:
        """confirmed 条目补向量进 Chroma；失败不影响确认主流程。"""
        if self._chroma_col is None:
            return
        hit = await self.get_memory(memory_id)
        if hit is None:
            return
        try:
            vecs = await self._embed.embed([f"{hit.title}\n{hit.statement}"])
            if not vecs:
                return
            self._chroma_col.upsert(
                ids=[memory_id],
                embeddings=[vecs[0]],
                documents=[f"{hit.title}\n{hit.statement}"],
                metadatas=[{"memory_type": hit.memory_type, "status": hit.status.value}],
            )
        except Exception:  # noqa: BLE001
            log.exception("vector index failed for memory %s (recall degrades)", memory_id)

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
        if self._chroma_col is not None:
            try:
                self._chroma_col.delete(ids=[memory_id])
            except Exception:  # noqa: BLE001
                log.warning("chroma delete failed for %s", memory_id)
        return True

    # ---- 仓库注册与群绑定（M:N，ADR-003 群仓库绑定的 L2 落地）----

    async def bind_repo(
        self, chat_id: str, name: str, url: str, *,
        description: str = "", test_cmd: str = "", bound_by: str = "",
    ) -> Repo:
        """绑定即注册：仓库不存在则创建实体；(chat_id, repo_id) 唯一，重复绑定幂等返回。"""
        name = name.strip()
        async with self._session() as s:
            row = (await s.execute(select(RepoRow).where(RepoRow.name == name))).scalar_one_or_none()
            if row is None:
                row = RepoRow(
                    repo_id=new_repo_id(), name=name, url=url,
                    description=description, default_test_cmd=test_cmd,
                    created_by=bound_by, created_at=time.time(),
                )
                s.add(row)
                await s.flush()
            binding = (
                await s.execute(
                    select(GroupRepoBindingRow).where(
                        GroupRepoBindingRow.chat_id == chat_id,
                        GroupRepoBindingRow.repo_id == row.repo_id,
                    )
                )
            ).scalar_one_or_none()
            if binding is None:
                s.add(GroupRepoBindingRow(
                    id=uuid.uuid4().hex, chat_id=chat_id, repo_id=row.repo_id,
                    test_cmd_override=test_cmd, bound_by=bound_by, bound_at=time.time(),
                ))
            else:  # 幂等：重复绑定刷新覆盖命令
                binding.test_cmd_override = test_cmd or binding.test_cmd_override
            await s.commit()
            return Repo(
                repo_id=row.repo_id, name=row.name, url=row.url,
                description=row.description, default_test_cmd=row.default_test_cmd,
                created_by=row.created_by, created_at=row.created_at,
            )

    async def unbind_repo(self, chat_id: str, name: str) -> bool:
        """解绑：只删关系不删仓库实体。返回是否发生了解绑。"""
        async with self._session() as s:
            row = (await s.execute(select(RepoRow).where(RepoRow.name == name.strip()))).scalar_one_or_none()
            if row is None:
                return False
            binding = (
                await s.execute(
                    select(GroupRepoBindingRow).where(
                        GroupRepoBindingRow.chat_id == chat_id,
                        GroupRepoBindingRow.repo_id == row.repo_id,
                    )
                )
            ).scalar_one_or_none()
            if binding is None:
                return False
            await s.delete(binding)
            await s.commit()
            return True

    async def list_repo_bindings(self, chat_id: str) -> list[Repo]:
        """本群绑定的全部仓库（按绑定时间序）。"""
        async with self._session() as s:
            rows = (
                await s.execute(
                    select(RepoRow)
                    .join(GroupRepoBindingRow, GroupRepoBindingRow.repo_id == RepoRow.repo_id)
                    .where(GroupRepoBindingRow.chat_id == chat_id)
                    .order_by(GroupRepoBindingRow.bound_at.asc())
                )
            ).scalars().all()
            return [
                Repo(
                    repo_id=r.repo_id, name=r.name, url=r.url,
                    description=r.description, default_test_cmd=r.default_test_cmd,
                    created_by=r.created_by, created_at=r.created_at,
                )
                for r in rows
            ]

    async def resolve_repo(self, chat_id: str, name: str) -> Repo | None:
        """委派解析：本群是否绑定了该名字的仓库（含大小写不敏感匹配）。"""
        name = name.strip()
        bindings = await self.list_repo_bindings(chat_id)
        for repo in bindings:
            if repo.name.lower() == name.lower():
                return repo
        return None

    # ---- 旁听缓冲（ADR-008）----

    async def save_observed(self, chat_id: str, messages: list[ChatMessage]) -> int:
        """批量落旁听消息，按 message_id 幂等（已存在的跳过）。返回新增条数。

        received_at 取消息到达本函数的时刻（本地接收时间，ADR-009）。
        """
        added = 0
        now = time.time()
        async with self._session() as s:
            for m in messages:
                if not m.message_id or not m.text:
                    continue
                exists = (
                    await s.execute(
                        select(ObservedMessageRow).where(
                            ObservedMessageRow.message_id == m.message_id
                        )
                    )
                ).scalar_one_or_none()
                if exists is not None:
                    continue
                s.add(ObservedMessageRow(
                    message_id=m.message_id, chat_id=chat_id,
                    user_id=m.user_id, user_name=m.user_name,
                    text=m.text, timestamp=m.timestamp, received_at=now,
                ))
                added += 1
            await s.commit()
        return added

    async def load_observed(
        self, chat_id: str, *, since: float = 0.0, limit: int = 200,
    ) -> list[ChatMessage]:
        """读取本群旁听消息（时间升序）。since 为Unix秒下限，limit 取最新 N 条。"""
        async with self._session() as s:
            q = (
                select(ObservedMessageRow)
                .where(ObservedMessageRow.chat_id == chat_id)
                .order_by(ObservedMessageRow.timestamp.desc())
            )
            if since > 0:
                q = q.where(ObservedMessageRow.timestamp >= since)
            result = await s.execute(q.limit(limit))
            # desc 取最新 N 条后翻回时间正序，供时间线合并
            rows = list(result.scalars().all())
            rows.reverse()
            return [
                ChatMessage(
                    role="user", text=r.text, user_id=r.user_id,
                    user_name=r.user_name, message_id=r.message_id, timestamp=r.timestamp,
                )
                for r in rows
            ]

    async def load_undistilled(
        self, chat_id: str, watermark_ts: float, *, limit: int = 200,
    ) -> list[ChatMessage]:
        """读取水位线之后的旁听消息（最老优先，ADR-009 蒸馏输入）。

        水位线按消息时间戳 timestamp 比较——与 prune 的「已蒸才可清」判定同一
        维度。首见回拉存量 timestamp 恒老于水位线，天然落在已覆盖侧。跨端时钟偏差
        的最坏影响是边界消息重复进入下一批（LLM 再看一次，无正确性影响）。
        与 load_observed 的「最新 N 条」语义相反：蒸馏必须覆盖最老的未蒸消息，
        积压时借后续周期逐批推进。
        """
        async with self._session() as s:
            rows = (
                await s.execute(
                    select(ObservedMessageRow)
                    .where(
                        ObservedMessageRow.chat_id == chat_id,
                        ObservedMessageRow.timestamp > watermark_ts,
                    )
                    .order_by(ObservedMessageRow.timestamp.asc())
                    .limit(limit)
                )
            ).scalars().all()
            return [
                ChatMessage(
                    role="user", text=r.text, user_id=r.user_id,
                    user_name=r.user_name, message_id=r.message_id, timestamp=r.timestamp,
                )
                for r in rows
            ]

    async def last_undistilled_timestamp(self, chat_id: str, watermark_ts: float) -> float | None:
        """水位线之后最新一条旁听消息的消息时间戳（水位线推进目标值，同维度单调推进）。"""
        async with self._session() as s:
            row = (
                await s.execute(
                    select(ObservedMessageRow.timestamp)
                    .where(
                        ObservedMessageRow.chat_id == chat_id,
                        ObservedMessageRow.timestamp > watermark_ts,
                    )
                    .order_by(ObservedMessageRow.timestamp.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            return row

    async def count_undistilled(self, chat_id: str, watermark_ts: float) -> int:
        """水位线之后的旁听消息数（活跃兜底触发判定用）。"""
        async with self._session() as s:
            row = await s.execute(
                select(ObservedMessageRow.message_id)
                .where(
                    ObservedMessageRow.chat_id == chat_id,
                    ObservedMessageRow.timestamp > watermark_ts,
                )
            )
            return len(row.all())

    async def last_observed_received_at(self, chat_id: str) -> float | None:
        """本群最新旁听消息的本地接收时间；群无任何旁听消息时返回 None。"""
        async with self._session() as s:
            row = (
                await s.execute(
                    select(ObservedMessageRow.received_at)
                    .where(ObservedMessageRow.chat_id == chat_id)
                    .order_by(ObservedMessageRow.timestamp.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            return row

    async def prune_observed(
        self, *, retain_days: int, respect_watermark: bool = False,
    ) -> int:
        """保留期清理：删除 retain_days 天前的旁听消息。返回删除条数。

        年龄判定按消息时间戳 timestamp（消息龄，ADR-008 保留期语义）。respect_watermark=True
        （ADR-009）时叠加第二条件「timestamp 在水位线之前」——与蒸馏范围查询同一
        维度：不在任何未蒸馏集合里的消息才可清，未蒸馏的消息不论多老都保留，
        宁可缓冲变大也不静默丢数据；水位线卡住时清理自然停滞，蒸馏恢复后追赶。
        开关关闭时退回纯年龄清理（E2E/关闭蒸馏场景）。
        """
        cutoff = time.time() - retain_days * 86400
        async with self._session() as s:
            if respect_watermark:
                watermarks = (
                    await s.execute(select(DistillStateRow.chat_id, DistillStateRow.watermark_ts))
                ).all()
                wm_map = {chat_id: wm for chat_id, wm in watermarks}
                chat_ids = (
                    await s.execute(select(ObservedMessageRow.chat_id).distinct())
                ).scalars().all()
                deleted = 0
                for chat_id in chat_ids:
                    wm = wm_map.get(chat_id)
                    # 该群尚无水位线记录（未初始化/未启用蒸馏）：整体不清理，避免误删
                    if wm is None:
                        continue
                    result = await s.execute(
                        delete(ObservedMessageRow).where(
                            ObservedMessageRow.chat_id == chat_id,
                            ObservedMessageRow.timestamp < cutoff,
                            ObservedMessageRow.timestamp < wm,
                        )
                    )
                    deleted += result.rowcount or 0
                await s.commit()
                return deleted
            result = await s.execute(
                delete(ObservedMessageRow).where(ObservedMessageRow.timestamp < cutoff)
            )
            await s.commit()
            return result.rowcount or 0

    async def list_observed_chats(self) -> list[str]:
        """重启补拉用：出现过旁听消息的群列表。"""
        async with self._session() as s:
            rows = await s.execute(select(ObservedMessageRow.chat_id).distinct())
            return [r[0] for r in rows.all()]

    # ---- 蒸馏游标（ADR-009）----

    async def get_distill_state(
        self, chat_id: str, *, init_watermark: float | None = None,
    ) -> DistillState:
        """取蒸馏游标；首见按 init_watermark 初始化落库。

        init_watermark 语义：首见取 None 时实现取「当前表内最新 received_at」。
        水位线与消息 timestamp 比较，而首见锚点取 received_at——存量消息（含回拉，
        timestamp 恒早于落库时刻）的 timestamp 恒小于该锚点，天然落入已覆盖侧，
        首见不蒸存量（ADR-009 首见决议）；显式传 0.0 表示全量蒸馏（测试用）。
        """
        async with self._session() as s:
            row = await s.get(DistillStateRow, chat_id)
            if row is not None:
                return DistillState(
                    chat_id=row.chat_id, watermark_ts=row.watermark_ts,
                    last_success_at=row.last_success_at, last_failure_at=row.last_failure_at,
                )
            if init_watermark is None:
                latest = (
                    await s.execute(
                        select(ObservedMessageRow.received_at)
                        .where(ObservedMessageRow.chat_id == chat_id)
                        .order_by(ObservedMessageRow.received_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                init_watermark = latest if latest is not None else time.time()
            s.add(DistillStateRow(
                chat_id=chat_id, watermark_ts=init_watermark,
            ))
            await s.commit()
            return DistillState(chat_id=chat_id, watermark_ts=init_watermark)

    async def save_distill_state(self, state: DistillState) -> None:
        async with self._session() as s:
            row = await s.get(DistillStateRow, state.chat_id)
            if row is None:
                row = DistillStateRow(chat_id=state.chat_id)
                s.add(row)
            row.watermark_ts = state.watermark_ts
            row.last_success_at = state.last_success_at
            row.last_failure_at = state.last_failure_at
            await s.commit()

    async def list_binding_chats(self) -> list[str]:
        """重启补拉用：绑定了仓库的群列表。"""
        async with self._session() as s:
            rows = await s.execute(select(GroupRepoBindingRow.chat_id).distinct())
            return [r[0] for r in rows.all()]

    # ---- L3 ----

    async def search_knowledge(self, query: str, *, limit: int = 8) -> list[KnowledgeHit]:
        """L3 正式知识库检索（ADR-002）：委托 KnowledgeBase 双路融合。"""
        from app.docs.knowledge import KnowledgeBase

        if self._knowledge is None:
            self._knowledge = KnowledgeBase(db_url=settings.memory_db_url, embedding=self._embed)
            await self._knowledge.init()
        try:
            return await self._knowledge.search(query, limit=limit)
        except Exception:  # noqa: BLE001 —— L3 故障不阻塞回答主链路
            log.exception("knowledge search failed, degrade to empty")
            return []
