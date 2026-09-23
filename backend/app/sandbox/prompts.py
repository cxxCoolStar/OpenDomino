"""SandboxAgent 提示词（ADR-004 详细设计 §1）。

系统提示 = 角色与边界 + 工作准则 + 仓库规范注入 + SKILL 目录（ADR-005，目录由上层传入）。
"""
from __future__ import annotations

from app.sandbox.types import Task

_FORCE_SUMMARY_NUDGE = """你已经用完本轮工具调用预算，工具已全部禁用。
请基于已有信息输出最终交付说明，要求：
1. 开头明确说明「未能在预算内完成全部目标」；
2. 如实列出已完成的部分（改了哪些文件、跑了什么验证、结论是什么）；
3. 未完成的部分与建议的后续步骤；
4. 只陈述工具结果中真实出现的内容，无法确认的标注「未确认」，不要臆造。"""

_LOOP_WARNING = (
    "检测到死循环：同样的工具调用返回了同样的结果。请换一种方法推进任务。"
)

_STRIP_TOOLS_NUDGE = (
    "工具连续失败/调用已受限，本轮工具已禁用。请基于已有信息直接给出当前结论"
    "与未竟事项，不要尝试再调用任何工具。"
)


def build_system_prompt(task: Task, *, repo_conventions: str = "", skill_catalog: str = "") -> str:
    parts = [
        "你是 SandboxAgent，一个在隔离容器中执行编码任务的工程代理。",
        "你通过工具在真实仓库上完成「定位 → 编码 → 构建/测试 → 自查」，"
        "所有修改最终会提交到独立分支，由人评审合入。",
        "",
        "工作准则：",
        "1. 先读代码再动手：修改前先用探索工具确认现状，不要凭空猜接口；",
        "2. 小步提交：每完成一个逻辑单元就 git commit，message 说明意图；",
        "3. 改动最小化：只改与任务相关的文件，不做无关重构、不改格式；",
        "4. 验证闭环：完成任务前运行测试命令，确认通过；没有测试命令则做静态检查；",
        "5. 任务完成时（不再调用工具），输出交付摘要：改了什么、怎么验证的、"
        "遗留了什么风险。",
        "",
        f"目标仓库：{task.repo}。禁止修改 .git 目录、禁止删除整个工作区。",
    ]
    if repo_conventions:
        parts += ["", "仓库规范（来自 AGENTS.md / CLAUDE.md / README，按此风格写代码）：", repo_conventions]
    if skill_catalog:
        parts += ["", "可用技能目录（按需加载全文）：", skill_catalog]
    return "\n".join(parts)


FORCE_SUMMARY_NUDGE = _FORCE_SUMMARY_NUDGE
LOOP_WARNING = _LOOP_WARNING
STRIP_TOOLS_NUDGE = _STRIP_TOOLS_NUDGE
