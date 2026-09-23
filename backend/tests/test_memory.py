"""记忆层单元测试（ADR-002）：L1 Session 存取 + L2 KBase 草稿/确认。"""
from __future__ import annotations

import pytest

from app.memory.service import SQLiteMemoryService
from app.memory.types import ChatMessage, MemoryDraft, MemoryStatus, PendingState


@pytest.fixture
async def mem(tmp_path):
    svc = SQLiteMemoryService(db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await svc.init()
    return svc


@pytest.mark.asyncio
async def test_session_roundtrip(mem):
    sess = await mem.load_session("chat1")
    assert sess.chat_id == "chat1" and sess.messages == []

    sess.messages.append(ChatMessage(role="user", text="发布流程是什么", user_id="u1"))
    sess.messages.append(ChatMessage(role="assistant", text="【正式文档】…"))
    await mem.save_session(sess)

    loaded = await mem.load_session("chat1")
    assert len(loaded.messages) == 2
    assert loaded.messages[0].text == "发布流程是什么"
    assert loaded.messages[1].role == "assistant"


@pytest.mark.asyncio
async def test_pending_state_persists(mem):
    sess = await mem.load_session("chat2")
    sess.pending = PendingState(kind="clarify", payload={"question": "哪个仓库？"})
    await mem.save_session(sess)

    loaded = await mem.load_session("chat2")
    assert loaded.pending is not None
    assert loaded.pending.kind == "clarify"
    assert loaded.pending.payload["question"] == "哪个仓库？"


@pytest.mark.asyncio
async def test_pending_cleared(mem):
    sess = await mem.load_session("chat2")
    sess.pending = PendingState(kind="clarify", payload={})
    await mem.save_session(sess)
    sess2 = await mem.load_session("chat2")
    sess2.pending = None
    await mem.save_session(sess2)
    assert (await mem.load_session("chat2")).pending is None


@pytest.mark.asyncio
async def test_sessions_isolated(mem):
    a = await mem.load_session("chatA")
    a.messages.append(ChatMessage(role="user", text="A群消息"))
    await mem.save_session(a)
    b = await mem.load_session("chatB")
    assert b.messages == []


@pytest.mark.asyncio
async def test_memory_draft_pending_not_recalled(mem):
    mid = await mem.create_memory_draft(
        MemoryDraft(title="i18n约定", statement="i18n key 用点分命名", memory_type="convention")
    )
    # pending 状态不进入召回
    hits = await mem.recall_kbase("i18n")
    assert hits == []
    # 确认后可召回
    await mem.confirm_memory(mid, actor_id="u1")
    hits = await mem.recall_kbase("i18n")
    assert len(hits) == 1 and hits[0].status == MemoryStatus.CONFIRMED


@pytest.mark.asyncio
async def test_get_memory(mem):
    mid = await mem.create_memory_draft(MemoryDraft(title="t", statement="s"))
    m = await mem.get_memory(mid)
    assert m is not None and m.title == "t" and m.status == MemoryStatus.PENDING
    assert await mem.get_memory("nope") is None
