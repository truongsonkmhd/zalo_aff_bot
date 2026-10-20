"""Hạn mức theo khách (phút, ngày) và toàn hệ thống (phút)."""

import time


class RateLimiter:
    def __init__(self, store, per_user_per_min: int, per_user_per_day: int, global_per_min: int):
        self.store = store
        self.per_user_per_min = per_user_per_min
        self.per_user_per_day = per_user_per_day
        self.global_per_min = global_per_min

    async def allow(self, user_hash: str, cost: int = 1, now: float = None):
        """Trả về (cho_phép, lý_do). cost = số link trong tin."""
        now = time.time() if now is None else now
        minute = int(now // 60)
        day = time.strftime("%Y%m%d", time.gmtime(now))
        checks = (
            (f"rl:g:{minute}", 120, self.global_per_min, "global"),
            (f"rl:m:{user_hash}:{minute}", 120, self.per_user_per_min, "user_minute"),
            (f"rl:d:{user_hash}:{day}", 2 * 86400, self.per_user_per_day, "user_day"),
        )
        for key, ttl, limit, reason in checks:
            count = 0
            for _ in range(cost):
                count = await self.store.incr_window(key, ttl)
            if count > limit:
                return False, reason
        return True, "ok"
