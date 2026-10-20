import json
import time
import unittest

from app.security import Sealer, hash_password, user_hash, verify_password, verify_zalo_webhook
from tests.helpers import APP_ID, OA_SECRET, signed, zalo_event


class WebhookSignatureTest(unittest.TestCase):
    def check(self, body, headers, **kw):
        return verify_zalo_webhook(body.encode(), headers.get("X-ZEvent-Signature", ""), APP_ID, OA_SECRET, 300, **kw)

    def test_valid(self):
        body, headers = signed(zalo_event("hi"))
        ok, reason, event = self.check(body, headers)
        self.assertTrue(ok, reason)
        self.assertEqual(event["message"]["text"], "hi")

    def test_wrong_secret(self):
        body, headers = signed(zalo_event("hi"), secret="khac")
        self.assertEqual(self.check(body, headers)[1], "bad_signature")

    def test_tampered_body(self):
        body, headers = signed(zalo_event("hi"))
        self.assertEqual(self.check(body.replace("hi", "ha"), headers)[1], "bad_signature")

    def test_missing_signature(self):
        body, _ = signed(zalo_event("hi"))
        self.assertEqual(self.check(body, {})[1], "missing_signature")

    def test_replay_of_old_event_rejected(self):
        body, headers = signed(zalo_event("hi", ts=time.time() - 3600))
        self.assertEqual(self.check(body, headers)[1], "stale")

    def test_other_app_rejected(self):
        ev = zalo_event("hi")
        ev["app_id"] = "khac"
        body = json.dumps(ev)
        from app.security import zalo_expected_mac
        mac = zalo_expected_mac(APP_ID, body, ev["timestamp"], OA_SECRET)
        self.assertEqual(self.check(body, {"X-ZEvent-Signature": f"mac={mac}"})[1], "wrong_app")

    def test_garbage_body(self):
        self.assertEqual(self.check("not json", {"X-ZEvent-Signature": "mac=00"})[1], "bad_body")


class PasswordAndHashTest(unittest.TestCase):
    def test_password_roundtrip(self):
        h = hash_password("mot-mat-khau-dai")
        self.assertTrue(verify_password("mot-mat-khau-dai", h))
        self.assertFalse(verify_password("sai", h))
        self.assertFalse(verify_password("x", "rac"))

    def test_user_hash_stable_and_keyed(self):
        self.assertEqual(user_hash("u1", "k" * 32), user_hash("u1", "k" * 32))
        self.assertNotEqual(user_hash("u1", "k" * 32), user_hash("u1", "z" * 32))
        self.assertNotIn("u1", user_hash("u1", "k" * 32))


class SealerTest(unittest.TestCase):
    def test_roundtrip_and_purpose_binding(self):
        import secrets
        s = Sealer(secrets.token_hex(32))
        sealed = s.seal(b"cookie-bi-mat", purpose="shopee_web_session")
        self.assertNotIn("cookie-bi-mat", sealed)
        self.assertEqual(s.open(sealed, purpose="shopee_web_session"), b"cookie-bi-mat")
        with self.assertRaises(Exception):
            s.open(sealed, purpose="zalo_tokens")   # không dán bản mã sang mục khác được


if __name__ == "__main__":
    unittest.main()
