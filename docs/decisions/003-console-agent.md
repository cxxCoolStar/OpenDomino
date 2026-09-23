# ADR-003 ConsoleAgent：理解与路由

> 状态：已确认
> 日期：2026-09-23
> 前置依赖：ADR-001 协作入口层、ADR-002 三层记忆
> 关联：ADR-004 SandboxAgent、ADR-007 链路追踪

## 背景

ConsoleAgent 位于协作入口层之上，是系统的「大脑」。它接收 ChannelAdapter 归一化后的
UnifiedMessage，借助三层记忆理解问题，然后三选一：直接回答、提出澄清、委派任务。

原文定位：**理解与路由，不直接改业务代码**。快路径答疑、慢路径委派是它的两条链路。

设计输入：

- ADR-001：ChannelAdapter 提供 UnifiedMessage 输入与 reply 输出能力；
- ADR-002：MemoryService 提供 Session / KBase / 知识库查询与记忆写入治理；
- 调研参考：hermes-agent（clarify_gateway、cwd 绑定）、cumora（needs_clarification、
  conversation↔project 绑定）、fastclaw。

## 核心决议

1. **单次结构化路由，不做 Agentic 工具循环**。每条消息一次 LLM 调用，输出 JSON 决策
   （answer / clarify / delegate），上下文在调用前由装配器一次性并行组装。
   - 理由：ConsoleAgent 的检索工具只有 recall_kbase / search_knowledge / load_session 三个，
     一次并行调用即可拿全，没有「先看 A 结果再决定查 B」的强依赖链，多轮循环是把简单问题
     复杂化；行为可预测、延迟可控、便于单测。
   - 取舍：召回不足时没有自主补查能力，用「召回不足 → 澄清或委派」兜底。Agentic 循环
     放到工具多、有真实迭代依赖的 SandboxAgent（ADR-004），符合原文快慢路径分流。
2. **路由动作只有三个**：answer / clarify / delegate。寒暄、+1 等轻量消息归入 answer
   （简短回应），不设 ignore 动作，避免静默丢弃（无人值守不等于无人负责）。
3. **pending 待澄清判定合并进路由调用**。不参考 hermes 的规则匹配（`_coerce_text_response`，
   会把无关消息/表情误判为回答），也不做独立的前置分类调用。借鉴 cumora「主脑读完整上下文
   自行理解」的思路：把 pending 原文注入路由 Prompt，由同一次 LLM 调用同时输出路由决策与
   `pending_resolution`（answered / new_question / cancel）。
4. **任务委派即创建 Task 后异步执行**。ConsoleAgent 调用 `TaskService.create` 后立即回复群
   「已受理」，不等待、不轮询任务结果；任务状态变化由 SandboxAgent 经 ChannelRegistry 直接
   回传来源群（见「与 SandboxAgent 通信」）。
5. **每群串行、跨群并行**。同一 chat_id 的消息用 asyncio.Lock 串行处理，避免会话状态竞争与
   LLM 调用交错；不同群互不阻塞。
6. **回答必须带来源标记**。正式文档 / 团队经验 / 当前会话 / 模型知识四级标记，与 ADR-002 的
   可信度规则一致，不允许把经验伪装成正式事实。

## 消息处理管线

```text
UnifiedMessage（来自 ADR-001 ChannelAdapter）
  ↓
1. 会话恢复     MemoryService.load_session(chat_id) → SessionContext
  ↓
2. 追加消息     本条消息写入 Session 历史（滚动窗口）
  ↓
3. 上下文装配   并行执行 recall_kbase + search_knowledge；
  ↓             注入 Session 窗口、群内进行中任务、群关联仓库、pending 原文
  ↓
4. 路由决策     Router.route(prompt) → Decision（一次 LLM 调用，JSON 输出）
  ↓             同次调用判定 pending_resolution（回答 / 新问题 / 取消）
  ↓
5. 动作执行     answer   → channel.reply（带来源标记）
  ↓             clarify  → 保存 pending_clarification → reply 澄清问题
  ↓             delegate → TaskService.create → reply 任务确认（异步执行）
  ↓
6. 会话持久化   保存路由结果、本次引用、pending 状态、关联任务 ID
```

`/沉淀` 命令不进入路由，直接触发记忆提炼流程（见「记忆提炼触发」）。

## 路由决策

### 决策输出 Schema

```json
{
  "action": "answer | clarify | delegate",
  "answer": "带来源标记的回答（action=answer 时）",
  "clarify_question": "澄清问题（action=clarify 时）",
  "task": {
    "title": "任务标题（action=delegate 时）",
    "instruction": "自包含任务指令：原问题 + 澄清结论 + 相关引用",
    "repo": "目标仓库名"
  },
  "pending_resolution": "answered | new_question | cancel | none",
  "correction_detected": false
}
```

