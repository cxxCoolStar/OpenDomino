# 任务委派（delegate → SandboxAgent）

## Sub-features

- Router 决策 `delegate` → `TaskService.create` → `TaskExecutor.submit` → 群内受理通知
- TaskPipeline：pending → running → review → done/failed
- modify：容器内 clone 仓库 → LLM 执行循环改代码 → pytest 验证 → push `domino/task-*` 分支 → diff 回传群
- investigate：只读探查 → 排查结论回传群（不产生分支）

## How to get to it (user POV)

用户 @Domino 说"帮我把 XX 仓库的登录超时改成 60 秒"，机器人回复受理通知（带任务 ID），后台跑完把分支名和 diff 发回群里。

## Driving it with L1 脚本（容器层）

```bash
cd backend
REPO_E2E_URL=https://github.com/<user>/<repo>.git REPO_E2E_NAME=e2e \
  [TASK_TYPE=modify|investigate|control] python scripts/e2e_sandbox.py
```

前置：Docker Desktop 运行中、git 凭据缓存了 GitHub PAT、预装镜像 `opendomino-sandbox:latest`。
产物：`data/e2e_sandbox_result.json`（task 终态 + TaskEvent 序列 + 分支名）+ 远端 `domino/task-*` 分支。

## Driving it with L2（管线层，不需要 Docker）

`tests/test_sandbox.py`、`scripts/e2e_sandbox.py` 的 TASK_TYPE=control 场景：不跑容器，验证进展查询（TaskOpHandler.status）与取消语义（pending 直落 CANCELED）。

## Gotchas

- Docker daemon 不可用是 Windows 常态故障（Docker Desktop 未启动）——先 Doctor 第 4 步，失败就明确报告"容器层未验证"
- 任务指令里的 marker 文件模式（`e2e_marker.md`）是最小可验证改动，别改成触碰业务代码
- `sandbox_max_concurrency` 默认 2，并行提交超过会排队
- 重启恢复语义：pending 续跑、running/review 标失败——验证重启场景看 `main.py` lifespan 的 `tasks.recover()`
