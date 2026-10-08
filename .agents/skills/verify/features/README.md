# OpenDomino 功能地图（Feature Map）

agent 的"物质化记忆"：每个用户可见功能一条链路——是什么、从用户视角怎么到达、
怎么驱动验证、有什么坑。验证改动前先查这里找对应功能。

功能文件清单（新增功能按此模板建文件）：

1. [群聊问答（answer）](answer-routing.md)
2. [澄清追问（clarify）](clarify-flow.md)
3. [任务委派（delegate → SandboxAgent）](delegate-sandbox.md)
4. [任务管控（task_op：status/steer/cancel）](task-control.md)
5. [旁听蒸馏（overheard → L2 记忆）](overheard-distill.md)
6. [知识库文档同步（docs → L3 检索）](kb-doc-sync.md)
