# OpenDomino

从群聊提问到任务交付 —— 团队级 Agent 基础设施全链路实践的开源复刻。

复刻自阿里云云原生消息前端团队自研研发协作平台 **Domino** 的架构思想：以「两类 Agent
接力 + 三层分层记忆 + SKILL 方法体系 + 工具生态」为蓝本，让 Agent 真正进入团队日常
协作入口，完成从群聊提问到代码交付的完整闭环。

## 核心架构

```
协作入口层  飞书机器人（lark-oapi WebSocket 长连接，ADR-008）
            @Domino 提问进管线；群内其余消息旁听落库；重启幂等回拉历史
                │
                ▼
ConsoleAgent（理解与路由，ADR-003；不直接改业务代码）
            上下文装配：L1 群 Session + L2 KBase 召回 + L3 知识库双路检索
                      + 旁听时间窗 + 进行中任务状态段
            路由五动作：
              answer   直接答疑（带来源标记）
              clarify  澄清追问
              delegate 委派任务 → SandboxAgent
              repo_op  本群绑定/解绑/查看仓库
              task_op  任务进展查询与中途管控（status/steer/cancel）
                │ delegate
                ▼
SandboxAgent（执行与交付，ADR-004；Docker 真实仓库沙箱）
            investigate 排查给结论 / modify 变更代码
            clone → 执行循环（固定工具面 9 项：目录/读文件/grep、写文件与
            patch_replace/apply_patch、run_command、git diff/log）
            → 测试验证 → push domino/task-* 分支 → 预评审 → 分支+diff 回传群聊

记忆层  L1 群级 Session（超窗压缩摘要）· L2 KBase 空间记忆（向量+关键词召回）
        · L3 知识库（FTS5 全文 + 向量双路检索）

后台循环  旁听蒸馏（ADR-009）：群空闲 → LLM 蒸馏候选结论 → 群内确认流 → 沉淀 L2
          （提议前查重闸 + supersedes 过时处理；modify/investigate 完成后亦提炼工程经验）
          知识库同步（ADR-011）：周期扫描 docs/ → 规范校验 → 切块入库 → 镜像删除
```

## 技术栈

- 后端：Python 3.12+ · FastAPI + SQLAlchemy（SQLite/aiosqlite）+ ChromaDB（向量检索）+ Docker SDK（沙箱）
- LLM：OpenAI 兼容接口，console / sandbox 双模型独立配置（base_url / api_key / model）
- 群聊入口：飞书机器人（lark-oapi WebSocket 长连接，见 ADR-008）
- 测试：pytest 单测 + `scripts/e2e_*.py` 链路级 E2E + 黄金测试集（21 case，真实 LLM 路由断言）

## 快速开始

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
# .env 必填：LLM_BASE_URL / LLM_API_KEY / CONSOLE_MODEL / SANDBOX_MODEL
#           FEISHU_APP_ID / FEISHU_APP_SECRET
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

健康检查 `http://localhost:8000/health`，就绪检查 `/ready`。

任务沙箱依赖 Docker Desktop 与预装镜像 `opendomino-sandbox:latest`（git + /workspace）；
仓库经群内 `repo_op bind` 绑定（L2 持久化），clone/push 凭据使用 `GITHUB_TOKEN`。

## 仓库结构

```
backend/
  app/channels/    # 飞书入口：消息解析、@ 门控、旁听、回拉（ADR-008）
  app/console/     # ConsoleAgent：上下文装配、路由、任务管控、蒸馏、确认流（ADR-003/009/010）
  app/memory/      # 三层记忆：Session / KBase / 知识库，向量召回（ADR-002）
  app/sandbox/     # SandboxAgent：容器池、执行循环、工具面、任务管线（ADR-004）
  app/docs/        # 知识库文档同步与切块入库（ADR-011）
  scripts/         # e2e_*.py 链路测试、run_golden.py 黄金测试集
  eval/cases/      # 黄金测试集 YAML case（路由族 v1，21 case）
docs/decisions/    # ADR 架构决策记录
```

## 设计原则（来自原文）

1. Agent 要进入工作，而不是让工作迁移到 Agent
2. 上下文越多不一定越好，分层比堆叠更重要
3. 团队经验必须从 Prompt 走向可维护的 SKILL
4. 无人值守不等于无人负责
5. 生产可用来自边界，而不是「全能」

## 设计决策（ADR）

逐模块讨论确认后归档于 `docs/decisions/`：

- [ADR-001 协作入口层：钉钉机器人（Stream 模式）](docs/decisions/001-channel-layer.md)（通道选型已被 ADR-008 取代）
- [ADR-002 三层记忆：Session、KBase 与正式知识库](docs/decisions/002-memory-layer.md)
- [ADR-003 ConsoleAgent：理解与路由](docs/decisions/003-console-agent.md)
- [ADR-004 SandboxAgent：执行与交付](docs/decisions/004-sandbox-agent.md)
- [ADR-005 SKILL 方法层](docs/decisions/005-skill-layer.md)
- [ADR-006 工具层：Tool Registry](docs/decisions/006-tool-layer.md)
- [ADR-007 链路追踪：Langfuse](docs/decisions/007-tracing.md)
- [ADR-008 协作入口层迁移飞书与群聊旁听上下文](docs/decisions/008-feishu-channel.md)
- [ADR-009 旁听结论蒸馏：群聊讨论经确认沉淀为团队记忆](docs/decisions/009-overheard-distillation.md)
- [ADR-010 任务进展可见与中途管控（task_op）](docs/decisions/010-task-progress-and-control.md)
- [ADR-011 知识库文档自动同步：周期扫描 + Source 抽象 + 镜像删除](docs/decisions/011-kb-doc-sync.md)

## 状态

核心闭环已通：飞书群 @ 提问 → ConsoleAgent 路由（answer/clarify/delegate/repo_op/task_op）
→ SandboxAgent 真实仓库交付（investigate 出排查结论 / modify 真实 push 分支）→ 分支与
diff 回传群聊。三层记忆（L1 Session / L2 KBase 向量召回 / L3 知识库双路检索）、旁听
上下文与蒸馏确认流（ADR-008/009，含查重与 supersedes）、任务完成经验提炼、任务管控
（ADR-010）、知识库自动同步（ADR-011）、黄金测试集回归均已落地。

进行中/未开工：SKILL 方法层（ADR-005 已定稿）、独立 AI 验证与回炉（ADR-004 二期，
一期以预评审 + 人工合入替代）、Langfuse 链路追踪（ADR-007 已定稿）、MCP 工具生态
（ADR-006，一期工具面固定于 `sandbox/tools.py`）、Web 管理后台。
