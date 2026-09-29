"""任务完成提炼（ADR-002 Phase B / ADR-003 触发点 3）。

任务 DONE 后对该任务的「需求 → 执行产出」做一次独立 LLM 提炼，把长期有效的
仓库/团队级经验产出为 KBase 草稿，经 pending_memory 确认流落库（写入必须确认，
绝不静默入库）。modify 与 investigate 两类任务都提炼（2026-09-29 实施确认）。

与蒸馏（ADR-009）的分工：蒸馏从群聊讨论沉淀「团队约定」，本模块从任务执行
沉淀「仓库工程事实」。两者共用确认流、草稿结构与单槽位规则（已有未过期待
确认记忆时跳过本次）。

三态结局与蒸馏同构：
- 无可沉淀 → 不提议（一次性操作细节不得入库，靠提示词守住）；
- 提议 → 建草稿挂槽位 + 群内确认提示（source_type=task）；
- LLM 异常/解析失败 → 只记日志，绝不影响任务交付消息（提炼在任务回传之后异步跑）。

提炼输入取轻量三件套（方案 A，实施确认）：instruction + 执行汇总/结论 + diff 统计，
不给完整 diff——经验在汇总与结论里，不在代码细节里。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Protocol

from app.config import settings
from app.memory.types import MEMORY_SOURCE_TASK, MemoryDraft, PendingState

log = logging.getLogger(__name__)

_MEM_CONFIRM = "memory_confirm"

_TASK_DISTILL_SYSTEM = """你是研发团队的工程知识提炼助手。一个由 AI 沙箱执行的任务刚完成，\
你要判断这次执行是否产出了值得沉淀为「团队长期记忆」的工程经验。

只提取同时满足以下条件的结论：
1. 长期有效（下次遇到同类任务仍然成立）：仓库结构、构建/测试方式、环境约束、\
易错点、排查路径、团队约定；
2. 自包含（脱离本次任务也能读懂，写明仓库名等限定语，不用「本次」「该文件」等指代）；
3. 是事实或经验，不是本次任务的操作流水（改了哪个文件、提交了什么这类细节不要）；
4. 不含凭据、token、内网地址等敏感信息。

如果这次执行没有任何符合条件的结果（大多数普通任务没有），返回 {"distilled": false}，\
不得强行生成。

若结论与「现有团队记忆」中的某条冲突（旧结论已过时），设置 supersedes_id 为该条 id。

