---
name: python-backend
description: 本仓库 Python 后端工程规范。当任务需要修改 backend/ 下的 Python 代码、新增配置项或编写测试时使用，对齐分层、风格与验证惯例。
metadata:
  always: false
  requires:
    tools: [read_file, grep, run_command]
    env: []
    bins: []
---

# python-backend

OpenDomino 后端（backend/）的工程规范。本文各条惯例均从仓库真实文件观察归纳并标注来源；
改代码先对齐既有模式，拿不准就 grep 同形模块找先例，不要发明新结构。

## 何时触发

- 任务要新增或修改 backend/app/ 下任何 Python 模块（console/sandbox/memory/docs/skills/channels）
- 任务要加配置项、加测试、改数据库模型或扩展 SKILL 层
- 任务要排查 backend/ 的测试失败或 ruff lint 问题

以下情况**不要**用（越界即停）：
- 改动目标在 docs/ 知识库文档——那里走 AGENTS.md 的知识库文档撰写规范（一节一主题、三级标题）
- 纯讨论、方案设计或与 Python 代码无关的运维操作

## 执行步骤

1. **先定位分层**：分层固定，不建平行顶层目录——app/console（路由与主管线，ADR-003/010）、
   app/sandbox（受控执行循环，ADR-004）、app/memory（三层记忆，ADR-002）、app/docs（L3 知识库与文档同步，ADR-011）、
   app/channels（飞书入口，ADR-008）、app/skills（SKILL 层，ADR-005）；跨模块装配只发生在 app/main.py lifespan；
2. **找同形先例再动手**：写模块前先读同类文件——加处理逻辑参考 app/console/task_ops.py，
   加存储参考 app/memory/service.py 或 app/sandbox/storage.py；复制现有骨架，不引入新范式；
3. **配置走 app/config.py**：新字段加进 Settings 对应的 `# ---- ADR-xxx ----` 分组；必填项不设默认值
   （参照 LLM 四项），可选项设默认值并写行内注释说明理由（参照 `sandbox_llm_max_tokens` 的写法）；
   消费方统一 `from app.config import settings`，禁止自己解析环境变量；
4. **测试先行设计**：按 AGENTS.md 测试策略，先列出所有可能失败的方式再写代码；优先 E2E，
   参照 scripts/e2e_*.py 的模式产出可复核工件（如 e2e_sandbox_report.json）；
5. **交付前全量验证**：`cd backend && python -m pytest tests/ -q` 全过、`python -m ruff check app tests` 无新增告警。

## 工程约束

- 模块骨架三件套：首行 `from __future__ import annotations`；中文模块 docstring 写明 ADR 依据；
  模块级 `log = logging.getLogger(__name__)`（见 app/sandbox/agent.py、app/console/router.py、app/skills/registry.py）
- Python 3.12+ 类型注解用内建泛型 `list[str]`、`X | None`，不用 typing.List/Optional（pyproject.toml requires-python>=3.12）
- 行宽 120（pyproject.toml ruff line-length=120，注释注明 88 会产生无意义 diff）；lint 规则集 E/W/F/I；
  app/sandbox/_vendor/ 是原样 vendor 的代码且已在 per-file-ignores 排除，不要顺手"修 lint"
- 错误返回 `{"error": ...}` JSON 信封（app/sandbox/tools.py 的 _err、app/skills/loader.py 同款），
  让执行循环的连败护栏统一感知，不抛裸异常穿透工具边界
- dataclass 字段引用 settings 用 `field(default_factory=lambda: settings.xxx)` 惰性读取——
  类属性默认值会在导入时固化快照，测试 monkeypatch settings 失效（app/sandbox/agent.py StepGuard 注释原话）
- 数据库用 SQLAlchemy 2.0 async + aiosqlite，DeclarativeBase + Mapped/mapped_column 声明式模型
  （app/memory/service.py）；向量进 Chroma 且 collection 按用途隔离（l3_chunks 与 kbase_memories，app/docs/knowledge.py）
- 测试放 tests/ 顶层 test_*.py；tests/conftest.py 负责 sys.path 注入，并有 autouse `_isolate_chroma`
  把 Chroma 持久化目录强制指向 tmp_path（曾发生污染真实 ./data/chroma 的事故，不允许绕过）
- 依赖注入用 Fake：FakeExecutor / 脚本化 LLM（tests/test_sandbox.py 头注），单测不碰 Docker 与真实 LLM；
  真实 Docker 集成用例标 @pytest.mark.docker 单独跑
- 数据库测试用真实 SQLite 实例 + tmp_path 隔离：`db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db"`
  （tests/test_memory.py、tests/test_task_ops.py 的 fixture 同款）
- pytest.ini 设 asyncio_mode=auto，但存量测试仍显式标 @pytest.mark.asyncio——新用例保持显式标注
- 提交纪律（AGENTS.md）：一个 commit 一个主题，验证可用立即提交，不积攒合并提交

## 验收标准

- `cd backend && python -m pytest tests/ -q` 全部通过；新功能有对应测试覆盖且先于实现设计好失败面
- `python -m ruff check app tests` 无新增违规（规则集 E/W/F/I，行宽 120）
- requirements.txt 不新增依赖，除非任务确实必需，且按现有分组注释（# 数据存储 / # LLM 等）归类
- 新配置项在 app/config.py 有说明理由的注释，默认值场景下测试可跑通
- 交付摘要能说清：改动落在哪个分层哪个模块、验证命令与关键输出
