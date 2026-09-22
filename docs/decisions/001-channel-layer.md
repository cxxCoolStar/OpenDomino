# ADR-001 协作入口层：钉钉机器人 + Web 群聊

> 状态：已确认
> 日期：2026-09-22
> 决策参考：hermes-agent（`C:\Users\CoolStar\Downloads\hermes-agent`）

## 背景

协作入口层是 OpenDomino 的最上层，负责把「用户在群里 @Domino 提问」这件事接进来，对上层的
ConsoleAgent 屏蔽消息来源（钉钉 / Web）。原文强调「Agent 要进入工作，而不是让工作迁移到 Agent」，
钉群是多数问题天然发生的地方，因此入口层是系统的第一块基石。

## 核心决议：参考 hermes-agent 的「机制」，但不引入其代码

经评估，hermes-agent 是生产级多平台网关，体量与 OpenDomino 完全不对等
（`gateway/platforms/base.py` 单文件约 26 万字符，支持 20+ 平台，含插件系统 / 配对 / 状态机 / TUI / 桌面应用）。
其钉钉 adapter（约 777 行）深度耦合于整套网关体系，直接引入会拖入半个 gateway，违背 OpenDomino
「分层清晰、可理解、可交付」的复刻目标。

**结论：借鉴其接入机制与踩过的坑，自行实现轻量入口层（体量约为 1/20）。**

### 借鉴的 6 个机制

| 机制 | hermes 做法 | OpenDomino 采用方式 |
|---|---|---|
| Stream 模式连接 | `dingtalk-stream` SDK 维持长 WebSocket，注册 `ChatbotMessage` 回调收消息，无需公网回调地址 | ✅ 直接采用（最省事，无需公网穿透） |
| 收发分离 | 收消息走 SDK 回调；发消息走消息携带的 `session_webhook`（httpx POST markdown） | ✅ 直接采用。session_webhook 5 分钟过期，需缓存并判过期 |
| 群聊 @ 触发门控 | 群消息默认要求 @机器人，用 SDK 结构化 `is_in_at_list` 判断（**不用正则扒文本**）；DM 永远放行；支持 `require_mention` / `free_response_chats` / `allowed_chats` | ✅ 采用并简化。重点学「用 is_in_at_list 而非正则」（正则会误删邮箱 / SSH 中的 @） |
| 纯解析与 I/O 分离 | `inbound.py` 全为纯函数（`extract_text` / `extract_media`），无 I/O 无状态，可单测 | ✅ 借鉴。解析写成纯函数模块便于测试 |
| 消息去重 | `MessageDeduplicator` 按 message_id 去重，防 Stream 重发 | ✅ 采用（内存集合 + 容量上限即可） |
| 统一消息事件 | `MessageEvent` 把各平台消息归一化为单一结构 | ✅ 借鉴「归一化」思想，**砍掉约 90% 字段**，只保留核心字段 |

### 明确不引入的能力（第一阶段砍掉）

- AI 卡片流式输出
- 🤔→🥳 表情反应（Thinking/Done）
- 图片 / 语音 / 文件等富媒体下载与解析
- 多平台抽象（signal / whatsapp / weixin 等）

> 以上均为后续增强项，第一阶段只做**纯文本问答闭环**。

## 三项已确认决议

1. **参考机制、不引入代码**：hermes-agent 仅作机制参考，不作为依赖 / git submodule 引入。
2. **钉钉接入用 Stream 模式**：无需公网地址，部署最简；不采用需公网穿透的 webhook 回调模式。
3. **第一阶段只做纯文本**：砍掉 AI 卡片、表情反应、富媒体，先跑通文字问答闭环。

## 模块结构

```
backend/app/channels/
├── base.py            # UnifiedMessage（精简版 MessageEvent）+ ChannelAdapter 抽象接口
│                      #   字段: text/type/user_id/user_name/chat_id/chat_name/is_group/
│                      #         message_id/session_webhook/timestamp/raw
├── dingtalk.py        # DingTalkChannel
│                      #   - Stream 模式连接（dingtalk-stream SDK）
│                      #   - 群聊 @ 门控（is_in_at_list）、用户白名单
│                      #   - session_webhook 缓存 + 过期判断、markdown 回复
│                      #   - 消息去重、断线重连
├── dingtalk_parse.py  # 纯函数解析（extract_text/extract_media，无 I/O，可单测）
└── web.py             # WebChannel（自家 Web 群聊，内部 API/WebSocket，无第三方依赖）
```

## 统一抽象

两个 Channel 实现同一接口，对上层 ConsoleAgent 完全屏蔽消息来源：

```python
class ChannelAdapter(Protocol):
    async def on_message(self, msg: UnifiedMessage) -> None: ...   # 入口：接消息
    async def reply(self, chat_id: str, text: str) -> SendResult: ...  # 出口：回消息
```

## 配置契约（对应 hermes plugin.yaml 的 env）

```
DINGTALK_CLIENT_ID            # 必填，钉钉应用 Client ID (app key)
DINGTALK_CLIENT_SECRET        # 必填，钉钉应用 Client Secret
DINGTALK_REQUIRE_MENTION=true # 群聊是否必须 @ 才响应
DINGTALK_ALLOWED_USERS=*      # 用户白名单（* = 所有人）
DINGTALK_FREE_RESPONSE_CHATS= # 免 @ 即可响应的群
DINGTALK_ALLOWED_CHATS=       # 群白名单（硬门控）
```

## 参考来源

- 机制参考：`hermes-agent/plugins/platforms/dingtalk/{adapter.py, inbound.py, plugin.yaml}`
- 统一事件参考：`hermes-agent/gateway/platforms/event.py`
