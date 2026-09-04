"""Zalo Official Account Open API client — OAuth, gửi tin, upload media.

Endpoint và mã lỗi ở đây được đối chiếu với bản triển khai production của
zca-bridge (Apache-2.0, https://github.com/diendh/zca-bridge, thư mục
``src/zalo-oa``) vì tài liệu chính thức là SPA không đọc được bằng máy và
không liệt kê đủ mã lỗi.

Ba điểm dễ mất dữ liệu / mất tiền, đọc kỹ trước khi sửa:

1. **Refresh token dùng MỘT LẦN.** Mỗi lần refresh, Zalo trả về cặp
   access+refresh MỚI và huỷ cặp cũ. Mất cặp mới = mất quyền, phải bấm OAuth
   lại bằng tay. Nên ``_persist_tokens`` ghi xuống đĩa NGAY trong cùng lời gọi
   refresh, trước khi trả token cho caller, và giữ một bản ``.prev`` để cứu.

2. **Chỉ có một luồng được refresh.** Hai lời gọi refresh song song → lời gọi
   thứ hai dùng refresh token đã bị huỷ → mất quyền. Khoá bằng asyncio.Lock.

3. **Lỗi API phân ba loại.** Retry được (rate limit, attachment hết hạn), hết
   cửa sổ gửi (không retry — retry chỉ tổ đốt quota), và lỗi vĩnh viễn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

OAUTH_PERMISSION_URL = "https://oauth.zaloapp.com/v4/oa/permission"
OAUTH_TOKEN_URL = "https://oauth.zaloapp.com/v4/oa/access_token"

# Gửi tin PHẢI có sub-path loại tin; gọi trần /v3.0/oa/message trả 404
# "You are accessing an empty or invalid API". `cs` = consultation (tin Tư vấn).
MESSAGE_CS_URL = "https://openapi.zalo.me/v3.0/oa/message/cs"
USER_DETAIL_URL = "https://openapi.zalo.me/v3.0/oa/user/detail"
# Các API đọc hội thoại + upload nằm ở v2.0, không phải v3.0.
GET_OA_URL = "https://openapi.zalo.me/v2.0/oa/getoa"
UPLOAD_IMAGE_URL = "https://openapi.zalo.me/v2.0/oa/upload/image"
UPLOAD_FILE_URL = "https://openapi.zalo.me/v2.0/oa/upload/file"
LIST_RECENT_CHAT_URL = "https://openapi.zalo.me/v2.0/oa/listrecentchat"

# Thử lại được: -32 rate limit ("reached limit call api"), -100 attachment_id
# hết hạn (lần thử sau upload lại là xong).
RETRYABLE_CODES = frozenset({-32, -100})
# Không với tới người dùng lúc này — retry vô nghĩa, chỉ tốn quota.
#   -213 chưa quan tâm OA · -217 chặn lời mời · -227 tài khoản khoá/không hoạt
#   động >45 ngày · -230 không tương tác trong 7 ngày · -232 tương tác hết hạn
#   -234 khung giờ đêm 22h-6h · -244 người dùng chặn loại tin này
WINDOW_CODES = frozenset({-213, -217, -227, -230, -232, -234, -244})

WINDOW_CODE_MEANING = {
    -213: "người dùng chưa quan tâm OA",
    -217: "người dùng đã chặn lời mời của OA",
    -227: "tài khoản bị khoá hoặc không hoạt động trên 45 ngày",
    -230: "người dùng không tương tác với OA trong 7 ngày",
    -232: "tương tác đã hết hạn",
    -234: "đang trong khung giờ đêm 22h-6h, OA không gửi được",
    -244: "người dùng đã hạn chế nhận loại tin này",
}

_HTTP_TIMEOUT_S = 30.0


class OaError(Exception):
    """Lỗi từ Zalo OA Open API (error != 0)."""

    def __init__(self, code: int, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class OaTransientError(OaError):
    """Thử lại với backoff có thể thành công."""


class OaWindowError(OaError):
    """Ngoài cửa sổ gửi tin / không với tới người dùng. KHÔNG retry."""


class OaPermanentError(OaError):
    """Sai tham số, token hỏng, vi phạm chính sách. KHÔNG retry."""


class OaAuthError(Exception):
    """Không lấy/làm mới được token — cần OAuth lại bằng tay."""


def classify_error(code: int, message: str) -> OaError:
    if code in RETRYABLE_CODES:
        return OaTransientError(code, message)
    if code in WINDOW_CODES:
        return OaWindowError(code, WINDOW_CODE_MEANING.get(code, message))
    return OaPermanentError(code, message)


# ── HTTP thô (chạy trong thread, urllib để không thêm dependency) ──────────


def _http(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: float = _HTTP_TIMEOUT_S,
) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, data=body)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        # Zalo trả JSON lỗi kèm HTTP 4xx/5xx — đọc body để lấy mã lỗi thật.
        return e.code, e.read()


def _parse_api_json(raw: bytes) -> Dict[str, Any]:
    """Bóc envelope {error, message, data} của Zalo, ném OaError nếu error != 0."""
    try:
        payload = json.loads(raw.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        raise OaPermanentError(-1, f"phản hồi không phải JSON: {raw[:200]!r}")
    if not isinstance(payload, dict):
        raise OaPermanentError(-1, f"phản hồi không hợp lệ: {payload!r}")
    code = int(payload.get("error") or 0)
    if code != 0:
        raise classify_error(code, str(payload.get("message") or ""))
    return payload


def build_multipart(filename: str, content_type: str, data: bytes) -> Tuple[bytes, str]:
    """Multipart body một file, field name ``file`` (đúng tên Zalo yêu cầu)."""
    boundary = f"----zaloOA{uuid.uuid4().hex}"
    safe_name = filename.replace('"', "").replace("\r", "").replace("\n", "")
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return head + data + tail, f"multipart/form-data; boundary={boundary}"


# ── Lưu token ─────────────────────────────────────────────────────────────


class TokenStore:
    """Cặp token OA trên đĩa. Ghi nguyên tử + giữ bản trước đó để cứu."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Dict[str, Any]:
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    return d
        except Exception as e:
            logger.error(f"[zalo-oa] đọc token hỏng ({e}) — cần OAuth lại")
        return {}

    def save(self, access_token: str, refresh_token: str, expires_at: float,
             extra: Optional[Dict[str, Any]] = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            # Refresh token dùng một lần: giữ bản trước để còn dò khi sự cố.
            try:
                self.path.replace(self.path.with_suffix(".prev.json"))
            except OSError as e:
                logger.warning(f"[zalo-oa] không lưu được bản token trước: {e}")
        payload = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
            "updated_at": time.time(),
        }
        payload.update(extra or {})
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)  # chứa bí mật — không để lộ cho user khác
        except OSError:
            pass
        os.replace(tmp, self.path)


