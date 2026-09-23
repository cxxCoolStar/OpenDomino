# ADR-005 SKILL 方法层

> 状态：已确认
> 日期：2026-09-23
> 前置依赖：ADR-002 三层记忆（SKILL 与 KBase 分工）、ADR-004 SandboxAgent
> 关联：ADR-006 工具层
> 调研参考：fastclaw（`internal/agent/skills.go`、`internal/agent/tools/load_skill.go`、`skills/`）

## 背景

通用模型会写 React、会解释 TypeScript，但不知道团队隐性知识：这个项目属于哪套框架、请求
怎么封装、中文文案存在哪、改完怎么验证。这些稳定可复用的经验，原文提炼为内置 SKILL。

原文定位：**SKILL 决定「怎么做」，CLI/MCP 决定「能访问什么」**。SKILL 不是万能提示词，而是
「带触发条件、执行步骤、工具选择和验收标准的工程方法」；Agent 只在命中场景时加载对应内容，
避免每轮对话都携带所有规范。

ADR-002 已定了 SKILL 与 KBase 的分工（流程/工作方法/踩坑 → SKILL；团队事实/长期约定 →
KBase），但**没定 SKILL 怎么被选中加载**——这是本 ADR 要解决的核心问题。

## 核心决议

1. **SKILL 是「方法」，不是工具也不是提示词**。SKILL 是磁盘上的 Markdown 文档
   （frontmatter + 步骤 + 指南）+ 可选脚本，**本身不是 function call**。模型读 SKILL 全文后，
   组合现有工具去执行。
2. **选中加载用方式一：模型自主**。系统提示注入 SKILL 目录（name + 一句话描述），模型在
   Agentic 循环里自己判断命中场景时调 `load_skill` 读全文。这是 fastclaw 生产验证的方式，
   也是主流 Agent 的通用做法。**不**额外加路由判定层（避免「路由给建议 + 模型再自主」两套
   并存的复杂度与排查困难）。
3. **渐进式披露，防上下文膨胀**。目录只放 name + 一句话描述（截首句、上限 ~140 字符）；
   `always:true` 的核心技能才内联全文；全文按需 `load_skill` 加载。SKILL 再多也不爆上下文。
4. **frontmatter 声明 + 加载期可用性预检**。加载时求值 `requires{env,bins,tools}`，缺失则前置
   `[SKILL CURRENTLY UNAVAILABLE: ...]`，把「装了但跑不了」从运行期瞎找提前为加载期明示
   （fastclaw 教训：模型曾烧 19 分钟 `find /` 找一个没装的二进制）。
### 作用域与治理

5. **SKILL 只在 SandboxAgent 生效**。ConsoleAgent 是单次路由、不进真实仓库，不加载 SKILL；
   SKILL 的「执行步骤」语义天然属于执行侧。
6. **第一阶段手工维护内置 SKILL**，复刻原文的核心技能；不做 skill-creator / find-skills 自动
   生态，不做 SKILL 市场。
7. **文件系统目录存储，git 管理版本**，不做数据库存储。

## SKILL 文件格式

一个 SKILL = 一个目录，含 `SKILL.md` + 可选脚本/资源。直接采用社区通用格式
（与 fastclaw、本机 `~/.agents/skills` 一致）：

```markdown
---
name: mq-frontend
description: 消息控制台前端研发总入口。当需要开发/修改消息控制台前端页面、识别工程规范时使用。
metadata:
  always: false
  requires:
    tools: [read_file, write_file, apply_patch, run_command]
    env: []
    bins: [node]
---

# mq-frontend

## 何时触发
...

## 执行步骤
...

## 工程约束（TypeScript / 组件复用 / 请求封装 / 样式 / 国际化 / 验证）
...

## 验收标准
...
```

frontmatter 字段：

| 字段 | 含义 |
|---|---|
| `name` | 技能唯一名 |
| `description` | 一句话，**说明什么时候用**（模型自主判断的依据，必须写清触发场景） |
| `metadata.always` | true 则全文内联进系统提示（仅核心少数技能） |
| `metadata.requires.tools` | 依赖的工具，缺失则预检不通过 |
| `metadata.requires.env` | 依赖的环境变量 |
| `metadata.requires.bins` | 依赖的可执行文件（沙箱内探测） |

## 加载机制

