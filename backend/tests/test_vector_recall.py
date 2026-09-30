"""向量召回集成测试（ADR-002 Phase C）：真实 embedding API + 临时 Chroma。

覆盖三级召回的切换逻辑与语义命中；无 EMBEDDING_MODEL 时跳过。
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.memory.embedding import ArkEmbeddingClient, NullEmbeddingClient
from app.memory.service import SQLiteMemoryService
from app.memory.types import MemoryDraft

pytestmark = pytest.mark.skipif(
    not settings.embedding_model, reason="EMBEDDING_MODEL not configured"
)


@pytest.fixture
async def vmem(tmp_path, monkeypatch):
    """独立 SQLite + 临时 Chroma 目录 + 阈值压低的向量召回服务。"""
    monkeypatch.setattr(settings, "memory_db_url", f"sqlite+aiosqlite:///{tmp_path}/t.db")
    monkeypatch.setattr(settings, "chroma_persist_dir", str(tmp_path / "chroma"))
    monkeypatch.setattr(settings, "kbase_full_load_threshold", 2)  # 强制走召回分支
    svc = SQLiteMemoryService(
        db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db",
        embedding=ArkEmbeddingClient(),
    )
    await svc.init()
    return svc


async def _confirm(svc: SQLiteMemoryService, title: str, statement: str) -> str:
    mid = await svc.create_memory_draft(MemoryDraft(title=title, statement=statement))
    await svc.confirm_memory(mid, "tester")
    return mid


@pytest.mark.asyncio
async def test_vector_recall_semantic_match(vmem):
    """语义相关但无公共子串的查询应命中（关键词召回的经典 miss 场景）。"""
    await _confirm(vmem, "代码合并前必须通过全部测试", "代码合并前必须通过全部测试，包括单元测试和集成测试")
    await _confirm(vmem, "开发环境端口", "开发环境端口是 8000")
    await _confirm(vmem, "代码评审", "代码评审必须至少两人参与")

    # "提交代码" 与 "代码合并" 无公共子串，向量应命中第一条
    hits = await vmem.recall_kbase("提交代码前要做什么", limit=3)
    assert hits, "向量召回应有结果"
    assert any("测试" in h.statement for h in hits), f"应语义命中合并测试条目: {[h.title for h in hits]}"


@pytest.mark.asyncio
async def test_vector_recall_only_confirmed(vmem):
    """pending 草稿不进 Chroma，不参与召回。"""
    await _confirm(vmem, "已确认条目", "已确认的约定内容")
    await vmem.create_memory_draft(MemoryDraft(title="未确认草稿", statement="还没确认的内容"))

    hits = await vmem.recall_kbase("还没确认的内容", limit=5)
    assert all(h.statement != "还没确认的内容" for h in hits)


@pytest.mark.asyncio
async def test_delete_removes_from_vector_index(vmem):
    """删除条目后向量召回不再命中。"""
    mid = await _confirm(vmem, "临时约定", "稍后会被删除的独特约定内容")
    await vmem.delete_memory(mid)

    hits = await vmem.recall_kbase("独特约定内容", limit=5)
    assert all(h.id != mid for h in hits)


@pytest.mark.asyncio
async def test_full_load_threshold_still_wins_below_limit(vmem, monkeypatch):
    """条目数 ≤ 阈值时全量注入优先，不走向量（三级召回顺序不变）。"""
    monkeypatch.setattr(settings, "kbase_full_load_threshold", 10)
    await _confirm(vmem, "条目A", "内容A")
    await _confirm(vmem, "条目B", "内容B")

    hits = await vmem.recall_kbase("任何查询都全量返回")
    assert len(hits) == 2 and all(h.score == 0.0 for h in hits)


@pytest.mark.asyncio
async def test_null_embedding_falls_back_to_keyword(tmp_path, monkeypatch):
    """未配置 embedding：服务正常初始化，召回退回关键词路径。"""
    monkeypatch.setattr(settings, "chroma_persist_dir", str(tmp_path / "chroma"))
    monkeypatch.setattr(settings, "kbase_full_load_threshold", 1)
    svc = SQLiteMemoryService(
        db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db", embedding=NullEmbeddingClient()
    )
    await svc.init()
    await _confirm(svc, "端口约定", "开发环境端口是 8000")
    await _confirm(svc, "评审约定", "代码评审必须两人")

    hits = await svc.recall_kbase("端口", limit=5)
    assert any("8000" in h.statement for h in hits)
