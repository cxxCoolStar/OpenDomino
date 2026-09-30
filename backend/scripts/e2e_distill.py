"""ADR-009 E2E 验证脚本：旁听结论蒸馏。

不依赖真实飞书与真实 LLM，用 FakeLLM 确定性输出验证链路行为：
  1. 首见初始化（存量消息不蒸）
  2. 空闲触发（超阈值 + 未蒸馏消息 → 蒸馏提议）
  3. 写前校验（蒸馏期间讨论恢复 → 作废，水位线不动）
  4. 三态水位线（确认推进 / 取消推进 / 过期不推进且草稿删除）
  5. supersedes（确认后旧条目 archived，新条目 confirmed）
  6. prune 协同（水位线之前的过期消息被清，之后的保留）
  7. 冷却（成功冷却 + 失败冷却生效，过期后恢复）
  8. 槽位占用（人工提议未决时蒸馏跳过）
  9. 单提议上限（LLM 返回多条只取第一条）
  10. 无可沉淀（无提议且水位线推进）

运行：cd backend && python scripts/e2e_distill.py
产出：scripts/e2e_distill_report.json（可重复验证 artifact）
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 独立临时库 + 蒸馏时序参数压缩（扫描间隔由测试直调 _scan_once 控制，不经 sleep）
_tmp_db = Path("./data/e2e_distill_tmp.db")
if _tmp_db.exists():
    _tmp_db.unlink()

os.environ["MEMORY_DB_URL"] = "sqlite+aiosqlite:///./data/e2e_distill_tmp.db"
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9")
os.environ.setdefault("LLM_API_KEY", "e2e-dummy")
os.environ.setdefault("CONSOLE_MODEL", "e2e-dummy")
os.environ.setdefault("SANDBOX_MODEL", "e2e-dummy")
os.environ.setdefault("FEISHU_APP_ID", "cli_e2e")
os.environ.setdefault("FEISHU_APP_SECRET", "e2e-secret")
os.environ["DISTILL_IDLE_MINUTES"] = "10"
os.environ["DISTILL_COOLDOWN_MINUTES"] = "360"
os.environ["DISTILL_FAILURE_RETRY_MINUTES"] = "30"
os.environ["DISTILL_SCAN_INTERVAL_SECONDS"] = "60"
os.environ["MEMORY_CONFIRM_TTL_MINUTES"] = "60"

from app.channels.base import UnifiedMessage                      # noqa: E402
from app.config import settings                                   # noqa: E402
from app.console.distiller import Distiller                       # noqa: E402
from app.console.memory_confirm import MemoryConfirmHandler       # noqa: E402
from app.memory.service import SQLiteMemoryService                # noqa: E402
from app.memory.types import ChatMessage, MemoryDraft, PendingState  # noqa: E402

PASS: list[dict] = []
FAIL: list[dict] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    entry = {"name": name, "ok": bool(cond), "detail": detail}
    (PASS if cond else FAIL).append(entry)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  | {detail}" if detail and not cond else ""))


IDLE = settings.distill_idle_minutes * 60          # 600s
COOLDOWN = settings.distill_cooldown_minutes * 60  # 21600s
FAIL_CD = settings.distill_failure_retry_minutes * 60  # 1800s


class FakeLLM:
    """按脚本序列返回蒸馏输出的假 LLM；记录调用次数。"""

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls = 0

    async def complete(self, system: str, user: str, *, model: str | None = None) -> str:
        self.calls += 1
        if self.outputs:
            out = self.outputs.pop(0)
        else:
            out = '{"distilled": false}'
        if out.startswith("@RAISE"):
            raise RuntimeError(out)
        return out


class Recorder:
    """记录锁使用情况与提议回复。"""

    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []
        self.lock_writes: list[str] = []


def obs_msgs(chat_id: str, n: int, start_ts: float, *, gap: float = 60.0) -> list[ChatMessage]:
    """构造 n 条旁听消息，时间戳递增，文本可蒸馏。"""
    out = []
    for i in range(n):
        out.append(ChatMessage(
            role="user",
            text=f"第{i}条：大家一致同意部署前必须跑 staging 环境验证，避免直接上生产。",
            user_id=f"u{i}", user_name=f"成员{i}",
            message_id=f"{chat_id}-m{start_ts:.0f}-{i}",
            timestamp=start_ts + i * gap,
        ))
    return out


async def settle(memory: SQLiteMemoryService, chat_id: str, msgs: list[ChatMessage],
                 *, age_seconds: float) -> float:
    """落一批旁听消息并把 received_at 回拨到足够老（模拟已空闲），返回该 received_at。

    水位线全程基于本地接收时间 received_at（ADR-009），与消息 timestamp 解耦：
    timestamp 只影响上下文渲染顺序，received_at 决定蒸馏归属。按 message_id 精确
    回拨，不影响同群其他批次。
    """
    added = await memory.save_observed(chat_id, msgs)
    assert added == len(msgs), f"save_observed added={added}"
    received = time.time() - age_seconds
    from sqlalchemy import update
    from app.memory.service import ObservedMessageRow
    async with memory._session() as s:
        await s.execute(
            update(ObservedMessageRow)
            .where(ObservedMessageRow.message_id.in_([m.message_id for m in msgs]))
            .values(received_at=received)
        )
        await s.commit()
    return received


def make_reply(rec: Recorder):
    async def reply(chat_id: str, text: str) -> None:
        rec.replies.append((chat_id, text))
    return reply


# ---------- 1. 首见初始化 + 2. 空闲触发 ----------

async def test_first_seen_and_idle_trigger(memory: SQLiteMemoryService) -> None:
    chat = "oc_first"
    # 存量消息（模拟回拉）：timestamp 是 8 天前（飞书服务器钟），received_at 保持
    # 当前时刻（刚回拉落库，本地钟）→ 首见锚点=最新 received_at=now，存量 timestamp
    # 远老于锚点，不计入未蒸馏
    old_msgs = obs_msgs(chat, 3, time.time() - 8 * 86400)
    await memory.save_observed(chat, old_msgs)

    # 首见：水位线锚点取表内最新接收时间（now）
    state = await memory.get_distill_state(chat)
    undistilled = await memory.count_undistilled(chat, state.watermark_ts)
    check("1. 首见初始化：存量消息不计入未蒸馏", undistilled == 0, f"undistilled={undistilled}")

    llm2 = FakeLLM(['{"distilled": true, "title": "部署约定", "statement": "部署前必须在 staging 环境完成验证后才允许上线生产。", '
                    '"rationale": "避免直接上生产造成事故", "memory_type": "convention", '
                    '"keywords": ["部署", "staging"], "supersedes_id": ""}'])
    rec = Recorder()
    d = Distiller(memory, llm2, chat_locks={}, reply=make_reply(rec))

    # 新消息到达后已空闲超阈值。首见锚点=now，新消息 timestamp 必须 > 锚点
    # 才计入未蒸馏——timestamp 取 now-60（刚发生），空闲用 received_at 回拨表达
    new_msgs = obs_msgs(chat, 2, time.time() - 60)
    await settle(memory, chat, new_msgs, age_seconds=IDLE + 60)

    await d._scan_once()
    state2 = await memory.get_distill_state(chat)
    check("2. 空闲触发：LLM 被调用且提议写入槽位", llm2.calls == 1 and len(rec.replies) == 1,
          f"calls={llm2.calls} replies={len(rec.replies)}")
    session = await memory.load_session(chat)
    check("2. 空闲触发：pending_memory 就绪且 source=overheard",
          session.pending_memory is not None
          and session.pending_memory.payload.get("source") == "overheard",
          f"pending={session.pending_memory}")
    # 水位线未推进（等确认/取消），目标值挂在 payload
    check("2. 空闲触发：水位线未提前推进", state2.watermark_ts == state.watermark_ts,
          f"wm={state2.watermark_ts} vs init={state.watermark_ts}")
    if rec.replies:
        check("2. 空闲触发：提议消息带确认提示", "确认" in rec.replies[0][1] and "取消" in rec.replies[0][1])


# ---------- 2z. 首见锚点回归锁（2026-09-30 事故根因 2） ----------

async def test_first_seen_anchor_ignores_zero_received(memory: SQLiteMemoryService) -> None:
    """received_at=0 的存量行不充当首见锚点。

    received_at 字段引入前的存量行 received_at=0；若锚点取到 0，水位线=0，
    全部历史（含存量）落入未蒸馏侧，蒸馏器每冷却周期重蒸重提（2026-09-30
    生产事故：同一结论被提议 3 次）。修复后锚点取 >0 的最大 received_at，
    全群无有效 received_at 时取当前时刻。
    """
    from sqlalchemy import update
    from app.memory.service import ObservedMessageRow

    async def zero_received(chat_id: str) -> None:
        async with memory._session() as s:
            await s.execute(
                update(ObservedMessageRow)
                .where(ObservedMessageRow.chat_id == chat_id)
                .values(received_at=0)
            )
            await s.commit()

    # 场景1：全群都是 received_at=0 的存量 → 锚点=当前时刻，存量不蒸、LLM 不被调用
    chat = "oc_legacy"
    legacy_ts = time.time() - 2 * 86400
    await memory.save_observed(chat, obs_msgs(chat, 3, legacy_ts))
    await zero_received(chat)
    state = await memory.get_distill_state(chat)
    undistilled = await memory.count_undistilled(chat, state.watermark_ts)
    check("2z. 首见锚点：received_at=0 存量不充当锚点（锚点>0 且存量不蒸）",
          state.watermark_ts > 0 and undistilled == 0,
          f"wm={state.watermark_ts} undistilled={undistilled}")

    llm = FakeLLM(['{"distilled": true, "statement": "不应被蒸出的结论。"}'])
    d = Distiller(memory, llm, chat_locks={}, reply=make_reply(Recorder()))
    await d._maybe_distill(chat)
    check("2z. 事故锁：存量群首扫不触发蒸馏调用", llm.calls == 0, f"calls={llm.calls}")

    # 场景2：混合——存量 received_at=0 + 回拉行 received_at=now → 锚点=最大有效
    # received_at；存量（timestamp 恒早）不落入未蒸馏侧
    chat2 = "oc_mixed"
    await memory.save_observed(chat2, obs_msgs(chat2, 3, legacy_ts))
    await zero_received(chat2)
    await memory.save_observed(chat2, [ChatMessage(
        role="user", text="刚回拉落库的消息", user_id="u",
        message_id=f"{chat2}-fresh", timestamp=time.time() - 30,
    )])
    state2 = await memory.get_distill_state(chat2)
    undistilled2 = await memory.count_undistilled(chat2, state2.watermark_ts)
    check("2z. 首见锚点：混合存量时取最大有效 received_at，存量不蒸",
          state2.watermark_ts > legacy_ts and undistilled2 == 0,
          f"wm={state2.watermark_ts} legacy_ts={legacy_ts} undistilled={undistilled2}")

    # 场景3：锚点确立后新消息正常蒸馏（修复不冻结管线）
    await settle(memory, chat, obs_msgs(chat, 2, time.time() + 10), age_seconds=IDLE + 60)
    state1b = await memory.get_distill_state(chat)
    state1b.last_success_at = 0.0
    await memory.save_distill_state(state1b)
    llm.outputs = ['{"distilled": false}']
    await d._maybe_distill(chat)
    state1c = await memory.get_distill_state(chat)
    check("2z. 不冻结：锚点确立后的新消息仍正常蒸馏且水位线推进",
          llm.calls == 1 and state1c.watermark_ts > state.watermark_ts,
          f"calls={llm.calls} wm={state1c.watermark_ts}")



async def test_three_states(memory: SQLiteMemoryService) -> None:
    handler = MemoryConfirmHandler(memory)

    def msg(text: str, chat_id: str = "oc_tri") -> UnifiedMessage:
        return UnifiedMessage(
            chat_id=chat_id,
            user_id="u_admin", user_name="管理员", message_id=f"msg-{time.time()}-{text}",
            text=text, is_at_bot=True,
        )

    async def setup_proposal(chat_id: str, wm_target: float) -> str:
        draft = MemoryDraft(
            title="测试结论", statement=f"{chat_id} 的测试性结论条目。",
            source_type="overheard", source_chat_id=chat_id,
        )
        mid = await memory.create_memory_draft(draft)
        session = await memory.load_session(chat_id)
        session.pending_memory = PendingState(
            kind="memory_confirm",
            payload={"memory_id": mid, "source": "overheard", "watermark_ts": wm_target},
        )
        await memory.save_session(session)
        return mid

    wm_target = time.time() - 100
    mid = await setup_proposal("oc_tri", wm_target)
    session = await memory.load_session("oc_tri")
    consumed = await handler.try_handle(msg("确认"), session, _noop_reply)
    state = await memory.get_distill_state("oc_tri")
    check("4a. 确认：消费消息、条目 confirmed、水位线推进",
          consumed and state.watermark_ts >= wm_target,
          f"consumed={consumed} wm={state.watermark_ts}")
    hit = await memory.get_memory(mid)
    check("4a. 确认：条目状态为 confirmed", hit is not None and hit.status.value == "confirmed")

    wm2 = time.time() + 50
    mid2 = await setup_proposal("oc_tri", wm2)
    session = await memory.load_session("oc_tri")
    consumed = await handler.try_handle(msg("取消"), session, _noop_reply)
    state = await memory.get_distill_state("oc_tri")
    hit2 = await memory.get_memory(mid2)
    check("4b. 取消：水位线推进且草稿物理删除",
          consumed and state.watermark_ts >= wm2 and hit2 is None,
          f"consumed={consumed} wm={state.watermark_ts} hit2={hit2}")

    # 4b 回归锁（2026-09-30 事故）：句尾 @ 粘连导致精确匹配失配——
    # 「取消@Domino」落到路由器被 LLM 代答「已取消」，草稿实际未删、水位线未动。
    # 修复后确认/取消匹配剥离句中所有 @token。
    wm_suffix = time.time() + 70
    mid_suffix = await setup_proposal("oc_tri", wm_suffix)
    session = await memory.load_session("oc_tri")
    consumed = await handler.try_handle(msg("取消@Domino"), session, _noop_reply)
    state = await memory.get_distill_state("oc_tri")
    hit_suffix = await memory.get_memory(mid_suffix)
    check("4b2. 取消：句尾@粘连（取消@Domino）同样命中并推进水位线",
          consumed and state.watermark_ts >= wm_suffix and hit_suffix is None,
          f"consumed={consumed} wm={state.watermark_ts} hit={hit_suffix}")

    wm_mid = time.time() + 80
    mid_mid = await setup_proposal("oc_tri", wm_mid)
    session = await memory.load_session("oc_tri")
    consumed = await handler.try_handle(msg("确认@Domino"), session, _noop_reply)
    hit_mid = await memory.get_memory(mid_mid)
    check("4b3. 确认：句尾@粘连（确认@Domino）同样命中入库",
          consumed and hit_mid is not None and hit_mid.status.value == "confirmed",
          f"consumed={consumed} hit={hit_mid}")

    # 非纯确认词（@ 剥离后仍不是关键词）不消费：防误伤正常讨论
    mid_keep = await setup_proposal("oc_tri", time.time() + 90)
    session = await memory.load_session("oc_tri")
    consumed = await handler.try_handle(msg("我确认一下细节@Domino"), session, _noop_reply)
    hit_keep = await memory.get_memory(mid_keep)
    check("4b4. 边界：@剥离后非精确关键词不消费（放行路由器）",
          (not consumed) and hit_keep is not None,
          f"consumed={consumed} hit={hit_keep}")
    await memory.delete_memory(mid_keep)

    # 过期：人工提议（无 watermark_ts）过期时水位线必然不动；草稿惰性删除
    mid3 = await setup_proposal("oc_tri", time.time() + 999)
    session = await memory.load_session("oc_tri")
    session.pending_memory.created_at = time.time() - settings.memory_confirm_ttl_minutes * 60 - 10
    await memory.save_session(session)
    session = await memory.load_session("oc_tri")
    consumed = await handler.try_handle(msg("随便聊聊"), session, _noop_reply)
    state = await memory.get_distill_state("oc_tri")
    hit3 = await memory.get_memory(mid3)
    check("4c. 过期：草稿物理删除、水位线不动",
          (not consumed) and hit3 is None and state.watermark_ts < time.time() + 999,
          f"consumed={consumed} hit3={hit3} wm={state.watermark_ts}")


async def _noop_reply(*args, **kwargs) -> None:
    pass


# ---------- 5. supersedes ----------

async def test_supersedes(memory: SQLiteMemoryService) -> None:
    chat = "oc_super"
    old = MemoryDraft(title="旧约定", statement="部署可以直接上生产，无需验证。", memory_type="convention")
    old_id = await memory.create_memory_draft(old)
    await memory.confirm_memory(old_id, "u_admin")

    # 确认前：召回命中旧条目
    hits = await memory.recall_kbase("部署 生产 验证", limit=10)
    check("5. supersedes 前旧条目可召回", any(h.id == old_id for h in hits))

    new_draft = MemoryDraft(
        title="新约定", statement="部署前必须在 staging 环境完成验证后才允许上线生产。",
        supersedes_id=old_id, source_type="overheard", source_chat_id=chat,
    )
    new_id = await memory.create_memory_draft(new_draft)
    await memory.confirm_memory(new_id, "u_admin")

    old_hit = await memory.get_memory(old_id)
    new_hit = await memory.get_memory(new_id)
    check("5. supersedes 后：旧条目 archived、新条目 confirmed",
          old_hit is not None and old_hit.status.value == "archived"
          and new_hit is not None and new_hit.status.value == "confirmed",
          f"old={old_hit.status if old_hit else None} new={new_hit.status if new_hit else None}")
    hits2 = await memory.recall_kbase("部署 生产 验证", limit=10)
    check("5. supersedes 后召回不再命中旧条目", all(h.id != old_id for h in hits2))

    # 旧条目已被归档后，第二条替代草稿确认：替代关系被丢弃，按普通新条目处理
    newer = MemoryDraft(title="更新约定", statement="部署前必须在 staging 与预发环境双重验证。",
                        supersedes_id=old_id)
    newer_id = await memory.create_memory_draft(newer)
    await memory.confirm_memory(newer_id, "u_admin")
    newer_hit = await memory.get_memory(newer_id)
    check("5. 替代已归档条目：按普通新条目确认，无异常",
          newer_hit is not None and newer_hit.status.value == "confirmed")


# ---------- 6. prune 协同 ----------

async def test_prune_watermark(memory: SQLiteMemoryService) -> None:
    """prune 与水位线协同。水位线基于 received_at：构造三条消息的本地接收时间——

    oc_prune：老消息 received_at=40 天前且已在水位线之前 → 可清；新消息保留。
    oc_prune_stuck：超龄消息 received_at=40 天前但水位线卡在它之前 → 必须保留。
    oc_prune_nostate：群无水位线记录 → 整体不清理。
    """
    from sqlalchemy import update
    from app.memory.service import ObservedMessageRow

    async def backdate_received(chat_id: str, message_id: str, received: float) -> None:
        async with memory._session() as s:
            await s.execute(
                update(ObservedMessageRow)
                .where(ObservedMessageRow.message_id == message_id)
                .values(received_at=received)
            )
            await s.commit()

    chat = "oc_prune"
    old_received = time.time() - 40 * 86400   # 40 天前收到（超 30 天保留期）
    wm = old_received + 100                    # 水位线已越过老消息（已蒸馏）
    await memory.get_distill_state(chat, init_watermark=wm)

    await memory.save_observed(chat, [
        ChatMessage(role="user", text="老消息已蒸", user_id="u", message_id=f"{chat}-old",
                    timestamp=old_received),
        ChatMessage(role="user", text="新消息未蒸", user_id="u", message_id=f"{chat}-new",
                    timestamp=time.time() - 3600),
    ])
    await backdate_received(chat, f"{chat}-old", old_received)

    deleted = await memory.prune_observed(retain_days=30, respect_watermark=True)
    remaining = await memory.load_observed(chat, limit=10)
    texts = [m.text for m in remaining]
    check("6. prune：水位线之前的过期消息被清、之后的保留",
          deleted == 1 and "老消息已蒸" not in texts and "新消息未蒸" in texts,
          f"deleted={deleted} remaining={texts}")

    # 水位线卡住（超龄消息未被蒸馏）→ 不清，不静默丢数据
    chat2 = "oc_prune_stuck"
    stuck_received = time.time() - 40 * 86400
    await memory.get_distill_state(chat2, init_watermark=stuck_received - 86400)  # 水位线更老
    await memory.save_observed(chat2, [
        ChatMessage(role="user", text="卡住未蒸的超龄消息", user_id="u", message_id=f"{chat2}-old",
                    timestamp=stuck_received),
    ])
    await backdate_received(chat2, f"{chat2}-old", stuck_received)
    deleted2 = await memory.prune_observed(retain_days=30, respect_watermark=True)
    remaining2 = await memory.load_observed(chat2, limit=10)
    check("6. prune：水位线之后的超龄消息保留（不静默丢数据）",
          deleted2 == 0 and len(remaining2) == 1,
          f"deleted={deleted2} remaining={len(remaining2)}")

    # 无水位线记录的群：整体不清理
    chat3 = "oc_prune_nostate"
    await memory.save_observed(chat3, [
        ChatMessage(role="user", text="无水位线群的超龄消息", user_id="u", message_id=f"{chat3}-old",
                    timestamp=stuck_received),
    ])
    await backdate_received(chat3, f"{chat3}-old", stuck_received)
    deleted3 = await memory.prune_observed(retain_days=30, respect_watermark=True)
    remaining3 = await memory.load_observed(chat3, limit=10)
    check("6. prune：无水位线记录的群不清理", deleted3 == 0 and len(remaining3) == 1,
          f"deleted={deleted3} remaining={len(remaining3)}")

    # 回拉存量（P1 回归锁）：timestamp 是 40 天前（消息龄超保留期）、received_at 是
    # 现在（刚回拉落库）。水位线推到两条之间：较新的那条已在水位线之前（已蒸馏）
    # → 按消息龄应清；较老的那条在水位线之后（蒸馏范围最老优先，它还没被蒸到）
    # → 必须保留，不静默丢回拉数据。
    chat4 = "oc_prune_backfill"
    bf_ts = time.time() - 40 * 86400
    await memory.get_distill_state(chat4, init_watermark=bf_ts + 30)  # 水位线在两条之间
    await memory.save_observed(chat4, [
        ChatMessage(role="user", text="回拉存量已蒸", user_id="u", message_id=f"{chat4}-bf-covered",
                    timestamp=bf_ts),
        ChatMessage(role="user", text="回拉存量未蒸", user_id="u", message_id=f"{chat4}-bf-uncovered",
                    timestamp=bf_ts + 60),
    ])
    # received_at 保持 save_observed 写入的当前时刻（模拟刚回拉回来）
    deleted4 = await memory.prune_observed(retain_days=30, respect_watermark=True)
    remaining4 = {m.message_id for m in await memory.load_observed(chat4, limit=10)}
    check("6. prune：回拉存量按消息龄判——已蒸的清、未蒸的保留",
          deleted4 == 1
          and f"{chat4}-bf-covered" not in remaining4
          and f"{chat4}-bf-uncovered" in remaining4,
          f"deleted={deleted4} remaining={sorted(remaining4)}")


# ---------- 7. 冷却 + 3. 写前校验 + 8. 槽位 + 9. 多条 + 10. 无可沉淀 ----------

async def test_cooldown_and_guards(memory: SQLiteMemoryService) -> None:
    chat = "oc_guard"

    # 首见 + 落一批已空闲消息；水位线显式设为「消息到达之前」的时刻
    init_wm = time.time() - IDLE - 600
    await memory.get_distill_state(chat, init_watermark=init_wm)
    received = await settle(memory, chat, obs_msgs(chat, 3, time.time() - IDLE - 120),
                            age_seconds=IDLE + 60)

    outputs = [
        # 第 1 次：成功提议
        '{"distilled": true, "title": "约定A", "statement": "约定A的内容：发版前必须跑 staging。", '
        '"memory_type": "convention", "keywords": [], "supersedes_id": ""}',
        # 第 2 次（冷却内，不应被调用）
        '{"distilled": false}',
        # 第 3 次（冷却过期后）：无可沉淀 → 水位线推进
        '{"distilled": false}',
        # 第 4 次：LLM 异常 → 失败冷却，水位线不动
        "@RAISE llm down",
        # 第 5 次（失败冷却内，不应被调用）
        '{"distilled": false}',
    ]
    llm = FakeLLM(outputs)
    rec = Recorder()
    d = Distiller(memory, llm, chat_locks={}, reply=make_reply(rec))

    # 第 1 次：成功提议，水位线不动（直调 _maybe_distill，避免扫描其他测试群）
    await d._maybe_distill(chat)
    state = await memory.get_distill_state(chat)
    check("7. 冷却：成功提议后进入成功冷却（不再调用）", llm.calls == 1, f"calls={llm.calls}")
    check("7. 冷却：成功提议后水位线未推进", state.watermark_ts < received + 1,
          f"wm={state.watermark_ts}")

    # 8. 槽位占用：手工把槽位塞一个未过期人工提议，再触发——LLM 不应被调用
    session = await memory.load_session(chat)
    manual_mid = await memory.create_memory_draft(
        MemoryDraft(title="人工", statement="人工沉淀的条目。", source_type="manual")
    )
    session.pending_memory = PendingState(kind="memory_confirm", payload={"memory_id": manual_mid})
    await memory.save_session(session)
    # 强行把成功冷却拨回去（模拟冷却过期），槽位占用应阻止触发
    state.last_success_at = time.time() - COOLDOWN - 10
    await memory.save_distill_state(state)
    await d._maybe_distill(chat)
    check("8. 槽位占用：人工提议未决时蒸馏跳过", llm.calls == 1, f"calls={llm.calls}")

    # 处置人工提议（取消），水位线不含 watermark_ts 不受影响
    session = await memory.load_session(chat)
    await memory.delete_memory(manual_mid)
    session.pending_memory = None
    await memory.save_session(session)

    # 7 续：冷却过期后恢复触发 → 这次返回「无可沉淀」→ 水位线推进
    state = await memory.get_distill_state(chat)
    state.last_success_at = time.time() - COOLDOWN - 10
    await memory.save_distill_state(state)
    await d._maybe_distill(chat)
    state = await memory.get_distill_state(chat)
    msgs_now = await memory.count_undistilled(chat, state.watermark_ts)
    check("10. 无可沉淀：无提议且水位线推进（消息全清）",
          llm.calls == 2 and msgs_now == 0 and len(rec.replies) == 1,
          f"calls={llm.calls} undistilled={msgs_now} replies={len(rec.replies)}")

    # 9. 单提议上限：LLM 返回多条候选也只保存一条草稿
    # 新群：水位线显式设早、消息已空闲
    chat_b = "oc_multi"
    await memory.get_distill_state(chat_b, init_watermark=time.time() - IDLE - 600)
    await settle(memory, chat_b, obs_msgs(chat_b, 2, time.time() - IDLE - 120), age_seconds=IDLE + 60)
    llm.outputs[0] = (
        '{"distilled": true, "title": "结论一", "statement": "第一条：测试环境必须与生产同构。", '
        '"memory_type": "convention", "keywords": [], "supersedes_id": ""}\n'
        '另外还有 {"distilled": true, "title": "结论二", "statement": "第二条：发布窗口只允许周二周四。"}'
    )
    await d._maybe_distill(chat_b)
    session_b = await memory.load_session(chat_b)
    check("9. 单提议上限：多条候选只产出一条提议",
          session_b.pending_memory is not None
          and session_b.pending_memory.payload.get("memory_id") is not None,
          f"pending={session_b.pending_memory}")

    # 3. 写前校验：蒸馏进行中有新消息到达（received_at 变化）→ 作废，水位线不动
    chat_c = "oc_writecheck"
    await memory.get_distill_state(chat_c, init_watermark=time.time() - IDLE - 600)
    received_c = await settle(memory, chat_c, obs_msgs(chat_c, 2, time.time() - IDLE - 120),
                              age_seconds=IDLE + 60)
    llm2 = FakeLLM(['{"distilled": true, "title": "T", "statement": "应被作废的结论。"}'])
    d2 = Distiller(memory, llm2, chat_locks={}, reply=make_reply(Recorder()))

    # 在蒸馏流程中途插入新旁听消息：包装 load_undistilled 触发后追加
    orig_load = d2._memory.load_undistilled

    async def load_and_interfere(chat_id: str, watermark_ts: float, *, limit: int = 200):
        result = await orig_load(chat_id, watermark_ts, limit=limit)
        if chat_id == chat_c:
            # 模拟蒸馏进行中讨论恢复（received_at 前移）
            await memory.save_observed(chat_c, [
                ChatMessage(role="user", text="插队的新消息", user_id="u9", user_name="成员9",
                            message_id=f"{chat_c}-interrupt", timestamp=time.time()),
            ])
        return result

    d2._memory.load_undistilled = load_and_interfere
    await d2._maybe_distill(chat_c)
    state_c = await memory.get_distill_state(chat_c)
    session_c = await memory.load_session(chat_c)
    last_c = await memory.last_observed_received_at(chat_c)
    check("3. 写前校验：讨论恢复后结果作废、水位线不动、无提议",
          llm2.calls == 1 and state_c.watermark_ts < received_c + 1
          and session_c.pending_memory is None and last_c != received_c,
          f"calls={llm2.calls} wm={state_c.watermark_ts} pending={session_c.pending_memory}")

    # 7 续：失败冷却——LLM 异常后 30 分钟内不重试
    chat_d = "oc_fail"
    await memory.get_distill_state(chat_d, init_watermark=time.time() - IDLE - 600)
    await settle(memory, chat_d, obs_msgs(chat_d, 2, time.time() - IDLE - 120), age_seconds=IDLE + 60)
    llm3 = FakeLLM(["@RAISE boom", '{"distilled": false}'])
    d3 = Distiller(memory, llm3, chat_locks={}, reply=make_reply(Recorder()))
    await d3._maybe_distill(chat_d)  # LLM 异常在 _distill 内部消化，进失败冷却
    state_d = await memory.get_distill_state(chat_d)
    check("7. 失败冷却：失败后水位线不动", state_d.watermark_ts < time.time() - IDLE - 120,
          f"wm={state_d.watermark_ts}")
    await d3._maybe_distill(chat_d)  # 失败冷却内：不应再调用
    check("7. 失败冷却：30 分钟内不重试", llm3.calls == 1, f"calls={llm3.calls}")
    # 拨过失败冷却 → 恢复
    state_d.last_failure_at = time.time() - FAIL_CD - 10
    await memory.save_distill_state(state_d)
    await d3._maybe_distill(chat_d)
    state_d2 = await memory.get_distill_state(chat_d)
    check("7. 失败冷却：冷却过期后恢复蒸馏",
          llm3.calls == 2 and state_d2.watermark_ts > time.time() - IDLE - 600,
          f"calls={llm3.calls} wm={state_d2.watermark_ts}")


async def main() -> None:
    memory = SQLiteMemoryService()
    await memory.init()

    await test_first_seen_and_idle_trigger(memory)
    await test_first_seen_anchor_ignores_zero_received(memory)
    await test_three_states(memory)
    await test_supersedes(memory)
    await test_prune_watermark(memory)
    await test_cooldown_and_guards(memory)

    report = {
        "adr": "ADR-009 旁听结论蒸馏",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "passed": len(PASS),
        "failed": len(FAIL),
        "cases": PASS + FAIL,
    }
    out = Path("./scripts/e2e_distill_report.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed → {out} ===")
    if FAIL:
        for f in FAIL:
            print(f"  FAIL: {f['name']} | {f['detail']}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
