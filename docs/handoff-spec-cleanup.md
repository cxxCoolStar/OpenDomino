# 待办交接：代码规范整治后续项

> 日期：2026-09-30
> 状态：待执行（本文档是新会话的执行输入，逐项独立实施）
> 关联：ADR-004、ADR-009、ADR-006、docs/standards/knowledge-doc-standard.md

## 背景与当前基线

2026-09-30 完成了一轮代码规范整治，当前基线：`ruff check app tests scripts` 全绿、97 个测试全过（`-m "not docker"` 过滤，与 CI 一致）、工作区干净、无死代码。

本轮已完成的四个 commit：

1. `11e1969` fix(ADR-004)：`dispatch_tool` 按 schema 强转参数（修 `replace_all="false"` 布尔反转）+ `apply_patch` 成功响应改用 `PatchResult.files_modified`（修按 schema 调用必现 KeyError）；
2. `3cafeeb` fix：清偿存量 F821/F811/F841（`context.py` 的 `ChatMessage`、`service.py` 的 `KnowledgeBase` 悬空引用等）；
3. `27a633b` chore：接入 ruff（`backend/pyproject.toml`：E/W/F/I、行宽 120、`_vendor` 豁免、`scripts/e2e_*` 豁免 E501）；
4. `1edfe31` ci：GitHub Actions 门禁（`.github/workflows/backend.yml`：lint job + test job）。

后续 CI 红过一轮，已修复：`340adfd`（`test_phase_a.py` 去 LLM 依赖，FakeLLM 脚本化）+ `10f48fd`（清零 ADR-009 提交带入的 17 处 lint 违规）。

## 工作纪律（每个新会话必须遵守）

- 改动前先摆方案与用户确认，确认后再动手（AGENTS.md「问题修复方式」）；
- 一个 commit 一个主题，完成即提交；
- 用户可能有**并行进行中的未提交改动**（此前是 ADR-009 蒸馏查重闸）：`git add` 严禁整文件暂存，必须逐文件核对 diff、确认与本次主题无关的内容不进暂存区；提交前用 `git diff --cached --stat` 复核；
- 测试策略：E2E 优先（Fake 而非 mock 框架），NEVER 在写完代码后补单元测试——修复的防回归测试跟随修复一起写。

## P1-1：修复 `_ops_cache` 内存泄漏与 id 复用风险

`sandbox/tools.py` 的模块级全局字典 `_ops_cache` 按 `id(ctx.executor)` 缓存 `ShellFileOperations` 实例，存在两个正确性问题：

1. **内存泄漏**：每个任务一个 executor，缓存条目永不清理（`ToolContext` 每任务新建），长驻进程无限增长；
2. **id 复用命中过期缓存**：Python 对象被 GC 后 `id()` 可被复用，新任务的新 executor 可能命中仍绑定旧容器的缓存实例——工具会写进已销毁容器的错误上下文。

修复方案（建议）：把缓存挪进 `ToolContext`（每任务一个实例，生命周期天然一致），在 `ToolContext` 上加一个惰性持有的 `_file_ops` 属性或字段，`_get_file_ops` 改为从 ctx 取。同时删除模块级 `_ops_cache`。改动面约 15 行，注意 `hermes_adapter.py` 不受影响（它只包 executor 本身）。

防回归：测试断言「同一 ctx 两次 `read_file` 复用同一 ops 实例」「不同 ctx 的 ops 实例互不相同」。现有 `test_sandbox.py` 的 `LocalShExecutor` 设施可复用。

## P1-2：补 eval/cases 真 LLM prompt 回归集

`test_phase_a.py` 去 LLM 化（`340adfd`）后，「真模型会不会正确路由/标注 correction_detected/产出可压缩摘要」这一回归价值悬空。承接处是 `backend/eval/cases/`（现有 21 个 yaml 用例的评测框架，跑法参考 `backend/scripts/run_golden.py`）。

需要补的用例（对齐被移除的三个真实 LLM 测试场景）：

1. 纠正消息 → 路由 `action=answer` 且 `correction_detected=true`；
2. 非纠正普通陈述 → `correction_detected=false`；
3. 超阈值会话 → 摘要产出且窗口保留（经 `maybe_compact`）。

用例 yaml 的字段结构抄现有 case 文件；真 LLM 跑、不进 CI 门禁。

## P1-3：StepGuard 配置快照不一致

`sandbox/agent.py` 的 `StepGuard` 中，`max_steps` 用 `field(default_factory=lambda: settings.sandbox_max_steps)` 惰性读取（类内注释解释了原因：测试 monkeypatch settings 才生效），但 `same_call_limit: int = settings.sandbox_loop_same_call_limit` 在**导入时固化快照**，与前者策略矛盾。统一改成 `field(default_factory=...)` 同款惰性读取。改动 1 行 + 跑 `test_sandbox.py` 的 StepGuard 用例确认。

## P2 清单（顺手时做，不单独立项）

- `_setup_file_logging` 从 `main.py` 抽成 `app/log_setup.py`（对齐 hermes 的 `hermes_logging.py` 模式）；
- `.env` 只放密钥：行为参数（超时/阈值）后续迁 config 文件（hermes 铁律，现量小不痛）；
- `app/docs/` 模块改名 `app/kb/`：与仓库根 `docs/` 语义撞车，下次大重构顺路做；
- `ruff format` 全库归一：diff 大但纯机械，选无并行改动的窗口执行；
- mypy 起步：`--ignore-missing-imports` 先行，重点覆盖 `sandbox/tools.py` 与 `console/`。

## 已知事实与陷阱（新会话避免重踩）

- 本仓库私有：`gh` CLI 本机未装，GitHub API 匿名访问 404——查 CI 结果需用户贴报错或装 `gh`；
- 本机 `python -c` subprocess 里 `bash` 会被 WSL 抢占（`C:\Windows\System32\bash.exe` 在 PATH 前），vendor 的 shell 脚本复现要用 Git Bash 全路径 `C:\Program Files\Git\usr\bin\bash.exe`；
- `pytest.ini` 无 markers 声明，`@pytest.mark.docker` 标记存在但 CI 靠 `-m "not docker"` 过滤（本地验证过 97 用例全过）；
- `config.py` 四项必填（LLM_BASE_URL 等）无默认值：任何导入 `app.config` 的脚本在无 `.env`/无环境变量时直接 ValidationError，测试外脚本需先注入；
- `test_phase_a.py` 曾用真实 LLM（本地 `.env` 有真 key），历史上它的「通过」不可重复；现已 Fake 化，勿再引入真实调用。
