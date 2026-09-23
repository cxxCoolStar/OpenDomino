"""DingTalkChannel：钉钉 Stream 模式通道（ADR-001）。

- Stream 模式连接（dingtalk-stream SDK），无需公网回调地址；
- 群聊 @ 门控（结构化判断）、用户/群白名单；
- session_webhook 缓存 + 过期判断、markdown 回复；
- 消息去重、断线重连（SDK 自带）。
SDK 导入延迟到 start()，便于无 SDK 环境下导入本模块做测试。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from typing import Awaitable, Callable

import httpx

from app.channels.base import ChannelAdapter, SendResult, UnifiedMessage
from app.channels.dingtalk_parse import ParsedDingTalkMessage, parse_message
from app.config import settings

log = logging.getLogger(__name__)

MessageHandler = Callable[[UnifiedMessage], Awaitable[None]]

# session_webhook 有效期（官方约 5 分钟，留 30s 余量）
_WEBHOOK_TTL_SECONDS = 4.5 * 60
_DEDUP_CAPACITY = 2048


class MessageDeduplicator:
    """按 message_id 去重，防 Stream 重发。内存集合 + 容量上限。"""

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


class DingTalkChannel(ChannelAdapter):
    """钉钉群聊通道。on_message 回调由上层注入，门控后转为 UnifiedMessage。"""

    def __init__(self, on_message: MessageHandler) -> None:
        self._on_message = on_message
        self._dedup = MessageDeduplicator()
        self._webhook_cache: dict[str, tuple[str, float]] = {}  # chat_id -> (webhook, expire_at)
        self._http = httpx.AsyncClient(timeout=15)

    # ---- 门控 ----

    def _allowed(self, msg: ParsedDingTalkMessage) -> bool:
        # 群白名单（硬门控）
        allowed_chats = _split(settings.dingtalk_allowed_chats)
        if allowed_chats and msg.chat_id not in allowed_chats:
            return False
        # 用户白名单（"*" = 所有人）
        allowed_users = _split(settings.dingtalk_allowed_users)
        if allowed_users and "*" not in allowed_users and msg.user_id not in allowed_users:
            return False
        # 仅群聊（第一阶段无私聊）
        if not msg.is_group:
            return False
        # @ 门控：require_mention 且该群不在免 @ 名单时，必须 @ 机器人
        free_chats = _split(settings.dingtalk_free_response_chats)
        if settings.dingtalk_require_mention and msg.chat_id not in free_chats:
            if not msg.is_at_bot:
                return False
        return True

    # ---- 收消息 ----

    async def handle_raw(self, data: object) -> bool:
        """处理一条 SDK 原始报文。返回是否被采纳（通过门控且未去重）。"""
        msg = parse_message(data)
        if msg.message_id and self._dedup.is_duplicate(msg.message_id):
            log.info("dingtalk duplicate message_id=%s dropped", msg.message_id)
            return False
        if not self._allowed(msg):
            log.info(
                "dingtalk message rejected by gate: message_id=%s chat_id=%s user=%s "
                "is_group=%s is_at_bot=%s",
                msg.message_id, msg.chat_id, msg.user_id, msg.is_group, msg.is_at_bot,
            )
            return False
        log.info(
            "dingtalk message accepted: message_id=%s chat_id=%s user=%s text=%r",
            msg.message_id, msg.chat_id, msg.user_id, msg.text[:80],
        )
        if msg.session_webhook:
            self._cache_webhook(msg.chat_id, msg.session_webhook)
        unified = UnifiedMessage(
            text=msg.text,
            user_id=msg.user_id,
            user_name=msg.user_name,
            chat_id=msg.chat_id,
            chat_name=msg.chat_name,
            is_group=msg.is_group,
            message_id=msg.message_id,
            is_at_bot=msg.is_at_bot,
            session_webhook=msg.session_webhook,
            raw=msg.raw,
        )
        await self._on_message(unified)
        return True

    # ---- 发消息 ----

    def _cache_webhook(self, chat_id: str, webhook: str) -> None:
        self._webhook_cache[chat_id] = (webhook, time.time() + _WEBHOOK_TTL_SECONDS)

    def _fresh_webhook(self, chat_id: str, fallback: str = "") -> str:
        if fallback:
            return fallback
        cached = self._webhook_cache.get(chat_id)
        if cached and cached[1] > time.time():
            return cached[0]
        if cached:
            del self._webhook_cache[chat_id]
        return ""

    async def reply(self, chat_id: str, text: str, *, session_webhook: str = "") -> SendResult:
        """回复群消息。

        主通道：钉钉机器人主动发消息 API（无过期，适配任务回传——session_webhook
        5 分钟过期而任务要跑几十分钟）。API 失败时退回 session_webhook（来自
        缓存或调用方传入），再失败返回错误由上层落 TaskEvent 兜底。
        """
        if settings.dingtalk_robot_messages_enabled:
            r = await self._send_via_robot(chat_id, text)
            if r.ok:
                return r
            log.warning("robot send failed, falling back to webhook: %s", r.error)
        webhook = self._fresh_webhook(chat_id, fallback=session_webhook)
        if not webhook:
            return SendResult(ok=False, error=f"no fresh session_webhook for chat {chat_id}")
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": text[:20] or "Domino", "text": text},
        }
        try:
            resp = await self._http.post(webhook, json=payload)
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if resp.status_code == 200 and body.get("errcode", 0) == 0:
                return SendResult(ok=True)
            return SendResult(ok=False, error=f"http {resp.status_code}: {body or resp.text[:200]}")
        except Exception as e:  # noqa: BLE001
            return SendResult(ok=False, error=f"{type(e).__name__}: {e}")

    # ---- 机器人主动发消息 API（ADR-004 回传主通道）----

    _token_cache: tuple[str, float] | None = None   # (access_token, expire_at)

    async def _access_token(self) -> str:
        cached = DingTalkChannel._token_cache
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        resp = await self._http.post(
            "https://api.dingtalk.com/v1.0/oauth2/accessToken",
            json={"appKey": settings.dingtalk_client_id, "appSecret": settings.dingtalk_client_secret},
        )
        resp.raise_for_status()
        data = resp.json()
        token, expire = data["accessToken"], int(data.get("expireIn", 7200))
        DingTalkChannel._token_cache = (token, time.time() + expire)
        return token

    async def _send_via_robot(self, chat_id: str, text: str) -> SendResult:
        robot_code = settings.dingtalk_robot_code or settings.dingtalk_client_id
        if not robot_code:
            return SendResult(ok=False, error="dingtalk_robot_code/client_id not configured")
        try:
            token = await self._access_token()
            resp = await self._http.post(
                "https://api.dingtalk.com/v1.0/robot/groupMessages/send",
                headers={"x-acs-dingtalk-access-token": token},
                json={
                    "robotCode": robot_code,
                    "openConversationId": chat_id,
                    "msgKey": "sampleMarkdown",
                    "msgParam": json.dumps(
                        {"title": text[:20] or "Domino", "text": text}, ensure_ascii=False
                    ),
                },
            )
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if resp.status_code < 300 and not body.get("code"):
                return SendResult(ok=True)
            return SendResult(ok=False, error=f"robot api {resp.status_code}: {body or resp.text[:200]}")
        except Exception as e:  # noqa: BLE001
            return SendResult(ok=False, error=f"{type(e).__name__}: {e}")

    # ---- 生命周期 ----

    async def start(self) -> None:
        """启动 Stream 长连接。SDK 导入延迟到这里，便于无 SDK 环境导入本模块。"""
        from dingtalk_stream import (  # type: ignore
            AckMessage,
            ChatbotHandler,
            ChatbotMessage,
            Credential,
            DingTalkStreamClient,
        )

        client = DingTalkStreamClient(
            Credential(settings.dingtalk_client_id, settings.dingtalk_client_secret)
        )

        channel = self

        class _Handler(ChatbotHandler):  # SDK requires a ChatbotHandler instance
            async def process(self, message):  # noqa: ANN001
                # SDK >= 0.20 passes a CallbackMessage envelope; the chatbot
                # payload dict is in .data. Unwrap it so parse_message sees the
                # raw DingTalk fields (conversationId, isInAtList, ...).
                data = getattr(message, "data", message)
                if isinstance(data, str):
                    import json

                    data = json.loads(data)
                # ACK quickly so a slow LLM response does not trigger DingTalk redelivery.
                task = asyncio.create_task(channel.handle_raw(data))
                task.add_done_callback(_log_task_error)
                return AckMessage.STATUS_OK, "ok"

        client.register_callback_handler(ChatbotMessage.TOPIC, _Handler())
        # start() 为阻塞协程（SDK 内部维持重连），交给调用方以任务方式运行
        await client.start()


def _log_task_error(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("DingTalk message processing failed")


def _split(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]
