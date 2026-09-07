"""Webhook OA: xác thực chữ ký, bóc sự kiện, và server HTTP thật.

Chữ ký sai = kẻ lạ bơm được tin giả vào agent, nên phần verify được test cả
ca hỏng: sửa body, sửa timestamp, sai khoá, thiếu header.
"""

import asyncio
import hashlib
import json
import os
import sys
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oa_webhook import (  # noqa: E402
    WebhookServer,
    classify_event,
    compute_mac,
    verify_mac,
)

APP_ID = "1234567890"
OA_SECRET = "oa-secret-key"


def _event(name, **over):
    ev = {
        "app_id": APP_ID,
        "event_name": name,
        "sender": {"id": "user-1"},
        "recipient": {"id": "oa-1"},
        "message": {"msg_id": "m1", "text": "chào shop"},
        "timestamp": "1757000000000",
    }
    ev.update(over)
    return ev


class MacTest(unittest.TestCase):
    def test_formula_matches_documented_order(self):
        # mac = SHA256(app_id + raw_body + timestamp + OA secret) — sai thứ tự
        # là hỏng, nên khoá công thức lại bằng test.
        expected = hashlib.sha256(f"{APP_ID}bodyhere1757{OA_SECRET}".encode()).hexdigest()
        self.assertEqual(compute_mac(APP_ID, "bodyhere", "1757", OA_SECRET), expected)

    def test_valid_signature(self):
        body = json.dumps(_event("user_send_text"))
        mac = compute_mac(APP_ID, body, "1757000000000", OA_SECRET)
        self.assertTrue(verify_mac(APP_ID, body, "1757000000000", mac, OA_SECRET))

    def test_tampered_body_rejected(self):
        body = json.dumps(_event("user_send_text"))
        mac = compute_mac(APP_ID, body, "1757000000000", OA_SECRET)
        self.assertFalse(verify_mac(APP_ID, body + " ", "1757000000000", mac, OA_SECRET))

    def test_wrong_secret_rejected(self):
        body = "{}"
        mac = compute_mac(APP_ID, body, "1", "khac")
        self.assertFalse(verify_mac(APP_ID, body, "1", mac, OA_SECRET))

    def test_wrong_timestamp_rejected(self):
        body = "{}"
        mac = compute_mac(APP_ID, body, "1", OA_SECRET)
        self.assertFalse(verify_mac(APP_ID, body, "2", mac, OA_SECRET))

    def test_empty_mac_rejected(self):
        self.assertFalse(verify_mac(APP_ID, "{}", "1", "", OA_SECRET))

    def test_uppercase_mac_accepted(self):
        body = "{}"
        mac = compute_mac(APP_ID, body, "1", OA_SECRET).upper()
        self.assertTrue(verify_mac(APP_ID, body, "1", mac, OA_SECRET))


class ClassifyTest(unittest.TestCase):
    def test_text(self):
        m = classify_event(_event("user_send_text"))
        self.assertEqual((m.user_id, m.msg_id, m.text), ("user-1", "m1", "chào shop"))
        self.assertFalse(m.is_self)

    def test_image_attachment(self):
        ev = _event(
            "user_send_image",
            message={
                "msg_id": "m2",
                "text": "ảnh nè",
                "attachments": [{"type": "image", "payload": {"url": "https://cdn.zalo/a.jpg"}}],
            },
        )
        m = classify_event(ev)
        self.assertEqual(m.media_kind, "image")
        self.assertEqual(m.media_url, "https://cdn.zalo/a.jpg")
        self.assertEqual(m.filename, "a.jpg")
        self.assertEqual(m.text, "ảnh nè")

    def test_file_uses_payload_name(self):
        ev = _event(
            "user_send_file",
            message={
                "msg_id": "m3",
                "attachments": [
                    {"type": "file", "payload": {"url": "https://cdn.zalo/x?y=1", "name": "báo giá.pdf"}}
                ],
            },
        )
        m = classify_event(ev)
        self.assertEqual((m.media_kind, m.filename), ("file", "báo giá.pdf"))

    def test_location_becomes_map_link(self):
        ev = _event(
            "user_send_location",
            message={
                "msg_id": "m4",
                "attachments": [
                    {"type": "location", "payload": {"coordinates": {"latitude": 10.77, "longitude": 106.7}}}
                ],
            },
        )
        self.assertIn("maps?q=10.77,106.7", classify_event(ev).text)

    def test_link_appends_url_to_text(self):
        ev = _event(
            "user_send_link",
            message={
                "msg_id": "m5",
                "text": "xem cái này",
                "attachments": [{"type": "link", "payload": {"url": "https://x.vn/a"}}],
            },
        )
        self.assertIn("https://x.vn/a", classify_event(ev).text)

    def test_quote_msg_id_surfaced(self):
        ev = _event("user_send_text", message={"msg_id": "m6", "text": "ok", "quote_msg_id": "m1"})
        self.assertEqual(classify_event(ev).quote_msg_id, "m1")

    def test_oa_send_is_self_and_thread_is_recipient(self):
        # Tin do OA gửi ra: người dùng là recipient, không phải sender.
        ev = _event("oa_send_text", sender={"id": "oa-1"}, recipient={"id": "user-9"})
        m = classify_event(ev)
        self.assertTrue(m.is_self)
        self.assertEqual(m.user_id, "user-9")

    def test_follow_is_interaction_only(self):
        ev = {"event_name": "follow", "follower": {"id": "user-7"}, "timestamp": "1"}
        m = classify_event(ev)
        self.assertTrue(m.is_interaction_only)
        self.assertEqual(m.user_id, "user-7")

    def test_unknown_event_ignored(self):
        self.assertIsNone(classify_event({"event_name": "something_else", "sender": {"id": "u"}}))

    def test_missing_user_ignored(self):
        self.assertIsNone(classify_event(_event("user_send_text", sender={}, recipient={})))

    def test_garbage_input_ignored(self):
        for junk in (None, "", 42, [], {}):
            self.assertIsNone(classify_event(junk))

    def test_unknown_user_send_kind_still_reaches_agent(self):
        # Zalo thêm loại tin mới → vẫn báo cho agent biết có gì đó vừa tới.
        m = classify_event(_event("user_send_newthing", message={"msg_id": "m7"}))
        self.assertIn("user_send_newthing", m.text)


