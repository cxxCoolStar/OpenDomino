"""FeishuChannel：飞书长连接通道（ADR-008）。

- lark-oapi SDK WebSocket 长连接，订阅 im.message.receive_v1，无需公网回调地址；
- 全量接收群消息：@ 消息走 on_message 管线，非 @ 消息写入旁听缓冲（不触发 LLM）；
- 门控沿用钉钉三开关语义（群白名单/用户白名单/@ 门控）；
- 回复走 bot 发消息 API（tenant_access_token 由 SDK 管理），按 chat_id 寻址；
- 历史回拉：入群事件 + 重启双触发，message_id 幂等。

SDK 导入延迟到 start()/_client 初始化，便于无 SDK 环境导入本模块做测试。

代理防护：SDK 底层 requests 会继承系统代理环境变量，本机代理（127.0.0.1:7890）
对 open.feishu.cn 的 TLS 握手会被掐断（SSL EOF）。本模块导入时对飞书域名强制
直连（NO_PROXY），不影响其他库的代理行为。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import OrderedDict
from typing import Awaitable, Callable

import httpx

from app.channels.base import ChannelAdapter, SendResult, UnifiedMessage
from app.channels.feishu_parse import ParsedFeishuMessage, parse_message
from app.config import settings

# 飞书域名强制直连（NO_PROXY 追加，不覆盖既有配置）
_forbidden = {"HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"}
_existing_noproxy = os.environ.get("NO_PROXY", os.environ.get("no_proxy", ""))
_feishu_hosts = "open.feishu.cn,open.larksuite.com"
if _feishu_hosts not in _existing_noproxy:
    os.environ["NO_PROXY"] = f"{_existing_noproxy},{_feishu_hosts}".lstrip(",")
    os.environ["no_proxy"] = os.environ["NO_PROXY"]
for _k in _forbidden:  # NO_PROXY 对 requests 优先级受实现影响，直接清飞书环境下的代理更稳
    os.environ.pop(_k, None)

log = logging.getLogger(__name__)

MessageHandler = Callable[[UnifiedMessage], Awaitable[None]]
ObservedSaver = Callable[[str, list[UnifiedMessage]], Awaitable[None]]  # (chat_id, msgs)

_DEDUP_CAPACITY = 2048


class MessageDeduplicator:
    """按 message_id 去重，防飞书事件重发（官方明示可能重复）。内存集合 + 容量上限。"""

    def __init__(self, capacity: int = _DEDUP_CAPACITY) -> None:
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._capacity = capacity

    def is_duplicate(self, message_id: str) -> bool:
        if not message_id:
            return False
        if message_id in self._seen:
            return True
        self._seen[message_id] = None
        if len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        return False


class FeishuChannel(ChannelAdapter):
    """飞书群聊通道。on_message（@ 管线）与 save_observed（旁听落库）由上层注入。"""

    def __init__(
        self,
        on_message: MessageHandler,
        save_observed: ObservedSaver | None = None,
    ) -> None:
        self._on_message = on_message
        self._save_observed = save_observed
        self._dedup = MessageDeduplicator()
        self._http = httpx.AsyncClient(timeout=15)
        self._bot_open_id = settings.feishu_bot_open_id
        self._bot_user_id = settings.feishu_bot_user_id
        self._bot_mention_names: set[str] = set()  # 自学到的机器人名（"Domino" 等）
        self._client: object | None = None  # lark.Client，start() 时初始化

    # ---- 机器人身份自动探测 ----

    def _learn_bot_identity(self, mentioned_ids: list[str], mention_names: list[str]) -> bool:
        """从事件 mentions 自学习机器人身份（零配置路径）。

        判定依据：mentions 中 name 命中机器人特征名（显示名含「机器人」、bot 后缀、
        Domino 本名）。返回是否学到了新身份。
        """
        learned = False
        for mid, name in zip(mentioned_ids, mention_names):
            if not mid:
                continue
            if _looks_like_bot_name(name, self._bot_mention_names):
                if mid not in (self._bot_open_id, self._bot_user_id):
                    log.info("feishu bot identity learned from mention: %s (%s)", mid, name)
                    if not self._bot_open_id:
                        self._bot_open_id = mid
                    self._bot_mention_names.add(name)
                    learned = True
        return learned

    def _mentions_me(self, mentioned_ids: list[str], mention_names: list[str]) -> bool:
        """结构化 @ 判定：显式配置优先，自学身份兜底。"""
        for mid in mentioned_ids:
            if self._bot_open_id and mid == self._bot_open_id:
                return True
            if self._bot_user_id and mid == self._bot_user_id:
                return True
            if mid == "_all":
                return True
        for name in mention_names:
            if name and name in self._bot_mention_names:
                return True
        return False

    # ---- 门控（语义沿用钉钉三开关，见 ADR-008 通道层决议 5）----

    def _allowed(self, msg: ParsedFeishuMessage) -> bool:
        allowed_chats = _split(settings.feishu_allowed_chats)
        if allowed_chats and msg.chat_id not in allowed_chats:
            return False
        allowed_users = _split(settings.feishu_allowed_users)
        if allowed_users and "*" not in allowed_users and msg.user_id not in allowed_users:
            return False
        if not msg.is_group:  # 第一阶段仅群聊
            return False
        free_chats = _split(settings.feishu_free_response_chats)
        if settings.feishu_require_mention and msg.chat_id not in free_chats:
            if not msg.is_at_bot:
                return False
        return True

    # ---- 收消息 ----

    async def handle_event(self, data: object) -> bool:
        """处理一条 im.message.receive_v1 事件。返回是否进入管线（@ 且通过门控）。

        飞书全量推送下消息分两路：@ 且过门控 → on_message 管线；否则 → 旁听缓冲
        （不调 LLM、不回复，ADR-008 成本底线）。去重在两路分流之前。
        """
        msg = parse_message(
            data, bot_open_id=self._bot_open_id, bot_user_id=self._bot_user_id
        )
        if not msg.message_id or self._dedup.is_duplicate(msg.message_id):
            log.info("feishu duplicate/empty message_id=%s dropped", msg.message_id)
            return False
        if msg.is_group and self._is_bot_own_message(msg):
            return False  # 事件里机器人消息已在权限层排除，此处兜底防回环

        # 机器人身份自学习（FEISHU_BOT_OPEN_ID 未配置时的零配置路径）
        mention_names = _extract_mention_names(data)
        if msg.mentioned_ids and self._learn_bot_identity(msg.mentioned_ids, mention_names):
            # 学到身份后重新判定本条消息
            msg.is_at_bot = self._mentions_me(msg.mentioned_ids, mention_names)
            msg.mentioned_bot = msg.is_at_bot
        elif msg.mentioned_ids and not self._bot_open_id:
            msg.is_at_bot = self._mentions_me(msg.mentioned_ids, mention_names)

        in_pipeline = msg.is_group and self._allowed(msg)
        if in_pipeline:
            log.info(
                "feishu message accepted: message_id=%s chat_id=%s user=%s text=%r",
                msg.message_id, msg.chat_id, msg.user_id, msg.text[:80],
            )
            await self._on_message(self._to_unified(msg))
        elif self._passthrough_observation(msg):
            log.debug(
                "feishu message observed: message_id=%s chat_id=%s user=%s",
                msg.message_id, msg.chat_id, msg.user_id,
            )
            if self._save_observed is not None:
                await self._save_observed(msg.chat_id, [self._to_unified(msg)])
        else:
            log.info(
                "feishu message rejected by gate: message_id=%s chat_id=%s user=%s "
                "is_group=%s is_at_bot=%s",
                msg.message_id, msg.chat_id, msg.user_id, msg.is_group, msg.is_at_bot,
            )
        return in_pipeline

    def _is_bot_own_message(self, msg: ParsedFeishuMessage) -> bool:
        """SDK 对象或 dict 事件下钻 event.sender.sender_type 判定。"""
        if isinstance(msg.raw, dict):
            sender = (msg.raw.get("event") or {}).get("sender") or {}
        else:
            sender = getattr(getattr(msg.raw, "event", None) or msg.raw, "sender", None) or {}
        raw_type = sender.get("sender_type", "") if isinstance(sender, dict) else getattr(sender, "sender_type", "")
        sender_type = str(raw_type or "")
        return sender_type == "bot"

    def _passthrough_observation(self, msg: ParsedFeishuMessage) -> bool:
        """旁听准入：仅群聊文本类消息。白名单群之外不旁听（与门控同一群边界）。

        非 @ 消息不受 require_mention / 用户白名单约束——旁听的目的是看到
        群成员的自然讨论，但群白名单（硬边界）仍然生效；单聊不入旁听。
        """
        if not msg.is_group:
            return False
        allowed_chats = _split(settings.feishu_allowed_chats)
        if allowed_chats and msg.chat_id not in allowed_chats:
            return False
        return True

    def _to_unified(self, msg: ParsedFeishuMessage) -> UnifiedMessage:
        ts = msg.create_time / 1000.0 if msg.create_time else time.time()
        return UnifiedMessage(
            text=msg.text,
            user_id=msg.user_id,
            user_name=msg.user_name,
            chat_id=msg.chat_id,
            chat_name=msg.chat_name,
            is_group=msg.is_group,
            message_id=msg.message_id,
            is_at_bot=msg.is_at_bot,
            timestamp=ts,
            raw=msg.raw if isinstance(msg.raw, dict) else {},
        )

    # ---- 发消息 ----

    async def reply(self, chat_id: str, text: str, *, session_webhook: str = "") -> SendResult:
        """回复群消息：bot 发消息 API 按 chat_id 寻址（ADR-008 通道层决议 4）。

        session_webhook 参数仅为兼容 ChannelAdapter 签名，飞书通道恒忽略。
        格式选择：内容含高置信 Markdown 特征（表格/标题/代码围栏/加粗/列表）时发
        interactive 卡片（JSON 2.0 schema，markdown 组件可渲染标题/表格/代码块），
        纯文本维持 msg_type=text；卡片超限（230020）降级回纯文本重试。
        """
        client = self._client
        if client is None:
            return SendResult(ok=False, error="feishu client not started")
        if _looks_like_markdown(text):
            card_result = await self._reply_card(chat_id, text)
            if card_result.ok:
                return card_result
            log.warning("feishu card reply failed, fallback to text: %s", card_result.error)
        return await self._reply_text(chat_id, text)

    async def _reply_text(self, chat_id: str, text: str) -> SendResult:
        client = self._client
        if client is None:
            return SendResult(ok=False, error="feishu client not started")
        try:
            from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

            body = CreateMessageRequestBody.builder() \
                .receive_id(chat_id) \
                .msg_type("text") \
                .content(_json_content(text)) \
                .build()
            request = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(body) \
                .build()
            response = await asyncio.to_thread(client.im.v1.message.create, request)
            if response.success():
                return SendResult(ok=True)
            # text 消息对特殊字符受限时降级 post（hermes 同思路：被拒则降级重试）
            if response.code == 230001 or "Invalid param" in str(response.msg):
                return await self._reply_post(chat_id, text)
            return SendResult(
                ok=False,
                error=f"feishu api {response.code}: {response.msg}",
            )
        except Exception as e:  # noqa: BLE001
            return SendResult(ok=False, error=f"{type(e).__name__}: {e}")

    async def _reply_card(self, chat_id: str, text: str) -> SendResult:
        """Markdown 内容发 interactive 卡片（JSON 2.0，单 markdown 组件承载全文）。"""
        try:
            from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

            card = _build_markdown_card(text)
            body = CreateMessageRequestBody.builder() \
                .receive_id(chat_id) \
                .msg_type("interactive") \
                .content(card) \
                .build()
            request = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(body) \
                .build()
            response = await asyncio.to_thread(self._client.im.v1.message.create, request)
            if response.success():
                return SendResult(ok=True)
            # 卡片超限（230020 卡片 JSON 过大）等失败 → 返回错误，由 reply 降级纯文本
            return SendResult(ok=False, error=f"feishu card {response.code}: {response.msg}")
        except Exception as e:  # noqa: BLE001
            return SendResult(ok=False, error=f"{type(e).__name__}: {e}")

    async def _reply_post(self, chat_id: str, text: str) -> SendResult:
        """text 被拒时的 post 富文本降级（行级段落，无样式）。"""
        try:
            from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

            post = {
                "post": {
                    "zh_cn": {
                        "title": "Domino",
                        "content": [[{"tag": "text", "text": line}] for line in (text.splitlines() or [text])],
                    }
                }
            }
            import json as _json

            body = CreateMessageRequestBody.builder() \
                .receive_id(chat_id) \
                .msg_type("post") \
                .content(_json.dumps(post, ensure_ascii=False)) \
                .build()
            request = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(body) \
                .build()
            response = await asyncio.to_thread(self._client.im.v1.message.create, request)
            if response.success():
                return SendResult(ok=True)
            return SendResult(ok=False, error=f"feishu post {response.code}: {response.msg}")
        except Exception as e:  # noqa: BLE001
            return SendResult(ok=False, error=f"{type(e).__name__}: {e}")

    # ---- 历史回拉（ADR-008 旁听决策 4）----

    async def backfill(self, chat_id: str, *, since: float = 0.0) -> int:
        """回拉群历史消息灌入旁听缓冲。since 为 Unix 秒下限；返回新增条数。

        幂等性：message_id 去重集合 + L2 observed_messages 主键双保险，
        重启反复补拉无副作用。仅收文本类消息。
        """
        if self._client is None or self._save_observed is None:
            return 0
        try:
            from lark_oapi.api.im.v1 import ListMessageRequest

            # 飞书历史 API 的 start/end_time 单位是秒（实测：毫秒值会被当成远古时间过滤为空集）
            now_s = str(int(time.time()))
            start_s = str(int(since)) if since > 0 else ""
            collected: list[UnifiedMessage] = []
            page_token = ""
            for _ in range(40):  # 40 页 × 50 条 = 2000 条硬上限（配置 1000 条的两倍余量）
                builder = ListMessageRequest.builder() \
                    .container_id_type("chat") \
                    .container_id(chat_id) \
                    .page_size(50) \
                    .sort_type("ByCreateTimeDesc")
                if start_s:
                    builder = builder.start_time(start_s).end_time(now_s)
                if page_token:
                    builder = builder.page_token(page_token)
                resp = await asyncio.to_thread(self._client.im.v1.message.list, builder.build())
                if not resp.success():
                    log.warning(
                        "feishu backfill failed chat_id=%s code=%s msg=%s",
                        chat_id, resp.code, resp.msg,
                    )
                    break
                items = getattr(resp.data, "items", None) or []
                for item in items:
                    msg_type = str(getattr(item, "msg_type", "") or "")
                    if msg_type not in ("text", "post"):
                        continue
                    mid = str(getattr(item, "message_id", "") or "")
                    if not mid or self._dedup.is_duplicate(mid):
                        continue
                    body = _parse_item_body(item, msg_type)
                    if not body:
                        continue
                    create_ms = int(getattr(item, "create_time", "0") or 0)
                    sender_id = ""
                    sender_obj = getattr(item, "sender", None)
                    if sender_obj is not None:
                        sender_id_obj = getattr(sender_obj, "id", None)
                        uid = getattr(sender_id_obj, "user_id", "") or getattr(sender_id_obj, "open_id", "")
                        sender_id = str(uid or "")
                    collected.append(UnifiedMessage(
                        text=body,
                        user_id=sender_id,
                        user_name=sender_id,
                        chat_id=chat_id,
                        message_id=mid,
                        timestamp=create_ms / 1000.0 if create_ms else time.time(),
                    ))
                    if len(collected) >= settings.feishu_backfill_max:
                        break
                has_more = bool(getattr(resp.data, "has_more", False))
                page_token = str(getattr(resp.data, "page_token", "") or "")
                if not has_more or not page_token or len(collected) >= settings.feishu_backfill_max:
                    break
            if collected:
                await self._save_observed(chat_id, collected)
            log.info("feishu backfill chat_id=%s fetched=%d", chat_id, len(collected))
            return len(collected)
        except Exception:  # noqa: BLE001
            log.exception("feishu backfill failed chat_id=%s", chat_id)
            return 0

    # ---- 生命周期 ----

    async def start(self) -> None:
        """启动长连接与 lark 客户端。SDK 导入延迟到这里，便于无 SDK 环境导入本模块。

        ws.start() 为阻塞调用（SDK 内部维持重连），放独立线程运行；P2 事件回调
        亦在 SDK 线程触发，经 run_coroutine_threadsafe 投回主 loop 处理。
        """
        import threading

        import lark_oapi as lark
        from lark_oapi import EventDispatcherHandler
        from lark_oapi.ws import Client as FeishuWSClient

        client = lark.Client.builder() \
            .app_id(settings.feishu_app_id) \
            .app_secret(settings.feishu_app_secret) \
            .domain(settings.feishu_domain) \
            .build()
        self._client = client

        loop = asyncio.get_running_loop()
        channel = self

        def _on_p2_event(event: object) -> None:
            asyncio.run_coroutine_threadsafe(channel.handle_event(event), loop)

        handler = EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(_on_p2_event) \
            .build()

        ws = FeishuWSClient(
            settings.feishu_app_id,
            settings.feishu_app_secret,
            event_handler=handler,
            domain=settings.feishu_domain,
            log_level=lark.LogLevel.INFO,
        )
        threading.Thread(
            target=_ws_start_in_thread,
            args=(ws, settings.feishu_domain),
            daemon=True,
        ).start()

    async def stop(self) -> None:
        await self._http.aclose()


def _ws_start_in_thread(ws: object, domain: str = "") -> None:
    """ws.start() 阻塞运行在独立线程（SDK 自带重连）。

    SDK 的 ws/client.py 在模块导入时绑定了事件循环（主线程导入 → 主 loop），
    在工作线程直接 start() 会撞 "This event loop is already running"。
    与 hermes 同解法：线程内把模块级 loop 换成本线程私有的新 loop 再启动。
    """
    try:
        import asyncio

        import lark_oapi.ws.client as ws_client_module

        thread_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(thread_loop)
        ws_client_module.loop = thread_loop
        ws.start()
    except Exception:  # noqa: BLE001
        log.exception("feishu ws client crashed")


def _extract_mention_names(data: object) -> list[str]:
    """从事件原始数据提取 mentions[].name 列表（与 mentioned_ids 同序）。"""
    if isinstance(data, dict):
        event = data.get("event") or {}
        message = event.get("message") or {}
    else:
        event = getattr(data, "event", None)
        message = getattr(event, "message", None) if event is not None else {}
    raw = getattr(message, "mentions", None)
    if raw is None and isinstance(message, dict):
        raw = message.get("mentions")
    names: list[str] = []
    for m in raw or []:
        if isinstance(m, dict):
            names.append(str(m.get("name", "") or ""))
        else:
            names.append(str(getattr(m, "name", "") or ""))
    return names


def _looks_like_bot_name(name: str, known: set[str]) -> bool:
    """机器人特征名判定：已知名单命中，或显式 Bot 名（飞书群成员名带「机器人」标记）。

    事件中 @ 机器人时 name 为机器人显示名（如「Domino」）。学习入口有二：
    1. 显示名含 Bot 特征（「机器人」字样 / bot 后缀 / Domino 本名）→ 直接学；
    2. 首条被 @ 且机器人随后成功回复的消息（说明真是 @ 我）→ 名单自增。
    判不准的名字不学，宁可漏听（走旁听）不可误答。
    """
    if not name:
        return False
    if name in known:
        return True
    n = name.strip().lower()
    return n.endswith("bot") or "机器人" in n or n == "domino"


def _json_content(text: str) -> str:
    import json

    return json.dumps({"text": text}, ensure_ascii=False)


# 高置信 Markdown 特征：命中任一才走卡片（保守判定，纯文本绝不误入卡片路径）
_MD_PATTERNS = (
    re.compile(r"^\s*\|.+\|\s*$", re.M),              # 表格行（| ... |）
    re.compile(r"^#{1,6}\s+\S", re.M),                # ATX 标题
    re.compile(r"^```", re.M),                        # 代码围栏
    re.compile(r"\*\*[^*\n]+\*\*"),                   # 加粗
    re.compile(r"^\s*[-*+]\s+\S", re.M),              # 无序列表
    re.compile(r"^\s*\d+\.\s+\S", re.M),              # 有序列表
)


def _looks_like_markdown(text: str) -> bool:
    return any(p.search(text) for p in _MD_PATTERNS)


def _build_markdown_card(text: str) -> str:
    """构造 JSON 2.0 卡片：单 markdown 组件承载全文。

    JSON 2.0 的 markdown 组件支持标题/表格/代码块/列表语法。
    实测：带 config.update_multi=false 会被拒（230099/ErrCode 300302
    "update_multi is false"），省略 config 整段即可发送成功。
    """
    import json

    card = {
        "schema": "2.0",
        "body": {
            "elements": [
                {"tag": "markdown", "content": text},
            ]
        },
    }
    return json.dumps(card, ensure_ascii=False)


def _parse_item_body(item: object, msg_type: str) -> str:
    """历史消息条目的 body.content 解析（与事件解析共用 text/post 逻辑）。

    回拉条目同样带 mentions（key→name 映射），占位符 @_user_1 需还原为显示名。
    """
    import json as _json

    from app.channels.feishu_parse import _render_post_text, _restore_placeholders, _strip_at_tags

    body_obj = getattr(item, "body", None)
    content = str(getattr(body_obj, "content", "") or "") if body_obj is not None else ""
    if not content:
        return ""
    placeholder_map: dict[str, str] = {}
    for m in getattr(item, "mentions", None) or []:
        key = str(getattr(m, "key", "") or "")
        name = str(getattr(m, "name", "") or "")
        if key and name:
            placeholder_map[key] = name
    try:
        obj = _json.loads(content)
    except (ValueError, TypeError):
        return ""
    if msg_type == "text":
        text = _strip_at_tags(str(obj.get("text", "") or ""))
        return _restore_placeholders(text, placeholder_map).strip()
    return _restore_placeholders(_render_post_text(content), placeholder_map)


def _split(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]
