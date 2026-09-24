"""Sandbox 套件测试（ADR-004）：storage / executor / agent 循环 / pipeline 装配。

全部使用 Fake（FakeExecutor / 脚本化 LLM），不依赖 Docker 与真实 LLM；
真实 Docker 集成用例标 @pytest.mark.docker 单独跑。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.sandbox import agent as agent_mod
from app.sandbox import git_ops as git_ops
from app.sandbox import pipeline as pipeline_mod
from app.sandbox.agent import SandboxAgent, StepGuard, _is_failed_result
from app.sandbox.executor import TaskExecutor
from app.sandbox.llm import LLMResult, ToolCall
from app.sandbox.storage import TaskService
from app.sandbox.tools import ToolContext, build_tool_definitions, dispatch_tool
from app.sandbox.types import Task, TaskStage, TaskStatus


# ---------- fakes ----------


class FakeExecutor:
    """tools.Executor 假实现：记录命令，返回可脚本化输出。"""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def exec(self, command: str, *, timeout: float | None = None) -> str:
        self.commands.append(command)
        if "status --porcelain" in command:
            return " M a.py\n"
        if "rev-parse HEAD" in command:
            return "abc123\n"
        if "git log" in command:
            return "abc000\x00feat: x\ndef111\x00fix: y\n"
        if "git diff --stat" in command:
            return " a.py | 2 +-\n"
        if "git diff" in command:
            return "diff --git a/a.py b/a.py\n"
        if "checkout -b" in command:
            return ""
        return "ok"

    async def read_file(self, path: str) -> str:
        self.commands.append(f"read:{path}")
        return "hello"

    async def write_file(self, path: str, content: str) -> None:
        self.commands.append(f"write:{path}")

    async def list_dir(self, path: str = ".") -> list[dict]:
        self.commands.append(f"ls:{path}")
        return [{"name": "a.py", "type": "file", "size": 12}]


class ScriptLLM:
    """按脚本吐 LLMResult；记录每轮（消息数，是否带工具）。"""

    def __init__(self, script: list[LLMResult]) -> None:
        self.script = list(script)
        self.calls: list[tuple[int, bool]] = []

    async def chat(self, messages: list[dict], tools=None) -> LLMResult:
        self.calls.append((len(messages), tools is not None))
        item = self.script.pop(0) if self.script else LLMResult(text="done", tool_calls=[])
        return item


def make_task(status: TaskStatus = TaskStatus.PENDING, type_: str = "modify") -> Task:
    return Task(
        id="task12345678", title="加接口", instruction="加一个 /ping",
        repo="demo", chat_id="c1", session_id="s1", created_by="u1",
        type=type_, status=status,
    )


# ---------- StepGuard（护栏三重保护）----------


def test_guard_same_call_loop():
    g = StepGuard(same_call_limit=3)
    assert g.observe_call("list_dir", {"p": "x"}, "same") is None
    assert g.observe_call("list_dir", {"p": "x"}, "same") is None
    assert g.observe_call("list_dir", {"p": "x"}, "same") == "same_call_loop"
    # 输出变化则不触发
    g2 = StepGuard(same_call_limit=3)
    assert g2.observe_call("run_command", {"c": "pytest"}, "pass") is None
    assert g2.observe_call("run_command", {"c": "pytest"}, "fail") is None


def test_guard_tool_fail_streak_and_budget():
    g = StepGuard(same_tool_fail_limit=2, all_failed_limit=3)
    g.observe_result("run_command", failed=True)
    assert not g.should_strip_tools
    g.observe_result("run_command", failed=True)   # 同工具连败 2 → 摘
    assert g.should_strip_tools

    g2 = StepGuard(all_failed_limit=3)
    for _ in range(3):
        g2.observe_round(all_failed=True)
    assert g2.should_strip_tools

    g3 = StepGuard(max_steps=2)
    g3.observe_call("list_dir", {}, "a")
    assert not g3.budget_exhausted
    g3.observe_call("list_dir", {}, "b")
    assert g3.budget_exhausted


# ---------- tools：脱敏 / 拦截 / 错误信封 ----------


@pytest.mark.asyncio
async def test_dispatch_and_sanitize(monkeypatch):
    from app.sandbox import tools as T

    monkeypatch.setattr(T, "_secret_strings", lambda: ["tok123"])
    ctx = ToolContext(executor=FakeExecutor(), repo="demo", task_id="t1")

    ents = json.loads(await dispatch_tool("list_dir", {}, ctx))
    assert ents[0]["name"] == "a.py"

    # PAT 出现在输出中必须被抹掉（run_command 返回裸文本，非 JSON 信封）
    class LeakyExecutor(FakeExecutor):
        async def exec(self, command: str, *, timeout: float | None = None) -> str:
            self.commands.append(command)
            return "token tok123 leaked"

    leaked = await dispatch_tool(
        "run_command", {"command": "env"}, ToolContext(executor=LeakyExecutor())
    )
    assert "tok123" not in leaked and "***" in leaked

    # 危险命令拦截
    blocked = json.loads(await dispatch_tool("run_command", {"command": "rm -rf /"}, ctx))
    assert "拦截" in blocked["error"]

    # 未知工具 → 错误信封（不抛异常）
    unknown = json.loads(await dispatch_tool("nope", {}, ctx))
    assert "error" in unknown

    names = {d["function"]["name"] for d in build_tool_definitions()}
    assert {"list_dir", "read_file", "grep", "write_file", "apply_patch",
            "run_command", "git_diff", "git_log"} <= names


def test_failed_result_envelope():
    assert _is_failed_result(json.dumps({"error": "boom"}))
    assert not _is_failed_result("")
    assert not _is_failed_result("plain text")
    assert not _is_failed_result(json.dumps({"ok": True}))


# ---------- agent：执行循环四场景 ----------


@pytest.mark.asyncio
async def test_agent_natural_completion():
    llm = ScriptLLM([
        LLMResult(text="", tool_calls=[ToolCall(id="1", name="list_dir", args={})]),
        LLMResult(text="全部完成", tool_calls=[]),
    ])
    r = await SandboxAgent(llm).run(make_task(), ToolContext(executor=FakeExecutor()))
    assert r.status == "done" and r.summary == "全部完成" and r.steps_used == 1
    # 轮1: system+user；轮2: +assistant(tool_calls)+tool（hermes 同构：assistant 先于 tool 回填）
    assert llm.calls[0] == (2, True) and llm.calls[1] == (4, True)


@pytest.mark.asyncio
async def test_agent_loop_guard_injects_warning(monkeypatch):
    from app.sandbox.prompts import LOOP_WARNING

    class LoopLLM:
        def __init__(self):
            self.n = 0

        async def chat(self, messages, tools=None):
            self.n += 1
            if self.n <= 3:
                return LLMResult(text="", tool_calls=[ToolCall(id=str(self.n), name="list_dir", args={})])
            assert any(m.get("content") == LOOP_WARNING for m in messages)
            return LLMResult(text="换路完成", tool_calls=[])

    r = await SandboxAgent(LoopLLM()).run(make_task(), ToolContext(executor=FakeExecutor()))
    assert r.status == "done" and r.steps_used == 3


@pytest.mark.asyncio
async def test_agent_budget_exhausted_force_summary(monkeypatch):
    from app.sandbox.prompts import FORCE_SUMMARY_NUDGE

    class BudgetLLM:
        async def chat(self, messages, tools=None):
            if tools is None:
                assert any(FORCE_SUMMARY_NUDGE in str(m.get("content")) for m in messages)
                return LLMResult(text="已改 2 个文件，测试未跑完", tool_calls=[])
            return LLMResult(text="", tool_calls=[ToolCall(id="x", name="run_command", args={"command": "pytest"})])

    monkeypatch.setattr(agent_mod.settings, "sandbox_max_steps", 2)
    r = await SandboxAgent(BudgetLLM()).run(make_task(), ToolContext(executor=FakeExecutor()))
    assert r.status == "summary" and "未跑完" in r.summary and r.exit_reason == "budget_exhausted"


# ---------- storage：CRUD + 恢复 ----------


@pytest.mark.asyncio
async def test_task_service_crud_and_recover(tmp_path):
    svc = TaskService(db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await svc.init()
    try:
        t1 = await svc.create({"title": "T1", "instruction": "i", "repo": "demo",
                               "chat_id": "c", "session_id": "s", "created_by": "u"})
        t2 = await svc.create({"title": "T2", "instruction": "i", "repo": "demo",
                               "chat_id": "c", "session_id": "s", "created_by": "u"})
        t3 = await svc.create({"title": "T3", "instruction": "i", "repo": "demo",
                               "chat_id": "c", "session_id": "s", "created_by": "u"})

        await svc.set_running(t3, branch_name="domino/task-x", base_ref="abc")
        await svc.append_event(t3, TaskStage.SETUP, "stage_enter", "ok")
        await svc.append_event(t3, TaskStage.EXECUTE, "stage_exit", "done")

        events = await svc.events(t3)
        assert [e.seq for e in events] == [1, 2]

        requeue = await svc.recover()
        assert set(requeue) == {t1, t2}
        assert (await svc.get(t3)).status == TaskStatus.FAILED
        assert (await svc.get(t1)).status == TaskStatus.PENDING

        await svc.set_review(t1, commits=[{"sha": "a", "message": "m"}], verify_result="passed")
        await svc.finish(t1, TaskStatus.DONE, verify_result="passed")
        done = await svc.get(t1)
        assert done.status == TaskStatus.DONE and done.commits[0]["sha"] == "a"
    finally:
        await svc.close()


# ---------- executor：同仓串行 / 跨仓并行 / 崩溃不死 ----------


@pytest.mark.asyncio
async def test_task_executor_serial_and_parallel():
    order: list[str] = []
    running: set[str] = set()
    peak = {"n": 0}

    async def run_task(tid: str) -> None:
        running.add(tid)
        peak["n"] = max(peak["n"], len(running))
        order.append(tid)
        await asyncio.sleep(0.02)
        running.discard(tid)
        if tid.endswith("boom"):
            raise RuntimeError("boom")

    ex = TaskExecutor(run_task, max_concurrency=2)
    await ex.submit("a:1", "a")
    await ex.submit("a:2", "a")
    await ex.submit("a:3boom", "a")
    await ex.submit("b:1", "b")
    await asyncio.wait_for(ex._queues["a"].join(), 5)
    await asyncio.wait_for(ex._queues["b"].join(), 5)

    idx_a = [i for i, t in enumerate(order) if t.startswith("a")]
    assert order[idx_a[0]] == "a:1" and idx_a[1] < idx_a[2]
    assert peak["n"] <= 2
    await ex.close()


# ---------- git_ops：分支 / 提交 / push / log ----------


@pytest.mark.asyncio
async def test_git_ops_chain():
    ex = FakeExecutor()
    branch = await git_ops.create_task_branch(ex, "task12345678")
    assert branch == "domino/task-task1234"
    sha = await git_ops.commit(ex, "feat: thing")
    assert sha == "abc123"
    assert await git_ops.push(ex, branch) is True
    log = await git_ops.commit_log(ex, "base")
    assert log[0]["sha"] == "abc000" and log[0]["message"] == "feat: x"
    stat = await git_ops.diff_stat(ex, "base")
    assert "a.py" in stat


# ---------- pipeline：装配级（happy / crash / skip-rerun）----------


class FakePool:
    def __init__(self):
        self.acquired: list[str] = []
        self.released: list[str] = []

    async def acquire(self, repo, *, chat_id: str = "", memory=None):
        self.acquired.append(repo)
        return FakeExecutor()

    async def reset_workspace(self, repo, executor, base_ref):
        return base_ref or "abc123"

    async def release(self, repo, executor):
        self.released.append(repo)


class FakeReporter:
    def __init__(self):
        self.calls: list[tuple] = []

    async def started(self, task):
        self.calls.append(("started", task.id))

    async def review_ready(self, task, **kw):
        self.calls.append(("review_ready", task.id, kw["commits"]))

    async def concluded(self, task, conclusion: str):
        self.calls.append(("concluded", task.id, conclusion))

    async def finished(self, task):
        self.calls.append(("finished", task.id, task.status.value))


class FakeTasks:
    def __init__(self, task):
        self.task = task
        self.states: list[tuple] = []

    async def get(self, tid):
        return self.task

    async def set_status(self, tid, st, **kw):
        self.states.append(("status", st.value))

    async def append_event(self, tid, stage, kind, summary):
        self.states.append(("event", stage.value if hasattr(stage, "value") else stage, kind))

    async def set_running(self, tid, **kw):
        self.states.append(("running", kw))

    async def set_review(self, tid, **kw):
        self.states.append(("review", kw))

    async def finish(self, tid, st, **kw):
        self.states.append(("finish", st.value, kw))


@pytest.mark.asyncio
async def test_pipeline_happy_path():
    task = make_task()
    tasks, pool, rep = FakeTasks(task), FakePool(), FakeReporter()
    llm = ScriptLLM([LLMResult(text="改了 a.py", tool_calls=[])])

    await pipeline_mod.TaskPipeline(tasks, pool, SandboxAgent(llm), rep).run_task(task.id)

    kinds = [s[0] for s in tasks.states]
    assert "running" in kinds and "review" in kinds and "finish" in kinds
    assert task.status == TaskStatus.DONE
    assert pool.acquired == ["demo"] and pool.released == ["demo"]
    assert any(c[0] == "review_ready" for c in rep.calls)
    assert any(c[0] == "finished" and c[2] == "done" for c in rep.calls)


@pytest.mark.asyncio
async def test_pipeline_crash_becomes_failed_and_releases():
    task = make_task()
    tasks, pool, rep = FakeTasks(task), FakePool(), FakeReporter()

    class BoomLLM:
        async def chat(self, messages, tools=None):
            raise RuntimeError("llm exploded")

    await pipeline_mod.TaskPipeline(tasks, pool, SandboxAgent(BoomLLM()), rep).run_task(task.id)

    assert task.status == TaskStatus.FAILED and "RuntimeError" in task.error
    assert pool.released == ["demo"]                      # finally 归还容器
    assert any(s[0] == "finish" and s[1] == "failed" for s in tasks.states)
    assert any(c[0] == "finished" and c[2] == "failed" for c in rep.calls)


@pytest.mark.asyncio
async def test_pipeline_skips_non_pending():
    task = make_task(status=TaskStatus.RUNNING)
    tasks, pool, rep = FakeTasks(task), FakePool(), FakeReporter()
    await pipeline_mod.TaskPipeline(tasks, pool, SandboxAgent(ScriptLLM([])), rep).run_task(task.id)
    assert pool.acquired == []                             # 不重复执行


@pytest.mark.asyncio
async def test_pipeline_investigate_readonly_path():
    """排查任务：不建分支、不提交；结论回传 done（原文主路径）。"""
    task = make_task(type_="investigate")
    tasks, pool, rep = FakeTasks(task), FakePool(), FakeReporter()
    llm = ScriptLLM([LLMResult(text="问题出在 config.py 的超时配置", tool_calls=[])])

    await pipeline_mod.TaskPipeline(tasks, pool, SandboxAgent(llm), rep).run_task(task.id)

    kinds = [s[0] for s in tasks.states]
    assert "running" not in kinds and "review" not in kinds       # 不建分支、不进评审
    assert "finish" in kinds
    fin = [s for s in tasks.states if s[0] == "finish"][0]
    assert fin[1] == "done" and fin[2].get("verify_result") == "conclusion"
    assert any(c[0] == "concluded" and "config.py" in c[2] for c in rep.calls)
    assert pool.released == ["demo"]


@pytest.mark.asyncio
async def test_pipeline_investigate_no_conclusion_fails():
    """排查任务无结论 → failed。"""
    task = make_task(type_="investigate")
    tasks, pool, rep = FakeTasks(task), FakePool(), FakeReporter()
    llm = ScriptLLM([LLMResult(text="   ", tool_calls=[])])       # 空白结论

    await pipeline_mod.TaskPipeline(tasks, pool, SandboxAgent(llm), rep).run_task(task.id)

    assert task.status == TaskStatus.FAILED and "排查未产出结论" in task.error
    assert any(c[0] == "finished" and c[2] == "failed" for c in rep.calls)


@pytest.mark.asyncio
async def test_pipeline_modify_without_commits_fails():
    """变更任务 HEAD 未领先 base_ref → failed（防空转被误判完成——E2E 95a6e5d3 教训）。"""
    task = make_task(type_="modify")
    tasks, pool, rep = FakeTasks(task), FakePool(), FakeReporter()

    class NoCommitExecutor(FakeExecutor):
        async def exec(self, command: str, *, timeout: float | None = None) -> str:
            # status 空（工作区 clean）+ git log 无领先提交 → 双重确认零提交
            if "status --porcelain" in command:
                return ""
            if "git log" in command:
                return ""
            return await super().exec(command, timeout=timeout)

    pool2 = FakePool()
    pool2.acquire = lambda repo, *, chat_id="", memory=None: self_acq(NoCommitExecutor())  # type: ignore[method-assign]

    async def self_acq(ex):
        pool2.acquired.append("demo")
        return ex

    llm = ScriptLLM([LLMResult(text="我觉得不需要改代码", tool_calls=[])])
    await pipeline_mod.TaskPipeline(tasks, pool2, SandboxAgent(llm), rep).run_task(task.id)

    assert task.status == TaskStatus.FAILED and "未产生任何代码提交" in task.error
    assert pool2.released == ["demo"]
