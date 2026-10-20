"""
Khung chat thử bot — KHÔNG dùng cho chạy thật. Giống Zalo trên trình duyệt: gõ tin như khách, bot trả lời ngay.
Toàn bộ logic bot là code thật (webhook ký chữ ký, URL Guard, hạn mức, pipeline); Zalo/Shopee/DNS giả lập.
  python -m app.chatsim
    Khung chat: http://127.0.0.1:8093/
Link rút gọn giả lập có sẵn: xem SHORT_LINKS trong app/simulate.py.
"""

import asyncio
import contextlib
import json
import os
import sys

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from .main import _serve
from .simulate import SHORT_LINKS, Sim
from .web import ADMIN_CSP, SecurityHeaders, read_limited

CHAT_UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chat_ui")
CHAT_FILES = {"": "index.html", "app.js": "app.js", "styles.css": "styles.css"}
MAX_TEXT = 2000


def create_chat_app() -> FastAPI:
    state = {}

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        state["sim"] = Sim()
        yield
        await state["sim"].close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(SecurityHeaders)

    @app.middleware("http")
    async def csp(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = ADMIN_CSP
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/chat")
    async def chat(request: Request):
        try:
            data = json.loads(await read_limited(request, 8 * 1024) or b"{}")
        except ValueError:
            raise HTTPException(400, "Dữ liệu gửi lên không phải JSON") from None
        if not isinstance(data, dict):
            raise HTTPException(400, "Dữ liệu gửi lên không hợp lệ")
        text = str(data.get("text") or "").strip()[:MAX_TEXT]
        user = str(data.get("user") or "khach-001")[:64]
        if not text:
            raise HTTPException(400, "Tin nhắn trống")
        sim = state["sim"]
        secret = "chu-ky-gia" if data.get("fake_signature") else sim.settings.zalo_oa_secret
        status, replies = await sim.send(text, user, secret)
        return {"webhook_status": status, "replies": replies}

    @app.get("/api/info")
    async def info():
        sim = state["sim"]
        stats = await asyncio.to_thread(sim.ctx.audit.stats, 1)
        recent = await asyncio.to_thread(sim.ctx.audit.recent, 8)
        return {"short_links": list(SHORT_LINKS), "per_user_per_min": sim.settings.per_user_per_min,
                "stats": stats, "counters": dict(sim.ctx.counters),
                "recent": [{k: r[k] for k in ("ts", "user_hash", "item_key", "status", "error_code", "source")}
                           for r in recent]}

    @app.get("/{name:path}")   # khai báo sau cùng để không chặn /api/*
    async def static(name: str):
        filename = CHAT_FILES.get(name)
        if not filename:
            raise HTTPException(404)
        return FileResponse(os.path.join(CHAT_UI_DIR, filename))

    return app


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("Khung chat thử bot: http://127.0.0.1:8093/   (Zalo/Shopee giả lập, không gọi mạng)")
    asyncio.run(_serve([(create_chat_app(), "127.0.0.1", 8093)]))


if __name__ == "__main__":
    main()
