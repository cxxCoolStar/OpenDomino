"""pytest 配置：把 backend/ 加入 import 路径。"""
import sys

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _isolate_chroma(tmp_path, monkeypatch):
    """所有测试的 ChromaDB 持久化目录强制指向 tmp_path，防止污染真实 ./data/chroma。

    （EMBEDDING_MODEL 配置后，未显式隔离的测试会以真实目录创建向量库——已发生过一次。）
    """
    from app.config import settings

    monkeypatch.setattr(settings, "chroma_persist_dir", str(tmp_path / "chroma"))
