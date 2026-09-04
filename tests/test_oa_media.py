"""Media OA: nén ảnh dưới trần ~1MB, tải media inbound có chặn dung lượng."""

import io as _io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import oa_media  # noqa: E402

try:
    from PIL import Image  # type: ignore

    _HAS_PIL = True
except Exception:  # pragma: no cover
    _HAS_PIL = False


class NameTest(unittest.TestCase):
    def test_ext_and_content_type(self):
        self.assertEqual(oa_media.ext_of("a.PNG"), "png")
        self.assertEqual(oa_media.content_type_for("a.PNG"), "image/png")
        self.assertEqual(oa_media.content_type_for("bao-gia.pdf"), "application/pdf")
        self.assertEqual(oa_media.content_type_for("x.unknownext"), "application/octet-stream")

    def test_is_image_name(self):
        self.assertTrue(oa_media.is_image_name("a.jpeg"))
        self.assertFalse(oa_media.is_image_name("a.pdf"))
        self.assertFalse(oa_media.is_image_name("noext"))

    def test_safe_filename_strips_paths_and_junk(self):
        # Tên file đến từ Zalo/URL là dữ liệu không tin được.
        self.assertEqual(oa_media.safe_filename("../../etc/passwd"), "passwd")
        self.assertEqual(oa_media.safe_filename("a b*c?.png"), "a_b_c_.png")
        self.assertEqual(oa_media.safe_filename(""), "file")
        self.assertEqual(oa_media.safe_filename("...."), "file")
        self.assertLessEqual(len(oa_media.safe_filename("x" * 500 + ".png")), 120)


class CompressTest(unittest.TestCase):
    def test_small_image_passes_through_untouched(self):
        data = b"x" * 100
        out, name, fits = oa_media.compress_image_under(data, "a.jpg")
        self.assertTrue(fits)
        self.assertIs(out, data)
        self.assertEqual(name, "a.jpg")

    def test_oversized_gif_reported_as_not_fitting(self):
        # GIF không nén lại được (mất animation) → caller phải gửi link.
        out, name, fits = oa_media.compress_image_under(b"x" * 2_000_000, "a.gif", 900_000)
        self.assertFalse(fits)
        self.assertEqual(name, "a.gif")

    def test_undecodable_bytes_fail_open(self):
        out, name, fits = oa_media.compress_image_under(b"x" * 2_000_000, "a.jpg", 900_000)
        self.assertFalse(fits)          # không cứu được, nhưng…
        self.assertEqual(out, b"x" * 2_000_000)  # …không ném lỗi, không mất data

    @unittest.skipUnless(_HAS_PIL, "cần Pillow")
    def test_big_photo_gets_under_target(self):
        buf = _io.BytesIO()
        # Nhiễu ngẫu nhiên: ảnh trơn nén quá tốt, không kiểm được gì.
        img = Image.frombytes("RGB", (3000, 2000), os.urandom(3000 * 2000 * 3))
        img.save(buf, format="JPEG", quality=95)
        data = buf.getvalue()
        self.assertGreater(len(data), 900_000)
        out, name, fits = oa_media.compress_image_under(data, "photo.jpg", 900_000)
        self.assertTrue(fits)
        self.assertLessEqual(len(out), 900_000)

    @unittest.skipUnless(_HAS_PIL, "cần Pillow")
    def test_huge_png_converts_to_jpeg_when_needed(self):
        buf = _io.BytesIO()
        Image.frombytes("RGB", (2000, 2000), os.urandom(2000 * 2000 * 3)).save(buf, format="PNG")
        out, name, fits = oa_media.compress_image_under(buf.getvalue(), "shot.png", 200_000)
        self.assertTrue(fits)
        self.assertTrue(name.endswith(".jpg"))


class _FakeResponse:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def read(self, n=-1):
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_rejects_non_http_scheme(self):
        self.assertIsNone(oa_media.download_to("file:///etc/passwd", self.dir))
        self.assertIsNone(oa_media.download_to("", self.dir))

    def test_saves_file(self):
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse([b"hello"])):
            path = oa_media.download_to("https://cdn.zalo/a.jpg", self.dir)
        self.assertIsNotNone(path)
        self.assertEqual(path.read_bytes(), b"hello")
        self.assertTrue(path.name.endswith("a.jpg"))

    def test_oversized_download_is_aborted_and_cleaned_up(self):
        chunks = [b"x" * 64_000] * 20
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(chunks)):
            path = oa_media.download_to("https://cdn.zalo/big.bin", self.dir, max_bytes=100_000)
        self.assertIsNone(path)
        self.assertEqual(list(self.dir.iterdir()), [])  # không để lại file dở

    def test_filename_from_url_is_sanitised(self):
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse([b"x"])):
            path = oa_media.download_to(
                "https://cdn.zalo/x?y=1", self.dir, filename_hint="../../evil.sh"
            )
        self.assertTrue(path.name.endswith("evil.sh"))
        self.assertEqual(path.parent, self.dir)

    def test_extensionless_name_gets_bin_suffix(self):
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse([b"x"])):
            path = oa_media.download_to("https://cdn.zalo/blob", self.dir)
        self.assertTrue(path.name.endswith(".bin"))

    def test_network_error_returns_none(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
            self.assertIsNone(oa_media.download_to("https://cdn.zalo/a.jpg", self.dir))


if __name__ == "__main__":
    unittest.main()
