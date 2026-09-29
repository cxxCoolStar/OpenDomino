# ADR-010 任务进展可见与中途管控（task_op）

> 状态：已确认（2026-09-29 讨论定稿并实施）
> 日期：2026-09-29
> 前置依赖：ADR-004 SandboxAgent（Task 状态机、TaskReporter 四类回传）
> 关联：ADR-003 ConsoleAgent（路由动作、Session 状态）

## 背景

任务提交给 SandboxAgent 后，执行期是一个信息黑洞：用户只能收到「受理 / 开始 /
待评审 / 终态」四个离散点的群通知，中间最长 30 分钟（`sandbox_timeout_minutes`）
完全静默，且没有任何主动查询进展的入口。

### 缺口清单

2026-09-29 代码分析定位出四层缺口：

1. **无查询入口**：`TaskService.list_by_chat()` 与 `events()` 已实现但零调用；
   Router 的 Action 枚举只有 `answer/clarify/delegate/repo_op`，没有查询动作；
   HTTP 侧也只有 `/health` 与 `/ready`。
2. **上下文只有裸任务 ID**：委派时 `session.active_task_ids` 只 append 不清理、
   状态不回写，上下文装配出的 `[进行中任务]` 段永远是过期的裸 ID。
3. **执行循环零事件**：`SandboxAgent.run` 的 `on_event` 回调只在循环结束时触发
   一次（ADR-004 定案「不做逐工具心跳播报」，播报指推群）。
4. **事件管道断裂**：`pipeline._on_event` 是 `pass` 空函数，`reporter.event()`
   只打日志不落库，TaskEvent 表执行期留痕为空。

### 参照系

参照 hermes-agent 的委托体系（`delegate_task` 工具族）：其 live registry（每次
工具调用更新记录）、`action=list` 查询、progress relay（一行摘要式进度事件）、
`steer`（迭代边界投递的中途纠偏）四个机制证明了「隔离执行」与「进展可见、中途
可管」可以并存。本 ADR 将这四个机制映射到 OpenDomino 的 DB 背队列作业形态。

核心立场：**ADR-004「不做心跳播报」的决策不变**——本 ADR 只加「被动可查」与
「落库留痕」，不往群里推执行期通知；群通知仍只有 ADR-004 定的四类。

## 核心决议

共五组决议：事件留痕（打通落库）、进展查询（task_op=status）、上下文活化
（状态段查库）、中途管控（steer/cancel）、管控投递语义（迭代边界）。

链路总览：执行循环每次工具调用后产出一条一行摘要事件落 TaskEvent 表并刷新任务
活跃时间戳；用户问进展时 Router 识别 `task_op` 动作查库格式化回群；每条消息的
上下文装配把 `[进行中任务]` 段换成查库的实时状态行；steer/cancel 经内存控制面
在执行循环的迭代边界生效，cancel 落 `CANCELED` 终态（状态机既有枚举首次启用）。

### 事件留痕决议：每次工具调用落一行摘要

打通 `on_event` 管道：`SandboxAgent.run` 在**每次工具调用完成后**触发回调，
`TaskPipeline` 接到 `tasks.append_event` 真正落库（stage=execute、
kind=tool_summary），同时 `TaskService.append_event` 顺带刷新该任务的
`updated_at`（活跃时间戳，供停滞判断复用，不新增列）。

- 事件内容学 hermes 的 relay：**一行摘要**（工具名 + 截断到 80 字符的参数
  预览），不存工具输出全文——TaskEvent 是进展线索不是执行日志，全文查容器
  留痕或 transcript 是后续独立需求。
- 失败事件（错误信封返回）kind=tool_error，摘要带「失败」标记；查询侧对
  连续失败给出「可能停滞」提示。
- 事件量上界 = 步数预算（默认 40 条/任务），SQLite 无压力；不做节流。
- `reporter.event()` 保持只打日志不推群（ADR-004 不播报不变）。

### 进展查询决议：task_op 动作（status）

Router 新增第五个路由动作 `task_op`（学 hermes 单工具 action 枚举思路：
一个动作 + op 参数，不为每种意图加一个动作）。`op=status` 时 ConsoleAgent
直接查 `TaskService`，不经 LLM 二次调用：

- 无参数：`list_by_chat(chat_id)` 列本群最近 10 个任务的状态
  （状态 / 分支 / 已耗时 / verify_result）；
- 带任务号（`#a1b2c3d4` 或 8 位 hex）：精确到单个任务，附最近 5 条
  TaskEvent（「最近在做什么」）；
- **停滞提示**：status=running 且 `updated_at` 距今超过
  `sandbox_stale_minutes`（新配置，默认 10 分钟）→ 回复中标注
  「⚠️ 超过 N 分钟无新进展，可能停滞」；
