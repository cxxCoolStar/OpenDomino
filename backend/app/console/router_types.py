"""路由决策 Schema（ADR-003/008）。"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, field_validator


class Action(str, Enum):
    ANSWER = "answer"
    CLARIFY = "clarify"
    DELEGATE = "delegate"
    REPO_OP = "repo_op"
    TASK_OP = "task_op"   # 任务进展查询 / 中途管控（ADR-010），执行是确定性代码


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
    """仓库管理意图（ADR-008）：LLM 识别意图提取参数，执行走确定性代码。

    op 默认空串：模型常把未选中的分支填成空对象 {}，校验层放行、
    执行层（_do_repo_op）已有空 op 兜底提示，不再因缺 op 整体拒收。
    """

    op: str = ""                 # bind | unbind | list（空=未选中该动作）
    name: str = ""               # 仓库名（bind/unbind 必填）
    url: str = ""                # git 地址（bind 必填）
    test_cmd: str = ""           # 可选测试命令


class TaskOp(BaseModel):
    """任务进展查询与中途管控意图（ADR-010）：LLM 识别意图提取参数，执行走确定性代码。

    status=查进展（task_id 空则列本群全部）；steer=中途纠偏；cancel=中途取消。
    op 默认空串，理由同 RepoOp：空对象 {} 放行给执行层兜底。
    """

    op: str = ""                 # status | steer | cancel（空=未选中该动作）
    task_id: str = ""            # 任务号（8 位 hex，用户常写 #a1b2c3d4；status 可空=列全部）
    message: str = ""            # steer 的纠偏内容（steer 必填）


class Decision(BaseModel):
    action: Action
    answer: str = ""
    clarify_question: str = ""
    task: TaskSpec | None = None
    repo_op: RepoOp | None = None
    task_op: TaskOp | None = None
    pending_resolution: PendingResolution = PendingResolution.NONE
    correction_detected: bool = False

    @field_validator("task", "repo_op", "task_op", mode="before")
    @classmethod
    def _empty_dict_to_none(cls, v: object) -> object:
        """模型常把未选中的分支字段填成 {}（如截图 bug），归一成 None 走默认兜底。"""
        return None if v == {} else v

    @staticmethod
    def json_schema_hint() -> str:
        return (
            '{"action":"answer|clarify|delegate|repo_op|task_op",'
            '"answer":"带来源标记的回答(action=answer)",'
            '"clarify_question":"澄清问题(action=clarify)",'
            '"task":{"title":"..","instruction":"自包含任务指令","repo":"仓库名",'
            '"type":"investigate|modify"}(仅 action=delegate 时输出;investigate=要排查结论(为什么/在哪/如何实现),'
            'modify=要改动仓库代码;拿不准填investigate),'
            '"repo_op":{"op":"bind|unbind|list","name":"仓库名","url":"git地址(bind必填)",'
            '"test_cmd":"可选测试命令"}(仅 action=repo_op 时输出;用户要给本群绑定/解绑/查看仓库时),'
            '"task_op":{"op":"status|steer|cancel","task_id":"任务号如a1b2c3d4",'
            '"message":"纠偏内容(steer必填)"}(仅 action=task_op 时输出;'
            '用户问任务进展/怎么样了→status,要求运行中任务改方向→steer,要求停掉任务→cancel;task_id取#号后的8位),'
            '"pending_resolution":"answered|new_question|cancel|none",'
            '"correction_detected":false}'
            '\n注意：task/repo_op/task_op 只在选择了对应 action 时才输出，未选中的分支一律省略该字段，不要填空对象。'
        )
