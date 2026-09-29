# SandboxAgent 详细设计

> 状态：与 ADR-004 配套的实现级设计，逐点讨论确认（2026-09-23）
> 前置：ADR-004 决议不变，本文只补充「怎么实现」；冲突时以 ADR-004 决议为准
> 参考实现：hermes-agent（agent 循环）、fastclaw（沙箱边界），均为本地兄弟目录

## 0. 定案速览

| # | 设计点 | 结论 |
|---|---|---|
| 1 | LLM 调用机制 | 原生 function calling；hermes 循环骨架；上下文管理用 8k 截断 + 确定性裁剪（LLM 摘要压缩留二期） |
| 2 | TaskService 与恢复 | 新增 Task/TaskEvent 两表；create 落库+入队即返回；重启时 pending 续跑、running 标 failed |
| 3 | 容器池 | 按 repo 池化复用；**容器内 clone**（非 bind-mount）；fastclaw 超时击杀/黑名单/env 三件套照搬 |
| 4 | 状态回传 | 飞书 bot 发消息 API 按 chat_id 寻址（ADR-008 迁移后修订）+ TaskEvent 永远落库 |
| 5 | 凭据注入 | HTTPS + PAT（`REPO_{NAME}_TOKEN`）+ git url.insteadOf + **工具输出脱敏** |
| 6 | 工具接入 | **全局 ToolRegistry 单例**，启动时静态注册一次，容器经 `dispatch(..., executor=...)` ctx 注入 |

## 1. 执行循环（sandbox/agent.py）

骨架照搬 hermes-agent 的最小可行结构（hermes 9000 行的复杂度在恢复/降级，不在骨架）：

```python
async def run(self, task: Task, executor: Executor) -> str:
    messages = build_initial_messages(task, executor)   # instruction + 规范 + 工具清单
    budget = IterationBudget(settings.sandbox_max_steps)
    loop_detector = ToolLoopDetector(settings.sandbox_loop_same_call_limit)
    all_failed_rounds = 0

    while budget.remaining > 0:
        budget.consume()

        # 反死循环检查（顺序：连败摘工具 → 整轮失败摘工具，避免同轮双 nudge）
        tools = TOOLS if not self._should_strip_tools(loop_detector, all_failed_rounds) else None
        if tools is None:
            messages.append(strip_tools_nudge(...))

        result = await self._llm.chat(messages, tools=tools)
        if not result.tool_calls:                        # 无工具调用 = 任务完成
            return result.text

        round_all_failed = True
        for tc in result.tool_calls:
            output = await self._registry.dispatch(tc.name, tc.args, executor=executor)
            messages.append(tool_message(tc, output))    # 失败也转错误串回填，绝不中断循环
            loop_detector.observe(tc, output)
            if not is_failed_result(output):
                round_all_failed = False
        all_failed_rounds = all_failed_rounds + 1 if round_all_failed else 0
        # 同调用重复超限 → 注入警告 break（ADR-004 LOOP_SAME_CALL_LIMIT=3）
        if loop_detector.loop_detected:
            messages.append(loop_warning())
            continue

    return await self._force_summary(messages)           # 强制汇总（见 1.2）
```

### 1.1 LLM 客户端（sandbox/llm.py，新增类，不动 console/llm.py）

```python
@dataclass
class LLMResult:
    text: str
    tool_calls: list[ToolCall]        # name + args(dict) + id

class ChatLLMClient:
    """多轮对话 + 工具调用。sandbox_model 默认强于 console_model。"""
    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResult:
        resp = await self._client.chat.completions.create(
            model=settings.sandbox_model,
            messages=messages,
            tools=tools or openai.NOT_GIVEN,
            temperature=0.2,
        )
        ...
```

消息历史就是标准 OpenAI 格式 list（system / user / assistant+tool_calls / role=tool），
与 `ToolRegistry.get_definitions()` 的输出格式直接匹配。

### 1.2 强制汇总（预算耗尽时保底交付）

照搬 fastclaw `capReachedNudge` + hermes `MAX_ITERATIONS_SUMMARY_REQUEST`：
预算耗尽**不返回 canned 错误**，追加一条 user 消息（声明预算用尽、工具已禁用、
如实汇总已完成/未竟项、无法确认的标注 unknown），**剥掉 tools 再调一次** LLM。
合成失败则返回兜底文案（已产生的 commit + 分支名）。

### 1.3 上下文管理（一期：确定性，无 LLM 摘要）

