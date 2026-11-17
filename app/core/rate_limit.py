"""Fixed-window rate limiting.

An in-memory backend serves a single process (development, tests). With
REDIS_URL set, counters live in Redis so that limits hold across processes.
"""

import logging
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from fastapi import Request

from app.core.config import get_settings
from app.core.errors import RateLimitedError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after: int


class RateLimiter(Protocol):
    def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        """Count one event and report whether it is within the limit."""

    def is_blocked(self, key: str, limit: int) -> RateLimitResult:
        """Report whether the limit is already used up, without counting."""

    def reset(self, key: str) -> None: ...


class MemoryRateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, tuple[int, float]] = {}

    def _current(self, key: str, now: float) -> tuple[int, float]:
        count, expires_at = self._counters.get(key, (0, 0.0))
        if expires_at <= now:
            return 0, 0.0
        return count, expires_at

    def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        now = time.monotonic()
        with self._lock:
            count, expires_at = self._current(key, now)
            if count == 0:
                expires_at = now + window_seconds
            count += 1
            self._counters[key] = (count, expires_at)
            if len(self._counters) > 50_000:
                self._prune(now)
        return RateLimitResult(count <= limit, max(1, int(expires_at - now)))

    def is_blocked(self, key: str, limit: int) -> RateLimitResult:
        now = time.monotonic()
        with self._lock:
            count, expires_at = self._current(key, now)
        return RateLimitResult(count < limit, max(1, int(expires_at - now)) if count else 0)

    def reset(self, key: str) -> None:
        with self._lock:
            self._counters.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._counters.clear()

    def _prune(self, now: float) -> None:
        self._counters = {k: v for k, v in self._counters.items() if v[1] > now}


class RedisRateLimiter:
    def __init__(self, client, prefix: str = "novalyn:ratelimit:") -> None:
        self._client = client
        self._prefix = prefix

    def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        name = self._prefix + key
        pipeline = self._client.pipeline()
        pipeline.incr(name)
        pipeline.expire(name, window_seconds, nx=True)
        pipeline.ttl(name)
        count, _, ttl = pipeline.execute()
        return RateLimitResult(int(count) <= limit, max(1, int(ttl)))

    def is_blocked(self, key: str, limit: int) -> RateLimitResult:
        name = self._prefix + key
        pipeline = self._client.pipeline()
        pipeline.get(name)
        pipeline.ttl(name)
        count, ttl = pipeline.execute()
        count = int(count or 0)
        return RateLimitResult(count < limit, max(1, int(ttl)) if count else 0)

    def reset(self, key: str) -> None:
        self._client.delete(self._prefix + key)


@lru_cache
def get_rate_limiter() -> RateLimiter:
    settings = get_settings()
    if settings.redis_url:
        import redis

        return RedisRateLimiter(redis.Redis.from_url(settings.redis_url))
    return MemoryRateLimiter()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def enforce(key: str, limit: int, window_seconds: int) -> None:
    """Count one event against the key and raise when the limit is exceeded."""
    if not get_settings().rate_limit_enabled:
        return
    result = get_rate_limiter().hit(key, limit, window_seconds)
    if not result.allowed:
        raise RateLimitedError(headers={"Retry-After": str(result.retry_after)})


def rate_limit(name: str, limit: int, window_seconds: int):
    """FastAPI dependency: limit requests per client address for one route group."""

    def dependency(request: Request) -> None:
        enforce(f"{name}:{client_ip(request)}", limit, window_seconds)

    return dependency
