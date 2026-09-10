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

3. **Nhận URL thì PHẢI chặn SSRF.** Kênh OA mở cho người lạ, nên một tool
   tải URL tuỳ ý là lỗ SSRF trỏ vào mạng nội bộ. Bản đầu chọn cách dễ là từ
   chối thẳng URL — sai lầm: agent có ảnh QR dạng URL, không còn tool nào tải
   về đĩa (52 tool cá nhân đã bị chặn khỏi kênh OA), nên nó thử lại vô hạn và
   spam khách. Ngõ cụt còn tệ hơn rủi ro. Giờ nhận URL nhưng qua
   ``_check_public_url``: chỉ http/https, phân giải DNS rồi từ chối mọi IP
   không phải public, KHÔNG theo redirect, có trần dung lượng.

4. **Tên tool KHÔNG được bắt đầu bằng ``zalo_``.** Plugin Zalo cá nhân cài
   cùng máy đăng ký một hook ``pre_tool_call`` gác mọi tool có tiền tố đó
   (``_is_zalo_tool = _base.startswith("zalo_")``) và TỪ CHỐI khi phiên không
   phải ``zalo-personal`` — tức mọi phiên của kênh OA. Đã dính thật: bản đầu
   đặt tên ``zalo_oa_send_image`` nên bị chặn, agent thử lại vô hạn và spam
   khách hàng chục tin. Danh sách miễn trừ ``_SAFE_SEND`` của họ chỉ có tool
   của chính họ, ta không thêm vào được.

Gửi tin văn bản KHÔNG thuộc phạm vi ở đây — adapter tự trả lời trong luồng
hội thoại. Hai tool này chỉ để đính kèm.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

try:  # pragma: no cover - production nạp plugin theo đường dẫn
    from . import oa_landing_bridge as _bridge  # type: ignore
except Exception:  # pragma: no cover
    import oa_landing_bridge as _bridge  # type: ignore

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


def _safe_basename(name: str) -> str:
    """Chỉ giữ phần tên file. Chặn ../ và dấu phân cách để một filename do
    model đặt không ghi ra ngoài thư mục tạm."""
    base = os.path.basename((name or "").replace("\\", "/").strip()) or "tepdinhkem"
    return base[:120]


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


# ── tải file từ URL, có chặn SSRF ─────────────────────────────────────────

# Thời gian chờ khi tải. Ngắn để một URL treo không giữ luôn lượt hội thoại.
_FETCH_TIMEOUT_S = 20.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Chặn redirect. Cho phép redirect là mở lại đúng lỗ vừa bịt: URL công
    khai có thể 302 sang 127.0.0.1 hoặc 169.254.169.254."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _check_public_url(url: str) -> Optional[str]:
    """Trả None nếu URL an toàn, hoặc câu lỗi nếu không.

    Phân giải DNS rồi soi TỪNG địa chỉ trả về: chỉ chấp nhận IP public. Chặn
    loopback, private, link-local (gồm 169.254.169.254 metadata của cloud),
    multicast và reserved.

    Còn một khe hẹp là DNS rebinding giữa lúc kiểm và lúc tải; đóng hẳn thì
    phải tự mở socket tới IP đã kiểm rồi ép SNI, phức tạp hơn nhiều. Với mức
    rủi ro ở đây (chỉ tải về rồi upload cho Zalo, không đọc nội dung ra) thì
    kiểm DNS + cấm redirect là đủ.
    """
    try:
        parts = urllib.parse.urlparse(url)
    except ValueError:
        return "URL không hợp lệ"
    if parts.scheme not in ("http", "https"):
        return f"chỉ nhận http/https, không nhận {parts.scheme!r}"
    host = parts.hostname
    if not host:
        return "URL thiếu tên miền"
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as e:
        return f"không phân giải được tên miền {host}: {e}"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            return f"URL trỏ vào địa chỉ nội bộ ({ip}) — từ chối"
    return None


