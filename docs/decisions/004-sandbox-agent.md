# ADR-004 SandboxAgent：执行与交付

> 状态：草案（待按 fastclaw 调研结论重写——容器池化、强制汇总、安全三件套、反死循环等待确认）
> 日期：2026-09-23
> 前置依赖：ADR-001 协作入口层、ADR-002 三层记忆、ADR-003 ConsoleAgent
> 调研参考：fastclaw（`internal/sandbox`、`internal/agent/loop.go`）

## 背景

SandboxAgent 是慢路径的执行侧，接收 ConsoleAgent 委派的 Task，在真实仓库上完成
「识别工程规范 → 定位 → 编码 → 构建测试 → 评审 → 部署 → AI 验证」并回传结果。

原文定位：**执行与交付，真实仓库沙箱**。README 技术栈已定：Docker SDK 沙箱、
Git 真实提交、CodeReview / 部署 / AI 验证为简化闭环。

设计输入：

- ADR-003：Task 概念模型、委派即异步执行、状态变化回传来源群；
- ADR-002：代码现状由 SandboxAgent 实时读取，结论经确认后沉淀 KBase；
- 原文：无人值守不等于无人负责；生产可用来自边界。

## 核心决议

1. **每任务一容器**。Task 开始时启动独立 Docker 容器，clone 真实仓库，任务结束销毁。
   隔离性（任务间互不污染）与可复现性（每次都是干净检出）优先于启动开销；
   第一阶段接受冷启动延迟，容器镜像预热留作优化。
2. **执行采用受控 Agentic 工具循环**，与 ConsoleAgent 的单次结构化路由刻意相反：
   - 理由：编码任务无法一次性规划全部编辑，必须「观察仓库 → 编辑 → 跑测试 → 修正」迭代；
     这是快/慢路径的本质差异，而非设计不一致。
   - 边界：最大步数、总超时、单命令超时、输出截断四重护栏（见「资源与安全边界」）。
3. **分支隔离，禁止直推主干**。所有改动提交到 `domino/task-{task_id}` 分支并 push；
   第一阶段不自动合并、不自动建 PR，评审由人在群内/平台上完成。
4. **原文七阶段映射为五阶段管线**（评审简化、部署退化为可选钩子）：

   | 原文阶段 | OpenDomino 第一阶段实现 |
   |---|---|
   | 识别工程规范 | 读取仓库内 AGENTS.md / CLAUDE.md / README + 注入命中的 SKILL 正文 |
   | 定位 + 编码 + 构建测试 | 合并为一个 Agentic 工具循环（探索、编辑、跑构建测试都在循环内） |
   | 评审 | LLM diff 预评审（自审）+ push 分支 + 群内回传 diff 摘要，人工评审 |
   | 部署 | 可选部署 webhook 钩子（默认关闭），不建设 CI/CD |
   | AI 验证 | 独立 LLM 调用，对照 instruction 检查最终 diff 与测试输出，passed / blocked |

5. **全程留痕，回传统摘要**。每个阶段与每轮工具调用写入 TaskEvent 日志（可查全文）；
   回传群聊的只有阶段级摘要与结论，不刷屏。
6. **失败如实回传**。任务失败（构建不过、步数/超时耗尽、验证 blocked）时回传失败原因
   与关键日志片段，不以「已完成」话术收尾。

## Task 数据模型（完整版）

在 ADR-003 概念模型上补充执行侧字段：

```text
Task
- id / title / instruction / repo          同 ADR-003
- created_by / chat_id / session_id        同 ADR-003
- status      pending → running → review → done | failed | canceled
- branch_name               domino/task-{id}
- base_ref                  任务开始时的基线 commit
- commits                   任务产生的 commit 列表（sha + message）
- skill_used                命中的 SKILL 名（可空）
- verify_result             passed | blocked（附说明）
- error                     失败原因（可空）
- created_at / updated_at
```

TaskEvent（执行留痕，只追加）：

