"""Chuyển tiếp ZNS (``oa_zns`` + route ``POST /zns/send`` của webhook server).

Hai chỗ hỏng đắt nhất được khoá bằng test:

* để lọt request không ký / ký cũ / gửi lại → máy bắn tin có phí cho kẻ lạ;
* đụng vào token OA (refresh, ghi file) → mất quyền OA VNNIC, phải OAuth lại tay.
"""

import asyncio
import datetime
import json
import os
import secrets
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import oa_zns as Z  # noqa: E402
from oa_client import OaClient, TokenStore  # noqa: E402
from oa_webhook import WebhookServer  # noqa: E402

SECRET = "zns-relay-secret"
TEMPLATES = {"don_xac_nhan": "355330", "lich_xac_nhan": "355331"}
TOKEN = "tok-access-SECRET-123"
# 10:00 sáng giờ Việt Nam.
DAY = datetime.datetime(2026, 10, 12, 10, 0, tzinfo=Z.VN_TZ).timestamp()


class FakeZalo:
    """Tầng HTTP giả: ghi lại request, trả phản hồi định sẵn (hoặc ném lỗi mạng)."""

    def __init__(self, status=200, body=None, exc=None):
        self.status = status
        self.body = {"error": 0, "message": "Success", "data": {"msg_id": "zns-msg-1"}} if body is None else body
        self.exc = exc
        self.calls = []

    def __call__(self, method, url, *, headers, body, **kw):
        self.calls.append({"method": method, "url": url, "headers": headers, "json": json.loads(body)})
        if self.exc:
            raise self.exc
        raw = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        return self.status, raw


def _body(**over):
    b = {"template": "don_xac_nhan", "phone": "+84 901 234 567",
         "params": {"ten_cua_hang": "Tiệm hoa Mai", "ma_don": "DH-0042", "tong_tien": "350.000đ", "thanh_toan": "COD"},
         "ref_type": "order", "ref_id": "4242"}
    b.update(over)
    return json.dumps(b, ensure_ascii=False).encode("utf-8")


def _headers(raw, *, ts=None, nonce=None, secret=SECRET, sig=None):
    ts = str(int(DAY if ts is None else ts))
    nonce = nonce or secrets.token_hex(16)
    return {"X-Zns-Timestamp": ts, "X-Zns-Nonce": nonce,
            "X-Zns-Signature": sig if sig is not None else Z.sign(secret, ts, nonce, raw)}


def _relay(zalo=None, *, now=DAY, token=TOKEN, secret=SECRET, templates=TEMPLATES):
    zalo = zalo if zalo is not None else FakeZalo()
    reader = mock.Mock(return_value=token)
    relay = Z.ZnsRelay(secret=secret, templates=templates, token_reader=reader, http=zalo, now=lambda: now)
    return relay, zalo, reader


