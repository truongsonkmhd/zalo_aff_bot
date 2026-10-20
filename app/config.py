"""
Cấu hình bot. Bí mật đọc theo thứ tự:
  1. File chỉ định bởi biến <TÊN>_FILE (Docker secrets, /run/secrets/...)
  2. Biến môi trường <TÊN>
Không bao giờ in giá trị bí mật: các trường bí mật có repr=False.
"""

import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    pass


def _secret(env, name, required=False):
    path = env.get(f"{name}_FILE")
    if path:
        try:
            with open(path, "r", encoding="utf-8") as f:
                value = f.read().strip()
        except OSError as e:
            raise ConfigError(f"Không đọc được {name}_FILE: {e.strerror}") from None
    else:
        value = env.get(name, "").strip()
    if required and not value:
        raise ConfigError(f"Thiếu cấu hình bắt buộc: {name}")
    return value


def _int(env, name, default, minimum=0):
    raw = env.get(name, "")
    try:
        value = int(raw) if raw else default
    except ValueError:
        raise ConfigError(f"{name} phải là số nguyên") from None
    if value < minimum:
        raise ConfigError(f"{name} phải >= {minimum}")
    return value


def _bool(env, name, default=False):
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # Zalo Official Account
    zalo_app_id: str
    zalo_oa_id: str
    zalo_oa_secret: str = field(repr=False)      # xác minh chữ ký webhook
    zalo_app_secret: str = field(repr=False)     # làm mới access token
    zalo_initial_refresh_token: str = field(repr=False)
    webhook_path_secret: str = field(repr=False)

    # Shopee
    shopee_app_id: str
    shopee_app_secret: str = field(repr=False)
    shopee_web_fallback: bool = False            # Phương án 2

    # Khóa nội bộ
    user_hash_key: str = field(repr=False, default="")
    data_key: str = field(repr=False, default="")    # AES-256-GCM cho token/cookie lưu trên đĩa
    admin_password_hash: str = field(repr=False, default="")

    # Hạ tầng
    redis_url: str = field(repr=False, default="")
    data_dir: str = "data"
    admin_allowed_cidrs: tuple = ("127.0.0.1/32", "::1/128")
    cookie_secure: bool = True

    # Thống kê sub_id
    sub_id_channel: str = "zalo"
    sub_id_campaign: str = ""

    # Hạn mức
    per_user_per_min: int = 5
    per_user_per_day: int = 100
    global_per_min: int = 120
    max_links_per_msg: int = 3
    web_fallback_per_min: int = 20
    queue_max: int = 1000

    # Giữ dữ liệu
    log_retention_days: int = 90
    webhook_max_skew_s: int = 300

    # Tra cứu đơn cho khách
    lookup_page_url: str = ""          # vd https://tenmien.vn/tra-don — gửi kèm khi khách nhắn /id
    cashback_percent: int = 0          # % hoa hồng hoàn cho khách; 0 = không hiển thị tiền hoàn
    lookup_per_ip_per_min: int = 10
    lookup_per_ip_per_day: int = 200
    orders_sync_days: int = 30
    # Chỉ bật khi cổng public CHỈ nhận kết nối từ Cloudflare Tunnel (khi đó IP thật nằm ở CF-Connecting-IP)
    trust_cf_header: bool = False

    # Phương án 2: bot tự mở Chrome và đọc phiên affiliate.shopee.vn (chỉ khi chạy trên máy có màn hình)
    shopee_browser: bool = False
    chrome_path: str = ""
    shopee_browser_keepalive_h: int = 6


