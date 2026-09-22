# WeKnora 知识库设计参考卡片

> 状态：调研中
> 来源：`D:\developper\project\WeKnora`
> 用途：为 OpenDomino L3 正式知识库设计提供参考，不表示照搬 WeKnora 全部实现。

## 1. 定位

WeKnora 的知识库是“正式文档事实源”，与 OpenDomino 的 KBase 团队记忆不同：

- 知识库：文档摄取、切块、建立索引、检索正式规范和流程。
- KBase：从群聊/任务讨论中提炼的团队决策、约定和排障结论。
- Session：当前群的短期上下文。

## 2. 已确认的 WeKnora 机制

### 文档切块

WeKnora 的 `internal/infrastructure/chunker` 不是简单按固定字符截断，而是分层处理：

- 优先识别 Markdown 标题层级；
- 通过 heading hierarchy / heading splitter 保留章节结构；
- 对无法按标题合理切分的内容使用 heuristic splitter；
- 使用 token 估算控制 chunk 大小；
- 通过 parent chunk、相邻 chunk 和 breadcrumb 信息保留上下文；
- 检索命中子块后，可以回溯/扩展父块和邻近块，避免只给模型一小段缺乏上下文的碎片。

### 检索

WeKnora 的检索管道是事件驱动、插件式的，主要流程为：

1. Query Understand：可用 LLM 结合历史改写查询并识别意图；
2. Search：按知识库/知识源范围执行检索，向量检索与关键词检索可并行；
3. Query Expansion：召回不足时进行查询扩展并追加结果；
4. Rerank：对候选结果重排；
5. Merge：去重、父块恢复、相邻块扩展、同文档合并、FAQ 结果处理、部分重叠清理；
6. 将最终上下文和引用来源注入回答。

### 混合检索

WeKnora 提供 `KeywordsVectorHybridRetrieveEngineService`：

- 关键词检索负责精确命中产品名、接口名、配置项、错误码等术语；
- 向量检索负责语义相似和自然语言问法；
- 结果经过归一化、融合和后续重排；
- 向量索引支持批量 embedding、并发保存和失败重试；
- 后端可插拔，支持多种检索引擎，而上层服务使用统一接口。

### 结果合并与上下文恢复

WeKnora 的 `PluginMerge` 明确包含以下阶段：

- 按 chunk ID 和内容签名去重；
- 注入历史引用；
- 解析 child chunk 对应的 parent chunk；
- 按知识源和 chunk 类型合并连续内容；
- 扩展短上下文的相邻 chunk；
- 对扩展结果再次合并；
- 清理完全重复和高度重叠内容。

### Wiki 加权

WeKnora 的 `PluginWikiBoost` 对 wiki 页面 chunk 的相关性分数乘以 `1.3`，原因是 wiki 页面属于 LLM 预合成、交叉引用后的结构化知识，通常比原始文档 chunk 更连贯。

## 3. 对 OpenDomino 的直接启示

### 第一版应采用

- Markdown/HTML/纯文本文档导入；
- 标题感知切块，而不是固定长度硬切；
- chunk 保存文档 ID、标题路径、起止位置、版本和来源 URL；
- 向量 + 关键词混合检索；
- 检索结果去重、父块恢复、相邻块扩展；
- 最终回答必须保留可追溯来源；
- 知识库检索结果与 KBase 记忆结果保持来源类型和可信度标记。

### 第一版暂缓

- WeKnora 支持的多种向量数据库后端；
- 完整事件插件总线；
- 图片/表格/多模态文档处理；
- Wiki 页面生成、目录树、双向链接和复杂 taxonomy；
- 多租户企业级权限矩阵。

## 4. 当前暂定技术路线

OpenDomino L3 知识库暂定为：

```text
文档导入
  -> 标题/段落感知切块
  -> SQLite 元数据 + ChromaDB 向量索引
  -> SQLite FTS5 关键词索引
  -> 并行召回
  -> 分数归一化与融合
  -> 去重 + 父块/相邻块扩展
  -> 来源引用
  -> ConsoleAgent 上下文
```

这只是调研结论，不代表已经完成最终 ADR。后续还需要继续确认：

- 文档来源和导入接口；
- 文档版本更新和增量重建策略；
- KBase 与 L3 知识库的联合检索顺序和冲突处理；
- LLM query rewrite 是否进入第一版；
- 引用格式和可信度规则。
