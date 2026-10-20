"""
Adapter đổi link affiliate.
  - ShopeeOpenApiAdapter: Phương án 1, Shopee Affiliate Open API (GraphQL, ký SHA256).
  - ShopeeWebSessionAdapter: Phương án 2, phiên đăng nhập affiliate.shopee.vn — chỉ dùng khi
    chưa có Open API. Gặp tín hiệu chặn là ngắt hẳn tới khi quản trị cập nhật phiên mới.
  - TikTokAdapter: chờ giai đoạn 3.
"""

import hashlib
import json
import re
import time

import httpx

from .url_guard import Product, is_safe_output_link

SHOPEE_OPENAPI_URL = "https://open-api.affiliate.shopee.vn/graphql"
SHOPEE_WEB_URL = "https://affiliate.shopee.vn/api/v3/gql?q=batchCustomLink"
_SUBID_RE = re.compile(r"[^A-Za-z0-9]")


class AdapterError(Exception):
    def __init__(self, code: str, transient: bool):
        super().__init__(code)
        self.code = code
        self.transient = transient


def session_fingerprint(cookie: str, csrf: str) -> str:
    return hashlib.sha256(f"{cookie}\n{csrf}".encode()).hexdigest()


def clean_sub_ids(values):
    """Shopee nhận tối đa 5 sub_id, chỉ chữ và số."""
    out = []
    for v in values:
        v = _SUBID_RE.sub("", str(v or ""))[:50]
        if v:
            out.append(v)
    return out[:5]


class CircuitBreaker:
    """Mở sau `threshold` lỗi liên tiếp, tự đóng thử lại sau `cooldown_s`.
    trip_hard(): mở vô thời hạn tới khi reset() (dùng cho phiên web bị chặn)."""

    def __init__(self, threshold=5, cooldown_s=60):
        self.threshold, self.cooldown_s = threshold, cooldown_s
        self.failures, self.open_until, self.hard_reason = 0, 0.0, None

    def allow(self):
        return self.hard_reason is None and time.time() >= self.open_until

    def success(self):
        self.failures = 0

    def failure(self):
        self.failures += 1
        if self.failures >= self.threshold:
            self.open_until = time.time() + self.cooldown_s
            self.failures = 0

    def trip_hard(self, reason):
        self.hard_reason = reason

    def reset(self):
        self.failures, self.open_until, self.hard_reason = 0, 0.0, None

    def state(self):
        if self.hard_reason:
            return {"state": "stopped", "reason": self.hard_reason}
        if time.time() < self.open_until:
            return {"state": "cooling", "seconds_left": int(self.open_until - time.time())}
        return {"state": "ok"}