1. 工具输出统一 8k 截断（`TOOL_OUTPUT_MAX_CHARS`，进 `app/config.py`）；
2. 循环每轮检查历史字符量，超过阈值（`SANDBOX_HISTORY_TRIM_CHARS`，默认 60k）时
   把**较旧的工具结果**替换为 `"[已裁剪]"` 占位，保留最近 6 步工具结果完整
   （hermes `prune_tool_results_only` 同款，零 LLM 调用、确定性）；
3. LLM 摘要压缩（hermes conversation_compression 同款，带防抖）留二期。

### 1.4 反死循环三重保护（阈值取 fastclaw 实测值）

| 保护 | 触发条件 | 动作 |
|---|---|---|
| 同调用重复 | 同 (工具名, 参数哈希, 结果哈希) 连续 3 次 | 注入警告消息，继续循环换路 |
| 同工具连败 | 同一工具名连败 2 次（不看参数） | 下一轮摘除全部工具 + nudge |
| 整轮全败 | 连续 3 轮所有工具都失败 | 下一轮摘除全部工具 + nudge |

失败判定保守：exec 空输出不算失败；`is_failed_result` 只认错误信封/超时标记。
摘工具后模型用已有信息作答（即自然走强制汇总路径），预算不退回。

## 2. TaskService 与持久化（sandbox/storage.py + executor.py）

### 2.1 表结构

新增两张表，复用 memory/service.py 的 SQLAlchemy async 模式（create_all + 轻量列迁移惯例），
SQLite 同库（`data/opendomino.db`）：

```python
class TaskRow(Base):
    __tablename__ = "sandbox_tasks"
    id: str (pk, uuid hex 前 8 位即可读性)
    title / instruction / repo: Text
    created_by / chat_id / session_id / trace_id: str
    status: str   # pending → running → review → done | failed | canceled
    branch_name / base_ref: str
    commits_json / verify_result / error: Text
    created_at / updated_at: Float

class TaskEventRow(Base):
    __tablename__ = "sandbox_task_events"
    id: str (pk)
    task_id / seq / stage / kind / summary / trace_id
    created_at: Float
    # (task_id, seq) 唯一索引；只追加不改
```

### 2.2 接口与并发模型

```python
class TaskService:
    async def create(self, draft: dict) -> str:
        # 落库 status=pending → 入内存队列 → 立即返回 task_id（fire-and-forget）
        ...
    async def get(self, task_id: str) -> Task | None: ...
```

- 签名与 `console/agent.py:20` 预留的 `DelegateFn` 完全对齐，**ConsoleAgent 零改动**，
  main.py 装配时把 `task_service.create` 注入即可；
- 并发：全局 `asyncio.Semaphore(sandbox_max_concurrency)` + 每 repo 一个惰性创建的
  `asyncio.Queue` worker（同仓库串行，跨仓库并行）；
- 重启恢复（lifespan 启动时执行）：
  - `status=pending` → 重新入队继续执行（尚未开始，零风险）；
  - `status=running` → 标 failed，`error="服务重启导致中断"`，照常回传终态事件
    （容器现场已随进程消失，不盲目重跑——分支上可能已有部分 commit）。

## 3. 容器池与命令执行（sandbox/container.py + workspace.py + git_ops.py）

### 3.1 池化与工作区

- 池：`dict[repo_name → 容器]` + 锁；懒创建；后台 sweeper 每 30s 扫描，
  idle 超过 `sandbox_container_idle_ttl_min`（默认 10 分钟）则 `remove(force=True)` 驱逐；
- **容器内 clone**：git clone 直接落在容器文件系统 `/workspace`（Linux 原生 I/O，
  规避 Windows Docker Desktop bind-mount 的 I/O 慢与 CRLF/filemode 噪音）；
  宿主侧只经 Docker SDK，不碰工作区文件；
- 容器保活：`containers.run(image, "tail -f /dev/null", detach=True, cpus=2, mem_limit="2g")`；
- 任务开始：`git fetch origin && git reset --hard <base_ref> && git clean -fdx`（容器内 exec）；
- 任务结束：工作区重置回基线，容器归还池；**驱逐前不做快照**（成果已 push 成分支，
  失败任务残留无保留价值；fastclaw 需要快照是因其沙箱承载聊天文件，场景不同）；
- 同仓库并发：队列串行（一个容器同一时刻只跑一个任务）。

### 3.2 命令执行与真超时（fastclaw 三层方案照搬）

SDK `exec_run(timeout=)` 只杀宿主侧 exec 进程，容器内进程仍在跑。因此：

```bash
# 包装命令：setsid 成新会话 leader，pgid 写 marker 文件
__FC_PGID=<marker> setsid -w sh -c 'echo $$ > "$__FC_PGID"; eval "$__FC_CMD"'
```

