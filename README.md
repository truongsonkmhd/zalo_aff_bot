# Bot Đổi Link Affiliate Zalo

Khách gửi link sản phẩm Shopee vào Zalo Official Account của shop; bot kiểm tra link, đổi sang link affiliate của shop và gửi lại. Thiết kế đầy đủ: https://claude.ai/artifact/UsLbPT8rwghr8irCJ7nGQ5

```
Khách → Zalo OA → [cổng 8090] Webhook (chữ ký, chống phát lại, chống trùng, hạn mức)
      → Hàng đợi → Worker: URL Guard (lọc miền, chống SSRF, chuẩn hóa)
      → Adapter (Shopee Open API | Phương án 2) → Soạn tin cố định → Zalo OA → Khách
Chủ shop → [cổng 8091, chỉ nội bộ] Trang quản trị
```

## Cấu trúc

| Đường dẫn | Nội dung |
|---|---|
| `app/web.py` | Webhook Zalo, API quản trị, header bảo mật, tách cổng public/quản trị |
| `app/url_guard.py` | Tách link, danh sách tên miền, mở link rút gọn an toàn, chuẩn hóa |
| `app/adapters.py` | Shopee Open API, Phương án 2 (phiên web), cầu dao, TikTok (chờ giai đoạn 3) |
| `app/pipeline.py` | Worker: cache, thử lại có giãn cách, hàng lỗi, ghi nhật ký |
| `app/zalo_client.py` | Gửi tin Zalo OA, xoay vòng token (khóa phân tán) |
| `app/security.py` | Chữ ký webhook, HMAC mã khách, scrypt, AES-256-GCM |
| `app/secret_store.py` | Token Zalo và cookie Phương án 2 lưu mã hóa trên đĩa |
| `app/audit.py` | Nhật ký ẩn danh (SQLite), thống kê, tự xóa sau 90 ngày |
| `admin_ui/` | Giao diện quản trị: HTML/CSS/JS thuần, không thư viện ngoài, tuân thủ CSP (đặc tả: `docs/UI_HANDOFF_GEMINI.md`) |
| `public_ui/` | Trang tra đơn cho khách (cổng public) |
| `chat_ui/`, `app/chatsim.py` | Khung chat thử bot giống Zalo (chỉ để demo, Zalo/Shopee giả lập) |
| `app/orders.py` | Mã tra cứu, kho đơn hàng, đọc báo cáo Open API và file CSV |
| `app/shopee_browser.py` | Phương án 2 tự động: mở Chrome riêng, đọc phiên affiliate.shopee.vn qua DevTools |
| `tests/` | 112 test, gồm các tình huống tấn công |

## Chạy thử trên máy

```bash
cd zalo_aff_bot
pip install -r requirements.txt
python -m unittest discover -s tests -t .      # chạy test
python -m app.simulate                         # mô phỏng hội thoại khách ↔ bot (Zalo/Shopee giả lập)
python -m app.devserver                        # trang quản trị demo: http://127.0.0.1:8091/admin/
python -m app.chatsim                          # khung chat thử bot trên trình duyệt: http://127.0.0.1:8093/
```

Chạy với Zalo OA thật (một tiến trình, không cần Redis):

1. `cp .env.example .env`, điền thông tin Zalo OA và Shopee.
2. `python -m app.cli gen-secrets` → dán `DATA_KEY`, `USER_HASH_KEY`, `WEBHOOK_PATH_SECRET` vào `.env`.
3. `python -m app.cli hash-password` → dán `ADMIN_PASSWORD_HASH`.
   `python -m app.cli zalo-token` → cấp quyền OA, dán `ZALO_OA_ID`, `ZALO_REFRESH_TOKEN`.
4. Nạp biến môi trường từ `.env` rồi `ADMIN_PORT=8091 python -m app.main`.
5. Đưa **riêng cổng 8090** ra Internet bằng HTTPS (Cloudflare Tunnel: `cloudflared tunnel --url http://127.0.0.1:8090`).
6. Trên developers.zalo.me, khai báo webhook `https://<tên-miền>/webhook/zalo/<WEBHOOK_PATH_SECRET>`, bật sự kiện `user_send_text` và `user_send_link`.

