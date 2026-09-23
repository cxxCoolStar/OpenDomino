"""SandboxAgent 执行侧入口（ADR-004）。

模块职责见 docs/design/sandbox-agent-detail.md：
- types/storage  Task 持久化与状态机
- executor       任务队列、按仓库串行、全局并发
- container/git  容器池与 git 操作
- agent          受控 ReAct 执行循环
- reporter       经 ChannelRegistry 回传来源群
"""