def load_settings(env=None) -> Settings:
    env = os.environ if env is None else env
    shopee_app_id = env.get("SHOPEE_APP_ID", "").strip()
    shopee_secret = _secret(env, "SHOPEE_APP_SECRET")
    web_fallback = _bool(env, "SHOPEE_WEB_FALLBACK")
    if bool(shopee_app_id) != bool(shopee_secret):
        raise ConfigError("SHOPEE_APP_ID và SHOPEE_APP_SECRET phải khai báo cùng nhau")
    if not shopee_app_id and not web_fallback:
        raise ConfigError("Cần Shopee Open API (SHOPEE_APP_ID/SECRET) hoặc bật SHOPEE_WEB_FALLBACK=1")

    data_key = _secret(env, "DATA_KEY", required=True)
    if not (_is_hex(data_key) and len(bytes.fromhex(data_key)) == 32):
        raise ConfigError("DATA_KEY phải là 64 ký tự hex (32 byte). Tạo bằng: python -m app.cli gen-key")

    path_secret = _secret(env, "WEBHOOK_PATH_SECRET", required=True)
    if len(path_secret) < 24:
        raise ConfigError("WEBHOOK_PATH_SECRET quá ngắn (cần >= 24 ký tự ngẫu nhiên)")
    user_hash_key = _secret(env, "USER_HASH_KEY", required=True)
    if len(user_hash_key) < 32:
        raise ConfigError("USER_HASH_KEY quá ngắn (cần >= 32 ký tự ngẫu nhiên)")

    lookup_url = env.get("LOOKUP_PAGE_URL", "").strip()
    if lookup_url and not lookup_url.startswith("https://"):
        raise ConfigError("LOOKUP_PAGE_URL phải bắt đầu bằng https://")

    shopee_browser = _bool(env, "SHOPEE_BROWSER")
    if shopee_browser and (shopee_app_id or not web_fallback):
        raise ConfigError("SHOPEE_BROWSER=1 chỉ dùng cho Phương án 2 (SHOPEE_WEB_FALLBACK=1, không có Open API)")

    cidrs = tuple(c.strip() for c in env.get("ADMIN_ALLOWED_CIDRS", "127.0.0.1/32,::1/128").split(",") if c.strip())

    return Settings(
        zalo_app_id=_secret(env, "ZALO_APP_ID", required=True),
        zalo_oa_id=env.get("ZALO_OA_ID", "").strip(),
        zalo_oa_secret=_secret(env, "ZALO_OA_SECRET", required=True),
        zalo_app_secret=_secret(env, "ZALO_APP_SECRET", required=True),
        zalo_initial_refresh_token=_secret(env, "ZALO_REFRESH_TOKEN"),
        webhook_path_secret=path_secret,
        shopee_app_id=shopee_app_id,
        shopee_app_secret=shopee_secret,
        shopee_web_fallback=web_fallback,
        user_hash_key=user_hash_key,
        data_key=data_key,
        admin_password_hash=_secret(env, "ADMIN_PASSWORD_HASH"),
        redis_url=_secret(env, "REDIS_URL"),
        data_dir=env.get("DATA_DIR", "data"),
        admin_allowed_cidrs=cidrs,
        cookie_secure=_bool(env, "COOKIE_SECURE", True),
        sub_id_channel=env.get("SUB_ID_CHANNEL", "zalo"),
        sub_id_campaign=env.get("SUB_ID_CAMPAIGN", ""),
        per_user_per_min=_int(env, "RATE_PER_USER_PER_MIN", 5, 1),
        per_user_per_day=_int(env, "RATE_PER_USER_PER_DAY", 100, 1),
        global_per_min=_int(env, "RATE_GLOBAL_PER_MIN", 120, 1),
        max_links_per_msg=_int(env, "MAX_LINKS_PER_MSG", 3, 1),
        web_fallback_per_min=_int(env, "WEB_FALLBACK_PER_MIN", 20, 1),
        queue_max=_int(env, "QUEUE_MAX", 1000, 10),
        log_retention_days=_int(env, "LOG_RETENTION_DAYS", 90, 1),
        webhook_max_skew_s=_int(env, "WEBHOOK_MAX_SKEW_S", 300, 30),
        lookup_page_url=env.get("LOOKUP_PAGE_URL", "").strip(),
        cashback_percent=min(100, _int(env, "CASHBACK_PERCENT", 0, 0)),
        lookup_per_ip_per_min=_int(env, "LOOKUP_PER_IP_PER_MIN", 10, 1),
        lookup_per_ip_per_day=_int(env, "LOOKUP_PER_IP_PER_DAY", 200, 1),
        orders_sync_days=_int(env, "ORDERS_SYNC_DAYS", 30, 1),
        trust_cf_header=_bool(env, "TRUST_CF_HEADER"),
        shopee_browser=shopee_browser,
        chrome_path=env.get("CHROME_PATH", "").strip(),
        shopee_browser_keepalive_h=_int(env, "SHOPEE_BROWSER_KEEPALIVE_H", 6, 1),
    )


def _is_hex(s):
    try:
        bytes.fromhex(s)
        return True
    except ValueError:
        return False
