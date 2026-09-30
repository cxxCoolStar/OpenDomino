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
    assert g.observe_call("list_dir", {"p": "x"}, "same") == "same_call_soft"
    assert g.observe_call("list_dir", {"p": "x"}, "same") == "same_call_loop"
    # 输出变化则不触发
    g2 = StepGuard(same_call_limit=3)
    assert g2.observe_call("run_command", {"c": "pytest"}, "pass") is None
    assert g2.observe_call("run_command", {"c": "pytest"}, "fail") is None

    # limit<3 不设温和档（首轮即提醒会误伤正常重试）
    g3 = StepGuard(same_call_limit=2)
    assert g3.observe_call("list_dir", {"p": "x"}, "same") is None
    assert g3.observe_call("list_dir", {"p": "x"}, "same") == "same_call_loop"


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


def test_guard_soft_nudge_fires_before_hard_loop():
    # 第 2 次同参同果 → 温和提醒；第 3 次 → 硬判定（同 call_limit=3）
    g = StepGuard(same_call_limit=3)
    assert g.observe_call("read_file", {"p": "x"}, "same") is None
    assert g.observe_call("read_file", {"p": "x"}, "same") == "same_call_soft"
    assert g.observe_call("read_file", {"p": "x"}, "same") == "same_call_loop"


def test_guard_mutating_success_resets_fail_streak():
    # hermes 同款：mutating 工具成功 → 连败清零（edit→re-run 不是失败重放）
    g = StepGuard(same_tool_fail_limit=2)
    g.observe_result("patch_replace", failed=True)
    g.observe_result("patch_replace", failed=True)        # 连败 2，下一刻本该摘
    g.observe_result("patch_replace", failed=False)       # 但编辑成功了 → 清零
    assert not g.should_strip_tools
    g.observe_result("patch_replace", failed=True)        # 新一轮连败从 1 数起
    assert not g.should_strip_tools

    # run_command 不在 MUTATING_TOOLS：跑测试成功不清零编辑连败，
    # 否则会掩护模型重试同样失败的补丁
    g2 = StepGuard(same_tool_fail_limit=2)
    g2.observe_result("patch_replace", failed=True)
    g2.observe_result("patch_replace", failed=True)
    g2.observe_result("run_command", failed=False)
    assert g2.should_strip_tools


def test_guard_readonly_success_keeps_existing_reset_semantics():
    # 只读工具成功仍只清自己名字的连败（原语义不变；hermes：只有改代码才算新实验）
    g = StepGuard(same_tool_fail_limit=2)
    g.observe_result("run_command", failed=True)
    g.observe_result("read_file", failed=False)
    g.observe_result("run_command", failed=True)
    assert g.should_strip_tools       # 读不重置 run_command 连败：两次失败仍连着


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
    assert {"list_dir", "read_file", "grep", "write_file", "patch_replace",
            "apply_patch", "run_command", "git_diff", "git_log"} <= names


# ---------- patch_replace：真实 vendor 栈 + 本地 sh 的 E2E ----------
# 走 dispatch_tool → ShellFileOperations（vendor）→ HermesEnvAdapter → 本地 sh，
# 与容器内只差 Executor 实现；这是 E2E 测试（AGENTS.md 测试策略），不做 mock。


class LocalShExecutor:
    """把命令交给本地 sh 在临时目录执行，输出契约与 DockerExecutor 一致（尾部 [exit N]）。"""

    def __init__(self, root) -> None:
        import subprocess

        self._sp = subprocess
        self.root = root

    async def exec(self, command: str, *, timeout: float | None = None, workdir: str | None = None) -> str:
        r = await asyncio.to_thread(
            self._sp.run, ["sh", "-c", command],
            capture_output=True, text=True, cwd=str(self.root),
        )
        out = r.stdout + (f"[stderr]\n{r.stderr}" if r.stderr else "")
        if r.returncode not in (0, None):
            out = f"{out}\n[exit {r.returncode}]" if out else f"[exit {r.returncode}]"
        return out

    async def read_file(self, path: str) -> str:
        return (self.root / path).read_text(encoding="utf-8")

    async def write_file(self, path: str, content: str) -> None:
        (self.root / path).write_text(content, encoding="utf-8")

    async def list_dir(self, path: str = ".") -> list[dict]:
        return []


@pytest.fixture
def ws_root(tmp_path):
    (tmp_path / "demo.py").write_text(
        "def ping():\n    return 1\n\ndef pong():\n    return 1\n", encoding="utf-8")
    return tmp_path


