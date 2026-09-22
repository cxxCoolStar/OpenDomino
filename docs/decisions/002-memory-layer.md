# ADR-002 三层记忆：Session、KBase 与正式知识库

> 状态：已确认整体边界；部分参数与实现细节待实施阶段确定
> 日期：2026-09-22
> 设计参考：Domino 原文、hermes-agent、WeKnora

## 背景

OpenDomino 的 ConsoleAgent 需要回答两类问题：

- 能够依据当前讨论、团队历史经验和正式文档直接回答的问题；
- 必须查看最新代码、复现页面或修改代码的问题。

这些信息的来源、可信度、生命周期和写入方式不同，不能把它们简单拼成一个无限增长的 Prompt，也不能把群聊流水账、团队经验和正式文档混成一个检索集合。

因此 OpenDomino 采用三层记忆模型：

```text
L1 群级 Session       当前群正在讨论什么
L2 KBase 空间记忆      团队从讨论中沉淀了什么经验和结论
L3 正式知识库          团队维护的正式事实、规范和流程是什么

代码现状不进入离线 CodeWiki 主链路：需要最新代码时委派 SandboxAgent 实时查看真实仓库。
```

## 总体决议

1. 三层记忆在逻辑上统一为一个 Memory Layer，但每层拥有独立的数据模型、生命周期、检索策略和治理规则。
2. ConsoleAgent 通过统一的 `MemoryService` 查询三层记忆，不直接依赖某个具体存储实现。
3. 记忆检索必须保留来源层级和引用信息，回答时不能把 KBase 经验伪装成正式文档事实。
4. 前两层和 L3 正式知识库都无法提供可信答案、且问题确实需要代码现状时，才委派 SandboxAgent 进入真实仓库。
5. 第一阶段不建设独立 CodeWiki 索引。代码高频变动，离线副本容易过期；代码相关问题由 SandboxAgent 实时读取，任务结果可以在确认后沉淀到 KBase。

## 三层职责对比

| 层级 | 名称 | 主要内容 | 来源 | 生命周期 | 可信度定位 |
|---|---|---|---|---|---|
| L1 | 群级 Session | 当前群的消息、待澄清状态、最近任务上下文 | 钉钉/Web 群消息 | 短期，持续滚动 | 当前上下文，不等于事实 |
| L2 | KBase 空间记忆 | 已提炼的团队决策、约定、排障结论 | 群聊/任务讨论，经提炼和确认 | 长期，可更新/删除 | 团队经验与共识 |
| L3 | 正式知识库 | 产品知识、研发规范、部署流程、架构说明、正式 Markdown/HTML 文档 | 人工维护或文档导入 | 版本化，权威来源 | 正式事实源 |

## L1：群级 Session

### 目标

Session 解决“这个群刚刚在聊什么”，让同一群的追问可以延续上下文，同时隔离不同群之间的临时讨论。

### 数据范围

每个群/会话至少持久化：

- `group_id` / `chat_id`
- `conversation_id`
- `session_id`
- 消息历史（sender、文本、时间、message_id、来源渠道）
- 当前待澄清状态（如果存在）
- 当前关联任务 ID（如果存在）
- 最近一次检索引用和路由结果

Session 不负责保存长期知识，也不应该把所有历史消息永久塞进模型上下文。进入 LLM 的内容使用最近窗口、摘要或按需检索。

### 待澄清状态

参考 hermes-agent 的 `clarify_gateway`，但不采用进程内阻塞 Event：

- Agent 发出澄清问题后立即结束本次消息处理；
- 在数据库 Session 中记录 `pending_clarification`；
- 下一条同群消息先检查待澄清状态，再进入普通路由；
- 解析成功后消费该状态并继续处理；
- 服务重启或多实例切换后，状态仍然可恢复；
- 支持超时、取消和用户明确发起新问题等状态转换。

这与 hermes 的“注册 pending clarify、消息入口先拦截回答”机制一致，但适配 OpenDomino 的无状态、多实例服务模型。

### Session 边界

- Session 中的临时讨论不会自动成为 KBase；
- 会话内容不能因为被检索过就获得“正式事实”身份；
- 需要跨群、跨会话复用的内容，必须经过 KBase 提炼和治理。

## L2：KBase 空间记忆

### 定义

KBase 是团队共享的空间记忆。它不是群聊归档，也不是普通文档切块库，而是从群聊或任务讨论中提炼出的、可跨群复用的完整结论条目。

底层可以使用 RAG 技术（embedding + 向量检索），但 KBase 的语义、数据模型和治理规则独立于通用 RAG：

- RAG 是检索增强技术范式；
- KBase 是有特定来源、粒度、确认和治理规则的团队记忆系统。

### 适合存储

- 已确认的技术决策及取舍原因；
- 项目长期有效的目录、模块和环境约定；
- 特殊环境约束、联调方式和排障结论；
- 多次协作后形成的研发规范与操作约定；
- 经确认、具有长期复用价值的任务结论。

