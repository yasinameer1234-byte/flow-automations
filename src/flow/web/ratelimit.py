"""Small in-memory sliding-window rate limiter (one process; enough for one instance).

Use a shared store such as Redis once the app runs on more than one machine.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float, clock=time.monotonic) -> None:
        self.limit = limit
        self.window = window_seconds
        self.clock = clock
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, q: deque, now: float) -> None:
        while q and now - q[0] >= self.window:
            q.popleft()

    def blocked(self, key: str) -> bool:
        """True if this key has used up its allowance (does not count a new hit)."""
        with self._lock:
            q = self._hits[key]
            self._trim(q, self.clock())
            return len(q) >= self.limit

    def hit(self, key: str) -> bool:
        """Count a hit; return True if it is allowed, False if over the limit."""
        with self._lock:
            now = self.clock()
            q = self._hits[key]
            self._trim(q, now)
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)