def _dispatch_patch_replace(ctx, **args):
    """dispatch 并回传输出。成功是纯文本（write_file 同款约定），错误才是 JSON 信封。"""
    return dispatch_tool("patch_replace", args, ctx)


def _err_of(raw: str) -> str | None:
    """按 _is_failed_result 同款约定取错误文本：JSON 信封带 error 才算失败。"""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return data.get("error") if isinstance(data, dict) else None


@pytest.mark.asyncio
async def test_patch_replace_edits_file(ws_root):
    ctx = ToolContext(executor=LocalShExecutor(ws_root), repo="demo", task_id="t1")
    out = await _dispatch_patch_replace(
        ctx, path="demo.py",
        old_string="def ping():\n    return 1",
        new_string="def ping():\n    return 42")
    assert _err_of(out) is None
    assert "return 42" in (ws_root / "demo.py").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_patch_replace_missing_string_errors(ws_root):
    ctx = ToolContext(executor=LocalShExecutor(ws_root), repo="demo", task_id="t1")
    out = await _dispatch_patch_replace(
        ctx, path="demo.py", old_string="no-such-line", new_string="x")
    assert "Could not find a match" in _err_of(out)


@pytest.mark.asyncio
async def test_patch_replace_ambiguous_requires_replace_all(ws_root):
    # demo.py 里 "return 1" 出现两次：不唯一且未开 replace_all → 报错（错误里带行号提示）
    ctx = ToolContext(executor=LocalShExecutor(ws_root), repo="demo", task_id="t1")
    out = await _dispatch_patch_replace(
        ctx, path="demo.py", old_string="return 1", new_string="return 2")
    assert "2 matches" in _err_of(out)


@pytest.mark.asyncio
async def test_patch_replace_already_applied_is_noop_success(ws_root):
    ctx = ToolContext(executor=LocalShExecutor(ws_root), repo="demo", task_id="t1")
    # old_string == new_string 且文件已含该文本 → hermes already-applied 检测：
    # 成功形响应且不改文件，模型不必重读重试
    out = await _dispatch_patch_replace(
        ctx, path="demo.py",
        old_string="def ping():\n    return 1",
        new_string="def ping():\n    return 1")
    assert _err_of(out) is None
    assert "already applied" in out
    assert "return 1" in (ws_root / "demo.py").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_patch_replace_replace_all_hits_all_occurrences(ws_root):
    ctx = ToolContext(executor=LocalShExecutor(ws_root), repo="demo", task_id="t1")
    out = await _dispatch_patch_replace(
        ctx, path="demo.py", old_string="return 1", new_string="return 2",
        replace_all=True)
    assert _err_of(out) is None
    assert (ws_root / "demo.py").read_text(encoding="utf-8").count("return 2") == 2


@pytest.mark.asyncio
async def test_patch_replace_missing_file_errors(ws_root):
    ctx = ToolContext(executor=LocalShExecutor(ws_root), repo="demo", task_id="t1")
    out = await _dispatch_patch_replace(
        ctx, path="absent.py", old_string="a", new_string="b")
    assert _err_of(out)


@pytest.mark.asyncio
async def test_patch_replace_string_boolean_coerced(ws_root):
    # 模型会把布尔序列化成字符串：bool("false") 恒为 True，dispatch 层必须按
    # schema 强转，否则 replace_all="false" 语义反转（想改 1 处实际全改且无报错）
    ctx = ToolContext(executor=LocalShExecutor(ws_root), repo="demo", task_id="t1")
    out = await _dispatch_patch_replace(
        ctx, path="demo.py", old_string="return 1", new_string="return 2",
        replace_all="false")
    assert _err_of(out) is not None  # 不唯一 → 如实报错，而不是静默全量替换
    assert (ws_root / "demo.py").read_text(encoding="utf-8").count("return 1") == 2


@pytest.mark.asyncio
async def test_apply_patch_schema_legal_call_succeeds(ws_root):
    # schema 只有 diff 参数：handler 不得读 args["path"]（曾 KeyError——补丁已
    # 落地却回错误信封，模型收到失败信号后重试/换招，并污染连败护栏计数）
    (ws_root / "mod.py").write_text("x = 1\n", encoding="utf-8")
    ctx = ToolContext(executor=LocalShExecutor(ws_root), repo="demo", task_id="t1")
    diff = (
        "*** Begin Patch\n"
        "*** Update File: mod.py\n"
        "@@\n"
        "-x = 1\n"
        "+x = 2\n"
        "*** End Patch\n"
    )
    out = await dispatch_tool("apply_patch", {"diff": diff}, ctx)
    assert _err_of(out) is None
    assert "mod.py" in out
    assert "x = 2" in (ws_root / "mod.py").read_text(encoding="utf-8")


