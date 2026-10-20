"""
Khởi chạy bot.
  ROLE=all      gateway + worker trong một tiến trình (mặc định, hợp với chạy thử / máy nhỏ)
  ROLE=gateway  chỉ nhận webhook + API quản trị (cần REDIS_URL)
  ROLE=worker   chỉ xử lý hàng đợi (cần REDIS_URL)
"""

import asyncio
import collections
import contextlib
import os
import time
from dataclasses import dataclass, field

import httpx

from .adapters import AdapterRouter, ShopeeOpenApiAdapter, ShopeeWebSessionAdapter
from .audit import AuditLog
from .config import ConfigError, Settings, load_settings
from .orders import OrderStore, parse_conversion_nodes
from .pipeline import Pipeline, run_worker
from .ratelimit import RateLimiter
from .secret_store import SecretStore
from .shopee_browser import ShopeeBrowserSession
from .store import make_store
from .zalo_client import TokenManager, ZaloClient


@dataclass
class AppContext:
    settings: Settings
    store: object
    router: AdapterRouter
    audit: AuditLog
    limiter: RateLimiter
    tokens: TokenManager
    zalo: ZaloClient
    web_adapter: object = None
    orders: OrderStore = None
    browser: object = None
    alerts: collections.deque = field(default_factory=lambda: collections.deque(maxlen=50))
    counters: collections.Counter = field(default_factory=collections.Counter)

    async def audit_async(self, method, *args):
        return await asyncio.to_thread(getattr(self.audit, method), *args)

    def close(self):
        self.audit.close()
        if self.orders:
            self.orders.close()

    async def sync_orders(self):
        """Lấy báo cáo chuyển đổi từ Shopee Open API trong N ngày gần nhất và cập nhật kho đơn."""
        end = int(time.time())
        start = end - self.settings.orders_sync_days * 86400
        nodes = await self.router.shopee_openapi.conversion_report(start, end)
        found = parse_conversion_nodes(nodes)
        n = await asyncio.to_thread(self.orders.upsert, found, "openapi")
        matched = sum(1 for o in found if o["code"])
        await asyncio.to_thread(self.orders.set_meta, "last_sync",
                                {"ts": end, "source": "openapi", "count": n, "matched": matched})
        return {"ok": True, "imported": n, "matched": matched}


def build_context(settings: Settings, store=None, transports=None) -> AppContext:
    transports = transports or {}
    os.makedirs(settings.data_dir, exist_ok=True)
    store = store or make_store(settings.redis_url)
    secrets = SecretStore(os.path.join(settings.data_dir, "secrets"), settings.data_key)
    alerts = collections.deque(maxlen=50)

    async def alert(text: str):
        alerts.appendleft({"ts": int(time.time()), "text": text})
        await send_telegram_alert(text)

    openapi = (ShopeeOpenApiAdapter(settings.shopee_app_id, settings.shopee_app_secret,
                                    transport=transports.get("shopee"))
               if settings.shopee_app_id else None)
    web = (ShopeeWebSessionAdapter(secrets, store, settings.web_fallback_per_min, alert=alert,
                                   transport=transports.get("shopee_web"))
           if settings.shopee_web_fallback and not openapi else None)
    browser = (ShopeeBrowserSession(web, os.path.join(settings.data_dir, "chrome_shopee"), settings.chrome_path,
                                    alert=alert, keepalive_s=settings.shopee_browser_keepalive_h * 3600)
               if web and settings.shopee_browser else None)
    tokens = TokenManager(settings.zalo_app_id, settings.zalo_app_secret, secrets, store,
                          settings.zalo_initial_refresh_token, transport=transports.get("zalo"))
    ctx = AppContext(
        settings=settings, store=store,
        router=AdapterRouter(shopee_openapi=openapi, shopee_web=web),
        audit=AuditLog(os.path.join(settings.data_dir, "audit.db")),
        limiter=RateLimiter(store, settings.per_user_per_min, settings.per_user_per_day, settings.global_per_min),
        tokens=tokens, zalo=ZaloClient(tokens, transport=transports.get("zalo")),
        web_adapter=web, alerts=alerts, browser=browser,
        orders=OrderStore(os.path.join(settings.data_dir, "orders.db")),
    )
    return ctx


