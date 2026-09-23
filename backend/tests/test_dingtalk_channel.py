"""DingTalkChannel 门控与去重单元测试（不依赖钉钉 SDK 连接）。"""
from __future__ import annotations

import pytest

from app.channels.base import UnifiedMessage
from app.channels.dingtalk import DingTalkChannel, MessageDeduplicator
from app.config import settings


def _group_msg(msg_id: str = "m1", chat_id: str = "c1", user_id: str = "u1", at: bool = True):
    return {
        "conversationType": "2",
        "conversationId": chat_id,
        "msgId": msg_id,
        "senderStaffId": user_id,
        "senderNick": "n",
        "sessionWebhook": "https://hook/session=" + msg_id,
        "is_in_at_list": at,
        "text": {"content": "hi"},
    }


@pytest.fixture
def collected():
    msgs: list[UnifiedMessage] = []

    async def on_message(m: UnifiedMessage) -> None:
        msgs.append(m)

    return msgs, on_message


@pytest.mark.asyncio
async def test_at_required_blocks_non_at(collected, monkeypatch):
    monkeypatch.setattr(settings, "dingtalk_require_mention", True)
    monkeypatch.setattr(settings, "dingtalk_allowed_users", "*")
    monkeypatch.setattr(settings, "dingtalk_allowed_chats", "")
    monkeypatch.setattr(settings, "dingtalk_free_response_chats", "")
    msgs, on_message = collected
    ch = DingTalkChannel(on_message)
    ok = await ch.handle_raw(_group_msg(at=False))
    assert ok is False and msgs == []


@pytest.mark.asyncio
async def test_at_passes_when_at(collected, monkeypatch):
    monkeypatch.setattr(settings, "dingtalk_require_mention", True)
    monkeypatch.setattr(settings, "dingtalk_allowed_users", "*")
    monkeypatch.setattr(settings, "dingtalk_allowed_chats", "")
    monkeypatch.setattr(settings, "dingtalk_free_response_chats", "")
    msgs, on_message = collected
    ch = DingTalkChannel(on_message)
    ok = await ch.handle_raw(_group_msg(at=True))
    assert ok is True and len(msgs) == 1


@pytest.mark.asyncio
async def test_free_response_chat_bypasses_at(collected, monkeypatch):
    monkeypatch.setattr(settings, "dingtalk_require_mention", True)
    monkeypatch.setattr(settings, "dingtalk_allowed_users", "*")
    monkeypatch.setattr(settings, "dingtalk_allowed_chats", "")
    monkeypatch.setattr(settings, "dingtalk_free_response_chats", "c1")
    msgs, on_message = collected
    ch = DingTalkChannel(on_message)
    ok = await ch.handle_raw(_group_msg(at=False))
    assert ok is True


@pytest.mark.asyncio
async def test_allowed_chats_hard_gate(collected, monkeypatch):
    monkeypatch.setattr(settings, "dingtalk_require_mention", False)
    monkeypatch.setattr(settings, "dingtalk_allowed_users", "*")
    monkeypatch.setattr(settings, "dingtalk_allowed_chats", "other")
    monkeypatch.setattr(settings, "dingtalk_free_response_chats", "")
    msgs, on_message = collected
    ch = DingTalkChannel(on_message)
    ok = await ch.handle_raw(_group_msg(at=True))
    assert ok is False


@pytest.mark.asyncio
async def test_single_chat_blocked(collected, monkeypatch):
    monkeypatch.setattr(settings, "dingtalk_require_mention", False)
    monkeypatch.setattr(settings, "dingtalk_allowed_users", "*")
    monkeypatch.setattr(settings, "dingtalk_allowed_chats", "")
    monkeypatch.setattr(settings, "dingtalk_free_response_chats", "")
    msgs, on_message = collected
    ch = DingTalkChannel(on_message)
    dm = _group_msg()
    dm["conversationType"] = "1"
    ok = await ch.handle_raw(dm)
    assert ok is False


def test_deduplicator():
    d = MessageDeduplicator(capacity=3)
    assert d.is_duplicate("a") is False
    assert d.is_duplicate("a") is True
    assert d.is_duplicate("") is False
    d.is_duplicate("b"); d.is_duplicate("c"); d.is_duplicate("d")  # 超出容量挤出 a
    assert d.is_duplicate("a") is False  # a 已被挤出，重新计为新


@pytest.mark.asyncio
async def test_duplicate_message_dropped(collected, monkeypatch):
    monkeypatch.setattr(settings, "dingtalk_require_mention", False)
    monkeypatch.setattr(settings, "dingtalk_allowed_users", "*")
    monkeypatch.setattr(settings, "dingtalk_allowed_chats", "")
    monkeypatch.setattr(settings, "dingtalk_free_response_chats", "")
    msgs, on_message = collected
    ch = DingTalkChannel(on_message)
    assert await ch.handle_raw(_group_msg(msg_id="dup")) is True
    assert await ch.handle_raw(_group_msg(msg_id="dup")) is False
    assert len(msgs) == 1