- 查询动作由 LLM 在路由时识别（说法开放：「#a1b2 怎么样了」「现在有啥
  任务在跑」），执行是确定性查库，不生成不编造。

### 上下文活化决议：状态段查库替代裸 ID

`ContextAssembler._state_section` 的 `[进行中任务]` 段改为查库渲染：装配器
新增可选的 `task_status_fn` 注入（保持装配器无 I/O 依赖的既有设计——由
ConsoleAgent 持有 TaskService 引用传入 async 查询函数），每条消息装配时把
`active_task_ids` 中的任务渲染为「#id 状态 已耗时 最近一步」，已终结的任务
从展示中剔除。`active_task_ids` 的清理策略维持惰性：不主动删，渲染时过滤
（Session 持久化里留着 ID 作为历史索引，代价为零）。

### 中途管控决议：TaskControl + steer/cancel

新增 `TaskControl`（sandbox 层新模块）：进程内控制面，
`dict[task_id → asyncio.Queue]` 结构，三个操作：

- `steer(task_id, text) -> bool`：running 任务队一条纠偏文本，返回 False
  表示任务不在跑（含已结束，调用方回群说明）；
- `request_cancel(task_id) -> bool`：置取消标志，执行循环在迭代边界感知；
- 管线领任务时 `control.bind(task_id)` 创建队列，finally 里 `unbind`。

ConsoleAgent 侧经 Router 扩展 `task_op` 的 `op=steer|cancel` 分支确定性执行：
校验任务属于本群（`chat_id` 匹配）+ 状态为 running 才接受，接受即回群
「已转达，将在任务下一步生效」（steer）/「将在下一步停止」（cancel）。
pending（排队中）任务不支持 steer；「改需求」场景引导 cancel 后重提
（一期不做 pending 改写 instruction）。

cancel 生效语义：迭代边界发现取消标志 → 跳出循环 → 复用 `_force_summary`
对已有进展做保底汇总 → `finish(CANCELED)` 落终态 → reporter 通知群
（reporter 已有 canceled 分支，首次启用）。cancel 是协作式停止：当前工具
调用不打断（与 steer 同一边界检查点，边际成本低）。

### 管控投递语义决议：迭代边界 + 独立 user 消息 + missed 报告

与 hermes 对齐的三条语义：

1. **不打断当前工具**：steer/cancel 都在「本轮工具结果全部回填后、下一次
   LLM 调用前」的边界生效，run_command 跑到一半不切断；
2. **独立 user 消息投递**：steer 文本以 `[中途指示] …` 前缀作为独立 user
   消息插入对话（tool 消息后插 user 是合法序列），多次 steer 合并投递；
   hermes 是追加到 tool 结果尾部，独立消息语义更干净；
3. **missed 报告**：任务在 steer 入队与消费之间结束时，管线 finally 处
   drain 残留文本，经 reporter 回群「指示未生效，任务已结束」；接受 steer
   时回执里注明这一可能。

权限：同群成员即可 steer/cancel（任务本是群协作产物），不做创建人限制；
steer 接受回执不带 @（一期从简）。

## 权衡与不做的决定

- **不推群播报**：ADR-004 定案维持。查询 + 留痕已覆盖「想知道时查得到」；
  主动推群的价值存疑且打扰性强，若未来要做，TaskEvent 落库是数据前提。
- **不做 pending 改写**：排队任务「改需求」用 cancel + 重提覆盖，不为
  少见场景加 instruction 改写的并发语义。
- **不做跨进程控制面**：ConsoleAgent 与 SandboxAgent 同进程，内存队列即够；
  若未来拆进程，控制面迁到 DB 表或消息通道，接口形状不变。
- **hermes 的 stale 强制中断不抄**：我们已有步数/时长双预算兜底，
  停滞只提示不自动杀（避免误杀慢工具，如长测试）。

## 实施落点

- `sandbox/control.py`（新）：TaskControl；
- `sandbox/agent.py`：循环内 on_event 触发 + 边界 drain steer / 查取消；
- `sandbox/pipeline.py`：绑定控制面、事件落库、missed steer 报告、
  cancel 终态；
- `sandbox/types.py`：EventKind 增补；
- `console/router_types.py` + `console/prompts.py`：task_op 动作；
- `console/agent.py`：task_op 执行 + 装配器注入状态查询；
- `console/context.py`：状态段查库渲染；
- `config.py`：`sandbox_stale_minutes`；
- E2E：`scripts/e2e_sandbox.py` 扩展 steer/cancel/status 场景，
  产物 `data/e2e_sandbox_result.json` 增补管控记录。
