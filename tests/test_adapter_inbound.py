"""Luồng thật của adapter: webhook → agent, và agent → Zalo.

``gateway.*`` chỉ có trong bản cài Hermes nên test dựng stub tối thiểu đúng
những gì adapter dùng (build_source, handle_message, MessageEvent, SendResult)
rồi chạy adapter THẬT trên đó — bắt được lỗi mà test từng-hàm bỏ sót: chống
trùng webhook, cổng bảo trì, đánh dấu cửa sổ, cắt tin dài.
"""

import asyncio
import os
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def _install_gateway_stub() -> None:
    if "gateway" in sys.modules:
        return

    def mod(name):
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    gateway = mod("gateway")
    platforms = mod("gateway.platforms")
    base = mod("gateway.platforms.base")
    config = mod("gateway.config")
    gateway.platforms = platforms
    platforms.base = base

    @dataclass
    class SendResult:
        success: bool
        error: Optional[str] = None
        message_id: Optional[str] = None
        raw_response: Optional[Dict[str, Any]] = None

    @dataclass
    class MessageEvent:
        text: str
        message_type: Any
        source: Any
        message_id: str
        timestamp: Any
        media_urls: List[str] = field(default_factory=list)
        media_types: List[str] = field(default_factory=list)
        channel_prompt: Optional[str] = None
        channel_context: Optional[str] = None
        auto_skill: Any = None
        reply_to_message_id: Optional[str] = None

    class MessageType:
        TEXT, PHOTO, VOICE, DOCUMENT = "text", "photo", "voice", "document"

    class BasePlatformAdapter:
        def __init__(self, config=None, platform=None):
            self.config = config
            self.platform = platform
            self.dispatched: List[Any] = []

        def build_source(self, **kwargs):
            return dict(kwargs)

        async def handle_message(self, event):
            self.dispatched.append(event)

    class Platform:
        def __init__(self, name):
            self.name = name

    base.BasePlatformAdapter = BasePlatformAdapter
    base.SendResult = SendResult
    base.MessageEvent = MessageEvent
    base.MessageType = MessageType
    base.resolve_channel_prompt = lambda *a, **k: None
    base.resolve_channel_skills = lambda *a, **k: []
    config.Platform = Platform


_install_gateway_stub()

import adapter as A  # noqa: E402

OWNER = "owner-uid"
USER = "user-1"


class FakeClient:
    """Thay OaClient: ghi lại lời gọi, không chạm mạng."""

    def __init__(self):
        self.texts: List[tuple] = []
        self.images: List[tuple] = []
        self.files: List[tuple] = []
        self.fail_with: Optional[Exception] = None

    async def send_text(self, user_id, text, quote_message_id=None):
        if self.fail_with:
            raise self.fail_with
        self.texts.append((user_id, text, quote_message_id))
        return f"m{len(self.texts)}"

    async def send_image(self, user_id, filename, content_type, data, caption=""):
        self.images.append((user_id, filename, caption))
        return "img-1"

    async def send_file(self, user_id, filename, content_type, data, caption=""):
        self.files.append((user_id, filename, caption))
        return "file-1"

    async def get_user_detail(self, user_id):
        return {"display_name": "Khách A"}

    async def get_oa_id(self):
        return "oa-1"


def _event(name="user_send_text", text="cho em hỏi giá", msg_id="m1", user=USER, **over):
    ev = {
        "app_id": "app-1",
        "event_name": name,
        "sender": {"id": user},
        "recipient": {"id": "oa-1"},
        "message": {"msg_id": msg_id, "text": text},
        "timestamp": "1757000000000",
    }
    ev.update(over)
    return ev


class AdapterTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._env = {}
        for k, v in {
            "ZALO_OA_SESSION_DIR": self.dir,
            "ZALO_OA_APP_ID": "app-1",
            "ZALO_OA_APP_SECRET": "app-secret",
            "ZALO_OA_SECRET_KEY": "oa-secret",
            "ZALO_OA_PUBLIC_BASE_URL": "https://oa.example.com",
            "ZALO_OA_OWNER_UID": OWNER,
        }.items():
            self._env[k] = os.environ.get(k)
            os.environ[k] = v
        cfg = types.SimpleNamespace(extra={})
        self.bot = A.ZaloOaAdapter(cfg)
        self.client = FakeClient()
        self.bot.client = self.client

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def feed(self, event):
        asyncio.run(self.bot._handle_event(event))


