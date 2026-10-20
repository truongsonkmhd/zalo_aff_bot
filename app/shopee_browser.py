"""
Phương án 2 — bot tự lấy phiên từ cửa sổ Chrome của chủ shop.

Bot mở Chrome thật với hồ sơ riêng (DATA_DIR/chrome_shopee) tới affiliate.shopee.vn. Chủ shop tự đăng
nhập trong cửa sổ đó một lần (mật khẩu, OTP, captcha do người làm). Sau đó bot tự đọc cookie + csrf
của phiên qua DevTools Protocol (chỉ nghe 127.0.0.1) và nạp vào Phương án 2; cookie đổi hoặc Shopee từ
chối phiên thì tải lại trang và đọc lại.

Giới hạn cố ý: không lưu mật khẩu, không tự điền form đăng nhập, không giải captcha, không giấu dấu
hiệu tự động hóa, không đổi link bằng cách điều khiển trang. Cookie chỉ nằm trong bộ nhớ rồi được
mã hóa qua SecretStore như khi dán tay.
"""

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time

import httpx

from .adapters import session_fingerprint as fingerprint

AFFILIATE_URL = "https://affiliate.shopee.vn/offer/custom_link"
LOGIN_COOKIE = "SPC_EC"          # có cookie này (khác rỗng) = đã đăng nhập
CSRF_COOKIE = "csrftoken"        # giá trị gửi kèm header csrf-token
_COOKIE_DOMAINS = {"affiliate.shopee.vn", ".affiliate.shopee.vn", ".shopee.vn"}
_BAD_CHARS = set(";\r\n")


class CdpError(Exception):
    pass


def find_chrome(explicit: str = "") -> str:
    """Đường dẫn Chrome (hoặc Edge) trên máy; rỗng nếu không thấy."""
    if explicit:
        return explicit if os.path.isfile(explicit) else ""
    candidates = []
    if sys.platform == "win32":
        for base in (os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                     os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                     os.environ.get("LOCALAPPDATA", "")):
            if base:
                candidates += [os.path.join(base, r"Google\Chrome\Application\chrome.exe"),
                               os.path.join(base, r"Microsoft\Edge\Application\msedge.exe")]
    elif sys.platform == "darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                      "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    else:
        candidates = [shutil.which(n) or "" for n in ("google-chrome", "google-chrome-stable", "chromium",
                                                      "chromium-browser", "microsoft-edge")]
    return next((c for c in candidates if c and os.path.isfile(c)), "")


def session_from_cookies(cookies, now=None):
    """Từ danh sách cookie CDP → (chuỗi header Cookie, csrf, đã đăng nhập?). Chỉ lấy cookie áp dụng cho
    affiliate.shopee.vn, bỏ cookie hết hạn và giá trị có ký tự phá header."""
    now = time.time() if now is None else now
    jar = {}
    for c in cookies or []:
        name, value, domain = str(c.get("name", "")), str(c.get("value", "")), str(c.get("domain", ""))
        expires = c.get("expires", -1) or -1
        if domain not in _COOKIE_DOMAINS or not name or (expires > 0 and expires < now):
            continue
        if _BAD_CHARS & set(name + value) or "=" in name or " " in name:
            continue
        # Cookie riêng của affiliate.shopee.vn được ưu tiên hơn cookie chung .shopee.vn cùng tên
        if name not in jar or "affiliate" in domain:
            jar[name] = value
    header = "; ".join(f"{k}={jar[k]}" for k in sorted(jar))
    return header, jar.get(CSRF_COOKIE, ""), bool(jar.get(LOGIN_COOKIE))


