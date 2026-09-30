"""OpenDomino 全局配置。

所有配置项经环境变量注入，默认值对应 ADR 的配置契约。
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ---- LLM ----（四项必填，全部由 .env 提供，代码不设默认值）
    llm_base_url: str
    llm_api_key: str
    console_model: str
    sandbox_model: str                      # ADR-004：建议配比 console_model 更强的模型
    sandbox_llm_timeout_seconds: int = 120  # 执行循环单次 LLM 调用硬超时（编码任务输出长，比 console 宽）
    sandbox_llm_max_tokens: int = 8192      # 末轮交卷/强制汇总是长文（排查清单可数千字），显式设防端点默认值截断

    # ---- ADR-008 入口层（飞书）----
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_domain: str = "https://open.feishu.cn"
    feishu_require_mention: bool = True     # @ 门控：@ 才进管线，其余旁听
    feishu_free_response_chats: str = ""    # 免 @ 即可进管线的群（仅特殊场景，旁听仍全量）
    feishu_allowed_users: str = "*"         # * = 所有人，否则逗号分隔 user_id/open_id
    feishu_allowed_chats: str = ""          # 群白名单（硬门控，同时约束旁听范围），逗号分隔
    feishu_bot_open_id: str = ""            # 机器人 open_id（mentions 结构化 @ 判定用）
    feishu_bot_user_id: str = ""            # 机器人 user_id（同上，可选）
    feishu_backfill_days: int = 7           # 回拉时间窗（天）
    feishu_backfill_max: int = 1000         # 单群单次回拉条数上限
    feishu_observe_retain_days: int = 30    # 旁听消息保留期（天）
    feishu_observe_window_hours: int = 6    # 注入上下文的旁听时间窗（小时），窗口外留 L2 存而不检索

    # ---- ADR-003 ConsoleAgent ----
    console_context_window: int = 20
    console_kbase_limit: int = 5
    console_knowledge_limit: int = 8
    console_max_context_chars: int = 12000
    console_clarify_ttl_minutes: int = 30
    console_llm_timeout_seconds: int = 60   # 单次 LLM 调用硬超时（SDK 重试已禁用）；glm 高峰期 25s 不够，60s 保群聊稳定性
    console_llm_dump_dir: str = ""          # 调试：非空时每次 LLM 调用的完整报文落盘到该目录（llm_dump_*.json）
    github_token: str = ""                  # 全局 GitHub PAT（沙箱 clone/push 凭据；REPO_{NAME}_TOKEN 未配时兜底）

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
    console_summary_model: str = ""         # 廉价摘要/蒸馏模型；空则回落 console_model

    # ---- ADR-009 旁听结论蒸馏 ----
    distiller_enabled: bool = True          # 总开关；false 不启动扫描器，prune 退回纯年龄清理
    distill_scan_interval_seconds: int = 60     # 扫描器轮询间隔
    distill_idle_minutes: int = 10          # 空闲判定阈值（距上一条旁听消息）
    distill_force_threshold: int = 50       # 未蒸馏消息数兜底触发阈值
    distill_cooldown_minutes: int = 360     # 两次成功蒸馏最小间隔（每群每天最多 4 次）
    distill_failure_retry_minutes: int = 30 # 蒸馏失败后重试冷却
    distill_batch_max: int = 200            # 单次蒸馏消息数上限（最老优先，积压分批推进）
    distill_dedup_enabled: bool = True      # 提议前查重闸（ADR-009 增补）；embedding 不可用时 fail-open
    distill_dedup_confirmed_threshold: float = 0.85   # vs confirmed 相似度阈值（收紧，宁漏勿滥）
    distill_dedup_rejected_threshold: float = 0.80    # vs 本群 rejected 阈值（放宽，唯一硬防线）
    distill_rejected_half_life_days: int = 90         # 被拒档案半衰期（超期不再拦截，可翻案）

    # ---- ADR-011 知识库文档自动同步 ----
    kbsync_enabled: bool = True             # 总开关；false 不启动同步循环
    kbsync_interval_seconds: int = 300      # 扫描间隔（docs 为 git 管理的低频内容，5 分钟延迟足够）
    docs_root: str = "../docs"              # 本地文档源根目录（相对 backend 运行目录）

    # ---- ADR-004 SandboxAgent ----
    sandbox_docker_image: str = "opendomino-sandbox:latest"  # 预装 git + /workspace（构建脚本见 handoff）
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
    sandbox_stale_minutes: int = 10             # running 且超 N 分钟无新事件视为疑似停滞（ADR-010）
    sandbox_cpus: float = 2.0
    sandbox_mem_limit: str = "2g"
    sandbox_sweep_interval_seconds: int = 30

    # ---- ADR-005 SKILL ----
    skills_dir: str = "skills"
    skills_desc_max_chars: int = 140

    # ---- ADR-007 链路追踪 ----
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = "http://localhost:3000"
    langfuse_enabled: bool = False
    tracing_sample_rate: float = 1.0


settings = Settings()
