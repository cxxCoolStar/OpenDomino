"""容器初始化 E2E 验证脚本（clone 认证修复）：真实 L2 绑定 + 真实 Docker + 真实 GitHub。

验证链路（ADR-004 _create 初始化序列，修复项：L2 路径 token 注入 + clone 失败可见性）：
  1. L2 绑定路径 token 解析：resolve_repo_config 走 L2 绑定时能从 .env GITHUB_TOKEN
     拿到 token（修复前 _env_token_for 只查 os.environ，pydantic-settings 不注入，
     导致 L2 主路径 token 恒为空 → 匿名 clone 私有仓失败）；
  2. 真实容器初始化：ContainerPool._create 对真实私有仓库 clone 成功，
     git rev-parse 探测通过；
  3. clone 失败可见性：故意指向不存在的仓库，ContainerError 消息含 git 真实
     报错（而非 NOT_A_REPO），且 token 即使出现在输出中也已被 sanitize 脱敏。

运行：cd backend && python scripts/e2e_container_init.py
产出：scripts/e2e_container_init_report.json（可重复验证 artifact）
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.memory.service import SQLiteMemoryService  # noqa: E402
from app.sandbox.container import (  # noqa: E402
    ContainerError,
    ContainerPool,
)
from app.sandbox.repo_config import (  # noqa: E402
    resolve_repo_config,
)

REPORT_PATH = Path(__file__).resolve().parent / "e2e_container_init_report.json"

# 独立临时库（不碰生产 data/opendomino.db）；绑定表结构与生产无关，直接新建
TMP_DB = Path("./data/e2e_container_init_tmp.db")
if TMP_DB.exists():
    TMP_DB.unlink()

CHAT_ID = "e2e-init-chat"
REPO_NAME = "e2e-init-repo"
REPO_URL = "https://github.com/cxxCoolStar/OpenDomino.git"
BAD_REPO_URL = "https://github.com/cxxCoolStar/__no_such_repo_e2e__.git"

results: list[dict] = []


def record(step: str, passed: bool, detail: str = "") -> None:
    results.append({"step": step, "passed": passed, "detail": detail})
    mark = "PASS" if passed else "FAIL"
    print(f"[{mark}] {step}" + (f" — {detail}" if detail else ""))


async def main() -> int:
    if not settings.github_token:
        print("SKIP: settings.github_token 未配置（.env 缺 GITHUB_TOKEN），无法验证认证 clone")
        return 2

    memory = SQLiteMemoryService(db_url=f"sqlite+aiosqlite:///{TMP_DB}")
    await memory.init()
    await memory.bind_repo(CHAT_ID, REPO_NAME, REPO_URL, bound_by="e2e")

    # ---- 步骤 1：L2 绑定路径 token 解析 ----
    cfg = await resolve_repo_config(REPO_NAME, chat_id=CHAT_ID, memory=memory)
    ok1 = cfg is not None and bool(cfg.token)
    record("L2 绑定路径 token 解析", ok1,
           "cfg.token 从 .env GITHUB_TOKEN 兜底注入" if ok1 else f"cfg={cfg!r}")

    # ---- 步骤 2：真实容器 clone 私有仓库 ----
    pool = ContainerPool()
    executor = None
    ok2 = False
    detail2 = ""
    try:
        executor = await pool.acquire(REPO_NAME, chat_id=CHAT_ID, memory=memory)
        probe = await executor.exec("git rev-parse --is-inside-work-tree && git log --oneline -1")
        ok2 = "true" in probe
        detail2 = probe.strip()[:120]
    except Exception as e:  # noqa: BLE001
        detail2 = f"{type(e).__name__}: {e}"
    finally:
        record("真实容器 clone 私有仓库", ok2, detail2)

    # ---- 步骤 3：clone 失败可见性 + 脱敏 ----
    ok3 = False
    detail3 = ""
    try:
        bad_cfg = await resolve_repo_config(REPO_NAME, chat_id=CHAT_ID, memory=memory)
        bad_cfg.url = BAD_REPO_URL
        bad_cfg.clone_url  # noqa: B018 —— 确认属性可访问
        # 直接以错误 URL 走 _create 等价路径：用 acquire 拿到池后手动构造
        # （不复用池内 entry——容器已 clone 成功，需走全新容器验证失败路径）
        await pool._create(bad_cfg)
        detail3 = "未抛出 ContainerError"
    except ContainerError as e:
        msg = str(e)
        has_git_error = ("git" in msg.lower()
                         and "NOT_A_REPO" not in msg
                         and "Repository not found" in msg)
        no_token_leak = settings.github_token not in msg
        ok3 = has_git_error and no_token_leak
        detail3 = (f"git 报错可见={has_git_error}, token 已脱敏={no_token_leak}: "
                   + msg[:150])
    except Exception as e:  # noqa: BLE001
        detail3 = f"异常类型不符: {type(e).__name__}: {e}"
    finally:
        record("clone 失败可见性 + 脱敏", ok3, detail3)

    # 清理：成功 clone 的容器入池，显式销毁
    await pool.close()

    report = {
        "script": "e2e_container_init.py",
        "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "repo": REPO_NAME,
        "all_passed": all(r["passed"] for r in results),
        "results": results,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n产物: {REPORT_PATH}")
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