class CdpClient:
    """Gọi Chrome DevTools Protocol qua 127.0.0.1. Mỗi lệnh một kết nối ngắn."""

    def __init__(self, timeout: float = 5.0):
        self._timeout = timeout

    async def _json(self, port: int, path: str):
        async with httpx.AsyncClient(timeout=self._timeout, trust_env=False) as client:
            resp = await client.get(f"http://127.0.0.1:{port}{path}")
        if resp.status_code != 200:
            raise CdpError(f"http_{resp.status_code}")
        return resp.json()

    async def _call(self, ws_url: str, method: str, params=None):
        import websockets
        if not ws_url.startswith("ws://127.0.0.1:"):
            raise CdpError("ws_not_local")
        async with websockets.connect(ws_url, max_size=16 * 1024 * 1024, open_timeout=self._timeout,
                                      proxy=None) as ws:
            await ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            async with asyncio.timeout(self._timeout):
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == 1:
                        if "error" in msg:
                            raise CdpError(str(msg["error"].get("message", "cdp_error"))[:100])
                        return msg.get("result", {})

    async def get_cookies(self, port: int):
        ws = (await self._json(port, "/json/version"))["webSocketDebuggerUrl"]
        return (await self._call(ws, "Storage.getCookies")).get("cookies", [])

    async def affiliate_tab(self, port: int):
        for t in await self._json(port, "/json/list"):
            if t.get("type") == "page" and str(t.get("url", "")).startswith("https://affiliate.shopee.vn"):
                return t
        return None

    async def open_tab(self, port: int, url: str):
        ws = (await self._json(port, "/json/version"))["webSocketDebuggerUrl"]
        await self._call(ws, "Target.createTarget", {"url": url})

    async def reload(self, port: int) -> bool:
        tab = await self.affiliate_tab(port)
        if not tab:
            await self.open_tab(port, AFFILIATE_URL)
            return True
        await self._call(tab["webSocketDebuggerUrl"], "Page.reload")
        return True


