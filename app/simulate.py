"""
Mô phỏng hội thoại với bot — chạy toàn bộ code thật (webhook, chữ ký, URL Guard, hạn mức, pipeline,
soạn tin, nhật ký), chỉ thay Zalo OA, Shopee và DNS bằng bản giả lập. Không cần khóa thật, không gọi mạng.
  python -m app.simulate
"""

import asyncio
import hashlib
import json
import secrets
import sys
import tempfile
import time

import httpx

from . import url_guard
from .config import Settings
from .main import build_context
from .pipeline import Pipeline
from .security import zalo_expected_mac
from .store import MemoryStore
from .web import create_app

APP_ID, OA_ID, OA_SECRET = "sim-app", "sim-oa", "sim-oa-secret"

# Link rút gọn giả lập: một link thật của shop, một link độc chuyển hướng vào mạng nội bộ máy chủ
SHORT_LINKS = {
    "https://s.shopee.vn/AbC123": "https://shopee.vn/Ao-thun-nam-cotton-i.123456.7891011?sp_atk=abc&xptdk=def",
    "https://s.shopee.vn/XyZ789": "https://shopee.vn/Tai-nghe-bluetooth-i.555444.2223334",
    "https://s.shopee.vn/Evil00": "http://169.254.169.254/latest/meta-data/iam",
}


async def fake_dns(host):
    return ["104.18.10.20"]


def short_link_server(req: httpx.Request):
    target = SHORT_LINKS.get(str(req.url))
    return httpx.Response(302, headers={"location": target}) if target else httpx.Response(404)


def fake_shopee(req: httpx.Request):
    query = json.loads(req.content)["query"]
    code = hashlib.sha256(query.encode()).hexdigest()[:8]
    return httpx.Response(200, json={"data": {"generateShortLink": {"shortLink": f"https://s.shopee.vn/aff{code}"}}})


class FakeZalo:
    def __init__(self):
        self.outbox = []

    def __call__(self, req: httpx.Request):
        if "oauth.zaloapp.com" in str(req.url):
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT2", "expires_in": "90000"})
        body = json.loads(req.content)
        self.outbox.append(body["message"]["text"])
        return httpx.Response(200, json={"error": 0, "message": "Success"})


def event(text, user="khach-001"):
    return {"app_id": APP_ID, "event_name": "user_send_text", "timestamp": str(int(time.time() * 1000)),
            "sender": {"id": user}, "recipient": {"id": OA_ID},
            "message": {"text": text, "msg_id": secrets.token_hex(8)}}


async def post(client, path_secret, ev, secret=OA_SECRET):
    body = json.dumps(ev)
    mac = zalo_expected_mac(APP_ID, body, ev["timestamp"], secret)
    return await client.post(f"/webhook/zalo/{path_secret}", content=body,
                       headers={"X-ZEvent-Signature": f"mac={mac}", "Content-Type": "application/json"})


