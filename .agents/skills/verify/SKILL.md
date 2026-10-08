---
name: verify
description: 驱动 OpenDomino 并证明改动有效。以真实用户的方式走一遍链路（群消息 → ConsoleAgent 路由 → SandboxAgent 交付），产出机器可读证据。任何代码改动合入前先跑本技能。
---

# OpenDomino 验证技能

本项目没有 Web UI：用户界面是飞书群聊，管线是「飞书消息 → ConsoleAgent 路由（answer/clarify/delegate/repo_op/task_op）→ SandboxAgent 容器交付」。因此验证分三个层级，按改动面选择，**永远跑你能承受的最高层级**：

| 层级 | 覆盖 | 用途 |
|---|---|---|
| L1 链路 E2E 脚本 | 单条链路全闭环（FakeLLM/临时库） | 改动落在一条链路内（记忆/蒸馏/通道/文档同步） |
| L2 内存驱动 ConsoleAgent | 路由五动作 + 上下文装配 | 改动落在 console 路由/装配层 |
| L3 真实进程 + HTTP | 进程能起、健康检查、任务管控 API | 启动装配、配置、进程级回归 |

Docker 沙箱任务（modify/investigate 的容器执行）需要 Docker Desktop + 镜像 `opendomino-sandbox:latest`；daemon 不可用时**明确报告跳过了什么**，不要静默降级为"看起来没问题"。

## Launch

```bash
cd backend
# 独立验证实例：临时库 + 独立端口，不碰 data/opendomino.db 真实数据
MEMORY_DB_URL="sqlite+aiosqlite:///./data/verify_tmp.db" \
CHROMA_PERSIST_DIR="./data/verify_chroma" \
python -m uvicorn app.main:app --port 8901
```

就绪判定：`curl -sf http://localhost:8901/health` 返回 `{"status":"ok"}`，且 `/ready` 返回 `status=ready`。真实 LLM/飞书凭据由 `.env` 提供（Launch 必须真实凭据，否则 lifespan 直接 RuntimeError）。

需要**不碰真实 LLM/飞书**的进程级验证时，不要 Launch 整个 app——用 L1/L2 层级脚本，它们自带 FakeLLM 与假凭据（见 scripts/e2e_*.py 的隔离模式）。

## Doctor（只读，先跑这个再驱动）

按顺序检查，任何一步失败先修环境再继续：

1. `cd backend && python -c "import fastapi, uvicorn, aiosqlite, chromadb"` —— 依赖在位（venv 是否激活）
2. `python -c "import sys; sys.path.insert(0,'.'); from app.config import settings; print(settings.memory_db_url)"` —— 配置可加载、确认当前指向的库路径（**防止误用真实库**）
3. `curl -sf http://localhost:8901/health`（若已 Launch）—— 进程存活
4. `docker version --format '{{.Server.Version}}'` —— 沙箱任务可用性（失败只影响 modify/investigate 容器层，L1/L2 不受影响）

## Drive

### L1：链路 E2E 脚本（首选，确定性，不依赖外部服务）

```bash
cd backend
python scripts/e2e_distill.py          # 旁听蒸馏全链路（ADR-009）
python scripts/e2e_feishu_channel.py   # 通道解析/分流/旁听（ADR-008）
python scripts/e2e_kbsync.py           # 知识库文档同步（ADR-011）
python scripts/e2e_sandbox.py          # 沙箱任务；需要 Docker + GitHub 凭据 + REPO_E2E_URL
cd .. && python -m app.docs.import_docs ../docs   # 文档规范校验
```

每个脚本自带隔离（`e2e_*_tmp.db` + 假凭据）并在 `scripts/` 落可重复验证的报告 JSON。退出码 0 且报告里 FAIL 为空才算过。

### L2：内存驱动 ConsoleAgent（路由/装配层改动用）

模仿 `tests/test_console_agent.py` 的 FakeLLM 模式，写一次性驱动脚本（放 `data/verify_drive.py`，验证完删除）：

