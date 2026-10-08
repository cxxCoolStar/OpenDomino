# 澄清追问（clarify）

## Sub-features

- Router 决策 `clarify` → 机器人反问澄清问题
- `PendingState` 落库（TTL `console_clarify_ttl_minutes` 默认 30 分钟）
- 用户补答后带着 pending 上下文重新路由

## How to get to it (user POV)

用户 @Domino 问了一个信息不足的问题（"帮我改下那个 bug"），机器人反问"要改哪个仓库？"，用户回答后机器人继续处理。

## Driving it with L2 内存驱动

FakeLLM 队列依次给：
1. `'{"action":"clarify","clarify_question":"要改哪个仓库？"}'`
2. 澄清后的最终决策

断言：首轮回复是澄清问题；`mem.load_session` / pending 状态里能看到 `PendingState`；第二轮正常走完。

## Gotchas

- clarify TTL 过期后 pending 静默失效，重新问等价于新会话——验证过期行为要直接操纵 pending 的时间戳
- 澄清中的群与蒸馏冷却共享 per-chat 锁，FakeLLM 并发场景注意锁竞争
