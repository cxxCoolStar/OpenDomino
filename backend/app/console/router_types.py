"""路由决策 Schema（ADR-003）。"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Action(str, Enum):
    ANSWER = "answer"
    CLARIFY = "clarify"
    DELEGATE = "delegate"


class PendingResolution(str, Enum):
    NONE = "none"
    ANSWERED = "answered"
    NEW_QUESTION = "new_question"
    CANCEL = "cancel"


class TaskSpec(BaseModel):
    title: str = ""
    instruction: str = ""
    repo: str = ""


class Decision(BaseModel):
    action: Action
    answer: str = ""
    clarify_question: str = ""
    task: TaskSpec | None = None
    pending_resolution: PendingResolution = PendingResolution.NONE
    correction_detected: bool = False

    @staticmethod
    def json_schema_hint() -> str:
        return (
            '{"action":"answer|clarify|delegate",'
            '"answer":"带来源标记的回答(action=answer)",'
            '"clarify_question":"澄清问题(action=clarify)",'
            '"task":{"title":"..","instruction":"自包含任务指令","repo":"仓库名"}(action=delegate),'
            '"pending_resolution":"answered|new_question|cancel|none",'
            '"correction_detected":false}'
        )
