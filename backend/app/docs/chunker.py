"""Markdown 标题感知切块器（ADR-002 L3，规范契约版）。

设计依据《知识库文档撰写规范》（docs/standards/knowledge-doc-standard.md）：
规范保证「一个 ## 小节一个主题」，因此主路径就是按标题切，零启发式；
兜底只处理违规文档（超长二次切、迷你小节合并），并产生违规警告供导入报告输出。

规则映射（规范条款 → 切块行为）：
- 小节主题唯一 → ## / 独立 ### 即切分点
- 标题层级固定 → heading_path 用 # → ## → ### 三级标题链
- 代码块完整 → fenced code block 内不产生切分点
- 篇幅约束 → >800 字二次切（段落到句），<50 字并入相邻块
- 首句自包含 / 引用出处 / 术语一致 → 不影响切块，属于写作约束
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 与规范条款对应的阈值（docs/standards/knowledge-doc-standard.md 篇幅约束节）
CHUNK_MAX_CHARS = 800    # 超过必须拆子节（规范）；切块器兜底二次切
CHUNK_MIN_CHARS = 50     # 低于并入相邻小节（规范）
CHUNK_TARGET_CHARS = 600 # 二次切目标大小
CHUNK_OVERLAP_CHARS = 50 # 二次切重叠

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^(```|~~~)")


@dataclass
class Chunk:
    heading_path: str          # "ADR-002 三层记忆 > L2：KBase > 提炼流程"
    content: str               # 含标题行的块文本
    start_offset: int          # 在原文中的字符偏移（起）
    end_offset: int
    warnings: list[str] = field(default_factory=list)  # 规范违反警告


@dataclass
class DocSection:
    """一个候选块：标题链 + 该小节的原始行。"""

    levels: list[tuple[int, str]]   # [(1, "ADR-002 ..."), (2, "L2：KBase"), (3, "提炼流程")]
    lines: list[str]
    start_offset: int
    warnings: list[str] = field(default_factory=list)

    @property
    def heading_path(self) -> str:
        return " > ".join(t for _, t in self.levels)

    @property
    def heading_line(self) -> str:
        level, title = self.levels[-1]
        return f"{'#' * level} {title}"


def split_sections(text: str) -> list[DocSection]:
    """按 #/##/### 标题切分候选块。fenced code block 内的 # 行不视为标题。"""
    sections: list[DocSection] = []
    current: DocSection | None = None
    offset = 0
    in_fence = False

    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if _FENCE_RE.match(stripped):
            in_fence = not in_fence
        is_heading = False
        if not in_fence:
            m = _HEADING_RE.match(stripped)
            if m:
                is_heading = True
                if current is not None:
                    sections.append(current)
                level = len(m.group(1))
                current = DocSection(
                    levels=[(level, m.group(2).strip())],
                    lines=[line],
                    start_offset=offset,
                )
        if not is_heading:
            if current is None:
                # 文档头（首个标题前的内容，如 frontmatter/引言）归属虚拟"导言"节
                current = DocSection(levels=[(1, "导言")], lines=[], start_offset=offset)
            current.lines.append(line)
        offset += len(line)
    if current is not None:
        sections.append(current)
    return sections


def _build_heading_chain(sections: list[DocSection]) -> None:
    """把每节的一级标题展开成完整标题链（# → ## → ### 逐级下钻）。"""
    chain: list[tuple[int, str]] = []
    for sec in sections:
        level, title = sec.levels[0]
        while chain and chain[-1][0] >= level:
            chain.pop()
        chain.append((level, title))
        sec.levels = list(chain)


def _code_block_dominant(body: str) -> bool:
    """规范条款 4 推论：单个 fenced code block 占主体（>50%）的小节不做二次切分。
    切开流水线图/配置示例会使命中块失去意义——代码块原子性优先于篇幅上限。"""
    fences = re.findall(r"```[\s\S]*?```", body)
    return bool(fences) and sum(len(f) for f in fences) > len(body) * 0.5


