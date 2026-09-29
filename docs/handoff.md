# OpenDomino 开发交接

> 更新日期：2026-09-29（第三次更新：清理过时结论——SandboxAgent、L3 检索、Phase A/C、蒸馏、任务管控、KB 自动同步均已落地；补当前真实缺口清单）
> 当前阶段：**核心闭环已通**——飞书群 @ 提问 → ConsoleAgent 路由 → SandboxAgent 真实仓库交付 → 回传群聊，三层记忆与旁听/蒸馏/管控/KB 同步全部上线；剩余缺口见「尚未实施」。

## 项目目标

OpenDomino 复刻「从群聊提问到任务交付」的团队级 Agent 基础设施：飞书群作为唯一消息入口（见 ADR-008），ConsoleAgent 负责理解、检索和路由，SandboxAgent 负责进入真实仓库执行和交付。Web 后期只做管理后台，不做聊天入口。

## 当前实现

后端主链路按「入口 → 记忆 → Agent → 沙箱」四层组织，位于 `backend/app/` 下。以下按层列出当前实现状态与关键能力，接手前请先通读本节再对照 ADR 细读。

### 入口与会话

后端位于 `backend/`，基于 Python 3.12、FastAPI、SQLAlchemy async/SQLite、OpenAI-compatible SDK 和 lark-oapi SDK（飞书，WebSocket 长连接）。

- **飞书入口（ADR-008）**：统一消息类型、消息解析、仅群聊、@ 门控（结构化 mentions 判定）、群/用户白名单、message_id 去重、bot 发消息 API 按 chat_id 寻址回复；订阅 `im.message.receive_v1` 全量接收群消息——@ 消息进 ConsoleAgent 管线，非 @ 消息写旁听缓冲（不触发回复）；支持群历史消息回拉（入群事件 + 重启双触发，message_id 幂等）。
- **L1 Session**：SQLite 持久化群消息、pending 状态、活跃任务 ID 和最近路由；SQLite 数据目录会在初始化时自动创建。
### 记忆与知识库

- **L2 KBase 基础**：草稿/确认记录落库；召回三级递进（Phase C 已实现）——向量召回（方舟 embedding）→ 关键词召回（未配 embedding 时）→ 全量注入（confirmed 条目数少时）；旁听消息写 L2 存而不检索（ADR-008）。
- **L3 知识库**：已落地（`app/docs/knowledge.py`）——规范契约切块 + FTS5 中文 2-gram 切词 + 向量双路检索 + RRF 融合；支持 `python -m app.docs.import_docs ../docs` 手动导入与周期自动同步（ADR-011，`docs/syncer.py`）。
### Agent 与工具

- **ConsoleAgent**：单次结构化 JSON 路由；answer / clarify / delegate / repo_op / task_op 五种动作；同群串行锁；pending 澄清随 Session 持久化；LLM JSON 解析失败重试一次后降级回复；滚动压缩（Phase A）与纠正提炼已上线。
- **旁听蒸馏（ADR-009）**：`console/distiller.py` 水位线扫描 + LLM 蒸馏 → 非阻塞记忆提议 → 群内确认流；E2E 26 用例全绿。
- **任务完成提炼（ADR-002 Phase B，2026-09-29）**：`console/task_distill.py`——modify/investigate 任务 DONE 后 fire-and-forget 提炼工程经验（instruction + 汇总 + 改动统计），草稿进确认流，来源 `task` 可溯 `source_task_id`；开关 `task_distill_enabled`；失败不影响任务交付。
- **任务管控（ADR-010）**：`sandbox/control.py` steer/cancel + `console/task_ops.py` 查询管控 + 上下文状态段活化。
- **ToolRegistry（ADR-006）**：`app/tools/registry.py` 注册、toolset 过滤、check_fn、参数类型强转、统一错误与结果截断已有基础实现（含单测）；尚未接入执行循环——沙箱现用自己的 `sandbox/tools.py` 调度（两套工具层并存的收敛待决策）。
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
  - `reporter.py` 四类事件回传 + `channels/registry.py` 寻址；FeishuChannel 经 bot 发消息 API 按 chat_id 寻址回传（text 被拒时降级 post，2026-09-29 起改发 interactive 卡片）；
  - `main.py` 已装配全链：TaskService→TaskExecutor→TaskPipeline→ConsoleAgent.delegate/task_op 真实接线；重启恢复 pending 续跑、running 标失败。
  - 测试 `tests/test_sandbox.py` Fake 全覆盖（不依赖 Docker）；真实 Docker + GitHub PAT 集成经 `scripts/e2e_sandbox.py` 验证通过。
