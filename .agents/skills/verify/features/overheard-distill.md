# 旁听蒸馏（overheard → L2 记忆）

## Sub-features

- 非 @ 消息旁听落 L2（存而不检索，时间窗 `feishu_observe_window_hours` 默认 6h）
- 群空闲（`distill_idle_minutes`）→ LLM 蒸馏候选结论 → 群内确认流 → confirmed 进 L2
- 提议前查重闸（`distill_dedup_enabled`，embedding 不可用时 fail-open）
- supersedes：确认后旧条目 archived；rejected 档案半衰期 90 天

## How to get to it (user POV)

群里普通讨论（不 @ 机器人）被旁听记录；讨论出现值得沉淀的结论时，机器人主动在群里提议"要不要把这条记下来？"，用户回复"确认"后进入团队记忆。

## Driving it with L1 脚本

```bash
cd backend && python scripts/e2e_distill.py
```

覆盖 10 个场景：首见初始化、空闲触发、写前校验（蒸馏期间讨论恢复→作废）、三态水位线、supersedes、prune 协同、冷却、槽位占用、单提议上限、无可沉淀。FakeLLM 确定性输出，不依赖真实飞书/LLM。

产物：`scripts/e2e_distill_report.json`。

## Gotchas

- 蒸馏首扫必须晚于启动回拉落库（锚点取表内最新 received_at）——2026-09-30 事故根因，改回拉/蒸馏时序必回归此场景
- `distill_cooldown_minutes` 默认 360（每群每天最多 4 次成功蒸馏），测试直调 `_scan_once` 绕过 sleep
- modify/investigate 任务完成后也会提炼工程经验（第二条蒸馏入口）
- prune 的 `respect_watermark=True` 时水位线之前的消息才可清
