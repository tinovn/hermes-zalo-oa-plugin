"""Webhook Zalo OA: xác thực chữ ký, phân loại sự kiện, HTTP server.

Zalo đẩy sự kiện tới một URL HTTPS công khai — plugin không kết nối ra ngoài
mà phải TỰ mở cổng nghe.

Chữ ký: header ``X-ZEvent-Signature: mac=<hex>`` với

    mac = SHA256( app_id + raw_body + timestamp + OA_SECRET_KEY )

Ba cái bẫy ở đây:

  * Ký trên **raw body** — parse JSON rồi dump lại là sai chữ ký ngay (thứ tự
    khoá, khoảng trắng, unicode escape đều đổi).
  * Khoá ký là **OA Secret Key**, KHÁC App Secret dùng cho OAuth.
  * Zalo gửi lại sự kiện khi không nhận được 200 → phải chống trùng theo
    ``msg_id``, nếu không bot trả lời hai lần cho một tin.

Server dùng ``http.server`` trong một thread riêng (không thêm dependency);
TLS do reverse proxy (nginx/Caddy) đứng trước lo. Sự kiện được đẩy vào vòng
lặp asyncio của adapter bằng ``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Sự kiện do CHÍNH OA gửi (bot ta, hoặc nhân viên trả lời trong OA Manager).
# Không đẩy cho agent, nếu không bot tự trả lời chính mình.
SELF_EVENTS = frozenset(
    {"oa_send_text", "oa_send_image", "oa_send_file", "oa_send_gif", "oa_send_sticker"}
)
# Sự kiện chỉ ghi nhận tương tác, không có nội dung cho agent.
INTERACTION_EVENTS = frozenset(
    {"follow", "unfollow", "user_seen_message", "user_received_message", "user_submit_info"}
)

_MAX_BODY_BYTES = 2 * 1024 * 1024


def compute_mac(app_id: str, raw_body: str, timestamp: str, oa_secret_key: str) -> str:
    return hashlib.sha256(
        f"{app_id}{raw_body}{timestamp}{oa_secret_key}".encode("utf-8")
    ).hexdigest()


def verify_mac(app_id: str, raw_body: str, timestamp: str, mac: str, oa_secret_key: str) -> bool:
    if not mac:
        return False
    expected = compute_mac(app_id, raw_body, timestamp, oa_secret_key)
    return hmac.compare_digest(expected, mac.strip().lower())


@dataclass
class InboundMessage:
    """Sự kiện đã bóc tách, sẵn sàng cho adapter."""

    event_name: str
    user_id: str
    msg_id: str
    text: str = ""
    media_url: str = ""
    media_kind: str = ""          # image | audio | video | file | ""
    filename: str = ""
    quote_msg_id: str = ""
    is_self: bool = False
    is_interaction_only: bool = False
    timestamp_ms: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)


def _first_attachment(message: Dict[str, Any]) -> Dict[str, Any]:
    atts = message.get("attachments")
    if isinstance(atts, list) and atts and isinstance(atts[0], dict):
        return atts[0]
    return {}


def classify_event(event: Dict[str, Any]) -> Optional[InboundMessage]:
    """Bóc payload webhook thành InboundMessage. None nếu không quan tâm."""
    if not isinstance(event, dict):
        return None
    name = str(event.get("event_name") or "")
    if not name:
        return None
    is_self = name in SELF_EVENTS
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    recipient = event.get("recipient") if isinstance(event.get("recipient"), dict) else {}
    follower = event.get("follower") if isinstance(event.get("follower"), dict) else {}
    # Luồng hội thoại luôn tính theo NGƯỜI DÙNG: họ là sender ở tin đến, là
    # recipient ở sự kiện oa_send_* (do OA gửi ra).
    user_id = str(
        (recipient.get("id") if is_self else sender.get("id"))
        or follower.get("id")
        or sender.get("id")
        or ""
    )
    if not user_id:
        return None

    try:
        ts = int(event.get("timestamp") or 0)
    except (TypeError, ValueError):
        ts = 0

    if name in INTERACTION_EVENTS:
        return InboundMessage(
            event_name=name, user_id=user_id, msg_id="", is_interaction_only=True,
            timestamp_ms=ts, raw=event,
        )
    if not name.startswith("user_send_") and not is_self:
        return None

    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    text = message.get("text") if isinstance(message.get("text"), str) else ""
    att = _first_attachment(message)
    payload = att.get("payload") if isinstance(att.get("payload"), dict) else {}
    url = payload.get("url") if isinstance(payload.get("url"), str) else ""

    msg = InboundMessage(
        event_name=name,
        user_id=user_id,
        msg_id=str(message.get("msg_id") or ""),
        text=text or "",
        quote_msg_id=str(message.get("quote_msg_id") or ""),
        is_self=is_self,
        timestamp_ms=ts,
        raw=event,
    )

    if name in ("user_send_text", "oa_send_text"):
        return msg
    if name == "user_send_link":
        # Link preview: text có thể rỗng, URL nằm trong attachment.
        if url and url not in msg.text:
            msg.text = f"{msg.text}\n{url}".strip()
        return msg
    if name in ("user_send_image", "user_send_gif", "user_send_sticker", "oa_send_image"):
        msg.media_kind, msg.media_url = "image", url
        msg.filename = _name_from(payload, url, "image.jpg")
        return msg
    if name == "user_send_audio":
        msg.media_kind, msg.media_url = "audio", url
        msg.filename = _name_from(payload, url, "audio.m4a")
        return msg
    if name == "user_send_video":
        msg.media_kind, msg.media_url = "video", url
        msg.filename = _name_from(payload, url, "video.mp4")
        return msg
    if name in ("user_send_file", "oa_send_file"):
        msg.media_kind, msg.media_url = "file", url
        msg.filename = _name_from(payload, url, "file.bin")
        return msg
    if name == "user_send_location":
        coords = payload.get("coordinates") if isinstance(payload.get("coordinates"), dict) else {}
        lat, lon = coords.get("latitude"), coords.get("longitude")
        msg.text = (
            f"[Vị trí] https://www.google.com/maps?q={lat},{lon}"
            if lat is not None and lon is not None
            else "[Vị trí — không đọc được toạ độ]"
        )
        return msg
    # Loại tin mới của Zalo: giữ lại để agent còn biết có gì đó vừa tới.
    msg.text = msg.text or f"[Tin loại {name} — mở app Zalo OA để xem]"
    return msg


def _name_from(payload: Dict[str, Any], url: str, fallback: str) -> str:
    name = payload.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    tail = url.split("?")[0].split("/")[-1] if url else ""
    return tail or fallback


class _Handler(BaseHTTPRequestHandler):
    server_version = "hermes-zalo-oa/1.0"

    # Log của BaseHTTPRequestHandler đi thẳng ra stderr — chuyển về logger.
    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("[zalo-oa http] " + fmt, *args)

    def _send(self, status: int, body: Dict[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, status: int, html: str) -> None:
        raw = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 (tên do BaseHTTPRequestHandler quy định)
        cfg = self.server.oa_config  # type: ignore[attr-defined]
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path.rstrip("/") or "/", urllib.parse.parse_qs(parsed.query)

        if path == "/health":
            self._send(200, {"status": "ok", "oa_id": cfg.get("oa_id") or None})
            return
        if path == "/oauth/start":
            url = cfg["on_oauth_start"]()
            self.send_response(302)
            self.send_header("Location", url)
            self.end_headers()
            return
        if path == "/oauth/callback":
            code = (query.get("code") or [""])[0]
            state = (query.get("state") or [""])[0]
            if not code:
                self._send_html(400, "<h3>Thiếu tham số code</h3>")
                return
            ok, detail = cfg["on_oauth_code"](code, state)
            self._send_html(
                200 if ok else 400,
                f"<h3>{'Đã kết nối OA thành công' if ok else 'Kết nối OA thất bại'}</h3><p>{detail}</p>",
            )
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        cfg = self.server.oa_config  # type: ignore[attr-defined]
        parsed = urllib.parse.urlparse(self.path)
        if (parsed.path.rstrip("/") or "/") != cfg["webhook_path"].rstrip("/"):
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > _MAX_BODY_BYTES:
            self._send(400, {"error": "bad length"})
            return
        raw_bytes = self.rfile.read(length)
        raw_body = raw_bytes.decode("utf-8", "replace")
        try:
            event = json.loads(raw_body)
        except json.JSONDecodeError:
            self._send(400, {"error": "bad json"})
            return

        sig = str(self.headers.get("X-ZEvent-Signature") or "")
        mac = sig.split("mac=", 1)[-1] if "mac=" in sig else sig
        timestamp = str(event.get("timestamp") or "") if isinstance(event, dict) else ""
        if not verify_mac(cfg["app_id"], raw_body, timestamp, mac, cfg["oa_secret_key"]):
            logger.warning("[zalo-oa] webhook sai chữ ký — từ chối")
            self._send(401, {"error": "bad signature"})
            return

        # Trả 200 NGAY rồi mới xử lý: Zalo gửi lại nếu chờ lâu hoặc non-200,
        # mà xử lý một lượt agent thì lâu hơn timeout của webhook nhiều.
        self._send(200, {"ok": True})
        try:
            cfg["on_event"](event)
        except Exception as e:  # pragma: no cover - phòng thủ
            logger.error(f"[zalo-oa] đẩy sự kiện vào loop lỗi: {e}")


class WebhookServer:
    """ThreadingHTTPServer chạy nền, bơm sự kiện về loop asyncio của adapter."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        app_id: str,
        oa_secret_key: str,
        webhook_path: str,
        loop: asyncio.AbstractEventLoop,
        on_event: Callable[[Dict[str, Any]], Any],
        on_oauth_start: Callable[[], str],
        on_oauth_code: Callable[[str, str], Any],
    ):
        self.host, self.port = host, port
        self._loop = loop
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._config = {
            "app_id": app_id,
            "oa_secret_key": oa_secret_key,
            "webhook_path": webhook_path,
            "oa_id": "",
            # Handler chạy trong thread của server — mọi thứ chạm vào asyncio
            # phải đi qua loop cho an toàn.
            "on_event": lambda ev: self._loop.call_soon_threadsafe(on_event, ev),
            "on_oauth_start": on_oauth_start,
            "on_oauth_code": on_oauth_code,
        }

    @property
    def bound_port(self) -> int:
        """Cổng thực tế đang nghe (khác self.port khi truyền port=0)."""
        return self._httpd.server_address[1] if self._httpd is not None else self.port

    def set_oa_id(self, oa_id: str) -> None:
        self._config["oa_id"] = oa_id

    def start(self) -> None:
        httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        httpd.daemon_threads = True
        httpd.oa_config = self._config  # type: ignore[attr-defined]
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="zalo-oa-webhook", daemon=True
        )
        self._thread.start()
        logger.info(f"[zalo-oa] webhook nghe tại http://{self.host}:{self.port}")

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception as e:
                logger.warning(f"[zalo-oa] dừng webhook lỗi: {e}")
        self._httpd = None
        self._thread = None