- `instruction` 必须自包含：脱离群聊也能执行，禁止出现「上面提到的」「那个 bug」等指代。
- `correction_detected=true` 表示用户明确纠正了 Agent 输出的知识，触发记忆提炼。

### 动作触发条件

| 动作 | 触发条件 | 典型例子 |
|---|---|---|
| answer | 三层记忆与模型知识足以支撑可信回答 | 「我们的发布流程是什么」→ L3 命中 |
| clarify | 缺少必要参数，或多个候选解释无法抉择 | 「帮我改下那个 bug」缺仓库与现象 |
| delegate | 需要代码现状、复现、修改、构建、测试等 Sandbox 能力 | 「帮我在 xx 仓库加个接口」 |

路由 Prompt 内置三条护栏：信息不足宁澄清不编造；涉及代码现状必须委派不凭记忆回答；
团队来源（L2/L3）与模型知识冲突时以团队来源为准并指出冲突。

## 上下文装配

| 内容 | 来源 | 预算 |
|---|---|---|
| 最近消息窗口 | L1 Session | 最近 20 条，单条截断 500 字符 |
| KBase 条目 | L2 | top 5（仅 confirmed），title + statement + rationale |
| 知识库 chunks | L3 | top 8，标题路径 + chunk 内容 |
| 群内任务状态 | Task | 进行中任务的 id / 标题 / 状态 |
| 群关联仓库 | 群仓库绑定表 | 默认仓库名 + 描述 |
| pending 原文 | Session | 待澄清问题 / 待确认记忆条目 |

- 总预算 `CONSOLE_MAX_CONTEXT_CHARS`（默认 12000 字符）。超限先丢弃最早的会话消息，
  再减少知识库条目数；pending 与任务状态不丢弃。
- 每条召回内容标注来源层级，模型不得混淆可信度（ADR-002）。

## 委派时的仓库选择：群关联仓库

采用「**群关联仓库**」为主，静态清单作备选（参考 cumora 的 conversation↔project 绑定与
hermes 的会话 pin cwd，均不做「模型从清单猜」）：

- 维护「群 ↔ 默认仓库」绑定表（数据库存储，管理接口/命令配置）；
- 路由时把该群的默认仓库注入上下文，委派默认落到绑定仓库；
- 用户消息明确指定了其他仓库时，以用户指定为准；
- 一群需要多仓库时，提供静态仓库清单（`REPO_REGISTRY`）供模型在用户意图明确时选择，
  意图不明确则 clarify 确认仓库，不臆测。

## 来源标记

| 标记 | 含义 | 示例 |
|---|---|---|
| 【正式文档】 | L3 知识库 | 【正式文档】《发布流程》v3 |
| 【团队经验】 | L2 KBase | 【团队经验】i18n key 命名约定 |
| 【当前会话】 | L1 Session | 【当前会话】你刚才说的仓库 |
| 【模型知识】 | 模型自身知识，未经团队佐证 | 【模型知识】Python 3.12 的新特性 |

回答末尾附「依据」小节列出全部引用；涉及团队事实时不得只靠模型知识作答。

## 与 SandboxAgent 通信

两 Agent 是同进程内的两个独立服务，经**持久化 Task + 内存队列**异步通信，不走网络 /
CLI 子进程（hermes bot relay 那套过重），也不走共享内存变量（重启丢失）。

**委派（ConsoleAgent → SandboxAgent，控制流）**：

1. ConsoleAgent 只调 `TaskService.create(task_draft)` 抽象接口，不直接调用 SandboxAgent；
2. Task 先**落库**（status=pending，含自包含 instruction、repo、chat_id 回传地址、session_id、
   trace_id），再放进内存 asyncio.Queue 通知 Executor；
3. 落库在前、通知在后：服务重启后 pending 任务从库里恢复重跑（呼应 ADR-002 可恢复原则）；
4. fire-and-forget：拿到 task_id 即回复群「已受理」，不等结果（借 hermes bot 异步语义）。

**回传（SandboxAgent → 群，数据流，不绕道 ConsoleAgent）**：

- SandboxAgent 跑完后经 ChannelRegistry（chat_id → ChannelAdapter）直接 reply 到来源群；
- ConsoleAgent 不参与回传链路，它作为普通会话上下文看到这条消息；
- 唯一例外：任务完成后的 KBase 沉淀（ADR-002 触发点 3），SandboxAgent 调共享的
  MemoryService 提炼管线产出草稿 → pending_memory_confirm → 群内确认。走共享服务，
  非私有通道。

不采用的方式：hermes CLI 子进程/relay（过重）、同步 RPC（卡死群聊）、共享内存直传
（不可恢复）、外部 MQ（单实例过度设计，第一阶段用 asyncio.Queue + DB 恢复）。

## 任务委派与回传

### Task 概念模型（完整模型见 ADR-004）