def _split_long(sec: DocSection) -> list[DocSection]:
    """超长兜底：按空行段落切，段落不足按句切，目标 CHUNK_TARGET_CHARS，重叠 CHUNK_OVERLAP_CHARS。"""
    body = "".join(sec.lines[1:])  # 去掉标题行
    if len(body) <= CHUNK_MAX_CHARS or _code_block_dominant(body):
        return [sec]

    warnings = [
        f"小节「{sec.heading_path}」{len(body)} 字超过 {CHUNK_MAX_CHARS}，已二次切分（建议按规范拆子节）"
    ]
    # 先按空行段落聚合成大段
    paras: list[str] = []
    buf: list[str] = []
    for line in sec.lines[1:]:
        if line.strip() == "" and buf:
            paras.append("".join(buf))
            buf = []
        else:
            buf.append(line)
    if buf:
        paras.append("".join(buf))

    # 段落聚合仍超长时按句硬切
    pieces: list[str] = []
    cur = ""
    for p in paras:
        if len(p) > CHUNK_TARGET_CHARS:
            # 按句边界切
            sentences = re.split(r"(?<=[。！？.!?\n])", p)
            for s in sentences:
                if cur and len(cur) + len(s) > CHUNK_TARGET_CHARS:
                    pieces.append(cur)
                    cur = cur[-CHUNK_OVERLAP_CHARS:] if CHUNK_OVERLAP_CHARS else ""
                cur += s
        else:
            if cur and len(cur) + len(p) > CHUNK_TARGET_CHARS:
                pieces.append(cur)
                cur = cur[-CHUNK_OVERLAP_CHARS:] if CHUNK_OVERLAP_CHARS else ""
            cur += p
    if cur.strip():
        pieces.append(cur)

    out: list[DocSection] = []
    for i, piece in enumerate(pieces, 1):
        if not piece.strip():
            continue
        out.append(
            DocSection(
                levels=sec.levels,
                lines=[sec.heading_line + "\n", piece],
                start_offset=sec.start_offset,
            )
        )
        out[-1].warnings = warnings if i == 1 else []
    return out


def _merge_tiny(sections: list[DocSection]) -> list[DocSection]:
    """迷你兜底：< CHUNK_MIN_CHARS 的节并入下一个兄弟/子节（保留自己的标题行文本）。"""
    out: list[DocSection] = []
    pending: DocSection | None = None
    for sec in sections:
        body_len = len("".join(sec.lines[1:]).strip())
        if pending is not None:
            # 把前一个迷你节并到当前节头部
            sec.lines = pending.lines + sec.lines
            sec.start_offset = pending.start_offset
            sec.warnings = pending.warnings + sec.warnings
            pending = None
        if body_len < CHUNK_MIN_CHARS and len(sec.levels) > 1:
            sec.warnings.append(
                f"小节「{sec.heading_path}」仅 {body_len} 字，已并入后续小节（建议按规范并入相邻小节）"
            )
            pending = sec
            continue
        out.append(sec)
    if pending is not None:
        out.append(pending)  # 文末迷你节无处可并，保留
    return out


def chunk_markdown(text: str) -> list[Chunk]:
    """切块主入口。返回带 heading_path 与违规警告的块列表。"""
    sections = split_sections(text)
    _build_heading_chain(sections)

    # 超长二次切（在合并之前：超长块的碎片可能仍需吸收迷你节）
    expanded: list[DocSection] = []
    for sec in sections:
        expanded.extend(_split_long(sec))

    merged = _merge_tiny(expanded)

    chunks: list[Chunk] = []
    for sec in merged:
        content = "".join(sec.lines).strip()
        if not content:
            continue
        chunks.append(
            Chunk(
                heading_path=sec.heading_path,
                content=content,
                start_offset=sec.start_offset,
                end_offset=sec.start_offset + len(content),
                warnings=sec.warnings,
            )
        )
    return chunks


def collect_warnings(chunks: list[Chunk]) -> list[str]:
    """汇总全文的规范违反警告（导入报告用）。"""
    seen: set[str] = set()
    out: list[str] = []
    for c in chunks:
        for w in c.warnings:
            if w not in seen:
                seen.add(w)
                out.append(w)
    return out