```text
TaskEvent
- id / task_id / seq
- stage       plan | execute | review | verify | report
- kind        llm_call | tool_call | stage_enter | stage_exit | error
- payload     摘要内容（工具名、命令、退出码、token 用量；全文日志落盘文件）
- created_at
```

## 执行管线

```text
TaskService.create（ADR-003 delegate 动作）
  ↓ 入队，status=pending，回复群「已受理」
Executor 领取任务（并发上限 TASK_MAX_CONCURRENCY）
  ↓
1. 启容器      启动容器 → clone 仓库 → 建分支 → 记录 base_ref
  ↓
2. 规范识别    读取仓库内 AGENTS.md 等规范文件（存在才注入，上限预算）
  ↓            SKILL Registry 命中任务类型 → 加载正文（ADR-005）
  ↓
3. 执行循环    系统提示 = instruction + 规范 + SKILL + 工具清单
  ↓            循环：LLM 选择工具 → 容器内执行 → 结果回填 → 直到 LLM 声明完成
  ↓            或触发护栏（步数/超时）
  ↓
4. 评审        LLM diff 预评审（对照 instruction 自审，列出风险与未竟项）
  ↓            push 分支 → status=review → 群内回传分支名 + commit 列表 + diff 摘要
  ↓
5. AI 验证    独立 LLM 调用：instruction vs 最终 diff + 测试输出 → passed / blocked
  ↓            blocked 且剩余步数 > 0 → 回到步骤 3（最多回炉 verify_max_retries 次）
  ↓
6. 收尾        status=done → 回传结论摘要（改了什么、验证结果、分支链接）
  ↓            触发 KBase 提炼判定（ADR-002 触发点 3，复用 ADR-003 提炼管线）
  ↓            销毁容器；全量日志按 task_id 归档
```

## 容器与仓库访问

### 仓库执行配置

仓库注册表（ADR-003）解决「是哪个仓库」；本模块补充「怎么构建测试」：

```
REPO_OPEN_DOMINO_BACKEND_URL=git@...:team/open-domino.git
REPO_OPEN_DOMINO_BACKEND_IMAGE=python:3.12-slim
REPO_OPEN_DOMINO_BACKEND_SETUP_CMD=pip install -r requirements.txt
REPO_OPEN_DOMINO_BACKEND_TEST_CMD=pytest -x -q
```

- 未配置 IMAGE 时使用默认通用镜像；未配置 TEST_CMD 时跳过测试步骤并在验证时注明；
- 凭据通过挂载 git 凭据文件 / SSH agent socket 注入容器，不写进镜像、不进 Prompt。

### 工具集（经 ADR-006 ToolRegistry 注入）

| 工具 | 说明 |
|---|---|
| `list_dir` / `read_file` / `grep` | 仓库探索，输出截断 |
| `write_file` / `apply_patch` | 文件编辑（整体写 / 差量补丁） |
| `run_command` | 容器内 bash，单命令超时 + 输出截断，exit code 回传 |
| `git_diff` / `git_log` | 查看当前改动与提交历史 |

所有工具经 docker exec 在容器内执行，宿主侧只做协议驱动与护栏检查；
工具实现遵循 ADR-006 的 ToolProvider 协议，便于后续替换为 MCP 形态。

## 评审与交付边界

- LLM 预评审输出固定三段：符合 instruction 的部分 / 偏离与风险 / 建议人工关注点；
- 回传消息固定结构：分支名、commit 列表、改动文件统计、预评审摘要、验证结果；
- **不自动合并、不自动建 PR、不自动部署**（部署钩子需显式配置且默认关闭）；
  人在回路是第一阶段交付的唯一合流方式——「无人值守不等于无人负责」。

## 资源与安全边界

| 护栏 | 默认值 | 说明 |
|---|---|---|
| TASK_MAX_STEPS | 40 | 执行循环最大轮次 |
| TASK_TIMEOUT_MINUTES | 30 | 任务总超时（含构建测试） |
| COMMAND_TIMEOUT_SECONDS | 120 | 单命令超时 |
| TOOL_OUTPUT_MAX_CHARS | 8000 | 单次工具输出截断 |
| TASK_MAX_CONCURRENCY | 2 | 同时执行的任务数 |
| VERIFY_MAX_RETRIES | 2 | AI 验证 blocked 后回炉次数 |