class AuthTest(unittest.TestCase):
    def test_signed_request_is_sent_with_current_token(self):
        relay, zalo, reader = _relay()
        raw = _body()
        status, out = relay.handle(_headers(raw), raw)
        self.assertEqual((status, out), (200, {"msg_id": "zns-msg-1"}))
        self.assertEqual(len(zalo.calls), 1)
        call = zalo.calls[0]
        self.assertEqual((call["method"], call["url"]), ("POST", Z.ZNS_URL))
        self.assertEqual(call["headers"]["access_token"], TOKEN)
        self.assertEqual(call["json"], {
            "phone": "84901234567", "template_id": "355330",
            "template_data": {"ten_cua_hang": "Tiệm hoa Mai", "ma_don": "DH-0042", "tong_tien": "350.000đ", "thanh_toan": "COD"},
            "tracking_id": "order:4242"})
        reader.assert_called_once()

    def test_bad_signature_rejected(self):
        relay, zalo, _ = _relay()
        raw = _body()
        self.assertEqual(relay.handle(_headers(raw, secret="khac"), raw)[0], 401)
        # Ký đúng nhưng sửa body sau khi ký.
        h = _headers(raw)
        self.assertEqual(relay.handle(h, _body(phone="0988888888"))[0], 401)
        self.assertEqual(relay.handle(_headers(raw, sig="sha256=00"), raw)[0], 401)
        self.assertEqual(zalo.calls, [])

    def test_missing_headers_rejected(self):
        relay, zalo, _ = _relay()
        raw = _body()
        for drop in ("X-Zns-Timestamp", "X-Zns-Nonce", "X-Zns-Signature"):
            h = _headers(raw)
            del h[drop]
            self.assertEqual(relay.handle(h, raw)[0], 401, drop)
        self.assertEqual(zalo.calls, [])

    def test_old_or_future_timestamp_rejected(self):
        relay, zalo, _ = _relay()
        raw = _body()
        self.assertEqual(relay.handle(_headers(raw, ts=DAY - 301), raw)[0], 401)
        self.assertEqual(relay.handle(_headers(raw, ts=DAY + 301), raw)[0], 401)
        self.assertEqual(relay.handle(_headers(raw, ts=DAY - 299), raw)[0], 200)
        self.assertEqual(len(zalo.calls), 1)

    def test_replayed_nonce_rejected(self):
        relay, zalo, _ = _relay()
        raw = _body()
        h = _headers(raw)
        self.assertEqual(relay.handle(h, raw)[0], 200)
        self.assertEqual(relay.handle(dict(h), raw)[0], 401)
        self.assertEqual(len(zalo.calls), 1)

    def test_bad_signature_does_not_burn_the_nonce(self):
        relay, _, _ = _relay()
        raw = _body()
        nonce = secrets.token_hex(16)
        self.assertEqual(relay.handle(_headers(raw, nonce=nonce, secret="ke-la"), raw)[0], 401)
        self.assertEqual(relay.handle(_headers(raw, nonce=nonce), raw)[0], 200)

    def test_nonce_forgotten_after_ten_minutes(self):
        cache = Z.NonceCache()
        self.assertTrue(cache.use("ab" * 8, 1000))
        self.assertFalse(cache.use("AB" * 8, 1000 + 599))
        self.assertTrue(cache.use("ab" * 8, 1000 + 600))

    def test_missing_secret_disables_route(self):
        relay, zalo, reader = _relay(secret="")
        raw = _body()
        self.assertEqual(relay.handle(_headers(raw), raw)[0], 503)
        self.assertEqual(zalo.calls, [])
        reader.assert_not_called()


class RequestTest(unittest.TestCase):
    def _send(self, raw, **kw):
        relay, zalo, reader = _relay(**kw)
        status, out = relay.handle(_headers(raw), raw)
        return status, out, zalo, reader

    def test_unknown_template_is_422_without_calling_zalo(self):
        status, out, zalo, reader = self._send(_body(template="khong_co"))
        self.assertEqual(status, 422)
        self.assertEqual(zalo.calls, [])
        reader.assert_not_called()

    def test_phone_forms(self):
        for given in ("0901234567", "+84901234567", "84 901.234.567"):
            self.assertEqual(Z.normalize_phone(given), "84901234567", given)
        for bad in ("12345", "+1 650 555 0100", "", None):
            self.assertEqual(Z.normalize_phone(bad), "", bad)
        status, _, zalo, _ = self._send(_body(phone="12345"))
        self.assertEqual((status, zalo.calls), (422, []))

    def test_bad_body_is_400(self):
        for raw in (b"khong-phai-json", b"[]", _body(params="x"), _body(ref_id=""), _body(ref_type="co dau cach"),
                    _body(params={"a": {"b": 1}}), _body(ref_id="x" * 45)):
            status, _, zalo, _ = self._send(raw)
            self.assertEqual((status, zalo.calls), (400, []), raw[:60])


class ZaloErrorTest(unittest.TestCase):
    def _status(self, zalo):
        relay, _, _ = _relay(zalo)
        raw = _body()
        return relay.handle(_headers(raw), raw)

    def test_permanent_zalo_errors_are_422(self):
        # Mẫu chưa duyệt / SĐT không nhận ZNS / tham số mẫu sai: thử lại vô ích.
        for code in (-108, -109, -118, -119, -131, -132):
            status, out = self._status(FakeZalo(body={"error": code, "message": "x"}))
            self.assertEqual((status, out["zalo_error"]), (422, code), code)

    def test_transient_errors_are_502(self):
        for code in (-124, -216, -32, -100):  # token hết hạn/sai, quá tần suất, lỗi lạ phía Zalo
            self.assertEqual(self._status(FakeZalo(body={"error": code, "message": "x"}))[0], 502, code)
        self.assertEqual(self._status(FakeZalo(status=503, body=b"<html>bad gateway</html>"))[0], 502)
        self.assertEqual(self._status(FakeZalo(status=200, body=b"not json"))[0], 502)
        self.assertEqual(self._status(FakeZalo(exc=urllib.error.URLError("timed out")))[0], 502)
        self.assertEqual(self._status(FakeZalo(exc=TimeoutError()))[0], 502)

    def test_no_token_is_502_without_calling_zalo(self):
        relay, zalo, _ = _relay(token="")
        raw = _body()
        self.assertEqual(relay.handle(_headers(raw), raw)[0], 502)
        self.assertEqual(zalo.calls, [])


