"""路由决策 Schema（ADR-003/008）。"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Action(str, Enum):
    ANSWER = "answer"
    CLARIFY = "clarify"
    DELEGATE = "delegate"
    REPO_OP = "repo_op"


class PendingResolution(str, Enum):
    NONE = "none"
    ANSWERED = "answered"
    NEW_QUESTION = "new_question"
    CANCEL = "cancel"


class TaskSpec(BaseModel):
    title: str = ""
    instruction: str = ""
    repo: str = ""
    type: str = "investigate"   # investigate=排查给结论 | modify=变更代码（默认排查）


class RepoOp(BaseModel):
    """仓库管理意图（ADR-008）：LLM 识别意图提取参数，执行走确定性代码。"""

    op: str                      # bind | unbind | list
    name: str = ""               # 仓库名（bind/unbind 必填）
    url: str = ""                # git 地址（bind 必填）
    test_cmd: str = ""           # 可选测试命令


class Decision(BaseModel):
    action: Action
    answer: str = ""
    clarify_question: str = ""
    task: TaskSpec | None = None
    repo_op: RepoOp | None = None
    pending_resolution: PendingResolution = PendingResolution.NONE
    correction_detected: bool = False

    @staticmethod
    def json_schema_hint() -> str:
        return (
            '{"action":"answer|clarify|delegate|repo_op",'
            '"answer":"带来源标记的回答(action=answer)",'
            '"clarify_question":"澄清问题(action=clarify)",'
            '"task":{"title":"..","instruction":"自包含任务指令","repo":"仓库名",'
            '"type":"investigate|modify"}(action=delegate;investigate=要排查结论(为什么/在哪/如何实现),'
            'modify=要改动仓库代码;拿不准填investigate),'
            '"repo_op":{"op":"bind|unbind|list","name":"仓库名","url":"git地址(bind必填)",'
            '"test_cmd":"可选测试命令"}(action=repo_op;用户要给本群绑定/解绑/查看仓库时),'
            '"pending_resolution":"answered|new_question|cancel|none",'
            '"correction_detected":false}'
        )
