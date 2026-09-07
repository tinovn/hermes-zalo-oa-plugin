"""Client OA: phân loại lỗi, kho token (refresh token dùng một lần), gửi tin.

Trọng tâm là hai thứ mất mát không cứu được nếu sai:
  * refresh token xoay vòng — ghi hụt một lần là mất quyền, phải OAuth tay;
  * hai luồng cùng refresh — cái sau dùng token đã bị huỷ, cũng mất quyền.
"""

import asyncio
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import oa_client  # noqa: E402
from oa_client import (  # noqa: E402
    OaAuthError,
    OaClient,
    OaPermanentError,
    OaTransientError,
    OaWindowError,
    TokenStore,
    build_multipart,
    classify_error,
)


class ErrorClassifyTest(unittest.TestCase):
    def test_retryable_codes(self):
        for code in (-32, -100):
            self.assertIsInstance(classify_error(code, "x"), OaTransientError)

    def test_window_codes(self):
        for code in (-213, -217, -227, -230, -232, -234, -244):
            self.assertIsInstance(classify_error(code, "x"), OaWindowError)

    def test_night_window_message_is_explained(self):
        # -234 nghĩa là khung giờ đêm; log ra mã trần thì sau này không ai hiểu.
        self.assertIn("22h", classify_error(-234, "").message)

    def test_unknown_code_is_permanent(self):
        self.assertIsInstance(classify_error(-9999, "x"), OaPermanentError)

    def test_parse_success(self):
        raw = json.dumps({"error": 0, "message": "Success", "data": {"message_id": "m1"}})
        self.assertEqual(oa_client._parse_api_json(raw.encode())["data"]["message_id"], "m1")

    def test_parse_raises_typed_error(self):
        raw = json.dumps({"error": -230, "message": "user out of interaction"}).encode()
        with self.assertRaises(OaWindowError):
            oa_client._parse_api_json(raw)

    def test_parse_non_json_is_permanent(self):
        with self.assertRaises(OaPermanentError):
            oa_client._parse_api_json(b"<html>502 bad gateway</html>")


class MultipartTest(unittest.TestCase):
    def test_shape(self):
        body, ct = build_multipart("a b.png", "image/png", b"\x89PNG")
        self.assertIn("multipart/form-data; boundary=", ct)
        boundary = ct.split("boundary=")[1]
        text = body.decode("latin-1")
        self.assertTrue(text.startswith(f"--{boundary}\r\n"))
        self.assertIn('name="file"; filename="a b.png"', text)
        self.assertIn("Content-Type: image/png", text)
        self.assertTrue(text.endswith(f"--{boundary}--\r\n"))
        self.assertIn("\x89PNG", text)

    def test_quotes_stripped_from_filename(self):
        body, _ = build_multipart('e"vil".png', "image/png", b"x")
        self.assertNotIn('"evil"', body.decode("latin-1").split("filename=")[1][:20])


class TokenStoreTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "tokens.json"

    def test_roundtrip(self):
        s = TokenStore(self.path)
        s.save("acc", "ref", 123.0)
        d = s.load()
        self.assertEqual((d["access_token"], d["refresh_token"], d["expires_at"]), ("acc", "ref", 123.0))

    def test_previous_pair_kept_for_recovery(self):
        s = TokenStore(self.path)
        s.save("acc1", "ref1", 1.0)
        s.save("acc2", "ref2", 2.0)
        prev = json.loads(self.path.with_suffix(".prev.json").read_text(encoding="utf-8"))
        self.assertEqual(prev["refresh_token"], "ref1")
        self.assertEqual(s.load()["refresh_token"], "ref2")

    def test_file_is_not_world_readable(self):
        s = TokenStore(self.path)
        s.save("acc", "ref", 1.0)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode) & 0o077, 0)

    def test_no_temp_file_left_behind(self):
        s = TokenStore(self.path)
        s.save("acc", "ref", 1.0)
        self.assertFalse(self.path.with_suffix(".tmp").exists())

    def test_corrupt_file_reads_as_empty(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{oops", encoding="utf-8")
        self.assertEqual(TokenStore(self.path).load(), {})


def _client(tmp: Path) -> OaClient:
    return OaClient("app-1", "app-secret", TokenStore(tmp / "tokens.json"))


class AccessTokenTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_valid_token_used_without_refresh(self):
        c = _client(self.dir)
        c.tokens.save("acc", "ref", oa_client.time.time() + 3600)
        with mock.patch.object(c, "_token_request", side_effect=AssertionError("không được refresh")):
            self.assertEqual(asyncio.run(c.access_token()), "acc")

    def test_expired_token_refreshes_and_persists_new_pair(self):
        c = _client(self.dir)
        c.tokens.save("old-acc", "old-ref", oa_client.time.time() - 10)

        async def fake(fields):
            self.assertEqual(fields["grant_type"], "refresh_token")
            self.assertEqual(fields["refresh_token"], "old-ref")
            c.tokens.save("new-acc", "new-ref", oa_client.time.time() + 3600)
            return {"access_token": "new-acc", "refresh_token": "new-ref", "expires_at": 0}

        with mock.patch.object(c, "_token_request", side_effect=fake):
            self.assertEqual(asyncio.run(c.access_token()), "new-acc")
        self.assertEqual(c.tokens.load()["refresh_token"], "new-ref")

    def test_missing_refresh_token_raises_auth_error(self):
        c = _client(self.dir)
        with self.assertRaises(OaAuthError):
            asyncio.run(c.access_token())

    def test_concurrent_calls_refresh_only_once(self):
        # Refresh token dùng một lần: hai lần refresh song song = mất quyền.
        c = _client(self.dir)
        c.tokens.save("old", "ref-1", oa_client.time.time() - 10)
        calls = []

        async def fake(fields):
            calls.append(fields["refresh_token"])
            await asyncio.sleep(0.05)
            c.tokens.save("new", "ref-2", oa_client.time.time() + 3600)
            return {"access_token": "new", "refresh_token": "ref-2", "expires_at": 0}

        async def main():
            with mock.patch.object(c, "_token_request", side_effect=fake):
                return await asyncio.gather(*[c.access_token() for _ in range(5)])

        self.assertEqual(asyncio.run(main()), ["new"] * 5)
        self.assertEqual(calls, ["ref-1"])

    def test_permission_url_carries_app_and_state(self):
        c = _client(self.dir)
        url = c.permission_url("https://x.vn/oauth/callback", "st-1")
        self.assertIn("app_id=app-1", url)
        self.assertIn("state=st-1", url)
        self.assertIn("redirect_uri=https%3A%2F%2Fx.vn%2Foauth%2Fcallback", url)


class SendTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.c = _client(self.dir)
        self.c.tokens.save("acc", "ref", oa_client.time.time() + 3600)

    def test_send_text_payload(self):
        seen = {}

        async def fake(url, payload):
            seen["url"], seen["payload"] = url, payload
            return {"data": {"message_id": "m9"}}

        with mock.patch.object(self.c, "_post_json", side_effect=fake):
            msg_id = asyncio.run(self.c.send_text("u1", "chào anh"))
        self.assertEqual(msg_id, "m9")
        self.assertEqual(seen["url"], oa_client.MESSAGE_CS_URL)
        self.assertEqual(seen["payload"]["recipient"], {"user_id": "u1"})
        self.assertEqual(seen["payload"]["message"], {"text": "chào anh"})

    def test_quote_included_when_given(self):
        async def fake(url, payload):
            self.assertEqual(payload["message"]["quote_message_id"], "m1")
            return {"data": {"message_id": "m2"}}

        with mock.patch.object(self.c, "_post_json", side_effect=fake):
            asyncio.run(self.c.send_text("u1", "ok", "m1"))

    def test_bad_quote_falls_back_to_plain_send(self):
        # Thà mất cái bong bóng trích dẫn còn hơn nuốt luôn câu trả lời.
        calls = []

        async def fake(url, payload):
            calls.append(payload)
            if "quote_message_id" in payload["message"]:
                raise OaPermanentError(-201, "invalid quote")
            return {"data": {"message_id": "m3"}}

        with mock.patch.object(self.c, "_post_json", side_effect=fake):
            self.assertEqual(asyncio.run(self.c.send_text("u1", "ok", "stale")), "m3")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("quote_message_id", calls[1]["message"])

    def test_window_error_is_not_retried_as_quote_problem(self):
        async def fake(url, payload):
            raise OaWindowError(-230, "hết cửa sổ")

        with mock.patch.object(self.c, "_post_json", side_effect=fake):
            with self.assertRaises(OaWindowError):
                asyncio.run(self.c.send_text("u1", "ok", "m1"))

    def test_send_image_uploads_then_sends_template(self):
        async def fake_upload(url, filename, ct, data, id_field):
            self.assertEqual(url, oa_client.UPLOAD_IMAGE_URL)
            self.assertEqual(id_field, "attachment_id")
            return "att-1"

        async def fake_post(url, payload):
            el = payload["message"]["attachment"]["payload"]["elements"][0]
            self.assertEqual(el, {"media_type": "image", "attachment_id": "att-1"})
            self.assertEqual(payload["message"]["text"], "ảnh sản phẩm")
            return {"data": {"message_id": "m4"}}

        with mock.patch.object(self.c, "_upload", side_effect=fake_upload), \
             mock.patch.object(self.c, "_post_json", side_effect=fake_post):
            self.assertEqual(
                asyncio.run(self.c.send_image("u1", "a.jpg", "image/jpeg", b"x", "ảnh sản phẩm")),
                "m4",
            )

    def test_send_file_uses_token_field(self):
        async def fake_upload(url, filename, ct, data, id_field):
            self.assertEqual(url, oa_client.UPLOAD_FILE_URL)
            self.assertEqual(id_field, "token")
            return "tok-1"

        async def fake_post(url, payload):
            self.assertEqual(payload["message"]["attachment"], {"type": "file", "payload": {"token": "tok-1"}})
            return {"data": {"message_id": "m5"}}

        with mock.patch.object(self.c, "_upload", side_effect=fake_upload), \
             mock.patch.object(self.c, "_post_json", side_effect=fake_post):
            asyncio.run(self.c.send_file("u1", "bao-gia.pdf", "application/pdf", b"%PDF", ""))


class OaTypeFallbackTest(unittest.TestCase):
    """OA cơ quan nhà nước bị -235 ở /v3.0/oa/message/cs nhưng gửi được qua
    /v2.0/oa/message. Đã gặp thật với OA loại "Tỉnh"; nếu không tự chuyển thì
    OA đó không trả lời được câu nào."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.c = _client(self.dir)
        self.c.tokens.save("acc", "ref", oa_client.time.time() + 3600)

    def _fake_post(self, calls, v3_error=None):
        async def fake(url, payload):
            calls.append(url)
            if url == oa_client.MESSAGE_CS_URL and v3_error is not None:
                raise v3_error
            return {"data": {"message_id": "m1"}}

        return fake

    def test_falls_back_to_v2_on_minus_235(self):
        calls = []
        err = oa_client.OaPermanentError(-235, "This API does not support this type of OA")
        with mock.patch.object(self.c, "_post_json", side_effect=self._fake_post(calls, err)):
            msg_id = asyncio.run(self.c.send_text("u1", "chào"))
        self.assertEqual(msg_id, "m1", "tin vẫn phải gửi được")
        self.assertEqual(calls, [oa_client.MESSAGE_CS_URL, oa_client.MESSAGE_V2_URL])

    def test_v2_is_pinned_after_first_fallback(self):
        calls = []
        err = oa_client.OaPermanentError(-235, "This API does not support this type of OA")
        with mock.patch.object(self.c, "_post_json", side_effect=self._fake_post(calls, err)):
            asyncio.run(self.c.send_text("u1", "tin 1"))
            asyncio.run(self.c.send_text("u1", "tin 2"))
            asyncio.run(self.c.send_text("u1", "tin 3"))
        # Chỉ tin đầu chịu một lần gọi hỏng; hai tin sau đi thẳng v2.0.
        self.assertEqual(
            calls,
            [oa_client.MESSAGE_CS_URL, oa_client.MESSAGE_V2_URL,
             oa_client.MESSAGE_V2_URL, oa_client.MESSAGE_V2_URL],
        )

    def test_other_permanent_errors_are_not_swallowed(self):
        calls = []
        err = oa_client.OaPermanentError(-201, "file is invalid")
        with mock.patch.object(self.c, "_post_json", side_effect=self._fake_post(calls, err)):
            with self.assertRaises(oa_client.OaPermanentError):
                asyncio.run(self.c.send_text("u1", "chào"))
        self.assertEqual(calls, [oa_client.MESSAGE_CS_URL], "không được thử v2.0 với lỗi khác")

    def test_window_error_still_raises(self):
        # -230 quá 7 ngày: đổi endpoint không cứu được, phải ném lên như cũ.
        calls = []
        err = oa_client.OaWindowError(-230, "hết cửa sổ")
        with mock.patch.object(self.c, "_post_json", side_effect=self._fake_post(calls, err)):
            with self.assertRaises(oa_client.OaWindowError):
                asyncio.run(self.c.send_text("u1", "chào"))
        self.assertEqual(calls, [oa_client.MESSAGE_CS_URL])

    def test_normal_oa_pins_v3_and_never_calls_v2(self):
        calls = []
        with mock.patch.object(self.c, "_post_json", side_effect=self._fake_post(calls)):
            asyncio.run(self.c.send_text("u1", "tin 1"))
            asyncio.run(self.c.send_text("u1", "tin 2"))
        self.assertEqual(calls, [oa_client.MESSAGE_CS_URL, oa_client.MESSAGE_CS_URL])

    def test_image_and_file_also_fall_back(self):
        err = oa_client.OaPermanentError(-235, "This API does not support this type of OA")

        async def fake_upload(*a, **k):
            return "att1"

        for kind in ("image", "file"):
            calls = []
            c = _client(Path(tempfile.mkdtemp()))
            c.tokens.save("acc", "ref", oa_client.time.time() + 3600)
            with mock.patch.object(c, "_upload", side_effect=fake_upload), \
                 mock.patch.object(c, "_post_json", side_effect=self._fake_post(calls, err)):
                if kind == "image":
                    asyncio.run(c.send_image("u1", "a.png", "image/png", b"x", ""))
                else:
                    asyncio.run(c.send_file("u1", "a.pdf", "application/pdf", b"x", ""))
            self.assertEqual(
                calls, [oa_client.MESSAGE_CS_URL, oa_client.MESSAGE_V2_URL],
                f"send_{kind} phải fallback như send_text",
            )


if __name__ == "__main__":
    unittest.main()