def _download(url: str, max_bytes: int) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Trả (data, filename_goi_y, loi)."""
    guard = _check_public_url(url)
    if guard:
        return None, None, guard
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-zalo-oa/1.0"})
    try:
        with opener.open(req, timeout=_FETCH_TIMEOUT_S) as r:
            # Đọc dư 1 byte để phân biệt "vừa đủ trần" với "vượt trần".
            data = r.read(max_bytes + 1)
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return None, None, "URL chuyển hướng — không theo redirect vì lý do an toàn"
        return None, None, f"tải URL lỗi HTTP {e.code}"
    except Exception as e:
        return None, None, f"tải URL thất bại: {e}"
    if not data:
        return None, None, "URL trả về nội dung rỗng"
    if len(data) > max_bytes:
        return None, None, f"file từ URL vượt trần {max_bytes} byte"
    name = os.path.basename(urllib.parse.urlparse(url).path) or ""
    if "." not in name:
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
               "image/webp": ".webp", "application/pdf": ".pdf"}.get(ctype, "")
        name = (name or "tepdinhkem") + ext
    return data, name, None


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

    url = _str(params.get("url")) or _str(params.get("file_url")) or _str(params.get("image_url"))
    file_path = _str(params.get("file_path"))
    if not url and not file_path:
        return {
            "success": False,
            "error": "cần file_path (đường dẫn trên máy chủ) hoặc url (http/https công khai)",
        }

    tmp_path: Optional[Path] = None
    if not file_path:
        # Nguồn là URL: tải về thư mục tạm rồi gửi như file thường.
        data, name, err = _download(url, SEND_FILE_MAX_BYTES)
        if err:
            return {"success": False, "error": err, "url": url}
        try:
            tmp_dir = Path(tempfile.mkdtemp(prefix="zalo-oa-"))
            tmp_path = tmp_dir / _safe_basename(_str(params.get("filename")) or name)
            tmp_path.write_bytes(data)
        except OSError as e:
            return {"success": False, "error": f"không ghi được file tạm: {e}"}
        path, size = tmp_path, len(data)
    else:
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

    try:
        result, err = _run_on_gateway_loop(
            adapter._send_attachment(
                chat_id, path, caption, force_file=force_file, override_name=filename
            )
        )
    finally:
        if tmp_path is not None:
            # Dọn file tạm dù gửi thành công hay không — không để rác tích lại.
            try:
                tmp_path.unlink(missing_ok=True)
                tmp_path.parent.rmdir()
            except OSError:
                pass
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


def _as_tool_result(payload: Dict[str, Any]) -> str:
    """Trả CHUỖI JSON, không trả dict.

    Tin ``role="tool"`` gửi lên provider phải có ``content`` là chuỗi hoặc
    mảng. Trả dict thô thì DeepSeek đáp HTTP 400 "content should be a string
    or a list" — và vì tin hỏng nằm lại trong lịch sử, MỌI lượt sau của phiên
    đó đều fail, không riêng lượt gây lỗi. Hội thoại coi như chết hẳn.

    Đã dính thật trên production: một tin ``oa_send_file`` trả dict làm hỏng
    vĩnh viễn phiên chat của khách. Trong cùng payload đó, 100 tin tool khác
    đều là chuỗi — ta là ngoại lệ duy nhất.
    """
    return json.dumps(payload, ensure_ascii=False)


def handle_send_file(args: Any = None, **kwargs) -> str:
    return _as_tool_result(_send(_params(args, kwargs), force_file=True))


def handle_send_image(args: Any = None, **kwargs) -> str:
    return _as_tool_result(_send(_params(args, kwargs), force_file=False))


# ── đưa ảnh khách gửi lên landing ─────────────────────────────────────────

# Trần đọc phản hồi của MCP. Phản hồi chỉ là JSON metadata vài trăm byte;
# đặt trần để một upstream hỏng không kéo cả lượt hội thoại đi theo.
_BRIDGE_RESP_MAX_BYTES = 256 * 1024
_BRIDGE_TIMEOUT_S = 120.0


def _bridge_http_post(url: str, headers: Dict[str, str],
                      body: Dict[str, Any]) -> Dict[str, Any]:
    """POST JSON sang MCP, KHÔNG đi theo redirect. Trả dict đã parse."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=_BRIDGE_TIMEOUT_S) as resp:
            raw = resp.read(_BRIDGE_RESP_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        # 4xx/5xx vẫn có thân JSON nói rõ lỗi — đọc để agent tự sửa.
        try:
            raw = e.read(_BRIDGE_RESP_MAX_BYTES + 1)
        except Exception:
            raise _bridge.BridgeError(f"HTTP {e.code}") from None
    except Exception as e:
        raise _bridge.BridgeError(f"transport error: {e.__class__.__name__}") from None
    if len(raw) > _BRIDGE_RESP_MAX_BYTES:
        raise _bridge.BridgeError("response too large")
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        raise _bridge.BridgeError("invalid JSON response") from None
    if isinstance(parsed, dict) and isinstance(parsed.get("result"), str):
        # REST của MCP bọc kết quả trong chuỗi JSON: {"result": "{...}"}.
        try:
            inner = json.loads(parsed["result"])
        except ValueError:
            inner = None
        if isinstance(inner, dict):
            return inner
    return parsed if isinstance(parsed, dict) else {}


def _bridge_session_resolver(task_id: str) -> Optional[Dict[str, str]]:
    """chat_id (khoá sổ ảnh) + conv_id (X-Session) từ ``task_id`` tin cậy.

    conv_id PHẢI là ``zalo-oa:<chat_id>`` — đúng chuỗi mà Hermes chèn vào
    ``conversation_id`` cho các tool ``mcp_tino_*`` của kênh này
    (``_trusted_conversation_id``: ``<platform>:<chat_id>``). Lệch một chữ là
    sang hội thoại khác: mất phiên OTP của khách, và trang đã có chủ thì
    ``require_owner`` phía MCP từ chối thẳng.
    """
    chat_id = resolve_chat_id_from_task(task_id)
    if not chat_id:
        return None
    return {"chat_id": chat_id, "conv_id": f"zalo-oa:{chat_id}"}


def _upload_recent_image(params: Dict[str, Any]) -> Dict[str, Any]:
    adapter = _LIVE_ADAPTER
    if adapter is None:
        return {"success": False, "error": "kênh zalo-oa chưa kết nối"}

    slug = _str(params.get("slug"))
    filename = _str(params.get("filename")) or None
    try:
        count = max(1, min(int(params.get("count") or 1), 5))
    except (TypeError, ValueError):
        count = 1

    try:
        cfg = _bridge.load_bridge_config(dict(os.environ), str(adapter.media_dir))
        bridge = _bridge.OaLandingBridge(
            cfg,
            recent_fn=lambda chat_id, n: adapter.recent_images(chat_id, count=n),
            session_resolver=_bridge_session_resolver,
            http_post=_bridge_http_post,
        )
        result = bridge.upload_recent(
            task_id=_str(params.get("task_id")),
            slug=slug, filename=filename, count=count,
        )
    except _bridge.BridgeError as e:
        err = str(e)
        # Chỉ đường để agent tự sửa thay vì đổ tại ảnh của khách.
        if "upload rejected" in err or "not_found" in err or "hội thoại khác" in err:
            hint = ("Slug có thể SAI hoặc trang không thuộc hội thoại này. Gọi "
                    "mcp_tino_landing_list lấy đúng slug rồi gọi lại. Nếu trang đã "
                    "có chủ, khách phải auth_start/auth_verify trước. KHÔNG tự bịa slug.")
        elif "no recent image" in err:
            hint = "Nhờ khách gửi lại ảnh dạng ẢNH (không phải File), rồi thử lại."
        elif "not configured" in err:
            hint = ("Máy chủ chưa cấu hình TINO_LANDING_BRIDGE_URL/KEY — báo kỹ thuật, "
                    "ĐỪNG bảo khách gửi lại ảnh.")
        else:
            hint = "Thử lại; nếu vẫn lỗi, nhờ khách gửi lại ảnh dạng ẢNH (không phải File)."
        return {"success": False, "error": err, "hint": hint}
    except Exception:
        logger.warning("[zalo-oa] cầu ảnh landing lỗi", exc_info=True)
        return {"success": False, "error": "upload lỗi nội bộ, thử lại sau."}

    return {"success": True, **result, "hint": _placement_hint(result)}


def _placement_hint(result: Dict[str, Any]) -> str:
    """Dặn agent ĐẶT ảnh vào trang thế nào — thiếu câu này agent hay bỏ lửng
    ở bước upload rồi báo khách là xong."""
    images = result.get("images") or []
    refs = [i.get("image_ref") for i in images if i.get("image_ref")]
    if refs:
        return ("Đặt ảnh vào trang bằng ops landing_update, path ảnh nhận đúng giá trị "
                f"image_ref (vd \"{refs[0]}\"). Gọi landing_get trước để biết index "
                "block. TUYỆT ĐỐI không dùng image_url cho trang mẫu TinoPage.")
    urls = [i.get("image_url") for i in images if i.get("image_url")]
    if urls:
        return (f"Dùng image_url ({urls[0]}) làm hero_image/gallery trong HTML của "
                "trang tự thiết kế.")
    return "Upload xong nhưng không nhận được địa chỉ ảnh — thử lại."


def handle_upload_recent_image_to_landing(args: Any = None, **kwargs) -> str:
    return _as_tool_result(_upload_recent_image(_params(args, kwargs)))


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
    "name": "oa_send_file",
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
                "description": "Đường dẫn tệp trên máy chủ. Dùng cái này HOẶC url.",
            },
            "url": {
                "type": "string",
                "description": "URL http/https công khai của tệp. Tool tự tải về rồi gửi.",
            },
            **_RECIPIENT_PROPS,
        },
        "required": [],
    },
}

