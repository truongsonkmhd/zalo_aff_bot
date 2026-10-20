"""
Server demo — KHÔNG dùng cho chạy thật. Dữ liệu mẫu trong thư mục tạm, không gọi Zalo/Shopee thật.
  python -m app.devserver [--zalo-id <ID Zalo>]
    Quản trị: http://127.0.0.1:8091/admin/    mật khẩu: demo-admin-123456
    Tra đơn : http://127.0.0.1:8092/tra-don/
Truyền --zalo-id để tạo sẵn đơn mẫu cho mã tra cứu của ID đó (mã demo khác mã khi chạy thật,
vì bản thật dùng khóa bí mật riêng).
"""

import argparse
import asyncio
import random
import secrets
import sys
import tempfile
import time

from .config import Settings
from .main import _serve, build_context
from .orders import customer_code
from .security import hash_password
from .store import MemoryStore
from .web import create_app

DEMO_PASSWORD = "demo-admin-123456"
DEMO_USER_KEY = "demo-only-user-hash-key-khong-dung-cho-chay-that-0001"   # cố định để mã demo không đổi
PRODUCTS = ["Áo thun nam cotton co giãn", "Tai nghe bluetooth chống ồn", "Kem chống nắng SPF50+",
            "Bình giữ nhiệt 750ml", "Ốp lưng iPhone 15 trong suốt", "Dép quai ngang nữ", "Sạc nhanh 20W Type-C",
            "Balo laptop chống nước", "Son kem lì màu đỏ đất", "Nồi chiên không dầu 5L"]


def seed_orders(ctx, rnd, now, code):
    """Đơn mẫu: một nhóm cho mã của bạn, một nhóm của khách khác, vài đơn chưa khớp mã."""
    def order(c, days_ago, status):
        items = [{"name": rnd.choice(PRODUCTS), "qty": rnd.randint(1, 2)} for _ in range(rnd.randint(1, 3))]
        value = sum(rnd.randint(59, 890) * 1000 * i["qty"] for i in items)
        oid = f"{time.strftime('%y%m%d', time.localtime(now - days_ago * 86400))}{''.join(rnd.choices('ABCDEFGHJKLMNPQRSTUVWXYZ0123456789', k=8))}"
        return {"order_id": oid, "code": c, "status": status, "purchase_ts": now - days_ago * 86400 - rnd.randint(0, 80000),
                "items": items, "order_value": value, "commission": int(value * rnd.uniform(0.03, 0.09))}
    mine = [order(code, d, s) for d, s in ((0, "pending"), (2, "pending"), (6, "completed"), (11, "completed"), (19, "cancelled"))]
    others = [order(customer_code(f"khach-{i}", DEMO_USER_KEY), rnd.randint(0, 28), rnd.choice(["completed", "pending", "cancelled"]))
              for i in range(40)]
    unmatched = [order("", rnd.randint(0, 28), "completed") for _ in range(6)]
    ctx.orders.upsert(mine + others + unmatched, "csv")
    ctx.orders.set_meta("last_sync", {"ts": now - 1800, "source": "csv", "count": 51, "matched": 45})
    return mine


def seed(ctx):
    rnd = random.Random(7)
    now = int(time.time())
    users = [secrets.token_hex(8) for _ in range(25)]
    statuses = ["ok"] * 12 + ["cached"] * 6 + ["rejected"] * 2 + ["failed", "rate_limited"]
    errors = {"rejected": ["host_not_allowed", "not_a_product"], "failed": ["http_503", "shopee_10020"],
              "rate_limited": ["user_minute"]}
    conn = ctx.audit._conn
    for ts in sorted(now - rnd.randint(0, 29 * 86400) for _ in range(600)):
        st = rnd.choice(statuses)
        item = f"shp:{rnd.randint(10**5, 10**6)}:{rnd.randint(10**9, 10**10)}" if st in ("ok", "cached", "failed") else None
        conn.execute("INSERT INTO conversions(ts,user_hash,platform,item_key,status,error_code,latency_ms,source) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (ts, rnd.choice(users), "shopee" if item else None, item, st,
                      rnd.choice(errors[st]) if st in errors else None,
                      rnd.randint(400, 1800) if st == "ok" else (rnd.randint(1, 5) if st == "cached" else None),
                      {"ok": "shopee_web", "cached": "cache", "failed": "shopee_web"}.get(st, "-")))
    conn.commit()
    ctx.audit.admin_event("127.0.0.1", "login")
    ctx.audit.admin_event("127.0.0.1", "web_session_update")
    ctx.alerts.appendleft({"ts": now - 3600, "text": "Cookie Phương án 2 đã dùng 8,0 ngày, nên thay mới."})
    ctx.web_adapter.set_session("SPC_EC=demo" + "x" * 40, "demo-csrf")
    asyncio.run(ctx.store.push_dlq({"ts": now - 600, "user_hash": users[0], "stage": "convert", "error": "http_503"}))


class DemoBrowser:
    """Giả lập cửa sổ Chrome đã đăng nhập, để xem giao diện mà không mở Chrome thật."""

    def __init__(self):
        now = int(time.time())
        self._s = {"browser": "open", "logged_in": True, "checked_at": now - 40, "synced_at": now - 7200,
                   "last_result": "unchanged"}

    def status(self):
        return dict(self._s)

    async def open(self):
        return self.status()

    async def sync(self):
        self._s["checked_at"] = int(time.time())
        return "unchanged"


def main():
    parser = argparse.ArgumentParser(description="Server demo Bot Đổi Link")
    parser.add_argument("--zalo-id", default="demo-khach-hang", help="ID Zalo để tạo sẵn đơn mẫu cho mã tra cứu")
    args = parser.parse_args()
    tmp = tempfile.mkdtemp(prefix="affbot_demo_")
    settings = Settings(
        zalo_app_id="demo", zalo_oa_id="demo", zalo_oa_secret="demo", zalo_app_secret="demo",
        zalo_initial_refresh_token="demo-refresh-token", webhook_path_secret=secrets.token_urlsafe(32),
        shopee_app_id="", shopee_app_secret="", shopee_web_fallback=True,
        user_hash_key=DEMO_USER_KEY, data_key=secrets.token_hex(32),
        admin_password_hash=hash_password(DEMO_PASSWORD), data_dir=tmp,
        admin_allowed_cidrs=("127.0.0.1/32", "::1/128"), cookie_secure=False, sub_id_campaign="demo",
        lookup_page_url="http://127.0.0.1:8092/tra-don/", cashback_percent=50,
    )
    ctx = build_context(settings, store=MemoryStore())
    ctx.browser = DemoBrowser()
    seed(ctx)
    code = customer_code(args.zalo_id, DEMO_USER_KEY)
    mine = seed_orders(ctx, random.Random(11), int(time.time()), code)
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"Quản trị : http://127.0.0.1:8091/admin/   (mật khẩu: {DEMO_PASSWORD})")
    print(f"Tra đơn  : http://127.0.0.1:8092/tra-don/")
    print(f"Mã tra cứu demo của ID {args.zalo_id}: {code}  ({len(mine)} đơn mẫu)")
    print(f"Tra một đơn bằng mã đơn: {mine[2]['order_id']}")
    asyncio.run(_serve([(create_app(ctx, include_webhook=False), "127.0.0.1", 8091),
                        (create_app(ctx, include_admin=False), "127.0.0.1", 8092)]))


if __name__ == "__main__":
    main()