### 运行入口

- **运行入口**：`backend/app/main.py` 启动 FastAPI、SQLite 与飞书 WebSocket 长连接；提供 `/health` 和 `/ready`。
### 尚未实施

- **verify.py 独立 AI 验证与 blocked 回炉**（ADR-004 管线步骤 5）：一期以预评审替代（`pipeline.py` finish 终态恒为 review），`sandbox_verify_max_retries` 配置暂无消费方。
- **ADR-008 入群事件回拉**：`im.chat.member.bot.added_v1` 订阅未实现，bot 新入群需重启才能补拉历史；重启补拉已实现。
- **SKILL 方法层（ADR-005）**：ADR 定稿、零代码——`app/skills/` 不存在、`load_skill` 未注册、`skills_dir` 为死配置。
- **Langfuse 链路追踪（ADR-007）**：ADR 定稿、零代码——`app/tracing/` 不存在；依赖已装、`LANGFUSE_*` 配置已备，Task.trace_id 字段在库但恒为空。
- **FeishuDocsSource（ADR-011）**：飞书云文档接入只留接口，周期同步架构已就绪。
- **Web 管理后台**：README/handoff 均列为后期建设，ADR 未立项。

## 验证状态

- 单测：`cd backend && python -m pytest tests/ -q`（112 passed；console/sandbox/记忆/确认流/Phase A/任务提炼/task_ops/tools/向量召回全覆盖）。
- **E2E（真实环境）**：飞书通道 44 用例（`scripts/e2e_feishu_channel.py`）、蒸馏 26 用例（`scripts/e2e_distill.py`）、任务提炼 9 断言（`scripts/e2e_task_distill.py`，FakeLLM 可离线复跑）、KB 同步（`scripts/e2e_kbsync.py`）、沙箱三模式（`scripts/e2e_sandbox.py`，真实 Docker + GitHub PAT，modify 真实 push 分支）——均有 JSON 报告产物。
- **黄金测试集**：`eval/cases/` 21 个路由族 case + `scripts/run_golden.py`（真实 LLM，Decision 主断言），最近报告见 `data/golden_report.json`；重跑后注意核对报告里的 git_commit 是否为最新提交。
- 服务运行于 `http://127.0.0.1:8000`，飞书 WebSocket 握手成功，真实群 @问答 + 旁听上下文闭环已验证（2026-09-28）。
- `init()` 已加轻量列迁移（`pending_memory_json`、`summary`），旧库自动升级。

## 已确认的设计决策

完整背景见 `docs/decisions/`：

- ADR-001：仅群聊作为入口（通道原为钉钉，后迁飞书）；不做私聊和 Web 群聊。Web 后期只作为管理后台。**通道选型已被 ADR-008 取代**。
- ADR-002：L1 群级 Session、L2 团队 KBase、L3 正式知识库分层；团队记忆写入需要确认。**Phase 0 / A / C 增补（已实现）**：双槽位 pending/pending_memory；确认流 `/沉淀` 建草稿群内确认、`记住:` 直达入库；确认词确定性拦截（@ 门控，不走 LLM）；拒绝/过期物理删除；上下文装配段落模型 + 预算反向填充；滚动压缩（Phase A）；向量召回三级递进（Phase C）；纠正提炼。**Phase B「任务完成提炼」未实施**；多 worker 并发控制暂缓。
- ADR-003：ConsoleAgent 单次结构化路由；pending 判定和路由合并；群关联仓库；Task 持久化后异步执行。
- ADR-008：**飞书为唯一协作入口通道**（lark-oapi WebSocket 长连接），钉钉通道代码与 `DINGTALK_*` 配置已移除、通道数据不迁移；群聊旁听上下文（@ 进管线、非 @ 写旁听缓冲、装配时合并为单一时间线）；历史回拉入群与重启双触发。已实测验证（2026-09-28：机器人身份自学习、@ 占位符还原、上下文段落按稳定性重排）。

### 执行与观测层决议

