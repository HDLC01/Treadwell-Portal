"""Tiny in-process sliding-window rate limiter. Single uvicorn worker, so a
process-local store is sufficient for the portal's volume. Not shared across
workers — fine here; revisit (Redis) only if we scale out."""
from __future__ import annotations

import time
from collections import deque

_email_hits: dict[str, deque] = {}
_ip_hits: dict[str, deque] = {}
_email_last: dict[str, float] = {}


def _prune(dq: deque, window: int, now: float) -> None:
    while dq and now - dq[0] > window:
        dq.popleft()


def allow_ip(ip: str, limit: int, window: int) -> bool:
    if not ip:
        return True
    now = time.time()
    dq = _ip_hits.setdefault(ip, deque())
    _prune(dq, window, now)
    if len(dq) >= limit:
        return False
    dq.append(now)
    return True


def allow_otp(email: str, per_email: int, window: int, cooldown: int) -> tuple[bool, int]:
    """Returns (allowed, seconds_to_wait). Enforces a min gap (cooldown) between
    codes and a per-email cap per window — blocks email-bombing and the
    'resend to reset the attempt counter' brute-force."""
    now = time.time()
    last = _email_last.get(email, 0.0)
    if now - last < cooldown:
        return False, int(cooldown - (now - last)) + 1
    dq = _email_hits.setdefault(email, deque())
    _prune(dq, window, now)
    if len(dq) >= per_email:
        return False, int(window - (now - dq[0])) + 1
    dq.append(now)
    _email_last[email] = now
    return True, 0
