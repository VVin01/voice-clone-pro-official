"""Simple, safe rate limiting and upload-size gating for Mini App POSTs.

This module is intentionally small and in-memory only. It is a first
layer of abuse protection, not a replacement for the queue, balance,
and ownership checks in app/services/queue.py and app/webapp/api.py.

Why per-user, not per-IP alone:
    The Mini App runs behind Render's proxy, so many users can share
    one proxy IP. The limiter therefore keys on the Telegram user id
    parsed (without verification, for limiting only) from the
    X-Telegram-Init-Data header when present, and falls back to the
    client IP otherwise. Authentication is still done separately by
    get_current_user(); this limiter never grants access.

What is limited, and what is deliberately NOT limited:
    Limited (POST only): voice generation, recap generation, media
    extraction, recap video upload, and payment-slip submission.
    NOT limited: GET endpoints, including job/status polling, config,
    curated voices, history, and admin reads. Status polling every few
    seconds would otherwise break normal Mini App use.

Limits are generous for normal use (a user generates a handful of
jobs per minute) and are meant to stop bulk abuse that would drain
VoxCPM / Gemini / Groq quotas, not to police ordinary users.
"""

from __future__ import annotations

import time
import urllib.parse
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# path -> (max requests, window seconds)
RATE_LIMITS: dict[str, tuple[int, int]] = {
    "/api/voices/generate-with-audio": (30, 60),
    "/api/voices/generate": (30, 60),
    "/api/recap/generate": (10, 60),
    "/api/recap/upload_video": (20, 60),
    "/api/downloader/extract": (30, 60),
    "/api/vip/submit-slip": (20, 60),
}

# path prefix -> max Content-Length in bytes, when the client sends one.
# Browsers and Telegram WebApp clients send Content-Length, so this
# stops oversized uploads before the body is read. Chunked uploads
# without Content-Length are still bounded by the in-code checks in
# app/webapp/api.py (worker upload: 100 MB; see below for the others).
MAX_CONTENT_LENGTH: dict[str, int] = {
    "/api/vip/submit-slip": 10 * 1024 * 1024,
    "/api/voices/generate-with-audio": 55 * 1024 * 1024,
    "/api/recap/upload_video": 1024 * 1024 * 1024,
    "/api/internal/worker/upload-result": 100 * 1024 * 1024,
}

TOO_MANY_DETAIL = "တောင်းဆိုမှု များနေပါသည်။ ခဏစောင့်ပြီးမှ ပြန်ကြိုးစားပါ။"
TOO_LARGE_DETAIL = "ဖိုင်အရွယ်အစား ကန့်သတ်ချက်ထက် ကျော်လွန်နေပါသည်။"


def _rate_key(request: Request) -> str:
    """Best-effort limiter key: Telegram user id, else client IP."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if init_data:
        # parse_qs gives a JSON string for the user field; extract id safely.
        try:
            import json as _json

            parsed = urllib.parse.parse_qs(init_data)
            user_obj = _json.loads(parsed.get("user", ["{}"])[0])
            user_id = user_obj.get("id")
            if user_id:
                return f"user:{user_id}"
        except Exception:
            pass
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() if forwarded else ""
    if not ip and request.client is not None:
        ip = request.client.host or ""
    return f"ip:{ip or 'unknown'}"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """In-memory sliding-window limiter for selected POST endpoints."""

    def __init__(self, app, limits: dict[str, tuple[int, int]] | None = None):
        super().__init__(app)
        self.limits = limits or RATE_LIMITS
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _is_limited(self, path: str) -> tuple[int, int] | None:
        # Exact match first so generate-with-audio is not confused with
        # generate; then fall back to the longest matching prefix.
        if path in self.limits:
            return self.limits[path]
        best: tuple[int, int] | None = None
        best_len = -1
        for prefix, limit in self.limits.items():
            if path.startswith(prefix) and len(prefix) > best_len:
                best = limit
                best_len = len(prefix)
        return best

    async def dispatch(self, request: Request, call_next):
        # Upload-size gate: only when Content-Length is present.
        if request.method == "POST":
            for prefix, max_bytes in MAX_CONTENT_LENGTH.items():
                if request.url.path.startswith(prefix):
                    length = request.headers.get("content-length")
                    if length is not None:
                        try:
                            if int(length) > max_bytes:
                                return JSONResponse(
                                    status_code=413,
                                    content={"detail": TOO_LARGE_DETAIL},
                                )
                        except ValueError:
                            pass
                    break
        # Rate limit: POST to a listed endpoint only.
        if request.method == "POST":
            limit = self._is_limited(request.url.path)
            if limit is not None:
                max_requests, window = limit
                key = f"{_rate_key(request)}:{request.url.path}"
                now = time.monotonic()
                hits = self._hits[key]
                while hits and hits[0] <= now - window:
                    hits.popleft()
                if len(hits) >= max_requests:
                    return JSONResponse(
                        status_code=429,
                        content={"detail": TOO_MANY_DETAIL},
                        headers={"Retry-After": str(window)},
                    )
                hits.append(now)
        return await call_next(request)
