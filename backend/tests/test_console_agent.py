"""ConsoleAgent 端到端闭环测试（fake LLM + SQLite 记忆）。"""
from __future__ import annotations

import pytest

from app.channels.base import UnifiedMessage
from app.console.agent import ConsoleAgent
from app.console.router import Router, _extract_json
from app.memory.service import SQLiteMemoryService


class FakeLLM:
    """按队列返回预设输出。"""

    def __init__(self, outputs: list[str]):
        self._outputs = list(outputs)
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, *, model=None) -> str:
        self.calls.append((system, user))
        return self._outputs.pop(0) if self._outputs else '{"action":"answer","answer":"默认"}'


@pytest.fixture
async def env(tmp_path):
    mem = SQLiteMemoryService(db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await mem.init()
    replies: list[tuple[str, str]] = []

    async def reply(chat_id: str, text: str, webhook: str = "") -> None:
        replies.append((chat_id, text))

    delegated: list[dict] = []

    async def delegate(draft: dict):
        delegated.append(draft)
        return "taskid-123456"

    return mem, replies, delegated


def make_msg(text: str, chat_id: str = "c1", user_id: str = "u1") -> UnifiedMessage:
    return UnifiedMessage(text=text, user_id=user_id, chat_id=chat_id, message_id="m1")


# ---- _extract_json ----

def test_extract_json_plain():
    assert _extract_json('{"action":"answer"}') == {"action": "answer"}


def test_extract_json_with_fence_and_noise():
    raw = '好的。\n```json\n{"action":"clarify","clarify_question":"哪个仓库？"}\n```\n以上。'
    assert _extract_json(raw)["action"] == "clarify"


# ---- 闭环 ----

@pytest.mark.asyncio
async def test_answer_flow(env):
    mem, replies, _ = env
    llm = FakeLLM(['{"action":"answer","answer":"【正式文档】发布走 O2 流程。"}'])

    async def reply(c, t, webhook=""):
        replies.append((c, t))
    agent = ConsoleAgent(mem, Router(llm), reply)
    await agent.handle(make_msg("发布流程是什么"))
    assert replies and "O2" in replies[0][1]

    sess = await mem.load_session("c1")
    assert len(sess.messages) == 2  # user + assistant


@pytest.mark.asyncio
async def test_clarify_sets_pending(env):
    mem, replies, _ = env
    llm = FakeLLM(['{"action":"clarify","clarify_question":"要改哪个仓库？"}'])

    async def reply(c, t, webhook=""):
        replies.append((c, t))
    agent = ConsoleAgent(mem, Router(llm), reply)
    await agent.handle(make_msg("帮我改下那个 bug"))
    assert "哪个仓库" in replies[0][1]

    sess = await mem.load_session("c1")
    assert sess.pending is not None and sess.pending.kind == "clarify"


@pytest.mark.asyncio
async def test_pending_consumed_on_answer(env):
    mem, replies, _ = env
    llm = FakeLLM([
        '{"action":"clarify","clarify_question":"哪个仓库？"}',
        '{"action":"delegate","pending_resolution":"answered","task":{"title":"改bug","instruction":"在 shop-frontend 修复登录按钮不响应","repo":"shop-frontend"}}',
    ])

    async def reply(c, t, webhook=""):
        replies.append((c, t))
    async def delegate(d):
        return "tid-999"
    agent = ConsoleAgent(mem, Router(llm), reply, delegate)
    await agent.handle(make_msg("帮我改下那个 bug"))
    await agent.handle(make_msg("shop-frontend 仓库", user_id="u1"))

    sess = await mem.load_session("c1")
    assert sess.pending is None           # pending 已消费
    assert "tid-999" in sess.active_task_ids  # 已委派
    assert any("已受理" in t for _, t in replies)


@pytest.mark.asyncio
async def test_delegate_without_sandbox_graceful(env):
    mem, replies, _ = env
    llm = FakeLLM(['{"action":"delegate","task":{"title":"x","instruction":"do","repo":"r"}}'])

    async def reply(c, t, webhook=""):
        replies.append((c, t))
    agent = ConsoleAgent(mem, Router(llm), reply, delegate=None)
    await agent.handle(make_msg("改代码"))
    assert any("尚未就绪" in t for _, t in replies)


@pytest.mark.asyncio
async def test_bad_json_degrades_to_answer(env):
    mem, replies, _ = env
    llm = FakeLLM(["这不是JSON", "还是不是JSON"])  # 两次都坏 → 降级原样回复

    async def reply(c, t, webhook=""):
        replies.append((c, t))
    agent = ConsoleAgent(mem, Router(llm), reply)
    await agent.handle(make_msg("你好"))
    assert replies and replies[0][1]  # 有回复（降级原样）


@pytest.mark.asyncio
async def test_group_serialization_lock(env):
    # 同群并发消息应串行（锁存在即可靠性验证在集成层）
    mem, replies, _ = env
    llm = FakeLLM(['{"action":"answer","answer":"a"}', '{"action":"answer","answer":"b"}'])

    async def reply(c, t, webhook=""):
        replies.append((c, t))
    agent = ConsoleAgent(mem, Router(llm), reply)
    import asyncio
    await asyncio.gather(agent.handle(make_msg("q1")), agent.handle(make_msg("q2")))
    sess = await mem.load_session("c1")
    assert len(sess.messages) == 4  # 两 user + 两 assistant，串行无交错丢失
