"""Cầu ảnh OA → landing (``oa_landing_bridge`` + tool trong ``oa_tools``).

Trọng tâm là những chỗ hỏng ÂM THẦM — trang vẫn báo "cập nhật thành công" mà
khách vào xem thấy ảnh vỡ:

* ``X-Session`` sai dạng → sang hội thoại khác → mất phiên OTP, trang có chủ bị
  từ chối. Đây là lỗi đắt nhất vì nhìn log upload vẫn thấy "gửi đi rồi".
* nhận URL không bền (``/media/...``) tưởng là xong.
* để lọt base64 / đường dẫn cục bộ ra kết quả cho model.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import oa_landing_bridge as B  # noqa: E402
import oa_tools  # noqa: E402

_JPEG = b"\xff\xd8\xff" + b"\x00" * 64
_ENV = {
    "TINO_LANDING_BRIDGE_URL": "https://mcp.example/tools/landing_upload_image",
    "TINO_LANDING_BRIDGE_KEY": "k-test",
}


class _Rec:
    def __init__(self, path):
        self.local_path = str(path)


def _bridge(media_dir, *, recents, post, chat="123", conv="zalo-oa:123"):
    cfg = B.load_bridge_config(dict(_ENV), str(media_dir))
    return B.OaLandingBridge(
        cfg,
        recent_fn=lambda c, n: recents[-n:] if c == chat else [],
        session_resolver=lambda t: {"chat_id": chat, "conv_id": conv} if t else None,
        http_post=post,
    )


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.img = Path(self.dir) / "anh1.jpg"
        self.img.write_bytes(_JPEG)
        self.calls = []

    def _post_ok(self, url, headers, body):
        self.calls.append((url, headers, body))
        return {"image_ref": "asset://up-abc123", "image_url": "https://builder/a/up-abc123"}

    def test_gui_x_session_dang_zalo_oa(self):
        """Chuỗi hội thoại phải khớp CHÍNH XÁC cái Hermes chèn cho tool MCP."""
        br = _bridge(self.dir, recents=[_Rec(self.img)], post=self._post_ok)
        br.upload_recent(task_id="sess-1", slug="mimishop")
        _url, headers, _body = self.calls[0]
        self.assertEqual(headers["X-Session"], "zalo-oa:123")
        self.assertEqual(headers["X-Agent-Key"], "k-test")

    def test_gui_conv_token_de_chung_minh_phien_dang_nhap(self):
        """Thiếu X-Conv-Token thì MCP coi như khách CHƯA đăng nhập.

        Đã trả giá 10/09 16:10-16:13: khách xác thực xong (client_id 60989) mà
        ba lần upload liền sau vẫn bị "Trang này thuộc tài khoản khác".
        """
        br = _bridge(self.dir, recents=[_Rec(self.img)], post=self._post_ok)
        br.upload_recent(task_id="sess-1", slug="s", conv_token="tok-abc")
        self.assertEqual(self.calls[0][1]["X-Conv-Token"], "tok-abc")

    def test_khong_co_conv_token_thi_khong_gui_header_rong(self):
        br = _bridge(self.dir, recents=[_Rec(self.img)], post=self._post_ok)
        br.upload_recent(task_id="sess-1", slug="s")
        self.assertNotIn("X-Conv-Token", self.calls[0][1])

    def test_ket_qua_khong_lo_base64_hay_duong_dan(self):
        br = _bridge(self.dir, recents=[_Rec(self.img)], post=self._post_ok)
        out = br.upload_recent(task_id="sess-1", slug="mimishop")
        blob = json.dumps(out, ensure_ascii=False)
        self.assertNotIn("base64", blob)
        self.assertNotIn(self.dir, blob)
        self.assertNotIn("local_path", blob)
        self.assertEqual(out["images"][0]["image_ref"], "asset://up-abc123")

    def test_ten_file_theo_noi_dung_nen_gui_lai_khong_de_len_anh_khac(self):
        br = _bridge(self.dir, recents=[_Rec(self.img)], post=self._post_ok)
        a = br.upload_recent(task_id="sess-1", slug="s")["images"][0]["filename"]
        b = br.upload_recent(task_id="sess-1", slug="s")["images"][0]["filename"]
        self.assertEqual(a, b)
        self.assertTrue(a.endswith(".jpg"))

    def test_tu_choi_url_khong_ben(self):
        def post(url, headers, body):
            return {"image_url": "https://mcp/media/tam-thoi.jpg"}
        br = _bridge(self.dir, recents=[_Rec(self.img)], post=post)
        with self.assertRaises(B.BridgeError) as e:
            br.upload_recent(task_id="sess-1", slug="s")
        self.assertIn("non-durable", str(e.exception))

    def test_url_assets_he_cu_van_duoc_coi_la_ben(self):
        def post(url, headers, body):
            return {"image_url": "https://landing/s/assets/anh.jpg"}
        br = _bridge(self.dir, recents=[_Rec(self.img)], post=post)
        out = br.upload_recent(task_id="sess-1", slug="s")
        self.assertNotIn("image_ref", out["images"][0])

    def test_loi_cua_mcp_duoc_neu_lai_cho_agent(self):
        def post(url, headers, body):
            return {"error": "not_found", "message": "không tìm thấy landing: sai-slug"}
        br = _bridge(self.dir, recents=[_Rec(self.img)], post=post)
        with self.assertRaises(B.BridgeError) as e:
            br.upload_recent(task_id="sess-1", slug="sai-slug")
        self.assertIn("sai-slug", str(e.exception))

    def test_thieu_slug_va_khong_co_anh(self):
        br = _bridge(self.dir, recents=[_Rec(self.img)], post=self._post_ok)
        with self.assertRaises(B.BridgeError):
            br.upload_recent(task_id="sess-1", slug="  ")
        br2 = _bridge(self.dir, recents=[], post=self._post_ok)
        with self.assertRaises(B.BridgeError) as e:
            br2.upload_recent(task_id="sess-1", slug="s")
        self.assertIn("no recent image", str(e.exception))

    def test_khong_resolve_duoc_phien_thi_dung(self):
        br = _bridge(self.dir, recents=[_Rec(self.img)], post=self._post_ok)
        with self.assertRaises(B.BridgeError):
            br.upload_recent(task_id="", slug="s")

    def test_chan_duong_dan_ngoai_kho_media(self):
        ngoai = Path(tempfile.mkdtemp()) / "ngoai.jpg"
        ngoai.write_bytes(_JPEG)
        br = _bridge(self.dir, recents=[_Rec(ngoai)], post=self._post_ok)
        with self.assertRaises(B.BridgeError) as e:
            br.upload_recent(task_id="sess-1", slug="s")
        self.assertIn("outside", str(e.exception))

    def test_chan_file_khong_phai_anh(self):
        gia = Path(self.dir) / "gia.jpg"
        gia.write_bytes(b"KHONG PHAI ANH" * 8)
        br = _bridge(self.dir, recents=[_Rec(gia)], post=self._post_ok)
        with self.assertRaises(B.BridgeError):
            br.upload_recent(task_id="sess-1", slug="s")

    def test_thieu_cau_hinh_thi_bao_ro(self):
        with self.assertRaises(B.BridgeError):
            B.load_bridge_config({}, self.dir)
        with self.assertRaises(B.BridgeError):
            B.load_bridge_config({"TINO_LANDING_BRIDGE_URL": "http://mcp/x",
                                  "TINO_LANDING_BRIDGE_KEY": "k"}, self.dir)


class ViTriTest(unittest.TestCase):
    """vi_tri = Ô TRÊN TRANG (từ landing_map), không phải file: kiểm dạng ở cầu, chỉ
    chuyển page/index/path; MCP kiểm ô format:image rồi tự gắn ảnh."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.img = Path(self.dir) / "anh1.jpg"
        self.img.write_bytes(_JPEG)
        self.calls = []

    def _post(self, resp):
        def post(url, headers, body):
            self.calls.append(body)
            return resp
        return post

    def test_chuan_hoa_chi_giu_page_index_path(self):
        self.assertIsNone(B.normalize_vi_tri(None))
        self.assertEqual(B.normalize_vi_tri({"index": 2, "la": 1}), {"page": 0, "index": 2})
        self.assertEqual(B.normalize_vi_tri('{"page": 1, "index": 3, "path": "items[0].image"}'),
                         {"page": 1, "index": 3, "path": "items[0].image"})
        for xau in ({"page": 0}, {"index": -2}, {"index": 1, "path": "/opt/data/zalo-oa/x.jpg"},
                    "khong-phai-json", 5):
            with self.assertRaises(B.BridgeError):
                B.normalize_vi_tri(xau)

    def test_gui_vi_tri_va_tra_ket_qua_gan(self):
        br = _bridge(self.dir, recents=[_Rec(self.img)], post=self._post(
            {"image_ref": "asset://up-1", "image_url": "https://builder/a/up-1",
             "gan_vao_trang": True, "url": "https://builder/mimishop",
             "vi_tri": {"page": 0, "index": 2, "path": "image"}}))
        out = br.upload_recent(task_id="sess-1", slug="mimishop", vi_tri={"index": 2})
        self.assertEqual(self.calls[0]["vi_tri"], {"page": 0, "index": 2})
        self.assertTrue(out["gan_vao_trang"])
        self.assertEqual(out["url"], "https://builder/mimishop")

    def test_khong_vi_tri_than_request_nhu_cu(self):
        br = _bridge(self.dir, recents=[_Rec(self.img)], post=self._post(
            {"image_ref": "asset://up-1", "image_url": "https://builder/a/up-1"}))
        out = br.upload_recent(task_id="sess-1", slug="s")
        self.assertNotIn("vi_tri", self.calls[0])
        self.assertNotIn("gan_vao_trang", out)

    def test_vi_tri_nhieu_anh_bi_chan_truoc_khi_gui(self):
        br = _bridge(self.dir, recents=[_Rec(self.img), _Rec(self.img)], post=self._post({}))
        with self.assertRaises(B.BridgeError):
            br.upload_recent(task_id="sess-1", slug="s", count=2, vi_tri={"index": 2})
        self.assertEqual(self.calls, [])

    def test_muc_nhieu_o_anh_tra_danh_sach_cho_agent(self):
        br = _bridge(self.dir, recents=[_Rec(self.img)], post=self._post(
            {"error": "invalid_parameter", "code": "CHON_O_ANH", "message": "Mục này có 2 ô ảnh",
             "o_anh": [{"path": "items.0.image", "label": "Ảnh"},
                       {"path": "items.1.image", "label": "Ảnh"}],
             "next_step": "Hỏi khách thay ảnh thứ mấy"}))
        with self.assertRaises(B.BridgeError) as cm:
            br.upload_recent(task_id="sess-1", slug="s", vi_tri={"index": 3})
        self.assertEqual(cm.exception.details["code"], "CHON_O_ANH")
        self.assertEqual(len(cm.exception.details["o_anh"]), 2)