## Chạy thật (Docker)

```bash
mkdir -p secrets && chmod 700 secrets
# mỗi bí mật một file: zalo_oa_secret, zalo_app_secret, zalo_refresh_token, shopee_app_secret,
# data_key, user_hash_key, webhook_path_secret, admin_password_hash, redis_password, redis_url
chmod 600 secrets/*
docker compose up -d --build
ssh -L 8091:127.0.0.1:8091 <máy-chủ>     # mở trang quản trị: http://127.0.0.1:8091/admin/
```

`redis_url` có dạng `redis://:<redis_password>@redis:6379/0`.

## Khách tra cứu đơn hàng (3 bước)

1. Khách nhắn **/id** cho OA → bot gửi **mã tra cứu** 10 ký tự và link trang tra đơn.
2. Khách mở trang tra đơn (`https://<tên-miền>/tra-don/`, cùng cổng public với webhook).
3. Nhập mã tra cứu (xem mọi đơn) hoặc mã đơn hàng Shopee (xem một đơn) → bấm Tra cứu.

Thiết kế an toàn hơn cách "tra bằng UserID Zalo": mã tra cứu sinh từ ID Zalo bằng HMAC với khóa bí mật, không đoán được và không lộ ID thật. Mã được gắn vào `sub_id` (dạng `KH<mã>`) của mọi link gửi cho khách, nên đơn trong báo cáo Shopee tự khớp về đúng khách. Trang tra đơn không trả hoa hồng gốc của shop; chỉ hiện "tiền hoàn dự kiến" khi đặt `CASHBACK_PERCENT`. Hạn mức tra cứu theo IP chống dò mã.

Nguồn đơn hàng:
- **Open API:** tự đồng bộ báo cáo chuyển đổi 30 phút/lần (trang quản trị có nút Đồng bộ ngay).
- **Phương án 2:** vào affiliate.shopee.vn xuất CSV báo cáo chuyển đổi, rồi tải lên ở trang quản trị → Đơn hàng. Bộ đọc nhận cả tiêu đề tiếng Việt và tiếng Anh.

Thử ngay: `python -m app.devserver --zalo-id <ID Zalo của bạn>` → in ra mã tra cứu demo và tạo sẵn đơn mẫu cho mã đó.

## Shopee: Phương án 1 và 2

- **Phương án 1 (ưu tiên):** đặt `SHOPEE_APP_ID` + `SHOPEE_APP_SECRET` (Shopee Affiliate Open API). Khi có hai biến này, Phương án 2 tự tắt.
- **Phương án 2 (tạm thời):** `SHOPEE_WEB_FALLBACK=1`, không có Open API. Chủ shop dán cookie affiliate.shopee.vn qua trang quản trị. Bot:
  - lưu cookie mã hóa, không bao giờ trả lại cookie qua API, không ghi vào nhật ký;
  - chỉ gửi cookie và `csrf-token` như phiên đăng nhập bình thường, **không** chép header chống bot, không giả trình duyệt, không tự đăng nhập, không giải captcha;
  - tối đa 20 link/phút; gặp 401/403/trang đăng nhập/captcha là **dừng hẳn** và báo chủ shop (Telegram + trang quản trị) cho tới khi cập nhật cookie mới;
  - nhắc thay cookie khi đã dùng quá 7 ngày.
