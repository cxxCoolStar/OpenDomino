"""仓库执行配置（ADR-004 详细设计 §5）：REPO_{NAME}_* 环境变量解析与凭据收集。

沙箱侧不维护「群→仓库」绑定（那是 ADR-003 console 侧的事），
只按任务里的仓库名查这里的执行配置：URL、镜像、依赖安装命令、测试命令。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

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


def parse_repo_execution_configs() -> dict[str, RepoExecutionConfig]:
    """解析 REPO_{NAME}_URL/_IMAGE/_SETUP_CMD/_TEST_CMD/_TOKEN。

    与 console/context.py 的 parse_repo_registry 同风格：宽容解析，坏条目跳过并记日志。
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
    for name, cfg in out.items():
        if not cfg.url:
            log.warning("repo %s has TOKEN/IMAGE but no REPO_%s_URL", name, name.upper())
    return out


def get_repo_config(repo: str) -> RepoExecutionConfig | None:
    return parse_repo_execution_configs().get(repo.lower())


def collect_secrets() -> list[str]:
    """所有需要从工具输出中脱敏的字符串（token 只此一用，不进 prompt）。"""
    return [c.token for c in parse_repo_execution_configs().values() if c.token]
