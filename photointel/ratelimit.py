"""Stop password and PIN guessing: after too many wrong tries in a window, that door stays
shut for a while — even to the right answer, so guessing gains nothing.

Kept in memory, per server process (a restart clears it). Keys name what is being guessed
and from where: a device's address, and for logins also the account name, so spreading the
guesses over many devices does not help either.
"""
from __future__ import annotations

import threading
import time


class Limiter:
    def __init__(self, max_failures: int, window_s: float, lockout_s: float):
        self.max_failures, self.window_s, self.lockout_s = max_failures, window_s, lockout_s
        self._fails: dict[str, list[float]] = {}
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait_for(self, *keys: str) -> float:
        """Seconds until these keys may try again (0 = now)."""
        now = time.time()
        with self._lock:
            return max((self._until.get(k, 0) - now for k in keys), default=0) if keys else 0

    def fail(self, *keys: str) -> None:
        now = time.time()
        with self._lock:
            for k in keys:
                recent = [t for t in self._fails.get(k, []) if now - t < self.window_s] + [now]
                self._fails[k] = recent
                if len(recent) >= self.max_failures:
                    self._until[k] = now + self.lockout_s
                    self._fails[k] = []

    def succeed(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._fails.pop(k, None)


LOGINS = Limiter(max_failures=10, window_s=15 * 60, lockout_s=15 * 60)
PINS = Limiter(max_failures=5, window_s=15 * 60, lockout_s=15 * 60)


def refuse_message(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    return f"too many wrong tries; try again in {minutes} minute{'s' if minutes != 1 else ''}"


def reset_all() -> None:
    """Forget every failure (tests; a server restart does the same)."""
    for lim in (LOGINS, PINS):
        with lim._lock:
            lim._fails.clear()
            lim._until.clear()
