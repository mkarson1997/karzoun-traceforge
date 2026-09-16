from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable


class ClientRateLimiter:
    """Bounded sliding-window limiter keyed by an already-derived client identity.

    The limiter intentionally keeps no request payloads. Client keys should be stable,
    non-secret identifiers such as an authenticated certificate thumbprint. The client
    table is bounded so an attacker cannot turn rate-limit bookkeeping into a memory
    exhaustion primitive.
    """

    def __init__(
        self,
        limit_per_minute: int,
        *,
        max_clients: int = 4096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit_per_minute < 0:
            raise ValueError("limit_per_minute must be zero or greater")
        if max_clients < 1:
            raise ValueError("max_clients must be at least 1")
        self.limit_per_minute = limit_per_minute
        self.max_clients = max_clients
        self._clock = clock
        self._windows: dict[str, deque[float]] = {}

    @property
    def enabled(self) -> bool:
        return self.limit_per_minute > 0

    @property
    def tracked_clients(self) -> int:
        return len(self._windows)

    def allow(self, client_key: str) -> bool:
        if not self.enabled:
            return True

        now = self._clock()
        cutoff = now - 60.0
        window = self._windows.get(client_key)
        if window is None:
            if len(self._windows) >= self.max_clients:
                self._evict_expired(cutoff)
            if len(self._windows) >= self.max_clients:
                return False
            window = deque()
            self._windows[client_key] = window

        while window and window[0] <= cutoff:
            window.popleft()

        if len(window) >= self.limit_per_minute:
            return False

        window.append(now)
        return True

    def _evict_expired(self, cutoff: float) -> None:
        stale = [
            client_key
            for client_key, window in self._windows.items()
            if not window or window[-1] <= cutoff
        ]
        for client_key in stale:
            self._windows.pop(client_key, None)