class InboundTest(AdapterTestBase):
    def test_text_reaches_agent(self):
        self.feed(_event())
        self.assertEqual(len(self.bot.dispatched), 1)
        ev = self.bot.dispatched[0]
        self.assertEqual(ev.text, "cho em hỏi giá")
        self.assertEqual(ev.message_id, "m1")
        # user_id có tiền tố kênh: bộ nhớ khách OA không lẫn với kênh cá nhân.
        self.assertEqual(ev.source["user_id"], f"zalo-oa:{USER}")
        self.assertEqual(ev.source["chat_type"], "dm")

    def test_inbound_opens_consultation_window(self):
        self.assertFalse(self.bot.window.evaluate(USER).allowed)
        self.feed(_event())
        self.assertTrue(self.bot.window.evaluate(USER).allowed)

    def test_duplicate_webhook_dispatched_once(self):
        # Zalo gửi lại khi không nhận 200 kịp — không chặn là bot trả lời 2 lần.
        self.feed(_event(msg_id="dup"))
        self.feed(_event(msg_id="dup"))
        self.assertEqual(len(self.bot.dispatched), 1)

    def test_oa_own_message_ignored(self):
        self.feed(_event(name="oa_send_text", sender={"id": "oa-1"}, recipient={"id": USER}))
        self.assertEqual(self.bot.dispatched, [])

    def test_seen_receipt_does_not_extend_window(self):
        # Biên nhận "đã xem" là tin của TA, không phải tương tác của khách.
        self.feed({"event_name": "user_seen_message", "sender": {"id": USER}, "timestamp": "1"})
        self.assertFalse(self.bot.window.evaluate(USER).allowed)
        self.assertEqual(self.bot.dispatched, [])

    def test_follow_opens_window_and_sends_welcome(self):
        self.bot.welcome_message = "Cảm ơn anh chị đã quan tâm shop ạ"
        self.feed({"event_name": "follow", "follower": {"id": USER}, "timestamp": "1"})
        self.assertTrue(self.bot.window.evaluate(USER).allowed)
        self.assertEqual(self.client.texts[0][1], "Cảm ơn anh chị đã quan tâm shop ạ")
        self.assertEqual(self.bot.dispatched, [])

    def test_follow_without_welcome_sends_nothing(self):
        self.feed({"event_name": "follow", "follower": {"id": USER}, "timestamp": "1"})
        self.assertEqual(self.client.texts, [])

    def test_image_downloaded_and_dispatched_as_photo(self):
        img = Path(self.dir) / "a.jpg"
        img.write_bytes(b"\xff\xd8\xff")
        ev = _event(
            name="user_send_image",
            msg_id="m2",
            message={
                "msg_id": "m2",
                "text": "ảnh này",
                "attachments": [{"type": "image", "payload": {"url": "https://cdn/a.jpg"}}],
            },
        )
        with mock.patch.object(A._media, "download_to", return_value=img):
            self.feed(ev)
        got = self.bot.dispatched[0]
        self.assertEqual(got.message_type, "photo")
        self.assertEqual(got.media_urls, [str(img)])
        self.assertEqual(got.text, "ảnh này")

    def test_failed_media_download_still_tells_the_agent(self):
        ev = _event(
            name="user_send_image",
            msg_id="m3",
            message={
                "msg_id": "m3",
                "attachments": [{"type": "image", "payload": {"url": "https://cdn/a.jpg"}}],
            },
        )
        with mock.patch.object(A._media, "download_to", return_value=None):
            self.feed(ev)
        self.assertIn("không tải được", self.bot.dispatched[0].text)

    def test_empty_message_not_dispatched(self):
        self.feed(_event(text="", msg_id="m4", message={"msg_id": "m4", "text": ""}))
        self.assertEqual(self.bot.dispatched, [])


class MaintenanceGateTest(AdapterTestBase):
    def test_customer_gets_notice_and_agent_is_skipped(self):
        A._set_maintenance(True, "Bên em bảo trì tới 15h30 ạ")
        self.feed(_event())
        self.assertEqual(self.bot.dispatched, [])
        self.assertEqual(self.client.texts[0][1], "Bên em bảo trì tới 15h30 ạ")

    def test_notice_rate_limited_per_user(self):
        A._set_maintenance(True, "Bảo trì nhé anh chị")
        self.feed(_event(msg_id="a"))
        self.feed(_event(msg_id="b"))
        self.assertEqual(len(self.client.texts), 1)

    def test_owner_bypasses_maintenance(self):
        A._set_maintenance(True, "Bảo trì nhé")
        self.feed(_event(user=OWNER, text="còn hàng không em", msg_id="c"))
        self.assertEqual(len(self.bot.dispatched), 1)