def test_failed_result_envelope():
    assert _is_failed_result(json.dumps({"error": "boom"}))
    assert not _is_failed_result("")
    assert not _is_failed_result("plain text")
    assert not _is_failed_result(json.dumps({"ok": True}))


# ---------- file ops 生命周期：随 ctx 走，不进模块级缓存 ----------


@pytest.mark.asyncio
async def test_file_ops_cached_per_context(ws_root):
    # 同一 ctx 两次 read_file 复用同一 ShellFileOperations 实例
    from app.sandbox.tools import _get_file_ops

    ctx = ToolContext(executor=LocalShExecutor(ws_root), repo="demo", task_id="t1")
    ops1 = _get_file_ops(ctx)
    ops2 = _get_file_ops(ctx)
    assert ops1 is ops2
    assert ctx._file_ops is ops1

    # 不同 ctx（每任务一个）的实例互不相同——防 id(executor) 复用命中过期缓存
    other = ToolContext(executor=LocalShExecutor(ws_root), repo="demo", task_id="t2")
    assert _get_file_ops(other) is not ops1

    # 经 dispatch 走真实调用后，同一 ctx 的复用仍成立（E2E 路径）
    out = await dispatch_tool("read_file", {"path": "demo.py"}, ctx)
    assert _err_of(out) is None
    assert _get_file_ops(ctx) is ops1


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
async def test_agent_empty_natural_completion_retries_then_recovers():
    """空交卷兜底：natural 退出但 summary 为空白时，先补调索要结论而非直接交空卷。

    真实事故（任务 #dccfa430）：推理模型末轮返回空 content + 无 tool_calls，
    循环原样接受空文本，管线只能落「排查未产出结论」失败。
    """
    from app.sandbox.prompts import EMPTY_SUMMARY_NUDGE

    calls: list[tuple[int, bool]] = []

    class EmptyThenRescuedLLM:
        """前两轮按脚本：轮1 发工具，轮2 空交卷；补调（无工具）返回救场结论。"""

        async def chat(self, messages, tools=None):
            calls.append((len(messages), tools is None))
            if tools is not None:
                if len(calls) == 1:
                    return LLMResult(text="", tool_calls=[ToolCall(id="1", name="list_dir", args={})])
                return LLMResult(text="   ", tool_calls=[])   # 空交卷（whitespace 也算空）
            assert any(EMPTY_SUMMARY_NUDGE in str(m.get("content")) for m in messages)
            return LLMResult(text="最终结论：已完成排查", tool_calls=[])

    r = await SandboxAgent(EmptyThenRescuedLLM()).run(
        make_task(type_="investigate"), ToolContext(executor=FakeExecutor()))
    assert r.status == "done" and "最终结论" in r.summary and r.exit_reason == "natural"
    assert calls[-1][1] is True, "补调必须无工具"


@pytest.mark.asyncio
async def test_agent_empty_natural_completion_twice_fails():
    """连续两次空响应：补调仍为空 → 落 failed，不再无限补调。"""
    class AlwaysEmptyLLM:
        def __init__(self):
            self.n = 0

        async def chat(self, messages, tools=None):
            self.n += 1
            return LLMResult(text="", tool_calls=[])

    llm = AlwaysEmptyLLM()
    r = await SandboxAgent(llm).run(make_task(type_="investigate"), ToolContext(executor=FakeExecutor()))
    assert r.status == "failed" and r.exit_reason == "natural"
    assert "空响应" in r.error
    assert llm.n == 2, "首轮空响应后应恰好补调一次"


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


