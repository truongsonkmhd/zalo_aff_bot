import base64
import hashlib
import json
import unittest
from urllib.parse import parse_qs, urlparse

import httpx

from app import zalo_auth


class PkceTest(unittest.TestCase):
    def test_challenge_is_s256_of_verifier(self):
        verifier, challenge, state = zalo_auth.new_pkce()
        self.assertTrue(43 <= len(verifier) <= 128)
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(challenge, expected)
        self.assertTrue(state)

    def test_permission_url(self):
        url = zalo_auth.permission_url("123", "https://shop.vn/cb?x=1", "CH", "ST")
        q = parse_qs(urlparse(url).query)
        self.assertTrue(url.startswith(zalo_auth.PERMISSION_URL + "?"))
        self.assertEqual(q, {"app_id": ["123"], "redirect_uri": ["https://shop.vn/cb?x=1"],
                             "code_challenge": ["CH"], "state": ["ST"]})


class CallbackTest(unittest.TestCase):
    def test_full_redirect_url(self):
        code, oa_id = zalo_auth.parse_callback("https://shop.vn/cb?oa_id=999&code=ABC&state=ST\n", "ST")
        self.assertEqual((code, oa_id), ("ABC", "999"))

    def test_bare_code(self):
        self.assertEqual(zalo_auth.parse_callback("  ABC  ", "ST"), ("ABC", ""))

    def test_wrong_state_rejected(self):
        with self.assertRaises(ValueError):
            zalo_auth.parse_callback("https://shop.vn/cb?code=ABC&state=OLD", "ST")

    def test_missing_code_or_empty(self):
        with self.assertRaises(ValueError):
            zalo_auth.parse_callback("https://shop.vn/cb?code=&state=ST", "ST")
        with self.assertRaises(ValueError):
            zalo_auth.parse_callback("   ", "ST")


class ExchangeTest(unittest.TestCase):
    def test_sends_pkce_and_secret(self):
        seen = {}

        def handler(req: httpx.Request):
            seen["secret"] = req.headers.get("secret_key")
            seen["form"] = parse_qs(req.content.decode())
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": "90000"})

        data = zalo_auth.exchange_code("123", "SEC", "ABC", "VER", transport=httpx.MockTransport(handler))
        self.assertEqual(data["refresh_token"], "RT")
        self.assertEqual(seen["secret"], "SEC")
        self.assertEqual(seen["form"], {"app_id": ["123"], "code": ["ABC"], "code_verifier": ["VER"],
                                        "grant_type": ["authorization_code"]})

    def test_rejected(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"error": -14019, "error_description": "Invalid code"}))
        with self.assertRaisesRegex(RuntimeError, "Invalid code"):
            zalo_auth.exchange_code("123", "SEC", "BAD", "VER", transport=transport)

    def test_non_json(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(502, content=b"<html>"))
        with self.assertRaisesRegex(RuntimeError, "502"):
            zalo_auth.exchange_code("123", "SEC", "ABC", "VER", transport=transport)


if __name__ == "__main__":
    unittest.main()