- 容器资源限制：CPU / 内存配额，文件系统限于工作目录；
- 超时或失败时容器照常销毁，已 push 的分支保留（现场不丢），未 push 的改动随容器丢弃并如实说明；
- 取消：群内回复 `/取消任务 {id}` 可取消 pending / running 任务，running 任务标记 canceled 后终止容器。

## 状态回传

执行侧通过 ChannelRegistry（chat_id → ChannelAdapter，补充于 ADR-001 模块结构，
不改动其接口定义）按 `task.chat_id` 推送四类事件：

| 事件 | 时机 | 内容 |
|---|---|---|
| 受理 | 任务入队 | 任务 id + 标题 + 排队位置 |
| 开始 | 容器就绪 | 仓库 + 分支名 |
| 待评审 | push 完成 | commit 列表 + diff 摘要 + 预评审要点 |
| 终态 | done / failed / canceled | 结论或失败原因 + 关键日志片段 |

不做进度心跳播报（每轮工具调用不推送）；中间过程通过 TaskEvent 离线可查。

## 模块结构

```
backend/app/sandbox/
├── agent.py         # 执行循环（Agentic loop、护栏、回炉控制）
├── executor.py      # 任务队列、状态机、并发控制
├── container.py     # Docker 容器生命周期（启动、exec、销毁、资源限制）
├── git_ops.py       # clone / branch / commit / push / diff
├── review.py        # LLM diff 预评审
├── verify.py        # AI 验证（passed / blocked 判定）
├── reporter.py      # 状态回传（经 ChannelRegistry）
└── events.py        # TaskEvent 留痕
backend/app/channels/registry.py   # 补充：chat_id → ChannelAdapter 注册表
```

Task / TaskEvent 持久化与 TaskService 归本模块，ConsoleAgent 经其抽象接口创建任务。

## 配置契约

```
SANDBOX_MODEL=                  # 执行循环使用的模型（建议强于 CONSOLE_MODEL）
SANDBOX_DOCKER_IMAGE=python:3.12-slim   # 默认镜像
SANDBOX_MAX_STEPS=40
SANDBOX_TIMEOUT_MINUTES=30
SANDBOX_COMMAND_TIMEOUT_SECONDS=120
SANDBOX_MAX_CONCURRENCY=2
SANDBOX_VERIFY_MAX_RETRIES=2
SANDBOX_DEPLOY_WEBHOOK=         # 可选，部署钩子 URL，默认空=关闭
REPO_{NAME}_URL / _IMAGE / _SETUP_CMD / _TEST_CMD   # 每仓库执行配置
```

## 非目标（第一阶段）

- 自动合并与自动建 PR（GitHub/GitLab API 对接）；
- CI/CD 与部署平台对接（部署仅保留 webhook 钩子位）；
- 多容器编排、远端 K8s 沙箱池、镜像预热；
- 富媒体回传（截图、录屏）；
- 任务拆分与子任务编排（一次委派 = 一个容器 = 一条分支）；
- 沙箱内联网白名单治理（第一阶段容器默认可联网，依赖容器隔离兜底）。

## 待讨论问题

1. 每任务一容器 + 冷启动 clone 的延迟（数十秒级）是否可接受？
2. 第一阶段「push 分支 + 群内回传，人工评审」的交付方式是否足够？
3. 部署 webhook 钩子是否保留，还是彻底砍掉留到后续 ADR？
4. 容器默认允许联网（装依赖需要），风险是否可接受？

## 参考实现

- Domino 原文：SandboxAgent 七阶段管线、真实仓库沙箱、AI 验证。
- ADR-002：代码实时代码访问原则、任务结论沉淀 KBase 的触发点。
- ADR-003：Task 概念模型、委派即异步、仓库注册表。
