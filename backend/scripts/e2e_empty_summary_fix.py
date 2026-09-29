"""任务 #dccfa430 事故复现与修复验证（可重复运行，不依赖 Docker 与真实 LLM）。

事故链路（2026-09-29）：排查任务执行 32 步后 natural 退出，但末轮 LLM 返回
空 content + 无 tool_calls（推理模型 reasoning 耗尽预算的典型形态），
agent 原样接受空文本 → 管线落「排查未产出结论」失败。

本脚本在真实 TaskPipeline + SandboxAgent 上重放该失败方式：
- 场景 A（修复前行为）：末轮空响应。修复后应被 EMPTY_SUMMARY_NUDGE 补调救回，
  任务 DONE 且结论非空；
- 场景 B（极端边界）：补调后仍为空。任务应 FAILED 且 error 带诊断信息
  （而非事故中无信息量的「排查未产出结论」）。

产物：data/e2e_empty_summary_fix_report.json

用法：
    cd backend
    python scripts/e2e_empty_summary_fix.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.sandbox.agent import SandboxAgent            # noqa: E402
from app.sandbox.llm import LLMResult                 # noqa: E402
from app.sandbox.pipeline import TaskPipeline         # noqa: E402
from app.sandbox.types import Task, TaskStatus        # noqa: E402


class _FakeTasks:
    """storage.TaskService 内存版（与 tests/test_sandbox.py FakeTasks 同构）。"""

    def __init__(self, task: Task) -> None:
        self.task = task
        self.finish_args: tuple = ()

    async def get(self, tid):
        return self.task

    async def set_status(self, tid, status):
        self.task.status = status

    async def set_running(self, tid, **kw):
        pass

    async def set_review(self, tid, **kw):
        pass

    async def finish(self, tid, status, error="", verify_result=""):
        self.task.status = status
        self.task.error = error
        self.task.verify_result = verify_result
        self.finish_args = (status.value, error, verify_result)

    async def append_event(self, tid, stage, kind, summary):
        pass


class _FakePool:
    async def acquire(self, repo, **kw):
        return object()

    async def reset_workspace(self, repo, executor, base_ref):
        return base_ref or "HEAD"

    async def release(self, repo, executor):
        pass


class _FakeReporter:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def started(self, task):
        self.calls.append(("started", task.status.value))

    async def concluded(self, task, conclusion):
        self.calls.append(("concluded", conclusion[:60]))

    async def finished(self, task):
        self.calls.append(("finished", task.status.value, task.error or ""))


def _investigate_task() -> Task:
    return Task(
        id="e2e-empty-summary", title="梳理待开发需求清单",
        instruction="排查仓库当前进度并产出待开发需求清单（复现 #dccfa430）",
        repo="OpenDomino", type="investigate", status=TaskStatus.PENDING,
        chat_id="e2e-chat", session_id="e2e", created_by="e2e-script",
    )


class _Dccfa430LLM:
    """重放事故形态：先正常干活（带工具），末轮空 content + 无 tool_calls。

    rescue_once 控制「补调索要结论」这一脚是否给真话：
    True  → 模型被救回（修复后场景 A 的预期）；
    False → 补调后仍为空（极端边界，场景 B）。
    """

    def __init__(self, rescue_once: bool) -> None:
        self.rescue_once = rescue_once
        self.n = 0

    async def chat(self, messages, tools=None):
        self.n += 1
        if tools is not None and self.n == 1:
            return LLMResult(text="", tool_calls=[])   # 直接空交卷（事故末轮形态）
        if tools is None:
            # 无工具调用 = agent 的空交卷补调
            if self.rescue_once:
                return LLMResult(
                    text="排查结论：1) ADR-007 Langfuse 未实现；2) verify 回炉未实现。", tool_calls=[])
            return LLMResult(text="", tool_calls=[])
        return LLMResult(text="", tool_calls=[])


async def _run_scenario(scenario: str, rescue_once: bool) -> dict:
    task = _investigate_task()
    rep = _FakeReporter()
    pipeline = TaskPipeline(_FakeTasks(task), _FakePool(),
                            SandboxAgent(_Dccfa430LLM(rescue_once)), rep)

    t0 = time.time()
    await pipeline.run_task(task.id)
    return {
        "scenario": scenario,
        "status": task.status.value,
        "error": task.error,
        "verify_result": task.verify_result,
        "reporter": [list(c) for c in rep.calls],
        "duration_seconds": round(time.time() - t0, 1),
    }


async def main() -> int:
    a = await _run_scenario("A-rescued", rescue_once=True)
    b = await _run_scenario("B-still-empty", rescue_once=False)

    # 场景 A：空交卷被补调救回 → DONE + 结论非空
    assert a["status"] == "done", f"场景 A 应 DONE，实际 {a['status']}: {a['error']}"
    assert a["verify_result"] == "conclusion" and a["error"] == ""
    assert any(c[0] == "concluded" for c in a["reporter"])
    # 场景 B：补调后仍空 → FAILED 且 error 可诊断（不再是「排查未产出结论」）
    assert b["status"] == "failed", f"场景 B 应 FAILED，实际 {b['status']}"
    assert "空响应" in b["error"], b["error"]
    assert any(c[0] == "finished" and c[1] == "failed" for c in b["reporter"])

    out = Path("data/e2e_empty_summary_fix_report.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"scenarios": [a, b]}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"场景 A（空交卷→补调救回）: DONE, verify={a['verify_result']}")
    print(f"场景 B（补调仍空）        : FAILED, error={b['error'][:40]}…")
    print(f"\n✅ E2E PASS —— 产物: {out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