- ADR-004：SandboxAgent 使用受控 Agentic 循环；容器按仓库池化，任务工作区独占并重置；强制汇总、反死循环和安全护栏；TaskEvent 给团队看、Langfuse trace 给开发者排查。**已实现（2026-09-23 定稿设计，见 `docs/design/sandbox-agent-detail.md`）**：原生 function calling 循环 + 8k 截断/确定性历史裁剪（LLM 摘要留二期）；TaskService 落库+入队即返回，重启时 pending 续跑、running 标 failed；容器按 repo 池化复用、**容器内 clone**（规避 Windows bind-mount I/O）；fastclaw 三层超时击杀（setsid+pgid+二次 exec kill）；回传走 FeishuChannel bot 发消息 API 按 chat_id 寻址 + TaskEvent 永远落库（ADR-008 迁移后修订，见设计文档 §4.2）；凭据 HTTPS+PAT+输出脱敏；**独立 verify.py 评审与回炉留待实施（一期以预评审替代）**。
- ADR-005：SKILL 由模型自主选择加载；系统提示只放技能目录，按需加载全文并预检依赖。**未实施**。
- ADR-006：工具参考 hermes-agent，普通 Python handler + schema 注册到统一 ToolRegistry；ConsoleAgent 当前无工具循环。Registry 基础实现与单测已有，**尚未被任何执行链路消费**（沙箱暂用自有工具调度，收敛方案待决策）。
- ADR-007：采用 Langfuse，trace_id 随 Task 传递并跨异步执行续接；TaskEvent 与 trace 各司其职。
- ADR-009：旁听结论蒸馏——群聊讨论经确认沉淀为团队记忆（水位线按消息时间戳推进）。
- ADR-010：任务进展可见与中途管控（task_op）。

## 下一步开发顺序

按「闭环完整性」优先排列；完成一项就移入「当前实现」并更新验证状态。第一优先是交付质量与观测闭环（主链路剩余断点）：verify.py 独立 AI 验证与回炉、Langfuse 接入与 trace_id 贯通（Phase B 任务完成提炼已于 2026-09-29 完成，移入「当前实现」）。

- **verify.py 独立 AI 验证与回炉**（ADR-004 步骤 5）：独立评审 + blocked 时按 `sandbox_verify_max_retries` 回炉重试；
- **Langfuse 接入**（ADR-007）：`app/tracing/` 埋点 + Task.trace_id 贯通（委派时生成、沙箱异步续接）。

### 2. 小而明确的补丁

- **入群事件回拉**（ADR-008 决议 4）：订阅 `im.chat.member.bot.added_v1` 触发 backfill，完成后回填 ADR 实测验收项 2；
- **解析降级话术**（router.py）：无效 JSON 降级回复补来源标记与安全话术，并补测试。

### 3. 大特性单独立项

- **SKILL 方法层**（ADR-005）：文件发现、frontmatter 校验、渐进式目录、`load_skill`；
- **FeishuDocsSource**（ADR-011）：飞书云文档 Source 子类；
- **Web 管理后台**：需先立 ADR（任务/KBase 治理、数据库可见性，ADR-009 亦标注值得单独立项）。

### 4. 质量加固

- `docs/knowledge.py` + `chunker.py`（L3 核心）补单测回归防线；
- `channels/feishu.py`、`console/distiller.py`、`sandbox/container.py` 现仅脚本级 E2E（依赖真实凭证），按需抽可离线跑的单测；
- 黄金测试集扩展：目前仅覆盖路由族，context 记忆/沙箱执行/蒸馏无 case。

## 启动与测试

```bash
cd backend
python -m pytest tests/ -q
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

服务需要 `backend/.env` 中配置 OpenAI-compatible LLM 和飞书应用凭证（`FEISHU_*`，见 `backend/.env.example`；ADR-008 后 `DINGTALK_*` 已移除）。`.env` 不应提交到版本库。

## 交接边界与注意事项

- 当前完成的是「飞书入口 + ConsoleAgent 路由 + SandboxAgent 交付 + 三层记忆 + 旁听蒸馏/管控/KB 同步」；未完成清单见「尚未实施」。
- 不要把 ADR 文档中已写的模块结构当成已实现代码：SKILL、Langfuse、verify.py 独立验证仍无代码。
- Session 并发安全仅单 worker（同群锁进程内有效，ADR-002 声明多 worker 需数据库级并发控制，暂缓）。
- KBase 现为全局共享（ADR-009 遗留：按群隔离作用域未做），多群使用后注意串味问题。
- 不要打印或提交 `.env`、API key、飞书/仓库 secret、webhook 等敏感值。
- 开发继续遵循 `AGENTS.md`：模块逐个讨论确认，发现需求与原文定位偏差时先指出再实施。
