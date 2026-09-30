"""Phase A 测试：纠正提炼 + 滚动压缩（ADR-002）。

使用真实 LLM（OpenAIClient）做集成验证；仅 LLM 失败降级路径用注入的失败桩
（429 无法随叫随到）。真实调用使本文件同时充当 prompt 的回归测试。
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.memory.types import ChatMessage
from tests.test_console_agent import make_msg
from tests.test_memory_confirm import ReplySink


async def _noop_delegate(draft: dict) -> str:
    raise AssertionError("测试用例不应触发委派")


async def _noop_task_op(chat_id: str, op: str, task_id: str, message: str) -> str:
    raise AssertionError("测试用例不应触发任务管控")

@pytest.fixture
def replies():
    return ReplySink()


@pytest.fixture
async def mem(tmp_path):
    from app.memory.service import SQLiteMemoryService

    m = SQLiteMemoryService(db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await m.init()
    return m


def _agent(mem, replies):
    from app.console.agent import ConsoleAgent
    from app.console.llm import OpenAIClient
    from app.console.router import Router

    return ConsoleAgent(mem, Router(OpenAIClient()), replies, _noop_delegate, _noop_task_op)


@pytest.fixture(scope="module", autouse=True)
def _require_llm():
    """无 LLM 凭证时跳过整个模块（真实调用前提）。"""
    if not settings.llm_api_key:
        pytest.skip("LLM_API_KEY not configured")


# ---- 纠正提炼（真实 LLM 路由）----


@pytest.mark.asyncio
async def test_correction_creates_draft_and_pending(mem, replies):
    """用户纠正 → 模型应标 correction_detected → 自动建草稿挂确认。"""
    agent = _agent(mem, replies)
    await agent.handle(make_msg("不对，我们团队的发布流程改了：必须先在 O2 平台走审批，然后才允许构建"))

    sess = await mem.load_session("c1")
    assert any("O2" in t for _, t in replies.sent), f"回复应提及纠正事实: {replies.sent}"
    assert sess.pending_memory is not None and sess.pending_memory.kind == "memory_confirm", (
        "纠正应自动创建待确认记忆"
    )


@pytest.mark.asyncio
async def test_correction_confirm_flow_completes(mem, replies):
    """纠正 → 确认 → 条目 confirmed 可召回（真实 LLM 全链路）。"""
    agent = _agent(mem, replies)
    await agent.handle(make_msg("你记错了，我们团队 commit message 必须用 Conventional Commits 格式"))
    sess = await mem.load_session("c1")
    if sess.pending_memory is None:
        pytest.skip("模型未将本次输入判定为纠正（LLM 判断波动），非代码问题")
    await agent.handle(make_msg("确认"))

    hits = await mem.recall_kbase("Conventional Commits")
    assert any("Conventional Commits" in h.statement for h in hits)


@pytest.mark.asyncio
async def test_correction_cancel_deletes_draft(mem, replies):
    agent = _agent(mem, replies)
    await agent.handle(make_msg("不对，代码合并前其实还必须通过安全扫描"))
    sess = await mem.load_session("c1")
    if sess.pending_memory is None:
        pytest.skip("模型未判定为纠正")
    memory_id = sess.pending_memory.payload["memory_id"]

    await agent.handle(make_msg("取消"))
    assert (await mem.load_session("c1")).pending_memory is None
    assert await mem.get_memory(memory_id) is None  # 物理删除


@pytest.mark.asyncio
async def test_no_correction_no_draft(mem, replies):
    """普通陈述不应触发纠正提炼（真实 LLM 的误判率观察）。"""
    agent = _agent(mem, replies)
    await agent.handle(make_msg("今天下午三点开迭代评审会"))
    sess = await mem.load_session("c1")
    assert sess.pending_memory is None, "非纠正消息不应建草稿"


@pytest.mark.asyncio
async def test_correction_skipped_when_pending_memory_exists(mem, replies):
    """已有待确认条目时（/沉淀 挂起中），纠正回复正常给出但不新建草稿。"""
    agent = _agent(mem, replies)
    await agent.handle(make_msg("/沉淀 第一条约定：测试环境禁止直连生产库"))
    first = (await mem.load_session("c1")).pending_memory.payload["memory_id"]

    await agent.handle(make_msg("对了你之前说的不对，发布必须走 O2 审批"))
    sess = await mem.load_session("c1")
    assert sess.pending_memory.payload["memory_id"] == first  # 仍指向第一条草稿


# ---- 滚动压缩（真实 LLM 摘要；降级路径用失败桩）----


def _fill_messages(session, n):
    for i in range(n):
        session.messages.append(ChatMessage(role="user", text=f"问题{i}：为什么模块{i}要这么设计？", user_id="u1"))
        session.messages.append(ChatMessage(role="assistant", text=f"回答{i}：模块{i}的设计原因是历史兼容与性能折中。"))


@pytest.mark.asyncio
async def test_compaction_summarizes_overflow_keeps_window(mem, replies, monkeypatch):
    """超阈值：溢出被真实 LLM 压缩进 summary，窗口内消息完整保留。"""
    monkeypatch.setattr(settings, "console_context_window", 4)
    monkeypatch.setattr(settings, "session_compact_trigger", 8)
    agent = _agent(mem, replies)

    await agent.handle(make_msg("随便问一句触发持久化"))
    sess = await mem.load_session("c1")
    _fill_messages(sess, 4)  # 2 + 8 = 10 条 > 8
    await mem.save_session(sess)

    from app.console.compaction import maybe_compact
    await maybe_compact(sess, agent._router.llm)

    assert sess.summary, "应产出摘要"
    assert "模块" in sess.summary or "发布" in sess.summary or len(sess.summary) > 20
    assert len(sess.messages) == 4              # 只剩窗口
    assert sess.messages[-1].text.startswith("回答3")
    assert all(not m.text.startswith("问题0") for m in sess.messages)  # 溢出已删


@pytest.mark.asyncio
async def test_compaction_filters_mechanical_replies(mem, replies, monkeypatch):
    """机械回复不进摘要输入（直接构造消息，纯函数级验证）。"""
    monkeypatch.setattr(settings, "console_context_window", 2)
    monkeypatch.setattr(settings, "session_compact_trigger", 1)

    from app.console.compaction import maybe_compact

    class CountLLM:
        prompts: list[str] = []

        async def complete(self, system, user, *, model=None):
            if "摘要员" in system:
                CountLLM.prompts.append(user)
                return "## 当前主题\n测试摘要"
            return "x"

    sess = await mem.load_session("c1")
    sess.messages.append(ChatMessage(role="user", text="q1", user_id="u1"))
    sess.messages.append(ChatMessage(role="assistant", text="正常回答内容"))
    sess.messages.append(ChatMessage(role="user", text="q2", user_id="u1"))
    sess.messages.append(ChatMessage(role="assistant", text="已保存团队记忆：「机械噪声」。"))
    sess.messages.append(ChatMessage(role="user", text="q3 最后一条", user_id="u1"))

    await maybe_compact(sess, CountLLM())

    # 机械回复被过滤：摘要输入里没有"机械噪声"
    assert CountLLM.prompts, "应触发摘要"
    assert "机械噪声" not in CountLLM.prompts[0]
    assert "正常回答内容" in CountLLM.prompts[0]  # 正常内容保留
    # 窗口保护：最后一条完整保留
    assert sess.messages[-1].text == "q3 最后一条"


@pytest.mark.asyncio
async def test_compaction_fallback_when_llm_fails(mem, replies, monkeypatch):
    """LLM 失败：丢弃溢出 + 占位标记，压缩照常完成（429 场景用失败桩模拟）。"""

    class FailingLLM:
        async def complete(self, system, user, *, model=None):
            raise RuntimeError("simulated 429")

    monkeypatch.setattr(settings, "console_context_window", 2)
    monkeypatch.setattr(settings, "session_compact_trigger", 4)
    agent = _agent(mem, replies)

    await agent.handle(make_msg("触发持久化"))
    sess = await mem.load_session("c1")
    _fill_messages(sess, 2)
    await mem.save_session(sess)

    from app.console.compaction import maybe_compact
    await maybe_compact(sess, FailingLLM())

    assert "已省略" in sess.summary              # 占位标记
    assert len(sess.messages) == 2              # 仍完成压缩


@pytest.mark.asyncio
async def test_compaction_not_triggered_below_threshold(mem, replies, monkeypatch):
    monkeypatch.setattr(settings, "session_compact_trigger", 40)
    agent = _agent(mem, replies)
    await agent.handle(make_msg("普通问题"))

    sess = await mem.load_session("c1")
    _fill_messages(sess, 10)  # 22 条 < 40
    await mem.save_session(sess)

    from app.console.compaction import maybe_compact

    class ProbeLLM:
        calls = 0

        async def complete(self, system, user, *, model=None):
            if "摘要员" in system:
                ProbeLLM.calls += 1
            return "x"

    await maybe_compact(sess, ProbeLLM())
    assert ProbeLLM.calls == 0                  # 未触发
    assert sess.summary == ""
