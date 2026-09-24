"""FeishuChannel：飞书长连接通道（ADR-008）。

- lark-oapi SDK WebSocket 长连接，订阅 im.message.receive_v1，无需公网回调地址；
- 全量接收群消息：@ 消息走 on_message 管线，非 @ 消息写入旁听缓冲（不触发 LLM）；
- 门控沿用钉钉三开关语义（群白名单/用户白名单/@ 门控）；
- 回复走 bot 发消息 API（tenant_access_token 由 SDK 管理），按 chat_id 寻址；
- 历史回拉：入群事件 + 重启双触发，message_id 幂等。

SDK 导入延迟到 start()/_client 初始化，便于无 SDK 环境导入本模块做测试。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Awaitable, Callable

import httpx

from app.channels.base import ChannelAdapter, SendResult, UnifiedMessage
from app.channels.feishu_parse import ParsedFeishuMessage, parse_message
from app.config import settings

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
        self._client: object | None = None  # lark.Client，start() 时初始化

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
        sender_type = str(sender.get("sender_type", "") if isinstance(sender, dict) else getattr(sender, "sender_type", "") or "")
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
        """
        client = self._client
        if client is None:
            return SendResult(ok=False, error="feishu client not started")
        try:
            import lark_oapi as lark
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

    async def _reply_post(self, chat_id: str, text: str) -> SendResult:
        """text 被拒时的 post 富文本降级（行级段落，无样式）。"""
        try:
            import lark_oapi as lark
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

            start_time = str(int(since * 1000)) if since > 0 else ""
            collected: list[UnifiedMessage] = []
            page_token = ""
            for _ in range(40):  # 40 页 × 50 条 = 2000 条硬上限（配置 1000 条的两倍余量）
                builder = ListMessageRequest.builder() \
                    .container_id_type("chat") \
                    .container_id(chat_id) \
                    .page_size(50) \
                    .sort_type("ByCreateTimeDesc")
                if start_time:
                    builder = builder.start_time(start_time)
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
                        sender_id = str(getattr(sender_id_obj, "user_id", "") or getattr(sender_id_obj, "open_id", "") or "")
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
            log_level=lark.LogLevel.INFO,
        )
        threading.Thread(target=_ws_start_in_thread, args=(ws,), daemon=True).start()

    async def stop(self) -> None:
        await self._http.aclose()


def _ws_start_in_thread(ws: object) -> None:
    """ws.start() 阻塞运行在独立线程（SDK 自带重连）。"""
    try:
        ws.start()
    except Exception:  # noqa: BLE001
        log.exception("feishu ws client crashed")


def _json_content(text: str) -> str:
    import json

    return json.dumps({"text": text}, ensure_ascii=False)


def _parse_item_body(item: object, msg_type: str) -> str:
    """历史消息条目的 body.content 解析（与事件解析共用 text/post 逻辑）。"""
    import json as _json

    from app.channels.feishu_parse import _render_post_text, _strip_at_tags

    body_obj = getattr(item, "body", None)
    content = str(getattr(body_obj, "content", "") or "") if body_obj is not None else ""
    if not content:
        return ""
    try:
        obj = _json.loads(content)
    except (ValueError, TypeError):
        return ""
    if msg_type == "text":
        return _strip_at_tags(str(obj.get("text", "") or "")).strip()
    return _render_post_text(content)


def _split(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]
