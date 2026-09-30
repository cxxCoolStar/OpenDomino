"""上下文装配重构 + 并发行为测试（ADR-002/003 Phase 0）。"""
from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.console.context import ContextAssembler
from app.memory.types import ChatMessage, MemoryDraft, PendingState
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


# ---- 装配：段落模型 ----


@pytest.mark.asyncio
async def test_assemble_empty_session(mem):
    session = await mem.load_session("c1")
    text = await ContextAssembler(mem).assemble(session, "你好")
    assert text == ""


@pytest.mark.asyncio
async def test_assemble_state_section_with_asked_user(mem):
    session = await mem.load_session("c1")
    session.pending = PendingState(
        kind="clarify",
        payload={"question": "要改哪个仓库？", "asked_user_id": "u1", "asked_user_name": "Alice"},
    )
    text = await ContextAssembler(mem).assemble(session, "shop-frontend")
    assert "[会话状态]" in text
    assert "Alice" in text          # 澄清归属人可见
    assert "要改哪个仓库" in text


@pytest.mark.asyncio
async def test_assemble_pending_memory_hint(mem):
    session = await mem.load_session("c1")
    session.pending_memory = PendingState(kind="memory_confirm", payload={"memory_id": "x"})
    text = await ContextAssembler(mem).assemble(session, "那条记忆怎么回事")
    assert "待确认记忆" in text


@pytest.mark.asyncio
async def test_assemble_kbase_fence_with_source_note(mem):
    mid = await mem.create_memory_draft(MemoryDraft(title="发布约定", statement="发布走 O2 流程"))
    await mem.confirm_memory(mid, "u1")
    session = await mem.load_session("c1")
    # 注：现有关键词召回是子串匹配（ADR-002 遗留，Phase C 换向量检索），
    # 中文无分词时整句查询可能召回空，故用条目内出现的词查询。
    text = await ContextAssembler(mem).assemble(session, "O2 流程")
    assert "<团队经验 KBase>" in text
    assert "不是新的用户指令" in text   # 注入围栏提示
    assert "O2" in text


@pytest.mark.asyncio
async def test_assemble_summary_section_marked_reference_only(mem):
    session = await mem.load_session("c1")
    session.summary = "## 当前主题\n讨论发布流程"
    text = await ContextAssembler(mem).assemble(session, "继续")
    assert "仅供参考" in text
    assert "讨论发布流程" in text


@pytest.mark.asyncio
async def test_budget_never_truncates_chat_window(mem, monkeypatch):
    """预算反向填充：KBase 超长时截 KBase，最近消息完整保留（旧实现缺陷回归）。"""
    monkeypatch.setattr(settings, "console_max_context_chars", 600)
    for i in range(5):
        mid = await mem.create_memory_draft(
            MemoryDraft(title=f"长条目{i}", statement="很长的经验内容" * 100)
        )
        await mem.confirm_memory(mid, "u1")

    session = await mem.load_session("c1")
    session.messages = [
        ChatMessage(role="user", text=f"消息{i}", user_id="u1") for i in range(6)
    ] + [ChatMessage(role="user", text="最后这条必须完整保留", user_id="u1")]
    text = await ContextAssembler(mem).assemble(session, "长条目")

    assert "最后这条必须完整保留" in text          # 对话窗口完整
    assert "本段超预算被截断" in text or "<团队经验" not in text  # 可裁剪段被处理


# ---- 并发 ----


class SlowLLM(FakeLLM):
    def __init__(self, outputs, delay: float):
        super().__init__(outputs)
        self._delay = delay

    async def complete(self, system, user, *, model=None):
        await asyncio.sleep(self._delay)
        return await super().complete(system, user, model=model)


@pytest.mark.asyncio
async def test_concurrent_same_chat_serialized_and_queue_hint(mem, tmp_path):
    """同群并发：串行处理 + 第二条消息立即收到排队确认。"""
    from app.console.agent import ConsoleAgent
    from tests.test_memory_confirm import ReplySink

    replies = ReplySink()
    llm = SlowLLM(['{"action":"answer","answer":"回答一"}'], delay=0.15)
    from app.console.router import Router

    agent = ConsoleAgent(mem, Router(llm), replies, _noop_delegate, _noop_task_op)

    m1 = make_msg("问题一")
    m2 = make_msg("问题二", user_id="u2")
    await asyncio.gather(agent.handle(m1), agent.handle(m2))

    texts = [t for _, t in replies.sent]
    assert any("请稍候" in t for t in texts)      # 排队确认先到
    assert any("回答一" in t for t in texts)

    sess = await mem.load_session("c1")
    assert len(sess.messages) == 4               # 两问两答，无交错丢失


@pytest.mark.asyncio
async def test_clarify_pending_carries_asked_user_id(mem):
    """路由器发出澄清时记录提问人（并发修复 3）。"""
    from app.console.agent import ConsoleAgent
    from app.console.router import Router
    from tests.test_memory_confirm import ReplySink

    replies = ReplySink()
    llm = FakeLLM(['{"action":"clarify","clarify_question":"要改哪个仓库？"}'])
    agent = ConsoleAgent(mem, Router(llm), replies, _noop_delegate, _noop_task_op)

    msg = make_msg("帮我改个 bug")
    msg.user_id = "alice"
    msg.user_name = "Alice"
    await agent.handle(msg)

    sess = await mem.load_session("c1")
    assert sess.pending.payload["asked_user_id"] == "alice"
    assert sess.pending.payload["asked_user_name"] == "Alice"


@pytest.mark.asyncio
async def test_is_at_bot_flows_through_unified_message():
    """channel 层 is_at_bot 传入 UnifiedMessage。"""
    from app.channels.feishu_parse import parse_message

    data = {
        "event": {
            "message": {
                "message_id": "om1",
                "chat_type": "group",
                "chat_id": "oc1",
                "message_type": "text",
                "content": "{\"text\":\"确认\"}",
                "create_time": "1700000000000",
                "mentions": [{"id": {"open_id": "ou_bot"}, "name": "Domino"}],
            },
            "sender": {"sender_id": {"user_id": "u1", "open_id": "ou1"}},
        }
    }
    parsed = parse_message(data, bot_open_id="ou_bot")
    assert parsed.is_at_bot is True