async def send_telegram_alert(text: str):
    token, chat = os.environ.get("ALERT_TELEGRAM_BOT_TOKEN", ""), os.environ.get("ALERT_TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat, "text": f"[Bot đổi link] {text}"})
    except httpx.HTTPError:
        pass


async def orders_sync_loop(ctx: AppContext, stop: asyncio.Event, every_s: int = 1800):
    """Đồng bộ đơn từ Open API mỗi 30 phút (chỉ khi có Open API)."""
    while not stop.is_set() and ctx.router.shopee_openapi:
        try:
            await ctx.sync_orders()
        except Exception as e:      # lỗi đồng bộ không được làm dừng bot
            ctx.alerts.appendleft({"ts": int(time.time()), "text": f"Đồng bộ đơn hàng lỗi: {type(e).__name__} {e}"[:200]})
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every_s)


async def maintenance(ctx: AppContext, stop: asyncio.Event):
    """Xóa nhật ký quá hạn mỗi ngày; nhắc thay cookie Phương án 2 khi quá 7 ngày."""
    while not stop.is_set():
        await ctx.audit_async("purge", ctx.settings.log_retention_days)
        if ctx.web_adapter:
            info = ctx.web_adapter.session_info()
            if info.get("configured") and info.get("age_days", 0) > 7 and \
                    await ctx.store.set_once("notice:web_session_age", 86400):
                ctx.alerts.appendleft({"ts": int(time.time()),
                                       "text": f"Cookie Phương án 2 đã dùng {str(info['age_days']).replace('.', ',')} ngày, nên thay mới."})
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=86400)


def main():
    from .web import create_app

    try:
        settings = load_settings()
    except ConfigError as e:
        raise SystemExit(f"Lỗi cấu hình: {e}")
    role = os.environ.get("ROLE", "all")
    if role != "all" and not settings.redis_url:
        raise SystemExit("ROLE=gateway/worker cần REDIS_URL để chia sẻ hàng đợi")
    if settings.shopee_browser and role != "all":
        raise SystemExit("SHOPEE_BROWSER=1 chỉ chạy với ROLE=all trên máy có Chrome và màn hình")
    ctx = build_context(settings)
    pipeline = Pipeline(settings, ctx.store, ctx.router, ctx.zalo, ctx.audit)

    if role == "worker":
        async def run():
            stop = asyncio.Event()
            await asyncio.gather(run_worker(pipeline, ctx.store, stop), maintenance(ctx, stop),
                                 orders_sync_loop(ctx, stop))
        asyncio.run(run())
        return

    background = ()
    if role == "all":
        background = (lambda stop: run_worker(pipeline, ctx.store, stop), lambda stop: maintenance(ctx, stop),
                      lambda stop: orders_sync_loop(ctx, stop))
        if ctx.browser:
            background += (lambda stop: ctx.browser.run(stop),)
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8090"))
    admin_port = os.environ.get("ADMIN_PORT", "")
    if admin_port:
        # Hai cổng: webhook (đưa ra tunnel) và quản trị (chỉ nội bộ). Route quản trị không tồn tại ở cổng webhook.
        apps = [(create_app(ctx, background=background, include_admin=False), host, port),
                (create_app(ctx, include_webhook=False), os.environ.get("ADMIN_HOST", "127.0.0.1"), int(admin_port))]
    else:
        apps = [(create_app(ctx, background=background), host, port)]
    asyncio.run(_serve(apps))


async def _serve(apps):
    import uvicorn
    servers = [uvicorn.Server(uvicorn.Config(app, host=h, port=p, proxy_headers=False, server_header=False,
                                             date_header=False)) for app, h, p in apps]
    await asyncio.gather(*(srv.serve() for srv in servers))


if __name__ == "__main__":
    main()