class ChromeLauncher:
    """Chrome với hồ sơ riêng của bot, cổng DevTools ngẫu nhiên chỉ trên 127.0.0.1."""

    def __init__(self, profile_dir: str, chrome_path: str = ""):
        self.profile_dir, self.chrome_path = profile_dir, chrome_path

    def port(self):
        """Cổng DevTools Chrome ghi ra khi chạy (cổng 0 = hệ điều hành chọn cổng trống)."""
        try:
            with open(os.path.join(self.profile_dir, "DevToolsActivePort"), encoding="ascii") as f:
                port = int(f.readline().strip())
        except (OSError, ValueError):
            return None
        return port if 0 < port < 65536 else None

    def launch(self, url: str):
        exe = find_chrome(self.chrome_path)
        if not exe:
            raise CdpError("Không tìm thấy Chrome/Edge. Đặt CHROME_PATH trỏ tới chrome.exe.")
        os.makedirs(self.profile_dir, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.profile_dir, 0o700)
        with contextlib.suppress(OSError):
            os.remove(os.path.join(self.profile_dir, "DevToolsActivePort"))
        subprocess.Popen(
            [exe, f"--user-data-dir={os.path.abspath(self.profile_dir)}", "--remote-debugging-address=127.0.0.1",
             "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check", "--new-window", url],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


class ShopeeBrowserSession:
    def __init__(self, web_adapter, profile_dir: str, chrome_path: str = "", alert=None,
                 keepalive_s: int = 6 * 3600, cdp=None, launcher=None, clock=time.time):
        self._adapter, self._alert, self._clock = web_adapter, alert, clock
        self._cdp = cdp or CdpClient()
        self._launcher = launcher or ChromeLauncher(profile_dir, chrome_path)
        self._keepalive_s = keepalive_s
        self._last_reload = clock()
        self._last_retry = 0.0
        self._alerted = {}
        self._status = {"browser": "closed", "logged_in": False, "checked_at": None, "synced_at": None,
                        "last_result": None}

    def status(self):
        return dict(self._status)

    async def _say(self, key: str, text: str, every_s: int = 3600):
        """Cảnh báo, mỗi loại tối đa một lần mỗi giờ."""
        now = self._clock()
        if now - self._alerted.get(key, 0) < every_s:
            return
        self._alerted[key] = now
        if self._alert:
            await self._alert(text)

    async def _reachable_port(self):
        port = self._launcher.port()
        if not port:
            return None
        try:
            await self._cdp._json(port, "/json/version")
        except (CdpError, httpx.HTTPError, ValueError, OSError):
            return None
        return port

    async def open(self):
        """Mở cửa sổ Chrome của bot (hoặc thêm thẻ affiliate nếu Chrome đang mở)."""
        port = await self._reachable_port()
        if port:
            if not await self._cdp.affiliate_tab(port):
                await self._cdp.open_tab(port, AFFILIATE_URL)
        else:
            await asyncio.to_thread(self._launcher.launch, AFFILIATE_URL)
            for _ in range(40):          # chờ Chrome ghi cổng DevTools, tối đa ~10 giây
                await asyncio.sleep(0.25)
                if await self._reachable_port():
                    break
        self._status["browser"] = "open" if await self._reachable_port() else "closed"
        return self.status()

    def _set(self, result: str):
        self._status.update(last_result=result, checked_at=int(self._clock()))
        return result

    async def _read(self, port):
        cookie, csrf, logged_in = session_from_cookies(await self._cdp.get_cookies(port), self._clock())
        self._status.update(browser="open", logged_in=logged_in)
        return cookie, csrf, logged_in

    async def sync(self) -> str:
        """Đọc phiên từ Chrome và nạp vào Phương án 2 khi có thay đổi. Trả về kết quả ngắn."""
        port = await self._reachable_port()
        if not port:
            self._status.update(browser="closed", logged_in=False)
            if self._adapter.breaker.hard_reason or not self._adapter.session_info().get("configured"):
                await self._say("closed", "Cửa sổ Chrome lấy phiên Shopee đang đóng. Vào trang quản trị → "
                                          "Kết nối Shopee → bấm “Mở Chrome đăng nhập Shopee”.")
            return self._set("browser_closed")
        try:
            cookie, csrf, logged_in = await self._read(port)
            stopped = bool(self._adapter.breaker.hard_reason)
            now = self._clock()
            # Phiên bị Shopee từ chối mà cookie chưa đổi: tải lại trang như người dùng bấm F5, rồi đọc lại
            if logged_in and stopped and fingerprint(cookie, csrf) == self._adapter.session_fingerprint() \
                    and now - self._last_retry > 600:
                self._last_retry = self._last_reload = now
                await self._cdp.reload(port)
                await asyncio.sleep(5)
                cookie, csrf, logged_in = await self._read(port)
            elif now - self._last_reload > self._keepalive_s:
                self._last_reload = now          # giữ phiên sống: tải lại trang affiliate định kỳ
                await self._cdp.reload(port)
        except (CdpError, httpx.HTTPError, ValueError, KeyError, OSError, asyncio.TimeoutError) as e:
            self._status["browser"] = "error"
            return self._set(f"cdp_error:{type(e).__name__}")
        except Exception as e:                   # lỗi websocket (ConnectionClosed…) không được làm dừng bot
            self._status["browser"] = "error"
            return self._set(f"cdp_error:{type(e).__name__}")

        if not logged_in:
            await self._say("login", "Chưa đăng nhập affiliate.shopee.vn trong cửa sổ Chrome của bot. "
                                     "Hãy đăng nhập trong cửa sổ đó, bot sẽ tự lấy phiên.")
            return self._set("not_logged_in")
        if not (20 <= len(cookie) <= 8000):
            return self._set("cookie_invalid")
        if fingerprint(cookie, csrf) == self._adapter.session_fingerprint():
            if self._adapter.breaker.hard_reason:
                await self._say("rejected", "Shopee từ chối phiên dù đã tải lại trang. Mở cửa sổ Chrome của bot "
                                            "kiểm tra (có thể Shopee yêu cầu xác minh hoặc đăng nhập lại).")
                return self._set("rejected")
            return self._set("unchanged")
        was_stopped = bool(self._adapter.breaker.hard_reason)
        self._adapter.set_session(cookie, csrf)
        self._status["synced_at"] = int(self._clock())
        if was_stopped:
            await self._say("resumed", "Đã tự lấy phiên Shopee mới từ Chrome, bot đổi link lại bình thường.", 60)
        return self._set("updated")

    async def run(self, stop: asyncio.Event, every_s: int = 60, open_on_start: bool = True):
        if open_on_start:
            with contextlib.suppress(Exception):
                await self.open()
        while not stop.is_set():
            await self.sync()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=every_s)
