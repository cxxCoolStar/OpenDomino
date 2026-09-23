"""上下文装配器（ADR-003）：并行召回三层，注入会话窗口/任务/仓库/pending，控预算。"""
from __future__ import annotations

import asyncio

from app.config import settings
from app.memory.types import MemoryService, SessionContext

_REPO_BINDINGS: dict[str, str] = {}  # chat_id -> repo_name（群关联仓库，ADR-003）


def bind_repo(chat_id: str, repo: str) -> None:
    _REPO_BINDINGS[chat_id] = repo


def get_repo(chat_id: str) -> str:
    return _REPO_BINDINGS.get(chat_id, "")


def parse_repo_registry() -> dict[str, str]:
    """解析 REPO_REGISTRY：name=url|desc;... -> {name: desc}"""
    out: dict[str, str] = {}
    for item in (settings.repo_registry or "").split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, rest = item.split("=", 1)
        desc = rest.split("|", 1)[1] if "|" in rest else rest
        out[name.strip()] = desc.strip()
    return out


class ContextAssembler:
    def __init__(self, memory: MemoryService) -> None:
        self._memory = memory

    async def assemble(self, session: SessionContext, query: str) -> str:
        kbase, knowledge = await asyncio.gather(
            self._memory.recall_kbase(query, limit=settings.console_kbase_limit),
            self._memory.search_knowledge(query, limit=settings.console_knowledge_limit),
            return_exceptions=True,
        )
        parts: list[str] = []

        # 群关联仓库
        repo = get_repo(session.chat_id)
        registry = parse_repo_registry()
        if repo:
            parts.append(f"[群关联仓库] 默认仓库：{repo}（{registry.get(repo, '')}）")
        elif registry:
            opts = "；".join(f"{n}({d})" for n, d in registry.items())
            parts.append(f"[可选仓库] {opts}")

        # KBase
        if isinstance(kbase, list) and kbase:
            lines = "\n".join(
                f"- 【团队经验】{h.title}：{h.statement}" + (f"（原因：{h.rationale}）" if h.rationale else "")
                for h in kbase
            )
            parts.append(f"[团队经验 KBase]\n{lines}")

        # 知识库
        if isinstance(knowledge, list) and knowledge:
            lines = "\n".join(
                f"- 【正式文档】《{k.doc_title}》{k.heading_path}：{k.content[:300]}"
                for k in knowledge
            )
            parts.append(f"[正式文档 知识库]\n{lines}")

        # 群内进行中任务
        if session.active_task_ids:
            parts.append(f"[群内进行中任务] {', '.join(session.active_task_ids)}")

        # 待澄清
        if session.pending and session.pending.kind == "clarify":
            q = session.pending.payload.get("question", "")
            parts.append(f"[待澄清问题] 你之前问过：{q}\n请判断用户本条消息是回答它、新话题还是取消。")

        # 会话窗口
        window = session.messages[-settings.console_context_window:]
        if window:
            lines = "\n".join(
                f"{'用户' if m.role == 'user' else 'Domino'}({m.user_name or m.user_id}): {m.text[:500]}"
                for m in window
            )
            parts.append(f"[群聊上下文]\n{lines}")

        return self._fit_budget("\n\n".join(parts))

    @staticmethod
    def _fit_budget(text: str) -> str:
        budget = settings.console_max_context_chars
        if len(text) <= budget:
            return text
        return text[:budget] + "\n…（上下文超出预算，已截断）"
