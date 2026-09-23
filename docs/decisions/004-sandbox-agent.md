# ADR-004 SandboxAgent：执行与交付

> 状态：已确认
> 日期：2026-09-23
> 前置依赖：ADR-001 协作入口层、ADR-002 三层记忆、ADR-003 ConsoleAgent
> 关联：ADR-005 SKILL 方法层、ADR-006 工具层、ADR-007 链路追踪
> 调研参考：fastclaw（`internal/sandbox`、`internal/agent/loop.go`）
>
> 实现级详细设计见 [docs/design/sandbox-agent-detail.md](../design/sandbox-agent-detail.md)
> （逐点讨论定案：function calling 循环、TaskService 恢复策略、容器内 clone、
> 主动 API 回传、HTTPS+PAT+脱敏、ToolRegistry 单例 + ctx 注入容器）。

## 背景

SandboxAgent 是慢路径的执行侧，接收 ConsoleAgent 委派的 Task，在真实仓库上完成
「识别工程规范 → 定位 → 编码 → 构建测试 → 评审 → 验证」并回传结果。

原文定位：**执行与交付，真实仓库沙箱**。ConsoleAgent 不猜业务代码、不直接改业务仓库；
需要深入读取真实代码、运行命令或产生变更时，才交给 SandboxAgent。任何代码修改都发生在
隔离、可追踪、可验证的环境中。

设计输入：

- ADR-003：Task 概念模型、委派即异步、状态变化经 ChannelRegistry 回传来源群；
- ADR-002：代码现状由 SandboxAgent 实时读取，结论经确认后沉淀 KBase；
- fastclaw：沙箱抽象、执行循环健壮性、反死循环、安全边界的生产实践；
- 原文：无人值守不等于无人负责；生产可用来自边界。

## 核心决议

1. **执行采用受控 Agentic 工具循环**，与 ConsoleAgent 的单次路由刻意相反：编码任务无法
   一次性规划全部编辑，必须「观察仓库 → 编辑 → 跑测试 → 修正」迭代。边界用迭代上限、
   总超时、单命令超时、输出截断、反死循环五重护栏（见「资源与安全边界」）。
2. **容器按仓库维度池化复用，每个任务独占并重置工作区**。不采用「每任务一容器」的冷启动
   方式，而是容器进程常驻复用（免重复起容器/装基础依赖），任务开始时把工作区重置到基线
   （`git reset --hard <base_ref>` + 清理未跟踪文件），既保速度又逼近干净检出。
   参考 fastclaw 的 ExecutorPool + LifecyclePool（懒创建 / idleTTL 驱逐 / 驱逐前 snapshot）。
3. **强制汇总保底交付**。任务跑满迭代上限或超时，**不返回 canned 错误**，而是追加一次
   无工具的强制汇总 LLM 调用，把已完成的改动与未竟项如实汇总回群——用户至少拿到一份
   交付物（参考 fastclaw `loop.go:2764` 的 capReachedNudge）。
4. **反死循环三重保护**（第一阶段就做，编码任务极易陷入「改了跑跑了改」死循环）：
   - 同一工具+参数+结果重复 N 次（默认 3）→ 注入警告并 break；
   - 整轮工具全部失败 → 下一轮摘除工具，强制模型用已有信息作答；
   - 同一工具连败（与参数无关）→ 提前摘除工具。
5. **分支隔离，禁止直推主干**。改动提交到 `domino/task-{task_id}` 分支并 push；第一阶段
   不自动合并、不自动建 PR，评审由人在群内/平台完成。
6. **砍掉部署钩子**。部署非执行 Agent 核心职责，第一阶段彻底不做，留待后续 ADR。
7. **容器默认可联网**（装依赖必需），用安全三件套兜底：危险命令黑名单、子进程不继承父
   env、预览端口只绑 127.0.0.1（参考 fastclaw 的边界工程）。
8. **TaskEvent 与 Langfuse trace 分工（方案 A，各司其职）**：TaskEvent 是给用户/团队的
   业务留痕（落业务库，原文「可分享、可交接、可追溯」的沙箱工作记录）；Langfuse trace 是
   给开发者的技术观测（prompt/token/耗时）。TaskEvent 关联 trace_id，两处分别埋点，
   不合并——业务资产不寄生在调试工具里（详见 ADR-007）。

## Task 数据模型

```text
Task
- id / title / instruction / repo          自包含任务指令与目标仓库
- created_by / chat_id / session_id        发起人、来源群、关联会话
- trace_id                                 链路追踪 ID（ADR-007）
- status      pending → running → review → done | failed | canceled
- branch_name               domino/task-{id}
- base_ref                  任务开始时的基线 commit
- commits                   任务产生的 commit 列表（sha + message）
- verify_result             passed | blocked（附说明）
- error                     失败原因（可空）
- created_at / updated_at
```

