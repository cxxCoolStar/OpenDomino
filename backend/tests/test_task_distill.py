"""TaskDistiller 单元测试（ADR-002 Phase B）：解析三态 + 触发与槽位规则。"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.console.task_distill import TaskDistiller, parse_task_distill_output
from app.memory.service import SQLiteMemoryService
from app.memory.types import MEMORY_SOURCE_TASK, MemoryDraft, MemoryStatus, PendingState


# ---- 解析三态 ----

def test_parse_proposal_ok():
    raw = json.dumps({
        "distilled": True, "title": "t", "statement": "s",
        "memory_type": "convention", "keywords": ["k"], "supersedes_id": "",
    }, ensure_ascii=False)
    data = parse_task_distill_output(raw)
    assert data is not None and data["statement"] == "s"


def test_parse_nothing():
    assert parse_task_distill_output('{"distilled": false}') is None


def test_parse_failure_raises():
    with pytest.raises(ValueError):
        parse_task_distill_output("模型胡言乱语没有JSON")
    with pytest.raises(ValueError):
        parse_task_distill_output('{"distilled": true}')  # 缺 statement


def test_parse_json_with_noise():
    raw = '好的\n```json\n{"distilled": true, "statement": "s"}\n```\n以上'
    assert parse_task_distill_output(raw)["statement"] == "s"


# ---- 触发与槽位 ----

class FakeLLM:
    def __init__(self, output: str):
        self.output = output
        self.calls: list[str] = []

    async def complete(self, system: str, user: str, *, model=None) -> str:
        self.calls.append(user)
        return self.output


PROPOSAL = json.dumps({
    "distilled": True, "title": "仓库经验", "statement": "shop-service 重试要复用 TaskScheduler",
    "memory_type": "convention", "keywords": [], "supersedes_id": "",
}, ensure_ascii=False)


@pytest.fixture
async def env(tmp_path):
    mem = SQLiteMemoryService(db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await mem.init()
    replies: list[str] = []

    async def reply(chat_id: str, text: str) -> None:
        replies.append(text)

    return mem, replies, reply


@pytest.mark.asyncio
async def test_propose_attaches_slot_with_task_source(env):
    mem, replies, reply = env
    d = TaskDistiller(mem, FakeLLM(PROPOSAL), reply=reply)
    await d._distill(task_id="task-abc12345", chat_id="c1", repo="shop-service",
                     task_type="modify", instruction="instr", outcome="done well", diff_stat="")
    assert replies and "#task-abc" in replies[0] and "工程经验" in replies[0]
    sess = await mem.load_session("c1")
    assert sess.pending_memory.payload["source"] == MEMORY_SOURCE_TASK
    hit = await mem.get_memory(sess.pending_memory.payload["memory_id"])
    assert hit is not None and hit.status == MemoryStatus.PENDING


@pytest.mark.asyncio
async def test_nothing_leaves_slot_empty(env):
    mem, replies, reply = env
    d = TaskDistiller(mem, FakeLLM('{"distilled": false}'), reply=reply)
    await d._distill(task_id="t2", chat_id="c1", repo="r", task_type="investigate",
                     instruction="i", outcome="o", diff_stat="")
    assert not replies
    assert (await mem.load_session("c1")).pending_memory is None


@pytest.mark.asyncio
async def test_slot_busy_skips_and_keeps_original(env):
    mem, replies, reply = env
    blocker = await mem.create_memory_draft(MemoryDraft(title="占位", statement="原提议"))
    sess = await mem.load_session("c1")
    sess.pending_memory = PendingState(kind="memory_confirm", payload={"memory_id": blocker})
    await mem.save_session(sess)

    d = TaskDistiller(mem, FakeLLM(PROPOSAL), reply=reply)
    await d._distill(task_id="t3", chat_id="c1", repo="r", task_type="modify",
                     instruction="i", outcome="o", diff_stat="")
    assert not replies  # 未发新提议
    sess = await mem.load_session("c1")
    assert sess.pending_memory.payload["memory_id"] == blocker  # 原占位保持


@pytest.mark.asyncio
async def test_llm_failure_is_silent(env):
    mem, replies, reply = env

    class Boom:
        async def complete(self, system: str, user: str, *, model=None) -> str:
            raise RuntimeError("upstream")

    d = TaskDistiller(mem, Boom(), reply=reply)
    await d._distill(task_id="t4", chat_id="c1", repo="r", task_type="modify",
                     instruction="i", outcome="o", diff_stat="")  # 不应抛出
    assert not replies
    assert (await mem.load_session("c1")).pending_memory is None


@pytest.mark.asyncio
async def test_user_content_contains_instruction_outcome_diffstat():
    content = TaskDistiller._build_user_content(
        repo="shop-service", task_type="modify", instruction="需求文本",
        outcome="执行汇总", diff_stat="分支 b，提交数 2",
    )
    assert "需求文本" in content and "执行汇总" in content and "提交数 2" in content
    assert "代码变更" in content


# ---- 冲突检测：双层 KBase 上下文（2026-09-29 补强）----

@pytest.mark.asyncio
async def test_filter_confirmed_by_repo_substring(tmp_path):
    mem = SQLiteMemoryService(db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await mem.init()
    hit_a = await mem.create_memory_draft(MemoryDraft(title="a", statement="shop-service 重试复用 TaskScheduler"))
    await mem.confirm_memory(hit_a, "u1")
    hit_b = await mem.create_memory_draft(MemoryDraft(title="b", statement="另一个仓库的经验 billing"))
    await mem.confirm_memory(hit_b, "u1")

    hits = await mem.filter_confirmed_by_repo("shop-service")
    assert [h.id for h in hits] == [hit_a]          # 只命中带仓库名的条目
    assert await mem.filter_confirmed_by_repo("") == []  # 空仓库名安全返回


@pytest.mark.asyncio
async def test_kbase_context_merges_recall_and_repo_filter(env):
    mem, _, _ = env
    old = await mem.create_memory_draft(MemoryDraft(title="旧约定", statement="shop-service 重试要复用 TaskScheduler"))
    await mem.confirm_memory(old, "u1")
    d = TaskDistiller(mem, FakeLLM(PROPOSAL))
    ctx = await d._kbase_context("shop-service", outcome="本次产出文本")
    assert f"[{old[:8]}]" in ctx and "TaskScheduler" in ctx


@pytest.mark.asyncio
async def test_user_content_includes_kbase_and_llm_sees_it(env):
    mem, replies, reply = env
    old = await mem.create_memory_draft(MemoryDraft(title="旧", statement="shop-service 既有结论"))
    await mem.confirm_memory(old, "u1")
    llm = FakeLLM(PROPOSAL)
    d = TaskDistiller(mem, llm, reply=reply)
    await d._distill(task_id="t9", chat_id="c1", repo="shop-service",
                     task_type="modify", instruction="i", outcome="o", diff_stat="")
    assert "【现有团队记忆】" in llm.calls[0] and "既有结论" in llm.calls[0]


@pytest.mark.asyncio
async def test_supersedes_flows_through_proposal(env):
    """模型给出 supersedes_id 时：草稿携带，确认后旧条目转 archived。"""
    mem, replies, reply = env
    old = await mem.create_memory_draft(MemoryDraft(title="旧", statement="旧结论"))
    await mem.confirm_memory(old, "u1")
    proposal = json.dumps({
        "distilled": True, "title": "新", "statement": "shop-service 新结论",
        "memory_type": "convention", "keywords": [], "supersedes_id": old,
    }, ensure_ascii=False)
    d = TaskDistiller(mem, FakeLLM(proposal), reply=reply)
    await d._distill(task_id="t10", chat_id="c1", repo="shop-service",
                     task_type="modify", instruction="i", outcome="o", diff_stat="")
    sess = await mem.load_session("c1")
    new_id = sess.pending_memory.payload["memory_id"]
    assert "替代一条旧记忆" in replies[0]
    await mem.confirm_memory(new_id, "u1")
    new_hit = await mem.get_memory(new_id)
    assert new_hit.status == MemoryStatus.CONFIRMED
    # 旧条目已归档（通过 confirm_memory 的 supersedes 语义验证）
    async with mem._session() as s:
        from app.memory.service import MemoryRow, MemoryStatus as MS
        row = await s.get(MemoryRow, old)
        assert row.status == MS.ARCHIVED.value