超时后**另起一个 exec**：`pgid=$(cat <marker>) && kill -KILL -$pgid`（5s 预算），
杀整个进程组（防 pytest 子进程漏杀）。marker 用随机串防并行 exec 互踩。

- 输出：`exec_run(demux=True)` 分离 stdout/stderr，合并后统一 8k 截断；
- 写文件走 **stdin 管道**而非 argv（argv 有 NUL 限制，二进制必炸，fastclaw 同款教训）。

### 3.3 安全边界

| 边界 | 实现 |
|---|---|
| 危险命令黑名单 | 五条子串（`rm -rf /`、`mkfs`、`dd if=`、fork bomb、`> /dev/sda`），toLower 后 contains，exec 前拦截 |
| env 隔离 | SDK `environment=` 显式构造（proxy 三件套 + git 凭据相关），绝不传宿主 environ |
| 端口绑定 | 预览端口只绑 `127.0.0.1` |
| 路径 | 容器内 exec 是唯一执行面，路径安全靠容器隔离本身；宿主侧无文件操作面 |
| 资源 | `cpus=2, mem_limit="2g"`（可配） |
| token 脱敏 | 所有 exec 输出回填 messages 前，把 PAT 字符串替换为 `***`（见 §5） |

## 4. 状态回传（sandbox/reporter.py + channels/registry.py）

### 4.1 回传路径

状态类通知（受理/开始/待评审/终态）为固定格式播报，**直接走 Channel 层到群**，
不经 ConsoleAgent（ADR-003 既定决议；省一次 LLM 路由，且 ConsoleAgent 三动作无一适配系统通知）。
ConsoleAgent 仍以两种方式参与：用户问「任务怎么样了」→ 拉取 TaskService 作答；
任务 done → 触发 KBase 提炼（ADR-002 触发点 3）。

### 4.2 飞书发送通道（ADR-008 迁移后修订；钉钉 session_webhook 过期问题已不存在）

任务跑 30 分钟，钉钉时代的 session_webhook（5 分钟过期）方案已随通道迁移作废。飞书通道下
本节方案简化为（定案）：

1. **唯一通道**：FeishuChannel 经 bot 发消息 API（`im/v1/messages`，`receive_id_type=chat_id`）
   按 `chat_id` 寻址发送，无过期时间、同一机器人身份、`tenant_access_token` 由 SDK 管理。
   text 消息因特殊字符被拒（错误码 230001）时降级 post 富文本重发。
2. **失败兜底**：API 调用失败 → 只落 TaskEvent。
3. **兜底兜底**：TaskEvent 表永远落库，任何情况下不丢留痕。

### 4.3 新增 channels/registry.py

`chat_id → ChannelAdapter` 注册表（ADR-004 模块结构已列）：sandbox/reporter 经它回传，
不 import 具体通道。main.py 装配时把 FeishuChannel 注册进去。

## 5. 仓库配置与凭据（sandbox/repo_config.py）

```
REPO_{NAME}_URL=git@...:team/repo.git     # 或 https URL
REPO_{NAME}_TOKEN=ghp_xxx                 # HTTPS + PAT（本次定案）
REPO_{NAME}_IMAGE=python:3.12-slim        # 缺省用 SANDBOX_DOCKER_IMAGE
REPO_{NAME}_SETUP_CMD=pip install -r requirements.txt
REPO_{NAME}_TEST_CMD=pytest -x -q
```

- 解析函数 `parse_repo_execution_config()` 照 `console/context.py` 的
  `parse_repo_registry()` 风格；沙箱不维护群→仓库绑定（ADR-003 的事），
  只按 `task.repo` 名字查执行配置；
- 凭据注入：容器内 `git config url."https://x-access-token:{TOKEN}@host/".insteadOf`
  自动带凭据；clone/push 全容器内完成；
- **脱敏**：所有 exec 输出（工具结果、diff、git log……）回填 messages 前做字符串替换，
  PAT → `***`。这是防止模型在输出里复述凭据的唯一防线（fastclaw env 事故同款教训）；
- SETUP_CMD 只在容器首次创建（clone 后）跑一次；任务重置不重跑（依赖装在
  site-packages，`git clean -fdx` 不影响）；TEST_CMD 未配置 → 验证时注明「未运行测试」；
- token 建议最小权限（仅目标仓库读写）。

## 6. 工具集（复用 ADR-006 ToolRegistry 单例 + ctx 注入容器）

**定案：全局单例静态注册一次，容器经 dispatch ctx 注入**。工具 schema/handler 全任务通用，
任务间唯一变化是 exec 到哪个容器——这正是 `dispatch(name, args, **ctx)` 的用途：

