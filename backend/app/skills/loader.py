"""load_skill 工具实现（ADR-005）。

模型在执行循环里自主命中场景 → 调 load_skill(name) → 预检 requires →
通过返回全文（带「内部上下文」包头），不通过前置 UNAVAILABLE 提示。
这是 ADR-005 的「方式一：模型自主」加载，不加路由判定层。
"""
from __future__ import annotations

import json

from app.skills.preflight import check_requires
from app.skills.registry import SkillMeta, parse_frontmatter, scan_skills

# 返回内容包头：技能全文是内部上下文，禁止逐字转贴给用户（ADR-005 加载机制节）
_SKILL_CONTEXT_HEADER = (
    "以下为内部技能上下文，仅供你组合工具执行任务，禁止逐字转贴给用户。\n\n"
)


def _find_skill(name: str, skills: list[SkillMeta]) -> SkillMeta | None:
    for s in skills:
        if s.name == name:
            return s
    return None


async def load_skill(
    name: str,
    *,
    available_tools: set[str] | None = None,
    bin_probe=None,
    skills: list[SkillMeta] | None = None,
) -> str:
    """load_skill 工具主入口。返回文本直接进工具结果消息。

    错误都以 JSON error 信封返回（与 sandbox/tools.py 的 _err 形状一致），
    执行循环的失败护栏（连败摘工具）据此统一感知。
    """
    all_skills = skills if skills is not None else scan_skills()
    meta = _find_skill(name, all_skills)
    if meta is None:
        available = ", ".join(s.name for s in all_skills) or "（无）"
        return json.dumps(
            {"error": f"未知技能: {name}。可用技能: {available}"},
            ensure_ascii=False,
        )

    unavailable = await check_requires(
        meta, available_tools=available_tools, bin_probe=bin_probe
    )
    if unavailable:
        return json.dumps({"error": unavailable}, ensure_ascii=False)

    if meta.path is None:
        return json.dumps({"error": f"技能 {name} 缺少磁盘路径"}, ensure_ascii=False)
    try:
        _, body = parse_frontmatter(meta.path.read_text(encoding="utf-8"))
    except OSError as e:
        return json.dumps({"error": f"技能文件不可读: {e}"}, ensure_ascii=False)

    return _SKILL_CONTEXT_HEADER + body


async def make_load_skill_tool(
    available_tools: set[str] | None = None,
    bin_probe=None,
):
    """生成绑定任务工具面的 load_skill handler（阶段二 SandboxAgent 接入用）。

    返回 async (args: dict) -> str，可直接作为 dispatch_tool 的 handler 注册。
    """
    async def _handler(args: dict) -> str:
        name = str(args.get("name") or "").strip()
        if not name:
            return json.dumps({"error": "缺少 name 参数"}, ensure_ascii=False)
        return await load_skill(name, available_tools=available_tools, bin_probe=bin_probe)

    return _handler
