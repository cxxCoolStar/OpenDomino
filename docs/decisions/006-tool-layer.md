# ADR-006 工具层：Tool Registry

> 状态：已确认
> 日期：2026-09-23
> 前置依赖：ADR-003 ConsoleAgent、ADR-004 SandboxAgent、ADR-005 SKILL 方法层
> 关联：ADR-007 链路追踪
> 调研参考：hermes-agent（`tools/registry.py`、`tools/file_tools.py`、`tools/mcp_tool.py`）

## 背景

原文把能力分两层：**SKILL 决定「怎么做」，CLI/MCP 决定「能访问什么」**。工具层就是后者——
连接真实系统（代码、知识、协作、发布）的标准化接口，把方法落实到实际数据与操作。

原文要求「按 Agent 角色、用户授权和工具白名单注入必要能力，避免无关工具占用上下文，也避免
Agent 获得不必要的操作范围」。

技术选型已和用户确认：**工具系统参考 hermes-agent**（同为 Python、工具设计更成熟）；
**沙箱生命周期参考 fastclaw**（已在 ADR-004 落地）。

## 核心决议

1. **极简注册协议，无基类、无装饰器、无 pydantic**。模块级单例 `ToolRegistry`，一个
   `register()` 调用完成自注册。工具 = 「schema dict + handler callable + 可选 check_fn」。
2. **第一阶段用普通 Python 函数**，但抽象设计成**原生工具与 MCP 工具同构**——两者转成同一
   schema、走同一个 `register()`，后续接 MCP 时无需改 Registry。MCP 工具名加 `mcp_<server>_`
   前缀防撞名。
3. **第一阶段只有 SandboxAgent 有工具**。ConsoleAgent 是单次路由、不做工具循环，它「查记忆」
   是装配器直接调 MemoryService 服务，**不经过 ToolRegistry**。原文「按角色注入白名单」在
   第一阶段退化为「只有 SandboxAgent 的注册表即白名单」，无需独立角色系统。
4. **用户授权（OAuth）第一阶段不建**。真实平台（Aone/代码/知识/发布）第一阶段为占位，不做
   真实对接，授权体系留后续。工具调用沿用服务端配置身份，授权失效明确反馈能力不可用。
5. **统一 dispatch 边界**：所有异常在 dispatch 处归一为 `{"error": ...}`，错误体封顶，结果按
   工具配 `max_result_size_chars` 截断。
6. **toolset 分组 + 白/黑名单过滤**，为后续给 ConsoleAgent 加只读工具、按任务类型挂不同
   工具留好扩展位。
7. **check_fn 可用性探测**：工具可带探测函数，不可用则不进入给模型的 schema（带缓存 +
   抗抖动），与 ADR-005 SKILL 的 requires 预检同一思想。

## 工具定义与注册

```python
# 模块级单例
registry = ToolRegistry()

# schema：OpenAI function 格式的 dict（不含外层 type:function，dispatch 时才包）
READ_FILE_SCHEMA = {
    "name": "read_file",
    "description": "读取文本文件（带行号与分页）。用它替代 cat/head/tail。输出格式 'LINE|CONTENT'。…",
    "parameters": {
        "type": "object",
        "properties": {
            "path":  {"type": "string", "description": "文件路径"},
            "offset": {"type": "integer", "default": 1, "minimum": 1},
            "limit":  {"type": "integer", "default": 500, "maximum": 2000},
        },
        "required": ["path"],
    },
}

# handler：统一签名 (args: dict, **ctx) -> JSON 字符串
def _handle_read_file(args: dict, **ctx) -> str:
    task_id = ctx.get("task_id", "default")
    return tool_result(read_file_impl(path=args["path"], offset=args.get("offset", 1),
                                      limit=args.get("limit", 500), task_id=task_id))

# 模块导入时自注册
registry.register(name="read_file", toolset="file", schema=READ_FILE_SCHEMA,
                  handler=_handle_read_file, check_fn=_check_file_reqs,
                  max_result_size_chars=100_000)
```

`register()` 签名（参考 hermes `registry.py:763`）：

```python
def register(
    self,
    name: str,
    toolset: str,                       # 分组：file / git / sandbox / mcp-<server> …
    schema: dict,                       # OpenAI function schema
    handler: Callable,                  # (args, **ctx) -> JSON str
    check_fn: Callable = None,          # 可用性探测，False 则不进 schema
    is_async: bool = False,
    max_result_size_chars: int = None,  # 结果截断上限
    description: str = "",
):
```

辅助函数 `tool_result(payload) -> str` / `tool_error(msg) -> str` 统一序列化；`ctx` 由 dispatch
注入 `task_id` / `session_id` 等会话上下文。

## 工具的发现、注入与过滤

```text
启动：扫描工具模块 → import 触发自注册 → Registry 持有全部 ToolEntry
  ↓
SandboxAgent 起执行循环：get_definitions(enabled_toolsets=...) 取本任务工具快照
  ↓  三层过滤：① toolset 白/黑名单  ② check_fn 可用性  ③ 会话级安全门
  ↓
得到 OpenAI 格式 tool 定义列表 → 注入系统提示 / function calling
valid_tool_names 用于校验模型产出的 tool_call（乱调不存在的工具 → 回合法名清单）
```