```python
# 启动时注册一次（toolset="sandbox"，共 9 个）
registry.register("run_command", "sandbox", RUN_COMMAND_SCHEMA,
                  handler=run_command_handler, is_async=True, ...)

async def run_command_handler(args: dict, executor: Executor) -> str:
    return await executor.exec(args["command"], timeout=settings.sandbox_command_timeout_seconds)

# 执行循环每次调用绑定该任务容器
output = await registry.dispatch(tc.name, tc.args, executor=task_executor)

# 给模型的定义：toolset 过滤
tools = registry.get_definitions(enabled_toolsets={"sandbox"})
```

- 工具名干净（无任务前缀）、无动态注册/注销、无并发写全局状态；
- 防误用：console 模型工具清单经 toolset 过滤不含 sandbox 工具；即使幻觉调用，
  handler 收不到 `executor` ctx 直接报错，摸不到容器；
- `executor` 是 fastclaw `Executor` 接口的 Python 版（exec / read_file / write_file /
  list_dir / close 门面），测试注入 `FakeExecutor`，无需真 Docker。

工具清单（与 ADR-004 一致）：

| 工具 | 说明 |
|---|---|
| `list_dir` / `read_file` / `grep` | 仓库探索；read_file 二进制嗅探拒绝 |
| `write_file` / `apply_patch` | 整体写（stdin 管道）/ 差量补丁 |
| `run_command` | 容器内 bash；真超时进程组击杀 + 输出截断 |
| `git_diff` / `git_log` | 查看改动与提交历史 |
| `load_skill` | 按需读 SKILL 全文（ADR-005 方式一；一期可先注册空实现） |

## 7. 评审与验证（sandbox/review.py + verify.py）

- 评审：LLM diff 预评审（对照 instruction 自审），输出三段：符合部分 / 偏离与风险 /
  建议人工关注点；push 分支后随「待评审」事件回群；
- AI 验证：独立 LLM 调用（instruction vs 最终 diff + 测试输出）→ passed / blocked；
  blocked 且预算有余 → 回炉执行循环，最多 `sandbox_verify_max_retries`（默认 2）次；
- 交付边界不变：不自动合并、不自动建 PR、不自动部署。

## 8. main.py 装配（新增部分）

```python
# lifespan 内
executor = SandboxExecutor(...)            # 容器池 + 队列 + 信号量
task_service = TaskService(memory_engine, executor, reporter)
await task_service.recover()               # pending 续跑 / running 标失败
reporter = TaskReporter(channel_registry)
agent = ConsoleAgent(memory=memory, router=router, reply=reply,
                     delegate=task_service.create)   # 原预留位接线
channel_registry.register(channel)
```

## 9. 配置契约增量（app/config.py）

```python
# ---- ADR-004 详细设计新增 ----
sandbox_history_trim_chars: int = 60000     # 历史裁剪阈值
tool_output_max_chars: int = 8000           # sandbox 工具输出截断（与 ADR-004 表一致）
sandbox_cpus: float = 2.0
sandbox_mem_limit: str = "2g"
sandbox_sweep_interval_seconds: int = 30
```

（`REPO_{NAME}_*` 与既有 `sandbox_*` 配置项已在 config.py 就位或按 §5 格式新增。）

## 10. 测试策略

- **FakeExecutor**：内存 dict 模拟 exec/read_file/write_file，可注入预设输出与失败序列；
- **FakeChatLLM**：脚本化返回序列（tool_calls → 最终 answer），覆盖：
  正常完成 / 预算耗尽强制汇总 / 同调用死循环触发警告 / 连败摘工具 / 整轮全败摘工具 /
  工具名幻觉 3-strike / 历史裁剪触发；
- **容器层**：真 Docker 集成测试单列（标记 `@pytest.mark.docker`，本地有 Docker 才跑），
  覆盖超时击杀、黑名单拦截、输出截断、PAT 脱敏；
- **TaskService**：pending 恢复入队 / running 标 failed / 同仓库串行（worker 消费顺序）。

## 11. 实施顺序建议

1. `sandbox/llm.py` + `sandbox/agent.py` 循环骨架（FakeExecutor + FakeLLM 全绿）
2. `sandbox/storage.py` TaskService + 重启恢复
3. `sandbox/container.py` 容器池 + git_ops + 真超时（docker 集成测试）
4. 工具注册（单例 + ctx）+ 脱敏
5. reporter + channels/registry + 飞书寻址回传
6. review/verify + main.py 接线（ConsoleAgent delegate 打通）
7. E2E：飞书群委派 → 容器执行 → 分支 push → 群回传

每步独立可测，1-2 步不依赖 Docker 即可先行。
