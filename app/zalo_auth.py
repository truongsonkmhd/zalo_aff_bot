"""
Lấy refresh token Zalo OA lần đầu (OAuth v4 + PKCE). Chạy qua: python -m app.cli zalo-token
  1. In link cấp quyền → chủ OA mở, đăng nhập Zalo, bấm Cho phép.
  2. Zalo chuyển trình duyệt về Callback URL kèm ?code=...&oa_id=... (trang đó không cần tồn tại).
  3. Dán cả địa chỉ đó vào → đổi code lấy access/refresh token.
"""

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .zalo_client import TOKEN_URL

PERMISSION_URL = "https://oauth.zaloapp.com/v4/oa/permission"


def new_pkce():
    """→ (code_verifier, code_challenge, state)"""
    verifier = secrets.token_urlsafe(32)   # 43 ký tự
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge, secrets.token_urlsafe(12)


def permission_url(app_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    return PERMISSION_URL + "?" + urlencode({"app_id": app_id, "redirect_uri": redirect_uri,
                                             "code_challenge": challenge, "state": state})


def parse_callback(pasted: str, state: str):
    """Nhận địa chỉ trình duyệt bị chuyển tới (hoặc chỉ mã code) → (code, oa_id). Sai state → ValueError."""
    pasted = pasted.strip()
    if "code=" not in pasted:
        if not pasted:
            raise ValueError("Chưa dán gì")
        return pasted, ""
    q = parse_qs(urlparse(pasted).query if "?" in pasted else pasted)
    if q.get("state", [state])[0] != state:
        raise ValueError("state không khớp: hãy dùng link cấp quyền vừa in ra, không dùng link cũ")
    code = q.get("code", [""])[0]
    if not code:
        raise ValueError("Không thấy code trong địa chỉ đã dán")
    return code, q.get("oa_id", [""])[0]


def exchange_code(app_id: str, app_secret: str, code: str, verifier: str, transport=None) -> dict:
    """Đổi authorization code → {"access_token", "refresh_token", "expires_in"}. Lỗi → RuntimeError."""
    with httpx.Client(timeout=15, transport=transport) as client:
        resp = client.post(TOKEN_URL, headers={"secret_key": app_secret}, data={
            "app_id": app_id, "code": code, "code_verifier": verifier, "grant_type": "authorization_code"})
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"Zalo trả về dữ liệu lạ (HTTP {resp.status_code})") from None
    if not data.get("refresh_token"):
        detail = data.get("error_description") or data.get("message") or data.get("error") or resp.status_code
        raise RuntimeError(f"Zalo từ chối: {detail}")
    return data
