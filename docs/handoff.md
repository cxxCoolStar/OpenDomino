# OpenDomino 开发交接

> 更新日期：2026-09-23
> 当前阶段：基础问答闭环已实现；真实钉钉 Stream 已连接；真实 LLM 请求被上游 429 限流阻塞。

## 项目目标

OpenDomino 复刻「从钉群提问到任务交付」的团队级 Agent 基础设施：钉钉群作为唯一消息入口，ConsoleAgent 负责理解、检索和路由，SandboxAgent 负责进入真实仓库执行和交付。Web 后期只做管理后台，不做聊天入口。

## 当前实现

后端位于 `backend/`，基于 Python 3.12、FastAPI、SQLAlchemy async/SQLite、OpenAI-compatible SDK 和 DingTalk Stream SDK。

- **钉钉入口**：统一消息类型、消息解析、仅群聊、@ 门控、群/用户白名单、消息去重、session webhook 回复。SDK 回调已按真实 `dingtalk-stream` API 接入，并在收到消息后快速 ACK，再异步处理。
- **L1 Session**：SQLite 持久化群消息、pending 状态、活跃任务 ID 和最近路由；SQLite 数据目录会在初始化时自动创建。
- **L2 KBase 基础**：草稿/确认记录可落库，普通召回只返回 confirmed 条目；当前召回是简单关键词匹配，不是 ADR 目标中的向量检索。
- **L3 知识库**：接口已定义，当前返回空结果，尚未接文档导入、FTS5 或 ChromaDB。
- **ConsoleAgent**：单次结构化 JSON 路由；answer / clarify / delegate 三种动作；同群串行锁；pending 澄清随 Session 持久化；LLM JSON 解析失败重试一次后降级回复。
- **ToolRegistry**：注册、toolset 过滤、check_fn、参数类型强转、统一错误与结果截断已有基础实现；尚未注册沙箱实际工具，也未接入执行循环。
- **运行入口**：`backend/app/main.py` 启动 FastAPI、SQLite 与钉钉 Stream；提供 `/health` 和 `/ready`。
- **尚未实施**：SandboxAgent、Docker 容器池与任务队列、仓库绑定管理、SKILL loader/目录、Langfuse 埋点、管理后台。

## 验证状态

- 最近测试：`cd backend && python -m pytest tests/ -q`，**37 passed**。
- Python 编译检查通过，FastAPI 可导入，健康路由注册。
- 钉钉凭证可创建 Stream 客户端，`open_connection()` 握手成功；Uvicorn 日志出现钉钉 endpoint，`/health` 返回 `{"status":"ok"}`，`/ready` 返回 `{"status":"ready","channel":"dingtalk-stream"}`。
- 真实 LLM 探测未通过：上游先返回 Cloudflare 502，之后返回 HTTP 429，提示所有可用账号当前都被限流。完整的「钉钉群消息 → LLM → 钉钉回复」尚未验证成功。
- 本地 Uvicorn 服务在最近一次验证后仍运行于 `http://127.0.0.1:8000`。启动日志位于当前会话工具输出，不作为项目交接依赖；需要时先检查端口/进程再启动第二份实例。

## 已确认的设计决策

完整背景见 `docs/decisions/`：

- ADR-001：仅钉钉群聊作为入口；不做私聊和 Web 群聊。Web 后期只作为管理后台。
- ADR-002：L1 群级 Session、L2 团队 KBase、L3 正式知识库分层；团队记忆写入需要确认。
- ADR-003：ConsoleAgent 单次结构化路由；pending 判定和路由合并；群关联仓库；Task 持久化后异步执行。
- ADR-004：SandboxAgent 使用受控 Agentic 循环；容器按仓库池化，任务工作区独占并重置；强制汇总、反死循环和安全护栏；TaskEvent 给团队看、Langfuse trace 给开发者排查。
- ADR-005：SKILL 由模型自主选择加载；系统提示只放技能目录，按需加载全文并预检依赖。
- ADR-006：工具参考 hermes-agent，普通 Python handler + schema 注册到统一 ToolRegistry；ConsoleAgent 当前无工具循环。
- ADR-007：采用 Langfuse，trace_id 随 Task 传递并跨异步执行续接；TaskEvent 与 trace 各司其职。

