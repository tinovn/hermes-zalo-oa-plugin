"""Tool gửi file/ảnh cho kênh Zalo OA — đi bằng Open API của chính OA.

Vì sao phải có: agent chạy trên kênh OA nhìn thấy luôn cả 52 tool của plugin
Zalo cá nhân, và nó chọn ``zalo_send_file``. Tool đó đẩy qua sidecar của TÀI
KHOẢN CÁ NHÂN — không liên quan gì tới OA — nên luôn hỏng ("sidecar HTTP 500:
Tham số không hợp lệ"). Hai tool ở đây là đường đúng: upload qua
``/v2.0/oa/upload/*`` rồi gửi bằng chính token của OA.

Ba ràng buộc phải trả giá mới biết, đừng phá:

1. **Dùng lại ``OaClient`` đang sống của adapter**, tuyệt đối không tạo client
   mới từ file token. Refresh token của Zalo dùng MỘT LẦN: hai client cùng
   refresh thì cặp của bên kia thành rác, mất quyền, phải OAuth lại bằng tay.

2. **Bắc cầu sang event loop của gateway.** Hermes chạy tool trong thread pool
   (``DaemonThreadPoolExecutor``), còn ``OaClient`` và ``asyncio.Lock`` chống
   refresh song song thuộc loop của gateway. Gọi coroutine từ thread khác sẽ
   làm hỏng khoá đó, nên phải đi qua ``run_coroutine_threadsafe``.

3. **Chỉ nhận file local, KHÔNG nhận URL.** Kênh OA mở cho người lạ nhắn vào;
   một tool tải URL tuỳ ý do agent chọn là lỗ SSRF trỏ thẳng vào mạng nội bộ.
   Cần gửi file từ web thì tải về đĩa bằng tool khác trước, rồi truyền path.

Gửi tin văn bản KHÔNG thuộc phạm vi ở đây — adapter tự trả lời trong luồng
hội thoại. Hai tool này chỉ để đính kèm.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Adapter đang sống, do chính adapter đặt vào trong connect(). Đây là đường
# DUY NHẤT để tool với tới token — xem ràng buộc 1 ở docstring.
_LIVE_ADAPTER: Any = None

# Zalo OA cho file tới ~20MB; chừa biên cho phần multipart bọc ngoài.
SEND_FILE_MAX_BYTES = 18 * 1024 * 1024
# Trần chờ một lượt gửi (upload + send). Quá thì trả lỗi cho agent thay vì
# treo cả lượt hội thoại.
SEND_TIMEOUT_S = 120.0


def set_live_adapter(adapter: Any) -> None:
    global _LIVE_ADAPTER
    _LIVE_ADAPTER = adapter


def clear_live_adapter(adapter: Any = None) -> None:
    """Gỡ tham chiếu. Truyền adapter để chỉ gỡ đúng instance của mình — tránh
    một adapter đang tắt xoá mất adapter mới vừa lên khi gateway reconnect."""
    global _LIVE_ADAPTER
    if adapter is None or _LIVE_ADAPTER is adapter:
        _LIVE_ADAPTER = None


# ── tham số ───────────────────────────────────────────────────────────────


def _params(args: Any, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Gộp tham số. Model có khi nhét hết vào ``args`` dạng dict, có khi dạng
    chuỗi JSON, có khi rải thẳng ra kwargs — đỡ cả ba."""
    merged: Dict[str, Any] = {}
    if isinstance(args, dict):
        merged.update(args)
    elif isinstance(args, str) and args.strip():
        try:
            parsed = json.loads(args)
            if isinstance(parsed, dict):
                merged.update(parsed)
        except (ValueError, TypeError):
            pass
    for k, v in (kwargs or {}).items():
        merged.setdefault(k, v)
    return merged