```text
系统提示注入：
  <skill_catalog>
    - mq-frontend: 消息控制台前端研发总入口。当需要…（一句话）
    - find-i18n-key: 从页面中文反查真实源码。当用户…
    - ai-testing: 让「页面看起来没问题」变成可执行验证。当…
  </skill_catalog>
  <always_loaded_skills>
    （仅 always:true 技能的全文）
  </always_loaded_skills>

执行循环中：
  模型判断命中某技能 → 调 load_skill(name) 工具
    → 预检 requires（缺 env/bins/tools？）
    → 通过：返回 SKILL.md 全文
    → 不通过：前置 [SKILL CURRENTLY UNAVAILABLE: 缺 bins: node]，
             并提示「不要满盘找/装，换方法或告知用户」
  模型据全文组合工具执行
```

- 目录里 `description` 的质量直接决定模型自主判断的准确性，**写作时必须把「什么时候用」
  写具体**，这是方式一成功的关键。
- `load_skill` 返回内容包头声明「这是内部上下文，禁止逐字转贴给用户」。

## SKILL 与相关层的边界

| 维度 | SKILL | KBase（L2） | 工具（ADR-006） |
|---|---|---|---|
| 是什么 | 工程方法（怎么做） | 团队事实（是什么/约定了什么） | 能力接口（能访问什么） |
| 形态 | Markdown 文档 + 脚本 | 向量记忆条目 | function call |
| 例子 | 「改 i18n 文案的完整流程」 | 「我们 i18n key 用 xxx 命名约定」 | `find_i18n_key` 反查接口 |
| 加载 | 模型命中场景时 load_skill | 检索召回 confirmed 条目 | 按角色白名单注入 |

- 具体工作流程、工具操作步骤、踩坑 → **SKILL**；
- 团队事实、决策、长期约定 → **KBase**（ADR-002）；
- SKILL 内部可以**调用**工具，但工具的实现归 ADR-006。

## 第一阶段内置 SKILL（复刻原文）

| SKILL | 对应原文 | 作用 |
|---|---|---|
| `brainstorming` | /brainstorming | 复杂实现前澄清目标、约束、验收标准 |
| `mq-frontend` | mq-frontend-skill | 识别工程规范，按项目规范开发 |
| `find-i18n-key` | find-i18n-key | 从页面中文反查源码定位 |
| `medusa` | medusa | 国际化文案变更纳入交付 |
| `ai-testing` | ai-testing | 生成可执行的页面验证指令 |

第一阶段做这 5 个的精简版；medusa / ai-testing 依赖的外部平台（Aone/SQA）以接口占位，
不实现真实对接。

## SKILL 在沙箱中的安全

- SKILL 目录**只读挂载**进容器 `/skills/<name>/`（参考 fastclaw）；
- 第一阶段 SKILL 是公开方法文档，**不**像 fastclaw 那样物理排除 SKILL.md 防 IP 泄露
  （OpenDomino 的 SKILL 非涉密 IP）；若后续 SKILL 含敏感信息再启用该防护。

## 模块结构

```
backend/app/skills/
├── registry.py     # SKILL 发现与目录构建（扫描 skills/ 目录、读 frontmatter）
├── loader.py       # load_skill 工具实现（读全文 + requires 预检）
├── preflight.py    # requires{env,bins,tools} 可用性检查
└── catalog.py      # 渐进式披露的目录注入（name+一句话 / always 全文）
skills/             # 内置 SKILL 目录（git 管理）
├── brainstorming/SKILL.md
├── mq-frontend/SKILL.md
├── find-i18n-key/SKILL.md
├── medusa/SKILL.md
└── ai-testing/SKILL.md
```

## 配置契约

```
SKILLS_DIR=skills            # SKILL 根目录
SKILLS_DESC_MAX_CHARS=140    # 目录中 description 截断上限
```

## 非目标（第一阶段）

- skill-creator / find-skills 自动技能生态、技能市场；
- 路由判定式加载（方式二）；
- SKILL 的 IPC/分级权限、敏感 SKILL 物理排除；
- 数据库存储与多副本技能同步（workspaceStore hydrate）。

## 参考实现

- Domino 原文：SKILL 与 CLI/MCP 分工、命中才加载、5 个内置 SKILL。
- fastclaw：`internal/agent/skills.go`（BuildSkillsSummary 渐进披露）、
  `internal/agent/tools/load_skill.go`（requires 预检、UNAVAILABLE 前置）、`skills/*/SKILL.md`。
- ADR-002（SKILL 与 KBase 分工）、ADR-004（Agentic 循环）、ADR-006（工具）。
