"""
Cổng HTTP:
  - POST /webhook/zalo/{path_secret}   nhận sự kiện Zalo OA (chữ ký, chống phát lại, chống trùng)
  - /admin/api/*                       API quản trị cho giao diện (phiên HttpOnly + CSRF + giới hạn IP)
  - /admin/                            giao diện quản trị tĩnh (thư mục admin_ui/)
"""

import hashlib
import hmac
import ipaddress
import json
import os
import asyncio
import contextlib
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from . import orders as orders_mod
from . import url_guard
from .orders import customer_code
from .security import new_token, user_hash, verify_password, verify_zalo_webhook

LOOKUP_COMMANDS = {"/id", "id", "/ma", "/mã", "mã", "/tracuu", "/tra", "/tradon"}
MAX_IMPORT_BODY = 5 * 1024 * 1024
PUBLIC_UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "public_ui")
PUBLIC_FILES = {"": "index.html", "app.js": "app.js", "styles.css": "styles.css"}

MAX_WEBHOOK_BODY = 64 * 1024
MAX_ADMIN_BODY = 16 * 1024
SESSION_COOKIE = "aff_admin"
SESSION_TTL_S = 8 * 3600
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_S = 15 * 60
HANDLED_EVENTS = {"user_send_text", "user_send_link"}
ADMIN_UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin_ui")

ADMIN_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
             "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        h = response.headers
        h["X-Content-Type-Options"] = "nosniff"
        h["Referrer-Policy"] = "no-referrer"
        h["X-Frame-Options"] = "DENY"
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        path = request.url.path
        if path.startswith(("/admin", "/tra-don", "/api/")):
            h["Content-Security-Policy"] = ADMIN_CSP
            h["Cache-Control"] = "no-store"
        if request.url.scheme == "https":
            h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


