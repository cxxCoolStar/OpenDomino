"""OpenDomino 全局配置。

所有配置项经环境变量注入，默认值对应 ADR 的配置契约。
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ---- LLM ----
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    console_model: str = "gpt-4o-mini"
    sandbox_model: str = "gpt-4o"

    # ---- ADR-001 入口层（钉钉）----
    dingtalk_client_id: str = ""
    dingtalk_client_secret: str = ""
    dingtalk_require_mention: bool = True
    dingtalk_allowed_users: str = "*"      # * = 所有人，否则逗号分隔 user_id
    dingtalk_free_response_chats: str = ""  # 免 @ 即可响应的群，逗号分隔
    dingtalk_allowed_chats: str = ""        # 群白名单（硬门控），逗号分隔

    # ---- ADR-003 ConsoleAgent ----
    console_context_window: int = 20
    console_kbase_limit: int = 5
    console_knowledge_limit: int = 8
    console_max_context_chars: int = 12000
    console_clarify_ttl_minutes: int = 30
    repo_registry: str = ""                 # name=url|desc;name2=url2|desc2

    # ---- ADR-002 记忆层 ----
    memory_db_url: str = "sqlite+aiosqlite:///./data/opendomino.db"
    chroma_persist_dir: str = "./data/chroma"

    # ---- ADR-004 SandboxAgent ----
    sandbox_docker_image: str = "python:3.12-slim"
    sandbox_max_steps: int = 40
    sandbox_timeout_minutes: int = 30
    sandbox_command_timeout_seconds: int = 120
    sandbox_max_concurrency: int = 2
    sandbox_verify_max_retries: int = 2
    sandbox_container_idle_ttl_min: int = 10
    sandbox_loop_same_call_limit: int = 3

    # ---- ADR-005 SKILL ----
    skills_dir: str = "skills"
    skills_desc_max_chars: int = 140

    # ---- ADR-006 工具层 ----
    tool_output_max_chars: int = 50000
    tool_error_max_chars: int = 2048
    tool_check_ttl_seconds: int = 30
    tool_check_grace_seconds: int = 60

    # ---- ADR-007 链路追踪 ----
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = "http://localhost:3000"
    langfuse_enabled: bool = False
    tracing_sample_rate: float = 1.0


settings = Settings()
