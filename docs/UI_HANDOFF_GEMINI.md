# Bàn giao: Giao diện quản trị Bot Đổi Link Affiliate

Tài liệu này dành cho người (hoặc AI) xây dựng giao diện quản trị. Backend, API và toàn bộ lớp bảo mật đã xong và có test; việc còn lại là **giao diện**: trực quan, dễ dùng, và không làm yếu đi các lớp bảo mật đã có.

## 1. Bối cảnh

Bot nhận link sản phẩm Shopee mà khách gửi qua Zalo OA, đổi sang link affiliate của shop và gửi lại. Người dùng giao diện là **chủ shop** (một người, không chuyên kỹ thuật). Họ mở trang quản trị để:

1. Xem bot có đang chạy tốt không (một cái nhìn là biết).
2. Bật/tắt bot khi cần bảo trì.
3. Xem thống kê: bao nhiêu link đã đổi, tỉ lệ thành công, lỗi hay gặp.
4. Khi dùng Phương án 2: dán cookie Shopee mới khi bot báo phiên hết hạn.
5. Xem các việc bị lỗi (hàng lỗi) và nhật ký thao tác quản trị.

Trang quản trị chạy trên cổng nội bộ riêng (mặc định `127.0.0.1:8091`), chủ shop vào qua SSH tunnel hoặc VPN. Trang được phục vụ tại `/admin/`.

## 2. Cách chạy để phát triển

```bash
cd zalo_aff_bot
pip install -r requirements.txt
python -m app.devserver
# mở http://127.0.0.1:8091/admin/   — mật khẩu demo: demo-admin-123456
```

Server demo có sẵn 600 lượt chuyển đổi mẫu trong 30 ngày, 1 việc trong hàng lỗi, 1 cảnh báo, đang ở chế độ Phương án 2. Không gọi Zalo/Shopee thật. Sửa file trong `admin_ui/` rồi tải lại trình duyệt là thấy.

## 3. Ràng buộc bảo mật (bắt buộc)

Server gửi kèm Content-Security-Policy sau cho mọi trang `/admin`:

```
default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:;
font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'
```

Hệ quả và yêu cầu:

| # | Yêu cầu | Lý do |
|---|---|---|
| 1 | Mọi file nằm trong `admin_ui/`: `index.html`, `app.js`, `styles.css`, và `assets/` nếu cần (font, icon SVG). **Không CDN, không Google Fonts, không thư viện ngoài.** Font tự host trong `assets/` hoặc dùng font hệ thống. | CSP chặn mọi nguồn ngoài; trang quản trị không được phụ thuộc bên thứ ba. |
| 2 | **Không có `<script>` nội tuyến, không thuộc tính `onclick=`/`onload=`…** Gắn sự kiện bằng `addEventListener` trong `app.js`. | `script-src 'self'` chặn script nội tuyến. |
| 3 | Gọi API bằng `fetch('/admin/api/...', { credentials: 'same-origin' })`. | Phiên đăng nhập là cookie HttpOnly, JS không đọc được — đúng thiết kế. |
| 4 | Mọi request **POST** gửi header `X-CSRF-Token: <csrf_token>`. Lấy mã từ phản hồi đăng nhập hoặc `GET /admin/api/session`; chỉ giữ trong biến JS (bộ nhớ). | Chống CSRF. |
| 5 | **Không bao giờ dùng `innerHTML` với dữ liệu từ API.** Dùng `textContent`, `createElement`. Biểu đồ vẽ bằng SVG/Canvas từ số. | Chống XSS. |
| 6 | Không lưu mật khẩu, mã CSRF hay dữ liệu API vào `localStorage`/`sessionStorage`. Chỉ được lưu tùy chọn giao diện (giao diện sáng/tối, tab đang mở). | Giảm thiệt hại nếu máy bị xâm nhập. |
| 7 | Ô nhập cookie (Phương án 2): `<textarea>` có `autocomplete="off"`, `spellcheck="false"`, xóa trắng ngay sau khi gửi, **không bao giờ hiển thị lại** cookie (API cũng không trả về). | Cookie có toàn quyền tài khoản affiliate. |
| 8 | Các thao tác có hậu quả (tạm dừng bot, xóa hàng lỗi, đặt lại adapter, cập nhật cookie) phải có bước xác nhận bằng hộp thoại tự làm (không dùng `confirm()`). | Tránh bấm nhầm. |
| 9 | Liên kết ra ngoài (vd. trang sản phẩm Shopee) dùng `target="_blank" rel="noopener noreferrer"`. | Không lộ trang quản trị qua `Referer`/`opener`. |

