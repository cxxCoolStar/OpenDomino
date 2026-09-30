"""记忆确认流（ADR-002 Phase 0）测试：状态机/拦截/@门控/过期/共存。"""
from __future__ import annotations

import time

import pytest

from app.config import settings
from app.console.agent import ConsoleAgent
from app.console.memory_confirm import (
    MemoryConfirmHandler,
    normalize_deposit,
    normalize_remember,
    normalize_text,
)
from app.memory.types import PendingState
from tests.test_console_agent import FakeLLM, make_msg


async def _noop_delegate(draft: dict) -> str:
    raise AssertionError("测试用例不应触发委派")


async def _noop_task_op(chat_id: str, op: str, task_id: str, message: str) -> str:
    raise AssertionError("测试用例不应触发任务管控")

@pytest.fixture
async def mem(tmp_path):
    from app.memory.service import SQLiteMemoryService

    m = SQLiteMemoryService(db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await m.init()
    return m


def _agent(mem, replies):
    """router 用永远 answer 的 fake——确认流测试只关心拦截与否，不关心路由内容。"""
    from app.console.router import Router

    return ConsoleAgent(mem, Router(FakeLLM([])), replies, _noop_delegate, _noop_task_op)


class ReplySink:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def __call__(self, chat_id: str, text: str, webhook: str = "") -> None:
        self.sent.append((chat_id, text))


@pytest.fixture
def replies():
    return ReplySink()


# ---- 纯函数 ----


def test_normalize_text_strips_at_prefix():
    assert normalize_text("@Domino 确认") == "确认"
    assert normalize_text("@Domino @Robot 确认") == "确认"
    assert normalize_text("确认") == "确认"


def test_normalize_remember_and_deposit():
    assert normalize_remember("记住：发布走 O2") == "发布走 O2"
    assert normalize_remember("记住:发布走 O2") == "发布走 O2"
    assert normalize_remember("记住：") is None
    assert normalize_remember("其他消息") is None
    assert normalize_deposit("/沉淀 测试约定") == "测试约定"
    assert normalize_deposit("沉淀：测试约定") == "测试约定"


# ---- /沉淀 → 确认流 ----


@pytest.mark.asyncio
async def test_deposit_creates_draft_and_pending(mem, replies):
    agent = _agent(mem, replies)
    await agent.handle(make_msg("/沉淀 发布必须走 O2 流程"))

    assert "待确认" in replies.sent[0][1] or "保存这条" in replies.sent[0][1]
    sess = await mem.load_session("c1")
    assert sess.pending_memory is not None and sess.pending_memory.kind == "memory_confirm"
    hits = await mem.recall_kbase("发布")
    assert hits == []  # 草稿 pending，不进召回


@pytest.mark.asyncio
async def test_confirm_writes_memory(mem, replies):
    agent = _agent(mem, replies)
    await agent.handle(make_msg("/沉淀 发布必须走 O2 流程"))
    await agent.handle(make_msg("确认"))

    assert any("已保存" in t for _, t in replies.sent)
    sess = await mem.load_session("c1")
    assert sess.pending_memory is None
    hits = await mem.recall_kbase("O2")
    assert len(hits) == 1


@pytest.mark.asyncio
async def test_cancel_deletes_draft(mem, replies):
    agent = _agent(mem, replies)
    await agent.handle(make_msg("/沉淀 临时约定"))
    await agent.handle(make_msg("取消"))

    assert any("未保存" in t for _, t in replies.sent)
    sess = await mem.load_session("c1")
    assert sess.pending_memory is None
    hits = await mem.recall_kbase("临时约定")
    assert hits == []


@pytest.mark.asyncio
async def test_at_prefix_still_matches_confirm(mem, replies):
    """钉钉 @ 消息带前缀也能确认（归一化）。"""
    agent = _agent(mem, replies)
    await agent.handle(make_msg("/沉淀 带前缀测试"))
    await agent.handle(make_msg("@Domino 确认"))

    assert any("已保存" in t for _, t in replies.sent)


@pytest.mark.asyncio
async def test_second_deposit_rejected_while_pending(mem, replies):
    agent = _agent(mem, replies)
    await agent.handle(make_msg("/沉淀 第一条"))
    n = len(replies.sent)
    await agent.handle(make_msg("/沉淀 第二条"))

    assert "先处理" in replies.sent[n][1] or "请先" in replies.sent[n][1]
    sess = await mem.load_session("c1")
    # 仍指向第一条
    assert sess.pending_memory.payload["memory_id"]


@pytest.mark.asyncio
async def test_other_message_keeps_pending_memory(mem, replies):
    """非确认/取消消息放行给路由器，但 pending_memory 保留（非阻塞提议）。"""
    agent = _agent(mem, replies)
    await agent.handle(make_msg("/沉淀 测试"))
    n = len(replies.sent)
    await agent.handle(make_msg("确认一下，顺便问个新问题"))

    # 放行产生路由回复，但不是确认/取消回复
    assert len(replies.sent) > n
    assert not any("已保存" in t for _, t in replies.sent[n:])
    sess = await mem.load_session("c1")
    assert sess.pending_memory is not None


@pytest.mark.asyncio
async def test_expired_pending_memory_cleaned(mem, replies, monkeypatch):
    monkeypatch.setattr(settings, "memory_confirm_ttl_minutes", 60)
    agent = _agent(mem, replies)
    await agent.handle(make_msg("/沉淀 过期测试"))

    sess = await mem.load_session("c1")
    sess.pending_memory.created_at = time.time() - 61 * 60
    await mem.save_session(sess)

    await agent.handle(make_msg("确认"))
    sess2 = await mem.load_session("c1")
    assert sess2.pending_memory is None
    assert not any("已保存" in t for _, t in replies.sent)  # 过期后确认无效


@pytest.mark.asyncio
async def test_confirm_blocked_while_clarify_pending(mem, replies):
    """clarify 槽占用时确认词不拦截（交给路由器判断归属）。"""
    handler = MemoryConfirmHandler(mem)
    session = await mem.load_session("c1")
    session.pending = PendingState(kind="clarify", payload={"question": "要改哪个仓库？"})
    mid = await _mk_draft(mem)
    session.pending_memory = PendingState(kind="memory_confirm", payload={"memory_id": mid})
    await mem.save_session(session)

    consumed = await handler.try_handle(make_msg("确认"), session, replies)
    assert consumed is False
    assert session.pending_memory is not None  # 未消费


@pytest.mark.asyncio
async def test_remember_direct_save(mem, replies):
    """记住：直达通道，不经确认流直接 confirmed。"""
    agent = _agent(mem, replies)
    await agent.handle(make_msg("记住：代码评审必须两人"))

    assert any("已记住" in t for _, t in replies.sent)
    hits = await mem.recall_kbase("代码评审")
    assert len(hits) == 1  # 直接可召回


@pytest.mark.asyncio
async def test_free_chat_requires_at_for_all_memory_ops(mem, replies, monkeypatch):
    """免 @ 群里，记忆操作（含 /沉淀 指令与确认词）都必须 @ 机器人。"""
    monkeypatch.setattr(settings, "feishu_require_mention", True)
    monkeypatch.setattr(settings, "feishu_free_response_chats", "c1")
    agent = _agent(mem, replies)

    # 未 @ 的 /沉淀 被放行当普通消息，不建草稿
    msg0 = make_msg("/沉淀 门控测试")
    msg0.is_at_bot = False
    await agent.handle(msg0)
    sess = await mem.load_session("c1")
    assert sess.pending_memory is None

    # @ 的 /沉淀 建草稿
    msg1 = make_msg("/沉淀 门控测试")
    msg1.is_at_bot = True
    await agent.handle(msg1)
    sess = await mem.load_session("c1")
    assert sess.pending_memory is not None

    # 未 @ 的确认不消费
    msg2 = make_msg("确认")
    msg2.is_at_bot = False
    await agent.handle(msg2)
    sess2 = await mem.load_session("c1")
    assert sess2.pending_memory is not None

    # @ 的确认消费
    msg3 = make_msg("确认")
    msg3.is_at_bot = True
    await agent.handle(msg3)
    sess3 = await mem.load_session("c1")
    assert sess3.pending_memory is None


async def _mk_draft(mem) -> str:
    from app.memory.types import MemoryDraft

    return await mem.create_memory_draft(MemoryDraft(title="t", statement="s"))
