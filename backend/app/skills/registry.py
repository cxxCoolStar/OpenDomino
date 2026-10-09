"""SKILL 发现与目录构建（ADR-005）。

SKILL 是「方法」不是工具也不是提示词：磁盘上一个目录 = 一个技能
（SKILL.md frontmatter + 正文 + 可选脚本）。registry 负责扫描
settings.skills_dir、解析 frontmatter、构建渐进式披露用的目录条目。

frontmatter 解析不引第三方 YAML 依赖：本项目的技能 frontmatter 是
「key: value + 两层嵌套 requires」的固定形状，手写解析即可，与
docs/import_docs.py 的既有做法一致。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)

_DESCRIPTION_MAX_CHARS = 140  # ADR-005：目录中 description 截断上限（与 SKILLS_DESC_MAX_CHARS 一致）

# frontmatter 行：`key: value` / `key:`（块开始）
_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")
# 嵌套块行：≥2 空格缩进的 `sub_key: value`（缩进层级区分 requires/metadata 与其子键）
_SUB_KEY_RE = re.compile(r"^\s{2,}([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")
# 列表项：`[a, b]` 内联或 `- a` 逐行
_INLINE_LIST_RE = re.compile(r"^\[(.*)\]$")


@dataclass
class SkillMeta:
    """一个 SKILL 的目录元数据（frontmatter 解析结果）。"""

    name: str
    description: str = ""
    always: bool = False
    requires_tools: list[str] = field(default_factory=list)
    requires_env: list[str] = field(default_factory=list)
    requires_bins: list[str] = field(default_factory=list)
    # 磁盘路径（loader 读全文用；registry 不持有内容）
    path: Path | None = None

    def catalog_line(self) -> str:
        """渐进式披露的目录行：name + 截断后的一句话描述。"""
        desc = self.description.strip()
        if len(desc) > _DESCRIPTION_MAX_CHARS:
            desc = desc[:_DESCRIPTION_MAX_CHARS] + "…"
        return f"- {self.name}: {desc}"


def _split_list(value: str) -> list[str]:
    """`[a, b]` 或 `a, b` → 列表；空串 → 空列表。"""
    value = value.strip()
    if not value:
        return []
    m = _INLINE_LIST_RE.match(value)
    if m:
        value = m.group(1)
    return [item.strip().strip("'\"") for item in value.split(",") if item.strip()]


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 SKILL.md 头部 frontmatter，返回 (元数据 dict, 正文)。

    支持的形状（ADR-005 文件格式节）：顶层标量 + requires 块下三层列表。
    无 frontmatter / 分隔符不完整 → 返回空 dict 与原文。
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n---", 1)
    if len(parts) < 2:
        return {}, text
    header_lines = parts[0].split("\n")[1:]  # 去掉首行 ---
    body = parts[1].split("\n", 1)[1] if "\n" in parts[1] else ""

    meta: dict = {}
    current_block: dict | None = None
    block_indent = 0   # 当前块的缩进深度（0=顶层 meta；2=metadata/requires；4=其嵌套子块）
    in_list: str | None = None  # 当前逐行列表归属的键
    for line in header_lines:
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        # 逐行列表项：`  - item`（缩进 + 短横线）
        list_item = re.match(r"^\s{2,}-\s+(.*)$", line)
        if list_item and current_block is not None and in_list:
            current_block[in_list].append(list_item.group(1).strip().strip("'\""))
            continue
        sub = _SUB_KEY_RE.match(line)
        if sub and current_block is not None:
            key, value = sub.group(1), sub.group(2)
            if not value:
                # 嵌套块键（requires: / metadata:）：缩进比当前块深 → 在当前块里建子块并下探
                current_block[key] = {}
                current_block = current_block[key]
                block_indent = indent
                in_list = None
                continue
            # 叶子值：标量（true/false）或列表，写入当前块
            if value in ("true", "false"):
                current_block[key] = value == "true"
            elif value.startswith("[") or "," in value:
                current_block[key] = _split_list(value)
            else:
                current_block[key] = value.strip("'\"")
            in_list = None
            continue
        top = _KEY_RE.match(line)
        if top and indent == 0:
            key, value = top.group(1), top.group(2).strip()
            if not value:
                # 顶层块键：metadata / requires
                current_block = meta.setdefault(key, {})
                block_indent = 0
            else:
                if value in ("true", "false"):
                    meta[key] = value == "true"
                else:
                    meta[key] = value.strip("'\"")
                current_block = meta  # 顶层标量后，缩进行仍归属最近的顶层块
            in_list = None
    return meta, body


def _meta_from_dict(data: dict, path: Path) -> SkillMeta:
    """frontmatter dict → SkillMeta。兼容两种形状：

    - 嵌套：metadata.requires.{tools,env,bins}（社区通用格式，本仓库内置技能用此）
    - 扁平：顶层 requires.{tools,env,bins}（ADR-005 文档示例的简写）
    """
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    requires = metadata.get("requires") or data.get("requires") or {}
    if not isinstance(requires, dict):
        requires = {}
    name = str(data.get("name") or path.parent.name)
    description = str(data.get("description") or "")
    always = bool(metadata.get("always") or data.get("always") or False)
    return SkillMeta(
        name=name,
        description=description,
        always=always,
        requires_tools=[str(t) for t in (requires.get("tools") or [])],
        requires_env=[str(e) for e in (requires.get("env") or [])],
        requires_bins=[str(b) for b in (requires.get("bins") or [])],
        path=path,
    )


def scan_skills(skills_dir: str | Path | None = None) -> list[SkillMeta]:
    """扫描技能根目录，返回全部有效技能的元数据。

    目录不存在 / 无有效技能 → 空列表（SKILL 层整体可缺省，不影响任务执行）。
    name 缺失时回退目录名；description 缺失仍然收录（目录行会缺描述，
    便于发现问题而不是静默吞掉）。
    """
    root = Path(skills_dir) if skills_dir else Path(settings.skills_dir)
    if not root.is_dir():
        log.info("skills dir not found: %s", root)
        return []
    skills: list[SkillMeta] = []
    for skill_md in sorted(root.glob("*/SKILL.md")):
        try:
            meta_dict, _ = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        except OSError:
            log.warning("skill file unreadable: %s", skill_md)
            continue
        meta = _meta_from_dict(meta_dict, skill_md)
        skills.append(meta)
    return skills