class _FakeAdapter:
    def __init__(self, media_dir, images):
        self.media_dir = Path(media_dir)
        self._images = images

    def recent_images(self, chat_id, count=1):
        return self._images[-count:]


class ToolTest(unittest.TestCase):
    """Tầng tool: phải trả CHUỖI JSON và phải dặn agent đặt ảnh vào đâu."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        img = Path(self.dir) / "a.jpg"
        img.write_bytes(_JPEG)
        oa_tools.set_live_adapter(_FakeAdapter(self.dir, [_Rec(img)]))
        self.addCleanup(oa_tools.clear_live_adapter)

    def test_tra_chuoi_json_va_nhac_dung_image_ref(self):
        with mock.patch.dict(os.environ, _ENV, clear=False), \
             mock.patch.object(oa_tools, "resolve_chat_id_from_task", return_value="123"), \
             mock.patch.object(oa_tools, "_bridge_http_post",
                               return_value={"image_ref": "asset://up-1",
                                             "image_url": "https://builder/a/up-1"}):
            raw = oa_tools.handle_upload_recent_image_to_landing(
                {"slug": "mimishop", "task_id": "sess-1"})
        self.assertIsInstance(raw, str)
        out = json.loads(raw)
        self.assertTrue(out["success"])
        self.assertIn("asset://up-1", out["hint"])
        self.assertIn("landing_update", out["hint"])

    def test_chua_ket_noi_thi_bao_loi_chu_khong_no(self):
        oa_tools.clear_live_adapter()
        out = json.loads(oa_tools.handle_upload_recent_image_to_landing({"slug": "s"}))
        self.assertFalse(out["success"])

    def test_thieu_cau_hinh_thi_khong_bat_khach_gui_lai_anh(self):
        with mock.patch.dict(os.environ, {"TINO_LANDING_BRIDGE_URL": "",
                                          "TINO_LANDING_BRIDGE_KEY": ""}, clear=False), \
             mock.patch.object(oa_tools, "resolve_chat_id_from_task", return_value="123"):
            out = json.loads(oa_tools.handle_upload_recent_image_to_landing(
                {"slug": "s", "task_id": "sess-1"}))
        self.assertFalse(out["success"])
        self.assertIn("kỹ thuật", out["hint"])

    def test_tool_chuyen_tiep_conv_token_cua_model(self):
        seen = {}
        def fake_post(url, headers, body):
            seen.update(headers)
            return {"image_ref": "asset://up-1", "image_url": "https://builder/a/up-1"}
        with mock.patch.dict(os.environ, _ENV, clear=False), \
             mock.patch.object(oa_tools, "resolve_chat_id_from_task", return_value="123"), \
             mock.patch.object(oa_tools, "_bridge_http_post", side_effect=fake_post):
            oa_tools.handle_upload_recent_image_to_landing(
                {"slug": "s", "task_id": "sess-1", "conv_token": "tok-xyz"})
        self.assertEqual(seen.get("X-Conv-Token"), "tok-xyz")
        self.assertEqual(seen.get("X-Session"), "zalo-oa:123")

    def test_bi_tu_choi_vi_trang_co_chu_thi_nhac_gui_conv_token(self):
        with mock.patch.dict(os.environ, _ENV, clear=False), \
             mock.patch.object(oa_tools, "resolve_chat_id_from_task", return_value="123"), \
             mock.patch.object(oa_tools, "_bridge_http_post",
                               return_value={"error": "forbidden",
                                             "message": "Trang này thuộc tài khoản khác."}):
            out = json.loads(oa_tools.handle_upload_recent_image_to_landing(
                {"slug": "s", "task_id": "sess-1"}))
        self.assertFalse(out["success"])
        self.assertIn("conv_token", out["hint"])

    def test_tool_chuyen_vi_tri_va_bao_da_gan(self):
        seen = {}
        def fake_post(url, headers, body):
            seen.update(body)
            return {"image_ref": "asset://up-1", "image_url": "https://builder/a/up-1",
                    "gan_vao_trang": True, "url": "https://builder/mimishop"}
        with mock.patch.dict(os.environ, _ENV, clear=False), \
             mock.patch.object(oa_tools, "resolve_chat_id_from_task", return_value="123"), \
             mock.patch.object(oa_tools, "_bridge_http_post", side_effect=fake_post):
            out = json.loads(oa_tools.handle_upload_recent_image_to_landing(
                {"slug": "mimishop", "task_id": "sess-1",
                 "vi_tri": {"page": 0, "index": 2, "path": "image"}}))
        self.assertEqual(seen["vi_tri"], {"page": 0, "index": 2, "path": "image"})
        self.assertTrue(out["success"] and out["gan_vao_trang"])
        self.assertIn("đã gắn", out["hint"])

    def test_tool_o_anh_chua_ro_tra_lua_chon(self):
        with mock.patch.dict(os.environ, _ENV, clear=False), \
             mock.patch.object(oa_tools, "resolve_chat_id_from_task", return_value="123"), \
             mock.patch.object(oa_tools, "_bridge_http_post",
                               return_value={"error": "invalid_parameter", "code": "CHON_O_ANH",
                                             "message": "Mục này có 2 ô ảnh",
                                             "o_anh": [{"path": "items.0.image"},
                                                       {"path": "items.1.image"}],
                                             "next_step": "Hỏi khách thay ảnh thứ mấy"}):
            out = json.loads(oa_tools.handle_upload_recent_image_to_landing(
                {"slug": "s", "task_id": "sess-1", "vi_tri": {"index": 3}}))
        self.assertFalse(out["success"])
        self.assertEqual(out["code"], "CHON_O_ANH")
        self.assertEqual([o["path"] for o in out["o_anh"]], ["items.0.image", "items.1.image"])
        self.assertIn("thay ảnh thứ mấy", out["hint"])

    def test_cau_khong_sua_duoc_chu_bao_ro(self):
        with mock.patch.dict(os.environ, _ENV, clear=False), \
             mock.patch.object(oa_tools, "resolve_chat_id_from_task", return_value="123"), \
             mock.patch.object(oa_tools, "_bridge_http_post",
                               return_value={"error": "forbidden",
                                             "message": "Cầu ảnh chỉ được gắn ảnh vào ô ảnh"}):
            out = json.loads(oa_tools.handle_upload_recent_image_to_landing(
                {"slug": "s", "task_id": "sess-1", "vi_tri": {"index": 2, "path": "heading"}}))
        self.assertFalse(out["success"])
        self.assertIn("landing_map", out["hint"])

    def test_conv_id_lay_tu_task_id(self):
        with mock.patch.object(oa_tools, "resolve_chat_id_from_task", return_value="777"):
            self.assertEqual(oa_tools._bridge_session_resolver("sess-1"),
                             {"chat_id": "777", "conv_id": "zalo-oa:777"})
        with mock.patch.object(oa_tools, "resolve_chat_id_from_task", return_value=""):
            self.assertIsNone(oa_tools._bridge_session_resolver("sess-1"))


if __name__ == "__main__":
    unittest.main()
