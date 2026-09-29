"""仓库执行配置解析（ADR-004 详细设计 §5 + M:N 绑定改造）。

解析优先级：L2 repo_bindings（chat_id + repo_name 消歧，主路径）
          → .env REPO_{NAME}_*（过渡兼容兜底，计划废弃）。
token 永不出自 L2/数据库：REPO_{NAME}_TOKEN → GITHUB_TOKEN（部署环境）。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from app.config import settings

log = logging.getLogger(__name__)

# 匹配 REPO_{NAME}_URL|IMAGE|SETUP_CMD|TEST_CMD|TOKEN
_NAME_RE = re.compile(r"^REPO_([A-Za-z0-9_]+)_(URL|IMAGE|SETUP_CMD|TEST_CMD|TOKEN)$")


@dataclass
class RepoExecutionConfig:
    name: str
    url: str = ""
    image: str = ""          # 留空用 settings.sandbox_docker_image
    setup_cmd: str = ""
    test_cmd: str = ""
    token: str = ""

    @property
    def clone_url(self) -> str:
        """把 PAT 织入 HTTPS URL（也可用容器内 git config insteadOf，直接织更简单）。"""
        if not self.token or self.url.startswith("git@"):
            return self.url
        return self.url.replace("https://", f"https://x-access-token:{self.token}@", 1)


def _global_github_token() -> str:
    """全局 GitHub token（GITHUB_TOKEN）。REPO_{NAME}_TOKEN 未配置时的兜底凭据。

    注意：pydantic-settings 只把 .env 读进 Settings 对象、不注入 os.environ，
    所以必须从 settings.github_token 取（os.environ 只在真环境变量时生效）。
    """
    if settings.github_token:
        return settings.github_token
    return os.environ.get("GITHUB_TOKEN", "")


def parse_repo_execution_configs() -> dict[str, RepoExecutionConfig]:
    """解析 REPO_{NAME}_URL/_IMAGE/_SETUP_CMD/_TEST_CMD/_TOKEN。

    与 console/context.py 的 parse_repo_registry 同风格：宽容解析，坏条目跳过并记日志。
    token 缺省回退 GITHUB_TOKEN（全局凭据，适配单 token 管多仓库的场景）。
    """
    out: dict[str, RepoExecutionConfig] = {}
    for key, value in os.environ.items():
        m = _NAME_RE.match(key)
        if not m:
            continue
        name, field_name = m.group(1).lower(), m.group(2)
        cfg = out.setdefault(name, RepoExecutionConfig(name=name))
        if field_name == "URL":
            cfg.url = value
        elif field_name == "IMAGE":
            cfg.image = value
        elif field_name == "SETUP_CMD":
            cfg.setup_cmd = value
        elif field_name == "TEST_CMD":
            cfg.test_cmd = value
        elif field_name == "TOKEN":
            cfg.token = value
    fallback = _global_github_token()
    for name, cfg in out.items():
        if not cfg.token and fallback and "github.com" in cfg.url:
            cfg.token = fallback
        if not cfg.url:
            log.warning("repo %s has TOKEN/IMAGE but no REPO_%s_URL", name, name.upper())
    return out


def get_repo_config(repo: str) -> RepoExecutionConfig | None:
    return parse_repo_execution_configs().get(repo.lower())


def collect_secrets() -> list[str]:
    """所有需要从工具输出中脱敏的字符串（token 只此一用，不进 prompt）。"""
    secrets = [c.token for c in parse_repo_execution_configs().values() if c.token]
    gt = _global_github_token()
    if gt:
        secrets.append(gt)
    return secrets


async def resolve_repo_config(repo_name: str, chat_id: str = "", memory=None) -> RepoExecutionConfig | None:
    """解析仓库执行配置：L2 绑定优先，.env 兜底。解析失败返回 None。"""
    name = (repo_name or "").strip()
    if not name:
        return None

    # 1) L2 绑定（M:N 主路径）
    if memory is not None and chat_id:
        try:
            repo = await memory.resolve_repo(chat_id, name)
        except Exception:  # noqa: BLE001 —— 绑定查询失败不阻塞，走 .env 兜底
            repo = None
            log.exception("resolve_repo from L2 failed, fallback to env")
        if repo is not None:
            return RepoExecutionConfig(
                name=repo.name,
                url=repo.url,
                test_cmd=repo.default_test_cmd,
                token=_env_token_for(repo.name),
            )

    # 2) .env REPO_{NAME}_* 兜底（过渡期兼容）
    return parse_repo_execution_configs().get(name.lower())


def _env_token_for(name: str) -> str:
    """token 来源：REPO_{NAME}_TOKEN（按仓覆盖）→ GITHUB_TOKEN（全局，github.com 才适用）。

    兜底必须走 _global_github_token()：pydantic-settings 只把 .env 读进 Settings
    对象、不注入 os.environ，只查 os.environ 会漏掉 .env 里的 GITHUB_TOKEN。
    """
    specific = os.environ.get(f"REPO_{name.upper()}_TOKEN", "")
    if specific:
        return specific
    return _global_github_token()
