"""In-memory per-IP rate limiting for the open registration endpoint.

Resets on process restart, which is acceptable for a self-hosted install.
"""

import time
from collections import defaultdict

_MAX_ATTEMPTS = 5
_WINDOW_SECONDS = 15 * 60


class RateLimiter:
    """Sliding-window limiter keyed by a client identifier (IP)."""

    def __init__(self, max_attempts: int = _MAX_ATTEMPTS, window_seconds: int = _WINDOW_SECONDS) -> None:
        self._max = max_attempts
        self._window = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str) -> bool:
        """Record an attempt for ``key``; return False when over the limit."""
        now = time.monotonic()
        recent = [ts for ts in self._hits[key] if now - ts < self._window]
        self._hits[key] = recent
        if len(recent) >= self._max:
            return False
        self._hits[key].append(now)
        return True

    def reset(self) -> None:
        """Clear all recorded attempts."""
        self._hits.clear()


rate_limiter = RateLimiter()