class ShopeeOpenApiAdapter:
    name = "shopee_openapi"
    platform = "shopee"

    def __init__(self, app_id: str, app_secret: str, transport=None, timeout=8.0):
        self._app_id, self._secret = app_id, app_secret
        self._transport, self._timeout = transport, timeout
        self.breaker = CircuitBreaker()

    def _sign(self, payload: str, ts: int) -> str:
        sig = hashlib.sha256(f"{self._app_id}{ts}{payload}{self._secret}".encode("utf-8")).hexdigest()
        return f"SHA256 Credential={self._app_id}, Timestamp={ts}, Signature={sig}"

    async def _post(self, query: str) -> dict:
        payload = json.dumps({"query": query}, separators=(",", ":"))   # ký đúng chuỗi sẽ gửi
        headers = {"Content-Type": "application/json", "Authorization": self._sign(payload, int(time.time()))}
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            resp = await client.post(SHOPEE_OPENAPI_URL, content=payload, headers=headers)
        if resp.status_code != 200:
            raise AdapterError(f"http_{resp.status_code}", transient=resp.status_code == 429 or resp.status_code >= 500)
        data = resp.json()
        if data.get("errors"):
            code = str((data["errors"][0].get("extensions") or {}).get("code", "api_error"))
            raise AdapterError(f"shopee_{code}", transient=False)
        return data.get("data") or {}

    async def conversion_report(self, start_ts: int, end_ts: int, max_pages: int = 50):
        """Báo cáo chuyển đổi (đơn hàng) trong khoảng thời gian, đi hết các trang bằng scrollId."""
        from .orders import CONVERSION_QUERY
        nodes, scroll = [], ""
        for _ in range(max_pages):
            extra = f',scrollId:{json.dumps(scroll)}' if scroll else ""
            try:
                data = await self._post(CONVERSION_QUERY % (start_ts, end_ts, extra))
            except httpx.HTTPError:
                raise AdapterError("network", transient=True) from None
            report = data.get("conversionReport") or {}
            nodes.extend(report.get("nodes") or [])
            page = report.get("pageInfo") or {}
            scroll = page.get("scrollId") or ""
            if not page.get("hasNextPage") or not scroll:
                break
        return nodes

    async def generate(self, product: Product, sub_ids) -> str:
        if not self.breaker.allow():
            raise AdapterError("adapter_paused", transient=True)
        # Truy vấn nội tuyến như ví dụ trong tài liệu Open API. Giá trị đã an toàn: URL dựng từ chữ số,
        # sub_id chỉ chữ/số; json.dumps tạo chuỗi/danh sách hợp lệ theo cú pháp GraphQL.
        query = "mutation{generateShortLink(input:{originUrl:%s,subIds:%s}){shortLink}}" % (
            json.dumps(product.canonical_url), json.dumps(clean_sub_ids(sub_ids)))
        payload = json.dumps({"query": query}, separators=(",", ":"))   # ký đúng chuỗi sẽ gửi
        headers = {"Content-Type": "application/json", "Authorization": self._sign(payload, int(time.time()))}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                resp = await client.post(SHOPEE_OPENAPI_URL, content=payload, headers=headers)
        except httpx.HTTPError:
            self.breaker.failure()
            raise AdapterError("network", transient=True) from None
        if resp.status_code == 429 or resp.status_code >= 500:
            self.breaker.failure()
            raise AdapterError(f"http_{resp.status_code}", transient=True)
        if resp.status_code != 200:
            self.breaker.failure()
            raise AdapterError(f"http_{resp.status_code}", transient=False)
        try:
            data = resp.json()
        except ValueError:
            self.breaker.failure()
            raise AdapterError("bad_json", transient=True) from None
        if data.get("errors"):
            self.breaker.failure()
            code = str((data["errors"][0].get("extensions") or {}).get("code", "api_error"))
            # Không đoán mã lỗi nào là tạm thời: ghi mã lại để quản trị xem, không thử lại
            raise AdapterError(f"shopee_{code}", transient=False)
        link = (((data.get("data") or {}).get("generateShortLink")) or {}).get("shortLink", "")
        if not is_safe_output_link(link):
            self.breaker.failure()
            raise AdapterError("unexpected_link", transient=False)
        self.breaker.success()
        return link


