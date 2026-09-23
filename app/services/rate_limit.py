"""Minimal in-process rate limiter for public, unauthenticated endpoints
(currently: POST /api/auth/signup - see app.web_api).

No external service (Redis, etc.) exists anywhere in this project (the web
app runs as a single uvicorn worker process, see
travel-ai-orchestrator-web.service) - a plain in-memory sliding window is
enough and keeps the dependency footprint at zero. Not meant to defend
against a distributed attack; it raises the bar for casual scripted signup
abuse from a single source without adding infrastructure.
"""

from __future__ import annotations

import time
from collections import defaultdict


class SlidingWindowRateLimiter:
    def __init__(self, *, max_events: int, window_seconds: float) -> None:
        self._max_events = max_events
        self._window_seconds = window_seconds
        self._events: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str) -> bool:
        """True if `key` (e.g. a client IP) is still under the limit -
        also records this attempt as an event, whether or not it's
        allowed, so a caller spamming past the limit doesn't get a free
        reset of the window."""
        now = time.monotonic()
        cutoff = now - self._window_seconds
        events = self._events[key]
        while events and events[0] < cutoff:
            events.pop(0)
        if len(events) >= self._max_events:
            return False
        events.append(now)
        return True


# 5 signup attempts per IP per hour - generous for a real visitor (who
# only ever needs one), tight enough to blunt a scripted loop.
signup_rate_limiter = SlidingWindowRateLimiter(max_events=5, window_seconds=3600)
