import os
import secrets
import tempfile
import unittest

from app.config import ConfigError, load_settings

BASE = {
    "ZALO_APP_ID": "1", "ZALO_OA_SECRET": "s", "ZALO_APP_SECRET": "a",
    "WEBHOOK_PATH_SECRET": "w" * 32, "USER_HASH_KEY": "u" * 40, "DATA_KEY": secrets.token_hex(32),
    "SHOPEE_APP_ID": "x", "SHOPEE_APP_SECRET": "y",
}


class ConfigTest(unittest.TestCase):
    def test_valid(self):
        s = load_settings(dict(BASE))
        self.assertEqual(s.shopee_app_id, "x")
        self.assertNotIn(BASE["DATA_KEY"], repr(s))     # bí mật không lộ khi in cấu hình
        self.assertNotIn("u" * 40, repr(s))

    def test_rejects_weak_or_missing_secrets(self):
        for key, bad in (("DATA_KEY", "abc"), ("WEBHOOK_PATH_SECRET", "ngan"), ("USER_HASH_KEY", "ngan"),
                         ("ZALO_OA_SECRET", "")):
            with self.subTest(key=key), self.assertRaises(ConfigError):
                load_settings({**BASE, key: bad})

    def test_needs_some_shopee_mode(self):
        env = {k: v for k, v in BASE.items() if not k.startswith("SHOPEE")}
        with self.assertRaises(ConfigError):
            load_settings(env)
        self.assertTrue(load_settings({**env, "SHOPEE_WEB_FALLBACK": "1"}).shopee_web_fallback)

    def test_browser_only_for_web_fallback(self):
        with self.assertRaises(ConfigError):
            load_settings({**BASE, "SHOPEE_BROWSER": "1"})          # đã có Open API
        env = {k: v for k, v in BASE.items() if not k.startswith("SHOPEE")}
        s = load_settings({**env, "SHOPEE_WEB_FALLBACK": "1", "SHOPEE_BROWSER": "1"})
        self.assertTrue(s.shopee_browser)

    def test_secret_from_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "oa")
            with open(path, "w") as f:
                f.write("tu-file\n")
            env = {**BASE, "ZALO_OA_SECRET": "", "ZALO_OA_SECRET_FILE": path}
            self.assertEqual(load_settings(env).zalo_oa_secret, "tu-file")


if __name__ == "__main__":
    unittest.main()
