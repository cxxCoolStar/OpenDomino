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
    sandbox_llm_timeout_seconds: int = 120  # 执行循环单次 LLM 调用硬超时（编码任务输出长，比 console 宽）

    # ---- ADR-001 入口层（钉钉）----
    dingtalk_client_id: str = ""
    dingtalk_client_secret: str = ""
    dingtalk_require_mention: bool = True
    dingtalk_allowed_users: str = "*"      # * = 所有人，否则逗号分隔 user_id
    dingtalk_free_response_chats: str = ""  # 免 @ 即可响应的群，逗号分隔
    dingtalk_allowed_chats: str = ""        # 群白名单（硬门控），逗号分隔
    dingtalk_robot_code: str = ""           # 机器人主动发消息用；留空则取 dingtalk_client_id
    dingtalk_robot_messages_enabled: bool = True  # 任务回传主通道开关；关闭则仅 session_webhook

    # ---- ADR-003 ConsoleAgent ----
    console_context_window: int = 20
    console_kbase_limit: int = 5
    console_knowledge_limit: int = 8
    console_max_context_chars: int = 12000
    console_clarify_ttl_minutes: int = 30
    console_llm_timeout_seconds: int = 25   # 单次 LLM 调用硬超时（SDK 重试已禁用），快速失败交给上层
    repo_registry: str = ""                 # name=url|desc;name2=url2|desc2

    # ---- ADR-002 记忆层 ----
    memory_db_url: str = "sqlite+aiosqlite:///./data/opendomino.db"
    chroma_persist_dir: str = "./data/chroma"
    # Embedding（Phase C 向量召回）：填方舟推理接入点 ep-xxx 启用；留空则退回关键词召回
    embedding_model: str = ""               # 例：ep-2024xxxxxx（doubao-embedding-large-text）
    embedding_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    embedding_api_key: str = ""             # 缺省复用 LLM_API_KEY
    embedding_dim: int = 2048
    memory_confirm_ttl_minutes: int = 60    # 待确认记忆 TTL（非阻塞提议，比澄清宽）
    memory_confirm_keywords: str = "确认,确定,同意,保存"
    memory_cancel_keywords: str = "取消,不用,不要,算了,放弃,不保存"
    kbase_full_load_threshold: int = 20     # confirmed 条目数 ≤ 阈值时全量注入，超过才走召回
    session_compact_trigger: int = 40       # 消息数达到此值触发压缩（窗口的 2 倍）
    session_summary_max_chars: int = 1500

    # ---- ADR-004 SandboxAgent ----
    sandbox_docker_image: str = "python:3.12-slim"
    sandbox_max_steps: int = 40
    sandbox_timeout_minutes: int = 30
    sandbox_command_timeout_seconds: int = 120
    sandbox_max_concurrency: int = 2
    sandbox_verify_max_retries: int = 2
    sandbox_container_idle_ttl_min: int = 10
    sandbox_loop_same_call_limit: int = 3
    sandbox_tool_output_max_chars: int = 8000   # 单次工具输出截断（ADR-004 表）
    sandbox_history_trim_chars: int = 60000     # 执行循环历史裁剪阈值
    sandbox_history_keep_recent: int = 6        # 裁剪时保留最近 N 步工具结果
    sandbox_cpus: float = 2.0
    sandbox_mem_limit: str = "2g"
    sandbox_sweep_interval_seconds: int = 30

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
