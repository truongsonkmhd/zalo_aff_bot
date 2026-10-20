import tempfile
import unittest

from app.adapters import AdapterError, AdapterRouter
from app.audit import AuditLog
from app.pipeline import Pipeline
from app.store import MemoryStore
from app.url_guard import Product, UrlRejected
from app.zalo_client import ZaloError
from tests.helpers import make_settings


class FakeAdapter:
    name, platform = "fake", "shopee"

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0
        from app.adapters import CircuitBreaker
        self.breaker = CircuitBreaker()

    async def generate(self, product, sub_ids):
        self.calls += 1
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeZalo:
    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    async def send_text(self, user_id, text):
        if self.fail:
            raise self.fail
        self.sent.append((user_id, text))


async def fake_inspect(url):
    if "evil" in url:
        raise UrlRejected("host_not_allowed")
    return Product("shopee", "https://shopee.vn/product/1/2", "shp:1:2")


class PipelineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(self.tmp.name)
        self.store = MemoryStore()
        self.audit = AuditLog(f"{self.tmp.name}/a.db")

    def tearDown(self):
        self.audit._conn.close()
        self.tmp.cleanup()

    def pipeline(self, adapter, zalo):
        return Pipeline(self.settings, self.store, AdapterRouter(shopee_openapi=adapter), zalo, self.audit,
                        inspect_fn=fake_inspect, retry_delays=(0, 0))

    async def test_success_then_cache(self):
        adapter, zalo = FakeAdapter(["https://s.shopee.vn/OK1"]), FakeZalo()
        p = self.pipeline(adapter, zalo)
        job = {"user_id": "U", "user_hash": "h", "kind": "links", "urls": ["https://s.shopee.vn/x"]}
        await p.handle(job)
        await p.handle(job)
        self.assertEqual(adapter.calls, 1)                       # lần 2 lấy từ cache
        self.assertIn("https://s.shopee.vn/OK1", zalo.sent[1][1])
        self.assertIn("tiếp thị liên kết", zalo.sent[0][1])
        self.assertEqual([r["status"] for r in self.audit.recent(5)], ["cached", "ok"])

    async def test_rejected_link_is_never_echoed(self):
        zalo = FakeZalo()
        await self.pipeline(FakeAdapter([]), zalo).handle(
            {"user_id": "U", "user_hash": "h", "kind": "links", "urls": ["https://evil.example/phish?x=1"]})
        self.assertNotIn("evil", zalo.sent[0][1])
        self.assertIn("không phải link Shopee", zalo.sent[0][1])

    async def test_transient_retries_then_busy_and_dlq(self):
        boom = AdapterError("http_503", transient=True)
        adapter, zalo = FakeAdapter([boom, boom, boom]), FakeZalo()
        await self.pipeline(adapter, zalo).handle(
            {"user_id": "U", "user_hash": "h", "kind": "links", "urls": ["https://s.shopee.vn/x"]})
        self.assertEqual(adapter.calls, 3)
        self.assertIn("đang bận", zalo.sent[0][1])
        self.assertEqual(await self.store.dlq_len(), 1)
        self.assertNotIn("user_id", (await self.store.dlq_items())[0])   # hàng lỗi không giữ ID Zalo thật

    async def test_permanent_error_not_retried(self):
        adapter = FakeAdapter([AdapterError("shopee_10020", transient=False)])
        zalo = FakeZalo()
        await self.pipeline(adapter, zalo).handle(
            {"user_id": "U", "user_hash": "h", "kind": "links", "urls": ["https://s.shopee.vn/x"]})
        self.assertEqual(adapter.calls, 1)

    async def test_send_failure_goes_to_dlq(self):
        zalo = FakeZalo(fail=ZaloError("zalo_-201", transient=False))
        await self.pipeline(FakeAdapter(["https://s.shopee.vn/OK"]), zalo).handle(
            {"user_id": "U", "user_hash": "h", "kind": "links", "urls": ["https://s.shopee.vn/x"]})
        self.assertEqual((await self.store.dlq_items())[0]["stage"], "send")

    async def test_audit_has_no_raw_identity(self):
        await self.pipeline(FakeAdapter(["https://s.shopee.vn/OK"]), FakeZalo()).handle(
            {"user_id": "ZALO-REAL-ID", "user_hash": "h", "kind": "links", "urls": ["https://s.shopee.vn/x"]})
        self.assertNotIn("ZALO-REAL-ID", str(self.audit.recent(5)))


if __name__ == "__main__":
    unittest.main()