class OwnerCommandTest(AdapterTestBase):
    def test_status_answered_inline_not_by_agent(self):
        self.feed(_event(user=OWNER, text="/bot status", msg_id="s1"))
        self.assertEqual(self.bot.dispatched, [])
        self.assertIn("Kênh Zalo OA", self.client.texts[0][1])

    def test_maintenance_toggle_via_command(self):
        self.feed(_event(user=OWNER, text="/bot baotri Bên em bảo trì tới 15h30", msg_id="s2"))
        self.assertTrue(A._get_maintenance()["enabled"])
        self.feed(_event(user=OWNER, text="/bot baotri off", msg_id="s3"))
        self.assertFalse(A._get_maintenance()["enabled"])

    def test_window_command_reports_state(self):
        self.feed(_event(msg_id="w0"))  # khách nhắn → mở cửa sổ
        self.feed(_event(user=OWNER, text=f"/bot window {USER}", msg_id="w1"))
        self.assertIn("GỬI ĐƯỢC", self.client.texts[-1][1])

    def test_non_owner_slash_bot_goes_to_agent(self):
        # Khách gõ /bot không được điều khiển bot.
        self.feed(_event(text="/bot status", msg_id="x1"))
        self.assertEqual(len(self.bot.dispatched), 1)
        self.assertEqual(self.client.texts, [])


class OutboundTest(AdapterTestBase):
    def _open_window(self):
        self.bot.window.mark_inbound(USER)

    def test_blocked_when_no_interaction_recorded(self):
        r = asyncio.run(self.bot.send(USER, "chào anh"))
        self.assertFalse(r.success)
        self.assertIn("cửa sổ", r.error)
        self.assertEqual(self.client.texts, [])

    def test_sends_within_window(self):
        self._open_window()
        r = asyncio.run(self.bot.send(USER, "dạ còn hàng ạ"))
        self.assertTrue(r.success)
        self.assertEqual(self.client.texts[0][1], "dạ còn hàng ạ")

    def test_long_message_split_into_chunks(self):
        self._open_window()
        self.bot.max_message_length = 50
        text = " ".join(f"từ{i}" for i in range(80))
        r = asyncio.run(self.bot.send(USER, text))
        self.assertTrue(r.success)
        self.assertGreater(len(self.client.texts), 1)
        for _, chunk, _ in self.client.texts:
            self.assertLessEqual(len(chunk), 50)

    def test_only_first_chunk_quotes(self):
        self._open_window()
        self.bot.max_message_length = 50
        asyncio.run(self.bot.send(USER, " ".join(f"từ{i}" for i in range(80)), reply_to="m1"))
        quotes = [q for _, _, q in self.client.texts]
        self.assertEqual(quotes[0], "m1")
        self.assertTrue(all(q is None for q in quotes[1:]))

    def test_outbound_counted_in_window_ledger(self):
        self._open_window()
        asyncio.run(self.bot.send(USER, "tin 1"))
        asyncio.run(self.bot.send(USER, "tin 2"))
        self.assertEqual(self.bot.window.sent_count(USER), 2)

    def test_window_error_is_not_retried(self):
        self._open_window()
        self.client.fail_with = A._oa.OaWindowError(-234, "khung giờ đêm")
        r = asyncio.run(self.bot.send(USER, "xin chào"))
        self.assertFalse(r.success)
        self.assertIn("-234", r.error)

    def test_transient_error_retries_then_succeeds(self):
        self._open_window()
        calls = {"n": 0}

        async def flaky(user_id, text, quote_message_id=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise A._oa.OaTransientError(-32, "rate limit")
            return "m-ok"

        self.client.send_text = flaky
        real_sleep = asyncio.sleep
        with mock.patch.object(A.asyncio, "sleep", new=lambda *_a, **_k: real_sleep(0)):
            r = asyncio.run(self.bot.send(USER, "thử lại nhé"))
        self.assertTrue(r.success)
        self.assertEqual(calls["n"], 2)

    def test_internal_diagnostics_never_reach_customer(self):
        self._open_window()
        r = asyncio.run(self.bot.send(USER, "⚠️ Model provider timeout — retrying in 3s"))
        self.assertTrue(r.success)
        self.assertEqual(self.client.texts, [])

    def test_empty_content_rejected(self):
        self.assertFalse(asyncio.run(self.bot.send(USER, "   ")).success)


if __name__ == "__main__":
    unittest.main()