### 明确不存储

- 个人隐私和敏感信息；
- 整段群聊原样归档；
- 没有结论的讨论、猜测和临时方案；
- 很快过期的一次性信息；
- 应该写入 SKILL 的具体工作流程和工具操作步骤。

### 与 SKILL 的分工

参考 hermes-agent 的 `build_memory_guidance`：

- 流程、工作方法、踩坑、任务相关的用户偏好和纠正，优先进入 SKILL；
- 适用于所有会话、与具体任务无关的团队事实和长期约定，进入 KBase；
- 记忆条目使用声明式事实，不写成会覆盖用户当前请求的命令式提示；
- 一周内可能过期的信息留在 Session 或任务记录中，不进入长期 KBase。

### 提炼触发时机

第一阶段采用保守触发，不监听所有群消息自动沉淀：

1. SandboxAgent 任务完成后，提炼任务中确认的结论；
2. 用户明确纠正 Agent 的知识时，提炼纠正后的事实；
3. 用户显式使用 `/沉淀` 等指令要求提炼；
4. 后续可增加“明确结论信号”触发，但必须经过人工确认，不能直接入库。

### 提炼流程

```text
任务完成 / 用户纠正 / 显式沉淀
        ↓
LLM 提炼结构化记忆，可返回空数组
        ↓
校验：是否长期有效、是否自包含、是否应进入 SKILL
        ↓
向群里展示待确认条目
        ↓
明确确认后写入 KBase；无确认默认不写入
        ↓
生成 embedding，保存条目和来源元数据
```

提炼必须允许输出“没有可沉淀内容”，不得为了填充库而强行生成记忆。

每条记忆必须脱离原对话也能理解，避免“上面提到的”“这个功能”等指代；应描述事实、结论以及必要的原因。

### KBase 条目模型（概念模型）

```text
MemoryEntry
- id                  唯一 ID
- title               主题
- memory_type        decision | convention | troubleshooting | environment
- statement           自包含的声明式结论
- rationale           决策原因/适用条件
- keywords            关键词
- embedding           向量
- source_session_id   来源会话
- source_task_id      来源任务（可空）
- source_message_ids  来源消息（可空）
- created_by          提炼者/确认者
- status              pending | confirmed | archived
- confidence          来源可信度/确认等级
- created_at
- updated_at
- supersedes_id       被新条目替代的旧条目（可空）
```

### 写入治理

参考 hermes-agent 的 `memory` 工具和 `write_approval`：

- 记忆写入采用 add / replace / remove 三类条目级操作；
- 写入默认经过确认，不允许 Agent 静默写入团队共享记忆；
- 用户拒绝时明确反馈“未保存”，不留下半条记忆；
- 旧约定发生变化时，优先 replace 或建立 supersedes 关系，而不是堆叠互相冲突的条目；
- 条目可精确定位、查看来源、更新和删除；
- 对单条长度和总量设置预算，避免 KBase 退化成聊天日志仓库；
- 检索只优先使用 `confirmed` 条目，`pending` 不进入普通回答上下文。

### KBase 检索

- 使用问题语义生成 embedding，在 KBase collection 中召回相关条目；
- 返回条目本体、相似度、状态和来源；
- 只将达到阈值且状态为 `confirmed` 的条目作为回答依据；
- 结果需要带“团队记忆”来源标记；
- 新旧条目冲突时，优先更新版本/`supersedes` 关系明确的新条目，并提示冲突，而不是静默拼接。

## L3：正式知识库

### 定义与边界

L3 是正式事实源，保存团队明确维护的文档，不负责从普通群聊中自动吸收经验：

- 产品知识；
- 研发规范；
- 发布和部署流程；
- 架构说明；
- 环境配置与操作手册；
- 已审核的 Markdown、HTML、PDF 等文档。

L3 与 KBase 的边界是：

- 文档原文、正式维护的流程和规范进入 L3；
- 群聊中形成、但尚未进入正式文档的团队共识进入 KBase；
- KBase 不能冒充正式文档；正式文档更新后，以正式文档为权威。

### WeKnora 参考结论

参考 `D:\developper\project\WeKnora` 的知识库实现，但不直接引入其 Go 服务：

- 标题/段落感知切块，而不是简单固定长度硬切；
- 保存标题层级、文档 ID、位置和来源信息；
- 子块命中后可恢复父块、扩展相邻块并合并连续上下文；
- 向量检索和关键词检索并行；
- 对结果归一化、融合、重排、去重和部分重叠清理；
- 检索结果保留可追溯引用；
- wiki 合成页面可以加权，但不作为第一阶段必需能力。

WeKnora 的事件驱动检索管道和多后端适配值得参考，但 OpenDomino 第一阶段不搬入完整事件总线、多租户权限矩阵和多种向量数据库后端。

