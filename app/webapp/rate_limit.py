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

import gzip
import hmac
import time
import urllib.parse
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

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
    "/api/user/logo": (10, 60),
    "/api/user/preferences": (30, 60),
    "/api/jobs/{job_id}/cancel": (30, 60),
    "/api/tts/generate": (15, 60),
    "/api/tts/gemini-key": (10, 60),
    "/api/recap/preview-composite": (30, 60),
    "/api/recap/{job_id}/thumbnail": (6, 60),
    "/api/recap/{job_id}/script": (20, 60),
    "/api/recap/upload_voice_ref": (10, 60),
    "/api/recap/upload_sub_font": (10, 60),
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
    "/api/recap/upload_voice_ref": 25 * 1024 * 1024,
    "/api/recap/upload_sub_font": 15 * 1024 * 1024,
    "/api/user/logo": 10 * 1024 * 1024,
    "/api/internal/worker/upload-result": 500 * 1024 * 1024,
    "/api/internal/worker/upload-recap-file": 500 * 1024 * 1024,
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

    def _match_limit(self, path: str) -> tuple[str, tuple[int, int]] | None:
        # Exact match first so generate-with-audio is not confused
        # with generate; then single-segment templates; then the
        # longest matching prefix (template keys never take part in
        # prefix matching). Returns (canonical_rule_key, (max_requests, window)).
        if path in self.limits:
            return path, self.limits[path]
        for template, limit in self.limits.items():
            if "{" in template and _template_matches(template, path):
                return template, limit
        best_rule: str | None = None
        best: tuple[int, int] | None = None
        best_len = -1
        for prefix, limit in self.limits.items():
            if "{" in prefix:
                continue
            if path.startswith(prefix) and len(prefix) > best_len:
                best_rule = prefix
                best = limit
                best_len = len(prefix)
        if best is not None and best_rule is not None:
            return best_rule, best
        return None

    def _is_limited(self, path: str) -> tuple[int, int] | None:
        matched = self._match_limit(path)
        return matched[1] if matched is not None else None

    def _hits_for(self, key: str, limit: tuple[int, int], now: float) -> bool:
        """Record one hit; return True when the caller is OVER the limit."""
        max_requests, window = limit
        if len(self._hits) > 2048:
            # Periodic bounded eviction of stale/empty sliding-window buckets.
            stale_keys = [
                k for k, dq in list(self._hits.items())
                if not dq or dq[-1] <= now - max(window, 60)
            ]
            for sk in stale_keys:
                self._hits.pop(sk, None)
            # Hard upper cap: evict oldest keys before inserting a new key so dict never exceeds 4096
            while len(self._hits) >= (4096 if key not in self._hits else 4097):
                oldest_key = next(iter(self._hits), None)
                if oldest_key is None:
                    break
                self._hits.pop(oldest_key, None)
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
        # Rate limit: POST/PUT/DELETE on listed routes (bucketing by canonical
        # route template so parameterized paths like /api/recap/{job_id}/retry
        # cannot bypass the bucket by rotating IDs).
        if request.method in ("POST", "PUT", "DELETE"):
            matched = self._match_limit(request.url.path)
            if matched is not None:
                rule_key, limit = matched
                rk = _rate_key(request)
                key = f"{rk}:{rule_key}"
                now_m = time.monotonic()
                ip_agg_over = (
                    rk.startswith("ipuser:")
                    and self._hits_for(
                        f"ipagg:{_client_ip(request)}:{rule_key}",
                        (max(limit[0] * 5, 25), limit[1]),
                        now_m,
                    )
                )
                if ip_agg_over or self._hits_for(key, limit, now_m):
                    return JSONResponse(
                        status_code=429,
                        content={"detail": TOO_MANY_DETAIL},
                        headers={"Retry-After": str(limit[1])},
                    )
        elif request.method == "GET" and request.url.path in GET_RATE_LIMITS:
            # Batch AJ (L10): the one GET with a real per-hit cost
            # (ffmpeg frame extraction) gets a bucket too.
            limit = GET_RATE_LIMITS[request.url.path]
            rk = _rate_key(request)
            key = f"{rk}:{request.url.path}"
            now_m = time.monotonic()
            ip_agg_over = (
                rk.startswith("ipuser:")
                and self._hits_for(
                    f"ipagg:{_client_ip(request)}:{request.url.path}",
                    (max(limit[0] * 5, 25), limit[1]),
                    now_m,
                )
            )
            if ip_agg_over or self._hits_for(key, limit, now_m):
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


