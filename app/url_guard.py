"""
URL Guard: tách link khỏi tin nhắn, chỉ nhận tên miền Shopee/TikTok, mở link rút gọn an toàn
(chống SSRF) và chuẩn hóa về link sản phẩm gốc. Mọi tham số truy vấn của khách đều bị bỏ.
"""

import asyncio
import ipaddress
import re
import socket
import urllib.parse
from dataclasses import dataclass

import httpx

MAX_URL_LEN = 2048
MAX_REDIRECTS = 5
HOP_TIMEOUT_S = 3.0

SHOPEE_HOSTS = {"shopee.vn", "www.shopee.vn", "m.shopee.vn"}
SHOPEE_SHORT_HOSTS = {"s.shopee.vn", "shp.ee", "vn.shp.ee", "shope.ee"}
TIKTOK_HOSTS = {"shop.tiktok.com", "www.tiktok.com", "tiktok.com", "shop-vn.tiktok.com"}
TIKTOK_SHORT_HOSTS = {"vt.tiktok.com", "vm.tiktok.com"}
SHORT_HOSTS = SHOPEE_SHORT_HOSTS | TIKTOK_SHORT_HOSTS
ALLOWED_HOSTS = SHOPEE_HOSTS | TIKTOK_HOSTS | SHORT_HOSTS

# Link rút gọn do API affiliate trả về: chỉ những tên miền này được phép xuất hiện trong tin gửi khách
OUTPUT_HOSTS = {"s.shopee.vn", "shp.ee", "vn.shp.ee", "shope.ee", "vt.tiktok.com", "vm.tiktok.com"}

_URL_RE = re.compile(r"(?:https?://|(?<![\w.])(?=(?:[a-z0-9-]+\.)*(?:shopee\.vn|shp\.ee|shope\.ee|tiktok\.com)/))"
                     r"[^\s<>\"'`]+", re.IGNORECASE)
_SHOPEE_ITEM_RES = (
    re.compile(r"/product/(\d{1,15})/(\d{1,20})"),
    re.compile(r"-i\.(\d{1,15})\.(\d{1,20})"),
    re.compile(r"^/i\.(\d{1,15})\.(\d{1,20})"),
)
_TIKTOK_PRODUCT_RE = re.compile(r"/product/(\d{10,25})")


class UrlRejected(Exception):
    """code là mã ngắn để thống kê; không chứa dữ liệu của khách."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Product:
    platform: str          # "shopee" | "tiktok"
    canonical_url: str
    key: str               # khóa cache: shp:<shop>:<item> | ttk:<id>


def extract_urls(text: str, limit: int):
    found, seen = [], set()
    for m in _URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,;:!?)]}>")
        if not url.lower().startswith("http"):
            url = "https://" + url
        if url not in seen:
            seen.add(url)
            found.append(url)
        if len(found) >= limit:
            break
    return found


def _check_shape(url: str) -> urllib.parse.SplitResult:
    if len(url) > MAX_URL_LEN:
        raise UrlRejected("too_long")
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        raise UrlRejected("malformed") from None
    if parts.scheme not in ("http", "https"):
        raise UrlRejected("bad_scheme")
    if parts.username or parts.password or "@" in parts.netloc:
        raise UrlRejected("userinfo")
    host = (parts.hostname or "").lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
        raise UrlRejected("ip_literal")
    except ValueError:
        pass
    if parts.port not in (None, 80, 443):
        raise UrlRejected("bad_port")
    if host not in ALLOWED_HOSTS:
        raise UrlRejected("host_not_allowed")
    return parts


def _is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not (addr.is_private or addr.is_loopback or addr.is_link_local
                                   or addr.is_multicast or addr.is_reserved or addr.is_unspecified)


async def _default_resolver(host: str):
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def _assert_public(host: str, resolver):
    try:
        ips = await asyncio.wait_for(resolver(host), timeout=HOP_TIMEOUT_S)
    except (OSError, asyncio.TimeoutError):
        raise UrlRejected("dns_failed") from None
    if not ips or not all(_is_public_ip(ip) for ip in ips):
        raise UrlRejected("non_public_ip")


async def resolve_short_link(url: str, transport=None, resolver=None) -> str:
    """Mở link rút gọn từng bước; mỗi bước phải ở trong ALLOWED_HOSTS và trỏ về IP công khai."""
    resolver = resolver or _default_resolver
    current = url
    # Client mới cho mỗi lần mở: không mang cookie giữa các khách
    async with httpx.AsyncClient(follow_redirects=False, timeout=HOP_TIMEOUT_S, transport=transport,
                                 headers={"User-Agent": "Mozilla/5.0 (compatible; AffLinkBot/1.0)"}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            parts = _check_shape(current)
            host = parts.hostname.lower().rstrip(".")
            if host not in SHORT_HOSTS:
                return current
            await _assert_public(host, resolver)
            safe = urllib.parse.urlunsplit(("https", host, parts.path, parts.query, ""))
            try:
                resp = await client.head(safe)
                if resp.status_code in (405, 501):
                    resp = await client.get(safe)
            except httpx.HTTPError:
                raise UrlRejected("resolve_failed") from None
            location = resp.headers.get("location")
            if resp.status_code not in (301, 302, 303, 307, 308) or not location:
                raise UrlRejected("short_link_dead")
            current = urllib.parse.urljoin(safe, location)
            client.cookies.clear()
    raise UrlRejected("too_many_redirects")


def _unwrap_universal(parts: urllib.parse.SplitResult) -> urllib.parse.SplitResult:
    """Link dạng universal-link?redir=... : lấy link bên trong và kiểm tra lại từ đầu."""
    qs = urllib.parse.parse_qs(parts.query)
    for key in ("redir", "url", "target"):
        if qs.get(key):
            return _check_shape(urllib.parse.unquote(qs[key][0]))
    return parts


def canonicalize(url: str) -> Product:
    parts = _unwrap_universal(_check_shape(url))
    host = parts.hostname.lower().rstrip(".")
    path = urllib.parse.unquote(parts.path)
    if host in SHOPEE_HOSTS:
        for rx in _SHOPEE_ITEM_RES:
            m = rx.search(path)
            if m:
                shop_id, item_id = m.group(1), m.group(2)
                return Product("shopee", f"https://shopee.vn/product/{shop_id}/{item_id}", f"shp:{shop_id}:{item_id}")
        raise UrlRejected("not_a_product")
    if host in TIKTOK_HOSTS:
        m = _TIKTOK_PRODUCT_RE.search(path)
        if m:
            pid = m.group(1)
            return Product("tiktok", f"https://shop.tiktok.com/view/product/{pid}", f"ttk:{pid}")
        raise UrlRejected("not_a_product")
    raise UrlRejected("host_not_allowed")


async def inspect(url: str, transport=None, resolver=None) -> Product:
    """Kiểm tra đầy đủ một link khách gửi → Product đã chuẩn hóa, hoặc UrlRejected."""
    parts = _check_shape(url)
    if parts.hostname.lower().rstrip(".") in SHORT_HOSTS:
        url = await resolve_short_link(url, transport=transport, resolver=resolver)
    return canonicalize(url)


def is_safe_output_link(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return (parts.scheme == "https" and not parts.username and not parts.password
            and (parts.hostname or "").lower() in OUTPUT_HOSTS and len(url) <= 512)
