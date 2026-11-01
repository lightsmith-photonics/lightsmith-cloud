"""Fixed-window request rate limiting, in memory (one process) or in Redis (shared by API replicas)."""

from __future__ import annotations

import threading
import time

from .errors import ApiError

clock = time.time  # replaceable in tests


class MemoryRateLimiter:
    def __init__(self):
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, int], int] = {}

    def hit(self, key: str, limit: int, window_s: int = 60) -> None:
        if limit <= 0:
            return
        now = clock()
        win = int(now // window_s)
        with self._lock:
            if len(self._counts) > 10_000:
                self._counts = {k: v for k, v in self._counts.items() if k[1] >= win}
            n = self._counts.get((key, win), 0) + 1
            self._counts[(key, win)] = n
        if n > limit:
            _raise(limit, window_s, int((win + 1) * window_s - now) + 1)


class RedisRateLimiter:
    def __init__(self, redis):
        self.redis = redis

    def hit(self, key: str, limit: int, window_s: int = 60) -> None:
        if limit <= 0:
            return
        now = clock()
        win = int(now // window_s)
        k = f"ls:rl:{key}:{win}"
        pipe = self.redis.pipeline()
        pipe.incr(k)
        pipe.expire(k, window_s * 2)
        n = pipe.execute()[0]
        if n > limit:
            _raise(limit, window_s, int((win + 1) * window_s - now) + 1)


def _raise(limit: int, window_s: int, retry: int) -> None:
    raise ApiError(429, "RateLimited", f"more than {limit} requests per {window_s} s", headers={"Retry-After": str(retry)})