## 4. Xử lý lỗi chung

| Mã | Ý nghĩa | Giao diện làm gì |
|---|---|---|
| 401 | Chưa đăng nhập / phiên hết hạn (8 giờ) | Về màn hình đăng nhập, giữ nguyên tab đang xem để quay lại sau khi đăng nhập |
| 403 | Thiếu/sai `X-CSRF-Token` | Gọi lại `GET /admin/api/session` để lấy mã mới, thử lại **một lần**; vẫn lỗi thì về đăng nhập |
| 404 | Truy cập từ IP không được phép, hoặc tài nguyên không có | Thông báo "Không truy cập được trang quản trị từ mạng này" |
| 409 | Thao tác không áp dụng (vd. cập nhật cookie khi Phương án 2 đang tắt) | Hiện nội dung `detail` |
| 413 | Dữ liệu gửi lên quá lớn | Hiện nội dung `detail` |
| 429 | Đăng nhập sai quá 5 lần trong 15 phút | Hiện thông báo và khóa nút đăng nhập, đếm ngược 15 phút |

Mọi lỗi có dạng `{"detail": "<câu tiếng Việt>"}` — hiển thị nguyên câu đó.

## 5. Các màn hình

### 5.1 Đăng nhập
- Một ô mật khẩu, nút "Đăng nhập". Nếu `GET /admin/api/session` trả `login_enabled: false`: hiện hướng dẫn "Chưa đặt mật khẩu quản trị: chạy `python -m app.cli hash-password` và đặt `ADMIN_PASSWORD_HASH`".

### 5.2 Tổng quan (màn hình mặc định)
Mục tiêu: **nhìn 3 giây biết bot có ổn không**. Gọi `GET /admin/api/overview`, tự làm mới 30 giây/lần khi tab đang hiển thị (dừng khi ẩn tab).
- Thanh trạng thái tổng: xanh "Bot đang chạy" / vàng "Có cảnh báo" / đỏ "Bot đang dừng hoặc adapter bị chặn". Suy ra từ `bot_enabled`, `adapters[*].state`, `zalo_token`, `dlq_len`, `alerts`.
- Công tắc **Bật/Tạm dừng bot** → `POST /admin/api/bot`.
- Chỉ số hôm nay (`today`): tổng lượt, tỉ lệ thành công, số lượt từ cache, số khách, độ trễ trung bình.
- Thẻ adapter: `shopee_openapi` (Phương án 1) hoặc `shopee_web` (Phương án 2), `tiktok` (`not_ready` → "Sắp có"). Trạng thái `ok` / `cooling` (đếm ngược `seconds_left`) / `stopped` (hiện `reason`, nút dẫn tới màn hình Phiên Shopee).
- Token Zalo: `configured`, `expires_in_s` (đổi sang "còn x giờ"; `0` = sẽ làm mới ở lần gửi tới, không phải lỗi).
- Hàng đợi `queue_len`, hàng lỗi `dlq_len` (bấm để sang màn hình Hàng lỗi).
- Cảnh báo `alerts` (mới nhất trước) và bộ đếm bảo mật `security_counters` (vd. `webhook_bad_signature` → "Webhook giả mạo bị chặn").

