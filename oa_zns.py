"""Chuyển tiếp ZNS (Zalo Notification Service) cho khách của chủ website.

MCP không giữ token OA VNNIC — token chỉ sống trong tiến trình gateway này
(``oa_tokens.json`` do ``oa_client.TokenStore`` quản). Vì vậy MCP chuyển mỗi tin
ZNS sang route ``POST /zns/send`` của webhook server, và route này gửi tới Zalo
bằng token ĐANG CÓ.

Luật cứng, đọc trước khi sửa:

* **Chỉ ĐỌC token.** Không refresh, không tạo OaClient/TokenStore thứ hai, không
  ghi ``oa_tokens.json``: refresh token Zalo dùng một lần, hai nơi cùng refresh
  là mất quyền OA (phải bấm OAuth lại bằng tay). Token hết hạn → 502, bên gọi
  thử lại sau khi gateway tự làm mới trong lượt gửi tin OA kế tiếp.
* **Xác thực mọi request**: ``X-Zns-Timestamp`` (giây unix, lệch ≤ 300s),
  ``X-Zns-Nonce`` (hex, nhớ 10 phút — gửi lại là 401) và
  ``X-Zns-Signature = "sha256=" + HMAC-SHA256(ZNS_RELAY_SECRET, ts + "." + nonce
  + "." + raw_body)``. Ký trên body THÔ, như webhook Zalo.
* **Giờ yên lặng 22h–6h (giờ Việt Nam)**: từ chối 409 — lưới an toàn cuối,
  builder đã dời tin trước đó.
* **Không log SĐT đầy đủ** (chỉ 3 số cuối) và không bao giờ log token.

Mã trả về cho bên gọi: 200 gửi xong (``msg_id``), 401 sai xác thực, 400 body
hỏng, 409 giờ yên lặng, 422 lỗi không thử lại được (mẫu lạ/chưa duyệt, SĐT không
nhận ZNS, tham số sai), 502 thử lại được (mạng, 5xx, token hết hạn, Zalo quá
tải), 503 relay chưa bật (thiếu ``ZNS_RELAY_SECRET``).
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

ZNS_URL = "https://business.openapi.zalo.me/message/template"

MAX_SKEW_S = 300
NONCE_TTL_S = 600
MAX_BODY_BYTES = 64 * 1024
_HTTP_TIMEOUT_S = 20.0

# Việt Nam không đổi giờ theo mùa: UTC+7 cố định, khỏi phụ thuộc tzdata trên máy.
VN_TZ = datetime.timezone(datetime.timedelta(hours=7))
QUIET_START_HOUR, QUIET_END_HOUR = 22, 6

# Lỗi Zalo thử lại được: token hết hạn/không hợp lệ (gateway sẽ tự làm mới), quá
# tần suất gọi API, lỗi không xác định phía Zalo. Mọi mã khác (mẫu chưa duyệt, SĐT
# không nhận ZNS, tham số sai…) là vĩnh viễn → 422.
TRANSIENT_ZALO_CODES = frozenset({-124, -216, -32, -100})

_NONCE_RE = re.compile(r"^[0-9a-fA-F]{16,128}$")
_REF_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
# tracking_id của ZNS tối đa 48 ký tự.
_MAX_TRACKING_ID = 48

Response = Tuple[int, Dict[str, Any]]
HttpFn = Callable[..., Tuple[int, bytes]]


def sign(secret: str, timestamp: str, nonce: str, raw_body: bytes) -> str:
    """Chữ ký bên gọi phải gửi trong ``X-Zns-Signature``."""
    msg = f"{timestamp}.{nonce}.".encode("utf-8") + raw_body
    return "sha256=" + hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def normalize_phone(phone: str) -> str:
    """``+84 90…`` / ``090…`` / ``8490…`` → ``8490…``; "" nếu không phải SĐT Việt Nam."""
    digits = re.sub(r"[\s.\-()]", "", str(phone or ""))
    if digits.startswith("+"):
        digits = digits[1:]
    if digits.startswith("0"):
        digits = "84" + digits[1:]
    return digits if re.fullmatch(r"84\d{9,10}", digits) else ""


def mask_phone(phone: str) -> str:
    """Chỉ để 3 số cuối cho log."""
    d = re.sub(r"\D", "", str(phone or ""))
    return "***" + d[-3:] if d else "***"


def in_quiet_hours(now: float) -> bool:
    hour = datetime.datetime.fromtimestamp(now, VN_TZ).hour
    return hour >= QUIET_START_HOUR or hour < QUIET_END_HOUR


def current_access_token(store: Any, now: Optional[float] = None) -> str:
    """Token OA ĐANG CÓ của gateway, chỉ đọc qua ``store.load()``; "" nếu chưa có
    hoặc đã hết hạn. KHÔNG refresh, KHÔNG ghi — xem đầu file."""
    stored = store.load() or {}
    access = str(stored.get("access_token") or "")
    try:
        expires_at = float(stored.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires_at = 0.0
    if not access or expires_at <= (time.time() if now is None else now):
        return ""
    return access


def parse_templates(raw: str) -> Dict[str, str]:
    """``ZALO_ZNS_TEMPLATES`` = JSON ``{"don_xac_nhan": "<template_id>", …}``."""
    if not (raw or "").strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"[zalo-oa zns] ZALO_ZNS_TEMPLATES không phải JSON: {e}")
        return {}
    if not isinstance(data, dict):
        logger.error("[zalo-oa zns] ZALO_ZNS_TEMPLATES phải là object JSON")
        return {}
    return {str(k): str(v) for k, v in data.items() if str(k).strip() and str(v).strip()}


def _default_http(method: str, url: str, *, headers: Dict[str, str], body: bytes,
                  timeout: float = _HTTP_TIMEOUT_S) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, data=body)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class NonceCache:
    """Nonce đã dùng trong ``ttl`` giây — gửi lại đúng request là bị từ chối."""

    def __init__(self, ttl: float = NONCE_TTL_S):
        self.ttl = ttl
        self._seen: Dict[str, float] = {}
        self._lock = threading.Lock()

    def use(self, nonce: str, now: float) -> bool:
        """True nếu nonce chưa dùng (và ghi nhận nó); False nếu đã dùng."""
        key = nonce.lower()
        with self._lock:
            for k in [k for k, exp in self._seen.items() if exp <= now]:
                del self._seen[k]
            if key in self._seen:
                return False
            self._seen[key] = now + self.ttl
            return True


class ZnsRelay:
    """Xử lý ``POST /zns/send`` (đồng bộ, chạy trong thread của webhook server)."""

    def __init__(
        self,
        *,
        secret: str,
        templates: Mapping[str, str],
        token_reader: Callable[[], str],
        http: HttpFn = _default_http,
        now: Callable[[], float] = time.time,
        nonces: Optional[NonceCache] = None,
    ):
        self.secret = (secret or "").strip()
        self.templates = dict(templates)
        self._token_reader = token_reader
        self._http = http
        self._now = now
        self._nonces = nonces or NonceCache()

    @classmethod
    def from_env(cls, token_reader: Callable[[], str], **kw: Any) -> "ZnsRelay":
        return cls(
            secret=os.getenv("ZNS_RELAY_SECRET", ""),
            templates=parse_templates(os.getenv("ZALO_ZNS_TEMPLATES", "")),
            token_reader=token_reader,
            **kw,
        )

    def _authenticate(self, headers: Mapping[str, str], raw_body: bytes) -> Optional[str]:
        """Lý do từ chối, hoặc None nếu hợp lệ. Nonce chỉ ghi nhận SAU khi chữ ký
        đúng — kẻ lạ không đốt được nonce của bên gọi thật."""
        ts = str(headers.get("X-Zns-Timestamp") or "").strip()
        nonce = str(headers.get("X-Zns-Nonce") or "").strip()
        sig = str(headers.get("X-Zns-Signature") or "").strip()
        if not ts or not nonce or not sig:
            return "thiếu header xác thực"
        try:
            ts_value = int(ts)
        except ValueError:
            return "timestamp không hợp lệ"
        now = self._now()
        if abs(now - ts_value) > MAX_SKEW_S:
            return "timestamp lệch quá 5 phút"
        if not _NONCE_RE.match(nonce):
            return "nonce không hợp lệ"
        if not hmac.compare_digest(sign(self.secret, ts, nonce, raw_body), sig.lower()):
            return "sai chữ ký"
        if not self._nonces.use(nonce, now):
            return "nonce đã dùng"
        return None

    def handle(self, headers: Mapping[str, str], raw_body: bytes) -> Response:
        if not self.secret:
            return 503, {"error": "zns relay chưa bật (thiếu ZNS_RELAY_SECRET)"}
        reason = self._authenticate(headers, raw_body)
        if reason:
            logger.warning(f"[zalo-oa zns] từ chối request: {reason}")
            return 401, {"error": "unauthorized"}

        try:
            body = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, {"error": "body không phải JSON"}
        if not isinstance(body, dict):
            return 400, {"error": "body phải là object JSON"}
        template = str(body.get("template") or "").strip()
        params = body.get("params")
        ref_type = str(body.get("ref_type") or "").strip()
        ref_id = str(body.get("ref_id") or "").strip()
        if not template or not isinstance(params, dict) or not _REF_RE.match(ref_type) or not _REF_RE.match(ref_id):
            return 400, {"error": "cần template, params (object), ref_type, ref_id (A-Z a-z 0-9 _ . -)"}
        if any(not isinstance(v, (str, int, float)) or isinstance(v, bool) for v in params.values()):
            return 400, {"error": "params chỉ nhận chuỗi hoặc số"}
        tracking_id = f"{ref_type}:{ref_id}"
        if len(tracking_id) > _MAX_TRACKING_ID:
            return 400, {"error": f"ref_type:ref_id dài quá {_MAX_TRACKING_ID} ký tự"}

        template_id = self.templates.get(template)
        if not template_id:
            return 422, {"error": f"mẫu {template!r} chưa khai trong ZALO_ZNS_TEMPLATES"}
        phone = normalize_phone(body.get("phone"))
        if not phone:
            return 422, {"error": "phone không phải số điện thoại Việt Nam"}
        if in_quiet_hours(self._now()):
            return 409, {"error": "giờ yên lặng 22h–6h, không gửi ZNS"}

        token = self._token_reader()
        if not token:
            logger.warning("[zalo-oa zns] chưa có token OA còn hạn — trả 502 để bên gọi thử lại")
            return 502, {"error": "token OA chưa sẵn sàng hoặc đã hết hạn"}

        payload = {"phone": phone, "template_id": template_id, "template_data": params,
                   "tracking_id": tracking_id}
        who = f"mẫu={template} sđt={mask_phone(phone)} ref={tracking_id}"
        try:
            status, raw = self._http(
                "POST", ZNS_URL,
                headers={"Content-Type": "application/json", "access_token": token},
                body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )
        except Exception as e:  # mạng, timeout, DNS…
            logger.warning(f"[zalo-oa zns] gửi lỗi mạng {who}: {type(e).__name__}")
            return 502, {"error": "không gọi được Zalo ZNS"}
        if status >= 500:
            logger.warning(f"[zalo-oa zns] Zalo HTTP {status} {who}")
            return 502, {"error": f"Zalo trả HTTP {status}"}
        try:
            resp = json.loads(raw.decode("utf-8", "replace") or "{}")
            code = int(resp.get("error") or 0)
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
            logger.warning(f"[zalo-oa zns] phản hồi không đọc được (HTTP {status}) {who}")
            return 502, {"error": "phản hồi Zalo không đọc được"}
        zalo_message = str(resp.get("message") or "")
        if code == 0 and 200 <= status < 300:
            data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
            msg_id = str(data.get("msg_id") or "")
            logger.info(f"[zalo-oa zns] đã gửi {who} msg_id={msg_id}")
            return 200, {"msg_id": msg_id}
        detail = {"zalo_error": code, "zalo_message": zalo_message}
        if code in TRANSIENT_ZALO_CODES or code == 0:
            logger.warning(f"[zalo-oa zns] lỗi tạm thời [{code}] {zalo_message} {who}")
            return 502, {"error": "Zalo tạm thời không nhận", **detail}
        logger.warning(f"[zalo-oa zns] Zalo từ chối [{code}] {zalo_message} {who}")
        return 422, {"error": "Zalo từ chối tin ZNS", **detail}