```python
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))  # backend/ 下运行
# 必须在 import app.config 之前设置隔离环境变量
import os
os.environ["MEMORY_DB_URL"] = "sqlite+aiosqlite:///./data/verify_tmp.db"
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9")
os.environ.setdefault("LLM_API_KEY", "verify-dummy")
os.environ.setdefault("CONSOLE_MODEL", "verify-dummy")
os.environ.setdefault("SANDBOX_MODEL", "verify-dummy")
os.environ.setdefault("FEISHU_APP_ID", "cli_verify")
os.environ.setdefault("FEISHU_APP_SECRET", "verify-secret")

from app.channels.base import UnifiedMessage
from app.console.agent import ConsoleAgent
from app.console.router import Router
from app.memory.service import SQLiteMemoryService

class FakeLLM:
    def __init__(self, outputs): self._outputs = list(outputs)
    async def complete(self, system, user, *, model=None): return self._outputs.pop(0)

async def main():
    mem = SQLiteMemoryService(); await mem.init()
    replies = []
    async def reply(chat_id, text, webhook=""): replies.append((chat_id, text))
    agent = ConsoleAgent(mem, Router(FakeLLM(['{"action":"answer","answer":"验证回复"}'])),
                         reply, lambda d: (_ for _ in ()).throw(AssertionError("不应委派")),
                         lambda *a: (_ for _ in ()).throw(AssertionError("不应管控")))
    await agent.handle(UnifiedMessage(text="发布流程是什么", user_id="u1",
                                      chat_id="c1", message_id="v1"))
    assert replies and "验证回复" in replies[0][1], f"路由未回话: {replies}"
    print(json.dumps({"ok": True, "replies": replies}, ensure_ascii=False))

asyncio.run(main())
```

要点：`UnifiedMessage(text, user_id, chat_id, message_id)` 是驱动入口；`FakeLLM` 按队列吐 JSON 决策（answer/clarify/delegate/repo_op/task_op）；断言 reply 内容与 L1/L2 session 落库。

### L3：真实进程 + HTTP

起服务后只打 `/health`、`/ready`。**没有其他 HTTP 业务端点**——管线驱动只能走 L1/L2（或真实飞书，本技能不覆盖）。

## Evidence

证据落 `backend/data/verify_artifacts/`（gitignore 范围内，不提交）：

1. **每次驱动一条 JSON**：脚本 stdout 用 `json.dumps(..., ensure_ascii=False)`，重定向存档
2. **L1 脚本报告**：`scripts/e2e_*_report.json` 原样保留在 PR 描述里引用
3. **日志摘录**：改动涉及后台循环/调度时，附 `logs/opendomino.log` 相关行（grep 时间戳窗口）
4. **判定标准**：FAIL 名称为空 / 断言全过 / 无 ERROR 级新日志。「测试通过」不算验证——必须说明你驱动了哪条链路、断言了什么行为

## Cleanup

1. 杀掉你起的 uvicorn（记下 PID，`kill <pid>`；**禁止**按进程名 pkill——用户可能开着真实实例）
2. 删除临时产物：`data/verify_tmp.db*`、`data/verify_chroma/`、`data/verify_drive.py`
3. 保留：`scripts/e2e_*_report.json`（可重复验证 artifact，AGENTS.md 要求）
4. 清理后确认证据文件仍在、且 `data/opendomino.db` 的 mtime 未变（没误碰真实库）

## Helpers

- 隔离三件套（所有驱动脚本的头部模板）：临时 `MEMORY_DB_URL` + `CHROMA_PERSIST_DIR` + 假凭据。参考 `scripts/e2e_feishu_channel.py` 头部
- ChromaDB 持久化目录必须指向临时路径（conftest.py 的教训：不隔离会污染真实向量库）
- 黄金测试集回归（路由族 21 case，需真实 LLM）：`python scripts/run_golden.py`，报告 `data/golden_report.json`
- pytest 单测（隔离 fixture 已内建）：`cd backend && python -m pytest tests/ -x`
- 本技能由 `/maintain-verification-skill` 负责保鲜；链路变了先改这里再改代码
