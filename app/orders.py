"""
Tra cứu đơn hàng cho khách.

Mã tra cứu: 10 ký tự sinh từ ID Zalo bằng HMAC (khóa bí mật, tách miền "lookup:"), không đoán được,
không lộ ID Zalo thật. Mã được gắn vào sub_id của mọi link affiliate gửi cho khách, nên đơn trong báo cáo
chuyển đổi của Shopee tự khớp về đúng khách.

Nguồn đơn hàng:
  - Shopee Affiliate Open API (conversionReport) — đồng bộ định kỳ
  - File CSV báo cáo chuyển đổi xuất từ affiliate.shopee.vn — quản trị tải lên (dùng với Phương án 2)
Chỉ lưu thông tin đơn (mã đơn, trạng thái, thời gian, tên sản phẩm, giá trị, hoa hồng). Báo cáo affiliate
không chứa thông tin người mua, và bot cũng không lưu.
"""

import csv
import datetime
import hashlib
import hmac
import io
import json
import re
import sqlite3
import threading
import time
import unicodedata

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # bỏ 0/O, 1/I để khách đọc không nhầm
CODE_LEN = 10                                          # 50 bit
_CODE_RE = re.compile(rf"^[{CODE_ALPHABET}]{{{CODE_LEN}}}$")
SUBID_PREFIX = "KH"                                    # sub_id = "KH" + mã, tránh nhầm với tên chiến dịch
_CODE_TOKEN_RE = re.compile(rf"(?<![A-Z0-9]){SUBID_PREFIX}([{CODE_ALPHABET}]{{{CODE_LEN}}})(?![A-Z0-9])")
_ORDER_ID_RE = re.compile(r"^[A-Z0-9]{8,32}$")

STATUS_LABELS = {"pending": "Đang chờ xác nhận", "completed": "Hoàn thành", "cancelled": "Đã hủy"}


# ── Mã tra cứu ────────────────────────────────────────────────────
def customer_code(user_id: str, key: str) -> str:
    digest = hmac.new(key.encode(), b"lookup:" + str(user_id).encode(), hashlib.sha256).digest()
    n = int.from_bytes(digest[:8], "big")
    out = []
    for _ in range(CODE_LEN):
        n, r = divmod(n, len(CODE_ALPHABET))
        out.append(CODE_ALPHABET[r])
    return "".join(out)


def code_sub_id(code: str) -> str:
    return SUBID_PREFIX + code


def normalize_query(q: str):
    """Trả về ("code", mã) | ("order", mã_đơn) | (None, None)."""
    q = re.sub(r"[\s\-_.]", "", str(q or "")).upper()[:40]
    if _CODE_RE.match(q):
        return "code", q
    if _ORDER_ID_RE.match(q):
        return "order", q
    return None, None


def find_code(values) -> str:
    """Tìm mã tra cứu trong các sub_id / utmContent của một đơn."""
    for v in values:
        for m in _CODE_TOKEN_RE.finditer(str(v or "").upper()):
            return m.group(1)
    return ""


def normalize_status(raw: str) -> str:
    s = _fold(raw)
    if any(k in s for k in ("complet", "hoan thanh", "thanh cong", "settled", "paid")):
        return "completed"
    if any(k in s for k in ("cancel", "huy", "invalid", "fail", "khong hop le")):
        return "cancelled"
    return "pending"


# ── Kho đơn hàng ─────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    code TEXT,
    status TEXT NOT NULL,
    purchase_ts INTEGER,
    items TEXT NOT NULL,            -- JSON [{"name","qty"}]
    order_value INTEGER,
    commission INTEGER,
    source TEXT,
    updated_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_orders_code ON orders(code);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