### 5.3 Thống kê
`GET /admin/api/stats?days=7|30|90` — bộ chọn khoảng thời gian.
- Biểu đồ cột chồng theo ngày (`by_day`: `success`, `failed`).
- Phân bố trạng thái (`by_status`), theo sàn (`by_platform`, `"-"` = link bị từ chối trước khi xác định sàn).
- Bảng lỗi hay gặp (`top_errors`) với nhãn tiếng Việt ở mục 6.

### 5.4 Lịch sử chuyển đổi
`GET /admin/api/conversions?limit=50..200` — bảng: thời gian (`ts` là Unix giây, hiển thị giờ địa phương), mã khách (`user_hash`, rút gọn 8 ký tự), sàn, sản phẩm (`item_key` dạng `shp:<shop>:<item>` → link tới `https://shopee.vn/product/<shop>/<item>`), trạng thái (chip màu), lỗi (nhãn), độ trễ, nguồn.

### 5.5 Hàng lỗi
`GET /admin/api/dlq` — danh sách việc không hoàn thành (`stage`: `convert` đổi link, `send` gửi tin, `internal` lỗi nội bộ). Nút "Xóa hàng lỗi" → `POST /admin/api/dlq/clear`. Hàng lỗi chỉ để xem nguyên nhân, không có thao tác gửi lại (bot đã báo khách thử lại sau).

### 5.6 Phiên Shopee (chỉ hiện khi `settings.shopee_mode == "web_session"`)
- Trạng thái phiên (`adapters.shopee_web.session`): đã cấu hình chưa, cập nhật lúc nào, đã dùng bao nhiêu ngày (cảnh báo khi > 7).
- Biểu mẫu "Cập nhật cookie": ô cookie (bắt buộc, 20–8000 ký tự, không xuống dòng), ô `csrf_token` (tùy chọn) → `POST /admin/api/shopee-web-session`. Thành công thì xóa trắng biểu mẫu, hiện trạng thái mới.
- Nút "Chạy lại adapter" → `POST /admin/api/adapters/shopee_web/reset`.
- Hướng dẫn ngắn cho chủ shop: đăng nhập affiliate.shopee.vn trên trình duyệt của mình → mở công cụ nhà phát triển (F12) → tab Network → bấm "Lấy link" một lần → chọn request `batchCustomLink` → sao chép giá trị header `cookie` (và `csrf-token` nếu có) → dán vào đây.
- Khung nhắc: "Đây là phương án tạm. Đăng ký Shopee Affiliate Open API để bot không phụ thuộc cookie."

### 5.7 Cài đặt & Nhật ký
- `GET /admin/api/settings`: chỉ đọc (chế độ Shopee, sub_id, hạn mức, thời gian giữ nhật ký, có Redis hay không). Ghi chú: "Thay đổi trong file cấu hình rồi khởi động lại bot".
- `GET /admin/api/audit-events`: nhật ký thao tác quản trị (thời gian, IP, hành động — nhãn ở mục 6).
- Nút **Đăng xuất** luôn có ở thanh điều hướng → `POST /admin/api/logout`.

## 6. Từ điển nhãn

**Trạng thái chuyển đổi (`status`)**: `ok` Thành công · `cached` Thành công (từ cache) · `rejected` Link không hợp lệ · `failed` Đổi link lỗi · `rate_limited` Vượt hạn mức

**Mã lỗi (`error_code` / `top_errors[].code` / `dlq.error`)**

