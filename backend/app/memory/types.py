"""记忆层类型定义与 MemoryService 抽象（ADR-002）。

三层记忆职责不同、来源分级。ConsoleAgent 依赖本抽象，不依赖具体存储。
本步实现 L1 Session 存取；L2 KBase、L3 知识库定义接口，先给空实现，后续补。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


# ---------- L1 Session ----------


@dataclass
class ChatMessage:
    role: str           # user | assistant
    text: str
    user_id: str = ""
    user_name: str = ""
    message_id: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class PendingState:
    """待处理状态：待澄清 / 待记忆确认。持久化在 Session 中，重启可恢复。"""

    kind: str           # clarify | memory_confirm
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


@dataclass
class SessionContext:
    session_id: str
    chat_id: str
    conversation_id: str = ""
    messages: list[ChatMessage] = field(default_factory=list)
    pending: PendingState | None = None           # 澄清等待（对话阻塞态）
    pending_memory: PendingState | None = None    # 待确认记忆（非阻塞提议，ADR-002）
    summary: str = ""                             # 已压缩对话的滚动摘要（Phase A 生成）
    active_task_ids: list[str] = field(default_factory=list)
    last_route: dict[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)

    @staticmethod
    def new(chat_id: str, conversation_id: str = "") -> "SessionContext":
        return SessionContext(
            session_id=str(uuid.uuid4()), chat_id=chat_id, conversation_id=conversation_id
        )


# ---------- L2 KBase ----------


class MemoryStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    ARCHIVED = "archived"


@dataclass
class MemoryHit:
    id: str
    title: str
    statement: str
    memory_type: str = ""
    rationale: str = ""
    score: float = 0.0
    status: MemoryStatus = MemoryStatus.CONFIRMED


@dataclass
class MemoryDraft:
    title: str
    statement: str
    memory_type: str = "convention"
    rationale: str = ""
    keywords: list[str] = field(default_factory=list)
    source_session_id: str = ""
    source_task_id: str = ""


# ---------- L3 知识库 ----------


@dataclass
class KnowledgeHit:
    doc_title: str
    heading_path: str
    content: str
    doc_version: str = ""
    source: str = ""
    score: float = 0.0


# ---------- 抽象 ----------


class MemoryService(Protocol):
    """三层记忆统一接口。实现可替换，不得改变层级职责与来源标记。"""

    # L1
    async def load_session(self, chat_id: str) -> SessionContext: ...
    async def save_session(self, session: SessionContext) -> None: ...

    # L2
    async def recall_kbase(self, query: str, *, limit: int = 5) -> list[MemoryHit]: ...
    async def create_memory_draft(self, draft: MemoryDraft) -> str: ...
    async def confirm_memory(self, memory_id: str, actor_id: str) -> None: ...
    async def get_memory(self, memory_id: str) -> MemoryHit | None: ...
    async def delete_memory(self, memory_id: str) -> bool: ...

    # L3
    async def search_knowledge(self, query: str, *, limit: int = 8) -> list[KnowledgeHit]: ...
