"""
Worker: lấy việc từ hàng đợi → URL Guard → adapter (có cache, thử lại khi lỗi tạm thời)
→ soạn tin → gửi qua Zalo OA → ghi nhật ký ẩn danh.
"""

import asyncio
import time

from . import composer, url_guard
from .orders import code_sub_id
from .adapters import AdapterError
from .zalo_client import ZaloError

CACHE_TTL_S = 24 * 3600


class Pipeline:
    def __init__(self, settings, store, router, zalo, audit, inspect_fn=None, retry_delays=(1, 4, 16)):
        self.settings = settings
        self.store = store
        self.router = router
        self.zalo = zalo
        self.audit = audit
        self.inspect = inspect_fn or url_guard.inspect
        self.retry_delays = retry_delays

    def _sub_ids(self, code=""):
        # Mã khách nằm trong sub_id để báo cáo chuyển đổi của Shopee khớp đơn về đúng khách
        return [self.settings.sub_id_channel, self.settings.sub_id_campaign] + ([code_sub_id(code)] if code else [])

    async def _convert(self, url, code=""):
        """Trả về (link | None, mã_lỗi | None, platform, item_key, source, transient)."""
        try:
            product = await self.inspect(url)
        except url_guard.UrlRejected as e:
            return None, e.code, None, None, "-", False
        # Link gắn mã khách nên cache theo từng khách
        cache_key = f"aff:{product.key}:{self.settings.sub_id_campaign}:{code}"
        cached = await self.store.get(cache_key)
        if cached and url_guard.is_safe_output_link(cached):
            return cached, None, product.platform, product.key, "cache", False
        try:
            adapter = self.router.for_platform(product.platform)
        except AdapterError as e:
            return None, e.code, product.platform, product.key, "-", False
        last = None
        for attempt, delay in enumerate((0,) + tuple(self.retry_delays)):
            if delay:
                await asyncio.sleep(delay)
            try:
                link = await adapter.generate(product, self._sub_ids(code))
                await self.store.set(cache_key, link, CACHE_TTL_S)
                return link, None, product.platform, product.key, adapter.name, False
            except AdapterError as e:
                last = e
                if not e.transient:
                    break
        return None, last.code, product.platform, product.key, adapter.name, last.transient

    async def _send(self, user_id, text):
        for delay in (0,) + tuple(self.retry_delays):
            if delay:
                await asyncio.sleep(delay)
            try:
                await self.zalo.send_text(user_id, text)
                return None
            except ZaloError as e:
                if not e.transient:
                    return e.code
                last = e.code
        return last

    async def handle(self, job: dict):
        user_id, user_hash = job["user_id"], job["user_hash"]
        kind = job.get("kind", "links")
        code = job.get("code", "")
        if kind == "lookup_id":
            err = await self._send(user_id, composer.lookup_text(code, self.settings.lookup_page_url))
            if err:
                await self.store.push_dlq({"ts": int(time.time()), "user_hash": user_hash, "stage": "send", "error": err})
            return
        if kind != "links":
            err = await self._send(user_id, composer.notice(kind))
            if err:
                await self.store.push_dlq({"ts": int(time.time()), "user_hash": user_hash, "stage": "send", "error": err})
            return

        results, any_transient = [], False
        for url in job.get("urls", []):
            t0 = time.monotonic()
            link, error_code, platform, item_key, source, transient = await self._convert(url, code)
            any_transient |= transient
            status = ("cached" if source == "cache" else "ok") if link else (
                "rejected" if source == "-" else "failed")
            await asyncio.to_thread(self.audit.record, user_hash, platform, item_key, status, error_code,
                                    int((time.monotonic() - t0) * 1000), source)
            results.append((link, error_code))

        if not any(link for link, _ in results) and any_transient:
            await self.store.push_dlq({"ts": int(time.time()), "user_hash": user_hash, "stage": "convert",
                                       "error": next(c for _, c in results if c)})
            text = composer.notice("busy")
        else:
            text = composer.compose(results, lookup_code=code)
        err = await self._send(user_id, text)
        if err:
            await self.store.push_dlq({"ts": int(time.time()), "user_hash": user_hash, "stage": "send", "error": err})


async def run_worker(pipeline: Pipeline, store, stop: asyncio.Event, concurrency: int = 4):
    async def loop():
        while not stop.is_set():
            job = await store.dequeue(timeout=1.0)
            if job is None:
                continue
            try:
                await pipeline.handle(job)
            except Exception as e:   # một việc lỗi không được làm chết worker
                await store.push_dlq({"ts": int(time.time()), "user_hash": job.get("user_hash"),
                                      "stage": "internal", "error": type(e).__name__})
    await asyncio.gather(*(loop() for _ in range(concurrency)))
