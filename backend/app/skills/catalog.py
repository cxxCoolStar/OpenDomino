"""渐进式披露的目录注入（ADR-005）。

系统提示只放 name + 一句话描述（目录）；always:true 的核心技能内联全文；
其余按需 load_skill 加载。SKILL 再多也不爆上下文。
"""
from __future__ import annotations

from app.skills.registry import SkillMeta, scan_skills


def build_catalog(skills: list[SkillMeta] | None = None) -> tuple[str, str]:
    """构建 (skill_catalog, always_loaded) 两段文本。

    skill_catalog：<skill_catalog> 块内容，每技能一行；
    always_loaded：<always_loaded_skills> 块内容，仅 always:true 技能全文。
    无技能时返回 ("", "")——调用方据此跳过注入，系统提示不留空块。
    """
    if skills is None:
        skills = scan_skills()
    if not skills:
        return "", ""

    catalog_lines = [s.catalog_line() for s in skills]
    catalog = "<skill_catalog>\n" + "\n".join(catalog_lines) + "\n</skill_catalog>"

    always_blocks = []
    for s in skills:
        if not s.always or s.path is None:
            continue
        try:
            full = s.path.read_text(encoding="utf-8")
        except OSError:
            continue
        always_blocks.append(f"<skill name=\"{s.name}\">\n{full}\n</skill>")
    always_loaded = ""
    if always_blocks:
        always_loaded = "<always_loaded_skills>\n" + "\n\n".join(always_blocks) + "\n</always_loaded_skills>"

    return catalog, always_loaded
