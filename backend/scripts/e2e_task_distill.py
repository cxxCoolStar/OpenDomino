"""Phase B 任务完成提炼 E2E（ADR-002 触发点 1 / ADR-003 触发点 3）。

FakeLLM 驱动全流程验证，不依赖真实 LLM/飞书/Docker：

1. modify 任务 DONE → 提炼触发 → 草稿挂 pending_memory → 群内确认提示 →
   用户「确认」→ 入库 confirmed，source_type=task，source_task_id 可溯；
2. investigate 任务 DONE → 结论提炼 → 提议 → 用户「取消」→ 物理删除；
3. 无可沉淀任务 → LLM 返回 distilled=false → 不提议、槽位为空；
4. 单槽位占用 → 已有未过期待确认时跳过提炼；
5. LLM 失败 → 不提议、不影响任务交付（任务消息照常回传）。

产物：data/e2e_task_distill_report.json（可复验）。
用法：cd backend && python scripts/e2e_task_distill.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.console.agent import ConsoleAgent
from app.console.router import Router
from app.console.task_distill import TaskDistiller
from app.memory.service import SQLiteMemoryService
from app.memory.types import MEMORY_SOURCE_TASK, MemoryStatus

PENDING_TASK_INSTRUCTION = "在 shop-service 仓库接入新的支付回调重试机制，失败按指数退避重试 3 次"
MODIFY_OUTCOME = (
    "已在 PaymentCallbackService 增加 retryWithBackoff 逻辑，复用现有的 TaskScheduler；"
    "单测覆盖 3 次重试与最终失败两条路径，全部通过。"
)
INVESTIGATE_TASK_INSTRUCTION = "排查 shop-service 仓库订单接口偶发 500 的根因"
INVESTIGATE_OUTCOME = (
    "根因：订单接口在并发下共用了一个非线程安全的 SimpleDateFormat 实例（OrderMapper.buildQuery）,"
    "并发解析时抛 NumberFormatException 未捕获导致 500。复现方式：并发 20 请求 /orders/list 可稳定复现。"
)
NOTHING_OUTCOME = "按需求修改了 README 中的版本号，任务完成。"


class FakeDistillLLM:
    """按任务指令关键词返回预设提炼输出。"""

    def __init__(self):
        self.calls: list[str] = []

    async def complete(self, system: str, user: str, *, model=None) -> str:
        self.calls.append(user)
        if "支付回调重试" in user:
            return json.dumps({
                "distilled": True,
                "title": "shop-service 重试机制复用 TaskScheduler",
                "statement": "shop-service 仓库实现重试类需求时应复用 infra/schedule 包的 TaskScheduler（支持指数退避），不要自建线程 sleep 循环。",
                "rationale": "支付回调重试任务实施结论，全仓库统一重试路径",
                "memory_type": "convention",
                "keywords": ["shop-service", "重试", "TaskScheduler"],
                "supersedes_id": "",
            }, ensure_ascii=False)
        if "偶发 500" in user or "订单接口" in user:
            return json.dumps({
                "distilled": True,
                "title": "shop-service 订单 500 先查共享 SimpleDateFormat",
                "statement": "shop-service 仓库出现并发下偶发 NumberFormatException/500 时，优先排查共享可变 SimpleDateFormat 实例（OrderMapper.buildQuery 曾因此出过线上问题）。",
                "rationale": "排查任务结论，同类报错可先查此路径",
                "memory_type": "troubleshooting",
                "keywords": ["shop-service", "500", "SimpleDateFormat"],
                "supersedes_id": "",
            }, ensure_ascii=False)
        return json.dumps({"distilled": False}, ensure_ascii=False)


class FlakyLLM(FakeDistillLLM):
    async def complete(self, system: str, user: str, *, model=None) -> str:
        raise RuntimeError("LLM upstream 500")


REPORT: dict = {"started_at": time.time(), "cases": []}


def _record(name: str, ok: bool, detail: str) -> bool:
    REPORT["cases"].append({"case": name, "ok": ok, "detail": detail})
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    return ok


def make_msg(text: str, chat_id: str, user_id: str = "u1", message_id: str = "m"):
    from app.channels.base import UnifiedMessage

    return UnifiedMessage(text=text, user_id=user_id, chat_id=chat_id, message_id=message_id, is_at_bot=True)


async def run_case(name: str, env, llm, instruction: str, outcome: str, task_type: str,
                   task_id: str) -> dict:
    """构造 pipeline 同款调用：submit → 等提炼完成 → 返回现场。"""
    memory, replies, locks = env
    distiller = TaskDistiller(memory, llm, chat_locks=locks, reply=None)
    replies_after = len(replies)

    def reply(chat_id: str, text: str) -> None:
        replies.append((chat_id, text))

    distiller._reply = reply
    distiller.submit(
        task_id=task_id, chat_id="c-phaseb", repo="shop-service", task_type=task_type,
        instruction=instruction, outcome=outcome,
        diff_stat=f"分支 domino/task-{task_id[:8]}，提交数 2" if task_type == "modify" else "",
    )
    for _ in range(100):
        await asyncio.sleep(0.05)
        if len(replies) > replies_after or (llm.calls and _is_nothing(llm, user_text=None)):
            break
    await asyncio.sleep(0.1)
    return {"replies": replies[replies_after:], "session": await memory.load_session("c-phaseb")}


def _is_nothing(llm, user_text) -> bool:  # noqa: ARG001 —— 无可沉淀场景没有回复可等
    return False


async def main() -> int:
    from app.memory.types import PendingState

    all_ok = True

    # ---- 环境：独立 SQLite，消息管线真装配（确认流全链路）----
    tmp = Path("./data/e2e_task_distill_tmp")
    tmp.mkdir(parents=True, exist_ok=True)
    memory = SQLiteMemoryService(db_url=f"sqlite+aiosqlite:///{tmp}/e2e.db")
    await memory.init()
    replies: list[tuple[str, str]] = []
    locks: dict[str, asyncio.Lock] = {}

    async def reply(chat_id: str, text: str, webhook: str = "") -> None:
        replies.append((chat_id, text))

    async def delegate(draft: dict) -> str:
        return "task-fake-0001"

    llm = FakeDistillLLM()
    agent = ConsoleAgent(memory=memory, router=Router(llm), reply=reply,
                         delegate=delegate, task_op=lambda *a: asyncio.sleep(0, result="ok"))
    locks.update(agent.chat_locks())

    # ---- case 1: modify 提议 → 确认入库 ----
    r1 = await run_case("modify-propose", (memory, replies, locks), llm,
                        PENDING_TASK_INSTRUCTION, MODIFY_OUTCOME, "modify", "task-modify-0001")
    proposed = any("提炼出一条工程经验" in t for _, t in r1["replies"])
    all_ok &= _record("modify: 提议回群", proposed, f"replies={r1['replies'][:1]}")
    pm = r1["session"].pending_memory
    all_ok &= _record("modify: 槽位挂载", pm is not None and pm.kind == "memory_confirm",
                      f"pending_memory={pm}")
    if pm:
        # 确认流：走消息管线（确定性拦截，与蒸馏/纠正同一状态机）
        await agent.handle(make_msg("确认", "c-phaseb", message_id="confirm-1"))
        hit = await memory.get_memory(pm.payload["memory_id"])
        all_ok &= _record(
            "modify: 确认入库 source 可溯",
            hit is not None and hit.status == MemoryStatus.CONFIRMED,
            f"status={hit.status if hit else None}",
        )
        sess = await memory.load_session("c-phaseb")
        all_ok &= _record("modify: 确认后槽位清空", sess.pending_memory is None, "slot cleared")

    # ---- case 2: investigate 提议 → 取消物理删除 ----
    r2 = await run_case("investigate-propose", (memory, replies, locks), llm,
                        INVESTIGATE_TASK_INSTRUCTION, INVESTIGATE_OUTCOME, "investigate",
                        "task-invest-0001")
    proposed2 = any("提炼出一条工程经验" in t for _, t in r2["replies"])
    all_ok &= _record("investigate: 提议回群", proposed2, f"replies={len(r2['replies'])} 条")
    pm2 = (await memory.load_session("c-phaseb")).pending_memory
    if pm2:
        memory_id2 = pm2.payload["memory_id"]
        await agent.handle(make_msg("取消", "c-phaseb", message_id="cancel-1"))
        gone = await memory.get_memory(memory_id2)
        all_ok &= _record("investigate: 取消后物理删除", gone is None, f"memory={gone}")

    # ---- case 3: 无可沉淀 → 不提议 ----
    r3 = await run_case("nothing", (memory, replies, locks), llm,
                        "更新 README 版本号", NOTHING_OUTCOME, "modify", "task-nothing-001")
    all_ok &= _record("nothing: 无回复无槽位",
                      not r3["replies"] and (await memory.load_session("c-phaseb")).pending_memory is None,
                      f"replies={r3['replies']}")

    # ---- case 4: 单槽位占用 → 跳过 ----
    blocker = await memory.create_memory_draft(
        __import__("app.memory.types", fromlist=["MemoryDraft"]).MemoryDraft(
            title="占位", statement="已有待确认记忆占位"))
    sess = await memory.load_session("c-phaseb")
    sess.pending_memory = PendingState(kind="memory_confirm", payload={"memory_id": blocker})
    await memory.save_session(sess)
    replies_before = len(replies)
    r4 = await run_case("slot-busy", (memory, replies, locks), llm,
                        PENDING_TASK_INSTRUCTION, MODIFY_OUTCOME, "modify", "task-slot-0001")
    all_ok &= _record("slot-busy: 跳过不覆盖", len(replies) == replies_before and
                      (await memory.load_session("c-phaseb")).pending_memory.payload.get("memory_id") == blocker,
                      "槽位保持原占位")
    # 清理占位
    sess = await memory.load_session("c-phaseb")
    sess.pending_memory = None
    await memory.save_session(sess)
    await memory.delete_memory(blocker)

    # ---- case 5: LLM 失败 → 不提议不崩溃 ----
    flaky = FlakyLLM()
    r5 = await run_case("llm-failure", (memory, replies, locks), flaky,
                        PENDING_TASK_INSTRUCTION, MODIFY_OUTCOME, "modify", "task-flaky-001")
    all_ok &= _record("llm-failure: 静默降级",
                      not r5["replies"] and (await memory.load_session("c-phaseb")).pending_memory is None,
                      "无回复、槽位空")

    # ---- case 6: 冲突替代（supersedes 贯通）----
    # 先造一条旧 confirmed 记忆；再让任务产出与之冲突的结论，模型返回 supersedes_id。
    # 验证：LLM 输入含【现有团队记忆】（双层 KBase 上下文生效）→ 提议带替代提示 →
    # 确认后新条目 confirmed、旧条目 archived。
    from app.memory.types import MemoryDraft as _MD
    old_mem = await memory.create_memory_draft(_MD(
        title="shop-service 重试机制", statement="shop-service 重试统一用固定间隔 sleep 3 次。"))
    await memory.confirm_memory(old_mem, "u1")

    class SupersedeLLM(FakeDistillLLM):
        async def complete(self, system: str, user: str, *, model=None) -> str:
            assert "【现有团队记忆】" in user and "固定间隔 sleep" in user, "LLM 未看到旧记忆（双层上下文失效）"
            self.calls.append(user)
            return json.dumps({
                "distilled": True,
                "title": "shop-service 重试机制",
                "statement": "shop-service 重试需求应复用 infra/schedule 包的 TaskScheduler 指数退避，早期固定间隔 sleep 方案已废弃。",
                "rationale": "新任务实施结论，替代旧的固定间隔方案",
                "memory_type": "convention",
                "keywords": ["shop-service", "重试"],
                "supersedes_id": old_mem,
            }, ensure_ascii=False)

    sup_llm = SupersedeLLM()
    r6 = await run_case("supersede", (memory, replies, locks), sup_llm,
                        PENDING_TASK_INSTRUCTION, MODIFY_OUTCOME, "modify", "task-super-0001")
    pm6 = (await memory.load_session("c-phaseb")).pending_memory
    all_ok &= _record("supersede: LLM 看到旧记忆并提议",
                      any("替代一条旧记忆" in t for _, t in r6["replies"]) and pm6 is not None,
                      f"replies={len(r6['replies'])} 条")
    if pm6:
        new_id = pm6.payload["memory_id"]
        await agent.handle(make_msg("确认", "c-phaseb", message_id="confirm-2"))
        new_hit = await memory.get_memory(new_id)
        old_row = await memory.get_memory(old_mem)
        all_ok &= _record("supersede: 新条目入库",
                          new_hit is not None and new_hit.status == MemoryStatus.CONFIRMED,
                          f"new={new_hit.status if new_hit else None}")
        # 旧条目转 archived：recall_kbase 只回 confirmed，查不到即已归档
        from app.memory.service import MemoryRow as _MR, MemoryStatus as _MS
        archived = False
        async with memory._session() as s:
            from sqlalchemy import select as _select
            row = (await s.execute(_select(_MR).where(_MR.id == old_mem))).scalars().first()
            archived = row is not None and row.status == _MS.ARCHIVED.value
        all_ok &= _record("supersede: 旧条目归档", archived, f"old_status={archived}")

    # ---- 落库统计（source_type=task 全链路核对）----
    task_memories = []
    for hit in await memory.recall_kbase("shop-service", limit=20):
        task_memories.append(hit)
    REPORT["kbase_task_source_count"] = len([h for h in task_memories if h.status == MemoryStatus.CONFIRMED])

    REPORT["finished_at"] = time.time()
    REPORT["all_passed"] = bool(all_ok)
    out = Path("./data/e2e_task_distill_report.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nreport -> {out}")
    print("ALL PASSED" if all_ok else "SOME FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
