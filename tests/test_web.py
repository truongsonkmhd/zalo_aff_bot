import json
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from app.main import build_context
from app.store import MemoryStore
from app.web import create_app
from tests.helpers import ADMIN_PASSWORD, PATH_SECRET, make_settings, signed, zalo_event


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore()
        self.ctx = build_context(make_settings(self.tmp.name, per_user_per_min=3), store=self.store)
        app = create_app(self.ctx)
        self.client = TestClient(app, client=("127.0.0.1", 50000))
        self.outsider = TestClient(app, client=("203.0.113.9", 50000))

    def tearDown(self):
        self.ctx.close()
        self.tmp.cleanup()

    def post_event(self, event, path=PATH_SECRET, secret=None):
        body, headers = signed(event) if secret is None else signed(event, secret=secret)
        return self.client.post(f"/webhook/zalo/{path}", content=body, headers=headers)

    def drain(self):
        jobs = []
        while self.store._queue.qsize():
            jobs.append(json.loads(self.store._queue.get_nowait()))
        return jobs

    # ── Webhook ──
    def test_wrong_path_is_404(self):
        self.assertEqual(self.post_event(zalo_event("x"), path="sai" * 10).status_code, 404)

    def test_bad_signature_is_403(self):
        self.assertEqual(self.post_event(zalo_event("x"), secret="gia").status_code, 403)
        self.assertEqual(self.ctx.counters["webhook_bad_signature"], 1)

    def test_oversized_body_rejected(self):
        r = self.client.post(f"/webhook/zalo/{PATH_SECRET}", content=b"x" * (70 * 1024))
        self.assertEqual(r.status_code, 413)

    def test_link_message_enqueued_with_hashed_identity(self):
        self.assertEqual(self.post_event(zalo_event("mua https://s.shopee.vn/AbC", user="ZALO-1")).status_code, 200)
        [job] = self.drain()
        self.assertEqual(job["kind"], "links")
        self.assertEqual(job["urls"], ["https://s.shopee.vn/AbC"])
        self.assertNotEqual(job["user_hash"], "ZALO-1")

    def test_link_attachment_event(self):
        ev = zalo_event("", event_name="user_send_link",
                        attachments=[{"type": "link", "payload": {"url": "https://shopee.vn/product/1/2"}}])
        self.post_event(ev)
        self.assertEqual(self.drain()[0]["urls"], ["https://shopee.vn/product/1/2"])

    def test_duplicate_message_ignored(self):
        ev = zalo_event("https://s.shopee.vn/A", msg_id="m1")
        self.post_event(ev)
        self.post_event(ev)
        self.assertEqual(len(self.drain()), 1)

    def test_help_sent_once_per_day(self):
        self.post_event(zalo_event("xin chao"))
        self.post_event(zalo_event("con hang khong"))
        self.assertEqual([j["kind"] for j in self.drain()], ["help"])

    def test_rate_limit_notice(self):
        for _ in range(5):
            self.post_event(zalo_event("https://s.shopee.vn/A"))
        kinds = [j["kind"] for j in self.drain()]
        self.assertEqual(kinds.count("links"), 3)
        self.assertEqual(kinds.count("rate_limited"), 1)

    def test_other_events_ignored(self):
        self.post_event(zalo_event("x", event_name="oa_send_text"))
        self.assertEqual(self.drain(), [])

    # ── Quản trị ──
    def login(self):
        r = self.client.post("/admin/api/login", json={"password": ADMIN_PASSWORD})
        self.assertEqual(r.status_code, 200)
        return r.json()["csrf_token"]

    def test_admin_hidden_from_other_ips(self):
        self.assertEqual(self.outsider.get("/admin/api/session").status_code, 404)
        self.assertEqual(self.outsider.post("/admin/api/login", json={"password": ADMIN_PASSWORD}).status_code, 404)

    def test_requires_login(self):
        self.assertEqual(self.client.get("/admin/api/overview").status_code, 401)

    def test_login_cookie_flags(self):
        r = self.client.post("/admin/api/login", json={"password": ADMIN_PASSWORD})
        cookie = r.headers["set-cookie"].lower()
        for flag in ("httponly", "samesite=strict", "path=/admin"):
            self.assertIn(flag, cookie)

    def test_login_brute_force_locked(self):
        for _ in range(5):
            self.assertEqual(self.client.post("/admin/api/login", json={"password": "sai"}).status_code, 401)
        r = self.client.post("/admin/api/login", json={"password": ADMIN_PASSWORD})
        self.assertEqual(r.status_code, 429)   # khóa cả mật khẩu đúng trong 15 phút

    def test_mutation_requires_csrf(self):
        csrf = self.login()
        self.assertEqual(self.client.post("/admin/api/bot", json={"enabled": False}).status_code, 403)
        r = self.client.post("/admin/api/bot", json={"enabled": False}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.client.get("/admin/api/overview").json()["bot_enabled"])

    def test_paused_bot_sends_notice_only(self):
        csrf = self.login()
        self.client.post("/admin/api/bot", json={"enabled": False}, headers={"X-CSRF-Token": csrf})
        self.post_event(zalo_event("https://s.shopee.vn/A"))
        self.assertEqual([j["kind"] for j in self.drain()], ["paused"])

    def test_overview_and_security_headers(self):
        self.login()
        r = self.client.get("/admin/api/overview")
        self.assertEqual(r.status_code, 200)
        self.assertIn("shopee_openapi", r.json()["adapters"])
        self.assertIn("frame-ancestors 'none'", r.headers["content-security-policy"])
        self.assertEqual(r.headers["x-frame-options"], "DENY")
        self.assertEqual(r.headers["cache-control"], "no-store")

    def test_logout_invalidates_session(self):
        csrf = self.login()
        self.client.post("/admin/api/logout", headers={"X-CSRF-Token": csrf})
        self.assertEqual(self.client.get("/admin/api/overview").status_code, 401)


