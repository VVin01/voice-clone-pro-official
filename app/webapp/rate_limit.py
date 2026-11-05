"""Simple, safe rate limiting and upload-size gating for Mini App POSTs.

This module is intentionally small and in-memory only. It is a first
layer of abuse protection, not a replacement for the queue, balance,
and ownership checks in app/services/queue.py and app/webapp/api.py.

Why IP + user id combined, not either alone:
    The Mini App runs behind Render's proxy, so many users can share
    one proxy IP — keying on IP alone would throttle innocents
    together. Keying on the Telegram user id alone is worse: the id
    is parsed (without verification, for limiting only) from the
    X-Telegram-Init-Data header, so anyone who knew a victim's id
    could spend that victim's quota from anywhere. The limiter
    therefore keys on the pair (client IP, parsed user id when
    present): a forged id only ever lands in buckets namespaced by
    the forger's own address, never the real owner's, and distinct
    users behind one proxy IP still get separate buckets.
    Authentication is still done separately by get_current_user();
    this limiter never grants access.

What is limited, and what is deliberately NOT limited:
    Limited (POST, plus the one listed DELETE): voice generation,
    recap generation and its per-job actions (retry / cancel /
    render / send-to-Telegram), media extraction and fetch, recap
    video upload, the recap demo send, preview frames, payment-slip
    submission, the send-to-Telegram posts, and history deletion.
    Per-job routes are listed as single-segment templates
    (``/api/recap/{job_id}/retry``). NOT limited: GET endpoints,
    including job/status polling, config, curated voices, history
    reads, and admin reads. Status polling every few seconds would
    otherwise break normal Mini App use.

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
    # Batch N: the remaining expensive / state-changing endpoints, in
    # the same generous band as the originals — these stop scripted
    # abuse (quota drain, Telegram spam, disk churn), not a human
    # tapping buttons.
    "/api/downloader/fetch": (20, 60),
    "/api/recap/demo/send": (10, 60),
    "/api/recap/preview-frame": (60, 60),
    # Single-segment templates: "{param}" matches any one segment.
    "/api/recap/{job_id}/retry": (10, 60),
    "/api/recap/{job_id}/cancel": (30, 60),
    "/api/recap/{job_id}/render": (20, 60),
    "/api/recap/{job_id}/send-telegram": (20, 60),
    "/api/voices/{job_id}/send-telegram": (20, 60),
    "/api/downloader/{download_id}/send-telegram": (20, 60),
    "/api/history/{job_id}": (30, 60),
    # Batch AJ (L10): the remaining abusable POSTs join the table —
    # membership re-verification (Telegram API calls per hit), the
    # branding-logo upload (disk write), and the voice-job cancel.
    "/api/user/verify-membership": (20, 60),
    "/api/user/logo": (20, 60),
    "/api/jobs/{job_id}/cancel": (30, 60),
}

# GET endpoints are deliberately NOT rate limited (status polling
# must stay cheap) — with ONE exception (Batch AJ, L10): the recap
# frame endpoint runs an ffmpeg extraction per hit, so it is an
# unauthenticated-cost amplifier without a cap.
GET_RATE_LIMITS: dict[str, tuple[int, int]] = {
    "/api/recap/frame": (30, 60),
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


def _client_ip(request: Request) -> str:
    """Best-effort client IP: the LAST X-Forwarded-For hop, else the
    direct peer address.

    Batch AJ (L10): the first hop used to be trusted, but the first
    hop is whatever the CLIENT claims — trivially rotatable to
    escape IP-keyed buckets. The platform proxy (Render) APPENDS the
    address it observed, so the last hop is the trustworthy one."""
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[-1].strip() if forwarded else ""
    if not ip and request.client is not None:
        ip = request.client.host or ""
    return ip or "unknown"


def _rate_key(request: Request) -> str:
    """Limiter key: client IP combined with the Telegram user id
    parsed (UNVERIFIED, for limiting only) from the init-data header
    when present; the IP alone otherwise. See the module docstring
    for why neither half is trusted on its own."""
    ip = _client_ip(request)
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if init_data:
        # parse_qs gives a JSON string for the user field; extract id safely.
        try:
            import json as _json

            parsed = urllib.parse.parse_qs(init_data)
            user_obj = _json.loads(parsed.get("user", ["{}"])[0])
            user_id = user_obj.get("id")
            if user_id:
                return f"ipuser:{ip}:{user_id}"
        except Exception:
            pass
    return f"ip:{ip}"


def _template_matches(template: str, path: str) -> bool:
    """Segment-wise template match: a ``{param}`` segment matches any
    single non-empty segment (a job id, a download id); every other
    segment must match literally, and the segment counts must agree."""
    template_parts = template.split("/")
    path_parts = path.split("/")
    if len(template_parts) != len(path_parts):
        return False
    for template_seg, path_seg in zip(template_parts, path_parts):
        if template_seg.startswith("{") and template_seg.endswith("}"):
            if not path_seg:
                return False
            continue
        if template_seg != path_seg:
            return False
    return True


class RateLimitMiddleware(BaseHTTPMiddleware):
    """In-memory sliding-window limiter for selected POST/DELETE endpoints."""

    def __init__(self, app, limits: dict[str, tuple[int, int]] | None = None):
        super().__init__(app)
        self.limits = limits or RATE_LIMITS
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _is_limited(self, path: str) -> tuple[int, int] | None:
        # Exact match first so generate-with-audio is not confused
        # with generate; then single-segment templates; then the
        # longest matching prefix (template keys never take part in
        # prefix matching).
        if path in self.limits:
            return self.limits[path]
        for template, limit in self.limits.items():
            if "{" in template and _template_matches(template, path):
                return limit
        best: tuple[int, int] | None = None
        best_len = -1
        for prefix, limit in self.limits.items():
            if "{" in prefix:
                continue
            if path.startswith(prefix) and len(prefix) > best_len:
                best = limit
                best_len = len(prefix)
        return best

    def _hits_for(self, key: str, limit: tuple[int, int], now: float) -> bool:
        """Record one hit; return True when the caller is OVER the limit."""
        max_requests, window = limit
        hits = self._hits[key]
        while hits and hits[0] <= now - window:
            hits.popleft()
        if len(hits) >= max_requests:
            return True
        hits.append(now)
        return False

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
        # Rate limit: POST to a listed endpoint, plus DELETE for the
        # listed destructive route (history delete). The upload-size
        # gate above stays POST-only.
        if request.method in ("POST", "DELETE"):
            limit = self._is_limited(request.url.path)
            if limit is not None:
                key = f"{_rate_key(request)}:{request.url.path}"
                if self._hits_for(key, limit, time.monotonic()):
                    return JSONResponse(
                        status_code=429,
                        content={"detail": TOO_MANY_DETAIL},
                        headers={"Retry-After": str(limit[1])},
                    )
        elif request.method == "GET" and request.url.path in GET_RATE_LIMITS:
            # Batch AJ (L10): the one GET with a real per-hit cost
            # (ffmpeg frame extraction) gets a bucket too.
            limit = GET_RATE_LIMITS[request.url.path]
            key = f"{_rate_key(request)}:{request.url.path}"
            if self._hits_for(key, limit, time.monotonic()):
                return JSONResponse(
                    status_code=429,
                    content={"detail": TOO_MANY_DETAIL},
                    headers={"Retry-After": str(limit[1])},
                )
        return await call_next(request)


class ChunkedUploadCapMiddleware:
    """Pure-ASGI byte-counting cap for upload endpoints (Batch AJ, H3).

    The Content-Length gate in RateLimitMiddleware only fires when
    the client SENDS the header — a chunked transfer (or a lying
    header) sailed past it, leaving each endpoint's own streaming cap
    as the only defence. This middleware counts the ACTUAL body bytes
    for the capped paths:

    * Small caps (≤ 64 MB — slips, reference audio): the body is
      buffered up to the cap and refused with a clean 413 before the
      application ever sees it; under-cap bodies replay byte-identical.
    * Large caps (the 1 GB recap upload, the 100 MB worker result):
      buffering would defeat the endpoints' streaming design, so the
      body streams through a counting receive; exceeding the cap
      converts to a disconnect, and the endpoint's own during-write
      cap + partial-file cleanup remain the enforcement of record.
    """

    _BUFFER_LIMIT = 64 * 1024 * 1024

    def __init__(self, app, max_content_length: dict[str, int] | None = None):
        self.app = app
        self.caps = max_content_length or MAX_CONTENT_LENGTH

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        cap = None
        for prefix, max_bytes in self.caps.items():
            if path.startswith(prefix):
                cap = max_bytes
                break
        if cap is None:
            await self.app(scope, receive, send)
            return

        if cap > self._BUFFER_LIMIT:
            total = 0

            async def counting_receive():
                nonlocal total
                message = await receive()
                if message["type"] == "http.request":
                    total += len(message.get("body", b""))
                    if total > cap:
                        return {"type": "http.disconnect"}
                return message

            await self.app(scope, counting_receive, send)
            return

        body_parts: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > cap:
                response = JSONResponse(
                    status_code=413, content={"detail": TOO_LARGE_DETAIL}
                )
                await response(scope, receive, send)
                return
            body_parts.append(chunk)
            if not message.get("more_body"):
                break

        replay = list(body_parts)

        async def replay_receive():
            if replay:
                chunk = replay.pop(0)
                return {
                    "type": "http.request",
                    "body": chunk,
                    "more_body": bool(replay),
                }
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)