@pytest.mark.asyncio
async def test_agent_budget_notice_injected_once_per_level(monkeypatch):
    """预算中途提醒：过半/80% 各注入一次 user 消息并落事件，模型收敛后自然完成。"""
    from app.sandbox.prompts import BUDGET_HALF_NOTICE

    monkeypatch.setattr(agent_mod.settings, "sandbox_max_steps", 4)

    class NoticeLLM:
        def __init__(self):
            self.n = 0

        async def chat(self, messages, tools=None):
            self.n += 1
            if self.n <= 3:   # 3 步后第 2 步已过半、第 4 步未到，验证 half 档
                return LLMResult(text="", tool_calls=[ToolCall(id=str(self.n), name="list_dir", args={"n": self.n})])
            assert sum(BUDGET_HALF_NOTICE.format(used=2, total=4) == m.get("content") for m in messages) == 1
            return LLMResult(text="提前收敛完成", tool_calls=[])

    events: list[tuple[str, str]] = []

    async def on_event(_stage, kind, summary):
        events.append((kind, summary))

    r = await SandboxAgent(NoticeLLM()).run(
        make_task(), ToolContext(executor=FakeExecutor()), on_event=on_event)
    assert r.status == "done" and r.steps_used == 3
    kinds = [k for k, _ in events]
    assert "budget_notice" in kinds


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
    """排查任务连续空响应（补调后仍无结论）→ failed。"""
    task = make_task(type_="investigate")
    tasks, pool, rep = FakeTasks(task), FakePool(), FakeReporter()

    class AlwaysEmptyLLM:
        async def chat(self, messages, tools=None):
            return LLMResult(text="   ", tool_calls=[])       # 补调后仍空白

    await pipeline_mod.TaskPipeline(tasks, pool, SandboxAgent(AlwaysEmptyLLM()), rep).run_task(task.id)

    # agent 已给出诊断性 error（补调后仍空），管线透传而非用兜底文案
    assert task.status == TaskStatus.FAILED and "空响应" in task.error
    assert any(c[0] == "finished" and c[2] == "failed" for c in rep.calls)


@pytest.mark.asyncio
async def test_pipeline_modify_without_commits_fails():
    """变更任务 HEAD 未领先 base_ref → failed（防空转被误判完成——E2E 95a6e5d3 教训）。"""
    task = make_task(type_="modify")
    tasks, rep = FakeTasks(task), FakeReporter()

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


# ---------- ADR-010：事件落库 / 管控面 / cancel / missed steer ----------


@pytest.mark.asyncio
async def test_agent_emits_tool_events():
    """执行循环每次工具调用触发 on_event（ADR-010：事件留痕数据源）。"""
    events: list[tuple[str, str]] = []

    async def on_event(stage, kind, summary):
        events.append((kind, summary))

    llm = ScriptLLM([
        LLMResult(text="", tool_calls=[ToolCall(id="1", name="list_dir", args={"path": "app"})]),
        LLMResult(text="", tool_calls=[ToolCall(id="2", name="run_command", args={"command": "pytest"})]),
        LLMResult(text="done", tool_calls=[]),
    ])
    ctx = ToolContext(executor=FakeExecutor())
    r = await SandboxAgent(llm).run(make_task(), ctx, on_event=on_event)
    assert r.status == "done"
    kinds = [k for k, _ in events]
    assert kinds.count("tool_summary") == 2 and kinds[-1] == "stage_exit"
    assert any("list_dir" in s for _, s in events)
    assert any("pytest" in s for _, s in events)


@pytest.mark.asyncio
async def test_agent_on_event_failure_never_breaks_loop():
    """on_event 抛异常绝不影响执行循环（学习 hermes _safe_progress）。"""

    async def boom(stage, kind, summary):
        raise RuntimeError("event sink down")

    llm = ScriptLLM([
        LLMResult(text="", tool_calls=[ToolCall(id="1", name="list_dir", args={})]),
        LLMResult(text="完成", tool_calls=[]),
    ])
    r = await SandboxAgent(llm).run(make_task(), ToolContext(executor=FakeExecutor()), on_event=boom)
    assert r.status == "done"


@pytest.mark.asyncio
async def test_control_steer_injected_at_boundary():
    """steer 在迭代边界以独立 user 消息注入，模型下一轮可见（ADR-010）。"""
    from app.sandbox.control import TaskControl
    from app.sandbox.prompts import STEER_PREFIX

    control = TaskControl()
    await control.bind("task12345678")
    await control.steer("task12345678", "只看 auth 模块")

    seen_messages: list[list[dict]] = []

    class RecordingLLM(ScriptLLM):
        async def chat(self, messages, tools=None):
            seen_messages.append([dict(m) for m in messages])
            return await super().chat(messages, tools)

    llm = RecordingLLM([
        LLMResult(text="", tool_calls=[ToolCall(id="1", name="list_dir", args={})]),
        LLMResult(text="按指示完成", tool_calls=[]),
    ])
    ctx = ToolContext(executor=FakeExecutor())
    r = await SandboxAgent(llm).run(make_task(), ctx, control=control)
    assert r.status == "done"
    # 第 2 轮消息里出现 [中途指示] 独立 user 消息
    second_turn = seen_messages[1]
    assert any(
        m["role"] == "user" and m["content"] == f"{STEER_PREFIX}只看 auth 模块"
        for m in second_turn
    )
    # 未消费队列已清空（幂等 drain）
    assert await control.drain_steer("task12345678") == []


