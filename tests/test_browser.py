import asyncio
import tempfile
import time
import unittest
from unittest import mock

from app.adapters import ShopeeWebSessionAdapter
from app.secret_store import SecretStore
from app.shopee_browser import ShopeeBrowserSession, session_from_cookies
from app.store import MemoryStore


def ck(name, value, domain=".shopee.vn", expires=-1):
    return {"name": name, "value": value, "domain": domain, "expires": expires}


LOGGED_IN = [ck("SPC_EC", "phien" + "x" * 30), ck("csrftoken", "tok1", "affiliate.shopee.vn"),
             ck("SPC_U", "123"), ck("khac", "y", ".google.com")]


class FakeCdp:
    def __init__(self, cookies):
        self.cookies, self.reloads, self.down = list(cookies), 0, False

    async def _json(self, port, path):
        if self.down:
            raise OSError("refused")
        return {"webSocketDebuggerUrl": "ws://127.0.0.1:1/x"}

    async def get_cookies(self, port):
        return list(self.cookies)

    async def affiliate_tab(self, port):
        return {"url": "https://affiliate.shopee.vn/"}

    async def open_tab(self, port, url):
        pass

    async def reload(self, port):
        self.reloads += 1
        return True


class FakeLauncher:
    def __init__(self, port=9222):
        self.p, self.launched = port, 0

    def port(self):
        return self.p

    def launch(self, url):
        self.launched += 1
        self.p = 9333


class CookieParseTest(unittest.TestCase):
    def test_only_shopee_cookies_and_csrf(self):
        header, csrf, logged_in = session_from_cookies(LOGGED_IN)
        self.assertTrue(logged_in)
        self.assertEqual(csrf, "tok1")
        self.assertIn("SPC_EC=phien", header)
        self.assertNotIn("khac", header)

    def test_expired_and_injection_dropped(self):
        now = time.time()
        header, _, logged_in = session_from_cookies(
            [ck("SPC_EC", "cu", expires=now - 10), ck("bad", "a;b"), ck("crlf", "a\r\nX: y")], now)
        self.assertFalse(logged_in)
        self.assertEqual(header, "")

    def test_affiliate_cookie_wins_over_shared(self):
        header, _, _ = session_from_cookies([ck("a", "chung"), ck("a", "rieng", "affiliate.shopee.vn")])
        self.assertEqual(header, "a=rieng")


class BrowserSyncTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.secrets = SecretStore(self.tmp.name, "11" * 32)
        self.alerts = []

        async def alert(text):
            self.alerts.append(text)
        self.adapter = ShopeeWebSessionAdapter(self.secrets, MemoryStore(), 20, alert=alert)
        self.cdp, self.launcher = FakeCdp(LOGGED_IN), FakeLauncher()
        self.b = ShopeeBrowserSession(self.adapter, self.tmp.name, alert=alert, cdp=self.cdp, launcher=self.launcher)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_login_then_update_once(self):
        self.assertEqual(await self.b.sync(), "updated")
        stored = self.secrets.get(ShopeeWebSessionAdapter.SESSION_KEY)
        self.assertEqual(stored["csrf"], "tok1")
        self.assertIn("SPC_EC=phien", stored["cookie"])
        self.assertEqual(await self.b.sync(), "unchanged")
        self.cdp.cookies[0] = ck("SPC_EC", "moi" + "z" * 30)
        self.assertEqual(await self.b.sync(), "updated")
        self.assertTrue(self.b.status()["logged_in"])

    async def test_not_logged_in_alerts_once(self):
        self.cdp.cookies = [ck("csrftoken", "t")]
        self.assertEqual(await self.b.sync(), "not_logged_in")
        self.assertEqual(await self.b.sync(), "not_logged_in")
        self.assertEqual(len(self.alerts), 1)
        self.assertFalse(self.secrets.get(ShopeeWebSessionAdapter.SESSION_KEY))

    async def test_browser_closed(self):
        self.cdp.down = True
        self.assertEqual(await self.b.sync(), "browser_closed")
        self.assertEqual(self.b.status()["browser"], "closed")

    async def test_rejected_session_reloads_page_and_recovers(self):
        await self.b.sync()
        self.adapter.breaker.trip_hard("HTTP 403")

        async def reload(port):
            self.cdp.reloads += 1
            self.cdp.cookies[0] = ck("SPC_EC", "sau-reload" + "q" * 30)
        self.cdp.reload = reload
        with mock.patch("app.shopee_browser.asyncio.sleep", new=mock.AsyncMock()):
            self.assertEqual(await self.b.sync(), "updated")
        self.assertEqual(self.cdp.reloads, 1)
        self.assertIsNone(self.adapter.breaker.hard_reason)        # set_session đã mở lại cầu dao
        self.assertTrue(any("tự lấy phiên" in a for a in self.alerts))

    async def test_rejected_session_unchanged_stays_stopped(self):
        await self.b.sync()
        self.adapter.breaker.trip_hard("HTTP 403")
        with mock.patch("app.shopee_browser.asyncio.sleep", new=mock.AsyncMock()):
            self.assertEqual(await self.b.sync(), "rejected")
            self.assertEqual(await self.b.sync(), "rejected")       # không reload liên tục
        self.assertEqual(self.cdp.reloads, 1)
        self.assertIsNotNone(self.adapter.breaker.hard_reason)

    async def test_open_launches_when_closed(self):
        async def json_after_launch(port, path):
            if self.launcher.launched:
                return {"webSocketDebuggerUrl": "ws://127.0.0.1:1/x"}
            raise OSError("refused")
        self.cdp._json = json_after_launch
        with mock.patch("app.shopee_browser.asyncio.sleep", new=mock.AsyncMock()):
            status = await self.b.open()
        self.assertEqual(self.launcher.launched, 1)
        self.assertEqual(status["browser"], "open")

    async def test_run_stops(self):
        stop = asyncio.Event()
        stop.set()
        await self.b.run(stop, open_on_start=False)


if __name__ == "__main__":
    unittest.main()
