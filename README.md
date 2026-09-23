# OpenDomino

从钉群提问到任务交付 —— 团队级 Agent 基础设施全链路实践的开源复刻。

复刻自阿里云云原生消息前端团队自研研发协作平台 **Domino** 的架构思想：以「两类 Agent 接力 + 三层分层记忆 + SKILL 方法体系 + CLI/MCP 工具生态」为核心，让 Agent 真正进入团队日常协作入口，完成从群聊提问到代码交付的完整闭环。

## 核心架构

```
协作入口层（钉钉机器人 + Web 群聊）
        │  @Domino 提问 · 群级会话延续 · 任务链接回传
        ▼
ConsoleAgent（理解与路由，不直接改业务代码）
        │  延续群 Session → 检索 KBase → 查知识库/CodeWiki
        │  快路径：直接答疑；慢路径：委派任务
        ▼
SandboxAgent（执行与交付，真实仓库沙箱）
        │  识别工程规范 → 定位 → 编码 → 构建测试 → 评审 → 部署 → AI 验证
        ▼
记忆层  L1 群级Session · L2 KBase空间记忆(向量) · L3 知识库/CodeWiki
方法层  SKILL（brainstorming / mq-frontend / find-i18n-key / medusa / ai-testing）
工具层  CLI/MCP 适配器（代码 / 知识 / 评审 / 发布，按角色+白名单注入）
```

## 技术栈

- 后端：Python FastAPI + SQLAlchemy + ChromaDB（向量记忆）+ Docker SDK（沙箱）
- 前端：React + Vite + Ant Design
- LLM：OpenAI 兼容接口（可配 base_url / api_key / model）
- 群聊入口：钉钉机器人（Stream 模式）+ Web 模拟群聊双通道
- Git：真实提交；CodeReview / 部署 / AI 验证：简化闭环

## 设计原则（来自原文）

1. Agent 要进入工作，而不是让工作迁移到 Agent
2. 上下文越多不一定越好，分层比堆叠更重要
3. 团队经验必须从 Prompt 走向可维护的 SKILL
4. 无人值守不等于无人负责
5. 生产可用来自边界，而不是「全能」

## 设计决策（ADR）

逐模块讨论确认后归档于 `docs/decisions/`：

- [ADR-001 协作入口层：钉钉机器人 + Web 群聊](docs/decisions/001-channel-layer.md)
- [ADR-002 三层记忆：Session、KBase 与正式知识库](docs/decisions/002-memory-layer.md)
- [ADR-003 ConsoleAgent：理解与路由](docs/decisions/003-console-agent.md)
- [ADR-004 SandboxAgent：执行与交付](docs/decisions/004-sandbox-agent.md)
- [ADR-005 SKILL 方法层](docs/decisions/005-skill-layer.md)
- [ADR-006 工具层：Tool Registry](docs/decisions/006-tool-layer.md)
- [ADR-007 链路追踪：Langfuse](docs/decisions/007-tracing.md)

## 状态

🚧 项目初始化阶段，开发中。