| Mã | Nhãn |
|---|---|
| `host_not_allowed` | Không phải link Shopee/TikTok |
| `not_a_product` | Không phải link sản phẩm |
| `too_long` | Link quá dài |
| `userinfo`, `ip_literal`, `bad_port`, `bad_scheme`, `malformed` | Link bị chặn vì không an toàn |
| `non_public_ip`, `dns_failed`, `resolve_failed`, `short_link_dead`, `too_many_redirects` | Không mở được link rút gọn |
| `user_minute`, `user_day`, `global` | Vượt hạn mức (theo phút / theo ngày / toàn hệ thống) |
| `network`, `bad_json`, `http_5xx` (vd. `http_503`), `http_429` | Shopee tạm thời lỗi |
| `shopee_<mã>` | Shopee từ chối (mã <mã>) |
| `unexpected_link` | Shopee trả link lạ, đã chặn |
| `adapter_paused` | Tạm ngưng do lỗi liên tiếp |
| `web_session_missing` | Chưa nhập cookie Phương án 2 |
| `web_session_stopped` | Phiên Shopee bị chặn, cần cập nhật cookie |
| `web_rate_limited` | Phương án 2 vượt 20 link/phút |
| `fail_code_<n>` | Shopee không tạo được link (mã <n>) |
| `tiktok_not_ready` | TikTok chưa hỗ trợ |
| `zalo_<mã>`, `send_network`, `token_refresh_*` | Lỗi gửi tin Zalo |

**Hành động quản trị (`action`)**: `login` Đăng nhập · `login_failed` Đăng nhập sai · `bot_pause` Tạm dừng bot · `bot_enable` Bật bot · `dlq_clear` Xóa hàng lỗi · `web_session_update` Cập nhật cookie Shopee · `adapter_reset` Chạy lại adapter

## 7. Tham chiếu API (phản hồi thật từ server)

| Phương thức | Đường dẫn | Thân gửi | CSRF |
|---|---|---|---|
| GET | `/admin/api/session` | — | — |
| POST | `/admin/api/login` | `{"password": "..."}` | không |
| POST | `/admin/api/logout` | — | có |
| GET | `/admin/api/overview` | — | — |
| GET | `/admin/api/stats?days=7` | — | — |
| GET | `/admin/api/conversions?limit=50` | — | — |
| GET | `/admin/api/dlq?limit=50` | — | — |
| POST | `/admin/api/dlq/clear` | — | có |
| POST | `/admin/api/bot` | `{"enabled": true\|false}` | có |
| POST | `/admin/api/shopee-web-session` | `{"cookie": "...", "csrf_token": "..."}` | có |
| POST | `/admin/api/adapters/{shopee_openapi\|shopee_web}/reset` | — | có |
| GET | `/admin/api/settings` | — | — |
| GET | `/admin/api/audit-events?limit=50` | — | — |

