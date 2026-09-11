"""Mô tả tool phải nói rõ tool này CHỈ dùng cho kênh OA.

Tool ``oa_upload_recent_image_to_landing`` được đăng ký vào registry chung của
Hermes, nên model ở chat Zalo **cá nhân** cũng đọc thấy nó. Mô tả đầu tiên chỉ
ghi "Dùng tool này mỗi khi khách gửi ảnh và muốn ảnh lên web" — không nói kênh
nào — nên model chọn nó thay cho ``zalo_upload_recent_image_to_landing`` của
kênh cá nhân, rồi bị hook chặn. Đo trên vnnic-dn 11/09: 71 lượt gọi nhầm trong
ngày, 44 phiên khách không nhận được ảnh.

Hàng rào phía kênh cá nhân nằm ở repo ``hermes-zalo-plugin``
(``goi_y_tool_dung_kenh``); đây là hàng rào phía OA: nói đúng phạm vi ngay
trong mô tả model đọc.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import oa_tools  # noqa: E402


class MoTaToolNeuRoKenh(unittest.TestCase):
    def setUp(self):
        self.mota = oa_tools.UPLOAD_IMAGE_SCHEMA["description"]

    def test_neu_ro_chi_dung_cho_kenh_oa(self):
        self.assertIn("CHỈ DÙNG TRONG HỘI THOẠI ZALO OA", self.mota)

    def test_chi_ten_tool_thay_the_cho_kenh_ca_nhan(self):
        self.assertIn("zalo_upload_recent_image_to_landing", self.mota)

    def test_khong_con_cau_moi_goi_o_moi_kenh(self):
        # Câu này là thứ kéo model sang tool sai kênh.
        self.assertNotIn("Dùng tool này mỗi khi khách gửi ảnh", self.mota)

    def test_van_giu_canh_bao_khong_dung_duong_dan_may_chu(self):
        # Bài học ca MimiShop 10/09: model bê path /opt/data/... vào landing.
        self.assertIn("TUYỆT ĐỐI KHÔNG lấy đường dẫn ảnh trên máy chủ", self.mota)

    def test_moi_tool_oa_deu_xung_ten_kenh(self):
        for ten, schema, *_ in oa_tools._TOOLS:
            self.assertRegex(
                schema["description"], r"(?i)(zalo official account|zalo oa)",
                f"mô tả {ten} không nói rõ kênh OA",
            )


if __name__ == "__main__":
    unittest.main()