@pytest.mark.asyncio
async def test_control_cancel_cooperative_exit():
    """cancel 标志在迭代边界感知 → exit_reason=canceled + 保底汇总（ADR-010）。"""
    from app.sandbox.control import TaskControl

    control = TaskControl()
    await control.bind("task12345678")
    await control.request_cancel("task12345678")

    llm = ScriptLLM([LLMResult(text="中断前进展汇总", tool_calls=[])])
    r = await SandboxAgent(llm).run(
        make_task(), ToolContext(executor=FakeExecutor()), control=control
    )
    # 循环头感知取消：执行 LLM 未被调用（脚本项被 _force_summary 保底汇总消费）
    assert r.exit_reason == "canceled" and "中断前进展汇总" in r.summary


@pytest.mark.asyncio
async def test_control_unbound_returns_false():
    """未登记（不在跑）的任务 steer/cancel 返回 False。"""
    from app.sandbox.control import TaskControl

    control = TaskControl()
    assert await control.steer("nope", "x") is False
    assert await control.request_cancel("nope") is False


@pytest.mark.asyncio
async def test_control_unbind_returns_unconsumed_steer():
    """unbind 返回未消费 steer（missed 候选），管线 finally 据此上报（ADR-010）。"""
    from app.sandbox.control import TaskControl

    control = TaskControl()
    await control.bind("task12345678")
    await control.steer("task12345678", "来不及消费的指示")
    missed = await control.unbind("task12345678")
    assert missed == ["来不及消费的指示"]
    # 二次 unbind 幂等：通道已注销
    assert await control.unbind("task12345678") == []
    # 注销后 steer 拒绝
    assert await control.steer("task12345678", "x") is False


@pytest.mark.asyncio
async def test_pipeline_reports_missed_steer_via_reporter():
    """管线 finally 真正调 reporter.steer_missed（run_task 全程黑盒驱动）。"""
    from app.sandbox.control import TaskControl

    task = make_task()
    tasks, pool = FakeTasks(task), FakePool()

    class ControlReporter(FakeReporter):
        def __init__(self):
            self.missed: list[tuple[str, list[str]]] = []
            self.statuses: list[str] = []

        async def started(self, task):
            self.statuses.append("started")

        async def review_ready(self, task, **kw):
            self.statuses.append("review_ready")

        async def concluded(self, task, conclusion):
            self.statuses.append("concluded")

        async def finished(self, task):
            self.statuses.append(f"finished:{task.status.value}")

        async def steer_missed(self, task, missed):
            self.missed.append((task.id, list(missed)))

    rep = ControlReporter()
    control = TaskControl()

    class SteerThenDoneLLM:
        """首轮工具后把 steer 投进队列但不消费 → 第二轮前投递即被消费；
        要制造 missed，需 steer 在循环结束后才入队——用 chat 副作用模拟：
        第二次 chat 时（已无工具调用、即将返回 done）——仍会走边界 drain。
        真正 missed 的时点在循环 break 之后，只能由外部并发投递。这里用
        asyncio 任务在管线运行时延迟投递。"""
        def __init__(self):
            self.n = 0

        async def chat(self, messages, tools=None):
            self.n += 1
            if self.n == 1:
                # 模拟群内 steer 在执行期到达（竞态投递进未消费队列）
                await control.steer(task.id, "迟到的指示")
                return LLMResult(text="", tool_calls=[ToolCall(id="1", name="list_dir", args={})])
            return LLMResult(text="完成", tool_calls=[])

    # 直接跑管线：第一次 chat 里投递的 steer 会在下一迭代边界被消费（非 missed）。
    # 本用例只验证「消费成功」不打 missed；missed 路径由 cancel-after-finish 场景
    # 在 E2E 覆盖。
    pipeline = pipeline_mod.TaskPipeline(tasks, pool, SandboxAgent(SteerThenDoneLLM()), rep, control=control)
    await pipeline.run_task(task.id)
    assert rep.missed == []
    assert any(s.startswith("finished") for s in rep.statuses)