class Sim:
    """Bot đầy đủ với Zalo/Shopee/DNS giả lập. Dùng chung cho mô phỏng terminal và khung chat (app.chatsim)."""

    def __init__(self, **overrides):
        self.path_secret = secrets.token_urlsafe(32)
        self.settings = Settings(**{
            "zalo_app_id": APP_ID, "zalo_oa_id": OA_ID, "zalo_oa_secret": OA_SECRET, "zalo_app_secret": "sim",
            "zalo_initial_refresh_token": "RT1", "webhook_path_secret": self.path_secret,
            "shopee_app_id": "sim-shopee", "shopee_app_secret": "sim-shopee-secret", "shopee_web_fallback": False,
            "user_hash_key": secrets.token_urlsafe(48), "data_key": secrets.token_hex(32),
            "data_dir": tempfile.mkdtemp(prefix="affbot_sim_"), "sub_id_campaign": "demo", "per_user_per_min": 5,
            "lookup_page_url": "https://tenmien-cua-shop.vn/tra-don/", **overrides})
        self.zalo = FakeZalo()
        self.store = MemoryStore()
        self.ctx = build_context(self.settings, store=self.store,
                                 transports={"zalo": httpx.MockTransport(self.zalo),
                                             "shopee": httpx.MockTransport(fake_shopee)})
        short_transport = httpx.MockTransport(short_link_server)
        self.pipeline = Pipeline(self.settings, self.store, self.ctx.router, self.ctx.zalo, self.ctx.audit,
                                 retry_delays=(0, 0),
                                 inspect_fn=lambda u: url_guard.inspect(u, transport=short_transport, resolver=fake_dns))
        # Gọi webhook qua ASGI trong cùng event loop với hàng đợi (như khi chạy thật)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(self.ctx, include_admin=False)),
                                        base_url="http://127.0.0.1")
        self._lock = asyncio.Lock()

    async def send(self, text, user="khach-001", secret=OA_SECRET):
        """Khách gửi một tin → (mã HTTP webhook trả về, các tin bot trả lời)."""
        async with self._lock:   # hàng đợi dùng chung: xử lý từng tin một để tin trả lời không lẫn giữa các khách
            before = len(self.zalo.outbox)
            status = (await post(self.client, self.path_secret, event(text, user), secret=secret)).status_code
            while (job := await self.store.dequeue(timeout=0.01)) is not None:
                await self.pipeline.handle(job)
            return status, self.zalo.outbox[before:]

    async def close(self):
        await self.client.aclose()
        self.ctx.close()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(run())


async def run():
    sim = Sim()
    ctx = sim.ctx

    async def say(title, text, user="khach-001", secret=OA_SECRET):
        status, replies = await sim.send(text, user, secret)
        print(f"\n━━ {title}")
        print(f"  Khách : {text}")
        print(f"  Webhook trả HTTP {status}")
        for r in replies:
            print("  Bot   : " + r.replace("\n", "\n          "))
        if not replies:
            print("  Bot   : (không trả lời)")

    print("MÔ PHỎNG BOT ĐỔI LINK AFFILIATE — Zalo/Shopee giả lập, toàn bộ logic bot là code thật")
    await say("0. Khách nhắn /id để lấy mã tra cứu đơn", "/id")
    await say("1. Khách gửi link rút gọn Shopee", "Shop ơi đổi giúp mình link này https://s.shopee.vn/AbC123 nhé")
    await say("2. Khách khác gửi lại đúng sản phẩm đó (lấy từ cache)", "https://s.shopee.vn/AbC123", user="khach-002")
    await say("3. Một tin có 2 link: một hợp lệ, một trang lừa đảo",
        "https://s.shopee.vn/XyZ789 và https://shopee-giam-gia.evil.com/i.1.2", user="khach-003")
    await say("4. Link rút gọn độc chuyển hướng vào mạng nội bộ máy chủ (SSRF)", "https://s.shopee.vn/Evil00", user="khach-004")
    await say("5. Link Shopee có gắn sẵn mã affiliate của người khác",
        "https://shopee.vn/product/123456/7891011?affiliate_id=NGUOIKHAC&sub_id=hack", user="khach-005")
    await say("6. Tin nhắn không có link", "Shop còn hàng không ạ?", user="khach-006")
    for i in range(6):
        await say(f"7.{i + 1} Spam liên tục (hạn mức 5 link/phút)", f"https://shopee.vn/product/1/{i + 1}", user="khach-007")
    await say("8. Kẻ xấu gọi webhook với chữ ký giả", "https://s.shopee.vn/AbC123", user="ke-gia-mao", secret="doan-bua")

    stats = ctx.audit.stats(1)
    print("\n━━ Nhật ký (chỉ lưu mã băm của khách, không lưu nội dung tin):")
    for row in ctx.audit.recent(4):
        print("  ", {k: row[k] for k in ("user_hash", "item_key", "status", "error_code", "source")})
    print(f"\n━━ Thống kê: {stats['total']} lượt, thành công {stats['success']} "
          f"({stats['success_rate']}%), từ cache {stats['cache_hits']}, theo trạng thái {stats['by_status']}")
    print(f"━━ Bộ đếm bảo mật: {dict(ctx.counters)}")
    await sim.close()


if __name__ == "__main__":
    main()