class ServerTest(unittest.TestCase):
    """Chạy server thật trên cổng ngẫu nhiên, bắn HTTP vào như Zalo."""

    def _post(self, port, body: str, mac, path="/webhooks/zalo-oa"):
        # mac=None: BỎ HẲN header chữ ký — mô phỏng request kiểm tra của Zalo
        # lúc đăng ký webhook.
        headers = {"Content-Type": "application/json"}
        if mac is not None:
            headers["X-ZEvent-Signature"] = f"mac={mac}"
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=body.encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def _run(self, body, mac, path="/webhooks/zalo-oa"):
        async def main():
            got = []
            loop = asyncio.get_running_loop()
            server = WebhookServer(
                host="127.0.0.1", port=0, app_id=APP_ID, oa_secret_key=OA_SECRET,
                webhook_path="/webhooks/zalo-oa", loop=loop,
                on_event=got.append,
                on_oauth_start=lambda: "https://oauth.example/start",
                on_oauth_code=lambda c, s: (True, "ok"),
            )
            server.start()
            try:
                status, payload = await asyncio.to_thread(
                    self._post, server.bound_port, body, mac, path
                )
                await asyncio.sleep(0.05)  # cho call_soon_threadsafe chạy
                return status, payload, got
            finally:
                server.stop()

        return asyncio.run(main())

    def test_valid_event_reaches_handler(self):
        body = json.dumps(_event("user_send_text"))
        mac = compute_mac(APP_ID, body, "1757000000000", OA_SECRET)
        status, payload, got = self._run(body, mac)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["message"]["text"], "chào shop")

    def test_bad_signature_rejected_and_not_dispatched(self):
        body = json.dumps(_event("user_send_text"))
        status, _, got = self._run(body, "deadbeef")
        self.assertEqual(status, 401)
        self.assertEqual(got, [])

    # Zalo chỉ lưu webhook URL khi POST kiểm tra nhận 200, và request đó không
    # mang chữ ký. Ba test dưới khoá đúng ranh giới: THIẾU header thì nới,
    # header SAI thì vẫn chặn.
    def test_probe_without_signature_gets_200_and_is_not_dispatched(self):
        body = json.dumps(_event("user_send_text"))
        status, payload, got = self._run(body, None)
        self.assertEqual(status, 200)
        self.assertTrue(payload["probe"])
        self.assertEqual(got, [], "request không chữ ký KHÔNG được đẩy vào agent")

    def test_probe_with_empty_body_gets_200(self):
        status, payload, got = self._run("", None)
        self.assertEqual(status, 200)
        self.assertTrue(payload["probe"])
        self.assertEqual(got, [])

    def test_present_but_empty_mac_is_rejected_not_treated_as_probe(self):
        # "mac=" là header CÓ MẶT nhưng rỗng — đó là chữ ký hỏng, không phải
        # request kiểm tra. Phải 401 để Zalo gửi lại, không được nới thành 200.
        body = json.dumps(_event("user_send_text"))
        status, _, got = self._run(body, "")
        self.assertEqual(status, 401)
        self.assertEqual(got, [])

    def test_unknown_path_404(self):
        body = json.dumps(_event("user_send_text"))
        mac = compute_mac(APP_ID, body, "1757000000000", OA_SECRET)
        status, _, got = self._run(body, mac, path="/nope")
        self.assertEqual(status, 404)
        self.assertEqual(got, [])

    def test_health_endpoint(self):
        async def main():
            loop = asyncio.get_running_loop()
            server = WebhookServer(
                host="127.0.0.1", port=0, app_id=APP_ID, oa_secret_key=OA_SECRET,
                webhook_path="/webhooks/zalo-oa", loop=loop, on_event=lambda e: None,
                on_oauth_start=lambda: "https://oauth.example/start",
                on_oauth_code=lambda c, s: (True, "ok"),
            )
            server.start()
            server.set_oa_id("oa-42")
            try:
                def get():
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{server.bound_port}/health", timeout=5
                    ) as r:
                        return json.loads(r.read().decode())
                return await asyncio.to_thread(get)
            finally:
                server.stop()

        self.assertEqual(asyncio.run(main()), {"status": "ok", "oa_id": "oa-42"})


if __name__ == "__main__":
    unittest.main()