```jsonc
// POST /admin/api/login
{ "ok": true, "csrf_token": "I_LfiJtmd5t01bsdglv-rg" }

// GET /admin/api/session
{ "authenticated": true, "csrf_token": "I_LfiJtmd5t01bsdglv-rg", "login_enabled": true }

// GET /admin/api/overview
{
  "bot_enabled": true,
  "queue_len": 0,
  "dlq_len": 1,
  "adapters": {
    "shopee_web": { "active": true, "state": "ok",
                    "session": { "configured": true, "updated_at": "2026-10-19 20:24:56", "age_days": 0.0 } },
    "tiktok": { "active": false, "state": "not_ready" }
  },
  // state khác: { "state": "cooling", "seconds_left": 42 }  |  { "state": "stopped", "reason": "phiên bị từ chối (HTTP 403)" }
  "zalo_token": { "configured": true, "expires_in_s": 0 },
  "today": { /* cùng cấu trúc với /stats */ },
  "security_counters": { "webhook_bad_signature": 3, "webhook_duplicate": 1 },
  "alerts": [ { "ts": 1792400000, "text": "Cookie Phương án 2 đã dùng 8.0 ngày, nên thay mới." } ]
}

// GET /admin/api/stats?days=7
{
  "days": 7, "total": 4, "success": 2, "success_rate": 50.0,   // success_rate = null khi total = 0
  "cache_hits": 1, "unique_users": 2, "avg_latency_ms": 842,   // avg_latency_ms có thể null
  "by_status": { "cached": 1, "ok": 1, "rate_limited": 1, "rejected": 1 },
  "by_platform": { "-": 2, "shopee": 2 },
  "by_day": [ { "date": "2026-10-19", "success": 2, "failed": 2 } ],   // chỉ có ngày có dữ liệu
  "top_errors": [ { "code": "user_minute", "count": 1 }, { "code": "host_not_allowed", "count": 1 } ]
}

// GET /admin/api/conversions?limit=3
{ "items": [
  { "ts": 1792416296, "user_hash": "3f9a1c22b0d4e871", "platform": "shopee", "item_key": "shp:123456:7891011",
    "status": "cached", "error_code": null, "latency_ms": 3, "source": "cache" },
  { "ts": 1792416296, "user_hash": "b71e0c9d4a2f6635", "platform": null, "item_key": null,
    "status": "rejected", "error_code": "host_not_allowed", "latency_ms": 1, "source": "-" }
] }

// GET /admin/api/dlq
{ "count": 1, "items": [ { "ts": 1792400100, "user_hash": "b71e0c9d4a2f6635", "stage": "convert", "error": "http_503" } ] }

// POST /admin/api/bot
{ "ok": true, "bot_enabled": false }

// POST /admin/api/shopee-web-session
{ "ok": true, "session": { "configured": true, "updated_at": "2026-10-19 20:24:56", "age_days": 0.0 } }

// POST /admin/api/adapters/shopee_web/reset
{ "ok": true, "adapters": { /* như overview.adapters */ } }

// GET /admin/api/settings
{ "shopee_mode": "web_session",            // hoặc "openapi"
  "sub_id_channel": "zalo", "sub_id_campaign": "c1",
  "rate_limits": { "per_user_per_min": 5, "per_user_per_day": 100, "global_per_min": 120,
                   "max_links_per_msg": 3, "web_fallback_per_min": 20 },
  "log_retention_days": 90, "redis": false }

// GET /admin/api/audit-events?limit=3
{ "items": [ { "ts": 1792416297, "ip": "127.0.0.1", "action": "web_session_update", "detail": "" } ] }

// Lỗi
{ "detail": "Phiên đăng nhập đã hết, vui lòng đăng nhập lại" }   // 401
{ "detail": "Thiếu hoặc sai mã CSRF" }                           // 403
```

## 8. Yêu cầu trải nghiệm

- Tiếng Việt toàn bộ, câu ngắn, gọi đúng tên chủ shop quen dùng ("link", "khách", "hoa hồng"), không dùng thuật ngữ kỹ thuật như "adapter", "DLQ", "circuit breaker" trên giao diện — dùng "Kết nối Shopee", "Việc bị lỗi", "Tạm ngưng tự động".
- Trạng thái thể hiện bằng màu **và** chữ/biểu tượng (không chỉ màu).
- Dùng được trên điện thoại từ 360px; bảng dài cuộn ngang trong khung riêng.
- Sáng/tối theo hệ thống, có nút chuyển.
- Dùng được bằng bàn phím, có trạng thái focus rõ ràng, nhãn `aria-*` cho biểu đồ và công tắc.
- Có trạng thái đang tải, trạng thái trống ("Chưa có lượt đổi link nào") và trạng thái lỗi cho từng khối.

## 9. Kiểm tra trước khi bàn giao

- [ ] Mở `/admin/` từ server demo: không có lỗi CSP trong Console (F12).
- [ ] Không có request nào ra ngoài `127.0.0.1:8091` (tab Network).
- [ ] `grep -n "innerHTML" admin_ui/app.js` không có kết quả dùng dữ liệu API.
- [ ] Mọi POST có `X-CSRF-Token`; xóa cookie phiên rồi bấm thao tác → về màn hình đăng nhập.
- [ ] Dán cookie → biểu mẫu trắng lại, cookie không xuất hiện ở đâu trên trang.
- [ ] Tạm dừng bot, xóa hàng lỗi, chạy lại adapter đều có hộp xác nhận.
- [ ] Hiển thị đúng ở 360px, 768px, 1440px; sáng và tối.