# ── Bandwidth & Anti-Flood Shield (DDoS / Static Flood + Origin Lock + Smart GZip) ─
_SHIELD_COUNTERS: dict[str, int] = {
    "blocked_static_flood_429": 0,
    "blocked_origin_403": 0,
}

_COMPRESSIBLE_PREFIXES = (
    b"text/",
    b"application/json",
    b"application/javascript",
    b"application/x-javascript",
    b"application/xml",
    b"image/svg+xml",
)


def get_bandwidth_shield_stats() -> dict[str, object]:
    """Snapshot of Bandwidth Shield config & live block counters for Admin Overview."""
    return {
        "gzip_enabled": True,
        "origin_lock_enabled": bool(str(getattr(settings, "cf_origin_secret", "") or "").strip()),
        "static_flood_max_requests": int(getattr(settings, "static_flood_max_requests", 180) or 180),
        "static_flood_window_seconds": int(getattr(settings, "static_flood_window_seconds", 60) or 60),
        "blocked_static_flood_429": _SHIELD_COUNTERS["blocked_static_flood_429"],
        "blocked_origin_403": _SHIELD_COUNTERS["blocked_origin_403"],
    }


def _scope_header(scope: dict, name_lower: bytes) -> str:
    for k, v in scope.get("headers") or ():
        if k.lower() == name_lower:
            return v.decode("latin-1", errors="ignore")
    return ""


def _scope_client_ip(scope: dict) -> str:
    forwarded = _scope_header(scope, b"x-forwarded-for")
    if forwarded:
        ip = forwarded.split(",")[-1].strip()
        if ip:
            return ip
    client = scope.get("client")
    if client and client[0]:
        return str(client[0])
    return "unknown"


