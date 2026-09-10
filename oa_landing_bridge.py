"""Cầu ảnh Zalo OA → landing: đẩy server-to-server, model không thấy bytes.

Vì sao phải có (ca thật 10/09, khách MimiShop): kênh OA không có đường nào đưa
ảnh khách gửi lên landing. Tool cầu của plugin Zalo cá nhân
(``zalo_upload_recent_image_to_landing``) nằm ở toolset khác VÀ bị hook
``pre_tool_call`` của plugin đó chặn cứng vì tiền tố ``zalo_`` — xem ràng buộc 4
trong ``oa_tools.py``. Bí đường, agent lấy luôn đường dẫn cục bộ mà Hermes gợi ý
cho ``vision_analyze`` (``/opt/data/zalo-oa/media/<file>.jpg``) nhét thẳng vào
``landing_update``; web-build nhận bừa nên trang xuất bản ra
``<img src="/opt/data/...">`` — 404 với mọi khách vào xem.

Ba ràng buộc phải giữ:

1. **Model chỉ được truyền ``slug``** (+ ``filename``/``count`` tuỳ chọn). Ảnh
   lấy từ sổ ảnh gần nhất của CHÍNH chat đang nói chuyện, chat lấy từ
   ``task_id`` tin cậy — model không truyền được ``chat_id`` hay đường dẫn.

2. **``X-Session`` phải đúng dạng ``zalo-oa:<user_id>``.** MCP chuẩn hoá conv
   bằng ``canon_conv`` — chỉ cắt tiền tố ``zalo:``, giữ nguyên ``zalo-oa:``.
   Gửi uid trần sẽ thành hội thoại KHÁC: mất phiên OTP của khách và
   ``require_owner`` từ chối ngay khi trang đã có chủ.

3. **base64 KHÔNG bao giờ đi qua LLM.** Đọc file → thu nhỏ → POST thẳng sang
   MCP. Kết quả trả về chỉ có ``image_url``/``image_ref``, không có đường dẫn
   cục bộ, không có tên người gửi.

Module thuần (chỉ nhận ``http_post``/``recent_fn``/``session_resolver`` tiêm
vào) nên test được mà không cần mạng lẫn Hermes sống.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

try:  # thu nhỏ ảnh trước khi gửi (fail-open: hỏng thì giữ bytes gốc)
    from .image_resize import MAX_DIMENSION_DEFAULT, resize_image_to_max_dim  # type: ignore
except Exception:  # pragma: no cover - production nạp theo đường dẫn
    from image_resize import MAX_DIMENSION_DEFAULT, resize_image_to_max_dim

# Trần bytes ĐƯỢC GỬI ĐI (trần của landing_upload_image phía MCP).
MAX_IMAGE_BYTES = 6 * 1024 * 1024
# Ảnh gốc đọc lên được phép to hơn — bước thu nhỏ kéo ảnh điện thoại xuống dưới
# trần trên. Không có Pillow thì trần 6MB cũ áp lại ở bước kiểm sau resize.
MAX_SOURCE_IMAGE_BYTES = 24 * 1024 * 1024

_STEM_RE = re.compile(r"[^a-z0-9]+")


class BridgeError(Exception):
    """Mọi lỗi kiểm tra/uỷ quyền — fail closed."""


def sniff_image_bytes(head: Optional[bytes]) -> Optional[Tuple[str, str]]:
    """``(mime, ext)`` cho ảnh nhận ra được, None nếu không phải ảnh.

    Nhận JPEG, PNG, GIF87a/89a, WebP theo bytes đầu — không tin phần mở rộng
    của tên file do Zalo đặt.
    """
    b = head or b""
    if len(b) < 12:
        return None
    if b[0] == 0xFF and b[1] == 0xD8 and b[2] == 0xFF:
        return ("image/jpeg", "jpg")
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return ("image/png", "png")
    if b[:4] == b"GIF8" and b[4] in (0x37, 0x39) and b[5] == 0x61:
        return ("image/gif", "gif")
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return ("image/webp", "webp")
    return None


def is_within_root(root: str, target: str) -> bool:
    """True khi ``target`` nằm trong ``root`` sau khi giải hết symlink."""
    try:
        root_r = os.path.realpath(root)
        tgt_r = os.path.realpath(target)
    except (OSError, ValueError):
        return False
    return tgt_r == root_r or tgt_r.startswith(root_r + os.sep)


def sanitize_stem(stem: Optional[str], fallback: str = "img") -> str:
    """Rút tên file model đặt về một mẩu slug an toàn."""
    base = os.path.basename(str(stem or "")).rsplit(".", 1)[0]
    base = _STEM_RE.sub("-", base.lower()).strip("-")
    return base[:40] or fallback


def content_addressed_name(stem: Optional[str], digest_hex: str, ext: str) -> str:
    """``<stem>-<sha256[:12]>.<ext>`` — cùng bytes thì cùng tên, gửi lại không
    đè lên ảnh khác."""
    return f"{sanitize_stem(stem)}-{digest_hex[:12]}.{ext}"


def read_cached_image(media_dir: str, local_path: str, *,
                      max_bytes: int = MAX_IMAGE_BYTES) -> Tuple[bytes, str, str]:
    """Đọc an toàn một ảnh trong kho media của OA. Trả ``(data, mime, ext)``.

    Mở theo BASENAME tương đối với fd của thư mục kho, cờ ``O_NOFOLLOW``; fstat
    / kiểm cỡ / đọc / soi magic đều làm trên CÙNG descriptor đó (không có khe
    check-then-open, không đi theo symlink ra ngoài kho).
    """
    if not local_path or not is_within_root(media_dir, local_path):
        raise BridgeError("path outside media root")
    basename = os.path.basename(local_path)
    if (not basename or basename in (".", "..") or os.sep in basename
            or (os.altsep and os.altsep in basename)):
        raise BridgeError("invalid basename")

    dir_fd = os.open(media_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(basename, flags, dir_fd=dir_fd)
    except OSError as e:
        raise BridgeError(f"open failed: {e.__class__.__name__}")
    finally:
        os.close(dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise BridgeError("not a regular file")
        if st.st_size > max_bytes:
            raise BridgeError("image too large")
        data = os.read(fd, max_bytes + 1)
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        raise BridgeError("image too large")
    sniff = sniff_image_bytes(data[:32])
    if not sniff:
        raise BridgeError("not a valid image")
    mime, ext = sniff
    return data, mime, ext


@dataclass
class BridgeConfig:
    url: str          # HTTPS cố định, vd https://mcp.tino.vn/tools/landing_upload_image
    key: str          # agent key chỉ dùng để upload (đọc từ env, không bao giờ log)
    media_dir: str
    max_dim: int = MAX_DIMENSION_DEFAULT  # cạnh dài nhất sau khi thu nhỏ


def load_bridge_config(env: Dict[str, str], media_dir: str) -> BridgeConfig:
    """Đọc cấu hình cầu từ môi trường; kiểm dạng URL trước khi tin."""
    url = str(env.get("TINO_LANDING_BRIDGE_URL") or "").strip()
    key = str(env.get("TINO_LANDING_BRIDGE_KEY") or "").strip()
    if not url or not key:
        raise BridgeError("bridge URL/key not configured")
    if not url.lower().startswith("https://"):
        raise BridgeError("bridge URL must be https")
    if "@" in url.split("//", 1)[-1].split("/", 1)[0]:
        raise BridgeError("bridge URL must not contain userinfo")
    max_dim = MAX_DIMENSION_DEFAULT
    raw_dim = str(env.get("TINO_LANDING_IMAGE_MAX_DIM") or "").strip()
    if raw_dim:
        try:
            v = int(raw_dim)
            if 16 <= v <= 8192:
                max_dim = v
        except ValueError:
            pass
    return BridgeConfig(url=url, key=key, media_dir=media_dir, max_dim=max_dim)


# session_resolver(task_id) -> {"chat_id": str, "conv_id": str} | None
#   chat_id: khoá sổ ảnh gần nhất được phép đọc
#   conv_id: id hội thoại phía MCP, gửi ở header X-Session ("zalo-oa:<chat_id>")
SessionResolver = Callable[[str], Optional[Dict[str, str]]]
# recent_fn(chat_id, count) -> danh sách bản ghi có .local_path (cũ→mới)
RecentFn = Callable[[str, int], List[Any]]
# http_post(url, headers, json_body) -> dict  (KHÔNG được đi theo redirect)
HttpPost = Callable[[str, Dict[str, str], Dict[str, Any]], Dict[str, Any]]


class OaLandingBridge:
    def __init__(self, config: BridgeConfig, *, recent_fn: RecentFn,
                 session_resolver: SessionResolver, http_post: HttpPost):
        self._cfg = config
        self._recent = recent_fn
        self._resolve = session_resolver
        self._post = http_post

    def upload_recent(self, *, task_id: str, slug: str,
                      filename: Optional[str] = None, count: int = 1) -> Dict[str, Any]:
        slug = str(slug or "").strip()
        if not slug:
            raise BridgeError("slug required")
        sess = self._resolve(str(task_id or ""))
        if not sess or not sess.get("chat_id") or not sess.get("conv_id"):
            raise BridgeError("cannot resolve current chat/conversation")
        chat_id = str(sess["chat_id"])
        conv_id = str(sess["conv_id"])

        try:
            n = max(1, min(int(count), 5))
        except (TypeError, ValueError):
            n = 1

        recents = self._recent(chat_id, n)  # cũ→mới
        if not recents:
            raise BridgeError("no recent image in this chat")

        images: List[Dict[str, Any]] = []
        for rec in recents:
            local_path = getattr(rec, "local_path", None)
            if local_path is None and isinstance(rec, dict):
                local_path = rec.get("local_path")
            data, mime, ext = read_cached_image(
                self._cfg.media_dir, str(local_path or ""),
                max_bytes=MAX_SOURCE_IMAGE_BYTES,
            )
            rr = resize_image_to_max_dim(data, mime=mime, ext=ext, max_dim=self._cfg.max_dim)
            data, mime, ext = rr.data, rr.mime, rr.ext
            if len(data) > MAX_IMAGE_BYTES:
                # Không thu nhỏ được (rr.reason) mà ảnh gốc vượt trần server.
                raise BridgeError("image too large")
            digest = hashlib.sha256(data).hexdigest()
            remote_name = content_addressed_name(filename, digest, ext)
            uploaded = self._upload_one(conv_id, slug, data, mime, remote_name)
            entry: Dict[str, Any] = {
                "image_url": uploaded["image_url"],
                "filename": remote_name,
                "mime": mime,
                "size": len(data),
            }
            if uploaded.get("image_ref"):
                # TinoPage (engine webbuild): prop của block cần ``asset://<id>``
                # chứ không phải URL công khai.
                entry["image_ref"] = uploaded["image_ref"]
            if rr.width and rr.height:
                entry["width"] = rr.width
                entry["height"] = rr.height
            images.append(entry)
        # Kết quả gọn: không có người gửi, đường dẫn cục bộ, chat id hay base64.
        return {"slug": slug, "count": len(images), "images": images}

    def _upload_one(self, conv_id: str, slug: str, data: bytes, mime: str,
                    remote_name: str) -> Dict[str, str]:
        """POST một ảnh; trả ``{"image_url", "image_ref"?}`` (không bao giờ bytes)."""
        import base64 as _b64
        headers = {
            "X-Agent-Key": self._cfg.key,
            "X-Session": conv_id,
            "Content-Type": "application/json",
        }
        body = {
            "slug": slug,
            # base64 chỉ tồn tại trong request tiến-trình-sang-tiến-trình này,
            # không bao giờ nằm trong transcript của model.
            "image_base64": "data:%s;base64,%s" % (mime, _b64.b64encode(data).decode()),
            "filename": remote_name,
        }
        resp = self._post(self._cfg.url, headers, body) or {}
        image_url = _extract_image_url(resp)
        image_ref = _extract_image_ref(resp)
        if not image_url and not image_ref:
            # Trả nguyên lỗi của MCP để agent tự sửa (vd sai slug → not_found).
            # Không kèm key, không kèm payload.
            err = _extract_error_message(resp)
            raise BridgeError(f"upload rejected: {err}" if err
                              else "upload failed: no image_url in response")
        if not _is_durable_upload(image_url, image_ref):
            raise BridgeError("upload returned a non-durable URL")
        out = {"image_url": image_url}
        if image_ref:
            out["image_ref"] = image_ref
        return out


def _extract_error_message(resp: Dict[str, Any]) -> str:
    if not isinstance(resp, dict):
        return ""
    msg = resp.get("message") or resp.get("error") or ""
    result = resp.get("result")
    if not msg and isinstance(result, dict):
        msg = result.get("message") or result.get("error") or ""
    return str(msg)[:300]


def _extract_image_url(resp: Dict[str, Any]) -> str:
    for holder in (resp, resp.get("result") if isinstance(resp, dict) else None):
        if isinstance(holder, dict):
            for key in ("image_url", "url"):
                v = holder.get(key)
                if isinstance(v, str) and v:
                    return v
    return ""


def _extract_image_ref(resp: Dict[str, Any]) -> str:
    """TinoPage (engine webbuild) trả ``image_ref: asset://<id>`` — đúng giá trị
    phải đặt vào prop của block qua ops ``landing_update``."""
    for holder in (resp, resp.get("result") if isinstance(resp, dict) else None):
        if isinstance(holder, dict):
            v = holder.get("image_ref")
            if isinstance(v, str) and v.startswith("asset://") and len(v) > len("asset://"):
                return v
    return ""


def _is_durable_asset_url(url: str) -> bool:
    """Chỉ nhận URL ``/<slug>/assets/`` (hệ cũ); loại đường truyền tạm
    (``/media/...``), đường dẫn cục bộ và giá trị rỗng."""
    u = str(url or "")
    if not u.lower().startswith("https://"):
        return False
    if "/media/" in u:
        return False
    return "/assets/" in u


def _is_durable_upload(image_url: str, image_ref: str) -> bool:
    """Bền = đã nằm trong kho của engine landing, trang trỏ vào được.

    * engine webbuild (TinoPage): MCP trả ``image_ref: asset://<id>`` — chính
      ref đó là bằng chứng, không soi dạng URL.
    * engine cũ: chỉ URL ``/<slug>/assets/`` mới tính.
    """
    if image_ref:
        return True
    return _is_durable_asset_url(image_url)
