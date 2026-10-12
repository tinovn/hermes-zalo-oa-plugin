"""Zalo Official Account platform adapter cho Hermes Agent.

Kênh CHÍNH THỨC, chạy hoàn toàn trên Open API được Zalo công bố:

  * Tin đến qua **webhook HTTPS** do Zalo gọi vào.
  * Tin đi qua **Open API v3.0** ``/oa/message/cs`` (tin Tư vấn).
  * OA **không nhắn tự do được**: chỉ trong 7 ngày kể từ tương tác cuối của
    người dùng, và chỉ miễn phí trong 48 giờ đầu — xem ``oa_window.py``.
  * Không có nhóm (ngoài GMF), không có typing/reaction, không kết bạn.

Biến môi trường bắt buộc:
    ZALO_OA_APP_ID          — App ID trên developers.zalo.me
    ZALO_OA_APP_SECRET      — App Secret (ký OAuth)
    ZALO_OA_SECRET_KEY      — OA Secret Key (ký webhook — KHÁC App Secret)
    ZALO_OA_PUBLIC_BASE_URL — URL công khai trỏ về plugin, vd https://oa.example.com

Tuỳ chọn: xem README.md và .env.example.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import secrets
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from gateway.platforms.base import (  # noqa: E402
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    resolve_channel_prompt,
    resolve_channel_skills,
)
from gateway.config import Platform  # noqa: E402

# Các module thuần của plugin: thử relative import (khi Hermes nạp plugin như
# package), fallback nạp theo path để chạy được cả khi import lẻ.
try:  # pragma: no cover - phụ thuộc cách nạp
    from . import message_filtering as _msgfilter  # type: ignore
    from . import oa_client as _oa
    from . import oa_media as _media
    from . import oa_webhook as _hook
    from . import oa_tools as _tools
    from . import outbound_scrub as _scrub
    from .oa_window import ConsultationWindow
except Exception:  # pragma: no cover
    import importlib.util as _ilu

    def _load(mod_name: str, filename: str):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        spec = _ilu.spec_from_file_location(mod_name, path)
        module = _ilu.module_from_spec(spec)
        import sys as _sys

        _sys.modules[mod_name] = module  # dataclass cần module có trong sys.modules
        spec.loader.exec_module(module)
        return module

    _msgfilter = _load("zalo_oa_message_filtering", "message_filtering.py")
    _oa = _load("zalo_oa_client", "oa_client.py")
    _media = _load("zalo_oa_media", "oa_media.py")
    _hook = _load("zalo_oa_webhook", "oa_webhook.py")
    _scrub = _load("zalo_oa_outbound_scrub", "outbound_scrub.py")
    _tools = _load("zalo_oa_tools", "oa_tools.py")
    ConsultationWindow = _load("zalo_oa_window", "oa_window.py").ConsultationWindow

_classify_outbound = _msgfilter.classify
_FilterAction = _msgfilter.FilterAction

DEFAULT_SESSION_DIR = "/opt/data/zalo-oa"
DEFAULT_WEBHOOK_PORT = 3939
DEFAULT_WEBHOOK_PATH = "/webhooks/zalo-oa"
# Zalo cắt tin quá dài; 2000 ký tự là mức an toàn dùng chung với kênh cá nhân.
DEFAULT_MAX_MESSAGE_LEN = 2000
# Chống trùng: Zalo gửi lại webhook khi không nhận được 200 kịp.
_SEEN_MSG_IDS_MAX = 1000
# Sổ ảnh gần nhất mỗi chat — nguồn DUY NHẤT cho oa_upload_recent_image_to_landing.
# Giữ ít thôi: chỉ cần đủ cho lượt "gửi 5 ảnh rồi bảo đưa lên web".
_RECENT_IMAGES_PER_CHAT = 10
# Trần số chat được theo dõi, tránh phình bộ nhớ khi OA đông khách lạ.
_RECENT_IMAGES_MAX_CHATS = 500
# Sự kiện không mang nội dung nhưng VẪN là tương tác của người dùng, tức mở
# lại cửa sổ 48h (theo tài liệu vận hành OA: quan tâm OA, chia sẻ thông tin).
WINDOW_OPENING_EVENTS = frozenset({"follow", "user_submit_info"})


def _session_dir() -> Path:
    return Path(os.getenv("ZALO_OA_SESSION_DIR") or DEFAULT_SESSION_DIR)


def _env_flag(name: str, default: str = "false") -> bool:
    return (os.getenv(name, default) or "").strip().lower() in ("1", "true", "yes", "on")


# ── Persona notice (câu đi thẳng ra khách, không qua LLM) ─────────────────


def _load_persona() -> Dict[str, Any]:
    try:
        p = _session_dir() / "oa_persona.json"
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _persona_notice(key: str, default: str) -> str:
    try:
        return _msgfilter.resolve_notice(_load_persona(), key, default)
    except Exception:
        return default


# ── Mode bảo trì (giống plugin cá nhân, state riêng cho kênh OA) ──────────

_MAINT_DEFAULT_MSG = (
    "Hệ thống đang được nâng cấp thêm tính năng mới nên tạm nghỉ một chút ạ. "
    "Sẽ quay lại sớm nhất — mọi người chờ chút rồi nhắn lại giúp nhé, "
    "xin lỗi vì sự bất tiện 🙏"
)
_MAINT_NOTICE_INTERVAL_S = 900


def _maint_file() -> Path:
    return _session_dir() / "maintenance.json"


def _get_maintenance() -> Dict[str, Any]:
    try:
        f = _maint_file()
        if f.exists():
            d = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception as e:
        logger.warning(f"[zalo-oa] đọc cờ bảo trì lỗi: {e}")
    return {"enabled": False, "message": ""}


def _set_maintenance(enabled: bool, message: str = "") -> bool:
    try:
        f = _maint_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(
            json.dumps(
                {
                    "enabled": bool(enabled),
                    "message": message or "",
                    "set_at": datetime.datetime.now().isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return True
    except Exception as e:
        logger.warning(f"[zalo-oa] ghi cờ bảo trì lỗi: {e}")
        return False


def _maintenance_message() -> str:
    custom = (_get_maintenance().get("message") or "").strip()
    return custom or _persona_notice("maintenance", _MAINT_DEFAULT_MSG)


def split_message(text: str, limit: int) -> List[str]:
    """Cắt tin dài theo đoạn/dòng/từ, không cắt giữa từ nếu tránh được."""
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if cut <= limit // 4:  # không có chỗ cắt tử tế → cắt cứng
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return [c for c in chunks if c]


class ZaloOaAdapter(BasePlatformAdapter):
    """Adapter Zalo Official Account (webhook vào, Open API ra)."""

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform("zalo-oa"))

        extra = getattr(config, "extra", {}) or {}
        self.app_id = (os.getenv("ZALO_OA_APP_ID") or extra.get("app_id", "")).strip()
        self.app_secret = (os.getenv("ZALO_OA_APP_SECRET") or extra.get("app_secret", "")).strip()
        self.oa_secret_key = (
            os.getenv("ZALO_OA_SECRET_KEY") or extra.get("oa_secret_key", "")
        ).strip()
        self.public_base_url = (
            os.getenv("ZALO_OA_PUBLIC_BASE_URL") or extra.get("public_base_url", "")
        ).strip().rstrip("/")

        self.webhook_host = os.getenv("ZALO_OA_WEBHOOK_HOST", "127.0.0.1").strip()
        self.webhook_port = int(os.getenv("ZALO_OA_WEBHOOK_PORT") or DEFAULT_WEBHOOK_PORT)
        self.webhook_path = os.getenv("ZALO_OA_WEBHOOK_PATH", DEFAULT_WEBHOOK_PATH).strip()

        # Đặt trước connect() để oa_tools hỏi tới lúc chưa kết nối vẫn ra None.
        self._loop = None
        self.session_dir = _session_dir()
        self.media_dir = self.session_dir / "media"
        # Chủ OA (user_id của sếp khi nhắn vào chính OA này) — chỉ người này
        # dùng được lệnh /bot. Để trống = không ai dùng được.
        self.owner_uid = (os.getenv("ZALO_OA_OWNER_UID") or extra.get("owner_uid", "")).strip()
        self.max_message_length = int(
            os.getenv("ZALO_OA_MAX_MESSAGE_LENGTH") or DEFAULT_MAX_MESSAGE_LEN
        )
        self.welcome_message = (os.getenv("ZALO_OA_WELCOME_MESSAGE") or "").strip()

        self.tokens = _oa.TokenStore(self.session_dir / "oa_tokens.json")
        self.client = _oa.OaClient(self.app_id, self.app_secret, self.tokens)
        self.window = ConsultationWindow(
            self.session_dir / "consultation_window.json",
            allow_paid_window=_env_flag("ZALO_OA_ALLOW_PAID_WINDOW"),
        )

        self.oa_id: str = ""
        self._server: Optional[Any] = None
        self._oauth_state: str = ""
        self._seen_msg_ids: deque = deque(maxlen=_SEEN_MSG_IDS_MAX)
        self._seen_msg_set: set = set()
        self._maint_notified: Dict[str, float] = {}
        self._profile_cache: Dict[str, Dict[str, Any]] = {}
        # chat_id -> deque các ảnh khách vừa gửi (cũ→mới), xem
        # ``remember_image``/``recent_images``.
        self._recent_images: Dict[str, deque] = {}
        # Giữ tham chiếu task đang chạy: asyncio chỉ giữ weakref, task không
        # ai cầm có thể bị GC nuốt giữa chừng.
        self._tasks: set = set()

    @property
    def name(self) -> str:
        return "zalo-oa"

    # ── sổ ảnh gần nhất ──────────────────────────────────────────────────

    def remember_image(self, chat_id: str, local_path: str) -> None:
        """Nhớ ảnh khách vừa gửi để tool đẩy lên landing dùng lại.

        Chỉ lưu đường dẫn — bytes vẫn nằm trên đĩa, không giữ trong RAM.
        """
        cid = str(chat_id or "")
        if not cid or not local_path:
            return
        book = self._recent_images.get(cid)
        if book is None:
            if len(self._recent_images) >= _RECENT_IMAGES_MAX_CHATS:
                # Bỏ chat cũ nhất theo thứ tự chèn (dict giữ thứ tự từ 3.7).
                self._recent_images.pop(next(iter(self._recent_images)), None)
            book = deque(maxlen=_RECENT_IMAGES_PER_CHAT)
            self._recent_images[cid] = book
        book.append({"local_path": str(local_path), "ts": time.time()})

    def recent_images(self, chat_id: str, count: int = 1) -> List[Dict[str, Any]]:
        """``count`` ảnh gần nhất của chat, trả theo thứ tự cũ→mới."""
        book = self._recent_images.get(str(chat_id or ""))
        if not book:
            return []
        try:
            n = max(1, min(int(count), _RECENT_IMAGES_PER_CHAT))
        except (TypeError, ValueError):
            n = 1
        return list(book)[-n:]

    # ── vòng đời ─────────────────────────────────────────────────────────
    # is_reconnect: gateway truyền bằng keyword khi watcher dựng lại kết nối
    # đã rớt. Kênh OA không giữ hàng đợi phía server (tin đến bằng webhook do
    # Zalo đẩy) nên không cần xử lý khác, nhưng PHẢI nhận tham số — thiếu là
    # gateway ném TypeError và platform không bao giờ lên được.
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        missing = [
            n
            for n, v in (
                ("ZALO_OA_APP_ID", self.app_id),
                ("ZALO_OA_APP_SECRET", self.app_secret),
                ("ZALO_OA_SECRET_KEY", self.oa_secret_key),
                ("ZALO_OA_PUBLIC_BASE_URL", self.public_base_url),
            )
            if not v
        ]
        if missing:
            logger.error(f"[zalo-oa] thiếu biến môi trường: {', '.join(missing)}")
            return False

        loop = asyncio.get_running_loop()
        # Tool gửi file chạy trong thread pool của Hermes, phải bắc cầu về
        # đúng loop này để dùng chung OaClient (và khoá chống refresh song
        # song) — xem oa_tools.py.
        self._loop = loop
        _tools.set_live_adapter(self)
        self._server = _hook.WebhookServer(
            host=self.webhook_host,
            port=self.webhook_port,
            app_id=self.app_id,
            oa_secret_key=self.oa_secret_key,
            webhook_path=self.webhook_path,
            loop=loop,
            on_event=self._on_webhook_event,
            on_oauth_start=self._oauth_start_url,
            on_oauth_code=lambda code, state: self._oauth_finish(code, state, loop),
        )
        try:
            self._server.start()
        except OSError as e:
            logger.error(f"[zalo-oa] không mở được cổng webhook {self.webhook_port}: {e}")
            return False

        logger.info(
            f"[zalo-oa] URL webhook khai báo trên developers.zalo.me: "
            f"{self.public_base_url}{self.webhook_path}"
        )
        try:
            self.oa_id = await self.client.get_oa_id()
            self._server.set_oa_id(self.oa_id)
            logger.info(f"[zalo-oa] đã kết nối OA id={self.oa_id}")
        except _oa.OaAuthError:
            logger.warning(
                "[zalo-oa] CHƯA có token — mở %s/oauth/start để cấp quyền cho OA",
                self.public_base_url,
            )
        except Exception as e:
            logger.error(f"[zalo-oa] gọi getoa lỗi: {e}")
        return True

    async def disconnect(self) -> None:
        _tools.clear_live_adapter(self)
        self._loop = None
        if self._server is not None:
            self._server.stop()
            self._server = None
        logger.info("[zalo-oa] đã dừng")

    # ── OAuth ────────────────────────────────────────────────────────────
    def _oauth_start_url(self) -> str:
        self._oauth_state = secrets.token_urlsafe(24)
        return self.client.permission_url(
            f"{self.public_base_url}/oauth/callback", self._oauth_state
        )

    def _oauth_finish(self, code: str, state: str, loop: asyncio.AbstractEventLoop):
        """Đổi code lấy token. Chạy trong thread của HTTP server."""
        if not self._oauth_state or not secrets.compare_digest(state or "", self._oauth_state):
            # state sai = có kẻ tự gọi callback để ép token của app khác vào.
            logger.warning("[zalo-oa] OAuth callback sai state — từ chối")
            return False, "state không khớp — bấm lại từ /oauth/start"
        self._oauth_state = ""
        try:
            fut = asyncio.run_coroutine_threadsafe(self.client.exchange_code(code), loop)
            fut.result(timeout=40)
            oa_fut = asyncio.run_coroutine_threadsafe(self.client.get_oa_id(), loop)
            self.oa_id = oa_fut.result(timeout=40)
            if self._server is not None:
                self._server.set_oa_id(self.oa_id)
            logger.info(f"[zalo-oa] OAuth xong, OA id={self.oa_id}")
            return True, f"OA id={self.oa_id}. Có thể đóng tab này."
        except Exception as e:
            logger.error(f"[zalo-oa] OAuth thất bại: {e}")
            return False, str(e)

    # ── tin đến ──────────────────────────────────────────────────────────
    def _on_webhook_event(self, event: Dict[str, Any]) -> None:
        """Chạy trong loop (đã qua call_soon_threadsafe)."""
        task = asyncio.create_task(self._handle_event(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _is_duplicate(self, msg_id: str) -> bool:
        if not msg_id:
            return False
        if msg_id in self._seen_msg_set:
            return True
        if len(self._seen_msg_ids) == self._seen_msg_ids.maxlen:
            self._seen_msg_set.discard(self._seen_msg_ids[0])
        self._seen_msg_ids.append(msg_id)
        self._seen_msg_set.add(msg_id)
        return False

    async def _handle_event(self, event: Dict[str, Any]) -> None:
        try:
            msg = _hook.classify_event(event)
            if msg is None:
                return
            if msg.is_self:
                # Tin do chính OA gửi (bot ta hoặc nhân viên trực trong OA
                # Manager) — không đẩy cho agent, tránh tự trả lời mình.
                return
            if self._is_duplicate(msg.msg_id):
                logger.debug(f"[zalo-oa] bỏ webhook trùng msg_id={msg.msg_id}")
                return

            # Chỉ hành động THẬT của người dùng mới mở lại cửa sổ 48h.
            # user_seen_message / user_received_message là biên nhận gửi của
            # Zalo cho tin CỦA TA — đếm chúng là tự gia hạn cửa sổ cho mình,
            # và mỗi tin gửi đi lại kéo theo hai lượt ghi file vô ích.
            if msg.event_name in WINDOW_OPENING_EVENTS or msg.event_name.startswith("user_send_"):
                self.window.mark_inbound(msg.user_id)

            if msg.is_interaction_only:
                await self._handle_interaction(msg)
                return

            # Lệnh của chủ OA: xử lý tại chỗ, không đẩy qua agent.
            text = (msg.text or "").strip()
            if (
                self.owner_uid
                and msg.user_id == self.owner_uid
                and text.lower().startswith("/bot")
            ):
                reply = self._handle_owner_command(text)
                if reply:
                    await self.send(msg.user_id, reply)
                return

            if msg.user_id != self.owner_uid and _get_maintenance().get("enabled"):
                await self._send_maintenance_notice(msg.user_id)
                return

            await self._dispatch_to_agent(msg)
        except Exception as e:
            logger.error(f"[zalo-oa] xử lý webhook lỗi: {e}", exc_info=True)

    async def _handle_interaction(self, msg) -> None:
        if msg.event_name == "follow":
            logger.info(f"[zalo-oa] người dùng {msg.user_id} vừa quan tâm OA")
            if self.welcome_message:
                # Quan tâm OA cho phép gửi lời chào NGAY — cửa sổ 48h vừa mở.
                await self.send(msg.user_id, self.welcome_message)
        elif msg.event_name == "unfollow":
            logger.info(f"[zalo-oa] người dùng {msg.user_id} bỏ quan tâm OA")
        elif msg.event_name == "user_submit_info":
            logger.info(f"[zalo-oa] người dùng {msg.user_id} vừa chia sẻ thông tin liên hệ")

    async def _send_maintenance_notice(self, user_id: str) -> None:
        now = time.time()
        if now - self._maint_notified.get(user_id, 0.0) <= _MAINT_NOTICE_INTERVAL_S:
            return
        if len(self._maint_notified) > 500:
            self._maint_notified = {
                k: v
                for k, v in self._maint_notified.items()
                if now - v <= _MAINT_NOTICE_INTERVAL_S
            }
        self._maint_notified[user_id] = now
        try:
            await self.send(user_id, _maintenance_message())
        except Exception as e:
            logger.warning(f"[zalo-oa] gửi thông báo bảo trì lỗi: {e}")

    async def _dispatch_to_agent(self, msg) -> None:
        media_urls: List[str] = []
        media_types: List[str] = []
        message_type = MessageType.TEXT
        text = msg.text or ""

        if msg.media_url:
            path = await asyncio.to_thread(
                _media.download_to, msg.media_url, self.media_dir, filename_hint=msg.filename
            )
            if path is None:
                text = (text + f"\n[{msg.media_kind} không tải được]").strip()
            else:
                media_urls.append(str(path))
                if msg.media_kind == "image":
                    media_types.append(_media.content_type_for(path.name))
                    message_type = MessageType.PHOTO
                    # Ghi sổ TRƯỚC khi giao cho agent: agent chỉ được cầm slug,
                    # đường dẫn ảnh lấy từ sổ này chứ không do model truyền.
                    self.remember_image(msg.user_id, str(path))
                elif msg.media_kind == "audio":
                    media_types.append("audio")
                    message_type = MessageType.VOICE
                elif msg.media_kind == "video":
                    media_types.append("video")
                    message_type = MessageType.DOCUMENT
                else:
                    media_types.append("file")
                    message_type = MessageType.DOCUMENT
                    text = text or f"[file: {path.name}]"

        if not text and not media_urls:
            return

        profile = await self._user_profile(msg.user_id)
        user_name = profile.get("display_name") or f"zalo-oa:{msg.user_id}"

        config_extra = getattr(self.config, "extra", {}) or {}
        channel_prompt = resolve_channel_prompt(config_extra, msg.user_id, parent_id=None)
        channel_skills = resolve_channel_skills(config_extra, msg.user_id, parent_id=None)

        source = self.build_source(
            chat_id=msg.user_id,
            chat_name=user_name,
            chat_type="dm",
            user_id=f"zalo-oa:{msg.user_id}",
            user_name=user_name,
        )
        event = MessageEvent(
            text=text,
            message_type=message_type,
            source=source,
            message_id=msg.msg_id or str(int(time.time() * 1000)),
            timestamp=datetime.datetime.now(),
            media_urls=media_urls,
            media_types=media_types,
            channel_prompt=channel_prompt,
            channel_context=None,
            auto_skill=channel_skills,
            reply_to_message_id=None,
        )
        await self.handle_message(event)

    async def _user_profile(self, user_id: str) -> Dict[str, Any]:
        """Tên hiển thị (webhook không kèm). Cache vì mỗi tin gọi một lần là phí."""
        cached = self._profile_cache.get(user_id)
        if cached is not None:
            return cached
        try:
            data = await self.client.get_user_detail(user_id)
        except Exception as e:
            logger.debug(f"[zalo-oa] lấy profile {user_id} lỗi: {e}")
            data = {}
        if len(self._profile_cache) > 2000:
            self._profile_cache.clear()
        self._profile_cache[user_id] = data
        return data

    # ── tin đi ───────────────────────────────────────────────────────────
    def _is_owner_chat(self, chat_id: str) -> bool:
        return bool(self.owner_uid) and str(chat_id) == self.owner_uid

    def _check_window(self, chat_id: str) -> Optional[SendResult]:
        decision = self.window.evaluate(str(chat_id))
        if decision.allowed:
            if not decision.within_free:
                logger.warning(f"[zalo-oa] gửi NGOÀI khung 48h cho {chat_id} — {decision.reason}")
            return None
        logger.warning(f"[zalo-oa] chặn gửi tới {chat_id}: {decision.reason}")
        return SendResult(success=False, error=f"ngoài cửa sổ gửi tin: {decision.reason}")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not content or not content.strip():
            return SendResult(success=False, error="empty content")

        # Zalo không render markdown — đổi về text thuần cho MỌI chat.
        content = _scrub.zalo_plaintext(content)

        is_owner = self._is_owner_chat(chat_id)
        decision = _classify_outbound(content, is_owner=is_owner)
        if decision.action == _FilterAction.DROP_OPERATIONAL:
            logger.info("[zalo-oa] bỏ tin vận hành nội bộ (%s)", ",".join(decision.categories))
            return SendResult(success=True, raw_response={"dropped": "operational"})
        if decision.action == _FilterAction.REPLACE_TERMINAL:
            content = _persona_notice("recovery", decision.cleaned_text)
        else:
            content = decision.cleaned_text
        if not content.strip():
            return SendResult(success=True, raw_response={"dropped": "empty_after_filter"})

        # Khách (không phải chủ OA) không được thấy chẩn đoán nội bộ, tên
        # model/nhà cung cấp, hay câu trạng thái của runtime. Kênh OA mang
        # thương hiệu doanh nghiệp nên lọt là hỏng hình ảnh thật.
        if not is_owner:
            content = _scrub.strip_internal_noise(content)
            if not content.strip():
                return SendResult(success=True, raw_response={"dropped": "internal_notice"})
            scrubbed = _scrub.scrub_outgoing(content)
            if scrubbed is None:
                logger.info("[zalo-oa] chặn tin nhiễu/nội bộ trước khi tới khách")
                return SendResult(success=True, raw_response={"dropped": "noisy_status"})
            content = scrubbed

        blocked = self._check_window(chat_id)
        if blocked is not None:
            return blocked

        quote_id = reply_to or None
        first: Optional[SendResult] = None
        for chunk in split_message(content, self.max_message_length):
            result = await self._send_text_with_retry(str(chat_id), chunk, quote_id)
            quote_id = None  # chỉ tin đầu trích dẫn, các tin sau nối tiếp
            if first is None:
                first = result
            if not result.success:
                return result
        return first or SendResult(success=False, error="no chunks sent")

    async def _send_text_with_retry(
        self, chat_id: str, text: str, quote_id: Optional[str], attempts: int = 3
    ) -> SendResult:
        delay = 1.0
        last_err = ""
        for attempt in range(1, attempts + 1):
            try:
                msg_id = await self.client.send_text(chat_id, text, quote_id)
                self.window.note_outbound(chat_id)
                return SendResult(success=True, message_id=msg_id)
            except _oa.OaTransientError as e:
                last_err = str(e)
                logger.info(f"[zalo-oa] lỗi tạm ({e}) — thử lại lần {attempt}/{attempts}")
                if attempt < attempts:
                    await asyncio.sleep(delay)
                    delay *= 3
            except _oa.OaWindowError as e:
                # Zalo từ chối vì cửa sổ/khung giờ — retry chỉ tốn quota.
                logger.warning(f"[zalo-oa] không gửi được tới {chat_id}: {e}")
                return SendResult(success=False, error=str(e))
            except (_oa.OaPermanentError, _oa.OaAuthError) as e:
                logger.error(f"[zalo-oa] gửi thất bại: {e}")
                return SendResult(success=False, error=str(e))
            except Exception as e:
                logger.error(f"[zalo-oa] gửi lỗi không rõ: {e}")
                return SendResult(success=False, error=str(e))
        return SendResult(success=False, error=f"hết lượt thử: {last_err}")

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_attachment(str(chat_id), Path(image_path), caption or "")

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if str(image_url).startswith(("http://", "https://")):
            path = await asyncio.to_thread(_media.download_to, image_url, self.media_dir)
            if path is None:
                return SendResult(success=False, error="image download failed")
            return await self._send_attachment(str(chat_id), path, caption or "")
        return await self._send_attachment(str(chat_id), Path(image_url), caption or "")

    # Tên tham số phải đúng ``file_path``: mọi call site trong Hermes (kanban
    # watcher, notification, slash command, base adapter) đều gọi bằng keyword
    # ``file_path=``. Đặt tên khác là TypeError mỗi lần gửi file — và chỉ lộ ra
    # đúng lúc gửi, không phải lúc khởi động.
    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self._send_attachment(
            str(chat_id),
            Path(file_path),
            caption or "",
            force_file=True,
            override_name=file_name,
        )

    async def _send_attachment(
        self,
        chat_id: str,
        path: Path,
        caption: str,
        force_file: bool = False,
        override_name: Optional[str] = None,
    ) -> SendResult:
        blocked = self._check_window(chat_id)
        if blocked is not None:
            return blocked
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except OSError as e:
            return SendResult(success=False, error=f"không đọc được file: {e}")

        # override_name: tên hiển thị caller muốn khách nhìn thấy, có thể khác
        # tên file tạm trên đĩa. Vẫn phải qua safe_filename.
        filename = _media.safe_filename(override_name or path.name)
        is_image = _media.is_image_name(filename) and not force_file
        try:
            if is_image:
                data, filename, fits = await asyncio.to_thread(
                    _media.compress_image_under, data, filename
                )
                if not fits:
                    # Zalo chặn ảnh quá ~1MB. Gửi text báo còn hơn im lặng.
                    logger.warning(f"[zalo-oa] ảnh {filename} vẫn quá cỡ sau khi nén")
                    return await self.send(
                        chat_id, (caption + "\n[ảnh quá lớn, không gửi được qua Zalo OA]").strip()
                    )
                msg_id = await self.client.send_image(
                    chat_id, filename, _media.content_type_for(filename), data, caption
                )
            else:
                msg_id = await self.client.send_file(
                    chat_id, filename, _media.content_type_for(filename), data, caption
                )
            self.window.note_outbound(chat_id)
            return SendResult(success=True, message_id=msg_id)
        except _oa.OaWindowError as e:
            return SendResult(success=False, error=str(e))
        except Exception as e:
            logger.error(f"[zalo-oa] gửi đính kèm lỗi: {e}")
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        # Open API không có API "đang soạn tin" — no-op để base class gọi được.
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        profile = await self._user_profile(str(chat_id))
        decision = self.window.evaluate(str(chat_id))
        return {
            "chat_id": str(chat_id),
            "chat_type": "dm",
            "display_name": profile.get("display_name") or "",
            "oa_id": self.oa_id,
            "window_allowed": decision.allowed,
            "window_reason": decision.reason,
        }

    # ── lệnh của chủ OA ──────────────────────────────────────────────────
    def _handle_owner_command(self, text: str) -> Optional[str]:
        parts = text.split(maxsplit=2)
        verb = parts[1].lower().strip() if len(parts) >= 2 else ""
        arg = parts[2].strip() if len(parts) >= 3 else ""

        if not verb or verb in ("help", "?", "h"):
            return (
                "🛠 Lệnh kênh Zalo OA:\n"
                "/bot status              — trạng thái kết nối + token\n"
                "/bot window <user_id>    — xem cửa sổ 48h/7 ngày của một khách\n"
                "/bot baotri              — xem trạng thái bảo trì\n"
                "/bot baotri <câu>        — BẬT bảo trì kèm câu báo khách\n"
                "/bot baotri off          — TẮT bảo trì"
            )
        if verb == "status":
            stored = self.tokens.load()
            exp = float(stored.get("expires_at") or 0)
            left = int(exp - time.time()) if exp else 0
            maint = _get_maintenance()
            return (
                "📊 Kênh Zalo OA:\n"
                f"• oa_id: {self.oa_id or 'chưa kết nối'}\n"
                f"• token: {'còn ' + str(left) + 's' if left > 0 else 'hết hạn / chưa có'}\n"
                f"• webhook: {self.public_base_url}{self.webhook_path}\n"
                f"• tin ngoài 48h: {'CHO PHÉP (mất phí)' if self.window.allow_paid_window else 'CHẶN'}\n"
                f"• bảo trì: {'BẬT' if maint.get('enabled') else 'TẮT'}"
            )
        if verb == "window":
            uid = arg or self.owner_uid
            d = self.window.evaluate(uid)
            hours = f"{d.seconds_since / 3600:.1f}h" if d.seconds_since is not None else "chưa có"
            return (
                f"⏱ Cửa sổ tư vấn với {uid}:\n"
                f"• tương tác cuối: {hours} trước\n"
                f"• đã gửi trong kỳ: {self.window.sent_count(uid)} tin\n"
                f"• {'GỬI ĐƯỢC' if d.allowed else 'KHÔNG gửi được'} — {d.reason}"
            )
        if verb in ("baotri", "bao-tri", "maintenance", "maint"):
            low = arg.lower()
            if low in ("off", "tắt", "tat", "stop", "0", "false"):
                if not _set_maintenance(False):
                    return "⚠️ Không ghi được file trạng thái bảo trì."
                self._maint_notified.clear()
                return "✅ Đã TẮT mode bảo trì."
            if not arg:
                cur = _get_maintenance()
                if cur.get("enabled"):
                    return f"🔧 Bảo trì: ĐANG BẬT.\nCâu gửi khách: {_maintenance_message()}"
                return "🔧 Bảo trì: ĐANG TẮT.\nBật: /bot baotri <câu báo khách>"
            if low in ("on", "bật", "bat", "1", "true"):
                arg = ""
            if not _set_maintenance(True, arg):
                return "⚠️ Không ghi được file trạng thái bảo trì."
            self._maint_notified.clear()
            return f"✅ Đã BẬT mode bảo trì.\nCâu gửi khách:\n{_maintenance_message()}"
        return f"Lệnh '{verb}' không hiểu. Gõ /bot help."


# ── đăng ký plugin ────────────────────────────────────────────────────────


def check_requirements() -> bool:
    return bool(
        os.getenv("ZALO_OA_APP_ID")
        and os.getenv("ZALO_OA_APP_SECRET")
        and os.getenv("ZALO_OA_SECRET_KEY")
        and os.getenv("ZALO_OA_PUBLIC_BASE_URL")
    )


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(os.getenv("ZALO_OA_APP_ID") or extra.get("app_id"))


def is_connected() -> bool:
    """Có token còn hạn = coi như đã kết nối (dùng cho UI trạng thái)."""
    try:
        store = _oa.TokenStore(_session_dir() / "oa_tokens.json")
        stored = store.load()
        return bool(stored.get("access_token")) and float(stored.get("expires_at") or 0) > time.time()
    except Exception:
        return False


def register(ctx):
    """Điểm vào plugin."""
    kwargs = dict(
        name="zalo-oa",
        label="Zalo OA (chính thức)",
        adapter_factory=lambda cfg: ZaloOaAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=[
            "ZALO_OA_APP_ID",
            "ZALO_OA_APP_SECRET",
            "ZALO_OA_SECRET_KEY",
            "ZALO_OA_PUBLIC_BASE_URL",
        ],
        install_hint=(
            "Tạo app trên developers.zalo.me, liên kết OA, khai báo webhook "
            "<PUBLIC_BASE_URL>/webhooks/zalo-oa rồi mở <PUBLIC_BASE_URL>/oauth/start để cấp quyền."
        ),
        emoji="🏢",
        pii_safe=True,
        max_message_length=DEFAULT_MAX_MESSAGE_LEN,
        allowed_users_env="ZALO_OA_ALLOWED_USER_IDS",
        allow_all_env="ZALO_OA_ALLOW_ALL_USERS",
        platform_hint=(
            "Bạn đang chat qua Zalo Official Account. KHÔNG dùng markdown — Zalo "
            "chỉ render plain text. Câu ngắn gọn, lịch sự, đúng giọng chăm sóc "
            "khách hàng của doanh nghiệp. Đây là kênh CHÍNH THỨC: không hứa hẹn "
            "sai, không xin số điện thoại ngoài luồng. KHÔNG tự giới thiệu là "
            "Hermes / Codex / GPT / OpenAI / Anthropic. "
            "Gửi tệp/ảnh cho khách thì dùng oa_send_file / "
            "oa_send_image. TUYỆT ĐỐI không dùng zalo_send_file, "
            "zalo_send_image hay bất kỳ tool zalo_* nào khác — chúng thuộc kênh "
            "Zalo cá nhân, không gửi được qua OA. Tệp tài liệu phải là PDF; "
            "Zalo OA từ chối .docx/.doc/.csv."
        ),
    )
    try:
        ctx.register_platform(**kwargs, cron_deliver_env_var="ZALO_OA_HOME_CHANNEL")
    except TypeError:
        ctx.register_platform(**kwargs)

    # Tool đính kèm đi bằng Open API của OA. Bắt buộc phải có: nếu không,
    # agent sẽ vớ lấy zalo_send_file của kênh Zalo CÁ NHÂN và luôn thất bại.
    _tools.register_tools(ctx)