# ── Client ────────────────────────────────────────────────────────────────


class OaClient:
    """Bọc Open API: tự làm mới token, gửi tin, upload ảnh/file."""

    def __init__(self, app_id: str, app_secret: str, token_store: TokenStore):
        self.app_id = (app_id or "").strip()
        self.app_secret = (app_secret or "").strip()
        self.tokens = token_store
        self._refresh_lock = asyncio.Lock()

    # ── OAuth ──
    def permission_url(self, redirect_uri: str, state: str) -> str:
        q = urllib.parse.urlencode(
            {"app_id": self.app_id, "redirect_uri": redirect_uri, "state": state}
        )
        return f"{OAUTH_PERMISSION_URL}?{q}"

    async def exchange_code(self, code: str) -> Dict[str, Any]:
        return await self._token_request(
            {"app_id": self.app_id, "grant_type": "authorization_code", "code": code}
        )

    async def _token_request(self, fields: Dict[str, str]) -> Dict[str, Any]:
        body = urllib.parse.urlencode(fields).encode("utf-8")
        headers = {
            "content-type": "application/x-www-form-urlencoded",
            # OAuth ký bằng App Secret. Webhook lại ký bằng OA Secret — HAI
            # bí mật khác nhau, đừng hoán đổi.
            "secret_key": self.app_secret,
        }
        status, raw = await asyncio.to_thread(
            _http, "POST", OAUTH_TOKEN_URL, headers=headers, body=body
        )
        try:
            payload = json.loads(raw.decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError:
            raise OaAuthError(f"OAuth trả phản hồi không phải JSON (HTTP {status})")
        access = payload.get("access_token")
        refresh = payload.get("refresh_token")
        if not access or not refresh:
            raise OaAuthError(
                f"OAuth thất bại: {payload.get('error')} {payload.get('error_name') or payload.get('message') or ''}".strip()
            )
        expires_in = float(payload.get("expires_in") or 3600)
        expires_at = time.time() + expires_in
        # Ghi NGAY: cặp cũ đã bị Zalo huỷ ở thời điểm này.
        self.tokens.save(str(access), str(refresh), expires_at)
        logger.info(f"[zalo-oa] token mới, hết hạn sau {int(expires_in)}s")
        return {"access_token": access, "refresh_token": refresh, "expires_at": expires_at}

    async def access_token(self) -> str:
        """Token còn hạn (làm mới trước 60s để không gửi bằng token vừa hết)."""
        stored = self.tokens.load()
        access = stored.get("access_token")
        expires_at = float(stored.get("expires_at") or 0)
        if access and expires_at - time.time() > 60:
            return str(access)
        async with self._refresh_lock:
            # Luồng khác có thể vừa refresh xong khi ta đợi khoá.
            stored = self.tokens.load()
            access = stored.get("access_token")
            expires_at = float(stored.get("expires_at") or 0)
            if access and expires_at - time.time() > 60:
                return str(access)
            refresh = stored.get("refresh_token")
            if not refresh:
                raise OaAuthError(
                    "chưa có refresh token — vào /oauth/start của plugin để cấp quyền OA"
                )
            result = await self._token_request(
                {
                    "app_id": self.app_id,
                    "grant_type": "refresh_token",
                    "refresh_token": str(refresh),
                }
            )
            return str(result["access_token"])

    # ── Gọi API ──
    async def _get(self, url: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        token = await self.access_token()
        full = url
        if data is not None:
            full = f"{url}?data={urllib.parse.quote(json.dumps(data, ensure_ascii=False))}"
        status, raw = await asyncio.to_thread(
            _http, "GET", full, headers={"access_token": token}
        )
        return _parse_api_json(raw)

    async def _post_json(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        token = await self.access_token()
        status, raw = await asyncio.to_thread(
            _http,
            "POST",
            url,
            headers={"content-type": "application/json", "access_token": token},
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        return _parse_api_json(raw)

    async def get_oa_id(self) -> str:
        d = await self._get(GET_OA_URL)
        oa_id = ((d.get("data") or {}).get("oa_id")) or ""
        if not oa_id:
            raise OaPermanentError(-1, "getoa không trả oa_id")
        return str(oa_id)

    async def get_user_detail(self, user_id: str) -> Dict[str, Any]:
        """Tên hiển thị + avatar. Webhook KHÔNG kèm tên người gửi nên muốn
        hiển thị tên là phải gọi cái này."""
        d = await self._get(USER_DETAIL_URL, {"user_id": str(user_id)})
        return (d.get("data") or {}) if isinstance(d.get("data"), dict) else {}

    async def list_recent_chat(self, offset: int = 0, count: int = 10) -> list:
        d = await self._get(LIST_RECENT_CHAT_URL, {"offset": offset, "count": count})
        data = d.get("data")
        return data if isinstance(data, list) else []

    async def send_text(
        self, user_id: str, text: str, quote_message_id: Optional[str] = None
    ) -> str:
        message: Dict[str, Any] = {"text": text}
        if quote_message_id:
            message["quote_message_id"] = str(quote_message_id)
        payload = {"recipient": {"user_id": str(user_id)}, "message": message}
        try:
            d = await self._post_json(MESSAGE_CS_URL, payload)
        except (OaPermanentError, OaTransientError):
            # quote_message_id sai/hết hạn bị từ chối như lỗi tham số. Thà gửi
            # tin không trích dẫn còn hơn nuốt luôn câu trả lời.
            if not quote_message_id:
                raise
            logger.info("[zalo-oa] quote bị từ chối — gửi lại không trích dẫn")
            d = await self._post_json(
                MESSAGE_CS_URL, {"recipient": {"user_id": str(user_id)}, "message": {"text": text}}
            )
        return str(((d.get("data") or {}).get("message_id")) or "")

    async def _upload(
        self, url: str, filename: str, content_type: str, data: bytes, id_field: str
    ) -> str:
        token = await self.access_token()
        body, multipart_ct = build_multipart(filename, content_type, data)
        status, raw = await asyncio.to_thread(
            _http,
            "POST",
            url,
            headers={
                "access_token": token,
                "content-type": multipart_ct,
                "content-length": str(len(body)),
            },
            body=body,
        )
        d = _parse_api_json(raw)
        ident = (d.get("data") or {}).get(id_field)
        if not ident:
            raise OaPermanentError(-1, f"upload không trả {id_field}")
        return str(ident)

    async def send_image(
        self, user_id: str, filename: str, content_type: str, data: bytes, caption: str = ""
    ) -> str:
        """Ảnh đi đường /upload/image → attachment_id → template media.

        Đẩy file không-phải-ảnh vào đây sẽ nhận '-201 file is invalid. We only
        support png and jpeg' — dùng send_file cho mọi loại khác.
        """
        attachment_id = await self._upload(
            UPLOAD_IMAGE_URL, filename, content_type, data, "attachment_id"
        )
        message: Dict[str, Any] = {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "media",
                    "elements": [{"media_type": "image", "attachment_id": attachment_id}],
                },
            }
        }
        if caption:
            message["text"] = caption
        d = await self._post_json(
            MESSAGE_CS_URL, {"recipient": {"user_id": str(user_id)}, "message": message}
        )
        return str(((d.get("data") or {}).get("message_id")) or "")

    async def send_file(
        self, user_id: str, filename: str, content_type: str, data: bytes, caption: str = ""
    ) -> str:
        token_id = await self._upload(UPLOAD_FILE_URL, filename, content_type, data, "token")
        message: Dict[str, Any] = {"attachment": {"type": "file", "payload": {"token": token_id}}}
        if caption:
            message["text"] = caption
        d = await self._post_json(
            MESSAGE_CS_URL, {"recipient": {"user_id": str(user_id)}, "message": message}
        )
        return str(((d.get("data") or {}).get("message_id")) or "")