- **check_fn 抗抖动**：结果 30s TTL 缓存 + 60s last-good 宽限，探测偶发失败不把工具从 schema
  静默剥掉（参考 hermes `registry.py:269`）。
- **Registry 维护单调 `_generation`**，下游 schema/缓存以 generation 为 key 自动失效。

## 工具的执行与 dispatch

```text
LLM 返回 tool_call
  ↓
handle_tool_call(name, args, task_id, session_id)
  ↓  参数类型强转（"42"→42，按 schema 声明）→ 校验名字合法
  ↓
registry.dispatch(name, args, task_id=..., session_id=...)
  ↓  查 entry → async 用 asyncio 桥接 → 调 handler → 结果形状归一
  ↓  异常捕获为 {"error": ...}（错误体封顶 2048 字符）
  ↓
tool_result 回传 LLM：{"role":"tool","name":...,"tool_call_id":...,"content":<json str>}
```

- **错误体统一封顶 2048 字符**，防异常文本跨轮堆积（参考 hermes `_MAX_TOOL_ERROR_CHARS`）。
- **结果按 `max_result_size_chars` 截断**；全局默认与每工具覆盖并存。
- 模型乱调不存在的工具：不执行，回一条含合法名清单的 tool message，同轮其余 tool_call 跳过。

## 第一阶段工具清单（SandboxAgent）

| 工具 | toolset | 说明 | max_result_chars |
|---|---|---|---|
| `list_dir` | file | 列目录 | 20_000 |
| `read_file` | file | 读文本，行号+分页，二进制嗅探拒绝 | 100_000 |
| `grep` | file | 内容搜索，替代 rg/grep | 50_000 |
| `write_file` | file | 整体写，返回 verified 哈希 | 20_000 |
| `apply_patch` | file | 差量补丁编辑 | 30_000 |
| `run_command` | sandbox | 容器内 bash，真超时进程组击杀 | 50_000 |
| `git_diff` | git | 查看当前改动 | 50_000 |
| `git_log` | git | 提交历史 | 20_000 |
| `load_skill` | skill | 读 SKILL 全文（ADR-005） | 50_000 |

ConsoleAgent 无工具（见核心决议 3）。

## 工具描述写作规范

工具 description 是**面向模型的行为契约**，直接决定用工具的质量。每个工具必须写清
（固化自 hermes `file_tools.py` 的写法）：

1. **替代什么原生命令**：如「用它替代 cat/head/tail」「用它替代 grep/rg/find」；
2. **输出格式**：如「输出格式 `LINE|CONTENT`」「返回 verified:true 表示落盘哈希已确认」；
3. **边界与截断行为**：如「超过 limit 时用 next_offset 续读」「二进制文件拒绝」；
4. **防无效往返提示**：如「verified:true 后别再 re-read 确认」；
5. **参数级 REQUIRED 条件**：如「REQUIRED when mode='replace'」，并在 description 内联格式示例。
6. **一致性原则**：模型看不到的描述里，不许出现它调不到的工具名。

## 模块结构

```
backend/app/tools/
├── registry.py      # ToolRegistry 单例、register/dispatch、_generation、check_fn 缓存
├── schema.py        # tool_result/tool_error 序列化、参数类型强转
├── dispatch.py      # handle_tool_call（参数强转、名校验、异常归一、截断）
├── toolsets.py      # toolset 分组定义与白/黑名单解析
├── file_tools.py    # list_dir/read_file/grep/write_file/apply_patch
├── sandbox_tools.py # run_command
├── git_tools.py     # git_diff/git_log
└── skill_tools.py   # load_skill（调 ADR-005 loader）
```

## 配置契约

```
TOOL_OUTPUT_MAX_CHARS=50000     # 全局结果截断默认
TOOL_ERROR_MAX_CHARS=2048       # 错误体封顶
TOOL_CHECK_TTL_SECONDS=30       # check_fn 结果缓存
TOOL_CHECK_GRACE_SECONDS=60     # check_fn 抗抖动宽限
```

## 非目标（第一阶段）

- **Tool Search 渐进披露**：第一阶段工具十余个，不爆 context，不做 tool_search/describe/call
  bridge（hermes 为上百工具做的瘦身，后续工具增多再引入）；
- AST 工具发现磁盘缓存：量小，直接 import；
- write_approval 写审批流：沙箱本就隔离，且评审在 push 后人工，不做写入审批闸；
- 危险命令交互式审批：第一阶段用黑名单直接拒（ADR-004），不做审批交互；
- 插件 override 授权机制；
- 真实 MCP server 对接（占位即可，抽象已同构，留前缀约定）。

## 参考实现

- Domino 原文：SKILL 与 CLI/MCP 分工、按角色+授权+白名单注入。
- hermes-agent：`tools/registry.py`（register/dispatch/_generation/check_fn 缓存）、
  `tools/file_tools.py`（schema 与 description 写法）、`tools/mcp_tool.py`（MCP 同构注册）、
  `toolsets.py`（toolset 分组）。
- fastclaw：工具按角色注入的思路（本 ADR 简化为仅 SandboxAgent）。
- ADR-003 / ADR-004 / ADR-005 / ADR-007。
