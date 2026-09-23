"""dingtalk_parse 纯函数单元测试。"""
from __future__ import annotations

from app.channels.dingtalk_parse import extract_text, parse_message


def test_parse_group_text_message():
    data = {
        "conversationType": "2",
        "conversationId": "cid123",
        "conversationTitle": "研发群",
        "msgId": "msg001",
        "senderStaffId": "user001",
        "senderNick": "星星",
        "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=xxx",
        "is_in_at_list": True,
        "text": {"content": "@Domino 发布流程是什么"},
    }
    msg = parse_message(data)
    assert msg.text == "@Domino 发布流程是什么"
    assert msg.is_group is True
    assert msg.chat_id == "cid123"
    assert msg.user_id == "user001"
    assert msg.user_name == "星星"
    assert msg.message_id == "msg001"
    assert msg.is_at_bot is True
    assert "session=" in msg.session_webhook


def test_parse_single_chat_marked_not_group():
    data = {
        "conversationType": "1",
        "conversationId": "cid_dm",
        "msgId": "msg002",
        "senderStaffId": "user002",
        "text": {"content": "hi"},
    }
    msg = parse_message(data)
    assert msg.is_group is False


def test_extract_text_from_plain_content_field():
    assert extract_text({"content": "  你好  "}) == "你好"


def test_extract_text_empty():
    assert extract_text({}) == ""
    assert extract_text({"text": {}}) == ""


def test_is_at_bot_via_at_users_list():
    data = {"conversationType": "2", "atUsers": [{"dingtalkId": "bot"}], "text": {"content": "x"}}
    assert parse_message(data).is_at_bot is True


def test_is_at_bot_false_when_no_at():
    data = {"conversationType": "2", "text": {"content": "普通消息"}}
    assert parse_message(data).is_at_bot is False