SEND_IMAGE_SCHEMA = {
    "name": "oa_send_image",
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
                "description": "Đường dẫn ảnh trên máy chủ. Dùng cái này HOẶC url.",
            },
            "url": {
                "type": "string",
                "description": "URL http/https công khai của ảnh. Tool tự tải về rồi gửi.",
            },
            **_RECIPIENT_PROPS,
        },
        "required": [],
    },
}

UPLOAD_IMAGE_SCHEMA = {
    "name": "oa_upload_recent_image_to_landing",
    "description": (
        "Đưa ảnh khách VỪA GỬI trong chat này lên website/landing của khách và "
        "trả về địa chỉ ảnh BỀN để đặt vào trang. Dùng tool này mỗi khi khách "
        "gửi ảnh và muốn ảnh lên web. "
        "TUYỆT ĐỐI KHÔNG lấy đường dẫn ảnh trên máy chủ (vd /opt/data/zalo-oa/"
        "media/....jpg — đường dẫn Hermes gợi ý cho vision_analyze) đưa vào "
        "landing_update: đường dẫn đó chỉ sống trong máy chủ, khách vào web sẽ "
        "thấy ảnh vỡ. Chỉ truyền slug; ảnh và hội thoại do máy chủ tự xác định."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "description": (
                    "Slug landing của khách (lấy bằng mcp_tino_landing_list nếu chưa chắc)."
                ),
            },
            "count": {
                "type": "integer",
                "description": "Số ảnh gần nhất cần đưa lên, 1-5. Mặc định 1.",
            },
            "filename": {
                "type": "string",
                "description": "Tên gợi ý cho file trên máy chủ (tuỳ chọn).",
            },
        },
        "required": ["slug"],
    },
}

# Toolset trùng tên toolset mặc định Hermes suy ra cho platform key "zalo-oa"
# (``f"hermes-{platform}"``), nên tool xuất hiện đúng ở kênh này.
TOOLSET = "hermes-zalo-oa"

_TOOLS = (
    ("oa_send_file", SEND_FILE_SCHEMA, handle_send_file, "📎"),
    ("oa_send_image", SEND_IMAGE_SCHEMA, handle_send_image, "🖼️"),
    ("oa_upload_recent_image_to_landing", UPLOAD_IMAGE_SCHEMA,
     handle_upload_recent_image_to_landing, "🖼️"),
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
