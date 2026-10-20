import hashlib
import json
import re
import secrets
import tempfile
import unittest

import httpx

from app.adapters import AdapterError, ShopeeOpenApiAdapter, ShopeeWebSessionAdapter, clean_sub_ids
from app.secret_store import SecretStore
from app.store import MemoryStore
from app.url_guard import Product

PRODUCT = Product("shopee", "https://shopee.vn/product/1/2", "shp:1:2")


class OpenApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_signature_and_success(self):
        seen = {}

        def handler(req: httpx.Request):
            seen["auth"] = req.headers["authorization"]
            seen["body"] = req.content.decode()
            return httpx.Response(200, json={"data": {"generateShortLink": {"shortLink": "https://s.shopee.vn/AbCd"}}})

        a = ShopeeOpenApiAdapter("APP", "SECRET", transport=httpx.MockTransport(handler))
        link = await a.generate(PRODUCT, ["zalo", "c-1!", ""])
        self.assertEqual(link, "https://s.shopee.vn/AbCd")
        m = re.match(r"SHA256 Credential=APP, Timestamp=(\d+), Signature=([0-9a-f]{64})$", seen["auth"])
        self.assertIsNotNone(m)
        expected = hashlib.sha256(f"APP{m.group(1)}{seen['body']}SECRET".encode()).hexdigest()
        self.assertEqual(m.group(2), expected)   # ký đúng chuỗi đã gửi
        self.assertIn('\\"c1\\"', seen["body"])  # sub_id đã được làm sạch

    async def test_unexpected_link_domain_rejected(self):
        t = httpx.MockTransport(lambda r: httpx.Response(
            200, json={"data": {"generateShortLink": {"shortLink": "https://evil.com/x"}}}))
        with self.assertRaises(AdapterError) as ctx:
            await ShopeeOpenApiAdapter("A", "S", transport=t).generate(PRODUCT, [])
        self.assertEqual(ctx.exception.code, "unexpected_link")

    async def test_transient_vs_permanent(self):
        t = httpx.MockTransport(lambda r: httpx.Response(503))
        with self.assertRaises(AdapterError) as ctx:
            await ShopeeOpenApiAdapter("A", "S", transport=t).generate(PRODUCT, [])
        self.assertTrue(ctx.exception.transient)
        t = httpx.MockTransport(lambda r: httpx.Response(200, json={"errors": [{"extensions": {"code": 10020}}]}))
        with self.assertRaises(AdapterError) as ctx:
            await ShopeeOpenApiAdapter("A", "S", transport=t).generate(PRODUCT, [])
        self.assertFalse(ctx.exception.transient)

    async def test_circuit_opens_after_repeated_failures(self):
        a = ShopeeOpenApiAdapter("A", "S", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        for _ in range(5):
            with self.assertRaises(AdapterError):
                await a.generate(PRODUCT, [])
        with self.assertRaises(AdapterError) as ctx:
            await a.generate(PRODUCT, [])
        self.assertEqual(ctx.exception.code, "adapter_paused")

    def test_clean_sub_ids(self):
        self.assertEqual(clean_sub_ids(["zalo", "a b-c", "", "1", "2", "3", "4"]), ["zalo", "abc", "1", "2", "3"])


class WebSessionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.secrets = SecretStore(f"{self.tmp.name}/sec", secrets.token_hex(32))
        self.alerts = []

    def tearDown(self):
        self.tmp.cleanup()

    def adapter(self, handler, per_min=20):
        async def alert(text):
            self.alerts.append(text)
        a = ShopeeWebSessionAdapter(self.secrets, MemoryStore(), per_min, alert=alert,
                                    transport=httpx.MockTransport(handler))
        a.set_session("SPC_EC=abc; SPC_U=1; rest=" + "x" * 20, "csrf123")
        return a

    async def test_success_sends_only_session_headers(self):
        seen = {}

        def handler(req):
            seen.update(req.headers)
            return httpx.Response(200, json={"data": {"batchCustomLink": [{"shortLink": "https://s.shopee.vn/Z1", "failCode": 0}]}})

        link = await self.adapter(handler).generate(PRODUCT, ["zalo"])
        self.assertEqual(link, "https://s.shopee.vn/Z1")
        self.assertEqual(seen["csrf-token"], "csrf123")
        for anti_bot in ("af-ac-enc-dat", "x-sap-sec", "x-sap-ri", "af-ac-enc-sz-token"):
            self.assertNotIn(anti_bot, seen)

    async def test_403_stops_hard_and_alerts(self):
        a = self.adapter(lambda r: httpx.Response(403))
        with self.assertRaises(AdapterError):
            await a.generate(PRODUCT, [])
        self.assertEqual(a.breaker.state()["state"], "stopped")
        self.assertEqual(len(self.alerts), 1)
        with self.assertRaises(AdapterError) as ctx:   # không thử lại cho tới khi cập nhật phiên
            await a.generate(PRODUCT, [])
        self.assertEqual(ctx.exception.code, "web_session_stopped")
        a.set_session("SPC_EC=moi; rest=" + "y" * 20)
        self.assertEqual(a.breaker.state()["state"], "ok")

    async def test_html_login_page_stops(self):
        a = self.adapter(lambda r: httpx.Response(200, text="<html>login</html>"))
        with self.assertRaises(AdapterError):
            await a.generate(PRODUCT, [])
        self.assertEqual(a.breaker.state()["state"], "stopped")

    async def test_rate_limit(self):
        ok = lambda r: httpx.Response(200, json={"data": {"batchCustomLink": [{"shortLink": "https://s.shopee.vn/Z1", "failCode": 0}]}})
        a = self.adapter(ok, per_min=2)
        await a.generate(PRODUCT, [])
        await a.generate(PRODUCT, [])
        with self.assertRaises(AdapterError) as ctx:
            await a.generate(PRODUCT, [])
        self.assertEqual(ctx.exception.code, "web_rate_limited")

    def test_cookie_encrypted_at_rest_and_not_in_info(self):
        a = self.adapter(lambda r: httpx.Response(200))
        with open(self.secrets.path_for("shopee_web_session"), encoding="utf-8") as f:
            self.assertNotIn("SPC_EC", f.read())
        self.assertNotIn("cookie", json.dumps(a.session_info()))


if __name__ == "__main__":
    unittest.main()
