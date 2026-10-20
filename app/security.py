"""Các hàm bảo mật lõi: chữ ký webhook, băm danh tính, mật khẩu, mã hóa dữ liệu lưu trữ."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# ── Webhook Zalo OA ───────────────────────────────────────────────
def zalo_expected_mac(app_id: str, raw_body: str, timestamp: str, oa_secret: str) -> str:
    """mac = sha256(appId + data + timeStamp + OASecretKey) — theo tài liệu webhook Zalo OA."""
    return hashlib.sha256(f"{app_id}{raw_body}{timestamp}{oa_secret}".encode("utf-8")).hexdigest()


def verify_zalo_webhook(raw_body: bytes, signature_header: str, app_id: str, oa_secret: str,
                        max_skew_s: int, now: float = None):
    """
    Trả về (ok, lý_do, event_dict). Kiểm tra theo thứ tự rẻ → đắt, không tiết lộ lý do cụ thể ra ngoài.
    """
    if not signature_header:
        return False, "missing_signature", None
    try:
        body_str = raw_body.decode("utf-8")
        event = json.loads(body_str)
    except (UnicodeDecodeError, ValueError):
        return False, "bad_body", None
    if not isinstance(event, dict):
        return False, "bad_body", None

    timestamp = str(event.get("timestamp", ""))
    if not timestamp.isdigit():
        return False, "bad_timestamp", None

    provided = signature_header.strip()
    if provided.lower().startswith("mac="):
        provided = provided[4:]
    expected = zalo_expected_mac(app_id, body_str, timestamp, oa_secret)
    if not hmac.compare_digest(provided.lower(), expected):
        return False, "bad_signature", None

    if str(event.get("app_id", "")) != str(app_id):
        return False, "wrong_app", None

    now = time.time() if now is None else now
    ts = int(timestamp)
    ts_s = ts / 1000 if ts > 10_000_000_000 else ts
    if abs(now - ts_s) > max_skew_s:
        return False, "stale", None
    return True, "ok", event


# ── Danh tính khách: chỉ lưu HMAC, không lưu ID thật ─────────────────
def user_hash(user_id: str, key: str, length: int = 16) -> str:
    return hmac.new(key.encode("utf-8"), str(user_id).encode("utf-8"), hashlib.sha256).hexdigest()[:length]


# ── Mật khẩu quản trị (scrypt) ─────────────────────────────────────
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 15, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                            maxmem=64 * 1024 * 1024, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(hash_b64)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt_b64),
                                n=int(n), r=int(r), p=int(p), maxmem=64 * 1024 * 1024, dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


# ── Mã hóa dữ liệu lưu trên đĩa (token Zalo, cookie Phương án 2) ───────
class Sealer:
    """AES-256-GCM. Định dạng: base64(nonce 12 byte + ciphertext). `purpose` gắn làm AAD để
    không thể lấy bản mã của mục này dán sang mục khác."""

    def __init__(self, key_hex: str):
        key = bytes.fromhex(key_hex)
        if len(key) != 32:
            raise ValueError("Khóa phải 32 byte")
        self._aead = AESGCM(key)

    def seal(self, plaintext: bytes, purpose: str) -> str:
        nonce = os.urandom(12)
        return base64.b64encode(nonce + self._aead.encrypt(nonce, plaintext, purpose.encode())).decode()

    def open(self, sealed: str, purpose: str) -> bytes:
        raw = base64.b64decode(sealed)
        return self._aead.decrypt(raw[:12], raw[12:], purpose.encode())