TaskEvent（业务留痕，只追加，给用户看）：

```text
TaskEvent
- id / task_id / seq
- stage       setup | execute | review | verify | report
- kind        stage_enter | stage_exit | tool_summary | error
- summary     业务语言摘要（「改了 3 个文件」「构建通过」），非技术细节
- trace_id    关联 Langfuse 链路（ADR-007）
- created_at
```

## 执行管线

```text
TaskService.create（ADR-003 delegate 动作）
  ↓ 落库 status=pending + 内存队列通知，回复群「已受理」
Executor 领取任务（按仓库串行，TASK_MAX_CONCURRENCY 控制全局并发）
  ↓
1. 取容器      按 repo 从容器池取/起容器 → 工作区重置到 base_ref
  ↓
2. 规范识别    读仓库内 AGENTS.md/CLAUDE.md/README（存在才注入，预算上限）
  ↓            系统提示注入 SKILL 目录（name+一句话描述，见 ADR-005）
  ↓
3. 执行循环    系统提示 = instruction + 规范 + SKILL 目录 + 工具清单
  ↓            ReAct 循环：LLM 选工具 → 容器内执行 → 结果回填 → 直到声明完成
  ↓            模型按需 load_skill 读 SKILL 全文（ADR-005 方式一）
  ↓            护栏：迭代上限 / 反死循环 / 超时 → 触发强制汇总
  ↓
4. 评审        LLM diff 预评审（对照 instruction 自审，列风险与未竟项）
  ↓            push 分支 → status=review → 群回传分支+commit+diff 摘要
  ↓
5. AI 验证    独立 LLM 调用：instruction vs 最终 diff + 测试输出 → passed/blocked
  ↓            blocked 且剩余步数 > 0 → 回炉步骤 3（最多 VERIFY_MAX_RETRIES 次）
  ↓
6. 收尾        status=done → 回传结论摘要（改了什么、验证结果、分支链接）
  ↓            触发 KBase 提炼判定（ADR-002 触发点 3，复用 ADR-003 提炼管线）
  ↓            工作区不销毁，容器归还池（idleTTL 后驱逐）；全程写 TaskEvent + trace
```

## 容器池与工作区

### 池化策略

- **池化键**：按 `repo` 维度（第一阶段简化 fastclaw 的 (agent,project,session) 键）。
- **生命周期**：懒创建（首个任务才起容器）→ 常驻复用 → idleTTL（默认 10 分钟）空闲驱逐。
- **工作区重置**：每个任务领取容器后，先把工作区 `git reset --hard <base_ref>` + 清未跟踪
  文件，保证代码干净；容器外壳（含已装基础依赖）复用。
- **同仓库并发**：第一阶段用队列按仓库串行（单仓库单容器，避免任务互抢互看改动）；
  全局并发由 `TASK_MAX_CONCURRENCY` 控制。同仓库高频并发时再演进为多容器池。

### 仓库执行配置

群仓库绑定表（ADR-003）解决「是哪个仓库」；本模块补充「怎么构建测试」：

```
REPO_{NAME}_URL=git@...:team/repo.git
REPO_{NAME}_IMAGE=python:3.12-slim
REPO_{NAME}_SETUP_CMD=pip install -r requirements.txt
REPO_{NAME}_TEST_CMD=pytest -x -q
```

- 未配 IMAGE 用默认镜像；未配 TEST_CMD 跳过测试并在验证时注明；
- 凭据通过挂载 git 凭据/SSH agent socket 注入容器，不进镜像、不进 Prompt。

## 工具集（经 ADR-006 ToolRegistry 注入）

| 工具 | 说明 |
|---|---|
| `list_dir` / `read_file` / `grep` | 仓库探索，输出截断，read_file 二进制嗅探拒绝 |
| `write_file` / `apply_patch` | 文件编辑（整体写 / 差量补丁） |
| `run_command` | 容器内 bash，真超时进程组击杀 + 输出截断 |
| `git_diff` / `git_log` | 查看改动与提交历史 |
| `load_skill` | 按需读 SKILL 全文（ADR-005 方式一） |

所有工具经容器 exec 执行，宿主侧只做协议驱动与护栏检查；遵循 ADR-006 ToolProvider 协议。

## 资源与安全边界

| 护栏 | 默认值 | 说明 |
|---|---|---|
| SANDBOX_MAX_STEPS | 40 | 执行循环最大轮次，超出触发强制汇总 |
| SANDBOX_TIMEOUT_MINUTES | 30 | 任务总超时 |
| COMMAND_TIMEOUT_SECONDS | 120 | 单命令超时（真超时进程组击杀） |
| TOOL_OUTPUT_MAX_CHARS | 8000 | 单次工具输出截断 |
| TASK_MAX_CONCURRENCY | 2 | 全局并发任务数 |
| VERIFY_MAX_RETRIES | 2 | AI 验证 blocked 后回炉次数 |
| CONTAINER_IDLE_TTL_MIN | 10 | 容器空闲驱逐时间 |
| LOOP_SAME_CALL_LIMIT | 3 | 反死循环：同调用重复上限 |

