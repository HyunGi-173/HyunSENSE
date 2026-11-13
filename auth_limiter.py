"""Best-effort server-process-wide login throttle for the SINGLE shared owner.

Not a substitute for production identity management, a WAF, per-user accounts,
or a distributed/DB-backed rate limiter. It persists only while this process lives.
"""
from __future__ import annotations
import threading
import time
from collections import deque

_lock = threading.Lock()
_failures: deque[float] = deque()
_WINDOW = 15 * 60
_MAX_FAILS = 6


def login_allowed(now: float | None = None) -> tuple[bool, int]:
    moment = time.monotonic() if now is None else now
    with _lock:
        while _failures and _failures[0] <= moment - _WINDOW:
            _failures.popleft()
        if len(_failures) >= _MAX_FAILS:
            return False, max(1, int(_WINDOW - (moment - _failures[0])))
        return True, 0


def record_login_failure(now: float | None = None) -> None:
    moment = time.monotonic() if now is None else now
    with _lock:
        _failures.append(moment)


def record_login_success() -> None:
    with _lock:
        _failures.clear()


def session_timed_out(last_activity: float, now: float, *, idle_seconds: int = 1800) -> bool:
    """True if a protected Streamlit session needs re-authentication."""
    return now < last_activity or now - last_activity > idle_seconds
