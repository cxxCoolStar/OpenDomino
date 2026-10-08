"""SKILL 方法层测试（ADR-005）：registry / preflight / catalog / loader。

全部用 tmp_path 临时技能目录，不碰真实 skills/。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.skills.catalog import build_catalog
from app.skills.loader import load_skill
from app.skills.preflight import check_requires, _sync_check
from app.skills.registry import SkillMeta, parse_frontmatter, scan_skills

# ---------- 测试技能目录 ----------

FULL_SKILL = """---
name: test-skill
description: 测试技能。当需要验证 ADR-005 加载链路时使用。
metadata:
  always: false
  requires:
    tools: [read_file, grep]
    env: []
    bins: [python]
---

# test-skill

## 执行步骤
1. 读代码
2. 改代码
"""

ALWAYS_SKILL = """---
name: core-skill
description: 核心技能，全文内联。
metadata:
  always: true
  requires:
    tools: []
    env: []
    bins: []
---

# core-skill
内联全文内容。
"""

NO_REQUIRES_SKILL = """---
name: minimal-skill
description: 极简技能，无 requires。
---

# minimal-skill
正文。
"""


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    d = tmp_path / "skills"
    for name, content in [
        ("test-skill", FULL_SKILL),
        ("core-skill", ALWAYS_SKILL),
        ("minimal-skill", NO_REQUIRES_SKILL),
    ]:
        sub = d / name
        sub.mkdir(parents=True)
        (sub / "SKILL.md").write_text(content, encoding="utf-8")
    return d


# ---------- parse_frontmatter ----------


def test_parse_frontmatter_full():
    meta, body = parse_frontmatter(FULL_SKILL)
    assert meta["name"] == "test-skill"
    assert meta["metadata"]["always"] is False
    assert meta["metadata"]["requires"]["tools"] == ["read_file", "grep"]
    assert body.lstrip().startswith("# test-skill")


def test_parse_frontmatter_missing():
    meta, body = parse_frontmatter("没有 frontmatter 的文本")
    assert meta == {}
    assert "frontmatter" in body


def test_parse_frontmatter_incomplete():
    meta, _ = parse_frontmatter("---\nname: x\n没有闭合分隔符")
    assert meta == {}


# ---------- scan_skills ----------


def test_scan_skills_reads_all(skills_dir: Path):
    skills = {s.name: s for s in scan_skills(skills_dir)}
    assert set(skills) == {"test-skill", "core-skill", "minimal-skill"}
    assert skills["test-skill"].requires_bins == ["python"]
    assert skills["core-skill"].always is True
    assert skills["test-skill"].path is not None and skills["test-skill"].path.parent.name == "test-skill"


def test_scan_skills_missing_dir(tmp_path: Path):
    assert scan_skills(tmp_path / "nope") == []


def test_scan_skills_skips_dirs_without_skill_md(tmp_path: Path):
    d = tmp_path / "skills"
    (d / "empty").mkdir(parents=True)
    assert scan_skills(d) == []


def test_catalog_line_truncates_description():
    meta = SkillMeta(name="x", description="长" * 200)
    line = meta.catalog_line()
    assert len(line) <= len("- x: ") + 140 + 1  # 140 字 + 省略号
    assert line.endswith("…")


# ---------- preflight ----------


@pytest.mark.asyncio
async def test_check_requires_all_pass():
    meta = SkillMeta(name="t", requires_tools=["read_file"], requires_bins=["python"])
    assert await check_requires(meta, available_tools={"read_file", "write_file"}) is None


@pytest.mark.asyncio
async def test_check_requires_missing_tool_and_bin():
    async def no_probe(bin_name: str) -> bool:
        return False

    meta = SkillMeta(name="t", requires_tools=["grep"], requires_bins=["rg"])
    out = await check_requires(meta, available_tools={"read_file"}, bin_probe=no_probe)
    assert out is not None
    assert "tools: grep" in out
    assert "bins: rg" in out
    assert "UNAVAILABLE" in out


def test_sync_check_missing_env(monkeypatch):
    monkeypatch.delenv("DEFINITELY_NOT_SET_VAR_XYZ", raising=False)
    meta = SkillMeta(name="t", requires_env=["DEFINITELY_NOT_SET_VAR_XYZ"])
    out = _sync_check(meta, available_tools=None)
    assert out is not None and "env: DEFINITELY_NOT_SET_VAR_XYZ" in out


# ---------- catalog ----------


def test_build_catalog_progressive_disclosure(skills_dir: Path):
    catalog, always = build_catalog(scan_skills(skills_dir))
    # 目录只有 name + 描述
    assert "test-skill: 测试技能" in catalog
    assert "执行步骤" not in catalog
    # always 技能全文内联，其余不内联
    assert "内联全文内容" in always
    assert "test-skill" not in always


def test_build_catalog_empty(tmp_path: Path):
    assert build_catalog([]) == ("", "")
    assert build_catalog(scan_skills(tmp_path / "nope")) == ("", "")


# ---------- load_skill ----------


@pytest.mark.asyncio
async def test_load_skill_returns_body(skills_dir: Path):
    out = await load_skill("test-skill", skills=scan_skills(skills_dir))
    assert "内部技能上下文" in out
    assert "# test-skill" in out
    assert "name:" not in out.split("# test-skill")[0].split("内部技能上下文")[1]  # 正文不含 frontmatter


@pytest.mark.asyncio
async def test_load_skill_unknown():
    out = json.loads(await load_skill("nope", skills=[]))
    assert "error" in out and "未知技能" in out["error"]


@pytest.mark.asyncio
async def test_load_skill_preflight_fail():
    async def no_probe(bin_name: str) -> bool:
        return False

    skills = scan_skills((skills_dir_dir := None) or Path("skills")) if False else None
    # 直接构造 requires 缺 bins 的技能
    meta = SkillMeta(name="needs-node", requires_bins=["definitely-not-a-bin-xyz"],
                     path=Path("unused"))
    out = json.loads(await load_skill("needs-node", skills=[meta], bin_probe=no_probe))
    assert "error" in out and "UNAVAILABLE" in out["error"]


@pytest.mark.asyncio
async def test_load_skill_end_to_end_with_tmpdir(skills_dir: Path):
    """全链路：磁盘扫描 → 预检 → 读正文。"""
    skills = scan_skills(skills_dir)
    out = await load_skill("core-skill", available_tools={"read_file"}, skills=skills)
    assert "core-skill" in out and "内联全文内容" in out
