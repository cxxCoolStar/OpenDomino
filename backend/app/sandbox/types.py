"""SandboxAgent 类型定义（ADR-004）：Task 状态机、TaskEvent、数据传输对象。

状态机：pending → running → review → done | failed | canceled（ADR-004 定案）。
review = 改动已 push、待人评审的中间态；done 仅在验证通过后进入。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    REVIEW = "review"      # 已 push 分支，待人工评审
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"

    @classmethod
    def terminal(cls) -> set["TaskStatus"]:
        return {cls.DONE, cls.FAILED, cls.CANCELED}


class TaskStage(str, Enum):
    """TaskEvent 阶段（对应 ADR-004 执行管线步骤）。"""

    SETUP = "setup"
    EXECUTE = "execute"
    REVIEW = "review"
    VERIFY = "verify"
    REPORT = "report"


class EventKind(str, Enum):
    STAGE_ENTER = "stage_enter"
    STAGE_EXIT = "stage_exit"
    TOOL_SUMMARY = "tool_summary"
    ERROR = "error"


@dataclass
class TaskEvent:
    stage: str
    kind: str
    summary: str
    trace_id: str = ""
    seq: int = 0
    created_at: float = 0.0


@dataclass
class Task:
    id: str
    title: str
    instruction: str
    repo: str
    chat_id: str
    session_id: str
    created_by: str
    type: str = "investigate"   # investigate=排查给结论 | modify=变更代码
    trace_id: str = ""
    status: TaskStatus = TaskStatus.PENDING
    branch_name: str = ""
    base_ref: str = ""
    commits: list[dict] = field(default_factory=list)   # [{sha, message}]
    verify_result: str = ""     # passed | blocked | ""（未验证）
    error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class TaskDraft:
    """ConsoleAgent delegate 动作提交的任务草稿。"""

    title: str
    instruction: str
    repo: str
    chat_id: str
    session_id: str
    created_by: str
