# 群聊问答（answer）

## Sub-features

- @ 机器人提问 → Router 决策 `answer` → 直接回复（带来源标记【正式文档】/【记忆】/【我的理解】）
- 回复同时落 L1 群 Session（user + assistant 两条）

## How to get to it (user POV)

用户在飞书群里 @Domino 问一个知识型问题（"发布流程是什么"），机器人回复一段带来源标记的答案。不产生任务。

## Driving it with L2 内存驱动

见 SKILL.md「Drive → L2」。FakeLLM 队列给 `'{"action":"answer","answer":"..."}'`，断言：
- `replies` 非空且内容含答案文本
- `mem.load_session(chat_id)` 返回 2 条消息（user + assistant）

## Gotchas

- Router 输出是 LLM 文本里的 JSON，`Router._extract_json` 容忍 markdown fence 与前后噪声
- `console_llm_timeout_seconds` 默认 60s；真实 LLM 慢是常态，FakeLLM 无此问题
- 免 @ 群（`feishu_free_response_chats`）所有消息都进管线，测试时注意消息别被旁听路径截走
