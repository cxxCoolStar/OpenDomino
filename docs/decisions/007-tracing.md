# ADR-007 链路追踪：Langfuse

> 状态：已确认
> 日期：2026-09-23
> 前置依赖：ADR-003 ConsoleAgent、ADR-004 SandboxAgent
> 调研参考：Langfuse 官方文档（Python SDK v3+/v4，基于 OpenTelemetry）

## 背景

OpenDomino 的链路长（群消息 → 路由 → 三层记忆 → 委派 → 沙箱执行 → 回传）、分支多
（answer / clarify / delegate 三条岔路）。用户需要一个指令下去能看到整个执行流程。

这不是普通运维日志，而是**链路追踪**：还原「一条群消息最后怎么样了」，包括模型当时想了
什么、调了什么工具、结果如何、耗时多少。日志横跨所有模块，单独成 ADR，不做附属。

## 核心决议

1. **采用开源 Langfuse，不自研 Tracer，不上 OpenTelemetry 全家桶样板**。Langfuse Python
   SDK（v3+，基于 OTel）自带埋点、全量 LLM 记录、trace_id 贯穿与可视化 UI，把精力留给
   核心业务。
2. **三类记录分离**（这是本 ADR 最关键的一条）：
   - **TaskEvent**（ADR-004）：给**用户/团队**的业务留痕，落业务库，是原文「可分享、可交接、
     可追溯」的沙箱工作记录——**产品，长期留存**；
   - **Langfuse trace**：给**开发者**的技术观测（prompt/token/耗时），进 Langfuse——
     **耗材，可调可清**；
   - **系统日志**：服务启动/异常/错误堆栈，走标准 logging。
   - 三者不混。TaskEvent 关联 trace_id，开发者想深挖时从业务记录跳进技术链路。
3. **全量 LLM 记录**：完整 prompt、模型原始输出、token 用量、耗时、模型名，经
   `langfuse.openai` 包装的 client 自动采集，用于调 prompt 与复盘。隐私字段第一阶段原样
   记录 + 文件访问受限，脱敏规则留作后续。
4. **trace_id 贯穿 + 异步断链续接**：解决委派是异步、trace 会在 Task 处断开的难题（见下）。
5. **方案 A 分工（不与 TaskEvent 合并）**：沙箱段既写精简 TaskEvent（业务语言）又埋
   Langfuse span（技术细节），两处分别埋点、带同一 trace_id。业务资产不寄生在调试工具里。

## 为什么 Langfuse（而非 Phoenix / 自研）

| 候选 | 结论 |
|---|---|
| **Langfuse**（选） | 核心 MIT，LLM 工程平台：trace + prompt 管理 + 成本看板 + 评估。Python SDK 装饰器/上下文管理器埋点轻。本地 docker compose 可起。 |
| Phoenix | 也合适且更轻（单容器、OTel 原生），但偏调试/评估，无 prompt 管理/成本看板成品。 |
| 自研 Tracer | 重复造轮子，违背「精力放核心业务」。 |
| OTel + Jaeger 全家桶 | 标准但样板代码重，第一阶段过重。 |

选 Langfuse 的额外收益：prompt 版本管理、成本看板是后续能用上的成品能力。

## trace_id 贯穿与异步断链续接

委派是异步的：ConsoleAgent 派任务后，SandboxAgent 几秒到几分钟才执行，trace 会断。
核心解法是**确定性 trace_id + 显式续接**：

```python
from langfuse import get_client
from langfuse.openai import openai

langfuse = get_client()                      # 单例，读 LANGFUSE_* 环境变量
client = openai.OpenAI(base_url=...)         # 包装后，LLM 调用自动记 generation

#### 三个链路场景

① 群消息进来：用消息 ID 做种子，生成确定性 trace_id
trace_id = langfuse.create_trace_id(seed=f"msg-{message.message_id}")

# ② ConsoleAgent：开根 span，路由/召回/委派都是子 span
with langfuse.start_as_current_observation(
    as_type="span", name="console-route",
    trace_context={"trace_id": trace_id},
):
    ...                                       # 记忆召回、路由 LLM 调用自动成为子 span
    task.trace_id = trace_id                  # 委派时把 trace_id 存进 Task（关键）

# ③ SandboxAgent 异步执行：从 Task 取同一 trace_id 续接
with langfuse.start_as_current_observation(
    as_type="span", name="sandbox-execute",
    trace_context={"trace_id": task.trace_id},
):
    ...                                       # 每轮工具调用、LLM 调用、验证都是同一 trace 的子 span
```

- `create_trace_id(seed=...)` 确定性生成（同种子同 ID）；
- `trace_context={"trace_id": ...}` 显式续接，把异步断裂无缝拼上；
- 效果：在 Langfuse UI 用这条 trace_id 一查，能从「群消息」一路看到「容器里跑 pytest」。

## span 埋点规范

埋点很轻，两类：

- **过程性节点**用 `@observe()` 装饰器：路由函数、记忆召回、工具执行、沙箱各阶段——
  自动记输入输出与耗时；
- **LLM 调用**靠 `langfuse.openai` client 自动埋点，或手动包 `as_type="generation"` 记
  model/usage/cost。

| 链路节点 | span 名 | 类型 |
|---|---|---|
| ConsoleAgent 路由 | `console-route` | span |
| 记忆召回（L1/L2/L3） | `recall-kbase` / `search-knowledge` | span |
| 路由 LLM 调用 | （自动） | generation |
| 委派创建 Task | `delegate` | span |
| SandboxAgent 执行 | `sandbox-execute` | span |
| 沙箱每轮工具执行 | `tool:{name}` | span |
| 沙箱 LLM 调用 | （自动） | generation |
| AI 验证 | `verify` | generation |
| KBase 提炼 | `memory-extract` | generation |

分支可视化：answer / clarify / delegate 三条岔路在 trace 树里是不同子 span，一眼看清走了
哪条——这正是「链路长、分支多」诉求的解法。

## 部署

第一阶段本地 `docker compose` 起一套：Langfuse Web（UI+API）+ Worker + Postgres +
ClickHouse + Redis。产出三个环境变量：

```
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_BASE_URL=http://localhost:3000
```

生产环境再上 K8s（Helm），第一阶段不管。自托管要求服务端版本 ≥ 3.63.0。

## 模块结构

```
backend/app/tracing/
├── client.py       # Langfuse 单例 + langfuse.openai 包装 client
├── trace.py        # create_trace_id(seed)、trace_context 续接辅助
└── decorators.py   # 项目统一的 @observed(name) 埋点装饰器封装
```

各模块（console / sandbox / memory）注入 tracing.client，按上表规范埋点。

## 配置契约

```
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_BASE_URL=http://localhost:3000
LANGFUSE_ENABLED=true        # false 时埋点为空操作，本地可关
TRACING_SAMPLE_RATE=1.0      # 采样率，第一阶段 1.0 全量
```

## 非目标（第一阶段）

- prompt 版本管理 / 成本看板 / 评估打分的深度使用（Langfuse 自带，先用 trace）；
- 隐私脱敏规则（原样记录 + 访问受限）；
- 采样降载（第一阶段全量）；
- 把 TaskEvent 的业务留痕合并进 Langfuse（明确不合并）；
- 接入 Jaeger / Grafana 等其他观测后端。

## 参考实现

- Langfuse 文档：Python SDK instrumentation、`create_trace_id` / `trace_context`（Trace IDs &
  Distributed Tracing）、`langfuse.openai` 集成。
- ADR-003（trace_id 随 Task 传递）、ADR-004（TaskEvent 与 trace 分工）。
