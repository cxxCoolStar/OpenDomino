"""第 1 级端到端测试（ADR-004 SandboxAgent）：脚本级 E2E，不碰钉钉。

链路：TaskService.create → TaskPipeline.run_task → 真实 Docker 容器
（clone GitHub 仓库 → LLM 执行循环改代码 → pytest 验证 → push domino/task-* 分支）。

ADR-010 场景（TASK_TYPE=control）：不跑容器，在真实管线上验证进展查询
（TaskOpHandler.status：事件留痕 + 汇总列表）与取消语义（pending 直落 CANCELED），
事件留痕随任意 TASK_TYPE 一并断言。

前置条件：
- Docker Desktop 已启动（docker version 可用）；
- 本地 git 凭据管理器已缓存 GitHub PAT（对目标仓库有 push 权限）；
- 环境变量 REPO_{NAME}_URL 已配置（token 由本脚本从凭据管理器注入，不落盘）。

产物（AGENTS.md 要求的可验证产物）：
- data/e2e_sandbox_result.json —— task 终态 + TaskEvent 序列 + 分支名
  （+ control 场景的查询/取消记录）；
- 远端 domino/task-* 分支（含 e2e 标记文件的 commit）。

用法：
    cd backend
    REPO_E2E_URL=https://github.com/<user>/<repo>.git \
    REPO_E2E_NAME=e2e \
    [TASK_TYPE=modify|investigate|control] \
    python scripts/e2e_sandbox.py
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.console.task_ops import TaskOpHandler  # noqa: E402
from app.sandbox.agent import SandboxAgent  # noqa: E402
from app.sandbox.container import ContainerPool  # noqa: E402
from app.sandbox.control import TaskControl  # noqa: E402
from app.sandbox.llm import OpenAIChatClient  # noqa: E402
from app.sandbox.pipeline import TaskPipeline  # noqa: E402
from app.sandbox.reporter import TaskReporter  # noqa: E402
from app.sandbox.storage import TaskService  # noqa: E402
from app.sandbox.types import TaskStatus  # noqa: E402

# 任务指令：最小可验证改动——新增 marker 文件，不触碰业务代码
INSTRUCTION = (
    "这是一个端到端验证任务。请在仓库根目录创建文件 e2e_marker.md，内容为一行："
    "# E2E marker created by OpenDomino SandboxAgent\n"
    "然后用 git add e2e_marker.md && git commit -m 'e2e: sandbox marker' 提交。"
    "不要修改任何其他文件。"
)

# 排查任务指令：只读探索 + 结论回传（原文主路径，不产生分支）
INVESTIGATE_INSTRUCTION = (
    "这是一个端到端排查验证任务。请探查仓库根目录的目录结构，"
    "回答两个问题：1) backend/app 下有哪些一级模块目录？2) README.md 的标题是什么？"
    "只做只读探查，不要修改或提交任何文件，回答控制在 5 行以内。"
)


def _token_from_credential_manager(host: str = "github.com") -> str:
    """从本地 git 凭据管理器取 PAT（只在内存中流转，不打印、不落盘）。"""
    probe = subprocess.run(
        ["git", "credential", "fill"],
        input=f"protocol=https\nhost={host}\n\n",
        capture_output=True, text=True, check=True,
    )
    for line in probe.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    raise RuntimeError(f"凭据管理器中没有 {host} 的 PAT，请先 git push 一次或手动配置")


def _token_scopes_ok(repo_url: str, token: str) -> bool:
    """轻量校验：token 对该仓库是否可访问（https API）。仅对 github.com 生效。"""
    if "github.com" not in repo_url:
        return True  # 其他平台跳过（如 GitLab 私服）
    import httpx

    m = repo_url.split("github.com/")[-1].removesuffix(".git")
    try:
        r = httpx.get(
            f"https://api.github.com/repos/{m}",
            headers={"Authorization": f"token {token}"},
            timeout=15,
        )
        return r.status_code == 200
    except Exception:
        return False


async def run_control_scenario(tasks: TaskService) -> dict:
    """ADR-010 控制场景（无容器）：真实 TaskService + TaskOpHandler 验证
    进展查询与取消语义。产物结构与其他场景一致，写入同一 result JSON。"""
    control = TaskControl()

    class QuietReporter(TaskReporter):
        def __init__(self):
            pass  # 不接 registry，本场景不推群

    # pending 任务：cancel 直落 CANCELED
    t_pending = await tasks.create({
        "title": "E2E control pending",
        "instruction": "（control 场景）", "repo": "e2e", "type": "investigate",
        "chat_id": "e2e-chat", "session_id": "e2e-session", "created_by": "e2e-script",
    })
    handler = TaskOpHandler(tasks, control)
    cancel_reply = await handler.handle("e2e-chat", "cancel", t_pending, "")
    t_pending_row = await tasks.get(t_pending)
    assert t_pending_row.status == TaskStatus.CANCELED, cancel_reply

    # running 任务：制造事件留痕 → status 单查带最近动态；steer 校验下发
    t_running = await tasks.create({
        "title": "E2E control running",
        "instruction": "（control 场景）", "repo": "e2e", "type": "investigate",
        "chat_id": "e2e-chat", "session_id": "e2e-session", "created_by": "e2e-script",
    })
    await tasks.set_status(t_running, TaskStatus.RUNNING)
    await control.bind(t_running)
    await tasks.append_event(t_running, "execute", "tool_summary", "run_command pytest tests/")
    await tasks.append_event(t_running, "execute", "tool_summary", "read_file backend/app/main.py")

    list_reply = await handler.handle("e2e-chat", "status", "", "")
    one_reply = await handler.handle("e2e-chat", "status", f"#{t_running[:8]}", "")
    steer_reply = await handler.handle("e2e-chat", "steer", t_running, "只看 auth 模块")
    steered = await control.drain_steer(t_running)

    assert f"#{t_running[:8]}" in list_reply and "执行中" in list_reply
    assert "run_command pytest tests/" in one_reply      # 单查带事件动态
    assert "下一步" in steer_reply and steered == ["只看 auth 模块"]

    print("  [control] cancel→CANCELED ok; status 单查带动态 ok; steer 下发 ok")
    return {
        "cancel_pending_reply": cancel_reply,
        "status_list_reply": list_reply,
        "status_one_reply": one_reply,
        "steer_reply": steer_reply,
        "steered": steered,
    }


async def main() -> int:
    t0 = time.time()
    task_type = os.environ.get("TASK_TYPE", "modify")
    repo_name = os.environ.get("REPO_E2E_NAME", "e2e")
    repo_url = os.environ.get("REPO_E2E_URL", "")

    # 组件装配（与 main.py 相同的管线，但 reporter 用打桩）——control 场景也要
    tasks = TaskService(db_url="sqlite+aiosqlite:///./data/e2e_sandbox.db")
    await tasks.init()

    if task_type == "control":
        # ADR-010 控制场景：不碰 Docker / LLM / 凭据，验证查询与管控语义
        control_result = await run_control_scenario(tasks)
        out = Path("data/e2e_sandbox_result.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        result = {
            "scenario": "control",
            "control": control_result,
            "duration_seconds": round(time.time() - t0, 1),
        }
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n✅ E2E PASS（control 场景）—— 产物: {out.resolve()}")
        await tasks.close()
        return 0

    if not repo_url:
        print("ERROR: 请设置 REPO_E2E_URL（HTTPS 格式，如 https://github.com/user/repo.git）")
        return 2

    token = _token_from_credential_manager()
    if not _token_scopes_ok(repo_url, token):
        print("ERROR: 凭据管理器中的 token 对该仓库无访问权限")
        return 2

    # 注入沙箱配置（内存中，不落盘）
    os.environ[f"REPO_{repo_name.upper()}_URL"] = repo_url
    os.environ[f"REPO_{repo_name.upper()}_TOKEN"] = token
    os.environ[f"REPO_{repo_name.upper()}_TEST_CMD"] = "python --version"  # 零风险验证命令

    settings.sandbox_timeout_minutes = 15   # E2E 上限收紧
    # sandbox_model 留空时 sandbox/llm.py 自动回退 console_model，无需在此补丁

    events_log: list[tuple[str, str, str]] = []

    class RecordingReporter(TaskReporter):
        """记录所有回传事件，替代真实群通知（本层不测钉钉）。"""

        def __init__(self):
            # 不调 super().__init__——本层不测钉钉，_notify 永远不会被走到（全部覆写）
            pass

        async def accepted(self, task):
            events_log.append(("accepted", task.status.value, task.id))
            print(f"  [reporter] 受理 task={task.id[:8]}")

        async def started(self, task):
            events_log.append(("started", task.status.value, task.branch_name))
            kind = "排查" if task.type == "investigate" else "变更"
            branch = task.branch_name or "(无分支)" if task.type == "investigate" else task.branch_name
            print(f"  [reporter] {kind}任务开始 branch={branch}")

        async def concluded(self, task, conclusion: str):
            events_log.append(("concluded", task.status.value, conclusion[:80]))
            print(f"  [reporter] 排查结论: {conclusion[:80]!r}")

        async def review_ready(self, task, **kw):
            events_log.append(("review_ready", task.status.value, str(len(kw.get('commits', [])))))
            print(f"  [reporter] 待评审 commits={len(kw.get('commits', []))}")

        async def finished(self, task):
            events_log.append(("finished", task.status.value, task.error or ""))
            print(f"  [reporter] 终态 {task.status.value} {task.error or ''}")

    pool = ContainerPool()
    agent = SandboxAgent(OpenAIChatClient())
    pipeline = TaskPipeline(tasks, pool, agent, RecordingReporter(), control=TaskControl())

    # 1) 创建任务（TASK_TYPE=investigate 跑排查用例，默认 modify 走完整交付链）
    instruction = INVESTIGATE_INSTRUCTION if task_type == "investigate" else INSTRUCTION
    task_id = await tasks.create({
        "title": f"E2E sandbox {task_type}",
        "instruction": instruction,
        "repo": repo_name,
        "type": task_type,
        "chat_id": "e2e-chat",
        "session_id": "e2e-session",
        "created_by": "e2e-script",
    })
    print(f"[e2e] task created: {task_id} (type={task_type})")

    # 2) 直接执行管线（跳过队列——队列已有单元测试覆盖）
    await pipeline.run_task(task_id)

    # 3) 验证终态
    task = await tasks.get(task_id)
    events = await tasks.events(task_id)
    assert task is not None

    result = {
        "task_id": task_id,
        "status": task.status.value,
        "branch": task.branch_name,
        "base_ref": task.base_ref,
        "commits": task.commits,
        "error": task.error,
        "verify_result": task.verify_result,
        "task_events": [{"seq": e.seq, "stage": e.stage, "kind": e.kind, "summary": e.summary} for e in events],
        "reporter_events": [list(x) for x in events_log],
        "duration_seconds": round(time.time() - t0, 1),
    }

    # ADR-010 断言：执行期事件留痕真实落库（tool_summary 至少 1 条，除非零工具任务）
    tool_events = [e for e in events if e.kind in ("tool_summary", "tool_error")]
    assert tool_events, "ADR-010 事件留痕缺失：执行循环未产出任何 tool_summary/tool_error 事件"

    out = Path("data/e2e_sandbox_result.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'=' * 60}")
    print(f"task status: {task.status.value}")
    print(f"branch:      {task.branch_name}")
    print(f"commits:     {len(task.commits)}")
    print(f"error:       {task.error or '(none)'}")
    print(f"events:      {len(events)} 条 TaskEvent")
    print(f"耗时:        {result['duration_seconds']}s")
    print(f"产物:        {out.resolve()}")

    await pool.close()
    await tasks.close()

    if task.status == TaskStatus.DONE:
        print("\n✅ E2E PASS —— 请到远端仓库确认分支:", task.branch_name)
        return 0
    print("\n❌ E2E FAIL —— 见 data/e2e_sandbox_result.json 的 error 与 task_events")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