class SplitListenerTest(unittest.TestCase):
    def test_public_port_has_no_admin_and_admin_port_has_no_webhook(self):
        tmp = tempfile.TemporaryDirectory()
        ctx = build_context(make_settings(tmp.name), store=MemoryStore())
        public = TestClient(create_app(ctx, include_admin=False), client=("127.0.0.1", 1))
        admin = TestClient(create_app(ctx, include_webhook=False), client=("127.0.0.1", 1))
        # Kể cả từ IP được phép, cổng public không có trang quản trị
        self.assertEqual(public.post("/admin/api/login", json={"password": ADMIN_PASSWORD}).status_code, 404)
        self.assertEqual(public.get("/admin/").status_code, 404)
        body, headers = signed(zalo_event("x"))
        self.assertEqual(admin.post(f"/webhook/zalo/{PATH_SECRET}", content=body, headers=headers).status_code, 404)
        self.assertEqual(admin.post("/admin/api/login", json={"password": ADMIN_PASSWORD}).status_code, 200)
        ctx.close()
        tmp.cleanup()


class WebSessionAdminTest(unittest.TestCase):
    def test_cookie_is_write_only(self):
        tmp = tempfile.TemporaryDirectory()
        ctx = build_context(make_settings(tmp.name, shopee_app_id="", shopee_app_secret="", shopee_web_fallback=True),
                            store=MemoryStore())
        client = TestClient(create_app(ctx), client=("127.0.0.1", 50000))
        csrf = client.post("/admin/api/login", json={"password": ADMIN_PASSWORD}).json()["csrf_token"]
        r = client.post("/admin/api/shopee-web-session", json={"cookie": "SPC_EC=bimat" + "z" * 30},
                        headers={"X-CSRF-Token": csrf})
        self.assertEqual(r.status_code, 200)
        overview = client.get("/admin/api/overview").text
        self.assertNotIn("SPC_EC", overview)
        self.assertTrue(json.loads(overview)["adapters"]["shopee_web"]["session"]["configured"])
        bad = client.post("/admin/api/shopee-web-session", json={"cookie": "a\r\nInjected: 1" + "z" * 30},
                          headers={"X-CSRF-Token": csrf})
        self.assertEqual(bad.status_code, 400)
        ctx.close()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
