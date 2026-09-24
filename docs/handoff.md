# OpenDomino 开发交接

> 更新日期：2026-09-23（第二次更新）
> 当前阶段：**真实钉钉 → LLM → 钉钉回复闭环已验证通过**；ADR-002 Phase 0（记忆确认流 + 上下文装配重构 + 并发修复）已实现并上线；**SandboxAgent（ADR-004）全模块已落地（2026-09-23，Fake 测试全绿，Docker 集成待真机验证）**。

## 项目目标

OpenDomino 复刻「从钉群提问到任务交付」的团队级 Agent 基础设施：钉钉群作为唯一消息入口，ConsoleAgent 负责理解、检索和路由，SandboxAgent 负责进入真实仓库执行和交付。Web 后期只做管理后台，不做聊天入口。

## 当前实现

后端主链路按「入口 → 记忆 → Agent → 沙箱」四层组织，位于 `backend/app/` 下。以下按层列出当前实现状态与关键能力，接手前请先通读本节再对照 ADR 细读。

### 入口与会话

后端位于 `backend/`，基于 Python 3.12、FastAPI、SQLAlchemy async/SQLite、OpenAI-compatible SDK 和 DingTalk Stream SDK。

- **钉钉入口**：统一消息类型、消息解析、仅群聊、@ 门控、群/用户白名单、消息去重、session webhook 回复。SDK 回调已按真实 `dingtalk-stream` API 接入，并在收到消息后快速 ACK，再异步处理。
- **L1 Session**：SQLite 持久化群消息、pending 状态、活跃任务 ID 和最近路由；SQLite 数据目录会在初始化时自动创建。
### 记忆与知识库

- **L2 KBase 基础**：草稿/确认记录可落库，普通召回只返回 confirmed 条目；当前召回是简单关键词匹配，不是 ADR 目标中的向量检索。
- **L3 知识库**：接口已定义，当前返回空结果，尚未接文档导入、FTS5 或 ChromaDB。
### Agent 与工具

- **ConsoleAgent**：单次结构化 JSON 路由；answer / clarify / delegate 三种动作；同群串行锁；pending 澄清随 Session 持久化；LLM JSON 解析失败重试一次后降级回复。
- **ToolRegistry**：注册、toolset 过滤、check_fn、参数类型强转、统一错误与结果截断已有基础实现；尚未注册沙箱实际工具，也未接入执行循环。
### 沙箱与运行入口

- **SandboxAgent（ADR-004，2026-09-23 落地）**：`app/sandbox/` 全套模块已实现——
  - `types.py` 状态机 pending→running→review→done|failed|canceled；`storage.py` Task/TaskEvent 两表（TaskService：create 落库即返回、事件 seq 自增、重启恢复 pending 续跑/running 标失败）；
  - `executor.py` 全局并发信号量 + 按仓库串行队列（worker 崩溃不退出）；`pipeline.py` run_task 管线（领取→取容器→建分支→执行循环→提交 push→预评审→待评审回传；异常转 failed 并归还容器）；
  - `agent.py` 原生 function calling 执行循环 + 反死循环三重护栏（同调用 3 次注入警告/同工具连败 2 次摘工具/整轮全败 3 轮摘工具）+ 预算耗尽强制汇总 + 确定性历史裁剪；
### 容器与回传