### 第一阶段技术路线

```text
文档导入
  -> Markdown/段落/标题感知切块
  -> SQLite 保存文档、版本、chunk 元数据
  -> ChromaDB 保存向量
  -> SQLite FTS5 保存关键词索引
  -> 向量 + 关键词并行召回
  -> 分数归一化与融合
  -> 去重、父块恢复、相邻块扩展
  -> 返回来源引用
```

### 文档版本与更新

正式知识库必须以文档版本为单位管理更新：

- 文档拥有稳定 ID 和版本号/内容哈希；
- 文档内容变化时只删除或失效该文档的旧 chunks，再生成新版本索引；
- 未变化的文档不重复 embedding；
- 删除文档时级联删除其 chunks、向量和 FTS 记录；
- 回答引用具体文档版本和位置；
- 旧版本可以保留审计记录，但默认不参与召回。

### L3 检索结果

每条结果至少包含：

- 文档标题和标题路径；
- 文档版本；
- chunk 内容；
- 起止位置；
- 来源 URL 或文件路径；
- 检索方式和分数；
- 可选的父块/相邻上下文。

回答必须能够把正式文档作为明确依据呈现给用户。

## CodeWiki 与实时代码访问

第一阶段不建设离线 CodeWiki 主链路。

原因是代码变动频繁：无论是 LLM 合成 wiki 页面，还是过期的离线代码副本，都可能在高频变更后自信地给出错误实现。Aider、Continue、Sourcegraph 等项目证明符号索引和增量索引可行，但它们属于后续优化，不是当前最小闭环的必需组件。

当前方案：

- ConsoleAgent 不直接挂载或漫游真实代码仓库；
- L1/L2/L3 无法可信回答、且问题确实涉及代码现状时，委派 SandboxAgent；
- SandboxAgent 在独立 Docker 沙箱中实时读取真实仓库；
- 实时排查结果回流群聊；
- 经确认且具长期复用价值的结论可提炼回 KBase；
- 当实际数据证明实时委派过多、轻量代码查询成为瓶颈时，再评估 tree-sitter 符号索引、增量缓存和 FTS/MCP 工具。

## 三层检索协同

ConsoleAgent 的默认认知装配顺序：

```text
收到消息
  -> L1 Session：恢复当前群上下文和待澄清状态
  -> L2 KBase：召回已确认的团队经验和决策
  -> L3 正式知识库：召回正式文档事实和流程
  -> LLM 依据来源层级判断是否能可信回答
      -> 能回答：直接回复，并标记来源
      -> 信息不足：在群里提出澄清问题，持久化 pending 状态
      -> 需要最新代码/复现/修改：委派 SandboxAgent
```

这不是机械的“命中一层就停止”：

- Session 负责上下文，不单独证明事实；
- L2 和 L3 可以并行或按意图选择查询；
- L3 正式文档与 KBase 经验冲突时，正式文档优先，但应把冲突记录出来；
- L2 的历史结论可以作为 L3 检索的查询补充，但不能覆盖正式文档；
- 所有召回结果都要带来源类型，避免模型混淆可信度。

## 统一接口边界

ConsoleAgent 依赖抽象接口，不直接依赖 ChromaDB、SQLite 或具体文档解析器：

```python
class MemoryService(Protocol):
    async def load_session(self, session_key: str) -> SessionContext: ...
    async def save_session(self, session: SessionContext) -> None: ...
    async def recall_kbase(self, query: str, *, limit: int = 5) -> list[MemoryHit]: ...
    async def search_knowledge(self, query: str, *, limit: int = 8) -> list[KnowledgeHit]: ...
    async def create_memory_draft(self, draft: MemoryDraft) -> str: ...
    async def confirm_memory(self, memory_id: str, actor_id: str) -> None: ...
```

具体实现可以替换，但不得改变三层职责和来源标记。

## 非目标

第一阶段不实现：

- 自动把所有聊天消息写进 KBase；
- 把 KBase 和正式知识库混成一个无来源的向量 collection；
- 使用离线 CodeWiki 代替实时代码现状；
- 完整 WeKnora wiki 页面生成、双向链接和复杂 taxonomy；
- 用单一相似度分数掩盖来源可信度差异。

## 参考实现

- Domino 原文：三层上下文、KBase 空间记忆、知识库/CodeWiki、SKILL 分工。
- hermes-agent：`tools/memory_tool.py`、`tools/memory_tool_store.py`、`agent/prompt_builder.py`、`tools/write_approval.py`。
- WeKnora：`internal/infrastructure/chunker`、`internal/application/service/chat_pipeline/search.go`、`merge.go`、`query_understand.go`、`retriever/keywords_vector_hybrid_indexer.go`。
- 调研卡片：[WeKnora 知识库设计参考](../research/weknora-knowledge-base-card.md)。