class StaticFloodShieldMiddleware:
    """Outermost pure-ASGI shield protecting Render egress against L7 floods.

    1. Optional Cloudflare Origin Lock: when ``CF_ORIGIN_SECRET`` is configured,
       every HTTP request except ``/health`` and the Telegram webhook path must
       present ``X-Origin-Secret`` matching the secret; otherwise returns a tiny
       10-byte 403 immediately (blocks direct attacks bypassing Cloudflare).
    2. Per-IP Static & Root Flood Cap: bounds GET/HEAD bursts on ``/`` and
       ``/static/*`` (default 180 req / 60 s per IP) with a tiny 17-byte 429 +
       ``Retry-After: 30``. Bounded to 4,096 IP buckets so distributed floods
       cannot OOM the container.
    """

    def __init__(
        self,
        app,
        *,
        max_requests: int | None = None,
        window_seconds: int | None = None,
    ):
        self.app = app
        self._override_max = max_requests
        self._override_window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _effective_limits(self) -> tuple[int, int]:
        max_req = (
            self._override_max
            if self._override_max is not None
            else int(getattr(settings, "static_flood_max_requests", 180) or 180)
        )
        win = (
            self._override_window
            if self._override_window is not None
            else int(getattr(settings, "static_flood_window_seconds", 60) or 60)
        )
        return max(1, max_req), max(1, win)

    def _is_flooding(self, ip: str, now: float) -> tuple[bool, int]:
        max_req, window = self._effective_limits()
        if len(self._hits) > 2048:
            stale = [
                k for k, dq in list(self._hits.items())
                if not dq or dq[-1] <= now - max(window, 60)
            ]
            for sk in stale:
                self._hits.pop(sk, None)
            while len(self._hits) >= (4096 if ip not in self._hits else 4097):
                oldest = next(iter(self._hits), None)
                if oldest is None:
                    break
                self._hits.pop(oldest, None)
        dq = self._hits[ip]
        while dq and dq[0] <= now - window:
            dq.popleft()
        if len(dq) >= max_req:
            return True, window
        dq.append(now)
        return False, window

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or ""
        method = (scope.get("method") or "GET").upper()

        # Layer 1: Optional Cloudflare Origin Secret Lock
        secret = str(getattr(settings, "cf_origin_secret", "") or "").strip()
        if secret:
            webhook_path = str(getattr(settings, "bot_webhook_path", "/telegram/webhook") or "/telegram/webhook")
            if path != "/health" and path != webhook_path:
                presented = _scope_header(scope, b"x-origin-secret").strip()
                if not presented or not hmac.compare_digest(presented, secret):
                    _SHIELD_COUNTERS["blocked_origin_403"] += 1
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 403,
                            "headers": [
                                (b"content-type", b"text/plain; charset=utf-8"),
                                (b"cache-control", b"no-store"),
                                (b"content-length", b"9"),
                            ],
                        }
                    )
                    await send({"type": "http.response.body", "body": b"Forbidden", "more_body": False})
                    return

        # Layer 2: Per-IP flood shield on "/" and "/static/*"
        if method in ("GET", "HEAD") and (path == "/" or path.startswith("/static/")):
            ip = _scope_client_ip(scope)
            flooded, window = self._is_flooding(ip, time.monotonic())
            if flooded:
                _SHIELD_COUNTERS["blocked_static_flood_429"] += 1
                retry_after = str(min(max(window, 10), 60)).encode("ascii")
                body = b"" if method == "HEAD" else b"Too Many Requests"
                await send(
                    {
                        "type": "http.response.start",
                        "status": 429,
                        "headers": [
                            (b"content-type", b"text/plain; charset=utf-8"),
                            (b"retry-after", retry_after),
                            (b"cache-control", b"no-store"),
                            (b"content-length", str(len(body)).encode("ascii")),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body, "more_body": False})
                return

        await self.app(scope, receive, send)


class SmartGZipMiddleware:
    """Pure-ASGI GZip middleware restricted to text/JS/CSS/JSON/SVG payloads.

    Compresses SPA HTML/JS/CSS and JSON API responses (~75-85% bandwidth drop)
    while leaving already-compressed binary media (video/*, audio/*, image/jpeg,
    image/png, woff2) and HTTP 206 Range streams completely untouched so video
    seeking never breaks and CPU is never wasted re-compressing MP4/JPG bytes.
    """

    def __init__(self, app, *, minimum_size: int = 500, compresslevel: int = 6):
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or (scope.get("method") or "").upper() == "HEAD":
            await self.app(scope, receive, send)
            return

        accept_encoding = _scope_header(scope, b"accept-encoding").lower()
        if "gzip" not in accept_encoding:
            await self.app(scope, receive, send)
            return

        start_message: dict | None = None
        passthrough = False
        chunks: list[bytes] = []

        async def send_wrapper(message):
            nonlocal start_message, passthrough
            msg_type = message.get("type")
            if msg_type == "http.response.start":
                status = int(message.get("status", 200))
                headers = list(message.get("headers") or [])
                hdr_map = {k.lower(): v for k, v in headers}
                ctype = hdr_map.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
                already_encoded = b"content-encoding" in hdr_map
                is_range = status == 206 or b"content-range" in hdr_map
                compressible = (
                    not already_encoded
                    and not is_range
                    and status >= 200
                    and status not in (204, 304)
                    and any(ctype.startswith(p) for p in _COMPRESSIBLE_PREFIXES)
                )
                if not compressible:
                    passthrough = True
                    await send(message)
                    return
                start_message = {"type": "http.response.start", "status": status, "headers": headers}
                return

            if msg_type == "http.response.body":
                if passthrough or start_message is None:
                    await send(message)
                    return
                body = message.get("body", b"")
                if body:
                    chunks.append(body)
                if message.get("more_body", False):
                    return

                raw = b"".join(chunks)
                chunks.clear()
                headers = list(start_message["headers"])
                if len(raw) >= self.minimum_size:
                    compressed = gzip.compress(raw, compresslevel=self.compresslevel)
                    if len(compressed) < len(raw):
                        new_headers = [
                            (k, v) for k, v in headers
                            if k.lower() not in (b"content-length", b"content-encoding")
                        ]
                        # Preserve/append Vary: Accept-Encoding cleanly
                        vary_vals = [v for k, v in new_headers if k.lower() == b"vary"]
                        if not any(b"accept-encoding" in v.lower() for v in vary_vals):
                            new_headers.append((b"vary", b"Accept-Encoding"))
                        new_headers.append((b"content-encoding", b"gzip"))
                        new_headers.append((b"content-length", str(len(compressed)).encode("ascii")))
                        await send(
                            {
                                "type": "http.response.start",
                                "status": start_message["status"],
                                "headers": new_headers,
                            }
                        )
                        await send({"type": "http.response.body", "body": compressed, "more_body": False})
                        return

                await send(start_message)
                await send({"type": "http.response.body", "body": raw, "more_body": False})
                return

            await send(message)

        await self.app(scope, receive, send_wrapper)
