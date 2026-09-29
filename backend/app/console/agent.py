"""ConsoleAgent 主管线（ADR-003）：会话恢复→状态检查→确认流拦截→装配→路由→动作→持久化，群级串行锁。

ADR-010：新增 task_op 动作执行（status 查询 / steer / cancel），经注入的
TaskOpFn 下发到沙箱管线，本模块只做路由衔接。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Awaitable, Callable

from app.channels.base import UnifiedMessage
from app.config import settings
from app.console.context import ContextAssembler
from app.console.memory_confirm import MemoryConfirmHandler
from app.console.router import Router
from app.console.router_types import Action, PendingResolution
from app.memory.service import SQLiteMemoryService
from app.memory.types import ChatMessage, MemoryService, PendingState
log = logging.getLogger(__name__)

ReplyFn = Callable[[str, str, str], Awaitable[None]]    # (chat_id, text, session_webhook)
DelegateFn = Callable[[dict], Awaitable[str]]            # task_draft -> task_id
TaskOpFn = Callable[[str, str, str, str], Awaitable[str]]  # (chat_id, op, task_id, message) -> 回复文案


class ConsoleAgent:
    def __init__(
        self,
        memory: MemoryService,
        router: Router,
        reply: ReplyFn,
        delegate: DelegateFn | None = None,
        task_op: TaskOpFn | None = None,
    ) -> None:
        self._memory = memory
        self._router = router
        self._reply = reply
        self._delegate = delegate
        self._task_op = task_op
        self._assembler = ContextAssembler(memory)
        self._confirm = MemoryConfirmHandler(memory)
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, chat_id: str) -> asyncio.Lock:
        if chat_id not in self._locks:
            self._locks[chat_id] = asyncio.Lock()
        return self._locks[chat_id]

    # ---- 蒸馏服务协作面（ADR-009）----

    def chat_locks(self) -> dict[str, asyncio.Lock]:
        """per-chat 锁注册表：蒸馏写 Session 与消息管线共用同一把锁。"""
        return self._locks

    async def handle(self, msg: UnifiedMessage) -> None:
        lock = self._lock(msg.chat_id)
        if lock.locked():
            # 排队反馈：webhook 仅 5 分钟有效，先确认收到，正式回答走完整管线
            await self._reply(msg.chat_id, "收到，正在处理前面的消息，请稍候。", msg.session_webhook)
        async with lock:
            try:
                await self._pipeline(msg)
            except Exception:  # noqa: BLE001
                log.exception("console pipeline failed for chat %s", msg.chat_id)
                await self._reply(
                    msg.chat_id, "模型服务暂时不可用，请稍后再试。", msg.session_webhook
                )

    async def _pipeline(self, msg: UnifiedMessage) -> None:
        session = await self._memory.load_session(msg.chat_id)
        self._expire_pending(session)

        # 记忆确认流拦截（确定性，@ 门控；消费后不走路由器）
        if await self._confirm.try_handle(msg, session, self._reply):
            session.last_route = {"action": "memory_confirm", "ts": time.time()}
            await self._memory.save_session(session)
            return

        # 仓库绑定指令拦截（确定性，不走 LLM；ADR-003 群仓库绑定的运行时入口）
        repo_reply = await self._handle_repo_command(msg)
        if repo_reply is not None:
            await self._reply(msg.chat_id, repo_reply, msg.session_webhook)
            session.messages.append(ChatMessage(role="assistant", text=repo_reply))
            session.last_route = {"action": "repo_command", "ts": time.time()}
            await self._memory.save_session(session)
            return

        # 追加用户消息
        session.messages.append(
            ChatMessage(
                role="user", text=msg.text, user_id=msg.user_id,
                user_name=msg.user_name, message_id=msg.message_id,
            )
        )

        # 旁听消息注入装配器（ADR-008 段 2 合并时间线；窗口外部分留 L2 存而不检索）
        if isinstance(self._memory, SQLiteMemoryService):
            observed = await self._memory.load_observed(
                msg.chat_id,
                since=time.time() - settings.feishu_observe_window_hours * 3600,
                limit=settings.console_context_window,
            )
            observed = [m for m in observed if m.message_id != msg.message_id]
            self._assembler.prime_observed(msg.chat_id, observed)
        # 滚动压缩（Phase A）：溢出消息在路由前压进摘要
        from app.console.compaction import maybe_compact
        await maybe_compact(session, self._router.llm)

        context = await self._assembler.assemble(session, msg.text)
        decision = await self._router.route(context, msg.text)

        # 消费 pending 状态
        self._resolve_pending(session, decision.pending_resolution)

        # 动作执行
        reply_text = ""
        if decision.action == Action.ANSWER:
            reply_text = decision.answer
        elif decision.action == Action.CLARIFY:
            session.pending = PendingState(
                kind="clarify",
                payload={
                    "question": decision.clarify_question,
                    "asked_user_id": msg.user_id,       # 多人场景：澄清归属（并发修复 3）
                    "asked_user_name": msg.user_name,
                },
            )
            reply_text = decision.clarify_question
        elif decision.action == Action.DELEGATE:
            reply_text = await self._do_delegate(session, msg, decision)
        elif decision.action == Action.REPO_OP:
            reply_text = await self._do_repo_op(msg, decision)
        elif decision.action == Action.TASK_OP:
            reply_text = await self._do_task_op(msg, decision)

        # 纠正提炼（Phase A）：用户纠正 → 自动建草稿进入确认流（复用 Phase 0 状态机）
        if (
            decision.correction_detected
            and decision.action == Action.ANSWER
            and session.pending_memory is None          # 已有待确认时不叠加（T2 同规则）
        ):
            reply_text = await self._draft_correction(session, msg, decision, reply_text)

        if reply_text:
            await self._reply(msg.chat_id, reply_text, msg.session_webhook)
            session.messages.append(ChatMessage(role="assistant", text=reply_text))

        # 持久化
        session.last_route = {"action": decision.action.value, "ts": time.time()}
        await self._memory.save_session(session)

    @staticmethod
    def _resolve_pending(session, resolution: PendingResolution) -> None:
        if session.pending is None:
            return
        if resolution in (PendingResolution.ANSWERED, PendingResolution.NEW_QUESTION, PendingResolution.CANCEL):
            session.pending = None
        # NONE 时保留 pending（用户没正面回应，留待下一条）

    async def _draft_correction(self, session, msg: UnifiedMessage, decision, reply_text: str) -> str:
        """纠正提炼：从纠正消息建草稿，挂 pending_memory，回复末尾附确认提示。"""
        from app.memory.types import MemoryDraft

        fact = self._extract_correction_fact(decision.answer)
        if not fact:
            return reply_text
        draft = MemoryDraft(
            title=fact[:30],
            statement=fact,
            memory_type="decision",
            source_session_id=session.session_id,
        )
        memory_id = await self._memory.create_memory_draft(draft)
        session.pending_memory = PendingState(
            kind="memory_confirm", payload={"memory_id": memory_id}
        )
        log.info("correction drafted: chat=%s memory=%s", msg.chat_id, memory_id)
        return reply_text

    @staticmethod
    def _extract_correction_fact(answer: str) -> str:
        """从回答中取纠正后的事实：优先「（待确认）」标注句，否则取首段。"""
        for line in answer.splitlines():
            if "待确认" in line or "团队" in line:
                cleaned = line.strip().lstrip("-*1234567890.、 【】").strip()
                if cleaned:
                    return cleaned[:200]
        first = answer.strip().splitlines()[0] if answer.strip() else ""
        return first[:200]

    async def _do_delegate(self, session, msg: UnifiedMessage, decision) -> str:
        task = decision.task
        if task is None or not task.instruction:
            return "我需要创建任务，但任务信息不完整，能再说清楚要做什么吗？"
        if self._delegate is None:
            return "任务执行功能尚未就绪（SandboxAgent 未接入）。"

        # 仓库名解析：优先用模型给的 repo 名，在本群绑定的仓库里消歧（大小写不敏感）
        repo_name = (task.repo or "").strip()
        repo = await self._memory.resolve_repo(msg.chat_id, repo_name) if repo_name else None
        if not repo:
            bindings = await self._memory.list_repo_bindings(msg.chat_id)
            if len(bindings) == 1:
                repo = bindings[0]                    # 单绑定仓库：委派自然归属
            elif len(bindings) > 1:
                # 多绑定：模型未给名或给的名不匹配 → 澄清（不猜）
                opts = "、".join(r.name for r in bindings)
                return f"请说明要到哪个仓库执行？本群可用：{opts}。"
            # 零绑定：沿用模型给的 repo 名直通（沙箱侧 .env 兜底解析），保持旧行为
        draft = {
            "title": task.title or msg.text[:30],
            "instruction": task.instruction,
            "repo": repo.name if repo else repo_name,
            "type": task.type if task.type in ("investigate", "modify") else "investigate",
            "chat_id": msg.chat_id,
            "session_id": session.session_id,
            "created_by": msg.user_id,
        }
        task_id = await self._delegate(draft)
        session.active_task_ids.append(task_id)
        suffix = f"（仓库：{repo.name}）" if repo else ""
        return f"已受理，任务 #{task_id[:8]}：{draft['title']}{suffix}。完成后我会在群里同步；随时可以问我「任务进展」。"

    async def _do_task_op(self, msg: UnifiedMessage, decision) -> str:
        """任务查询与中途管控（ADR-010）：校验后经注入的 TaskOpFn 确定性执行。"""
        op = decision.task_op
        if op is None or not op.op:
            return "你是想查任务进展，还是要对任务下中途指示？可以说「#任务号 怎么样了」或「查看任务」。"
        if self._task_op is None:
            return "任务执行功能尚未就绪（SandboxAgent 未接入）。"
        task_id = (op.task_id or "").strip().lstrip("#")
        return await self._task_op(msg.chat_id, op.op.strip().lower(), task_id, (op.message or "").strip())

    # ---- 仓库管理意图执行（ADR-008 repo_op：LLM 识别，代码确定性执行）----

    async def _do_repo_op(self, msg: UnifiedMessage, decision) -> str:
        """LLM 识别出的仓库管理意图，参数校验后确定性执行，回复带撤销提示。"""
        op = decision.repo_op
        if op is None or not op.op:
            return "你是要对本群的仓库做什么操作？可以「绑定仓库 名字: git地址」或「查看仓库」。"
        name = (op.name or "").strip()
        if op.op == "list":
            repos = await self._memory.list_repo_bindings(msg.chat_id)
            if not repos:
                return "本群还没有绑定仓库。可以直接发「绑定 仓库名 git地址」或仓库地址给我。"
            lines = [f"- **{r.name}**：{r.url}" for r in repos]
            return "本群已绑定仓库：\n" + "\n".join(lines)
        if not name:
            return "我理解为要管理仓库，但缺少仓库名。请告诉我要操作哪个仓库？"
        if op.op == "bind":
            url = (op.url or "").strip()
            if not url:
                return f"要绑定仓库 [{name}]，还需要 git 地址（https://…）。"
            if not url.startswith("http"):
                return f"仓库地址 [{url}] 不像 git URL，请确认后再试（形如 https://github.com/org/repo.git）。"
            repo = await self._memory.bind_repo(
                msg.chat_id, name, url, test_cmd=op.test_cmd, bound_by=msg.user_id
            )
            log.info("repo bound via repo_op: chat=%s repo=%s by=%s", msg.chat_id, repo.name, msg.user_id)
            cmd_part = f"\n测试命令：{op.test_cmd}" if op.test_cmd else ""
            return (
                f"🔗 已绑定仓库 [{repo.name}] → {url}{cmd_part}\n"
                f"现在可以说「帮我到 {repo.name} 仓库排查/修改…」了。绑错了可回复「解绑 {repo.name}」撤销。"
            )
        if op.op == "unbind":
            ok = await self._memory.unbind_repo(msg.chat_id, name)
            if not ok:
                return f"本群没有绑定名为 [{name}] 的仓库。"
            log.info("repo unbound via repo_op: chat=%s repo=%s by=%s", msg.chat_id, name, msg.user_id)
            return f"已解绑仓库 [{name}]（仓库档案保留，可随时重新绑定）。"
        return f"还不支持「{op.op}」这个仓库操作，目前可以绑定、解绑、查看。"

    # ---- 仓库绑定指令（确定性解析，ADR-003 群仓库绑定的运行时入口）----
    _REPO_BIND_RE = re.compile(
        r"^绑定仓库\s+[:：]?\s*([^\s:：]+)\s*[:：]\s*(https?://\S+)(?:\s+测试命令\s*[:：]?\s*(.+))?$",
        re.IGNORECASE,
    )
    _REPO_UNBIND_RE = re.compile(r"^解绑仓库\s+[:：]?\s*(\S+)$")
    _REPO_LIST_RE = re.compile(r"^查看仓库$|^仓库列表$")

    async def _handle_repo_command(self, msg: UnifiedMessage) -> str | None:
        """命中绑定/解绑/查看指令返回回复文本；否则返回 None 走正常管线。"""
        text = msg.text.strip()

        m = self._REPO_BIND_RE.match(text)
        if m:
            name, url, test_cmd = m.group(1), m.group(2), (m.group(3) or "").strip()
            repo = await self._memory.bind_repo(
                msg.chat_id, name, url, test_cmd=test_cmd, bound_by=msg.user_id
            )
            log.info("repo bound: chat=%s repo=%s(%s) by=%s", msg.chat_id, repo.name, repo.repo_id, msg.user_id)
            cmd_part = f"\n测试命令：{test_cmd}" if test_cmd else ""
            return (
                f"🔗 已绑定仓库 [{repo.name}] → {url}{cmd_part}\n"
                f"现在可以说「帮我到 {repo.name} 仓库排查/修改…」了。"
            )

        m = self._REPO_UNBIND_RE.match(text)
        if m:
            name = m.group(1)
            ok = await self._memory.unbind_repo(msg.chat_id, name)
            if not ok:
                return f"本群没有绑定名为 [{name}] 的仓库。"
            log.info("repo unbound: chat=%s repo=%s by=%s", msg.chat_id, name, msg.user_id)
            return f"已解绑仓库 [{name}]（仓库档案保留，可随时重新绑定）。"

        if self._REPO_LIST_RE.match(text):
            repos = await self._memory.list_repo_bindings(msg.chat_id)
            if not repos:
                return "本群还没有绑定仓库。用「绑定仓库 名字: https://github.com/…」添加。"
            lines = [f"- **{r.name}**：{r.url}" for r in repos]
            return "本群已绑定仓库：\n" + "\n".join(lines)
        return None

    # 待处理状态超时被动清理（下一条消息到达时调用）
    @staticmethod
    def _expire_pending(session) -> None:
        ttl = settings.console_clarify_ttl_minutes * 60
        if session.pending and session.pending.kind == "clarify":
            if time.time() - session.pending.created_at > ttl:
                session.pending = None
        # pending_memory 的 TTL 由确认流拦截器在匹配时惰性检查
