import json
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.adapters import AdapterRouter, CircuitBreaker
from app.main import build_context
from app.orders import customer_code
from app.pipeline import Pipeline
from app.store import MemoryStore
from app.url_guard import Product
from app.web import create_app
from tests.helpers import ADMIN_PASSWORD, PATH_SECRET, make_settings, signed, zalo_event

USER = "2578938647871172479"


def order(oid, code, status="completed"):
    return {"order_id": oid, "code": code, "status": status, "purchase_ts": 1729240000,
            "items": [{"name": "Áo thun", "qty": 1}], "order_value": 150000, "commission": 9000}


class LookupWebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore()
        self.ctx = build_context(make_settings(self.tmp.name, cashback_percent=50, lookup_per_ip_per_min=10),
                                 store=self.store)
        self.code = customer_code(USER, self.ctx.settings.user_hash_key)
        self.other = customer_code("nguoi-khac", self.ctx.settings.user_hash_key)
        self.ctx.orders.upsert([order("2410AAAA1111", self.code), order("2410BBBB2222", self.code, "pending"),
                                order("2410CCCC3333", self.other)], "csv")
        self.public = TestClient(create_app(self.ctx, include_admin=False), client=("127.0.0.1", 1))
        self.admin = TestClient(create_app(self.ctx, include_webhook=False), client=("127.0.0.1", 1))

    def tearDown(self):
        self.ctx.close()
        self.tmp.cleanup()

    def lookup(self, q, client=None, headers=None):
        return (client or self.public).post("/api/tra-cuu", json={"q": q}, headers=headers or {})

    def drain(self):
        jobs = []
        while self.store._queue.qsize():
            jobs.append(json.loads(self.store._queue.get_nowait()))
        return jobs

    # ── Bot: lệnh /id và mã trong link ──
    def test_id_command_replies_with_code_not_raw_id(self):
        body, headers = signed(zalo_event("/id", user=USER))
        self.public.post(f"/webhook/zalo/{PATH_SECRET}", content=body, headers=headers)
        [job] = self.drain()
        self.assertEqual((job["kind"], job["code"]), ("lookup_id", self.code))

    def test_id_command_rate_limited(self):
        for _ in range(8):
            body, headers = signed(zalo_event("/ID", user=USER))
            self.public.post(f"/webhook/zalo/{PATH_SECRET}", content=body, headers=headers)
        self.assertEqual(len(self.drain()), 5)

    # ── Tra cứu ──
    def test_lookup_by_code_returns_only_own_orders(self):
        r = self.lookup(self.code.lower())
        self.assertEqual(r.status_code, 200)
        ids = [x["order_id"] for x in r.json()["orders"]]
        self.assertEqual(sorted(ids), ["2410AAAA1111", "2410BBBB2222"])
        raw = r.text
        self.assertNotIn(self.code, raw)          # không trả lại mã khách
        self.assertNotIn("commission", raw)       # không lộ hoa hồng gốc của shop
        self.assertEqual([x["cashback"] for x in r.json()["orders"]], [4500, 4500])

    def test_lookup_by_order_id(self):
        r = self.lookup("2410cccc3333")
        self.assertEqual([x["order_id"] for x in r.json()["orders"]], ["2410CCCC3333"])

    def test_unknown_and_invalid(self):
        self.assertEqual(self.lookup("ZZZZZZZZZZ").json()["orders"], [])
        self.assertEqual(self.lookup("<script>alert(1)</script>").status_code, 400)
        self.assertEqual(self.public.post("/api/tra-cuu", content=b"x" * 5000).status_code, 413)

    def test_rate_limit_per_ip(self):
        codes = [self.lookup("ZZZZZZZZZZ").status_code for _ in range(12)]
        self.assertEqual(codes.count(429), 2)

    def test_cloudflare_ip_header_only_when_trusted(self):
        tmp = tempfile.TemporaryDirectory()
        ctx = build_context(make_settings(tmp.name, lookup_per_ip_per_min=2, trust_cf_header=True), store=MemoryStore())
        c = TestClient(create_app(ctx, include_admin=False), client=("127.0.0.1", 1))
        hit = lambda ip: c.post("/api/tra-cuu", json={"q": "ZZZZZZZZZZ"}, headers={"CF-Connecting-IP": ip}).status_code
        self.assertEqual([hit("1.1.1.1") for _ in range(3)], [200, 200, 429])
        self.assertEqual(hit("2.2.2.2"), 200)        # mỗi khách tính hạn mức riêng
        ctx.close()
        tmp.cleanup()

    def test_public_page_served_safely(self):
        r = self.public.get("/tra-don/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("script-src 'self'", r.headers["content-security-policy"])
        for bad in ("/tra-don/../app/config.py", "/tra-don/..%2Fapp%2Fconfig.py", "/tra-don/index.html.bak"):
            self.assertEqual(self.public.get(bad).status_code, 404, bad)

    def test_ports_separated(self):
        self.assertEqual(self.admin.post("/api/tra-cuu", json={"q": self.code}).status_code, 404)
        self.assertEqual(self.public.get("/admin/api/orders").status_code, 404)

    # ── Quản trị: tải file đơn ──
    def test_admin_import_requires_csrf_and_matches_codes(self):
        csrf = self.admin.post("/admin/api/login", json={"password": ADMIN_PASSWORD}).json()["csrf_token"]
        csv_text = f"Order id,Order Status,Item Name,Qty,Item Price,Net Commission,Sub_id\n2410DDDD4444,Completed,Bag,1,100000,5000,KH{self.code}\n"
        self.assertEqual(self.admin.post("/admin/api/orders/import", json={"csv": csv_text}).status_code, 403)
        r = self.admin.post("/admin/api/orders/import", json={"csv": csv_text}, headers={"X-CSRF-Token": csrf})
        self.assertEqual((r.status_code, r.json()["matched"]), (200, 1))
        self.assertEqual(len(self.lookup(self.code).json()["orders"]), 3)
        bad = self.admin.post("/admin/api/orders/import", json={"csv": "a,b\n1,2\n"}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(bad.status_code, 400)
        listing = self.admin.get("/admin/api/orders").json()
        self.assertEqual(listing["summary"]["total"], 4)


class PipelineCodeTest(unittest.IsolatedAsyncioTestCase):
    async def test_links_carry_customer_code_and_cache_per_customer(self):
        tmp = tempfile.TemporaryDirectory()
        settings = make_settings(tmp.name)
        seen = []

        class Adapter:
            name, platform = "fake", "shopee"
            breaker = CircuitBreaker()

            async def generate(self, product, sub_ids):
                seen.append(sub_ids)
                return f"https://s.shopee.vn/L{len(seen)}"

        class Zalo:
            sent = []

            async def send_text(self, uid, text):
                self.sent.append(text)

        async def inspect(url):
            return Product("shopee", "https://shopee.vn/product/1/2", "shp:1:2")

        ctx = build_context(settings, store=MemoryStore())
        zalo = Zalo()
        p = Pipeline(settings, MemoryStore(), AdapterRouter(shopee_openapi=Adapter()), zalo, ctx.audit,
                     inspect_fn=inspect, retry_delays=())
        await p.handle({"user_id": "A", "user_hash": "ha", "code": "ABCDEFGHJK", "kind": "links", "urls": ["u"]})
        await p.handle({"user_id": "B", "user_hash": "hb", "code": "BCDEFGHJKL", "kind": "links", "urls": ["u"]})
        self.assertEqual(seen[0][-1], "KHABCDEFGHJK")
        self.assertEqual(seen[1][-1], "KHBCDEFGHJKL")       # khách khác không dùng chung link trong cache
        self.assertIn("ABCDEFGHJK", zalo.sent[0])
        await p.handle({"user_id": "A", "user_hash": "ha", "code": "ABCDEFGHJK", "kind": "lookup_id"})
        self.assertIn("Mã tra cứu đơn hàng của bạn: ABCDEFGHJK", zalo.sent[-1])
        ctx.close()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
