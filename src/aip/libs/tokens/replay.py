"""jti 防重放存储（architecture.md 3.1）：Valkey 为主，不可用时退化为进程内缓存并收紧有效期。"""

from __future__ import annotations

import time
from typing import Protocol

import redis.asyncio as redis
from redis.exceptions import RedisError


class ReplayStore(Protocol):
    degraded: bool

    async def first_use(self, key: str, ttl_s: int) -> bool:
        """首次出现返回 True；已用过返回 False。"""


class MemoryReplayStore:
    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self.degraded = False

    async def first_use(self, key: str, ttl_s: int) -> bool:
        now = time.monotonic()
        if len(self._seen) > 100_000:
            self._seen = {k: v for k, v in self._seen.items() if v > now}
        if self._seen.get(key, 0) > now:
            return False
        self._seen[key] = now + max(ttl_s, 1)
        return True


class ValkeyReplayStore:
    """SET NX EX；Valkey 异常时切到内存兜底，并把 degraded 置为 True，调用方据此收紧策略。"""

    def __init__(self, url: str) -> None:
        self._r = redis.from_url(url, socket_timeout=0.2, socket_connect_timeout=0.2)
        self._fallback = MemoryReplayStore()
        self.degraded = False

    async def first_use(self, key: str, ttl_s: int) -> bool:
        try:
            ok = await self._r.set(f"jti:{key}", b"1", nx=True, ex=max(ttl_s, 1))
            self.degraded = False
            return bool(ok)
        except (RedisError, OSError):
            self.degraded = True
            return await self._fallback.first_use(key, ttl_s)

    async def aclose(self) -> None:
        await self._r.aclose()