class OrderStore:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self):
        with self._lock:
            self._conn.close()

    def upsert(self, orders, source: str) -> int:
        now = int(time.time())
        with self._lock:
            for o in orders:
                self._conn.execute(
                    "INSERT INTO orders(order_id,code,status,purchase_ts,items,order_value,commission,source,updated_ts) "
                    "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET "
                    "code=COALESCE(NULLIF(excluded.code,''),orders.code), status=excluded.status, "
                    "purchase_ts=COALESCE(excluded.purchase_ts,orders.purchase_ts), items=excluded.items, "
                    "order_value=excluded.order_value, commission=excluded.commission, source=excluded.source, "
                    "updated_ts=excluded.updated_ts",
                    (o["order_id"], o.get("code") or "", o["status"], o.get("purchase_ts"),
                     json.dumps(o.get("items", []), ensure_ascii=False), o.get("order_value"), o.get("commission"),
                     source, now))
            self._conn.commit()
        return len(orders)

    def _rows(self, sql, args):
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        keys = ("order_id", "code", "status", "purchase_ts", "items", "order_value", "commission", "source", "updated_ts")
        out = []
        for r in rows:
            d = dict(zip(keys, r))
            d["items"] = json.loads(d["items"] or "[]")
            out.append(d)
        return out

    def by_code(self, code: str, limit: int = 100):
        return self._rows("SELECT * FROM orders WHERE code=? ORDER BY purchase_ts DESC LIMIT ?", (code, limit))

    def by_order_id(self, order_id: str):
        return self._rows("SELECT * FROM orders WHERE order_id=?", (order_id,))

    def recent(self, limit: int = 50):
        return self._rows("SELECT * FROM orders ORDER BY COALESCE(purchase_ts, updated_ts) DESC LIMIT ?", (limit,))

    def summary(self):
        with self._lock:
            by_status = dict(self._conn.execute("SELECT status, COUNT(*) FROM orders GROUP BY status").fetchall())
            customers = self._conn.execute("SELECT COUNT(DISTINCT code) FROM orders WHERE code != ''").fetchone()[0]
            unmatched = self._conn.execute("SELECT COUNT(*) FROM orders WHERE code = ''").fetchone()[0]
            commission = self._conn.execute(
                "SELECT COALESCE(SUM(commission),0) FROM orders WHERE status != 'cancelled'").fetchone()[0]
            meta = dict(self._conn.execute("SELECT k, v FROM meta").fetchall())
        return {"total": sum(by_status.values()), "by_status": by_status, "customers": customers,
                "unmatched": unmatched, "commission": commission,
                "last_sync": json.loads(meta["last_sync"]) if "last_sync" in meta else None}

    def set_meta(self, key, value):
        with self._lock:
            self._conn.execute("INSERT INTO meta(k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                               (key, json.dumps(value, ensure_ascii=False)))
            self._conn.commit()


def public_view(order: dict, cashback_percent: int) -> dict:
    """Thông tin đơn cho khách xem: không có mã khách, không có hoa hồng gốc của shop."""
    view = {
        "order_id": order["order_id"],
        "status": order["status"],
        "status_label": STATUS_LABELS.get(order["status"], order["status"]),
        "purchase_ts": order["purchase_ts"],
        "items": [{"name": str(i.get("name", ""))[:200], "qty": i.get("qty") or 1} for i in order["items"][:20]],
        "order_value": order["order_value"],
    }
    if cashback_percent and order.get("commission") is not None and order["status"] != "cancelled":
        view["cashback"] = int(order["commission"] * cashback_percent / 100)
    return view


# ── Shopee Open API: conversionReport ─────────────────────────────
CONVERSION_QUERY = (
    "query{conversionReport(purchaseTimeStart:%d,purchaseTimeEnd:%d,limit:500%s){"
    "nodes{purchaseTime utmContent orders{orderId orderStatus items{itemName itemPrice qty itemTotalCommission}}}"
    "pageInfo{hasNextPage scrollId}}}"
)


def parse_conversion_nodes(nodes):
    """Chuyển các node conversionReport thành danh sách đơn. Một đơn xuất hiện trong một node."""
    out = []
    for node in nodes or []:
        code = find_code([node.get("utmContent"), *(node.get("subIds") or [])])
        for o in node.get("orders") or []:
            oid = str(o.get("orderId") or "").strip().upper()
            if not _ORDER_ID_RE.match(oid):
                continue
            items = o.get("items") or []
            out.append({
                "order_id": oid,
                "code": code,
                "status": normalize_status(o.get("orderStatus")),
                "purchase_ts": _to_ts(node.get("purchaseTime")),
                "items": [{"name": str(i.get("itemName") or ""), "qty": _to_int(i.get("qty")) or 1} for i in items],
                "order_value": sum((_to_int(i.get("itemPrice")) or 0) * (_to_int(i.get("qty")) or 1) for i in items),
                "commission": sum(_to_int(i.get("itemTotalCommission")) or 0 for i in items),
            })
    return out


# ── CSV báo cáo chuyển đổi xuất từ affiliate.shopee.vn ────────────
HEADER_ALIASES = {
    "order_id": ("order id", "orderid", "order_id", "ma don hang", "id don hang", "ma don"),
    "status": ("order status", "trang thai dat hang", "trang thai don hang", "trang thai"),
    "purchase_time": ("purchase time", "thoi gian dat hang", "order time", "ngay dat hang", "thoi gian mua"),
    "item_name": ("item name", "ten item", "ten san pham", "product name", "san pham"),
    "qty": ("qty", "quantity", "so luong"),
    "price": ("item price", "price", "gia", "gia tri don hang", "purchase value", "gia tri mua"),
    "commission": ("net commission", "item total commission", "hoa hong rong", "tong hoa hong",
                   "hoa hong san pham", "hoa hong"),
}


def _fold(s) -> str:
    s = unicodedata.normalize("NFD", str(s or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").replace("đ", "d")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _match_columns(headers):
    folded = [_fold(h) for h in headers]
    mapping = {}
    for field, aliases in HEADER_ALIASES.items():
        for alias in aliases:                     # ưu tiên alias cụ thể hơn (đứng trước)
            for idx, h in enumerate(folded):
                if idx not in mapping.values() and (h == alias or h.startswith(alias + " ")):
                    mapping[field] = idx
                    break
            if field in mapping:
                break
    sub_cols = [i for i, h in enumerate(folded) if "sub" in h.split() or h.startswith("sub ") or "utm" in h]
    return mapping, sub_cols


def parse_conversion_csv(text: str):
    """Trả về (danh_sách_đơn, thông_tin). Gộp các dòng sản phẩm cùng mã đơn."""
    if text.startswith("﻿"):
        text = text[1:]
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows:
        return [], {"rows": 0, "skipped": 0, "missing": ["order_id"]}
    mapping, sub_cols = _match_columns(rows[0])
    missing = [f for f in ("order_id", "status") if f not in mapping]
    if missing:
        return [], {"rows": len(rows) - 1, "skipped": len(rows) - 1, "missing": missing}

    orders, skipped = {}, 0
    get = lambda r, f: r[mapping[f]].strip() if f in mapping and mapping[f] < len(r) else ""
    for r in rows[1:]:
        if not any(c.strip() for c in r):
            continue
        oid = re.sub(r"\s", "", get(r, "order_id")).upper()
        if not _ORDER_ID_RE.match(oid):
            skipped += 1
            continue
        qty = _to_int(get(r, "qty")) or 1
        o = orders.setdefault(oid, {"order_id": oid, "code": "", "status": normalize_status(get(r, "status")),
                                    "purchase_ts": _parse_time(get(r, "purchase_time")), "items": [],
                                    "order_value": 0, "commission": 0})
        o["code"] = o["code"] or find_code([r[i] for i in sub_cols if i < len(r)])
        name = get(r, "item_name")
        if name:
            o["items"].append({"name": name, "qty": qty})
        o["order_value"] += (_to_int(get(r, "price")) or 0) * (qty if "qty" in mapping else 1)
        o["commission"] += _to_int(get(r, "commission")) or 0
    return list(orders.values()), {"rows": len(rows) - 1, "skipped": skipped, "missing": []}


# ── Tiện ích ──────────────────────────────────────────────────────
def _to_int(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = re.sub(r"[^\d.,-]", "", str(v))
    s = re.sub(r"[.,]\d{1,2}$", "", s)            # bỏ phần lẻ kiểu 1.234,50 / 1,234.50
    s = re.sub(r"[.,]", "", s)
    try:
        return int(s)
    except ValueError:
        return None


def _to_ts(v):
    n = _to_int(v)
    if n:
        return n // 1000 if n > 10_000_000_000 else n
    return None


def _parse_time(s: str):
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return _to_ts(s)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return int(datetime.datetime.strptime(s, fmt).timestamp())
        except ValueError:
            continue
    return None
