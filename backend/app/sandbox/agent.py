"""SandboxAgent 受控执行循环（ADR-004 详细设计 §1）。

骨架（hermes-agent 同构）：LLM 调用 → 有 tool_calls 则执行回填 → 无则视为完成。
护栏：迭代预算 / 同调用死循环检测 / 同工具连败摘工具 / 整轮全败摘工具，
预算耗尽触发一次无工具的强制汇总（fastclaw capReachedNudge 同款），保底交付。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass

from app.config import settings
from app.sandbox.llm import LLMResult, SandboxLLMClient
from app.sandbox.prompts import (
    FORCE_SUMMARY_NUDGE,
    LOOP_WARNING,
    STRIP_TOOLS_NUDGE,
    build_system_prompt,
)
from app.sandbox.tools import ToolContext, build_tool_definitions, dispatch_tool
from app.sandbox.types import Task, TaskStage

log = logging.getLogger(__name__)


@dataclass
class StepGuard:
    """反死循环三重保护状态（阈值取 fastclaw 实测值）。"""

    max_steps: int = settings.sandbox_max_steps
    steps: int = 0

    same_call_limit: int = settings.sandbox_loop_same_call_limit
    last_sig: tuple | None = None
    same_sig_count: int = 0

    same_tool_fail_limit: int = 2
    last_failed_tool: str = ""
    same_tool_fail_streak: int = 0

    all_failed_limit: int = 3
    all_failed_rounds: int = 0

    def observe_call(self, name: str, args: dict, output: str) -> str | None:
        """记录一次调用。返回触发的护栏名（无则 None）。"""
        self.steps += 1
        sig = (
            name,
            hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            hashlib.sha256(output.strip().encode()).hexdigest(),
        )
        if sig == self.last_sig:
            self.same_sig_count += 1
        else:
            self.last_sig = sig
            self.same_sig_count = 1
        if self.same_sig_count >= self.same_call_limit:
            return "same_call_loop"
        return None

    def observe_result(self, name: str, failed: bool) -> None:
        if failed:
            if name == self.last_failed_tool:
                self.same_tool_fail_streak += 1
            else:
                self.same_tool_fail_streak = 1
                self.last_failed_tool = name
        elif name == self.last_failed_tool:
            self.same_tool_fail_streak = 0
            self.last_failed_tool = ""

    def observe_round(self, all_failed: bool) -> None:
        self.all_failed_rounds = self.all_failed_rounds + 1 if all_failed else 0

    @property
    def should_strip_tools(self) -> bool:
        """同工具连败 ≥2 或整轮全败 ≥3 → 摘除工具（先连败，避免同轮双 nudge）。"""
        return (
            self.same_tool_fail_streak >= self.same_tool_fail_limit
            or self.all_failed_rounds >= self.all_failed_limit
        )

    @property
    def budget_exhausted(self) -> bool:
        return self.steps >= self.max_steps


@dataclass
class SandboxRunResult:
    status: str                 # done | failed | summary（强制汇总交付）
    summary: str = ""
    steps_used: int = 0
    exit_reason: str = ""       # natural | budget_exhausted | timeout | error
    error: str = ""


class SandboxAgent:
    def __init__(self, llm: SandboxLLMClient) -> None:
        self._llm = llm

    async def run(self, task: Task, ctx: ToolContext, *, on_event=None) -> SandboxRunResult:
        """执行一个任务。on_event(stage, kind, summary) 用于 TaskEvent 留痕。"""
        on_event = on_event or (lambda *a, **k: None)
        started = time.time()
        guard = StepGuard()

        messages: list[dict] = [
            {"role": "system", "content": build_system_prompt(task)},
            {"role": "user", "content": task.instruction},
        ]
        tools = build_tool_definitions()

        result = SandboxRunResult(status="failed", exit_reason="error")
        try:
            while True:
                # 摘工具判断（循环头）：护栏触发时本轮不传工具并注入 nudge
                strip = guard.should_strip_tools
                if strip:
                    messages.append({"role": "user", "content": STRIP_TOOLS_NUDGE})

                llm_out: LLMResult = await self._llm.chat(messages, None if strip else tools)

                if not llm_out.tool_calls:
                    result = SandboxRunResult(
                        status="done", summary=llm_out.text,
                        steps_used=guard.steps, exit_reason="natural",
                    )
                    break

                all_failed = True
                for tc in llm_out.tool_calls:
                    output = await dispatch_tool(tc.name, tc.args, ctx)
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": output}
                    )
                    verdict = guard.observe_call(tc.name, tc.args, output)
                    failed = _is_failed_result(output)
                    guard.observe_result(tc.name, failed)
                    if not failed:
                        all_failed = False
                    if verdict == "same_call_loop":
                        messages.append({"role": "user", "content": LOOP_WARNING})
                guard.observe_round(all_failed)

                # 历史超长：确定性裁剪旧工具结果（零 LLM 调用）
                _trim_history(messages)

                if guard.budget_exhausted:
                    summary = await self._force_summary(messages)
                    result = SandboxRunResult(
                        status="summary", summary=summary,
                        steps_used=guard.steps, exit_reason="budget_exhausted",
                    )
                    break

                if time.time() - started > settings.sandbox_timeout_minutes * 60:
                    summary = await self._force_summary(messages)
                    result = SandboxRunResult(
                        status="summary", summary=summary,
                        steps_used=guard.steps, exit_reason="timeout",
                    )
                    break
        except Exception as e:  # noqa: BLE001 —— 循环崩溃也要给群一个交代
            log.exception("sandbox loop crashed task=%s", task.id)
            result = SandboxRunResult(
                status="failed", steps_used=guard.steps,
                exit_reason="error", error=f"{type(e).__name__}: {e}",
            )

        on_event(TaskStage.EXECUTE, "stage_exit",
                 f"执行循环结束（{result.exit_reason}，{result.steps_used} 步）")
        return result

    async def _force_summary(self, messages: list[dict]) -> str:
        """预算耗尽/超时：剥掉工具再调一次，把已完成的交付如实汇总。"""
        try:
            final = await self._llm.chat(
                messages + [{"role": "user", "content": FORCE_SUMMARY_NUDGE}], tools=None
            )
            if final.text.strip():
                return final.text
        except Exception:  # noqa: BLE001
            log.exception("force summary failed")
        return "任务达到步数上限且汇总调用失败；已有改动保存在任务分支上，请人工查看。"


def _is_failed_result(output: str) -> bool:
    """失败判定保守：exec 空输出不算失败；只认错误信封（{"error": ...}）。"""
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(data, dict) and "error" in data


def _trim_history(messages: list[dict]) -> None:
    """总字符量超阈值时，把较旧的工具结果替换为占位符，保留最近 N 条完整。

    确定性、零 LLM 调用（hermes prune_tool_results_only 同款思路）。
    """
    total = sum(len(str(m.get("content") or "")) for m in messages)
    if total <= settings.sandbox_history_trim_chars:
        return
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    keep = set(tool_indices[-settings.sandbox_history_keep_recent:])
    for i in tool_indices:
        if i not in keep:
            messages[i]["content"] = "[已裁剪]"
