"""
Tiện ích dòng lệnh:
  python -m app.cli gen-secrets     in ra DATA_KEY, USER_HASH_KEY, WEBHOOK_PATH_SECRET mới
  python -m app.cli hash-password   nhập mật khẩu quản trị (không hiện trên màn hình) → ADMIN_PASSWORD_HASH
  python -m app.cli zalo-token      cấp quyền cho ứng dụng gửi tin bằng OA → ZALO_REFRESH_TOKEN, ZALO_OA_ID
"""

import getpass
import os
import secrets
import sys

from . import zalo_auth
from .security import hash_password


def zalo_token():
    app_id = os.environ.get("ZALO_APP_ID") or input("App ID (developers.zalo.me → ứng dụng): ").strip()
    app_secret = os.environ.get("ZALO_APP_SECRET") or getpass.getpass("Khóa bí mật của ứng dụng (không hiện): ").strip()
    print("Callback URL phải trùng với mục Official Account → Callback URL trong cài đặt ứng dụng Zalo.")
    redirect_uri = input("Callback URL (vd https://tenmien-cua-ban.vn/zalo-callback): ").strip()
    if not (app_id and app_secret and redirect_uri.startswith("https://")):
        print("Cần đủ App ID, khóa bí mật và Callback URL dạng https://", file=sys.stderr)
        return 1
    verifier, challenge, state = zalo_auth.new_pkce()
    print("\n1. Mở link dưới đây bằng tài khoản Zalo quản trị OA, chọn đúng OA rồi bấm Cho phép:\n")
    print("   " + zalo_auth.permission_url(app_id, redirect_uri, challenge, state))
    print("\n2. Trình duyệt sẽ chuyển tới Callback URL (trang báo lỗi cũng được).")
    print("   Sao chép NGUYÊN địa chỉ trên thanh địa chỉ (có ?code=...) và dán vào đây. Mã chỉ dùng được trong vài phút.")
    try:
        code, oa_id = zalo_auth.parse_callback(input("\nĐịa chỉ sau khi cấp quyền: "), state)
        data = zalo_auth.exchange_code(app_id, app_secret, code, verifier)
    except (ValueError, RuntimeError) as e:
        print(f"Không lấy được token: {e}", file=sys.stderr)
        return 1
    print("\nThành công. Dán các dòng sau vào .env (hoặc file secrets khi chạy Docker):\n")
    if oa_id:
        print(f"ZALO_OA_ID={oa_id}")
    print(f"ZALO_REFRESH_TOKEN={data['refresh_token']}")
    print("\nLưu ý: refresh token chỉ dùng được MỘT lần. Đừng dùng nó ở công cụ khác; bot sẽ tự đổi và lưu token mới.")
    print("Nếu bot đã từng chạy với token cũ, xóa file <DATA_DIR>/secrets/zalo_tokens.enc rồi khởi động lại để bot nhận token này.")
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "gen-secrets":
        print(f"DATA_KEY={secrets.token_hex(32)}")
        print(f"USER_HASH_KEY={secrets.token_urlsafe(48)}")
        print(f"WEBHOOK_PATH_SECRET={secrets.token_urlsafe(32)}")
        return 0
    if cmd == "hash-password":
        pw = getpass.getpass("Mật khẩu quản trị (>= 12 ký tự): ")
        if len(pw) < 12:
            print("Mật khẩu quá ngắn", file=sys.stderr)
            return 1
        if pw != getpass.getpass("Nhập lại: "):
            print("Hai lần nhập không khớp", file=sys.stderr)
            return 1
        print(f"ADMIN_PASSWORD_HASH={hash_password(pw)}")
        return 0
    if cmd == "zalo-token":
        return zalo_token()
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