class QuietHoursTest(unittest.TestCase):
    def test_refused_between_22h_and_6h_vietnam_time(self):
        for hour, minute, want in ((21, 59, 200), (22, 0, 409), (23, 30, 409), (2, 0, 409), (5, 59, 409), (6, 0, 200)):
            now = datetime.datetime(2026, 10, 12, hour, minute, tzinfo=Z.VN_TZ).timestamp()
            relay, zalo, _ = _relay(now=now)
            raw = _body()
            status, _ = relay.handle(_headers(raw, ts=now), raw)
            self.assertEqual(status, want, f"{hour:02d}:{minute:02d}")
            self.assertEqual(len(zalo.calls), 1 if want == 200 else 0)


class TokenReadOnlyTest(unittest.TestCase):
    """Relay chỉ ĐỌC token của gateway: không refresh, không ghi oa_tokens.json."""

    def test_current_token_reads_store_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "oa_tokens.json"
            store = TokenStore(path)
            store.save("tok-1", "refresh-1", time.time() + 3600)
            before = (path.read_bytes(), path.stat().st_mtime_ns, sorted(os.listdir(d)))
            with mock.patch.object(TokenStore, "save") as save, \
                    mock.patch.object(OaClient, "access_token") as refresh_path, \
                    mock.patch.object(OaClient, "_token_request") as token_request:
                self.assertEqual(Z.current_access_token(store), "tok-1")
                save.assert_not_called()
                refresh_path.assert_not_called()
                token_request.assert_not_called()
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns, sorted(os.listdir(d))), before)

    def test_expired_or_missing_token_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            store = TokenStore(Path(d) / "oa_tokens.json")
            self.assertEqual(Z.current_access_token(store), "")
            store.save("tok-old", "refresh-1", time.time() - 1)
            self.assertEqual(Z.current_access_token(store), "")

    def test_relay_module_never_refreshes_or_writes(self):
        src = Path(Z.__file__).read_text(encoding="utf-8")
        code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
        for forbidden in ("OaClient(", "TokenStore(", ".save(", "_token_request", "refresh_token\"", "grant_type"):
            self.assertNotIn(forbidden, code, forbidden)


class LogPrivacyTest(unittest.TestCase):
    def test_logs_show_last_three_digits_and_never_the_token(self):
        for zalo in (FakeZalo(), FakeZalo(body={"error": -119, "message": "x"}), FakeZalo(exc=OSError("down"))):
            relay, _, _ = _relay(zalo)
            raw = _body()
            with self.assertLogs("oa_zns", level="INFO") as logs:
                relay.handle(_headers(raw), raw)
            text = "\n".join(logs.output)
            self.assertIn("***567", text)
            for leak in ("901234567", "0901 234 567", TOKEN):
                self.assertNotIn(leak, text)


class ServerRouteTest(unittest.TestCase):
    """Route thật trên webhook server: header đi qua HTTP, đường webhook cũ không đổi."""

    def _post(self, port, path, raw, headers):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=raw, method="POST",
                                     headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read().decode() or "{}")

    def _run(self, zns, calls):
        async def main():
            loop = asyncio.get_running_loop()
            got = []
            server = WebhookServer(
                host="127.0.0.1", port=0, app_id="app", oa_secret_key="oa-secret",
                webhook_path="/webhooks/zalo-oa", loop=loop, on_event=got.append,
                on_oauth_start=lambda: "https://oauth.example/start",
                on_oauth_code=lambda c, s: (True, "ok"), zns=zns,
            )
            server.start()
            try:
                return [await asyncio.to_thread(self._post, server.bound_port, *c) for c in calls], got
            finally:
                server.stop()
        return asyncio.run(main())

    def test_route_sends_and_rejects_replay(self):
        relay, zalo, _ = _relay()
        raw = _body()
        h = _headers(raw)
        results, got = self._run(relay, [("/zns/send", raw, h), ("/zns/send", raw, h)])
        self.assertEqual(results[0], (200, {"msg_id": "zns-msg-1"}))
        self.assertEqual(results[1][0], 401)
        self.assertEqual((len(zalo.calls), got), (1, []))

    def test_route_without_relay_is_503(self):
        raw = _body()
        results, _ = self._run(None, [("/zns/send", raw, _headers(raw))])
        self.assertEqual(results[0][0], 503)


if __name__ == "__main__":
    unittest.main()