def _str(v: Any) -> str:
    """Ép tham số về chuỗi sạch. Một số model gói chuỗi trong object
    (``{"value": "..."}``) — không đỡ thì handler vỡ ngay ở .strip()."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        for k in ("value", "text", "path", "file_path", "id"):
            inner = v.get(k)
            if isinstance(inner, str):
                return inner.strip()
    return str(v).strip()


# ── tìm người nhận ────────────────────────────────────────────────────────


def _hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME") or (Path.home() / ".hermes"))


def resolve_chat_id_from_task(task_id: str) -> str:
    """Agent rất hay quên truyền user_id. Hermes truyền ``task_id`` vào mọi
    tool và task_id chính là session_id, nên tra ``sessions.json`` là ra người
    đang chat. Không thấy thì trả rỗng để caller báo lỗi rõ ràng."""
    if not task_id:
        return ""
    try:
        with open(_hermes_home() / "sessions" / "sessions.json", encoding="utf-8") as f:
            sessions = json.load(f)
    except (OSError, ValueError):
        return ""
    if not isinstance(sessions, dict):
        return ""
    for sess in sessions.values():
        # sessions.json có cả entry sentinel không phải session (vd "_README").
        if not isinstance(sess, dict):
            continue
        if sess.get("platform") == "zalo-oa" and sess.get("session_id") == task_id:
            chat_id = (sess.get("origin") or {}).get("chat_id")
            if chat_id:
                return str(chat_id)
    return ""


# ── bắc cầu sang loop của gateway ─────────────────────────────────────────


def _run_on_gateway_loop(coro) -> Tuple[Any, Optional[str]]:
    adapter = _LIVE_ADAPTER
    loop = getattr(adapter, "_loop", None) if adapter is not None else None
    if adapter is None or loop is None or loop.is_closed():
        coro.close()  # không await thì phải đóng, nếu không Python cảnh báo
        return None, "kênh zalo-oa chưa kết nối — không gửi được"
    try:
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=SEND_TIMEOUT_S), None
    except (asyncio.TimeoutError, TimeoutError):
        return None, f"quá {int(SEND_TIMEOUT_S)}s vẫn chưa gửi xong"
    except Exception as e:  # pragma: no cover - phòng thủ
        return None, str(e)


# ── thân chung của hai tool ───────────────────────────────────────────────


def _send(params: Dict[str, Any], *, force_file: bool) -> Dict[str, Any]:
    adapter = _LIVE_ADAPTER
    if adapter is None:
        return {"success": False, "error": "kênh zalo-oa chưa kết nối"}

    # Chặn URL ngay và nói rõ lý do, đừng để model thử lại mãi. Xem ràng buộc 3.
    if _str(params.get("url")) or _str(params.get("file_url")):
        return {
            "success": False,
            "error": "tool này không nhận URL (tránh SSRF) — tải file về đĩa rồi truyền file_path",
        }

    file_path = _str(params.get("file_path"))
    if not file_path:
        return {"success": False, "error": "thiếu file_path — đường dẫn file trên máy chủ"}

    path = Path(file_path)
    try:
        if not path.is_file():
            return {"success": False, "error": f"file không tồn tại: {file_path}"}
        size = path.stat().st_size
    except OSError as e:
        return {"success": False, "error": f"không đọc được file: {e}"}
    if size <= 0:
        return {"success": False, "error": f"file rỗng: {file_path}"}
    if size > SEND_FILE_MAX_BYTES:
        return {
            "success": False,
            "error": f"file {size} byte, vượt trần {SEND_FILE_MAX_BYTES} byte của Zalo OA",
        }

    chat_id = _str(params.get("user_id")) or _str(params.get("chat_id"))
    if not chat_id:
        chat_id = resolve_chat_id_from_task(_str(params.get("task_id")))
    if not chat_id:
        return {
            "success": False,
            "error": "không xác định được người nhận — truyền user_id của khách",
        }

    caption = _str(params.get("caption"))
    filename = _str(params.get("filename")) or None

    result, err = _run_on_gateway_loop(
        adapter._send_attachment(
            chat_id, path, caption, force_file=force_file, override_name=filename
        )
    )
    if err:
        return {"success": False, "error": err, "chat_id": chat_id}
    if result is None or not getattr(result, "success", False):
        return {
            "success": False,
            "error": getattr(result, "error", None) or "gửi thất bại",
            "chat_id": chat_id,
        }
    return {
        "success": True,
        "chat_id": chat_id,
        "message_id": getattr(result, "message_id", "") or "",
        "size_bytes": size,
    }


def handle_send_file(args: Any = None, **kwargs) -> Dict[str, Any]:
    return _send(_params(args, kwargs), force_file=True)


def handle_send_image(args: Any = None, **kwargs) -> Dict[str, Any]:
    return _send(_params(args, kwargs), force_file=False)


# ── schema ────────────────────────────────────────────────────────────────

_RECIPIENT_PROPS = {
    "user_id": {
        "type": "string",
        "description": "user_id Zalo của người nhận. Bỏ trống = người đang chat.",
    },
    "caption": {"type": "string", "description": "Chú thích kèm theo, có thể bỏ trống."},
    "filename": {
        "type": "string",
        "description": "Tên hiển thị cho khách. Bỏ trống thì lấy tên file trên đĩa.",
    },
}

SEND_FILE_SCHEMA = {
    "name": "zalo_oa_send_file",
    "description": (
        "Gửi một tệp từ máy chủ tới khách qua Zalo Official Account. "
        "QUAN TRỌNG: Zalo OA chỉ nhận PDF cho tệp tài liệu — .docx/.doc/.csv bị "
        "từ chối với lỗi -201, hãy xuất ra PDF trước khi gọi tool này. "
        "Dùng tool này chứ KHÔNG dùng zalo_send_file hay bất kỳ tool zalo_* nào "
        "khác: những tool đó thuộc kênh Zalo cá nhân và không gửi được qua OA."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Đường dẫn tệp trên máy chủ. Không nhận URL.",
            },
            **_RECIPIENT_PROPS,
        },
        "required": ["file_path"],
    },
}

SEND_IMAGE_SCHEMA = {
    "name": "zalo_oa_send_image",
    "description": (
        "Gửi một ảnh từ máy chủ tới khách qua Zalo Official Account. Chỉ nhận "
        "png/jpeg/gif/webp; ảnh lớn được tự nén xuống dưới trần ~1MB của Zalo. "
        "Dùng tool này chứ KHÔNG dùng zalo_send_image của kênh Zalo cá nhân."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Đường dẫn ảnh trên máy chủ. Không nhận URL.",
            },
            **_RECIPIENT_PROPS,
        },
        "required": ["file_path"],
    },
}

# Toolset trùng tên toolset mặc định Hermes suy ra cho platform key "zalo-oa"
# (``f"hermes-{platform}"``), nên tool xuất hiện đúng ở kênh này.
TOOLSET = "hermes-zalo-oa"

_TOOLS = (
    ("zalo_oa_send_file", SEND_FILE_SCHEMA, handle_send_file, "📎"),
    ("zalo_oa_send_image", SEND_IMAGE_SCHEMA, handle_send_image, "🖼️"),
)


def register_tools(ctx) -> None:
    """Đăng ký hai tool. Lỗi đăng ký không được làm chết cả plugin: mất tool
    thì chỉ mất khả năng đính kèm, còn nhắn tin vẫn phải chạy."""
    for name, schema, handler, emoji in _TOOLS:
        try:
            ctx.register_tool(
                name=name,
                toolset=TOOLSET,
                schema=schema,
                handler=handler,
                emoji=emoji,
            )
        except Exception as e:  # pragma: no cover - tuỳ phiên bản Hermes
            logger.warning(f"[zalo-oa] không đăng ký được tool {name}: {e}")
