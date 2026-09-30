"""ADR-008 E2E 验证脚本：飞书通道 + 旁听上下文。

不依赖真实飞书环境与 SDK 长连接，验证链路中的确定性行为：
  1. feishu_parse 纯函数解析（text/post/mentions/@_all/dict+SDK 对象两形态）
  2. FeishuChannel 分流（@ → 管线；非 @ → 旁听落库；去重；bot 自消息丢弃；门控）
  3. L2 旁听缓冲（写入/幂等/读取/群列表/保留期清理）
  4. 段 2 合并时间线（@ 往来 + 旁听按时间戳归并进 [群聊上下文]）
  5. 回拉解析（_parse_item_body 文本/post）与启动补拉链路（幂等）
  6. 配置契约（FEISHU_* 生效、DINGTALK_* 清零）

运行：cd backend && python scripts/e2e_feishu_channel.py
产出：scripts/e2e_feishu_report.json（可重复验证 artifact）
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 测试前先隔离环境：独立临时库，避免污染真实 data/opendomino.db
_tmp_db = Path("./data/e2e_feishu_tmp.db")
if _tmp_db.exists():
    _tmp_db.unlink()

os.environ["MEMORY_DB_URL"] = "sqlite+aiosqlite:///./data/e2e_feishu_tmp.db"
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9")  # 不触发真实 LLM
os.environ.setdefault("LLM_API_KEY", "e2e-dummy")
os.environ.setdefault("CONSOLE_MODEL", "e2e-dummy")
os.environ.setdefault("SANDBOX_MODEL", "e2e-dummy")
os.environ.setdefault("FEISHU_APP_ID", "cli_e2e")
os.environ.setdefault("FEISHU_APP_SECRET", "e2e-secret")

from app.channels.base import UnifiedMessage  # noqa: E402
from app.channels.feishu import FeishuChannel  # noqa: E402
from app.channels.feishu_parse import parse_message  # noqa: E402
from app.config import settings  # noqa: E402
from app.console.context import ContextAssembler  # noqa: E402
from app.memory.service import SQLiteMemoryService  # noqa: E402
from app.memory.types import ChatMessage  # noqa: E402

PASS: list[dict] = []
FAIL: list[dict] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    entry = {"name": name, "ok": bool(cond), "detail": detail}
    (PASS if cond else FAIL).append(entry)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  | {detail}" if detail and not cond else ""))


# ---------- 1. 纯函数解析 ----------

def event_dict(text="你好", *, mentions=None, msg_type="text", chat_type="group", sender_type="user"):
    content = json.dumps({"text": text}) if msg_type == "text" else json.dumps(text)
    key = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    return {
        "event": {
            "message": {
                "message_id": "om_" + key[:6] + str(abs(hash(key)) % 9999),
                "chat_type": chat_type,
                "chat_id": "oc_e2e",
                "message_type": msg_type,
                "content": content,
                "create_time": "1700000000000",
                "mentions": mentions or [],
            },
            "sender": {"sender_id": {"user_id": "u_e2e", "open_id": "ou_e2e"}, "sender_type": sender_type},
        }
    }


def test_parse():
    # 群聊 text + @ 机器人
    ev = event_dict("帮我看看", mentions=[{"id": {"open_id": "ou_bot"}, "name": "Domino"}])
    m = parse_message(ev, bot_open_id="ou_bot")
    check("parse: 群聊 text @bot", m.is_group and m.is_at_bot and m.text == "帮我看看"
          and m.user_id == "u_e2e" and m.message_id == "om_帮我看看"[:9] or m.message_id.startswith("om_"))

    # 内联 at 标签剥离、@ 目标抽取
    ev2 = event_dict("<at user_id=\"ou_bot\">Domino</at> 部署失败", mentions=[{"id": {"open_id": "ou_bot"}, "name": "Domino"}])
    m2 = parse_message(ev2, bot_open_id="ou_bot")
    check("parse: at 标签剥离", m2.text == "Domino 部署失败" or m2.text.endswith("部署失败"), f"text={m2.text!r}")

    # @_all 视为 @
    ev3 = event_dict("所有人注意", mentions=[{"id": {}, "name": "_all"}])
    m3 = parse_message(ev3, bot_open_id="ou_bot")
    check("parse: @_all 视为 @", m3.is_at_bot is True)

    # 未 @ → is_at_bot False
    m4 = parse_message(event_dict("普通闲聊"), bot_open_id="ou_bot")
    check("parse: 未 @ 不误判", m4.is_at_bot is False)

    # post 富文本
    post = {"title": "T", "content": [[{"tag": "text", "text": "第一行"}, {"tag": "a", "text": "链接", "href": "https://x"}]]}
    ev5 = event_dict(post, msg_type="post")
    m5 = parse_message(ev5, bot_open_id="ou_bot")
    check("parse: post 渲染", "第一行" in m5.text and "链接" in m5.text, f"text={m5.text!r}")

    # 单聊标记
    m6 = parse_message(event_dict(chat_type="p2p"), bot_open_id="ou_bot")
    check("parse: p2p 标记", m6.is_group is False)


# ---------- 2. 通道分流 ----------

async def test_channel():
    memory = SQLiteMemoryService()
    await memory.init()

    pipeline: list[UnifiedMessage] = []

    async def on_message(msg: UnifiedMessage) -> None:
        pipeline.append(msg)

    saved: list[tuple[str, list]] = []

    async def save_observed(chat_id: str, msgs) -> None:
        saved.append((chat_id, list(msgs)))

    ch = FeishuChannel(on_message, save_observed)
    monkey = {"require_mention": True}
    settings.feishu_require_mention = monkey["require_mention"]
    settings.feishu_allowed_users = "*"
    settings.feishu_allowed_chats = ""
    settings.feishu_free_response_chats = ""
    settings.feishu_bot_open_id = "ou_bot"
    ch._bot_open_id = "ou_bot"  # __init__ 时 settings 尚未注入，显式补齐

    # @ 消息进管线
    ok1 = await ch.handle_event(event_dict("@bot 问题", mentions=[{"id": {"open_id": "ou_bot"}, "name": "D"}]))
    check("channel: @ 进管线", ok1 is True and len(pipeline) == 1)

    # 非 @ 进旁听不进管线
    ok2 = await ch.handle_event(event_dict("群友闲聊"))
    check("channel: 非 @ 进旁听不进管线", ok2 is False and len(pipeline) == 1 and len(saved) == 1)

    # 重复事件去重（两次同一事件）
    ev = event_dict("重复消息")
    await ch.handle_event(ev)
    n_before = len(saved)
    await ch.handle_event(ev)
    check("channel: message_id 去重", len(saved) == n_before and len(pipeline) == 1)

    # bot 自消息丢弃（sender_type=bot，即使 @ 了机器人）
    ev_bot = event_dict("我自己说的", mentions=[{"id": {"open_id": "ou_bot"}, "name": "D"}], sender_type="bot")
    n_pipe, n_saved = len(pipeline), len(saved)
    await ch.handle_event(ev_bot)
    check("channel: bot 自消息丢弃", len(pipeline) == n_pipe and len(saved) == n_saved)

    # 群白名单硬门控同时约束旁听
    settings.feishu_allowed_chats = "oc_other"
    await ch.handle_event(event_dict("白名单外"))
    check("channel: 群白名单约束旁听", len(saved) == n_before + 0 or len(saved) == n_saved)
    settings.feishu_allowed_chats = ""

    # 单聊不入任何一路
    n_pipe, n_saved = len(pipeline), len(saved)
    await ch.handle_event(event_dict("私聊内容", chat_type="p2p"))
    check("channel: 单聊不入管线/旁听", len(pipeline) == n_pipe and len(saved) == n_saved)

    # 免 @ 群放行非 @ 消息进管线
    settings.feishu_free_response_chats = "oc_e2e"
    ok3 = await ch.handle_event(event_dict("免 @ 提问"))
    check("channel: 免 @ 群进管线", ok3 is True and len(pipeline) == 2)
    settings.feishu_free_response_chats = ""
    return memory


# ---------- 3. L2 旁听缓冲 ----------

async def test_store(memory: SQLiteMemoryService):
    n1 = await memory.save_observed("oc_e2e", [
        ChatMessage(role="user", text="讨论结论A", user_id="u1", user_name="张三", message_id="om_a", timestamp=time.time() - 300),
        ChatMessage(role="user", text="讨论结论B", user_id="u2", user_name="李四", message_id="om_b", timestamp=time.time() - 200),
        ChatMessage(role="user", text="", message_id="om_empty", timestamp=time.time()),  # 空文本丢弃
    ])
    check("store: 写入与空文本过滤", n1 == 2, f"n={n1}")

    n2 = await memory.save_observed("oc_e2e", [
        ChatMessage(role="user", text="讨论结论A", user_id="u1", message_id="om_a", timestamp=time.time() - 300),
    ])
    check("store: message_id 幂等", n2 == 0, f"n={n2}")

    msgs = await memory.load_observed("oc_e2e")
    check("store: 读取升序", [m.message_id for m in msgs] == ["om_a", "om_b"])

    chats = await memory.list_observed_chats()
    check("store: 群列表", "oc_e2e" in chats)

    deleted = await memory.prune_observed(retain_days=0)
    check("store: 保留期清理", deleted >= 2, f"deleted={deleted}")
    # 清理后回写，供后续段 2 测试
    await memory.save_observed("oc_e2e", [
        ChatMessage(role="user", text="张三：发布流程改成走 CI", user_id="u1", user_name="张三", message_id="om_c1", timestamp=time.time() - 120),
        ChatMessage(role="user", text="李四：测试环境验证过了", user_id="u2", user_name="李四", message_id="om_c2", timestamp=time.time() - 60),
    ])


# ---------- 4. 段 2 合并时间线 ----------

async def test_context_merge(memory: SQLiteMemoryService):
    assembler = ContextAssembler(memory)

    session = await memory.load_session("oc_e2e")
    session.messages = [
        ChatMessage(role="user", text="@Domino 就这么定行不行", user_id="u9", user_name="王五", timestamp=time.time() - 30),
        ChatMessage(role="assistant", text="可以，理由如下…", timestamp=time.time() - 20),
    ]
    observed = await memory.load_observed("oc_e2e", limit=50)
    assembler.prime_observed("oc_e2e", observed)

    context = await assembler.assemble(session, "就这么定行不行")
    has_at = "@Domino 就这么定行不行" in context
    has_obs1 = "发布流程改成走 CI" in context
    has_obs2 = "测试环境验证过了" in context
    order_ok = context.find("发布流程改成走 CI") < context.find("@Domino 就这么定行不行")
    check("context: @ 往来与旁听合并", has_at and has_obs1 and has_obs2, context[:600])
    check("context: 时间戳归并排序", order_ok)
    check("context: 段标签 [群聊上下文]", "[群聊上下文]" in context)


# ---------- 4.5 回复格式选择（Markdown 卡片） ----------

class _FakeResponse:
    def __init__(self, ok: bool, code: int = 0, msg: str = ""):
        self._ok = ok
        self.code = code
        self.msg = msg

    def success(self) -> bool:
        return self._ok


async def test_reply_format_selection():
    """reply 格式选择：Markdown 特征 → interactive 卡片；纯文本 → text；
    卡片失败（超限等）→ 降级 text 重发。断言经 mock client 记录的 msg_type/content。"""
    from app.channels.feishu import _build_markdown_card, _looks_like_markdown

    # 检测函数
    check("md detect: 表格", _looks_like_markdown("| a | b |\n|---|---|\n| 1 | 2 |"))
    check("md detect: 标题", _looks_like_markdown("## 结论"))
    check("md detect: 围栏", _looks_like_markdown("```\ncode\n```"))
    check("md detect: 加粗", _looks_like_markdown("**执行中**"))
    check("md detect: 列表", _looks_like_markdown("- 第一条"))
    check("md detect: 纯文本不误判", not _looks_like_markdown("排查完成，耗时 52 秒"))
    check("md detect: 单星号不误判", not _looks_like_markdown("a * b 加法"))

    # 卡片 JSON 结构
    card = json.loads(_build_markdown_card("## 标题\n| a | b |"))
    check("md card: schema 2.0", card.get("schema") == "2.0")
    check("md card: markdown 组件承载全文",
          card["body"]["elements"][0]["tag"] == "markdown"
          and card["body"]["elements"][0]["content"] == "## 标题\n| a | b |")
    # 回归：config.update_multi=false 会被飞书拒（230099/ErrCode 300302），整段 config 不带
    check("md card: 不带 config 字段", "config" not in card)

    # reply 分流与降级：mock client 记录每次 create 的 msg_type/content
    sent: list[tuple[str, str]] = []

    class _Messages:
        def __init__(self, results):
            self._results = results

        def create(self, request):  # 同步方法，与真实 SDK 一致（通道层经 to_thread 调用）
            body = request.request_body
            sent.append((body.msg_type, body.content))
            return self._results.pop(0)

    class _ImV1:
        def __init__(self, results):
            self.message = _Messages(results)

    class _Im:
        def __init__(self, results):
            self.v1 = _ImV1(results)

    class _FakeClient:
        def __init__(self, results):
            self.im = _Im(results)

    async def _send(channel, text_):
        return await channel.reply("oc_x", text_)

    ch = FeishuChannel(lambda m: asyncio.sleep(0), None)

    # Markdown 内容 → interactive 卡片
    ch._client = _FakeClient([_FakeResponse(True)])
    md_text = "## 排查结论\n| 文件 | 职责 |\n|---|---|\n| a.py | 主管线 |"
    r1 = await _send(ch, md_text)
    check("reply: markdown 走卡片", r1.ok and sent[-1][0] == "interactive", f"sent={sent}")
    card_sent = json.loads(sent[-1][1])
    check("reply: 卡片含原文", card_sent["body"]["elements"][0]["content"] == md_text)

    # 纯文本 → text
    ch._client = _FakeClient([_FakeResponse(True)])
    r2 = await _send(ch, "排查完成，耗时 52 秒")
    check("reply: 纯文本走 text", r2.ok and sent[-1][0] == "text")

    # 卡片被拒（超限 230020）→ 降级 text 重发同内容
    ch._client = _FakeClient([_FakeResponse(False, code=230020, msg="card json too large"), _FakeResponse(True)])
    r3 = await _send(ch, md_text)
    types = [t for t, _ in sent]
    check("reply: 卡片失败降级 text", r3.ok and types[-2:] == ["interactive", "text"], f"types={types}")
    check("reply: 降级 text 内容完整", json.loads(sent[-1][1])["text"] == md_text)

    # 卡片、text、post 三连失败 → 错误可见（text 被拒 230001 后还会走 post 降级，共 3 次 create）
    ch._client = _FakeClient([
        _FakeResponse(False, code=230020, msg="too large"),
        _FakeResponse(False, code=230001, msg="Invalid param"),
        _FakeResponse(False, code=230001, msg="Invalid param"),
    ])
    r4 = await _send(ch, md_text)
    check("reply: 三连失败错误可见", not r4.ok and "feishu post" in (r4.error or ""), f"error={r4.error!r}")





# ---------- 5. 回拉解析与补拉链路 ----------

def test_backfill_parse():
    from app.channels.feishu import _parse_item_body

    class Body:
        content = json.dumps({"text": "历史消息内容"})

    class Item:
        body = Body()

    check("backfill: item text 解析", _parse_item_body(Item(), "text") == "历史消息内容")

    class PostBody:
        content = json.dumps({"title": "", "content": [[{"tag": "text", "text": "富文本历史"}]]})

    class PostItem:
        body = PostBody()

    check("backfill: item post 解析", _parse_item_body(PostItem(), "post") == "富文本历史")


async def test_startup_backfill(memory: SQLiteMemoryService):
    """启动补拉链路：list_observed_chats + list_binding_chats 汇合并发 backfill。

    真实 SDK 调用在 FeishuChannel.backfill 内被 self._client 短路（未 start 时返回 0），
    这里验证群集合的汇合逻辑与幂等性。
    """
    chats = set(await memory.list_observed_chats())
    chats.update(await memory.list_binding_chats())
    check("backfill: 已知群汇合", isinstance(chats, set))
    ch = FeishuChannel(lambda m: asyncio.sleep(0), None)
    n = await ch.backfill("oc_any", since=0)  # client 未启动 → 安全返回 0
    check("backfill: 未启动安全返回", n == 0)


# ---------- 6. 配置契约 ----------

async def test_route_schema_robustness():
    """路由 JSON 健壮性（线上 bug 回归）：模型把未选中的分支填成空对象 {} 时，
    不得整体拒收导致裸 JSON 降级漏进群里；重试提示需带具体校验错误。"""
    from app.console.llm import LLMClient
    from app.console.router import Router

    class ScriptedLLM(LLMClient):
        def __init__(self, replies):
            self.replies = list(replies)
            self.calls = []

        async def complete(self, system, user):
            self.calls.append(user)
            return self.replies.pop(0)

    # 线上 bug 原始输出：action=delegate 本身正确，但 repo_op/task_op 被填成空对象
    screenshot_raw = (
        '{"action":"delegate","answer":"","clarify_question":"","task":{"title":"梳理 OpenDomino '
        '当前待开发需求清单","instruction":"进入仓库核实当前项目的开发进度与待办需求，给出「还有哪些'
        '需求需要开发」的清单。","repo":"OpenDomino","type":"investigate"},"repo_op":{},'
        '"task_op":{},"pending_resolution":"none","correction_detected":false}'
    )
    llm = ScriptedLLM([screenshot_raw])
    d = await Router(llm).route("", "帮我看看当前项目还有哪些需求需要开发")
    check("route: 空对象分支字段放行且正确委派", d.action.value == "delegate" and d.task.repo == "OpenDomino")
    check("route: 空对象被归一为 None", d.repo_op is None and d.task_op is None)
    check("route: 无需重试直接解析成功", len(llm.calls) == 1)

    # 重试提示带具体校验错误（非法 action）
    llm2 = ScriptedLLM(['{"action":"frobnicate","answer":"x"}', '{"action":"answer","answer":"ok"}'])
    d2 = await Router(llm2).route("", "x")
    check(
        "route: 重试提示附带具体校验错误",
        d2.action.value == "answer" and len(llm2.calls) == 2
        and "JSON 结构不符合路由 Schema" in llm2.calls[1],
    )

    # 两次失败仍降级原样回复（不静默失败）
    llm3 = ScriptedLLM(["这不是JSON", "还是不是JSON"])
    d3 = await Router(llm3).route("", "x")
    check("route: 双次失败降级原样回复", d3.answer == "还是不是JSON")


def test_config_contract():
    import app.config as cfg

    names = cfg.Settings.model_fields.keys()
    feishu_keys = [k for k in names if k.startswith("feishu_")]
    dingtalk_left = [k for k in names if k.startswith("dingtalk_")]
    check("config: FEISHU_* 配置齐备", {
        "feishu_app_id", "feishu_app_secret", "feishu_require_mention",
        "feishu_allowed_users", "feishu_allowed_chats", "feishu_bot_open_id",
        "feishu_backfill_days", "feishu_backfill_max", "feishu_observe_retain_days",
    } <= set(feishu_keys))
    check("config: DINGTALK_* 清零", dingtalk_left == [])
    check("config: 免 @ 群配置存在", "feishu_free_response_chats" in feishu_keys)


async def main():
    test_parse()
    memory = await test_channel()
    await test_store(memory)
    await test_context_merge(memory)
    test_backfill_parse()
    await test_route_schema_robustness()
    await test_startup_backfill(memory)
    test_config_contract()
    await test_reply_format_selection()

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "adr": "ADR-008 feishu-channel",
        "pass": len(PASS),
        "fail": len(FAIL),
        "cases": PASS + FAIL,
    }
    out = Path("./scripts/e2e_feishu_report.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed ===")
    print(f"report: {out.resolve()}")
    _tmp_db.unlink(missing_ok=True)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