## 下一步开发顺序

### 1. 先完成真实钉钉 + LLM 闭环验证

这是当前最高优先级，不要先扩展沙箱。LLM 服务解除限流后，在已授权钉钉群 @机器人发一条无敏感内容的测试问题，确认：

1. Stream 收到并 ACK 消息；
2. 群聊/@/白名单门控正确；
3. ConsoleAgent 调用配置模型并解析结构化决策；
4. 回复使用触发消息对应的 `session_webhook`；
5. SQLite 保存 user/assistant 消息；
6. 重启后群 Session 能恢复。

如果持续 429，检查 LLM 服务提供方的限流/账户配额与模型配置；不要把该问题误判成钉钉或本地 SDK 故障。真实消息测试前避免在共享群发送敏感信息。

### 2. 修复 ConsoleAgent 的已知正确性缺口

真实闭环前后都应优先处理：

- **回复 webhook 竞态**：`DingTalkChannel._fresh_webhook()` 优先取按 chat_id 缓存值，再取当前消息 fallback；同群并发时可能用到另一条消息 webhook。应改成当前消息 webhook 优先，缓存仅作必要 fallback，或完全移除缓存路径。
- **Session 并发安全**：同群锁仅在单个 ConsoleAgent 进程内有效；多 worker 部署会有跨进程竞争。第一阶段可明确单 worker 约束，未来再做数据库级并发控制。
- **pending 超时未接入**：`ConsoleAgent._expire_pending()` 已写但 pipeline 尚未调用；需在路由前检查并持久化清理。
- **pending_resolution 边界**：new_question/cancel/answered 的状态转移需测试，确认新话题不会意外消费或保留错误 pending。
- **群仓库绑定**：目前只是进程内字典，未持久化，也没有管理入口；委派前还不能依赖它作为可靠 repo 来源。
- **解析降级行为**：无效 JSON 两次后把模型原始文本直接答给用户，可能缺少来源标记；需要决定安全的降级话术并补测试。

### 3. 实现持久化任务骨架，再做 SandboxAgent

先建立 Task/TaskEvent 数据模型、TaskService、按仓库串行任务队列和失败恢复语义，再接 Docker 容器池。ADR-003 的 ConsoleAgent 目前没有真实 TaskService 注入，因此 delegate 只会返回「SandboxAgent 未接入」或依赖测试 fake。

SandboxAgent 按 ADR-004 实施：仓库维度复用容器、每 Task 独占工作区并重置；任务完成 push 独立分支，不自动合并；工具执行使用硬超时、进程组清理、输出上限和危险命令防护。安全边界应优先于扩大可执行能力。

### 4. 实现 SKILL，再接入 Langfuse

- SKILL：先做文件发现、frontmatter 校验、渐进式目录和 `load_skill`，再添加最小示例技能和测试。
- Langfuse：部署自托管服务前确认 Docker Compose 配置与资源需求；Python SDK 初始化和采样关闭路径先测试，再在 LLM generation、记忆召回、委派、工具执行等节点埋点。Task 的 trace_id 必须持久化以续接异步沙箱链路。

### 5. 再补完整记忆检索与管理后台

KBase 语义检索、L3 文档导入/版本/FTS5/向量检索，以及 Web 管理后台都在已确认设计中，但目前不应先于真实入口闭环和任务交付骨架。

## 启动与测试

```bash
cd backend
python -m pytest tests/ -q
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

服务需要 `backend/.env` 中配置 OpenAI-compatible LLM 和 DingTalk Stream 凭证。`.env` 不应提交到版本库。示例见 `backend/.env.example`。

## 交接边界与注意事项

- 当前完成的是「钉钉入口 + ConsoleAgent 答疑骨架」，不是完整 Domino 交付闭环。
- 不要把 ADR 文档中已写的模块结构当成已实现代码；SandboxAgent、SKILL、Langfuse、L3 检索仍未实现。
- 不要宣称真实群聊回复已验证：钉钉连接成功，但 LLM 上游 429 阻止了端到端验证。
- 不要打印或提交 `.env`、API key、钉钉 secret、webhook 等敏感值。
- 开发继续遵循 `AGENTS.md`：模块逐个讨论确认，发现需求与原文定位偏差时先指出再实施。
