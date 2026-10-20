"""
Zalo Official Account: gửi tin tư vấn và làm mới access token.
Refresh token của Zalo chỉ dùng được một lần, nên việc làm mới đặt sau khóa phân tán và
token mới được ghi ngay vào SecretStore (mã hóa).
"""

import asyncio
import time

import httpx

SEND_URL = "https://openapi.zalo.me/v3.0/oa/message/cs"
TOKEN_URL = "https://oauth.zaloapp.com/v4/oa/access_token"
TOKEN_KEY = "zalo_tokens"
REFRESH_MARGIN_S = 600


class ZaloError(Exception):
    def __init__(self, code: str, transient: bool):
        super().__init__(code)
        self.code = code
        self.transient = transient


class TokenManager:
    def __init__(self, app_id, app_secret, secret_store, store, initial_refresh_token="", transport=None):
        self._app_id, self._app_secret = app_id, app_secret
        self._secrets, self._store = secret_store, store
        self._transport = transport
        self._local_lock = asyncio.Lock()
        if initial_refresh_token and not self._secrets.get(TOKEN_KEY):
            # Lần chạy đầu: chỉ có refresh token → sẽ làm mới ngay ở lần gửi đầu tiên
            self._secrets.put(TOKEN_KEY, {"access_token": "", "refresh_token": initial_refresh_token, "expires_at": 0})

    def info(self):
        t = self._secrets.get(TOKEN_KEY) or {}
        return {"configured": bool(t.get("refresh_token")),
                "expires_in_s": max(0, int(t.get("expires_at", 0) - time.time())) if t.get("access_token") else 0}

    async def access_token(self, force_refresh=False) -> str:
        t = self._secrets.get(TOKEN_KEY) or {}
        if t.get("access_token") and not force_refresh and t.get("expires_at", 0) - time.time() > REFRESH_MARGIN_S:
            return t["access_token"]
        async with self._local_lock:
            t = self._secrets.get(TOKEN_KEY) or {}
            if t.get("access_token") and not force_refresh and t.get("expires_at", 0) - time.time() > REFRESH_MARGIN_S:
                return t["access_token"]
            lock = await self._store.acquire_lock("lock:zalo_refresh", 30)
            if not lock:
                # Tiến trình khác đang làm mới: đợi rồi đọc lại
                await asyncio.sleep(2)
                t = self._secrets.get(TOKEN_KEY) or {}
                if t.get("access_token"):
                    return t["access_token"]
                raise ZaloError("token_refresh_busy", transient=True)
            try:
                return await self._refresh(t)
            finally:
                await self._store.release_lock("lock:zalo_refresh", lock)

    async def _refresh(self, t) -> str:
        refresh_token = t.get("refresh_token")
        if not refresh_token:
            raise ZaloError("no_refresh_token", transient=False)
        try:
            async with httpx.AsyncClient(timeout=10, transport=self._transport) as client:
                resp = await client.post(TOKEN_URL, headers={"secret_key": self._app_secret}, data={
                    "refresh_token": refresh_token, "app_id": self._app_id, "grant_type": "refresh_token"})
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            raise ZaloError("token_refresh_network", transient=True) from None
        if not data.get("access_token") or not data.get("refresh_token"):
            raise ZaloError("token_refresh_rejected", transient=False)
        expires_in = int(data.get("expires_in") or 3600)
        self._secrets.put(TOKEN_KEY, {"access_token": data["access_token"], "refresh_token": data["refresh_token"],
                                      "expires_at": time.time() + expires_in})
        return data["access_token"]


class ZaloClient:
    def __init__(self, tokens: TokenManager, transport=None):
        self._tokens = tokens
        self._transport = transport

    async def send_text(self, user_id: str, text: str):
        for attempt in range(2):
            token = await self._tokens.access_token(force_refresh=attempt > 0)
            try:
                async with httpx.AsyncClient(timeout=10, transport=self._transport) as client:
                    resp = await client.post(SEND_URL, headers={"access_token": token, "Content-Type": "application/json"},
                                             json={"recipient": {"user_id": user_id}, "message": {"text": text[:2000]}})
                data = resp.json()
            except (httpx.HTTPError, ValueError):
                raise ZaloError("send_network", transient=True) from None
            error = int(data.get("error", -1))
            if error == 0:
                return
            if error == -216 and attempt == 0:      # access token không hợp lệ/hết hạn → làm mới một lần
                continue
            raise ZaloError(f"zalo_{error}", transient=resp.status_code >= 500)
        raise ZaloError("zalo_token_invalid", transient=False)
