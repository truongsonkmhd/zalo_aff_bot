"""
Kho trạng thái dùng chung: hạn mức, chống trùng, cache, hàng đợi, khóa phân tán.
MemoryStore cho chạy thử một tiến trình; RedisStore cho chạy thật (nhiều tiến trình).
"""

import asyncio
import json
import secrets
import time


class MemoryStore:
    def __init__(self):
        self._kv = {}          # key -> (value, expires_at)
        self._queue = asyncio.Queue()
        self._dlq = []
        self._lock = asyncio.Lock()

    def _alive(self, key):
        item = self._kv.get(key)
        if item and item[1] and item[1] < time.time():
            self._kv.pop(key, None)
            return None
        return item

    async def incr_window(self, key: str, ttl: int) -> int:
        async with self._lock:
            item = self._alive(key)
            value = (item[0] if item else 0) + 1
            self._kv[key] = (value, item[1] if item else time.time() + ttl)
            return value

    async def set_once(self, key: str, ttl: int) -> bool:
        async with self._lock:
            if self._alive(key):
                return False
            self._kv[key] = (1, time.time() + ttl)
            return True

    async def get(self, key: str):
        item = self._alive(key)
        return item[0] if item else None

    async def set(self, key: str, value: str, ttl: int = 0):
        self._kv[key] = (value, time.time() + ttl if ttl else 0)

    async def delete(self, key: str):
        self._kv.pop(key, None)

    async def enqueue(self, job: dict):
        await self._queue.put(json.dumps(job))

    async def dequeue(self, timeout: float):
        try:
            return json.loads(await asyncio.wait_for(self._queue.get(), timeout))
        except asyncio.TimeoutError:
            return None

    async def queue_len(self) -> int:
        return self._queue.qsize()

    async def push_dlq(self, job: dict):
        self._dlq.append(json.dumps(job))
        del self._dlq[:-500]

    async def dlq_items(self, limit: int = 50):
        return [json.loads(x) for x in self._dlq[-limit:]][::-1]

    async def dlq_len(self) -> int:
        return len(self._dlq)

    async def dlq_clear(self):
        self._dlq.clear()

    async def acquire_lock(self, key: str, ttl: int):
        token = secrets.token_hex(8)
        return token if await self.set_once(key, ttl) else None

    async def release_lock(self, key: str, token: str):
        await self.delete(key)

    async def close(self):
        pass


class RedisStore:
    QUEUE = "aff:queue"
    DLQ = "aff:dlq"

    def __init__(self, url: str):
        import redis.asyncio as redis
        self._r = redis.from_url(url, decode_responses=True)

    async def incr_window(self, key, ttl):
        pipe = self._r.pipeline()
        pipe.incr(key)
        pipe.expire(key, ttl, nx=True)
        value, _ = await pipe.execute()
        return int(value)

    async def set_once(self, key, ttl):
        return bool(await self._r.set(key, "1", ex=ttl, nx=True))

    async def get(self, key):
        return await self._r.get(key)

    async def set(self, key, value, ttl=0):
        await self._r.set(key, value, ex=ttl or None)

    async def delete(self, key):
        await self._r.delete(key)

    async def enqueue(self, job):
        await self._r.lpush(self.QUEUE, json.dumps(job))

    async def dequeue(self, timeout):
        item = await self._r.brpop(self.QUEUE, timeout=max(1, int(timeout)))
        return json.loads(item[1]) if item else None

    async def queue_len(self):
        return await self._r.llen(self.QUEUE)

    async def push_dlq(self, job):
        pipe = self._r.pipeline()
        pipe.lpush(self.DLQ, json.dumps(job))
        pipe.ltrim(self.DLQ, 0, 499)
        await pipe.execute()

    async def dlq_items(self, limit=50):
        return [json.loads(x) for x in await self._r.lrange(self.DLQ, 0, limit - 1)]

    async def dlq_len(self):
        return await self._r.llen(self.DLQ)

    async def dlq_clear(self):
        await self._r.delete(self.DLQ)

    async def acquire_lock(self, key, ttl):
        token = secrets.token_hex(8)
        return token if await self._r.set(key, token, ex=ttl, nx=True) else None

    async def release_lock(self, key, token):
        # Chỉ xóa khóa do chính mình giữ
        script = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
        await self._r.eval(script, 1, key, token)

    async def close(self):
        await self._r.aclose()


def make_store(redis_url: str):
    return RedisStore(redis_url) if redis_url else MemoryStore()