async def read_limited(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and (not declared.isdigit() or int(declared) > limit):
        raise HTTPException(413)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(413)
    return bytes(body)


def collect_text(event: dict) -> str:
    msg = event.get("message") or {}
    parts = [str(msg.get("text") or "")]
    for att in msg.get("attachments") or []:
        payload = (att or {}).get("payload") or {}
        parts.append(str(payload.get("url") or ""))
    return "\n".join(p for p in parts if p)[:4000]


def create_app(ctx, background=(), include_webhook=True, include_admin=True) -> FastAPI:
    """ctx: AppContext (xem main.py). background: các hàm async(stop_event) chạy kèm (worker, bảo trì).
    Chạy thật nên tách 2 app trên 2 cổng: webhook (ra Internet qua tunnel) và admin (chỉ nội bộ)."""
    s = ctx.settings

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        stop = asyncio.Event()
        tasks = [asyncio.create_task(fn(stop)) for fn in background]
        yield
        stop.set()
        for t in tasks:
            t.cancel()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(SecurityHeaders)
    networks = [ipaddress.ip_network(c, strict=False) for c in s.admin_allowed_cidrs]

    # ── Webhook Zalo ──────────────────────────────────────────────
    @app.post("/webhook/zalo/{path_secret}")
    async def zalo_webhook(path_secret: str, request: Request):
        if not hmac.compare_digest(path_secret, s.webhook_path_secret):
            raise HTTPException(404)
        raw = await read_limited(request, MAX_WEBHOOK_BODY)
        ok, reason, event = verify_zalo_webhook(raw, request.headers.get("x-zevent-signature", ""),
                                                s.zalo_app_id, s.zalo_oa_secret, s.webhook_max_skew_s)
        if not ok:
            ctx.counters[f"webhook_{reason}"] += 1
            raise HTTPException(403)
        if event.get("event_name") not in HANDLED_EVENTS:
            return {"ok": True}
        sender = str((event.get("sender") or {}).get("id") or "")
        recipient = str((event.get("recipient") or {}).get("id") or "")
        if not sender or (s.zalo_oa_id and recipient != s.zalo_oa_id):
            return {"ok": True}
        msg_id = str((event.get("message") or {}).get("msg_id") or hashlib.sha256(raw).hexdigest())
        if not await ctx.store.set_once(f"seen:{msg_id}", 86400):
            ctx.counters["webhook_duplicate"] += 1
            return {"ok": True}

        uh = user_hash(sender, s.user_hash_key)
        job = {"user_id": sender, "user_hash": uh, "code": customer_code(sender, s.user_hash_key),
               "ts": int(time.time())}
        if await ctx.store.get("cfg:bot_paused") == "1":
            if await ctx.store.set_once(f"notice:paused:{uh}", 3600):
                await ctx.store.enqueue({**job, "kind": "paused"})
            return {"ok": True}

        text = collect_text(event)
        if text.strip().lower() in LOOKUP_COMMANDS:
            # Lệnh /id: tối đa 5 lần/giờ để không bị dùng làm công cụ spam tin nhắn
            if await ctx.store.incr_window(f"rl:id:{uh}:{int(time.time() // 3600)}", 7200) <= 5:
                await ctx.store.enqueue({**job, "kind": "lookup_id"})
            return {"ok": True}

        urls = url_guard.extract_urls(text, s.max_links_per_msg)
        if not urls:
            if await ctx.store.set_once(f"notice:help:{uh}", 86400):
                await ctx.store.enqueue({**job, "kind": "help"})
            return {"ok": True}

        allowed, why = await ctx.limiter.allow(uh, cost=len(urls))
        if not allowed:
            await ctx.audit_async("record", uh, None, None, "rate_limited", why, None, "-")
            if await ctx.store.set_once(f"notice:rl:{uh}", 600):
                await ctx.store.enqueue({**job, "kind": "rate_limited"})
            return {"ok": True}
        if await ctx.store.queue_len() >= s.queue_max:
            ctx.counters["queue_full"] += 1
            await ctx.store.enqueue({**job, "kind": "busy"})
            return {"ok": True}
        await ctx.store.enqueue({**job, "kind": "links", "urls": urls})
        return {"ok": True}

    # Tệp xác minh tên miền mà Zalo yêu cầu khi đăng ký webhook (tùy chọn)
    verifier_name = os.environ.get("ZALO_VERIFIER_FILENAME", "")
    if verifier_name:
        verifier_body = os.environ.get("ZALO_VERIFIER_CONTENT", "")

        @app.get("/" + verifier_name)
        async def zalo_verifier():
            return PlainTextResponse(verifier_body, media_type="text/html")

    # ── Tra cứu đơn cho khách (cổng public) ──────────────────────
    def visitor_ip(request: Request) -> str:
        # Sau Cloudflare Tunnel mọi kết nối đều từ 127.0.0.1; IP thật nằm ở CF-Connecting-IP.
        # Chỉ tin header này khi đã bật TRUST_CF_HEADER (cổng public không nhận kết nối trực tiếp).
        if s.trust_cf_header:
            try:
                return str(ipaddress.ip_address(request.headers.get("cf-connecting-ip", "").strip()))
            except ValueError:
                pass
        return request.client.host if request.client else "unknown"

    @app.get("/tra-don")
    async def lookup_root():
        return Response(status_code=307, headers={"Location": "/tra-don/"})

    @app.get("/tra-don/{name:path}")
    async def lookup_page(name: str):
        filename = PUBLIC_FILES.get(name)       # chỉ phục vụ đúng 3 file, không đọc đường dẫn tùy ý
        if not filename:
            raise HTTPException(404)
        return FileResponse(os.path.join(PUBLIC_UI_DIR, filename))

    @app.post("/api/tra-cuu")
    async def lookup(request: Request):
        ip = visitor_ip(request)
        now = time.time()
        minute, day = int(now // 60), time.strftime("%Y%m%d", time.gmtime(now))
        if await ctx.store.incr_window(f"rl:lk:g:{minute}", 120) > 600:
            raise HTTPException(429, "Hệ thống đang bận, bạn thử lại sau ít phút nhé")
        if (await ctx.store.incr_window(f"rl:lk:m:{ip}:{minute}", 120) > s.lookup_per_ip_per_min or
                await ctx.store.incr_window(f"rl:lk:d:{ip}:{day}", 2 * 86400) > s.lookup_per_ip_per_day):
            ctx.counters["lookup_rate_limited"] += 1
            raise HTTPException(429, "Bạn tra cứu quá nhiều lần, vui lòng thử lại sau ít phút")
        raw = await read_limited(request, 1024)
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise HTTPException(400, "Yêu cầu không hợp lệ") from None
        kind, value = orders_mod.normalize_query(data.get("q") if isinstance(data, dict) else "")
        if not kind:
            raise HTTPException(400, "Mã không hợp lệ. Nhập mã tra cứu 10 ký tự (nhắn /id cho shop để lấy) "
                                     "hoặc mã đơn hàng Shopee.")
        rows = await asyncio.to_thread(ctx.orders.by_code if kind == "code" else ctx.orders.by_order_id, value)
        ctx.counters["lookup"] += 1
        return {"kind": kind, "cashback_enabled": bool(s.cashback_percent),
                "orders": [orders_mod.public_view(o, s.cashback_percent) for o in rows]}

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    # ── Quản trị ──────────────────────────────────────────────────
    def client_ip(request: Request) -> str:
        return request.client.host if request.client else ""

    def ip_allowed(ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in n for n in networks)

    @app.middleware("http")
    async def admin_gate(request: Request, call_next):
        if request.url.path.startswith("/admin") and not ip_allowed(client_ip(request)):
            return PlainTextResponse("Not found", status_code=404)
        return await call_next(request)

    def _sid_key(token: str) -> str:
        return "sess:" + hashlib.sha256(token.encode()).hexdigest()

    async def current_session(request: Request):
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        raw = await ctx.store.get(_sid_key(token))
        return json.loads(raw) if raw else None

    async def require_admin(request: Request, mutate: bool = False):
        sess = await current_session(request)
        if not sess:
            raise HTTPException(401, "Phiên đăng nhập đã hết, vui lòng đăng nhập lại")
        if mutate and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), sess["csrf"]):
            raise HTTPException(403, "Thiếu hoặc sai mã CSRF")
        return sess

    async def admin_json(request: Request) -> dict:
        raw = await read_limited(request, MAX_ADMIN_BODY)
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise HTTPException(400, "Dữ liệu gửi lên không phải JSON") from None
        if not isinstance(data, dict):
            raise HTTPException(400, "Dữ liệu gửi lên không hợp lệ")
        return data

    @app.post("/admin/api/login")
    async def login(request: Request):
        if not s.admin_password_hash:
            raise HTTPException(403, "Chưa đặt mật khẩu quản trị (ADMIN_PASSWORD_HASH)")
        ip = client_ip(request)
        attempts = await ctx.store.incr_window(f"login:{ip}", LOGIN_WINDOW_S)
        if attempts > LOGIN_MAX_ATTEMPTS:
            raise HTTPException(429, "Đăng nhập sai quá nhiều lần, thử lại sau 15 phút")
        data = await admin_json(request)
        password = str(data.get("password", ""))[:256]
        if not verify_password(password, s.admin_password_hash):
            await ctx.audit_async("admin_event", ip, "login_failed")
            raise HTTPException(401, "Sai mật khẩu")
        token, csrf = new_token(), new_token(16)
        await ctx.store.set(_sid_key(token), json.dumps({"csrf": csrf, "ip": ip, "at": int(time.time())}),
                            SESSION_TTL_S)
        await ctx.store.delete(f"login:{ip}")
        await ctx.audit_async("admin_event", ip, "login")
        resp = JSONResponse({"ok": True, "csrf_token": csrf})
        resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL_S, httponly=True, secure=s.cookie_secure,
                        samesite="strict", path="/admin")
        return resp

    @app.post("/admin/api/logout")
    async def logout(request: Request):
        await require_admin(request, mutate=True)
        await ctx.store.delete(_sid_key(request.cookies.get(SESSION_COOKIE, "")))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/admin")
        return resp

    @app.get("/admin/api/session")
    async def session_info(request: Request):
        sess = await current_session(request)
        return {"authenticated": bool(sess), "csrf_token": sess["csrf"] if sess else None,
                "login_enabled": bool(s.admin_password_hash)}

    @app.get("/admin/api/overview")
    async def overview(request: Request):
        await require_admin(request)
        return {
            "bot_enabled": await ctx.store.get("cfg:bot_paused") != "1",
            "queue_len": await ctx.store.queue_len(),
            "dlq_len": await ctx.store.dlq_len(),
            "adapters": ctx.router.status(),
            "browser": ctx.browser.status() if ctx.browser else None,
            "zalo_token": ctx.tokens.info(),
            "today": await ctx.audit_async("stats", 1),
            "security_counters": dict(ctx.counters),
            "alerts": list(ctx.alerts),
        }

    @app.get("/admin/api/stats")
    async def stats(request: Request, days: int = 7):
        await require_admin(request)
        return await ctx.audit_async("stats", max(1, min(days, 90)))

    @app.get("/admin/api/conversions")
    async def conversions(request: Request, limit: int = 50):
        await require_admin(request)
        return {"items": await ctx.audit_async("recent", max(1, min(limit, 200)))}

    @app.get("/admin/api/dlq")
    async def dlq(request: Request, limit: int = 50):
        await require_admin(request)
        return {"count": await ctx.store.dlq_len(), "items": await ctx.store.dlq_items(max(1, min(limit, 200)))}

    @app.post("/admin/api/dlq/clear")
    async def dlq_clear(request: Request):
        await require_admin(request, mutate=True)
        await ctx.store.dlq_clear()
        await ctx.audit_async("admin_event", client_ip(request), "dlq_clear")
        return {"ok": True}

    @app.post("/admin/api/bot")
    async def set_bot(request: Request):
        await require_admin(request, mutate=True)
        enabled = bool((await admin_json(request)).get("enabled"))
        await ctx.store.set("cfg:bot_paused", "0" if enabled else "1")
        await ctx.audit_async("admin_event", client_ip(request), "bot_enable" if enabled else "bot_pause")
        return {"ok": True, "bot_enabled": enabled}

    @app.post("/admin/api/shopee-web-session")
    async def set_web_session(request: Request):
        """Chỉ ghi. Cookie không bao giờ được trả về qua API."""
        await require_admin(request, mutate=True)
        if not ctx.web_adapter:
            raise HTTPException(409, "Phương án 2 đang tắt (SHOPEE_WEB_FALLBACK=0)")
        data = await admin_json(request)
        cookie = str(data.get("cookie", "")).strip()
        csrf = str(data.get("csrf_token", "")).strip()
        if not (20 <= len(cookie) <= 8000) or any(c in cookie for c in "\r\n"):
            raise HTTPException(400, "Cookie không hợp lệ")
        if len(csrf) > 200 or any(c in csrf for c in "\r\n"):
            raise HTTPException(400, "csrf_token không hợp lệ")
        ctx.web_adapter.set_session(cookie, csrf)
        await ctx.audit_async("admin_event", client_ip(request), "web_session_update")
        return {"ok": True, "session": ctx.web_adapter.session_info()}

    @app.post("/admin/api/shopee-browser/open")
    async def browser_open(request: Request):
        """Mở cửa sổ Chrome của bot tới affiliate.shopee.vn để chủ shop tự đăng nhập."""
        await require_admin(request, mutate=True)
        if not ctx.browser:
            raise HTTPException(409, "Chưa bật SHOPEE_BROWSER=1")
        try:
            status = await ctx.browser.open()
        except Exception as e:
            raise HTTPException(502, str(e)[:200] if "CHROME_PATH" in str(e) else "Không mở được Chrome") from None
        await ctx.audit_async("admin_event", client_ip(request), "browser_open")
        return {"ok": True, "browser": status}

    @app.post("/admin/api/shopee-browser/sync")
    async def browser_sync(request: Request):
        await require_admin(request, mutate=True)
        if not ctx.browser:
            raise HTTPException(409, "Chưa bật SHOPEE_BROWSER=1")
        result = await ctx.browser.sync()
        await ctx.audit_async("admin_event", client_ip(request), "browser_sync", result[:40])
        return {"ok": True, "result": result, "browser": ctx.browser.status(), "adapters": ctx.router.status()}

    @app.post("/admin/api/adapters/{name}/reset")
    async def reset_adapter(name: str, request: Request):
        await require_admin(request, mutate=True)
        adapter = {a.name: a for a in (ctx.router.shopee_openapi, ctx.router.shopee_web, ctx.router.tiktok) if a}.get(name)
        if not adapter:
            raise HTTPException(404, "Không có adapter này")
        adapter.breaker.reset()
        await ctx.audit_async("admin_event", client_ip(request), "adapter_reset", name)
        return {"ok": True, "adapters": ctx.router.status()}

    @app.get("/admin/api/settings")
    async def settings_view(request: Request):
        await require_admin(request)
        return {
            "shopee_mode": "openapi" if ctx.router.shopee_openapi else "web_session",
            "sub_id_channel": s.sub_id_channel, "sub_id_campaign": s.sub_id_campaign,
            "rate_limits": {"per_user_per_min": s.per_user_per_min, "per_user_per_day": s.per_user_per_day,
                            "global_per_min": s.global_per_min, "max_links_per_msg": s.max_links_per_msg,
                            "web_fallback_per_min": s.web_fallback_per_min},
            "log_retention_days": s.log_retention_days,
            "redis": bool(s.redis_url),
        }

    @app.get("/admin/api/audit-events")
    async def audit_events(request: Request, limit: int = 50):
        await require_admin(request)
        return {"items": await ctx.audit_async("recent_admin_events", max(1, min(limit, 200)))}

    @app.get("/admin/api/orders")
    async def admin_orders(request: Request, limit: int = 50):
        await require_admin(request)
        summary = await asyncio.to_thread(ctx.orders.summary)
        items = await asyncio.to_thread(ctx.orders.recent, max(1, min(limit, 500)))
        return {"summary": summary, "items": items, "cashback_percent": s.cashback_percent,
                "lookup_page_url": s.lookup_page_url, "can_sync": bool(ctx.router.shopee_openapi)}

    @app.post("/admin/api/orders/import")
    async def import_orders(request: Request):
        await require_admin(request, mutate=True)
        raw = await read_limited(request, MAX_IMPORT_BODY)
        try:
            text = str(json.loads(raw).get("csv", ""))
        except (ValueError, AttributeError):
            raise HTTPException(400, "Dữ liệu gửi lên không hợp lệ") from None
        found, info = orders_mod.parse_conversion_csv(text)
        if info["missing"]:
            raise HTTPException(400, "File thiếu cột bắt buộc: " + ", ".join(
                {"order_id": "Mã đơn hàng (Order ID)", "status": "Trạng thái đơn (Order Status)"}[m] for m in info["missing"]))
        n = await asyncio.to_thread(ctx.orders.upsert, found, "csv")
        matched = sum(1 for o in found if o["code"])
        await asyncio.to_thread(ctx.orders.set_meta, "last_sync",
                                {"ts": int(time.time()), "source": "csv", "count": n, "matched": matched})
        await ctx.audit_async("admin_event", client_ip(request), "orders_import", f"{n} đơn")
        return {"ok": True, "imported": n, "matched": matched, "rows": info["rows"], "skipped": info["skipped"]}

    @app.post("/admin/api/orders/sync")
    async def sync_orders(request: Request):
        await require_admin(request, mutate=True)
        if not ctx.router.shopee_openapi:
            raise HTTPException(409, "Đồng bộ tự động cần Shopee Open API. Với Phương án 2, hãy tải file CSV báo cáo lên.")
        result = await ctx.sync_orders()
        await ctx.audit_async("admin_event", client_ip(request), "orders_sync", f"{result['imported']} đơn")
        return result

    # Giao diện tĩnh (Gemini xây dựng trong admin_ui/)
    @app.get("/admin")
    async def admin_root():
        return Response(status_code=307, headers={"Location": "/admin/"})

    @app.get("/admin/{path:path}")
    async def admin_ui(path: str):
        target = os.path.realpath(os.path.join(ADMIN_UI_DIR, path or "index.html"))
        if os.path.isdir(target):   # thư mục con (vd /admin/don-gian/) dùng index.html của chính nó
            target = os.path.join(target, "index.html")
        inside = os.path.commonpath([target, os.path.realpath(ADMIN_UI_DIR)]) == os.path.realpath(ADMIN_UI_DIR)
        if path.startswith("api/"):
            raise HTTPException(404)
        if not inside or not os.path.isfile(target):
            target = os.path.join(ADMIN_UI_DIR, "index.html")
        if not os.path.isfile(target):
            return Response("Giao diện quản trị chưa được cài đặt", status_code=404)
        return FileResponse(target)

    # Bỏ hẳn các route không thuộc vai trò của cổng này (không chỉ chặn bằng IP)
    def keep(route):
        path = getattr(route, "path", "")
        is_admin = path.startswith("/admin")
        # Cổng public: webhook Zalo + trang tra đơn cho khách
        is_webhook = (path.startswith(("/webhook", "/tra-don")) or path == "/api/tra-cuu"
                      or (verifier_name and path == "/" + verifier_name))
        return (include_admin or not is_admin) and (include_webhook or not is_webhook)
    app.router.routes = [r for r in app.router.routes if keep(r)]
    return app
