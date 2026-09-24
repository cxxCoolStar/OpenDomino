"""仓库注册与群绑定（M:N，ADR-003 群仓库绑定的 L2 落地）。

Repo：仓库实体，全局唯一（name 唯一），URL/test_cmd 等档案只存一份；
GroupRepoBinding：群 ↔ 仓库关系，(chat_id, repo_id) 唯一，群级 test_cmd 可覆盖。

绑定即注册：绑定未注册仓库时自动创建 Repo 实体（用消息里给的 URL）。
凭据不在此存储——token 走部署环境（GITHUB_TOKEN / REPO_{NAME}_TOKEN）。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass
class Repo:
    repo_id: str
    name: str
    url: str
    description: str = ""
    default_test_cmd: str = ""
    created_by: str = ""
    created_at: float = 0.0


@dataclass
class GroupRepoBinding:
    chat_id: str
    repo_id: str
    repo_name: str = ""       # join 便利字段（读取时填充）
    url: str = ""             # join 便利字段
    test_cmd_override: str = ""
    bound_by: str = ""
    bound_at: float = 0.0


def new_repo_id() -> str:
    return uuid.uuid4().hex[:12]
