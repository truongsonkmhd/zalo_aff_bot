import unittest

import httpx

from app import url_guard as g
from tests.helpers import public_resolver


def redirect_transport(chain):
    """chain: {url: (status, location)}"""
    def handler(request: httpx.Request):
        status, loc = chain.get(str(request.url), (404, None))
        return httpx.Response(status, headers={"location": loc} if loc else {})
    return httpx.MockTransport(handler)


class ExtractTest(unittest.TestCase):
    def test_extract_with_and_without_scheme(self):
        text = "mua cai nay https://s.shopee.vn/AbC123, va shopee.vn/product/1/2 nua"
        self.assertEqual(g.extract_urls(text, 3), ["https://s.shopee.vn/AbC123", "https://shopee.vn/product/1/2"])

    def test_limit(self):
        text = " ".join(f"https://shopee.vn/product/1/{i}" for i in range(10))
        self.assertEqual(len(g.extract_urls(text, 3)), 3)


class ShapeTest(unittest.TestCase):
    def rejected(self, url):
        with self.assertRaises(g.UrlRejected) as ctx:
            g.canonicalize(url)
        return ctx.exception.code

    def test_disallowed_host(self):
        self.assertEqual(self.rejected("https://evil.com/product/1/2"), "host_not_allowed")
        self.assertEqual(self.rejected("https://shopee.vn.evil.com/product/1/2"), "host_not_allowed")

    def test_userinfo_trick(self):
        self.assertEqual(self.rejected("https://shopee.vn@evil.com/product/1/2"), "userinfo")

    def test_ip_and_port(self):
        self.assertEqual(self.rejected("https://127.0.0.1/product/1/2"), "ip_literal")
        self.assertEqual(self.rejected("https://shopee.vn:8443/product/1/2"), "bad_port")

    def test_scheme(self):
        self.assertEqual(self.rejected("file:///etc/passwd"), "bad_scheme")

    def test_too_long(self):
        self.assertEqual(self.rejected("https://shopee.vn/" + "a" * 3000), "too_long")


class CanonicalizeTest(unittest.TestCase):
    def test_shopee_forms(self):
        for url in ("https://shopee.vn/Ao-thun-nam-i.123456.7891011?sp_atk=x&utm_source=y",
                    "https://shopee.vn/product/123456/7891011?aff_id=nguoi_khac",
                    "https://m.shopee.vn/i.123456.7891011"):
            p = g.canonicalize(url)
            self.assertEqual(p.canonical_url, "https://shopee.vn/product/123456/7891011")
            self.assertEqual(p.key, "shp:123456:7891011")

    def test_query_params_never_survive(self):
        p = g.canonicalize("https://shopee.vn/product/1/2?affiliate_id=999&sub_id=hack")
        self.assertNotIn("?", p.canonical_url)

    def test_tiktok(self):
        p = g.canonicalize("https://shop.tiktok.com/view/product/1729384756123456789?region=VN")
        self.assertEqual(p.key, "ttk:1729384756123456789")

    def test_universal_link_inner_url_rechecked(self):
        ok = "https://shopee.vn/universal-link?redir=https%3A%2F%2Fshopee.vn%2Fproduct%2F1%2F2"
        self.assertEqual(g.canonicalize(ok).key, "shp:1:2")
        with self.assertRaises(g.UrlRejected):
            g.canonicalize("https://shopee.vn/universal-link?redir=https%3A%2F%2Fevil.com%2Fx")

    def test_not_a_product(self):
        with self.assertRaises(g.UrlRejected):
            g.canonicalize("https://shopee.vn/buyer/login")


class ResolveTest(unittest.IsolatedAsyncioTestCase):
    async def test_follows_allowed_redirect(self):
        t = redirect_transport({"https://s.shopee.vn/AbC": (302, "https://shopee.vn/x-i.1.2?sp_atk=z")})
        p = await g.inspect("https://s.shopee.vn/AbC", transport=t, resolver=public_resolver)
        self.assertEqual(p.key, "shp:1:2")

    async def test_redirect_to_internal_host_blocked(self):
        t = redirect_transport({"https://s.shopee.vn/AbC": (302, "http://169.254.169.254/latest/meta-data")})
        with self.assertRaises(g.UrlRejected) as ctx:
            await g.inspect("https://s.shopee.vn/AbC", transport=t, resolver=public_resolver)
        self.assertEqual(ctx.exception.code, "ip_literal")

    async def test_redirect_to_foreign_domain_blocked(self):
        t = redirect_transport({"https://s.shopee.vn/AbC": (302, "https://evil.com/phish")})
        with self.assertRaises(g.UrlRejected) as ctx:
            await g.inspect("https://s.shopee.vn/AbC", transport=t, resolver=public_resolver)
        self.assertEqual(ctx.exception.code, "host_not_allowed")

    async def test_dns_pointing_to_private_ip_blocked(self):
        async def evil_dns(host):
            return ["10.0.0.5"]
        t = redirect_transport({})
        with self.assertRaises(g.UrlRejected) as ctx:
            await g.inspect("https://s.shopee.vn/AbC", transport=t, resolver=evil_dns)
        self.assertEqual(ctx.exception.code, "non_public_ip")

    async def test_redirect_loop_stops(self):
        t = redirect_transport({"https://s.shopee.vn/A": (302, "https://s.shopee.vn/B"),
                                "https://s.shopee.vn/B": (302, "https://s.shopee.vn/A")})
        with self.assertRaises(g.UrlRejected) as ctx:
            await g.inspect("https://s.shopee.vn/A", transport=t, resolver=public_resolver)
        self.assertEqual(ctx.exception.code, "too_many_redirects")


class OutputLinkTest(unittest.TestCase):
    def test_only_affiliate_short_domains(self):
        self.assertTrue(g.is_safe_output_link("https://s.shopee.vn/9zXyQ"))
        self.assertFalse(g.is_safe_output_link("http://s.shopee.vn/9zXyQ"))
        self.assertFalse(g.is_safe_output_link("https://evil.com/9zXyQ"))
        self.assertFalse(g.is_safe_output_link("https://s.shopee.vn@evil.com/"))


if __name__ == "__main__":
    unittest.main()
