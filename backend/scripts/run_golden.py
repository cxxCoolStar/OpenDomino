"""黄金测试集 Runner（路由族）：加载 YAML case → 逐 case 环境准备 → 进程内跑 ConsoleAgent → 评分 → 报告。

设计对齐 open-agent eval（docs/eval/EVALUATION_PLAN.md 取舍后的形态）：
- 隔离三原则：每 case 独立 tmp 目录（SQLite + Chroma + dump），独立 chat_id，
  fixtures 每 case 重建，case 间零污染；运行开头清理上一次残留目录（Windows 下
  SQLite/Chroma 句柄由 aclose + 进程退出释放，目录不即时删）。
- 断言主打结构化 Decision（action / task / pending_resolution / repo_op / task_op /
  correction_detected），经 RecordingRouter 截获；回复文本只做事实锚点辅助断言。
- 真实 LLM：直接构造 OpenAIClient，不 mock；LLM 未配置时整体退出。
- 报告：data/golden_report.json，含每 case 逐轮得分、扣分项、原始决策。

用法：
    cd backend && python scripts/run_golden.py [--cases id1,id2] [--list]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.channels.base import UnifiedMessage  # noqa: E402
from app.config import settings  # noqa: E402
from app.console.agent import ConsoleAgent  # noqa: E402
from app.console.llm import OpenAIClient  # noqa: E402
from app.console.router import Router  # noqa: E402
from app.console.router_types import Decision  # noqa: E402
from app.memory.service import SQLiteMemoryService  # noqa: E402
from app.memory.types import (  # noqa: E402
    ChatMessage,
    MemoryDraft,
    PendingState,
)

CASES_DIR = BACKEND / "eval" / "cases"
DATA_DIR = BACKEND / "data"
TMP_ROOT = DATA_DIR / "golden_tmp"
REPORT_PATH = DATA_DIR / "golden_report.json"
VALID_ACTIONS = {"answer", "clarify", "delegate", "repo_op", "task_op"}

# 单条 turn 整体硬超时：死循环/上游挂起不拖垮整个 suite（内部另有 60s LLM 超时）
TURN_TIMEOUT_SECONDS = 120
PASS_THRESHOLD = 80


# ---- 数据结构 ----

@dataclass
class TurnResult:
    index: int
    action: str = ""
    score: int = 0
    deductions: list[dict] = field(default_factory=list)
    reply_excerpt: str = ""
    decision_raw: dict = field(default_factory=dict)
    error: str = ""


@dataclass
class CaseResult:
    case_id: str
    name: str
    category: str
    priority: str
    status: str = ""       # passed | failed | skipped | error
    score: int = 0
    turns: list[TurnResult] = field(default_factory=list)
    error: str = ""
    duration_s: float = 0.0


class RecordingRouter(Router):
    """截获每轮 Decision：黄金断言打在结构化决策上，而不是回复文本。"""

    def __init__(self, llm) -> None:
        super().__init__(llm)
        self.decisions: list[Decision] = []

    async def route(self, context: str, user_text: str) -> Decision:
        d = await super().route(context, user_text)
        self.decisions.append(d)
        return d


def load_cases(cases_dir: Path) -> list[dict]:
    """加载目录下全部 YAML case，按文件名排序；无 id 的文件跳过。"""
    cases = []
    for p in sorted(cases_dir.glob("*.yaml")):
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or "id" not in raw:
            continue
        cases.append(raw)
    return cases


# ---- 评分（扣分制：起点 100，结果性错误归零，锚点缺失小扣）----

def _deduct(deductions: list[dict], points: int, reason: str) -> None:
    deductions.append({"points": points, "reason": reason})


def grade_turn(expected: dict, outcome: dict) -> TurnResult:
    deductions: list[dict] = []
    action = outcome["action"]

    exp_action = (expected.get("action") or "").strip()
    if exp_action and exp_action not in VALID_ACTIONS:
        raise ValueError(f"case 的 expected.action 非法: {exp_action!r}（合法：{sorted(VALID_ACTIONS)}）")

    # ---- 结果性错误：直接归零 ----
    exp_actions = expected.get("action_in") or ([expected.get("action")] if expected.get("action") else [])
    for a in exp_actions:
        if a not in VALID_ACTIONS:
            raise ValueError(f"case 的 expected.action 非法: {a!r}（合法：{sorted(VALID_ACTIONS)}）")
    if exp_actions and action not in exp_actions:
        _deduct(deductions, 100, f"action 期望 {'/'.join(exp_actions)}，实际 {action}")
    if not deductions:
        if expected.get("delegate_called") and not outcome["delegate_called"]:
            _deduct(deductions, 100, "期望触发 delegate，实际未触发")
        elif "delegate_called" in expected and not expected["delegate_called"] and outcome["delegate_called"]:
            _deduct(deductions, 100, "期望不触发 delegate，实际触发了")
        if expected.get("delegate_called") and not (outcome["delegate_draft"].get("instruction") or "").strip():
            _deduct(deductions, 100, "delegate 草稿缺 instruction")
        exp_task = expected.get("task") or {}
        if "delegate" in exp_actions and exp_task:
            task = outcome["task"]
            exp_type = (exp_task.get("type") or "").strip()
            if exp_type and task.get("type") != exp_type:
                _deduct(deductions, 100, f"task.type 期望 {exp_type}，实际 {task.get('type')}")
            exp_repo = (exp_task.get("repo") or "").strip()
            if exp_repo:
                got = (task.get("repo") or "").strip()
                if exp_repo.lower() != got.lower():
                    _deduct(deductions, 100, f"task.repo 期望 {exp_repo}，实际 {got!r}")
        exp_instr = expected.get("instruction_must_contain") or []
        if exp_instr and not outcome["delegate_called"]:
            _deduct(deductions, 100, "instruction 断言依赖 delegate 触发，但本轮未触发")
        else:
            for anchor in exp_instr:
                if anchor not in (outcome["delegate_draft"].get("instruction") or ""):
                    _deduct(deductions, 20, f"委派 instruction 缺锚点 {anchor!r}（指代未消解或指令不自包含）")
        exp_pr = (expected.get("pending_resolution") or "").strip()
        if exp_pr and outcome["pending_resolution"] != exp_pr:
            _deduct(deductions, 100, f"pending_resolution 期望 {exp_pr}，实际 {outcome['pending_resolution']}")
        exp_corr = expected.get("correction_detected")
        if exp_corr is not None and outcome["correction_detected"] != bool(exp_corr):
            _deduct(deductions, 100, f"correction_detected 期望 {exp_corr}，实际 {outcome['correction_detected']}")
    exp_repo_op = expected.get("repo_op") or {}
    if "repo_op" in exp_actions and exp_repo_op:
        op = outcome["repo_op"]
        exp_op = (exp_repo_op.get("op") or "").strip()
        if exp_op and op.get("op") != exp_op:
            _deduct(deductions, 100, f"repo_op.op 期望 {exp_op}，实际 {op.get('op')}")

    # ---- 过程性扣分：每项 -20（字段级锚点）----
    exp_task_op = expected.get("task_op") or {}
    if "task_op" in exp_actions and exp_task_op:
        op = outcome["task_op"]
        exp_tid = (exp_task_op.get("task_id") or "").strip()
        if exp_tid and exp_tid not in (op.get("task_id") or ""):
            _deduct(deductions, 20, f"task_op.task_id 未含 {exp_tid}，实际 {op.get('task_id')!r}")
        exp_msg = (exp_task_op.get("message") or "").strip()
        if exp_msg and exp_msg not in (op.get("message") or ""):
            _deduct(deductions, 20, f"task_op.message 未含 {exp_msg!r}，实际 {op.get('message')!r}")
    exp_repo_op = expected.get("repo_op") or {}
    if exp_action == "repo_op" and exp_repo_op:
        op = outcome["repo_op"]
        for k in ("name", "url"):
            exp_v = (exp_repo_op.get(k) or "").strip()
            if exp_v and exp_v not in (op.get(k) or ""):
                _deduct(deductions, 20, f"repo_op.{k} 未含 {exp_v!r}，实际 {op.get(k)!r}")
    for anchor in expected.get("reply_must_contain") or []:
        if anchor not in outcome["reply"]:
            _deduct(deductions, 20, f"回复缺事实锚点 {anchor!r}")

    score = max(0, 100 - sum(d["points"] for d in deductions))
    return TurnResult(
        index=outcome["index"], action=action, score=score, deductions=deductions,
        reply_excerpt=outcome["reply"][:200], decision_raw=outcome["decision_raw"],
    )


# ---- 单 case 执行：环境准备 → 逐轮跑 → 打扫 ----

async def run_case(case: dict, case_dir: Path) -> CaseResult:
    result = CaseResult(
        case_id=case["id"], name=case.get("name", ""), category=case.get("category", ""),
        priority=case.get("priority", "P1"),
    )
    t0 = time.time()

    if case.get("requires_embedding") and not settings.embedding_model:
        result.status = "skipped"
        result.error = "case 要求 embedding，但 EMBEDDING_MODEL 未配置"
        return result

    # 环境准备：SQLite / Chroma / dump 全部指进独立 case 目录
    from app.memory.embedding import NullEmbeddingClient

    db_url = f"sqlite+aiosqlite:///{(case_dir / 'mem.db').as_posix()}"
    saved = (settings.memory_db_url, settings.chroma_persist_dir, settings.console_llm_dump_dir)
    settings.memory_db_url = db_url
    settings.chroma_persist_dir = str(case_dir / "chroma")
    settings.console_llm_dump_dir = str(case_dir / "dump")
    mem: SQLiteMemoryService | None = None
    try:
        embed = None if settings.embedding_model else NullEmbeddingClient()
        mem = SQLiteMemoryService(db_url=db_url, embedding=embed)
        await mem.init()

        fx = case.get("fixtures") or {}
        chat_id = f"golden-{re.sub(r'[^a-z0-9-]', '-', case['id'].lower())[:40]}"
        for m in fx.get("session_messages") or []:
            session = await mem.load_session(chat_id)
            session.messages.append(ChatMessage(
                role=m.get("role", "user"), text=m["text"],
                user_id=m.get("user_id", "u1"), user_name=m.get("user_name", "张三"),
                message_id=f"fx-{len(session.messages)}",
            ))
            await mem.save_session(session)
        if fx.get("active_task_ids"):
            session = await mem.load_session(chat_id)
            session.active_task_ids = [str(t) for t in fx["active_task_ids"]]
            await mem.save_session(session)
        pc = fx.get("pending_clarify")
        if pc:
            session = await mem.load_session(chat_id)
            session.pending = PendingState(
                kind="clarify",
                payload={"question": pc.get("question", ""), "asked_user_id": "u1", "asked_user_name": "张三"},
                created_at=time.time() - float(pc.get("age_minutes", 0)) * 60,
            )
            await mem.save_session(session)
        for b in fx.get("repo_bindings") or []:
            await mem.bind_repo(chat_id, b["name"], b["url"], test_cmd=b.get("test_cmd"), bound_by="fx")
        for memo in fx.get("memories") or []:
            mid = await mem.create_memory_draft(MemoryDraft(
                title=memo.get("title", memo["statement"][:30]),
                statement=memo["statement"],
                memory_type=memo.get("memory_type", "decision"),
                keywords=[str(k) for k in memo.get("keywords") or []],
                source_session_id="golden-fixture",
            ))
            await mem.confirm_memory(mid, "fx")
        observed = fx.get("observed_messages") or []
        if observed:
            now = time.time()
            await mem.save_observed(chat_id, [ChatMessage(
                role=m.get("role", "user"), text=m["text"],
                user_id=m.get("user_id", "u2"), user_name=m.get("user_name", "李四"),
                message_id=f"obs-{i}", timestamp=now - float(m.get("age_minutes", 5)) * 60,
            ) for i, m in enumerate(observed)])

        replies: list[str] = []
        delegated: list[dict] = []

        async def reply(chat_id_: str, text: str, webhook: str = "") -> None:
            replies.append(text)

        async def delegate(draft: dict) -> str:
            delegated.append(draft)
            return f"gt{len(delegated):08x}"

        async def task_op_fn(chat_id_: str, op: str, task_id: str, message: str) -> str:
            return f"[{op}] 已执行（测试桩）"

        agent = ConsoleAgent(
            memory=mem, router=RecordingRouter(OpenAIClient()), reply=reply,
            delegate=delegate, task_op=task_op_fn,
        )
        router: RecordingRouter = agent._router

        for i, t in enumerate(case.get("turns") or []):
            tr = TurnResult(index=i)
            try:
                replies_before = len(replies)
                delegated_before = len(delegated)
                msg = UnifiedMessage(
                    text=t["input"], user_id="u1", user_name="张三",
                    chat_id=chat_id, message_id=f"golden-{i}", is_at_bot=True,
                )
                await asyncio.wait_for(agent.handle(msg), timeout=TURN_TIMEOUT_SECONDS)
                d = router.decisions[-1] if len(router.decisions) > i else None
                if d is None:
                    tr.error = "路由器未产出决策（确定性拦截或异常）"
                    result.turns.append(tr)
                    result.status = "error"
                    break
                outcome = {
                    "index": i,
                    "action": d.action.value,
                    "reply": "\n".join(replies[replies_before:]),
                    "delegate_called": len(delegated) > delegated_before,
                    "delegate_draft": delegated[-1] if len(delegated) > delegated_before else {},
                    "pending_resolution": d.pending_resolution.value,
                    "task_op": d.task_op.model_dump() if d.task_op else {},
                    "repo_op": d.repo_op.model_dump() if d.repo_op else {},
                    "task": d.task.model_dump() if d.task else {},
                    "correction_detected": d.correction_detected,
                    "decision_raw": {
                        "action": d.action.value,
                        "task": d.task.model_dump() if d.task else None,
                        "pending_resolution": d.pending_resolution.value,
                        "correction_detected": d.correction_detected,
                    },
                }
                tr = grade_turn(t.get("expected") or {}, outcome)
            except asyncio.TimeoutError:
                tr.error = f"turn 超时（>{TURN_TIMEOUT_SECONDS}s）"
                result.turns.append(tr)
                result.status = "error"
                break
            except Exception as exc:  # noqa: BLE001 —— 单 case 异常隔离，suite 必须跑完
                tr.error = f"{type(exc).__name__}: {exc}"
                result.turns.append(tr)
                result.status = "error"
                break
            result.turns.append(tr)
            if tr.score == 0:
                result.status = "failed"   # 结果性失败，后续轮失去前提
                break
        else:
            all_turns = result.turns
            result.status = "passed" if all_turns and all(tr.score >= PASS_THRESHOLD for tr in all_turns) else "failed"

        result.score = min((tr.score for tr in result.turns), default=0) if result.status != "error" else 0
    except Exception as exc:  # noqa: BLE001
        result.status = "error"
        result.error = f"环境/执行失败：{type(exc).__name__}: {exc}"
    finally:
        # ---- 打扫：释放句柄（Windows 文件锁），settings 回滚；目录留给下轮运行开头清理 ----
        if mem is not None:
            try:
                await mem.aclose()
            except Exception:  # noqa: BLE001
                pass
        (settings.memory_db_url, settings.chroma_persist_dir, settings.console_llm_dump_dir) = saved
        result.duration_s = round(time.time() - t0, 1)

    return result


# ---- 入口 ----

def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=BACKEND.parent,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


async def main_async(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="OpenDomino 黄金测试集（路由族）")
    ap.add_argument("--cases", default="", help="逗号分隔 case id，缺省跑全部")
    ap.add_argument("--list", action="store_true", help="仅列出 case")
    args = ap.parse_args(argv)

    cases = load_cases(CASES_DIR)
    if args.list:
        for c in cases:
            print(f"{c['id']:45s} {c.get('priority', 'P1'):3s} {c.get('category', ''):15s} {c.get('name', '')}")
        return 0
    if not settings.llm_base_url or not settings.llm_api_key:
        print("LLM 未配置（LLM_BASE_URL / LLM_API_KEY），黄金集需要真实 LLM，退出。")
        return 2
    only = {s.strip() for s in args.cases.split(",") if s.strip()}
    if only:
        cases = [c for c in cases if c["id"] in only]
        missing = only - {c["id"] for c in cases}
        if missing:
            print(f"未找到 case: {sorted(missing)}")
            return 2

    if TMP_ROOT.exists():
        shutil.rmtree(TMP_ROOT, ignore_errors=True)   # 清理上一次运行残留（本进程尚未持有句柄）
    TMP_ROOT.mkdir(parents=True, exist_ok=True)

    print(f"黄金测试集：{len(cases)} 个 case，模型 {settings.console_model}，开始……")
    results: list[CaseResult] = []
    for idx, case in enumerate(cases, 1):
        case_dir = TMP_ROOT / re.sub(r"[^a-z0-9-]", "-", case["id"].lower())[:40]
        case_dir.mkdir(parents=True, exist_ok=True)
        r = await run_case(case, case_dir)
        results.append(r)
        mark = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP", "error": "ERR!"}[r.status] if r.status else "??"
        print(f"[{idx}/{len(cases)}] {r.case_id:45s} {mark:4s} {r.score:3d}  {r.duration_s:5.1f}s")
        for tr in r.turns:
            for d in tr.deductions:
                print(f"    - turn{tr.index} {d['points']:>3d}分：{d['reason']}")
            if tr.error:
                print(f"    - turn{tr.index} 错误：{tr.error}")
        if r.error:
            print(f"    - {r.error}")

    passed = sum(1 for r in results if r.status == "passed")
    skipped = sum(1 for r in results if r.status == "skipped")
    failed = sum(1 for r in results if r.status == "failed")
    errored = sum(1 for r in results if r.status == "error")
    report = {
        "suite": "golden-routing-v1",
        "started_cases": len(results),
        "summary": {
            "total": len(results), "passed": passed, "failed": failed,
            "error": errored, "skipped": skipped,
            "pass_rate": round(passed / len(results), 3) if results else 0.0,
        },
        "config": {
            "console_model": settings.console_model,
            "embedding_model": settings.embedding_model,
            "git_commit": _git_commit(),
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "cases": [asdict(r) for r in results],
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果：{passed} passed / {failed} failed / {errored} error / {skipped} skipped"
          f"（pass_rate {report['summary']['pass_rate']:.0%}）")
    print(f"报告：{REPORT_PATH}")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(main_async(sys.argv[1:])))


if __name__ == "__main__":
    main()