- `container.py` 按 repo 池化容器（容器内 clone 规避 Windows bind-mount、setsid+marker+二次 exec 击杀进程组、idle TTL 驱逐）；`git_ops.py` 分支/提交/push/log/diff 封装；
  - `tools.py` 8 个工具（list_dir/read_file/grep/write_file/apply_patch/run_command/git_diff/git_log）+ 危险命令拦截 + PAT 输出脱敏；`repo_config.py` REPO_{NAME}_* 环境变量解析与凭据收集；
  - **文件工具 vendor hermes ShellFileOperations（MIT，`_vendor/` + `hermes_adapter.py`）**：原子写（mktemp+mv）、模糊补丁 + V4A 格式、rg→grep 降级搜索、行号分页读；`DockerExecutor` 经 adapter 提供 execute() 契约；已修 exec 单引号击穿（'''转义）与 127 分层误伤；
  - **双任务类型（原文主路径对齐）**：ConsoleAgent 路由判定 `task.type`（investigate=排查给结论，默认 / modify=变更代码），pipeline 分流：investigate 只读路径（不建分支不提交，结论即交付物，verify_result=conclusion）；modify 交付判定为 HEAD 领先 base_ref（兼容模型自行 commit）；
  - **E2E 已验证（scripts/e2e_sandbox.py）**：investigate 与 modify 双用例全绿——modify 真实 push `domino/task-b37c141b` 分支到 GitHub（产物 data/e2e_sandbox_result.json）；修复过程关键点：执行循环须先回填 assistant(tool_calls) 再回填 tool 消息（hermes 同构，否则上游 400）；容器 mkdir 必须先于 workdir exec；exec 错误分层（125/126/127 → ExecError）。
  - `reporter.py` 四类事件回传 + `channels/registry.py` 寻址；DingTalkChannel 新增机器人主动发消息 API（主通道，解决 session_webhook 5 分钟过期）+ webhook 兜底；
  - `main.py` 已装配：TaskService→TaskExecutor→TaskPipeline→ConsoleAgent.delegate 真实接线（「SandboxAgent 未接入」占位已移除）。
  - 测试 `tests/test_sandbox.py` 13 个（Fake 全覆盖，不依赖 Docker）；真实 Docker 集成待本地验证。
### 运行入口

- **运行入口**：`backend/app/main.py` 启动 FastAPI、SQLite 与钉钉 Stream；提供 `/health` 和 `/ready`。
### 尚未实施

- **尚未实施**：verify.py 独立 AI 验证与回炉（一期以预评审替代）、SKILL loader/目录、Langfuse 埋点、管理后台。

## 验证状态

- 最近测试：`cd backend && python -m pytest tests/ -q`，**63 passed**（含确认流 12 个、装配/并发 9 个新测试）。
- **真实闭环已验证**（2026-09-23）：在真实钉钉群 @机器人 提问，ConsoleAgent 调用配置 LLM 并以 answer 动作正确回复，SQLite 已落库 user/assistant 消息（chat `cidYK5nEw+...`，见 `data/opendomino.db`）。
- LLM 上游 429 已解除（探测成功）。
- 服务运行于 `http://127.0.0.1:8000`，Stream 握手成功。
- `init()` 已加轻量列迁移（`pending_memory_json`、`summary`），旧库自动升级，`create_all` 不会给已有表加列的问题已处理。

## 已确认的设计决策

完整背景见 `docs/decisions/`：

- ADR-001：仅钉钉群聊作为入口；不做私聊和 Web 群聊。Web 后期只作为管理后台。
- ADR-002：L1 群级 Session、L2 团队 KBase、L3 正式知识库分层；团队记忆写入需要确认。**Phase 0 增补（已实现）**：双槽位 pending/pending_memory；确认流 `/沉淀` 建草稿群内确认、`记住:` 直达入库；确认词确定性拦截（@ 门控，不走 LLM）；拒绝/过期物理删除；上下文装配段落模型 + 预算反向填充；压缩设计（Phase A 实施）；并发模型。
- ADR-003：ConsoleAgent 单次结构化路由；pending 判定和路由合并；群关联仓库；Task 持久化后异步执行。
### 执行与观测层决议

- ADR-004：SandboxAgent 使用受控 Agentic 循环；容器按仓库池化，任务工作区独占并重置；强制汇总、反死循环和安全护栏；TaskEvent 给团队看、Langfuse trace 给开发者排查。**详细设计已定稿（2026-09-23，见 `docs/design/sandbox-agent-detail.md`，未实现）**：原生 function calling 循环 + 8k 截断/确定性历史裁剪（LLM 摘要留二期）；TaskService 落库+入队即返回，重启时 pending 续跑、running 标 failed；容器按 repo 池化复用、**容器内 clone**（规避 Windows bind-mount I/O）；fastclaw 三层超时击杀（setsid+pgid+二次 exec kill）；回传走钉钉主动发消息 API + webhook 兜底 + TaskEvent 永远落库（session_webhook 5 分钟过期问题）；凭据 HTTPS+PAT+输出脱敏；工具集复用 ToolRegistry 全局单例、容器经 dispatch ctx 注入。
- ADR-005：SKILL 由模型自主选择加载；系统提示只放技能目录，按需加载全文并预检依赖。
- ADR-006：工具参考 hermes-agent，普通 Python handler + schema 注册到统一 ToolRegistry；ConsoleAgent 当前无工具循环。
- ADR-007：采用 Langfuse，trace_id 随 Task 传递并跨异步执行续接；TaskEvent 与 trace 各司其职。

## 下一步开发顺序

按优先级排列，0 是当前最高优先级，其余顺序在 0 完成后可能按联调结果调整。每项都标注了对应 ADR；完成一项就把该项移入「当前实现」并更新验证状态。

### 0. SandboxAgent 联调验证（当前最高优先级）

代码已全部落地、Fake 测试全绿，但**尚未连真实 Docker 与真实钉钉**：

- 本机启动 Docker Desktop，配 `REPO_{NAME}_URL/_TOKEN/_TEST_CMD` 环境变量，跑一个真实小仓库任务（clone→改代码→测试→push 分支）；
- 钉钉机器人确认已开通「企业内部机器人发群消息」API 权限（`robot/groupMessages/send`），验证任务回传走主动 API 通道；
- 跑通后可删掉 `SandboxAgent 未接入` 兜底分支（console/agent.py `_do_delegate` 已真实接线，该占位分支自然失效）。

### 1. Phase A：LLM 提炼 + 滚动摘要

LLM 已可用，Phase 0 的状态机和 `summary` 字段已就位，可直接开工：

- **纠正提炼**：路由器 `correction_detected=true` 时，把纠正内容建草稿进入确认流（复用 `MemoryConfirmHandler`，汇入同一状态机）。
- **滚动摘要**：`messages` 超过 `session_compact_trigger`（40）时触发压缩；Stage 1 免费过滤机械回复；Stage 2 用 `console_summary_model`（默认 = console_model）按 ADR-002 模板（当前主题/已解决问答+来源标记/关键决定/用户纠正原话/未决问题）迭代更新 `session.summary`；LLM 失败降级为丢弃 + "[更早 N 条消息已省略]"占位。
- **路由器 prompt 补澄清归属**：pending 带 `asked_user_id` 后，prompt 中加"旁人插话时倾向 new_question、不消费别人的澄清"指引。
- 详细设计已定稿于 `docs/decisions/002-memory-layer.md`（Phase 0 增补节）。

### 2. 修复 ConsoleAgent 的剩余正确性缺口

- **Session 并发安全**：同群锁仅在单个 ConsoleAgent 进程内有效；第一阶段维持单 worker 约束，未来做数据库级并发控制。
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