- **Phương án 2 tự động (`SHOPEE_BROWSER=1`):** không phải dán cookie nữa.
  1. Bot mở một cửa sổ Chrome **riêng** (hồ sơ `data/chrome_shopee`, tách khỏi Chrome cá nhân) tới affiliate.shopee.vn. Mở lại được từ trang quản trị → Kết nối Shopee → “Mở Chrome đăng nhập Shopee”.
  2. Bạn đăng nhập tay trong cửa sổ đó một lần (mật khẩu, OTP, captcha do bạn làm).
  3. Mỗi phút bot đọc cookie + `csrftoken` của phiên qua DevTools (chỉ nghe `127.0.0.1`, cổng ngẫu nhiên) và nạp vào Phương án 2 khi phiên đổi. Shopee từ chối phiên thì bot tải lại trang affiliate một lần rồi đọc lại; vẫn không được thì báo bạn.
  4. Mỗi 6 giờ bot tải lại trang affiliate để phiên không hết hạn. Giữ cửa sổ mở (thu nhỏ được), không đăng xuất.

  Bot không lưu mật khẩu, không tự đăng nhập, không giải captcha, không giấu dấu hiệu tự động hóa và không bấm trên trang để đổi link. Chỉ chạy với `ROLE=all` trên máy có màn hình (không dùng trong Docker). Cổng DevTools cho phép điều khiển phiên Shopee đang đăng nhập, nên chỉ chạy trên máy không có người lạ dùng chung và không cài phần mềm không rõ nguồn gốc.

## Lớp bảo mật đã có

| Mối đe dọa | Biện pháp | Test |
|---|---|---|
| Giả mạo webhook | Đường dẫn bí mật + chữ ký `X-ZEvent-Signature` (so sánh thời gian hằng) + kiểm tra `app_id`/OA nhận | `test_security`, `test_web` |
| Phát lại sự kiện cũ | Lệch quá 5 phút bị từ chối; chống trùng theo `msg_id` 24 giờ | `test_security`, `test_web` |
| SSRF qua link rút gọn | Danh sách tên miền ở **mỗi** bước chuyển hướng, chặn IP literal/cổng lạ/userinfo, DNS phải ra IP công khai, tối đa 5 bước, 3 giây/bước, không mang cookie | `test_url_guard` |
| Chèn mã affiliate người khác | Chuẩn hóa về `shopee.vn/product/<shop>/<item>`, bỏ toàn bộ tham số | `test_url_guard` |
| Bot phát tán link lừa đảo | Chỉ gửi link có tên miền rút gọn chính thức của sàn, mẫu tin cố định, không lặp nội dung khách | `test_pipeline` |
| Spam, cạn hạn mức API | Hạn mức theo khách (phút/ngày) và toàn hệ thống, hàng đợi có giới hạn, cache 24 giờ, cầu dao | `test_web`, `test_adapters` |
| Lộ bí mật | Đọc từ Docker secrets, không in ra (`repr=False`), token/cookie mã hóa AES-256-GCM có gắn mục đích | `test_config`, `test_security`, `test_zalo` |
| Lộ dữ liệu khách | Chỉ lưu HMAC mã khách, không lưu nội dung tin; hàng lỗi không giữ ID Zalo; tự xóa sau 90 ngày | `test_pipeline` |
| Chiếm trang quản trị | Cổng riêng không ra Internet, lọc IP, scrypt, khóa 15 phút sau 5 lần sai, cookie HttpOnly + SameSite=Strict, CSRF, CSP nghiêm, nhật ký thao tác | `test_web` |
| Refresh token Zalo dùng hai lần | Khóa phân tán, ghi token mới ngay | `test_zalo` |
| Container bị khai thác | User thường, file hệ thống chỉ đọc, bỏ mọi capability, Redis ở mạng nội bộ không ra Internet | `docker-compose.yml` |

## Việc bạn cần tự làm

- Đối chiếu lại với tài liệu mới nhất: công thức chữ ký webhook Zalo, endpoint gửi tin/làm mới token, mã lỗi `-216`, endpoint và chữ ký Shopee Open API. Mã nguồn để chúng ở một chỗ (`security.py`, `zalo_client.py`, `adapters.py`) để dễ sửa.
- Đăng ký Shopee Affiliate Open API để bỏ Phương án 2.
- Cấu hình tường lửa chỉ cho máy chủ đi ra `openapi.zalo.me`, `oauth.zaloapp.com`, `open-api.affiliate.shopee.vn`, `affiliate.shopee.vn`, các tên miền Shopee/TikTok trong `url_guard.py` và `api.telegram.org`.
- Không đưa nick Zalo cá nhân vào bot; chỉ dùng Official Account.
