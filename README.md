# Hermes Zalo OA Plugin (Official Account)

Plugin nối **Zalo Official Account** vào [Hermes Agent](https://github.com/) qua **Open API chính thức** của Zalo: tin đến bằng **webhook**, tin đi bằng `POST /v3.0/oa/message/cs`.

Vì chạy trên Open API chính thức, kênh này hoàn toàn hợp lệ: không QR, không proxy dân cư, không sợ mất số. Đổi lại, OA **không được nhắn tự do**.

---

## Luật gửi tin của OA — đọc trước khi cài

| | |
|---|---|
| Trong **48 giờ** kể từ tương tác cuối của khách | tin Tư vấn **miễn phí** |
| Ngoài 48 giờ, còn trong **7 ngày** | vẫn gửi được nhưng **Zalo tính phí** |
| Quá **7 ngày** | OpenAPI **không gửi được** (lỗi `-230`/`-232`) |
| Khung **22h–6h** | Zalo chặn gửi (lỗi `-234`) |

"Tương tác" gồm: nhắn tin cho OA, quan tâm OA, gọi thoại tới OA, bình luận bài viết, bấm menu/CTA/widget.

Vì vậy plugin có **sổ theo dõi cửa sổ** (`consultation_window.json`) và **mặc định CHẶN mọi tin ngoài khung 48h** — không ai muốn phát hiện mình đốt tiền lúc nhận hoá đơn. Chấp nhận trả phí thì bật:

```bash
ZALO_OA_ALLOW_PAID_WINDOW=true
```

Hệ quả cho các tính năng chủ động (nhắc lịch, follow-up, cron): khách im quá 7 ngày là **không** với tới được nữa, kể cả chịu trả tiền. Muốn nhắn ngoài luồng đó phải dùng ZNS Template Message (đăng ký + duyệt + trả phí) — plugin này **chưa** làm.

---

## Không có gì (so với plugin Zalo cá nhân)

Kết bạn / quét nhóm / nhắn người lạ · nhóm chat thường (OA chỉ có nhóm GMF) · "đang soạn tin" · thả cảm xúc · phễu marketing. Đây là giới hạn của nền tảng OA, không phải thiếu sót của plugin.

---

## Yêu cầu

- **Hermes Agent** đang chạy (plugin nền tảng — `kind: platform`).
- **App trên [developers.zalo.me](https://developers.zalo.me)** đã liên kết Official Account.
- **Domain HTTPS công khai** trỏ về máy chạy Hermes (Zalo gọi webhook vào — tunnel tạm không dùng cho production được).
- Python 3.9+ và (khuyến nghị) **Pillow** — thiếu Pillow thì ảnh > 1MB không nén được và Zalo sẽ từ chối.

---

## Cài đặt

1. **Chép plugin** vào thư mục plugins của Hermes, ví dụ `/opt/data/plugins/zalo-oa/`.

2. **Khai báo env** (xem `.env.example`). Chú ý **hai bí mật khác nhau**:
   - `ZALO_OA_APP_SECRET` — App Secret, dùng **ký OAuth**;
   - `ZALO_OA_SECRET_KEY` — OA Secret Key, dùng **xác thực chữ ký webhook**.

   Hoán đổi hai cái này là triệu chứng "OAuth ok nhưng webhook luôn 401" (hoặc ngược lại).

3. **Reverse proxy** cho cổng webhook (mặc định `127.0.0.1:3939`):

   ```nginx
   location /webhooks/zalo-oa { proxy_pass http://127.0.0.1:3939; }
   location /oauth/           { proxy_pass http://127.0.0.1:3939; }
   ```

   Plugin **không tự làm TLS** — chỉ nghe HTTP trên loopback, TLS do proxy lo.

4. **Khai báo webhook** trên developers.zalo.me: `https://<domain>/webhooks/zalo-oa`, bật các sự kiện `user_send_*`, `follow`, `unfollow`.

5. **Cấp quyền OAuth**: mở `https://<domain>/oauth/start` bằng trình duyệt, đăng nhập tài khoản quản trị OA, xác nhận. Token được lưu vào `<session_dir>/oa_tokens.json` và tự làm mới.

6. Kiểm tra: `curl https://<domain>/health` → `{"status":"ok","oa_id":"..."}`.

---

## Lệnh của chủ OA

Đặt `ZALO_OA_OWNER_UID` = `user_id` của bạn khi nhắn vào chính OA (xem log `[zalo-oa] webhook` hoặc `/bot status`). Sau đó nhắn cho OA:

```
/bot status              — trạng thái kết nối, hạn token, webhook, cờ tin mất phí
/bot window <user_id>    — cửa sổ 48h/7 ngày của một khách cụ thể
/bot baotri <câu>        — BẬT bảo trì, khách nhắn tới nhận câu này (1 lần / 15 phút)
/bot baotri off          — TẮT bảo trì
```

Câu bảo trì mặc định không gắn tên riêng; muốn đổi giọng theo persona thì tạo `<session_dir>/oa_persona.json`:

```json
{ "notices": { "maintenance": "Dạ shop đang bảo trì, tí nữa em rep liền ạ" } }
```

---

## Sổ tay lỗi

| Mã | Nghĩa | Làm gì |
|---|---|---|
| `-32` | rate limit | tự thử lại (3 lần, backoff) |
| `-100` | `attachment_id` hết hạn | tự upload lại ở lần thử sau |
| `-213` | khách chưa quan tâm OA | không gửi được, chờ khách tương tác |
| `-230` / `-232` | quá 7 ngày không tương tác | chỉ còn đường ZNS |
| `-234` | khung giờ đêm 22h–6h | đợi sáng |
| `-244` | khách chặn loại tin này | tôn trọng, đừng gửi tiếp |

Webhook luôn 401 → sai `ZALO_OA_SECRET_KEY`, hoặc có tầng nào đó (proxy/WAF) **sửa body** trước khi tới plugin: chữ ký ký trên **raw body**, đổi một khoảng trắng là hỏng.

Mất quyền sau một lần refresh lỗi → refresh token **dùng một lần**; xem `<session_dir>/oa_tokens.prev.json` rồi mở lại `/oauth/start`.

---

## Cấu trúc

```
adapter.py       — adapter Hermes: vòng đời, tin đến/đi, lệnh owner
oa_client.py     — OAuth + Open API (gửi tin, upload ảnh/file, profile)
oa_webhook.py    — HTTP server, xác thực chữ ký, bóc sự kiện
oa_window.py     — sổ cửa sổ tư vấn 48h/7 ngày
oa_media.py      — nén ảnh dưới trần ~1MB, tải media inbound có chặn dung lượng
outbound_scrub.py — chặn rò rỉ vận hành/tên model ra khách, markdown → plain
message_filtering.py, image_resize.py  — dùng chung với plugin Zalo cá nhân
```

Test: `python3 -m unittest discover -s tests -t .` (không cần mạng, không cần Zalo).

---

## Ghi công

Danh sách endpoint, công thức chữ ký webhook và bảng mã lỗi được đối chiếu với [`diendh/zca-bridge`](https://github.com/diendh/zca-bridge) (Apache-2.0), thư mục `src/zalo-oa` — tài liệu chính thức của Zalo không liệt kê đủ phần mã lỗi.

Quy tắc gửi tin lấy từ [tài liệu vận hành Zalo OA](https://oa.zalo.me/home/documents/guides/tong-quan-cac-loai-tin-nhan-tren-zalo-official-account-_3651713298729094511) (bản hiệu lực 01/01/2026).
