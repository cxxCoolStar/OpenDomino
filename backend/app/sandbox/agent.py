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
from dataclasses import dataclass, field

from app.config import settings
from app.sandbox.llm import LLMResult, SandboxLLMClient
from app.sandbox.prompts import (
    BUDGET_HALF_NOTICE,
    BUDGET_HIGH_NOTICE,
    EMPTY_SUMMARY_NUDGE,
    FORCE_SUMMARY_NUDGE,
    LOOP_SOFT_NUDGE,
    LOOP_WARNING,
    STEER_PREFIX,
    STRIP_TOOLS_NUDGE,
    build_system_prompt,
)
from app.sandbox.tools import MUTATING_TOOLS, ToolContext, build_tool_definitions, dispatch_tool
from app.sandbox.types import Task, TaskStage

log = logging.getLogger(__name__)

# steer 摘要预览长度（一行事件，非全文——全文在对话里，模型看得到）
_STEER_PREVIEW_CHARS = 80


@dataclass
class StepGuard:
    """反死循环三重保护状态（阈值取 fastclaw 实测值）。"""

    # default_factory 惰性读 settings：测试 monkeypatch settings 才能生效
    # （类属性默认值会在导入时固化 settings 快照，改配置失效）
    max_steps: int = field(default_factory=lambda: settings.sandbox_max_steps)
    steps: int = 0

    same_call_limit: int = field(default_factory=lambda: settings.sandbox_loop_same_call_limit)
    last_sig: tuple | None = None
    same_sig_count: int = 0

    same_tool_fail_limit: int = 2
    last_failed_tool: str = ""
    same_tool_fail_streak: int = 0

    all_failed_limit: int = 3
    all_failed_rounds: int = 0

    _notified: int = 0   # 预算提醒已触发的档位（0 无 / 1 过半 / 2 80%）

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
        if self.same_call_limit >= 3 and self.same_sig_count >= self.same_call_limit - 1:
            # 温和档（hermes 第 3 次才 notice 的前移版）：第 2 次同参同果先提醒，
            # 给模型在第 3 次硬判定前换方法的机会；limit<3 时不设温和档（会误触发首轮）
            return "same_call_soft"
        return None

    def observe_result(self, name: str, failed: bool) -> None:
        if failed:
            if name == self.last_failed_tool:
                self.same_tool_fail_streak += 1
            else:
                self.same_tool_fail_streak = 1
                self.last_failed_tool = name
        elif not failed and name in MUTATING_TOOLS:
            # hermes 同款：mutating 工具成功 → 失败计数清零。「改代码→重跑测试」
            # 是新实验的开始，不是失败重放，不能把后续同工具重试误判成连败。
            self.same_tool_fail_streak = 0
            self.last_failed_tool = ""
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

    def budget_notice(self) -> str | None:
        """预算中途提醒（确定性、每档只触发一次）：过半催收窄，80% 催收敛。

        模型全程感知不到剩余步数是排查任务烧光预算的直接原因之一；
        在迭代边界注入（不是系统提示）保证剩余额度是最新事实而非开局快照。
        """
        used = self.steps
        half, high = self.max_steps // 2, self.max_steps * 8 // 10
        if used >= high and self._notified < 2:
            self._notified = 2
            return "high"
        if used >= half and self._notified < 1:
            self._notified = 1
            return "half"
        return None


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

    async def run(self, task: Task, ctx: ToolContext, *, on_event=None, control=None) -> SandboxRunResult:
        """执行一个任务。

        on_event(stage, kind, summary)：TaskEvent 留痕（ADR-010：每次工具调用
        后触发一次，由管线落库；回调异常绝不影响执行）。
        control：TaskControl 管控面——迭代边界 drain steer 文本、查取消标志。
        """
        on_event = on_event or (lambda *a, **k: None)
        started = time.time()
        guard = StepGuard()

        async def emit(kind: str, summary: str) -> None:
            try:
                await on_event(TaskStage.EXECUTE, kind, summary)
            except Exception:  # noqa: BLE001 —— 留痕失败不影响执行（学习 hermes _safe_progress）
                log.debug("on_event failed task=%s", task.id)

        messages: list[dict] = [
            {"role": "system", "content": build_system_prompt(task)},
            {"role": "user", "content": task.instruction},
        ]
        tools = build_tool_definitions()

        canceled = False
        result = SandboxRunResult(status="failed", exit_reason="error")
        try:
            while True:
                # 管控边界（ADR-010）：上轮工具结果已回填、下次 LLM 调用前——
                # 注入 steer 文本、感知取消请求。当前工具调用永不被打断。
                if control is not None:
                    steers = await control.drain_steer(task.id)
                    for s in steers:
                        messages.append({"role": "user", "content": f"{STEER_PREFIX}{s}"})
                        await emit("steer_applied", f"中途指示已注入：{s[:_STEER_PREVIEW_CHARS]}")
                    if await control.cancel_requested(task.id):
                        canceled = True
                        await emit("cancel_requested", "收到取消请求，停止执行并汇总已有进展")
                        break

                # 预算中途提醒：与 steer 同一迭代边界注入，确定性触发、每档一次
                notice_level = guard.budget_notice()
                if notice_level == "high":
                    notice_text = BUDGET_HIGH_NOTICE.format(
                        left=guard.max_steps - guard.steps, used=guard.steps, total=guard.max_steps)
                    messages.append({"role": "user", "content": notice_text})
                    await emit("budget_notice", notice_text)
                elif notice_level == "half":
                    notice_text = BUDGET_HALF_NOTICE.format(used=guard.steps, total=guard.max_steps)
                    messages.append({"role": "user", "content": notice_text})
                    await emit("budget_notice", notice_text)

                # 摘工具判断（循环头）：护栏触发时本轮不传工具并注入 nudge
                strip = guard.should_strip_tools
                if strip:
                    messages.append({"role": "user", "content": STRIP_TOOLS_NUDGE})

                llm_out: LLMResult = await self._llm.chat(messages, None if strip else tools)

                if not llm_out.tool_calls:
                    # 空交卷兜底：无工具调用但正文空白，不直接当交付（推理模型
                    # reasoning 耗尽预算、content 为空的边界，任务 #dccfa430）。
                    # 注入 nudge 无工具补调一次，仍为空才如实落 failed。
                    if not llm_out.text.strip():
                        messages.append({"role": "assistant", "content": None})
                        messages.append({"role": "user", "content": EMPTY_SUMMARY_NUDGE})
                        rescued = await self._llm.chat(messages, tools=None)
                        if rescued.text.strip():
                            llm_out = rescued
                        else:
                            result = SandboxRunResult(
                                status="failed", steps_used=guard.steps,
                                exit_reason="natural",
                                error="空响应：末轮无工具调用且正文为空，补调索要结论后仍为空",
                            )
                            break
                    result = SandboxRunResult(
                        status="done", summary=llm_out.text,
                        steps_used=guard.steps, exit_reason="natural",
                    )
                    break

                all_failed = True
                # 先回填 assistant(tool_calls) 消息——role=tool 必须紧跟在带 tool_calls 的
                # assistant 消息之后，否则消息序列非法（OpenAI 协议要求，缺失时上游 400）
                messages.append({
                    "role": "assistant",
                    "content": llm_out.text or None,
                    "tool_calls": [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.name,
                                      "arguments": json.dumps(tc.args, ensure_ascii=False)}}
                        for tc in llm_out.tool_calls
                    ],
                })
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
                    # 进展留痕（ADR-010）：每次工具调用一行摘要，落库由管线承担
                    if failed:
                        await emit("tool_error", f"{tc.name} 失败：{_preview_args(tc.args)}")
                    else:
                        await emit("tool_summary", f"{tc.name} {_preview_args(tc.args)}")
                    if verdict == "same_call_loop":
                        messages.append({"role": "user", "content": LOOP_WARNING})
                    elif verdict == "same_call_soft":
                        messages.append({"role": "user", "content": LOOP_SOFT_NUDGE})
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

        if canceled:
            # 协作式取消：对已有进展做保底汇总，状态交由管线落 CANCELED
            result.summary = await self._force_summary(messages)
            result.exit_reason = "canceled"
            result.steps_used = guard.steps
        exit_line = (
            "（收到取消，已停止并汇总）" if canceled
            else f"执行循环结束（{result.exit_reason}，{result.steps_used} 步）"
        )
        await emit("stage_exit", exit_line)
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


def _preview_args(args: dict, limit: int = 80) -> str:
    """工具调用的一行参数预览（事件摘要用，非全文）。"""
    if not args:
        return ""
    first = next(iter(args.values()))
    text = str(first).replace("\n", " ").strip()
    return text[:limit] + ("…" if len(text) > limit else "")


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
