"""Media cho kênh OA: nén ảnh xuống dưới trần của Zalo, tải media inbound về.

Hai ràng buộc riêng của kênh OA:

  * **Ảnh gửi đi phải nhỏ hơn ~1MB** và chỉ png/jpeg/gif/webp. Ảnh chụp điện
    thoại 3-12MB bị từ chối thẳng, nên phải nén TRƯỚC khi upload.
  * **Media inbound đến dưới dạng URL**, không phải file sẵn trên đĩa, nên phải
    tự tải về, có trần dung lượng để một file khổng lồ không thổi bay RAM/đĩa.

Nén ảnh fail-open như ``image_resize``: hỏng thì trả nguyên bản, để tầng trên
quyết định (gửi link thay vì ảnh), không bao giờ ném lỗi ra ngoài.
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Optional, Tuple

from image_resize import resize_image_to_max_dim

logger = logging.getLogger(__name__)

# Trần thật của Zalo quanh 1MB; chừa biên để phần multipart không vượt.
IMAGE_TARGET_BYTES = 900_000
# Trần tải media inbound. Zalo OA cho gửi file tới 20MB+; ta cắt ở 25MB để
# một file rác không làm nghẽn tiến trình.
INBOUND_MAX_BYTES = 25 * 1024 * 1024
_DOWNLOAD_TIMEOUT_S = 30.0

IMAGE_EXTS = frozenset({"jpg", "jpeg", "png", "gif", "webp"})
# GIF không nén lại được (re-encode làm mất animation) — quá cỡ thì để tầng
# trên chuyển sang gửi link.
COMPRESSIBLE_EXTS = frozenset({"jpg", "jpeg", "png", "webp"})

_IMAGE_CONTENT_TYPE = {
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
}
_FILE_CONTENT_TYPE = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "csv": "text/csv",
    "txt": "text/plain",
    "html": "text/html",
    "json": "application/json",
    "zip": "application/zip",
}

# Thang thu nhỏ dần: dừng ngay khi lọt trần để giữ ảnh nét nhất có thể.
_DIM_LADDER = (1600, 1280, 1024, 800, 640)


def ext_of(filename: str) -> str:
    m = re.search(r"\.([A-Za-z0-9]{1,8})$", filename or "")
    return m.group(1).lower() if m else ""


def content_type_for(filename: str) -> str:
    ext = ext_of(filename)
    if ext in _IMAGE_CONTENT_TYPE:
        return _IMAGE_CONTENT_TYPE[ext]
    return _FILE_CONTENT_TYPE.get(ext, "application/octet-stream")


def is_image_name(filename: str) -> bool:
    return ext_of(filename) in IMAGE_EXTS


def safe_filename(name: str, fallback: str = "file") -> str:
    """Bỏ đường dẫn và ký tự lạ — tên từ URL/Zalo là dữ liệu không tin được."""
    base = (name or "").replace("\\", "/").split("/")[-1].strip()
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or fallback
    return base[:120]


def compress_image_under(
    data: bytes, filename: str, target_bytes: int = IMAGE_TARGET_BYTES
) -> Tuple[bytes, str, bool]:
    """Nén ảnh xuống dưới ``target_bytes``.

    Trả về ``(data, filename, fits)``. ``fits=False`` nghĩa là vẫn quá cỡ —
    caller nên gửi link thay vì cố upload (Zalo sẽ từ chối).
    """
    if len(data) <= target_bytes:
        return data, filename, True
    ext = ext_of(filename)
    if ext not in COMPRESSIBLE_EXTS:
        return data, filename, False

    mime = _IMAGE_CONTENT_TYPE.get(ext, "image/jpeg")
    best = data
    for max_dim in _DIM_LADDER:
        out = resize_image_to_max_dim(data, mime=mime, ext=ext, max_dim=max_dim)
        if not out.resized:
            # Pillow thiếu / ảnh động / giải mã hỏng — không cứu được.
            logger.info(f"[zalo-oa] không nén được ảnh ({out.reason})")
            break
        best = out.data
        if len(best) <= target_bytes:
            return best, filename, True

    # PNG ảnh chụp màn hình nhiều khi vẫn quá cỡ ở 640px vì bảng màu lớn —
    # đổi sang JPEG là hạ được vài lần nữa.
    if ext in ("png", "webp"):
        jpeg = _reencode_jpeg(best, target_bytes)
        if jpeg is not None:
            new_name = re.sub(r"\.[^.]+$", ".jpg", filename) or "image.jpg"
            return jpeg, new_name, len(jpeg) <= target_bytes
    return best, filename, len(best) <= target_bytes


def _reencode_jpeg(data: bytes, target_bytes: int) -> Optional[bytes]:
    try:
        import io as _io

        from PIL import Image  # type: ignore
    except Exception:
        return None
    try:
        with Image.open(_io.BytesIO(data)) as im:
            im = im.convert("RGB")
            for quality in (80, 70, 60, 50):
                buf = _io.BytesIO()
                im.save(buf, format="JPEG", quality=quality, optimize=True)
                out = buf.getvalue()
                if len(out) <= target_bytes:
                    return out
            return out
    except Exception as e:
        logger.info(f"[zalo-oa] re-encode JPEG lỗi: {e}")
        return None


def download_to(
    url: str, dest_dir: Path, *, filename_hint: str = "", max_bytes: int = INBOUND_MAX_BYTES
) -> Optional[Path]:
    """Tải media inbound về ``dest_dir``, cắt ở ``max_bytes``. None nếu hỏng.

    Đọc theo khối và dừng khi vượt trần — Content-Length có thể nói dối hoặc
    vắng mặt, không tin được.
    """
    if not str(url).lower().startswith(("http://", "https://")):
        return None
    name = safe_filename(filename_hint or url.split("?")[0].split("/")[-1], "media")
    if "." not in name:
        name += ".bin"
    dest_dir = Path(dest_dir)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        out_path = dest_dir / f"{uuid.uuid4().hex[:12]}_{name}"
        req = urllib.request.Request(url, headers={"User-Agent": "hermes-zalo-oa/1.0"})
        with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT_S) as r:
            total = 0
            with open(out_path, "wb") as f:
                while True:
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        f.close()
                        out_path.unlink(missing_ok=True)
                        logger.warning(
                            f"[zalo-oa] media inbound vượt trần {max_bytes} bytes — bỏ qua"
                        )
                        return None
                    f.write(chunk)
        return out_path
    except (urllib.error.URLError, OSError, ValueError) as e:
        logger.warning(f"[zalo-oa] tải media inbound lỗi: {e}")
        return None