class ShopeeWebSessionAdapter:
    """Phương án 2. Không giả lập trình duyệt, không chép header chống bot, không tự đăng nhập."""
    name = "shopee_web"
    platform = "shopee"
    SESSION_KEY = "shopee_web_session"

    def __init__(self, secret_store, store, per_min: int, alert=None, transport=None, timeout=10.0):
        self._secrets, self._store, self._per_min = secret_store, store, per_min
        self._alert = alert
        self._transport, self._timeout = transport, timeout
        self.breaker = CircuitBreaker(threshold=3, cooldown_s=300)

    def session_info(self):
        s = self._secrets.get(self.SESSION_KEY)
        if not s:
            return {"configured": False}
        return {"configured": True, "updated_at": s.get("updated_at"),
                "age_days": round((time.time() - s.get("updated_ts", time.time())) / 86400, 1)}

    def session_fingerprint(self) -> str:
        """Dấu SHA-256 của phiên đang lưu, để so phiên mới mà không cần đọc cookie ra ngoài."""
        s = self._secrets.get(self.SESSION_KEY) or {}
        if not s.get("cookie"):
            return ""
        return session_fingerprint(s["cookie"], s.get("csrf", ""))

    def set_session(self, cookie: str, csrf_token: str = ""):
        self._secrets.put(self.SESSION_KEY, {
            "cookie": cookie, "csrf": csrf_token,
            "updated_ts": time.time(), "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        self.breaker.reset()

    async def _stop(self, reason):
        self.breaker.trip_hard(reason)
        if self._alert:
            await self._alert(f"Phương án 2 đã dừng: {reason}. Đăng nhập lại affiliate.shopee.vn (bot tự lấy phiên nếu bật SHOPEE_BROWSER) hoặc dán cookie mới trong trang quản trị.")
        raise AdapterError(reason, transient=False)

    async def generate(self, product: Product, sub_ids) -> str:
        if not self.breaker.allow():
            raise AdapterError("web_session_stopped" if self.breaker.hard_reason else "adapter_paused",
                               transient=self.breaker.hard_reason is None)
        session = self._secrets.get(self.SESSION_KEY)
        if not session or not session.get("cookie"):
            raise AdapterError("web_session_missing", transient=False)
        minute = int(time.time() // 60)
        if await self._store.incr_window(f"rl:web:{minute}", 120) > self._per_min:
            raise AdapterError("web_rate_limited", transient=True)

        sub = clean_sub_ids(sub_ids)
        params = {f"subId{i + 1}": v for i, v in enumerate(sub)}
        body = {
            "operationName": "batchGetCustomLink",
            "query": "query batchGetCustomLink($linkParams:[CustomLinkParam!],$sourceCaller:SourceCaller){"
                     "batchCustomLink(linkParams:$linkParams,sourceCaller:$sourceCaller){shortLink failCode}}",
            "variables": {"linkParams": [{"originalLink": product.canonical_url, "advancedLinkParams": params}],
                          "sourceCaller": "CUSTOM_LINK_CALLER"},
        }
        headers = {"Content-Type": "application/json", "Cookie": session["cookie"],
                   "Origin": "https://affiliate.shopee.vn", "Referer": "https://affiliate.shopee.vn/offer/custom_link"}
        if session.get("csrf"):
            headers["csrf-token"] = session["csrf"]
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport,
                                         follow_redirects=False) as client:
                resp = await client.post(SHOPEE_WEB_URL, content=json.dumps(body), headers=headers)
        except httpx.HTTPError:
            self.breaker.failure()
            raise AdapterError("network", transient=True) from None

        if resp.status_code in (401, 403) or 300 <= resp.status_code < 400:
            await self._stop(f"phiên bị từ chối (HTTP {resp.status_code})")
        if resp.status_code == 429 or resp.status_code >= 500:
            self.breaker.failure()
            raise AdapterError(f"http_{resp.status_code}", transient=True)
        try:
            data = resp.json()
        except ValueError:
            await self._stop("phản hồi không phải JSON (có thể là trang đăng nhập hoặc captcha)")
        items = ((data.get("data") or {}).get("batchCustomLink")) or []
        if not items:
            await self._stop("Shopee yêu cầu xác thực thêm")
        link = items[0].get("shortLink") or ""
        if items[0].get("failCode") not in (0, None) or not is_safe_output_link(link):
            self.breaker.failure()
            raise AdapterError(f"fail_code_{items[0].get('failCode')}", transient=False)
        self.breaker.success()
        return link


class TikTokAdapter:
    name = "tiktok"
    platform = "tiktok"

    def __init__(self):
        self.breaker = CircuitBreaker()

    async def generate(self, product: Product, sub_ids) -> str:
        raise AdapterError("tiktok_not_ready", transient=False)


class AdapterRouter:
    """Chọn adapter theo sàn. Shopee: Open API nếu có, Phương án 2 chỉ khi chưa có Open API."""

    def __init__(self, shopee_openapi=None, shopee_web=None, tiktok=None):
        self.shopee = shopee_openapi or shopee_web
        self.shopee_openapi, self.shopee_web = shopee_openapi, shopee_web
        self.tiktok = tiktok or TikTokAdapter()

    def for_platform(self, platform: str):
        adapter = self.shopee if platform == "shopee" else self.tiktok if platform == "tiktok" else None
        if adapter is None:
            raise AdapterError("no_adapter", transient=False)
        return adapter

    def status(self):
        out = {}
        for a in (self.shopee_openapi, self.shopee_web, self.tiktok):
            if a is None:
                continue
            item = {"active": a is self.shopee or a is self.tiktok, **a.breaker.state()}
            if isinstance(a, TikTokAdapter):
                item = {"active": False, "state": "not_ready"}
            if isinstance(a, ShopeeWebSessionAdapter):
                item["session"] = a.session_info()
            out[a.name] = item
        return out
