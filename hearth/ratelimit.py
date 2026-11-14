"""In-process rate limiter + account lockout for auth endpoints.

Not distributed (single uvicorn process assumption for family scale). Tracks
failed attempts per (ip, username). On exceeding threshold, locks the pair for
lockout_seconds. Successful login resets counters. Thread-safe via a lock since
FastAPI may run sync handlers on a threadpool.
"""
from __future__ import annotations
import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    fails: int = 0
    first_fail: int = 0
    locked_until: int = 0


class RateLimiter:
    def __init__(self, max_attempts: int, lockout_seconds: int):
        self.max = max_attempts
        self.lockout = lockout_seconds
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def _key(self, ip: str, user: str) -> str:
        return f"{ip}|{user.lower()}"

    def is_locked(self, ip: str, user: str) -> tuple[bool, int]:
        with self._lock:
            b = self._buckets.get(self._key(ip, user))
            if not b:
                return False, 0
            remaining = b.locked_until - int(time.time())
            return (remaining > 0), max(0, remaining)

    def record_failure(self, ip: str, user: str) -> None:
        now = int(time.time())
        with self._lock:
            k = self._key(ip, user)
            b = self._buckets.get(k) or _Bucket()
            if b.first_fail == 0 or now - b.first_fail > self.lockout:
                b.fails, b.first_fail = 0, now
            b.fails += 1
            if b.fails >= self.max:
                b.locked_until = now + self.lockout
            self._buckets[k] = b

    def record_success(self, ip: str, user: str) -> None:
        with self._lock:
            self._buckets.pop(self._key(ip, user), None)


_limiter: RateLimiter | None = None


def get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        from .config import get_boot_settings
        bs = get_boot_settings()
        _limiter = RateLimiter(bs.login_max_attempts, bs.login_lockout_seconds)
    return _limiter
