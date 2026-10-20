"""
Nhật ký chuyển đổi (SQLite). Chỉ lưu: thời điểm, HMAC mã khách, sàn, mã sản phẩm, trạng thái,
mã lỗi, độ trễ, nguồn. Không lưu nội dung tin nhắn, ID Zalo thật hay link khách gửi.
"""

import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    user_hash TEXT NOT NULL,
    platform TEXT,
    item_key TEXT,
    status TEXT NOT NULL,          -- ok | cached | rejected | failed | rate_limited
    error_code TEXT,
    latency_ms INTEGER,
    source TEXT                    -- shopee_openapi | shopee_web | cache | -
);
CREATE INDEX IF NOT EXISTS ix_conv_ts ON conversions(ts);
CREATE TABLE IF NOT EXISTS admin_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    ip TEXT,
    action TEXT NOT NULL,
    detail TEXT
);
"""


class AuditLog:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        # WAL: gateway và worker là hai tiến trình cùng ghi một file
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self):
        with self._lock:
            self._conn.close()

    def record(self, user_hash, platform, item_key, status, error_code=None, latency_ms=None, source=None):
        with self._lock:
            self._conn.execute(
                "INSERT INTO conversions(ts,user_hash,platform,item_key,status,error_code,latency_ms,source) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (int(time.time()), user_hash, platform, item_key, status, error_code, latency_ms, source))
            self._conn.commit()

    def admin_event(self, ip, action, detail=""):
        with self._lock:
            self._conn.execute("INSERT INTO admin_events(ts,ip,action,detail) VALUES (?,?,?,?)",
                               (int(time.time()), ip, action, detail[:200]))
            self._conn.commit()

    def stats(self, days: int):
        since = int(time.time()) - days * 86400
        with self._lock:
            cur = self._conn.cursor()
            by_status = dict(cur.execute(
                "SELECT status, COUNT(*) FROM conversions WHERE ts>=? GROUP BY status", (since,)).fetchall())
            by_platform = dict(cur.execute(
                "SELECT COALESCE(platform,'-'), COUNT(*) FROM conversions WHERE ts>=? GROUP BY platform",
                (since,)).fetchall())
            by_day = cur.execute(
                "SELECT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') d, "
                "SUM(status IN ('ok','cached')), SUM(status NOT IN ('ok','cached')) "
                "FROM conversions WHERE ts>=? GROUP BY d ORDER BY d", (since,)).fetchall()
            errors = cur.execute(
                "SELECT error_code, COUNT(*) c FROM conversions WHERE ts>=? AND error_code IS NOT NULL "
                "GROUP BY error_code ORDER BY c DESC LIMIT 10", (since,)).fetchall()
            avg_latency = cur.execute(
                "SELECT AVG(latency_ms) FROM conversions WHERE ts>=? AND status='ok'", (since,)).fetchone()[0]
            users = cur.execute(
                "SELECT COUNT(DISTINCT user_hash) FROM conversions WHERE ts>=?", (since,)).fetchone()[0]
        total = sum(by_status.values())
        success = by_status.get("ok", 0) + by_status.get("cached", 0)
        return {
            "days": days, "total": total, "success": success,
            "success_rate": round(success * 100 / total, 1) if total else None,
            "cache_hits": by_status.get("cached", 0),
            "unique_users": users,
            "avg_latency_ms": round(avg_latency) if avg_latency else None,
            "by_status": by_status, "by_platform": by_platform,
            "by_day": [{"date": d, "success": s or 0, "failed": f or 0} for d, s, f in by_day],
            "top_errors": [{"code": c, "count": n} for c, n in errors],
        }

    def recent(self, limit: int):
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts,user_hash,platform,item_key,status,error_code,latency_ms,source "
                "FROM conversions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        keys = ("ts", "user_hash", "platform", "item_key", "status", "error_code", "latency_ms", "source")
        return [dict(zip(keys, r)) for r in rows]

    def recent_admin_events(self, limit: int):
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts,ip,action,detail FROM admin_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(zip(("ts", "ip", "action", "detail"), r)) for r in rows]

    def purge(self, retention_days: int) -> int:
        cutoff = int(time.time()) - retention_days * 86400
        with self._lock:
            n = self._conn.execute("DELETE FROM conversions WHERE ts<?", (cutoff,)).rowcount
            self._conn.execute("DELETE FROM admin_events WHERE ts<?", (cutoff,))
            self._conn.commit()
        return n