输出 JSON（不要输出其他内容）：
{"distilled": true, "title": "主题（30字内）", "statement": "自包含的结论陈述", \
"rationale": "原因或适用条件（可为空）", "memory_type": "decision|convention|troubleshooting|environment", \
"keywords": ["关键词"], "supersedes_id": "替代的旧条目id，无则为空"}"""


class DistillLLM(Protocol):
    async def complete(self, system: str, user: str, *, model: str | None = None) -> str: ...


_JSON_LAZY_RE = re.compile(r"\{.*?\}", re.DOTALL)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _first_json_object(text: str) -> str | None:
    """从可能拼接了多个 JSON 对象的文本中抓出第一个可解析的完整对象（与蒸馏器同法）。"""
    for m in _JSON_LAZY_RE.finditer(text):
        candidate = m.group(0)
        try:
            json.loads(candidate)
            return candidate
        except ValueError:
            continue
    m = _JSON_RE.search(text)
    return m.group(0) if m else None


def parse_task_distill_output(raw: str) -> dict | None:
    """解析提炼输出。dict=提议；None=明确无可沉淀；解析失败抛 ValueError。"""
    if not raw or not raw.strip():
        raise ValueError("empty task distill output")
    text = raw.strip()
    try:
        data = json.loads(text)
    except ValueError:
        candidate = _first_json_object(text)
        if candidate is None:
            raise ValueError(f"no JSON in task distill output: {text[:120]}")
        try:
            data = json.loads(candidate)
        except ValueError as e:
            raise ValueError(f"invalid JSON in task distill output: {text[:120]}") from e
    if not isinstance(data, dict):
        raise ValueError("task distill output is not an object")
    if not data.get("distilled"):
        return None
    statement = str(data.get("statement") or "").strip()
    if not statement:
        raise ValueError("distilled=true but statement missing")
    return data


class TaskDistiller:
    """任务完成提炼器：pipeline 在任务 DONE 后经 here 异步触发，不阻塞回传。"""

    def __init__(self, memory, llm: DistillLLM, *, chat_locks=None, reply=None) -> None:
        self._memory = memory
        self._llm = llm
        self._chat_locks = chat_locks   # 与消息管线/蒸馏共用的 per-chat 锁注册表
        self._reply = reply

    def submit(self, *, task_id: str, chat_id: str, repo: str, task_type: str,
               instruction: str, outcome: str, diff_stat: str = "") -> None:
        """fire-and-forget：任务回传后异步提炼，任何异常只记日志。"""
        asyncio.get_running_loop().create_task(self._distill(
            task_id=task_id, chat_id=chat_id, repo=repo, task_type=task_type,
            instruction=instruction, outcome=outcome, diff_stat=diff_stat,
        ))

    async def _distill(self, *, task_id: str, chat_id: str, repo: str, task_type: str,
                       instruction: str, outcome: str, diff_stat: str) -> None:
        try:
            await self._run(task_id=task_id, chat_id=chat_id, repo=repo,
                            task_type=task_type, instruction=instruction,
                            outcome=outcome, diff_stat=diff_stat)
        except Exception:  # noqa: BLE001 —— 提炼失败不影响任务交付
            log.exception("task distill failed task=%s", task_id)

    async def _run(self, *, task_id: str, chat_id: str, repo: str, task_type: str,
                   instruction: str, outcome: str, diff_stat: str) -> None:
        session = await self._memory.load_session(chat_id)
        if session.pending_memory is not None:
            if time.time() - session.pending_memory.created_at <= settings.memory_confirm_ttl_minutes * 60:
                log.info("task distill skipped: pending slot busy chat=%s task=%s", chat_id, task_id)
                return
            # 过期槽位：惰性清理（与确认流拦截器同规则）
            old_id = session.pending_memory.payload.get("memory_id", "")
            if old_id:
                await self._memory.delete_memory(old_id)
            session.pending_memory = None

        user_content = self._build_user_content(
            repo=repo, task_type=task_type, instruction=instruction,
            outcome=outcome, diff_stat=diff_stat,
        )
        model = settings.console_summary_model or None
        raw = await self._llm.complete(_TASK_DISTILL_SYSTEM, user_content, model=model)
        data = parse_task_distill_output(raw)
        if data is None:
            log.info("task distilled nothing: task=%s", task_id)
            return

        # 提议写入与消息管线/蒸馏同群锁互斥；持锁后复检槽位
        if self._chat_locks is not None:
            lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
            async with lock:
                await self._propose(chat_id=chat_id, task_id=task_id, data=data)
        else:
            await self._propose(chat_id=chat_id, task_id=task_id, data=data)

    async def _propose(self, *, chat_id: str, task_id: str, data: dict) -> None:
        """建草稿、挂槽位、发确认提示（持锁执行）。"""
        session = await self._memory.load_session(chat_id)
        if session.pending_memory is not None:
            created = session.pending_memory.created_at
            if time.time() - created <= settings.memory_confirm_ttl_minutes * 60:
                log.info("task distill propose skipped: slot busy chat=%s task=%s", chat_id, task_id)
                return
            old_id = session.pending_memory.payload.get("memory_id", "")
            if old_id:
                await self._memory.delete_memory(old_id)
            session.pending_memory = None

        draft = MemoryDraft(
            title=str(data.get("title") or "")[:30] or "任务经验",
            statement=str(data.get("statement") or ""),
            memory_type=str(data.get("memory_type") or "convention"),
            rationale=str(data.get("rationale") or ""),
            keywords=[str(k) for k in (data.get("keywords") or []) if k][:5],
            supersedes_id=str(data.get("supersedes_id") or ""),
            source_type=MEMORY_SOURCE_TASK,
            source_chat_id=chat_id,
            source_task_id=task_id,
        )
        memory_id = await self._memory.create_memory_draft(draft)
        session.pending_memory = PendingState(
            kind=_MEM_CONFIRM,
            payload={"memory_id": memory_id, "source": MEMORY_SOURCE_TASK},
        )
        await self._memory.save_session(session)
        log.info("task distill proposed: chat=%s task=%s memory=%s", chat_id, task_id, memory_id)
        if self._reply is not None:
            supersede_note = "（该结论将替代一条旧记忆）" if draft.supersedes_id else ""
            await self._reply(
                chat_id,
                f"我从任务 #{task_id[:8]}「{draft.title}」的执行中提炼出一条工程经验，供大家确认：\n"
                f"「{draft.statement}」\n"
                f"回复「确认」保存，「取消」放弃{supersede_note}"
                f"（{settings.memory_confirm_ttl_minutes} 分钟内有效）。",
            )

    @staticmethod
    def _build_user_content(*, repo: str, task_type: str, instruction: str,
                            outcome: str, diff_stat: str) -> str:
        kind = "代码变更" if task_type == "modify" else "排查"
        parts = [
            "【任务信息】",
            f"仓库：{repo}",
            f"任务类型：{kind}",
            f"需求：{instruction}",
        ]
        if outcome:
            parts += ["", "【执行产出】", outcome]
        if diff_stat:
            parts += ["", "【改动统计】", diff_stat]
        return "\n".join(parts)