```text
Task
- id / title / instruction   自包含任务指令
- repo                       目标仓库（来自群仓库绑定）
- created_by / chat_id       发起人与来源群（回传寻址）
- session_id / trace_id      关联会话与链路追踪 ID
- status                     pending → running → review → done | failed | canceled
- result                     结论摘要、commit 列表、产物链接
```

### 回传机制

执行侧在任务状态变化（受理 / 开始 / 待评审 / 终态）时，按 `task.chat_id` 找到对应 Channel
调用 `reply`。ConsoleAgent 只负责委派时刻的确认回复；不设轮询与主动进度播报。

## 记忆提炼触发

对应 ADR-002 的三个保守触发点，ConsoleAgent 负责前两个入口：

1. **用户纠正**：`correction_detected=true` 时，先正常回复，再后台 LLM 提炼 → 草稿 →
   展示并进入 `pending_memory_confirm`；
2. **显式沉淀**：消息以 `/沉淀` 开头时跳过路由，对最近会话窗口执行提炼；
3. **任务完成提炼**：由 SandboxAgent 在任务 done 后发起，复用同一提炼管线。

提炼流程、确认治理、写入规则全部遵循 ADR-002，ConsoleAgent 只做编排与展示。

## pending 状态机

| 状态 | 写入时机 | 消费方式 | 退出条件 |
|---|---|---|---|
| `pending_clarification` | 路由为 clarify 后 | 同一次路由调用输出 resolution | answered / new_question / cancel / 超时被动清除 |
| `pending_memory_confirm` | 记忆草稿展示后 | 关键词匹配 | 「确认」→ confirm；「取消」→ 丢弃并反馈未保存 |

- 超时 `CONSOLE_CLARIFY_TTL_MINUTES`（默认 30 分钟），下一条消息到达时被动检查，不主动推送。
- 记忆确认关键词未命中时视为普通消息，pending 保留并提示可用「/确认 /取消」。

## 链路追踪（详见 ADR-007）

每条入群消息生成确定性 `trace_id`（`create_trace_id(seed=msg-id)`），路由、记忆召回、
委派都埋 span；委派时把 trace_id 存入 Task，SandboxAgent 执行时经 `trace_context` 续接同一
trace，解决异步断链。全量 LLM prompt/输出经 Langfuse 记录。

## 并发与失败处理

- **群级锁**：`Dict[chat_id, asyncio.Lock]`，同群消息串行进管线；
- **LLM 输出解析失败**：带错误信息重试 1 次；仍失败则降级为原样回复并记录日志；
- **MemoryService 某层异常**：跳过该层并在 Prompt 中注明「该层不可用」，不阻塞整体回答；
- **Task 创建失败**：reply 明确报错，不留「已受理」假象；
- **持久化时序**：先完成路由与动作决策，最后统一写 Session，避免中途异常留下脏状态。

## 模块结构

```
backend/app/console/
├── agent.py        # ConsoleAgent 主管线（步骤 1-6 编排、群级锁）
├── context.py      # 上下文装配器（并行召回、预算控制、来源标注）
├── router.py       # 路由器（LLM 调用、JSON 解析、重试与降级）
├── prompts.py      # 路由 / 提炼展示提示词模板
├── pending.py      # pending 状态机（澄清、记忆确认、超时）
├── delegation.py   # 任务委派（TaskService 客户端、群仓库绑定读取）
└── repo_binding.py # 群 ↔ 仓库绑定表
```

Task 数据模型与执行侧接口归 ADR-004，ConsoleAgent 依赖其抽象。

## 配置契约

```
CONSOLE_MODEL=                    # 路由与回答使用的模型
CONSOLE_CONTEXT_WINDOW=20         # 进入 Prompt 的最近消息条数
CONSOLE_KBASE_LIMIT=5             # KBase 召回条数
CONSOLE_KNOWLEDGE_LIMIT=8         # 知识库召回条数
CONSOLE_MAX_CONTEXT_CHARS=12000   # 上下文总预算（字符）
CONSOLE_CLARIFY_TTL_MINUTES=30    # 待澄清过期时间
REPO_REGISTRY=                    # 备选静态仓库清单，name=url|desc，分号分隔
```

## 非目标（第一阶段）

- 多轮 Agentic 工具调用循环（放到 SandboxAgent）；
- 任务拆分与多任务编排（一次委派 = 一个 Task）；
- 主动推送（任务回传由执行侧触发）；
- 权限审批流（沿用通道层白名单）；
- 多模型分级路由（路由与回答使用同一模型）。

## 参考实现

- Domino 原文：ConsoleAgent 理解与路由定位、快慢路径分流。
- hermes-agent：`tools/clarify_gateway.py`（规则匹配的坑）、`agent/runtime_cwd.py`（cwd 绑定）。
- cumora：`server/src/agents/turn.ts`（needs_clarification 非终态）、
  `server/src/db/migrate.ts`（conversations.project_id 群↔项目绑定）。
- ADR-001 / ADR-002 / ADR-004 / ADR-007。