安全防护：

- **危险命令黑名单**：`rm -rf /`、`mkfs`、`dd if=`、fork bomb、`> /dev/sda` 等子串拦截；
- **env 不继承**：子进程永不继承宿主 os.Environ()，显式构造脱敏后的 env；
- **端口只绑本地**：容器预览端口只绑 127.0.0.1，防 LLM 生成的 dev server 暴露公网；
- **路径沙箱**：拒绝逃逸工作区的绝对路径与 `..` 穿越；
- **真超时**：单命令超时杀整个进程组（setsid+pgid），不只杀直接子进程，防 pytest 子进程漏杀；
- **资源限额**：容器 CPU/内存配额，文件系统限于工作目录；
- 任务失败/取消时容器照常归还或销毁，已 push 分支保留（现场不丢）。

## 评审与交付边界

- LLM 预评审输出三段：符合 instruction 的部分 / 偏离与风险 / 建议人工关注点；
- 回传固定结构：分支名、commit 列表、改动文件统计、预评审摘要、验证结果；
- **不自动合并、不自动建 PR、不自动部署**：人在回路是第一阶段交付的唯一合流方式
  （无人值守不等于无人负责）。

## 状态回传

经 ChannelRegistry（chat_id → ChannelAdapter）按 `task.chat_id` 推送四类事件：

| 事件 | 时机 | 内容 |
|---|---|---|
| 受理 | 任务入队 | 任务 id + 标题 |
| 开始 | 容器就绪 | 仓库 + 分支名 |
| 待评审 | push 完成 | commit 列表 + diff 摘要 + 预评审要点 |
| 终态 | done/failed/canceled | 结论或失败原因 + 关键片段 |

不做进度心跳播报（每轮工具调用不推送群）；中间过程经 TaskEvent 离线可查。

## 模块结构

```
backend/app/sandbox/
├── agent.py         # 执行循环（ReAct loop、反死循环、强制汇总、回炉控制）
├── executor.py      # 任务队列、状态机、按仓库串行、并发控制
├── container.py     # 容器池（懒创建、idleTTL 驱逐、工作区重置、资源限制）
├── workspace.py     # 工作区重置（reset 到 base_ref）、snapshot
├── git_ops.py       # clone / branch / reset / commit / push / diff
├── review.py        # LLM diff 预评审
├── verify.py        # AI 验证（passed / blocked 判定）
├── reporter.py      # 状态回传（经 ChannelRegistry）
└── events.py        # TaskEvent 业务留痕
backend/app/channels/registry.py   # chat_id → ChannelAdapter 注册表
```

Task / TaskEvent 持久化与 TaskService 归本模块，ConsoleAgent 经其抽象接口创建任务。

## 配置契约

```
SANDBOX_MODEL=                  # 执行循环使用的模型（建议强于 CONSOLE_MODEL）
SANDBOX_DOCKER_IMAGE=python:3.12-slim
SANDBOX_MAX_STEPS=40
SANDBOX_TIMEOUT_MINUTES=30
SANDBOX_COMMAND_TIMEOUT_SECONDS=120
SANDBOX_MAX_CONCURRENCY=2
SANDBOX_VERIFY_MAX_RETRIES=2
SANDBOX_CONTAINER_IDLE_TTL_MIN=10
SANDBOX_LOOP_SAME_CALL_LIMIT=3
REPO_{NAME}_URL / _IMAGE / _SETUP_CMD / _TEST_CMD
```

## 非目标（第一阶段）

- 自动合并与自动建 PR；
- 部署（CI/CD 与部署平台对接，已砍）；
- 多容器池、远端 K8s 沙箱、镜像预热；
- 富媒体回传（截图、录屏）；
- 任务拆分与子任务编排（一次委派 = 一个容器工作区 = 一条分支）；
- E2B 等远端沙箱后端（第一阶段仅本地 Docker）。

## 参考实现

- Domino 原文：SandboxAgent 执行链、真实仓库沙箱、AI 验证、沙箱工作记录。
- fastclaw：`internal/sandbox/executor.go`（Executor 接口与池化）、
  `internal/sandbox/lifecycle.go`（懒创建/驱逐/snapshot）、
  `internal/agent/loop.go`（ReAct loop、强制汇总、反死循环、真超时）、
  `internal/agent/tools/exec.go`（危险命令黑名单、env 不继承）。
- ADR-002 / ADR-003 / ADR-005 / ADR-006 / ADR-007。
