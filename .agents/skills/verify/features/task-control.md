# 任务管控（task_op：status/steer/cancel）

## Sub-features

- 用户在群里问任务进展 → Router 决策 `task_op` → `TaskOpHandler.handle`
- status：事件留痕 + 汇总列表；steer：中途改指令；cancel：取消任务
- 上下文状态段活化：装配器把裸任务 ID 渲染成实时状态（`[进行中任务]` 段）

## How to get to it (user POV)

用户 @Domino 说"刚才那个任务怎么样了"或"把那个任务取消掉"，机器人回复任务当前状态/最近事件，或确认取消。

## Driving it with L2

`scripts/e2e_sandbox.py` 的 TASK_TYPE=control 场景在真实管线上验证 status 查询与 cancel 语义，不需要 Docker。断言点：
- status 返回事件序列（TaskEvent）
- pending 状态 cancel 直落 CANCELED
- 事件留痕随任意 TASK_TYPE 一并断言

## Gotchas

- 归属校验在 TaskOpHandler 内（chat_id 必须与任务一致），跨群操作应被拒
- `sandbox_stale_minutes`（默认 10）running 超时无新事件视为疑似停滞——status 输出会反映
- 状态中文映射（排队中/执行中/待评审/已完成/失败/已取消）在 `main.py` 的 `task_status_section`
