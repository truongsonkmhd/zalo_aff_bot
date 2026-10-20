import asyncio
import secrets
import tempfile
import unittest

import httpx

from app.secret_store import SecretStore
from app.store import MemoryStore
from app.zalo_client import TOKEN_KEY, TokenManager, ZaloClient, ZaloError


class ZaloTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.secrets = SecretStore(f"{self.tmp.name}/sec", secrets.token_hex(32))
        self.refresh_calls = 0
        self.sent = []
        self.send_errors = []

    def tearDown(self):
        self.tmp.cleanup()

    def transport(self):
        def handler(req: httpx.Request):
            if "oauth.zaloapp.com" in str(req.url):
                self.refresh_calls += 1
                assert req.headers["secret_key"] == "APPSECRET"
                return httpx.Response(200, json={"access_token": f"AT{self.refresh_calls}",
                                                 "refresh_token": f"RT{self.refresh_calls}", "expires_in": "90000"})
            self.sent.append(req.headers["access_token"])
            err = self.send_errors.pop(0) if self.send_errors else 0
            return httpx.Response(200, json={"error": err, "message": "x"})
        return httpx.MockTransport(handler)

    def manager(self):
        return TokenManager("APP", "APPSECRET", self.secrets, MemoryStore(), "RT0", transport=self.transport())

    async def test_first_send_refreshes_and_rotates_refresh_token(self):
        tm = self.manager()
        await ZaloClient(tm, transport=self.transport()).send_text("U", "xin chao")
        self.assertEqual(self.refresh_calls, 1)
        stored = self.secrets.get(TOKEN_KEY)
        self.assertEqual(stored["refresh_token"], "RT1")   # refresh token cũ đã bị thay
        self.assertEqual(self.sent, ["AT1"])

    async def test_concurrent_sends_refresh_only_once(self):
        tm = self.manager()
        client = ZaloClient(tm, transport=self.transport())
        await asyncio.gather(*(client.send_text("U", "x") for _ in range(10)))
        self.assertEqual(self.refresh_calls, 1)

    async def test_invalid_token_triggers_single_forced_refresh(self):
        tm = self.manager()
        client = ZaloClient(tm, transport=self.transport())
        await client.send_text("U", "x")
        self.send_errors = [-216]
        await client.send_text("U", "x")
        self.assertEqual(self.refresh_calls, 2)
        self.assertEqual(self.sent[-1], "AT2")

    async def test_other_error_raises(self):
        client = ZaloClient(self.manager(), transport=self.transport())
        self.send_errors = [-201]
        with self.assertRaises(ZaloError) as ctx:
            await client.send_text("U", "x")
        self.assertEqual(ctx.exception.code, "zalo_-201")

    def test_tokens_encrypted_at_rest(self):
        self.manager()
        with open(self.secrets.path_for("zalo_tokens"), encoding="utf-8") as f:
            self.assertNotIn("RT0", f.read())


if __name__ == "__main__":
    unittest.main()
