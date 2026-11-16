"""
api.py - Complete REST API Router for Unified Telegram Mini App (Studio Pro).
Supports:
  - User profile & minute balance
  - Curated VoxCPM2 voices
  - Voice generation with reference audio upload & curated personas
  - Audio streaming & downloading
  - Media downloader extraction & conversion (YouTube/TikTok)
  - 1-Click AI Movie Recap Studio configuration
  - VIP packages & Payment slip submission
  - Role-gated Admin Console (Slips approval, user manager, broadcast, worker status)
"""

from __future__ import annotations

import os
import re
import time
import json
import uuid
import shutil
import logging
import asyncio
import functools
import hashlib
import hmac
import html
import math
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, UploadFile, File, Form, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import case, delete, select, func, update, or_, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.config.settings import get_watermark_config
from app.db.session import AsyncSessionLocal, get_db_session
from app.models.enums import AccountCategory, JobStatus, UserStatus, VoiceModelStatus, VoiceModelType, PaymentStatus, JobType
from app.models.schema import User, Job, VoiceModel, MinuteBalance, MinuteLedgerEvent, RecapCreditLedgerEvent, PaymentRequest, AuditLog, UserPreference, utc_now
from app.services.i18n import t
from app.services.models import get_authorized_model, get_available_models_for_user, is_displayable_model
from app.services.myanmar_text import convert_zawgyi_to_unicode
from app.services.download_names import branded_download_filename
from app.services.curated_voices import list_curated_voices, get_curated_voice
from app.services import recap_eta
from app.services import voice_choice as _voice_choice
from app.services.speed import (
    DEFAULT_SPEED,
    SPEED_OPTIONS,
    migrate_speed_pref,
    normalize_api_speed,
)
from app.webapp.auth import (
    ADMIN_INIT_DATA_MAX_AGE_SECONDS,
    init_data_auth_date,
    parse_and_verify_init_data,
)

logger = logging.getLogger(__name__)
api_router = APIRouter(prefix="/api")


# ── Maintenance refusal (shared error shape) ────────────────────────────────
# The ONE detail string every maintenance refusal carries. The write-side
# guard in require_gate_verified() raises it, and since Batch AR the
# read side raises it too (see _enforce_maintenance_access) — the Mini
# App client keys its full-screen maintenance gate off this prefix.
MAINTENANCE_MODE_DETAIL = (
    "MAINTENANCE_MODE: ဆာဗာ ပြုပြင်ထိန်းသိမ်းမှု ဆောင်ရွက်နေပါသည် — "
    "မကြာမီ ပြန်လည် အသုံးပြုနိုင်ပါမည်။"
)


def _enforce_maintenance_access(user: User) -> None:
    """Batch AR (owner order 2026-11-05): while maintenance mode is ON,
    non-admin users may not use the Mini App AT ALL — reads included,
    not just writes. Before AR, maintenance only refused writes (hunt
    C1, in require_gate_verified below) and users could still open,
    browse and read everything; the owner ordered entry itself closed.

    This helper is the read-side half of that one mechanism — same
    flag, same exemption, same error shape:
    - the flag is settings.maintenance_mode (the dynamic setting the
      admin toggle writes; no new setting was added);
    - admins are exempt and keep full access;
    - the refusal is the shared MAINTENANCE_MODE 403.

    It is enforced inside get_current_user() so every authenticated
    Mini App endpoint inherits it, and re-checked explicitly on the
    signed-token download paths that resolve a user without going
    through get_current_user(). The surfaces that must stay reachable
    never pass through here: GET /api/config (no user dependency),
    static assets, /health, the Telegram webhook and the WORKER_TOKEN
    internal upload — none of them resolve a Telegram user.
    """
    if getattr(settings, "maintenance_mode", False) and not settings.is_admin(user.id):
        raise HTTPException(status_code=403, detail=MAINTENANCE_MODE_DETAIL)


# ── Dependency: Authenticate Telegram User ──────────────────────────────────
async def get_current_user(
    x_telegram_init_data: Optional[str] = Header(None, alias="X-Telegram-Init-Data"),
    db: AsyncSession = Depends(get_db_session),
) -> User:
    """Extract and authenticate Telegram user from initData."""
    telegram_user_data = None
    if x_telegram_init_data:
        telegram_user_data = parse_and_verify_init_data(x_telegram_init_data)

    if not telegram_user_data:
        # Security fix (audit 2026-10-28): no Guest fallback. Previously a
        # missing or invalid initData auto-created a Guest user, which let
        # any browser call authenticated endpoints. Authenticated endpoints
        # now return 401 instead. The only unauthenticated exception is the
        # read-only curated-persona list, which uses get_optional_user()
        # below and creates no user.
        raise HTTPException(
            status_code=401, detail="Telegram authentication required"
        )

    user_id = int(telegram_user_data["id"])
    user = await db.get(User, user_id)
    if not user:
        from app.services.ledger import get_or_create_user

        user, _ = await get_or_create_user(
            db,
            telegram_id=user_id,
            username=telegram_user_data.get("username"),
            first_name=telegram_user_data.get("first_name", "User"),
            language_code=telegram_user_data.get("language_code", "my"),
        )
        await db.commit()
        await db.refresh(user)
    else:
        # Profile sync (audit fix 2026-11-03): this used to assign and
        # COMMIT on every authenticated request — a DB write (and a
        # pooled-connection checkout held to commit) behind every Mini
        # App poll, even though the Telegram profile almost never
        # changes between requests. Only write when a value actually
        # changed.
        new_first_name = telegram_user_data.get("first_name", user.first_name)
        new_username = telegram_user_data.get("username", user.username)
        if new_first_name != user.first_name or new_username != user.username:
            user.first_name = new_first_name
            user.username = new_username
            await db.commit()

    # Batch AR: maintenance refuses non-admins here, at the shared
    # identity dependency, so EVERY authenticated Mini App endpoint —
    # reads included — refuses with the MAINTENANCE_MODE shape while
    # maintenance is on. Admins pass through untouched.
    _enforce_maintenance_access(user)
    return user


# ── Signed stream tokens (R40 verify-first (ii)) ─────────────────────
# The recap player used to fetch the ENTIRE video through
# fetch()+blob just to attach the initData header — a 100MB+ render
# held fully in WebView memory, which is what surfaced as the native
# "Unable to play media" in the Telegram client. The file itself is
# WebView-safe (libx264/yuv420p + AAC + faststart) and Starlette's
# FileResponse serves HTTP Range, so the player only needs a URL it
# can load directly. A <video> element cannot send the initData
# header, so status payloads carry a short-lived HMAC token scoped to
# exactly one (job, file, user); the stream/download endpoint accepts
# it in place of the header and STILL runs the normal ownership
# checks against the token's user.
_STREAM_TOKEN_TTL_SECONDS = 2 * 3600


def _stream_signing_key() -> bytes:
    """Dedicated signing key for stream tokens: HMAC-SHA256 with the
    bot token as key over a fixed domain-separation label, so the raw
    bot token itself is never the HMAC key of tokens handed out in
    URLs. Returns b"" when no bot token is configured (signing off).
    Deriving the key invalidated all previously minted tokens when
    this shipped; their TTL is at most _STREAM_TOKEN_TTL_SECONDS, so
    no migration was needed."""
    bot_token = (settings.bot_token or "").encode("utf-8")
    if not bot_token:
        return b""
    return hmac.new(bot_token, b"stream-token-v1", hashlib.sha256).digest()


def _make_stream_token(job_id: str, user_id: int, file_type: str) -> Optional[str]:
    secret = _stream_signing_key()
    if not secret:
        return None
    exp = int(time.time()) + _STREAM_TOKEN_TTL_SECONDS
    msg = f"stream:{job_id}:{int(user_id)}:{file_type}:{exp}".encode("utf-8")
    sig = hmac.new(secret, msg, hashlib.sha256).hexdigest()
    return f"{int(user_id)}.{exp}.{sig}"


def _verify_stream_token(token: str, job_id: str, file_type: str) -> Optional[int]:
    secret = _stream_signing_key()
    if not secret or not token:
        return None
    try:
        uid_s, exp_s, sig = token.split(".", 2)
        uid, exp = int(uid_s), int(exp_s)
    except (ValueError, AttributeError):
        return None
    if exp < int(time.time()):
        return None
    msg = f"stream:{job_id}:{uid}:{file_type}:{exp}".encode("utf-8")
    expected = hmac.new(secret, msg, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    return uid


def _append_stream_token(url: Optional[str], token: Optional[str]) -> Optional[str]:
    """Append a scoped stream token to a media URL as ?t= (or &t= when
    the URL already carries a query string). Empty URLs, a missing
    token (no bot token configured) and URLs that already carry a t=
    parameter pass through unchanged, so re-running the signing wrapper
    over an already-signed dict never stacks tokens."""
    if not url or not token:
        return url
    if "?" in url:
        query = url.split("?", 1)[1]
        if any(p.startswith("t=") for p in query.split("&")):
            return url
        return f"{url}&t={token}"
    return f"{url}?t={token}"


def _with_stream_url(urls: Dict[str, Any], job_id: str, user_id: int) -> Dict[str, Any]:
    """Copy a download_urls dict and sign its media URLs.

    Adds the signed direct "stream_url" for the <video> player AND
    appends each entry's own scoped token (?t=) to the video, audio,
    srt and script download URLs and to every split-part URL. The
    result-screen download buttons open those URLs in the device
    browser via Telegram openLink, where no initData header exists —
    the scoped token is what authenticates those opens (the endpoint
    still runs its ownership checks against the token's user). In-app
    blob downloads are unaffected: when both are sent, the header user
    wins. Stored registry URLs are never mutated — parts entries are
    copied before signing."""
    out = dict(urls or {})
    for key in ("video", "audio", "srt", "script"):
        if out.get(key):
            out[key] = _append_stream_token(
                out[key], _make_stream_token(job_id, user_id, key)
            )
    parts = out.get("parts")
    if isinstance(parts, list) and parts:
        signed_parts = []
        for part in parts:
            if isinstance(part, dict) and part.get("url") and part.get("n") is not None:
                part = dict(part)
                part["url"] = _append_stream_token(
                    part["url"],
                    _make_stream_token(job_id, user_id, f"part{part['n']}"),
                )
            signed_parts.append(part)
        out["parts"] = signed_parts
    if out.get("video"):
        token = _make_stream_token(job_id, user_id, "video")
        if token:
            out["stream_url"] = f"/api/recap/stream/{job_id}/video?t={token}"
    return out


def _recap_deliverables_download_form(urls: Dict[str, Any]) -> Dict[str, Any]:
    """Batch CK: point a signed recap deliverables dict's video/audio
    entries at the DOWNLOAD route (attachment) instead of the stream
    route (inline), for the ADMIN serializers only.

    The modal's Download buttons must SAVE the file; the stream route
    plays it. The swap is a pure route-prefix change on the already-
    signed URL: the stream token is bound to (job, file type), not to
    the route, and the endpoint verifies it against the path's file
    type, so the swapped URL's token still verifies. srt/script
    already use the download route and pass through unchanged, as
    does the separate download_url (callers keep the stream form for
    the player / Browser Tab)."""
    out = dict(urls or {})
    for key in ("video", "audio"):
        url = out.get(key)
        if isinstance(url, str) and url.startswith("/api/recap/stream/"):
            out[key] = "/api/recap/download/" + url[len("/api/recap/stream/"):]
    return out


async def get_optional_user(
    x_telegram_init_data: Optional[str] = Header(None, alias="X-Telegram-Init-Data"),
    db: AsyncSession = Depends(get_db_session),
) -> User | None:
    """Optional identity for read-only preview endpoints only.

    Returns the authenticated user, or None when initData is missing or
    invalid. Unlike get_current_user(), it never creates a user and never
    grants access: callers must treat None as "preview only" (language
    defaults, public lists) and must not use it for writes, generation,
    history, downloads, or admin actions.

    Batch AR exception: a MAINTENANCE_MODE refusal is re-raised, not
    anonymised — during maintenance an identified non-admin must be
    refused outright, never silently downgraded to the anonymous
    preview (which would keep serving them the persona list). Genuinely
    anonymous callers (no/invalid initData) still get the preview.
    """
    try:
        return await get_current_user(x_telegram_init_data, db)
    except HTTPException as exc:
        if str(getattr(exc, "detail", "") or "").startswith("MAINTENANCE_MODE"):
            raise
        return None


def require_admin(
    x_telegram_init_data: Optional[str] = Header(None, alias="X-Telegram-Init-Data"),
    user: User = Depends(get_current_user),
) -> User:
    """Cryptographically enforce that the requester is a verified Telegram Admin."""
    if not x_telegram_init_data:
        raise HTTPException(status_code=403, detail="Telegram cryptographic signature required for Admin operations")
    verified_data = parse_and_verify_init_data(x_telegram_init_data)
    if not verified_data or int(verified_data.get("id", 0)) != user.id:
        raise HTTPException(status_code=403, detail="Invalid Telegram authentication signature")
    # Batch AJ (M8): admin actions require FRESH initData (≤ 12 h),
    # not the 24 h window user reads tolerate — a day-old leaked
    # admin session must not drive admin actions. Reopening the Mini
    # App mints fresh initData, so this only bites stale sessions.
    _auth_date = init_data_auth_date(x_telegram_init_data)
    if _auth_date is None or time.time() - _auth_date > ADMIN_INIT_DATA_MAX_AGE_SECONDS:
        raise HTTPException(
            status_code=403,
            detail="Admin session expired — reopen the Mini App and try again",
        )
    if not settings.is_admin(user.id):
        raise HTTPException(status_code=403, detail="Admin authorization required")
    return user


async def require_gate_verified(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> User:
    """Gate Mini App WRITE endpoints on live channel/group membership.

    Root-cause fix (2026-10-29): before this branch, NO Mini App endpoint
    checked membership at all. get_current_user() only authenticated the
    Telegram identity (and on live main even auto-created a Guest user),
    so anyone who opened the Mini App -- or called the API directly --
    could generate, extract, recap and submit slips without ever joining
    the channel/group, even though bot /start enforces the gate. Bot
    /start alone cannot protect an HTTP API.

    Design (fail-closed, no cached-flag shortcut):
    - Admins bypass, mirroring AccessControlMiddleware and gate.py.
    - Otherwise a LIVE Telegram get_chat_member check runs on every gated
      write. Cached channel_verified/group_verified flags are deliberately
      NOT trusted here: on live main the old fail-open check set those
      flags to True for everyone when gate IDs were missing, so trusting
      them would keep the bypass alive for legacy users. The live check
      also catches users who joined, verified, then left.
    - Gate not configured (either ID empty) or bot token missing -> 403
      GATE_NOT_CONFIGURED, not a "you have not joined" message, so the
      operator can tell misconfiguration apart from a real non-member.
    - Non-member -> 403 MEMBERSHIP_REQUIRED. The Mini App should then
      show the channel/group invite links (returned by /api/user/me)
      and a re-verify button calling POST /api/user/verify-membership.
    - Reads are NOT gated here: /api/user/me, job status, history and
      downloads of the user's own jobs stay available so a blocked user
      can still see why they are blocked and what they already own.
      The internal worker upload endpoint is also untouched (it uses
      WORKER_TOKEN, not a Telegram user).
    - On any verification outcome the flags are committed, so a later
      bot /start sees the same state. Bonus/referral side effects live
      in verify_user_membership() and run at most once by its own
      idempotency (welcome bonus only when configured > 0, never for
      admins).
    """
    from app.services.gate import is_gate_configured, verify_user_membership

    if settings.is_admin(user.id):
        return user
    # Maintenance enforcement (hunt C1): the flag existed and the bot
    # honoured it at /start and the clone entry, but Mini App writes
    # never checked it — during maintenance the webapp kept accepting
    # generations, slips and uploads. Admins are exempt (above).
    # Batch AR: reads no longer stay open either — get_current_user()
    # raises the same refusal for every authenticated endpoint (see
    # _enforce_maintenance_access); this check remains as the
    # write-side belt-and-braces for any caller that resolved a user
    # without that dependency.
    if getattr(settings, "maintenance_mode", False):
        raise HTTPException(
            status_code=403,
            detail=MAINTENANCE_MODE_DETAIL,
        )
    # Production ban enforcement (2026-10-31): the bot has always blocked
    # status=BLOCKED users via AccessControlMiddleware, but the Mini App
    # API never checked the flag, so a banned user could keep generating
    # here. Mirror the bot: a BLOCKED user is suspended everywhere.
    if (user.status or "") == UserStatus.BLOCKED.value:
        raise HTTPException(
            status_code=403,
            detail="USER_BANNED: This account has been suspended by the admin. Please contact support.",
        )
    if not is_gate_configured():
        raise HTTPException(
            status_code=403,
            detail="GATE_NOT_CONFIGURED: Channel/Group gate IDs are not configured on the server. Please contact the admin.",
        )
    if not settings.bot_token:
        raise HTTPException(
            status_code=403,
            detail="GATE_NOT_CONFIGURED: Telegram bot token is not configured on the server. Please contact the admin.",
        )
    from aiogram import Bot

    bot = Bot(token=settings.bot_token)
    try:
        is_verified, _reason = await verify_user_membership(db, user, bot)
    finally:
        try:
            await bot.session.close()
        except Exception:
            pass
    # Persist the verified flags (and any idempotent bonus/referral
    # outcome) even on failure, so partial state is not lost.
    try:
        await db.commit()
    except Exception:
        pass
    if not is_verified:
        raise HTTPException(
            status_code=403,
            detail="MEMBERSHIP_REQUIRED: Please join the official Channel and Group first, then verify again.",
        )
    return user


async def _require_job_owner(
    db: AsyncSession, job_id: str, user: User
) -> Job:
    """Return a job only when the requester owns it or is an admin.

    Returns 404 (not 403) for missing jobs and for jobs owned by someone
    else, matching get_job_status() and avoiding ownership probing.
    """
    job = await db.get(Job, str(job_id))
    if not job or (job.user_id != user.id and not settings.is_admin(user.id)):
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _recap_registry_owner_ok(job_id: str, user: User) -> bool:
    """True when the in-memory recap registry entry belongs to the user.

    Returns True when there is no registry entry, so callers fall back to
    the DB job check. Callers must still 404 when neither source has a job.
    """
    entry = RECAP_JOBS_REGISTRY.get(job_id)
    if entry is None:
        return True
    return entry.get("user_id") == user.id or settings.is_admin(user.id)


# ── Security audit round 2: upload / recap helpers ─────────────────────────
MAX_RECAP_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1 GB, same as polish reference cap
MAX_SLIP_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB, same as polish reference cap
MAX_LOGO_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MB cap for a user's own channel logo
_UPLOAD_CHUNK_SIZE = 1024 * 1024  # 1 MB chunks: never buffer a whole upload


async def _save_upload_streaming(upload_file, dest_path, max_bytes: int) -> int:
    """Stream *upload_file* to *dest_path* in 1 MB chunks.

    Aborts with 413 and deletes the partial file as soon as the limit is
    exceeded, so an oversized upload cannot exhaust server memory (OOM/DoS).
    """
    total = 0
    try:
        with open(dest_path, "wb") as out:
            while True:
                chunk = await upload_file.read(_UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(status_code=413, detail="ဖိုင် အရွယ်အစား ကန့်သတ်ချက် ကျော်လွန်နေပါသည်")
                out.write(chunk)
    except HTTPException:
        try:
            dest_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    except BaseException as exc:
        # Batch AJ L5: asyncio.CancelledError is a BaseException — the
        # old `except Exception` cleanup let a client disconnect (or a
        # task cancel) mid-upload leave the partial file behind.
        try:
            dest_path.unlink(missing_ok=True)
        except Exception:
            pass
        if not isinstance(exc, Exception):
            raise
        # Never leak the raw exception (paths/internals) to the client:
        # log it server-side, return a clean user-safe detail.
        logger.warning("Failed to save uploaded file %s: %s", dest_path, exc)
        raise HTTPException(status_code=500, detail="ဖိုင် သိမ်းဆည်းရာတွင် အဆင်မပြေ ဖြစ်သွားပါသည်")
    return total


# ── Batch AJ M1: disk headroom gate ──────────────────────────────────
# The 2026-11-04 production incident was a FULL 10 GB disk: every job
# failed after charging until the retention sweep landed. The admin
# overview displayed free space, but nothing REFUSED new work. Upload
# and job-submission entry points now check headroom BEFORE accepting
# bytes or charging, so a filling disk degrades to a clear refusal
# instead of charged-then-failed jobs.
DISK_HEADROOM_MIN_FREE_BYTES = 512 * 1024 * 1024


def _require_disk_headroom() -> None:
    import shutil

    try:
        free = shutil.disk_usage(settings.storage_root).free
    except Exception:
        return  # cannot measure → do not block legitimate work
    if free < DISK_HEADROOM_MIN_FREE_BYTES:
        raise HTTPException(
            status_code=507,
            detail="ဆာဗာ သိုလှောင်နေရာ ပြည့်လုနီးပါး ဖြစ်နေပါတယ် — ခဏနေ ပြန်စမ်းကြည့်ပါ။",
        )


# ── Batch AJ L18: bounded in-memory registries ───────────────────────
def _remember_bounded(mapping: dict, key, value, *, cap: int = 2048) -> None:
    """Insert into an in-memory registry dict, evicting oldest entries
    (insertion order) once *cap* is exceeded. The upload title/owner
    registries previously grew for the life of the process."""
    mapping[key] = value
    while len(mapping) > cap:
        mapping.pop(next(iter(mapping)), None)


# ── Custom channel logo (owner decision 2026-11-02) ─────────────────
# The Recap logo overlay is a PAID feature and stamps the user's OWN
# uploaded channel logo — there is no bundled brand logo. Free users
# get no logo at all: uploads are rejected and the generate path
# ignores logo settings for them, even if a stale client still sends
# include_logo=true. Admins are allowed (standing exemption).
LOGO_IMAGE_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}
LOGO_FILE_EXTS = (".png", ".jpg", ".webp")
LOGO_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}
LOGO_VIP_ONLY_DETAIL = (
    "ကိုယ်ပိုင် Logo ထည့်ခြင်းသည် VIP အသုံးပြုသူများအတွက်သာ ဖြစ်ပါသည်။ "
    "VIP tab တွင် Plan ဝယ်ယူနိုင်ပါသည်။"
)


def _can_use_custom_logo(user: User) -> bool:
    """Paid tier (PURCHASED) or admin — the same test the recap
    duration/watermark benefits use."""
    return (
        user.account_category == AccountCategory.PURCHASED.value
        or settings.is_admin(user.id)
    )


def _clamp_recap_render_quality(
    req: "RecapConfigRequest",
    *,
    user: Optional[User] = None,
    user_id: Optional[int] = None,
    is_vip: Optional[bool] = None,
) -> str:
    """Batch AU: render quality is a paid-tier benefit.

    FREE users always render with ``standard``; PURCHASED users and
    admins keep the quality they selected. This is a clamp, never a
    rejection: the requested job still runs, just with the free tier's
    encoder preset. ``is_vip`` is the execution paths' existing
    purchased-or-admin flag; submit paths pass the user and reuse the
    same tier test as the custom-logo gate above.
    """
    if req is None:
        return "standard"
    if is_vip is None:
        is_vip = _can_use_custom_logo(user) if user is not None else False
    requested = req.render_quality
    if is_vip or requested == "standard":
        return requested
    req.render_quality = "standard"
    logger.info(
        "Recap render quality clamped for user %s: requested=%s applied=standard",
        user_id if user_id is not None else (user.id if user is not None else None),
        requested,
    )
    return req.render_quality


_RECAP_BILLING_ADMIN_FREE = "admin_free"
_RECAP_BILLING_CREDIT_ONLY = "credit_only"
_RECAP_BILLING_CREDIT_PLUS_VOICE = "credit_plus_voice_minutes"
_RECAP_ALLOWED_TTS_ENGINES = ("voxcpm", "gemini", "edge")


def _clamp_recap_tts_engine(
    req: "RecapConfigRequest",
    *,
    user: Optional[User] = None,
    user_id: Optional[int] = None,
    is_vip: Optional[bool] = None,
) -> str:
    """Phase 5: FREE and VIP users may both use Persona voices (``voxcpm``)
    provided they hold sufficient Recap Credits AND Voice Clone Minutes
    under Option B combined billing. My Own Clone Voice (``voice_ref_id``)
    remains strictly paid-only via ``_clamp_recap_voice_ref``.

    Unknown/empty engine values normalize safely to ``gemini``.
    """
    if req is None:
        return "gemini"
    requested = str(getattr(req, "tts_engine", "") or "").strip().lower()
    if requested not in _RECAP_ALLOWED_TTS_ENGINES:
        req.tts_engine = "gemini"
        return req.tts_engine
    req.tts_engine = requested
    return requested


def _recap_billing_mode(tts_engine: str, *, is_admin: bool = False) -> str:
    """Return the immutable billing mode recorded on a recap job at submit."""
    if is_admin:
        return _RECAP_BILLING_ADMIN_FREE
    if str(tts_engine or "").strip().lower() == "voxcpm":
        return _RECAP_BILLING_CREDIT_PLUS_VOICE
    return _RECAP_BILLING_CREDIT_ONLY


def _recap_effective_duration_seconds(
    checked_duration: Optional[float],
    max_duration_allowed: Optional[float],
    *,
    segmented: bool = False,
) -> float:
    """Effective source seconds used to estimate Option B clone voice hold."""
    try:
        dur = float(checked_duration or 0.0)
    except (TypeError, ValueError):
        dur = 0.0
    if dur <= 0:
        # Unknown remote source duration: enforce a minimum 60s pre-check/hold
        # so zero-minute accounts cannot submit VoxCPM free; completion settles
        # actual duration with the safety-net cap.
        return 60.0
    if segmented or not max_duration_allowed or math.isinf(max_duration_allowed):
        return dur
    return min(dur, float(max_duration_allowed))


def _recap_estimated_voice_seconds(
    tts_engine: str,
    checked_duration: Optional[float],
    max_duration_allowed: Optional[float],
    *,
    is_admin: bool = False,
    segmented: bool = False,
) -> int:
    """Compute submit-time clone-voice hold: ceil(effective_duration * 1.0)."""
    if is_admin or str(tts_engine or "").strip().lower() != "voxcpm":
        return 0
    eff = _recap_effective_duration_seconds(
        checked_duration,
        max_duration_allowed,
        segmented=segmented,
    )
    return max(1, math.ceil(eff * 1.0))


def _recap_insufficient_voice_minutes_response(
    *,
    required_seconds: int,
    available_seconds: int,
) -> JSONResponse:
    """Structured 403 payload before any charge when Voice Clone Minutes fall short."""
    req_min = round(max(0, int(required_seconds)) / 60.0, 1)
    avail_min = round(max(0, int(available_seconds)) / 60.0, 1)
    detail = (
        f"Persona / Clone အသံဖြင့် Recap ဖန်တီးရန် Voice Clone မိနစ် မလုံလောက်ပါ "
        f"(လိုအပ်ချက် ~{req_min} မိနစ် • လက်ကျန် {avail_min} မိနစ်)။ "
        "Gemini AI / Edge TTS (Recap Credit သီးသန့်) သို့ ပြောင်းသုံးနိုင်သည် (သို့) VIP tab တွင် Voice မိနစ် ဖြည့်နိုင်ပါသည်။"
    )
    return JSONResponse(
        status_code=403,
        content={
            "detail": detail,
            "code": "INSUFFICIENT_VOICE_MINUTES",
            "required_seconds": int(required_seconds),
            "available_seconds": int(available_seconds),
            "fallback_engines": ["gemini", "edge"],
        },
    )


def _probe_single_media_duration(path_str: Optional[str]) -> Optional[float]:
    """Measure exact duration in seconds for a local WAV/MP3/MP4 output, or None."""
    if not path_str:
        return None
    p = Path(path_str)
    if not p.is_file():
        return None
    # Fast path for PCM WAV (exact frames / rate)
    try:
        import wave
        with wave.open(str(p), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate > 0 and frames > 0:
                return float(frames) / float(rate)
    except Exception:
        pass
    try:
        from app.services.recap_engine.core import probe_duration, find_ffprobe
        val = float(probe_duration(find_ffprobe(), p))
        if val > 0:
            return val
    except Exception:
        pass
    try:
        from app.services.downloader import probe_local_media
        val = float((probe_local_media(str(p)) or {}).get("duration") or 0.0)
        if val > 0:
            return val
    except Exception:
        pass
    return None


def _measure_recap_voice_seconds(
    audio_paths: list[Optional[str]],
    video_paths: list[Optional[str]],
) -> Optional[int]:
    """Sum synthesized narration duration (seconds) from output audio paths,
    falling back to video outputs if no separate audio path exists. Returns None
    when neither could be probed.
    """
    total = 0.0
    measured_any = False
    for ap in audio_paths:
        dur = _probe_single_media_duration(ap)
        if dur is not None and dur > 0:
            total += dur
            measured_any = True
    if measured_any:
        return max(1, math.ceil(total))
    for vp in video_paths:
        dur = _probe_single_media_duration(vp)
        if dur is not None and dur > 0:
            total += dur
            measured_any = True
    if measured_any:
        return max(1, math.ceil(total))
    return None


async def _refund_recap_voice_minutes(
    user_id: int,
    job_id: Optional[str],
    *,
    billing_mode: Optional[str] = None,
    entry: Optional[dict] = None,
    session: Optional[AsyncSession] = None,
    note: Optional[str] = None,
) -> int:
    """Refund held Voice Clone Minutes for an Option B recap job.

    Strictly checks the job's recorded ``billing_mode`` so pre-deploy /
    ``credit_only`` / ``admin_free`` jobs never trigger minute refunds.
    Idempotent via ``net_job_seconds_held`` + ``refund_job_seconds``.
    """
    if not job_id or settings.is_admin(user_id):
        return 0
    if billing_mode != _RECAP_BILLING_CREDIT_PLUS_VOICE:
        return 0
    from app.services.ledger import net_job_seconds_held, refund_job_seconds

    async def _run(s: AsyncSession) -> int:
        held_before = await net_job_seconds_held(s, user_id, job_id)
        if held_before <= 0:
            return 0
        await refund_job_seconds(
            s, user_id, job_id, note=note or f"Recap voice hold refund ({job_id})"
        )
        held_after = await net_job_seconds_held(s, user_id, job_id)
        return max(0, held_before - held_after)

    refunded_now = 0
    if session is not None:
        refunded_now = await _run(session)
    else:
        async with AsyncSessionLocal() as _s:
            refunded_now = await _run(_s)
            await _s.commit()

    if entry is not None and refunded_now > 0:
        entry["voice_refunded"] = True
        entry["voice_seconds_refunded"] = (
            int(entry.get("voice_seconds_refunded") or 0) + refunded_now
        )
    return refunded_now


# ── Batch BV: own-voice recap narration ──────────────────────────────
# A recap can be narrated in the user's OWN voice: a sample uploaded
# via POST /recap/upload_voice_ref drives VoxCPM cloning instead of a
# curated persona sample. Own-voice IS clone narration, so it inherits
# the Batch AW gate exactly (Option 1, owner decision 2026-11-06):
# PURCHASED + admins only, enforced by clamp-never-reject at submit
# and at execution entry. The upload filename embeds the owner id
# (webapp_ref_u<user_id>_<uuid12>.<ext>) so ownership validation at
# submit is a prefix check — no registry table.
RECAP_OWN_VOICE_VIP_ONLY_DETAIL = (
    "ကိုယ်ပိုင်အသံနဲ့ ဇာတ်လမ်းပြောခြင်းသည် VIP အသုံးပြုသူများအတွက်သာ ဖြစ်ပါသည်။ "
    "VIP tab တွင် Plan ဝယ်ယူနိုင်ပါသည်။"
)
VOXCPM_REF_AUDIO_EXTS = (".wav", ".mp3", ".ogg", ".m4a")
_RECAP_VOICE_REF_ID_RE = re.compile(r"^webapp_ref_u([0-9]+)_([0-9a-f]{12})$")


def _clamp_recap_voice_ref(
    req: "RecapConfigRequest",
    *,
    user: Optional[User] = None,
    user_id: Optional[int] = None,
    is_vip: Optional[bool] = None,
) -> Optional[str]:
    """Batch BV: own-voice narration is a purchased-tier benefit.

    A FREE user's uploaded-sample reference is dropped (handle AND
    resolved path) at the same two choke points as the Batch AW
    engine clamp — submit and execution entry — and the substitution
    is logged at INFO. The job itself is never rejected: it narrates
    with the picked persona on the clamped engine instead. Returns
    the surviving resolved path (or None).
    """
    if req is None:
        return None
    if user is not None:
        # The user row is the tier's source of truth (the AW clamp's
        # rule): the canonical tier helper, plus an is_admin attribute
        # when a caller supplies one (real User rows carry none — for
        # them the helper's settings.is_admin(id) check decides).
        is_vip = _can_use_custom_logo(user) or bool(
            getattr(user, "is_admin", False)
        )
    elif is_vip is None and user_id is not None:
        is_vip = settings.is_admin(user_id)
    if is_vip:
        return getattr(req, "voice_ref_path", None)
    had_ref = bool(
        getattr(req, "voice_ref_id", None) or getattr(req, "voice_ref_path", None)
    )
    req.voice_ref_id = None
    req.voice_ref_path = None
    if had_ref:
        logger.info(
            "Recap own-voice reference clamped for user %s: dropped (free tier)",
            user_id if user_id is not None else (user.id if user is not None else None),
        )
    return None


def _resolve_recap_voice_ref(ref_id: str, user: User) -> Path:
    """Resolve an upload handle from POST /recap/upload_voice_ref to
    its on-disk sample, validating shape, ownership and existence.

    Raises 400 (the invalid-voice style) on any problem — callers run
    this before any credit moves, so a bad handle can never charge.
    Admins keep the service-wide ownership exemption (the
    _recap_frame_owner_id precedent); everyone else may only resolve
    a handle whose embedded owner id is their own.
    """
    detail = "ရွေးထားတဲ့ အသံနမူနာ မမှန်ကန်ပါ။ ကျေးဇူးပြု၍ နမူနာ ပြန်တင်ပေးပါခင်ဗျာ။"
    m = _RECAP_VOICE_REF_ID_RE.match(str(ref_id or "").strip())
    if not m:
        raise HTTPException(status_code=400, detail=detail)
    owner_id = int(m.group(1))
    if owner_id != user.id and not settings.is_admin(user.id):
        raise HTTPException(status_code=400, detail=detail)
    ref_dir = settings.storage_root / "temp" / "voxcpm_refs"
    for ext in VOXCPM_REF_AUDIO_EXTS:
        candidate = ref_dir / f"{ref_id}{ext}"
        if candidate.is_file():
            return candidate
    raise HTTPException(status_code=400, detail=detail)


# ── Batch DD: Step-2 rebuild shared pieces ──────────────────────
# Custom subtitle fonts ride the voice-ref pattern exactly: the
# upload endpoint stores the file under an owner-embedded handle
# (webapp_font_u<uid>_<uuid12>), the config carries only the
# handle, and the server resolves it to a path at submit (and at
# preview) — a forged path can never take effect. The engine falls
# back to the chosen built-in on any load failure.
RECAP_SUB_FONT_EXTS = (".ttf", ".otf")
RECAP_SUB_FONT_MAX_BYTES = 5 * 1024 * 1024
_RECAP_SUB_FONT_ID_RE = re.compile(r"^webapp_font_u([0-9]+)_([0-9a-f]{12})$")
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _resolve_recap_sub_font(font_id: str, user: User) -> Path:
    """Resolve a custom-font upload handle to its file, validating
    shape, ownership and existence (the voice-ref resolver's rules).
    Raises 400 on any problem — callers run this before any credit
    moves."""
    detail = "ရွေးထားတဲ့ Font မမှန်ကန်ပါ။ ကျေးဇူးပြု၍ Font ပြန်တင်ပေးပါခင်ဗျာ။"
    m = _RECAP_SUB_FONT_ID_RE.match(str(font_id or "").strip())
    if not m:
        raise HTTPException(status_code=400, detail=detail)
    owner_id = int(m.group(1))
    if owner_id != user.id and not settings.is_admin(user.id):
        raise HTTPException(status_code=400, detail=detail)
    font_dir = settings.storage_root / "temp" / "sub_fonts"
    for ext in RECAP_SUB_FONT_EXTS:
        candidate = font_dir / f"{font_id}{ext}"
        if candidate.is_file():
            return candidate
    raise HTTPException(status_code=400, detail=detail)


def _dd_overrides_from(req) -> Optional[Dict[str, Any]]:
    """Build the engine's sub_visual_overrides dict from a request's
    flat DD style fields (both recap schemas carry the same names).
    Returns None when the user made no custom choices, so presets
    render exactly as before. bg_opacity converts percent → 0..1."""
    out: Dict[str, Any] = {}
    for src, dst in (
        ("sub_text_color", "text_color"),
        ("sub_outline_color", "outline_color"),
        ("sub_bg_color", "bg_color"),
    ):
        v = getattr(req, src, None)
        if v:
            out[dst] = v
    ow = getattr(req, "sub_outline_width", None)
    if ow is not None:
        out["outline_width"] = float(ow)
    bo = getattr(req, "sub_bg_opacity", None)
    if bo is not None:
        out["bg_opacity"] = max(0.0, min(1.0, float(bo) / 100.0))
    sh = getattr(req, "sub_shadow", None)
    if sh is not None:
        out["shadow"] = bool(sh)
    return out or None


def _dd_blur_boxes_fractions(req) -> Optional[list]:
    """The request's manual blur-box list as picture-fraction dicts
    (the engine's basis), or None. When the client sent no list but
    the config carries the LEGACY single explicit box, migrate it
    to a one-element list (Batch DD migration-on-read) using the
    single-box path's own defaults (x 5% / w 90% when unset)."""
    boxes = getattr(req, "blur_boxes", None)
    if boxes:
        return [b.to_fraction_dict() for b in boxes]
    if getattr(req, "blur_box_explicit", False):
        return [{
            "x": (float(req.blur_x) / 100.0) if getattr(req, "blur_x", None) is not None else 0.05,
            "y": float(req.blur_y) / 100.0,
            "w": (float(req.blur_w) / 100.0) if getattr(req, "blur_w", None) is not None else 0.90,
            "h": float(req.blur_h) / 100.0,
            "strength": int(req.blur_strength),
        }]
    return None


def _user_logo_dir(user_id: int) -> Path:
    return settings.storage_root / "user_logos" / str(int(user_id))


def _find_user_logo(user_id: int) -> Optional[Path]:
    logo_dir = _user_logo_dir(user_id)
    for ext in LOGO_FILE_EXTS:
        candidate = logo_dir / f"logo{ext}"
        if candidate.is_file():
            return candidate
    return None


def _resolve_recap_logo_path(
    user_id: int, include_logo: bool, is_vip: bool
) -> Optional[str]:
    """The ONLY logo path the recap generate path may render with.

    Returns the user's own stored logo when (and only when) the
    toggle is on, the job owner is paid/admin (is_vip already carries
    the admin exemption), and a logo file actually exists. Toggle on
    with nothing uploaded renders no logo; free users never resolve.
    """
    if not include_logo or not is_vip:
        return None
    found = _find_user_logo(user_id)
    return str(found) if found is not None else None


def _validate_logo_image(path: Path, expected_format: str) -> None:
    """Decode-check a saved upload with Pillow: the file must really be
    the image format its extension claims (an extension is not proof),
    with sane dimensions."""
    from PIL import Image

    try:
        with Image.open(path) as im:
            fmt = im.format
            width, height = im.size
            im.verify()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="တင်လိုက်တဲ့ ဖိုင်ဟာ မှန်ကန်တဲ့ ပုံဖိုင် မဟုတ်ပါ — PNG, JPG ဒါမှမဟုတ် WEBP ပုံ ပြန်ရွေးပေးပါ",
        )
    if fmt != expected_format or not (1 <= width <= 4096 and 1 <= height <= 4096):
        raise HTTPException(
            status_code=400,
            detail="တင်လိုက်တဲ့ ဖိုင်ဟာ မှန်ကန်တဲ့ ပုံဖိုင် မဟုတ်ပါ — PNG, JPG ဒါမှမဟုတ် WEBP ပုံ ပြန်ရွေးပေးပါ",
        )


def _logo_state_payload(user: User) -> Dict[str, Any]:
    found = _find_user_logo(user.id)
    logo_url = None
    if found is not None:
        # An <img> tag cannot send the initData header, so the preview
        # URL carries a signed token scoped to exactly this user's
        # logo (same HMAC scheme as the recap stream tokens). The file
        # endpoint serves ONLY the resolved user's own logo, so a
        # token can never reach another user's file.
        token = _make_stream_token("userlogo", user.id, "logo")
        logo_url = f"/api/user/logo/file?t={token}" if token else "/api/user/logo/file"
    return {
        "can_use_logo": _can_use_custom_logo(user),
        "has_logo": found is not None,
        "logo_url": logo_url,
    }


async def _recap_active_job_count(db, user_id: int) -> int:
    """Unique active recap jobs for a user (registry ∪ DB, not summed).

    Registry entries and DB rows describe the same job, so counting both
    naively would double-count and break limits above 1.

    SCRIPT_READY counts as active (hunt #3 E8): a job parked in script
    review still owns the user's slot — its DB row stays RUNNING, so
    the DB branch below already covers the normal path; the registry
    branch names it too so a registry-only job (whose DB insert failed
    at submit) cannot open a parallel-submit window either.
    """
    ids = set()
    for job_id, entry in RECAP_JOBS_REGISTRY.items():
        if entry.get("user_id") == user_id and entry.get("status") in ("QUEUED", "RUNNING", "SCRIPT_READY"):
            ids.add(job_id)
    try:
        res = await db.execute(
            select(Job.id).where(
                Job.user_id == user_id,
                Job.job_type == JobType.RECAP.value,
                Job.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
            )
        )
        ids.update(str(row[0]) for row in res.all())
    except Exception as exc:
        # Batch AJ L13: silently swallowing a DB error here returned a
        # registry-only count — exactly while the database was
        # struggling, the per-user concurrency cap degraded invisibly.
        # The only production caller is the submit path (under the
        # user-row lock): fail the submit visibly instead.
        logger.warning("Recap active-job count failed for user %s: %s", user_id, exc)
        raise
    return len(ids)


def _recap_job_config(job) -> Optional["RecapConfigRequest"]:
    """Rebuild the submit-time config for a recap Job row.

    Used by user retry (batch L) and the History re-run prefill: the
    persisted blob (jobs.recap_config) wins; legacy rows without one
    fall back to the same reconstruction _recap_redispatch_plan uses
    (source/title/voice from the row, defaults for everything else).
    Returns None when no usable config can be built.
    """
    state = _load_persisted_recap_state(job)
    if state is not None:
        try:
            return RecapConfigRequest(**state["config"])
        except Exception:
            return None
    source_url = getattr(job, "source_audio_path", None) or job.input_text or ""
    if not source_url:
        return None
    clean_title = _clean_recap_display_title(
        convert_zawgyi_to_unicode(job.input_text or ""), source_url
    ) or "Movie Recap Video"
    try:
        return RecapConfigRequest(
            url=source_url,
            title=clean_title,
            voice_slug=job.preset_voice or "komin",
        )
    except Exception:
        return None


def _recap_source_available(url) -> bool:
    """Can this recap source still be fetched for a retry / re-run?

    URL sources are always re-downloadable. Upload sources are local
    files under storage/temp/recap_uploads: the runner uses them in
    place and never deletes them, and the retention sweep only purges
    them together with the (terminal) job row that references them —
    so while the failed job's History row exists, its upload normally
    still exists too. It CAN be gone early (e.g. the same upload was
    shared with an older job whose retention purge took the file), so
    availability is always checked live, never assumed.
    """
    u = (url or "").strip()
    if u.startswith(("http://", "https://")):
        return True
    if not u:
        return False
    try:
        return Path(u.strip("'\"")).is_file()
    except Exception:
        return False


def _recap_can_retry(job) -> bool:
    """True when a FAILED recap job can be retried right now: it has a
    rebuildable config and its source is still available."""
    if job is None or job.status != JobStatus.FAILED.value:
        return False
    cfg = _recap_job_config(job)
    return bool(cfg is not None and _recap_source_available(cfg.url))


# ── Queue position / ETA (batch L, shape-conditioned in batch CB) ───
# Recap execution is one FIFO path: submissions dispatch immediately
# and wait on RECAP_EXECUTION_SEMAPHORE (2 concurrent renders), and
# the server queue worker claims the oldest QUEUED row first. A job's
# queue position is therefore honestly countable: how many active
# recap jobs were created before it. The ETA derives from the median
# processing time (started_at → finished_at) of recently completed
# recap jobs — a real signal recorded on every Job row — and is only
# reported when enough samples exist; otherwise the payload carries
# the position alone rather than an invented number.
#
# Batch CB Item 1: the medians are now SHAPE-CONDITIONED — bucketed
# by narration engine class (voxcpm / gemini / edge, from the job's
# persisted config) x source-duration band (<=60 / <=180 / <=600 /
# >600 s), with per-engine and global levels behind them (fallback
# chain bucket -> engine -> global). One 60 s-cached query of the
# last 100 completed rows feeds every level; the GLOBAL level keeps
# Batch L's exact definition (the most recent 25 rows, >= 3 usable
# samples) so its long-standing behaviour is unchanged. Rows whose
# persisted config is missing/unreadable still count toward the
# engine/global levels they can be attributed to (a row with no
# config at all counts only globally — the pre-persistence era).
_RECAP_ETA_MEDIAN_CACHE: dict = {"at": 0.0, "seconds": None, "medians": None}


def _recap_median_of(durations) -> Optional[float]:
    """The house median convention (sorted, upper-middle element) —
    the same pick Batch L's global median has always made."""
    vals = sorted(durations)
    if not vals:
        return None
    return float(vals[len(vals) // 2])


def _recap_row_shape(job) -> tuple:
    """(engine_class, duration_seconds) for a completed Job row, from
    its persisted recap state. Either may be None when the row
    predates persistence or the blob is unusable — the caller then
    attributes the row only to the levels it can prove."""
    state = _load_persisted_recap_state(job)
    engine = None
    duration = None
    if state is not None:
        cfg = state.get("config") or {}
        engine = recap_eta.engine_class(cfg.get("tts_engine"))
        raw_duration = state.get("source_duration_seconds")
        if not raw_duration:
            raw_duration = cfg.get("duration_seconds")
        try:
            duration = float(raw_duration) if raw_duration else None
        except (TypeError, ValueError):
            duration = None
    if duration is None:
        secs = getattr(job, "seconds_estimated", None)
        try:
            duration = float(secs) if secs else None
        except (TypeError, ValueError):
            duration = None
    return engine, duration


async def _recap_median_buckets() -> dict:
    """Shape-conditioned recap duration medians, 60 s-cached.

    Returns {"buckets": {(engine, band): (median, count)},
             "engines": {engine: (median, count)},
             "global": (median, count) | None}.
    """
    now = time.time()
    if now - float(_RECAP_ETA_MEDIAN_CACHE.get("at") or 0.0) < 60:
        cached = _RECAP_ETA_MEDIAN_CACHE.get("medians")
        if cached is not None:
            return cached
    medians: dict = {"buckets": {}, "engines": {}, "global": None}
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            res = await session.execute(
                select(Job)
                .where(
                    (Job.job_type == JobType.RECAP.value)
                    | (Job.id.startswith("rc_"))
                    | (Job.engine == "recap"),
                    Job.status == JobStatus.SUCCESS.value,
                    Job.started_at.is_not(None),
                    Job.finished_at.is_not(None),
                )
                .order_by(Job.finished_at.desc())
                .limit(100)
            )
            rows = list(res.scalars().all())
        usable = []
        for job in rows:
            try:
                d = (job.finished_at - job.started_at).total_seconds()
            except Exception:
                continue
            if 30 <= d <= 6 * 3600:
                usable.append((job, float(d)))
        # Global: Batch L's definition — the most recent 25 rows.
        global_durations = [d for _job, d in usable[:25]]
        if global_durations:
            medians["global"] = (
                _recap_median_of(global_durations),
                len(global_durations),
            )
        bucket_rows: dict = {}
        engine_rows: dict = {}
        for job, d in usable:
            engine, duration = _recap_row_shape(job)
            if engine is not None:
                engine_rows.setdefault(engine, []).append(d)
                if duration:
                    band = recap_eta.duration_band(duration)
                    bucket_rows.setdefault((engine, band), []).append(d)
        medians["engines"] = {
            k: (_recap_median_of(v), len(v)) for k, v in engine_rows.items()
        }
        medians["buckets"] = {
            k: (_recap_median_of(v), len(v)) for k, v in bucket_rows.items()
        }
    except Exception as exc:
        logger.warning(f"Could not compute recap duration medians: {exc}")
        medians = {"buckets": {}, "engines": {}, "global": None}
    _RECAP_ETA_MEDIAN_CACHE.update(
        {
            "at": now,
            "seconds": (
                medians["global"][0]
                if medians["global"] and medians["global"][1] >= 3
                else None
            ),
            "medians": medians,
        }
    )
    return medians


def _recap_resolve_total_range(
    medians: dict,
    engine: str,
    duration_seconds,
    quality: str,
    *,
    require_samples: bool,
) -> Optional[tuple]:
    """Resolve a job shape's TOTAL duration range.

    Returns (low, high, point, source) or None. Priority: the shape's
    own bucket median -> its engine median -> the global median (each
    gated by its sample threshold, surfaced with the calibrated
    spread; the point is the median itself). With require_samples
    False and no trusted median, the calibrated stage model
    (app.services.recap_eta) supplies the range — the cold-start
    basis for the pre-submit surfaces. With require_samples True and
    no trusted median there is NO estimate (Batch L's rule, kept for
    the in-job status surfaces).
    """
    eng = recap_eta.engine_class(engine)
    band = recap_eta.duration_band(duration_seconds)
    thresholds = recap_eta.MEDIAN_THRESHOLDS
    bucket = (medians.get("buckets") or {}).get((eng, band))
    if bucket and bucket[1] >= thresholds["bucket"]:
        low, high = recap_eta.spread_range(bucket[0])
        return (low, high, float(bucket[0]), "bucket")
    engine_med = (medians.get("engines") or {}).get(eng)
    if engine_med and engine_med[1] >= thresholds["engine"]:
        low, high = recap_eta.spread_range(engine_med[0])
        return (low, high, float(engine_med[0]), "engine")
    glob = medians.get("global")
    if glob and glob[1] >= thresholds["global"]:
        low, high = recap_eta.spread_range(glob[0])
        return (low, high, float(glob[0]), "global")
    if require_samples:
        return None
    try:
        d = float(duration_seconds or 0)
    except (TypeError, ValueError):
        d = 0.0
    if d <= 0:
        return None
    low, high = recap_eta.total_range(
        eng, d, recap_eta.estimate_segments(d), quality
    )
    return (low, high, (low + high) / 2.0, "calibration")


def _recap_eta_note_progress(entry: dict, stage, sub) -> None:
    """Batch CB Item 2 (reporting only): stamp stage timing + segment
    marks on the registry entry so the ETA path can derive the job's
    OWN observed pace. Called from the pipeline progress callback and
    the authored-label seam whenever the canonical stage changes or a
    new "i/n" sub-progress arrives. No pipeline behaviour changes —
    these fields are read by the status endpoint alone."""
    now = time.time()
    if stage and entry.get("eta_stage") != stage:
        entry["eta_stage"] = stage
        entry["eta_stage_since"] = now
        entry["eta_mark_first"] = None
        entry["eta_mark_last"] = None
    if sub:
        m = _RECAP_SUB_PROGRESS_RE.match(str(sub).strip())
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            if not entry.get("eta_mark_first"):
                entry["eta_mark_first"] = (done, now)
            entry["eta_mark_last"] = (done, now, total)


def _recap_observed_pace(entry: dict, stage) -> Optional[float]:
    """The running job's own observed seconds-per-unit for its current
    counted stage (segments for narration, parts for sync), from the
    progress marks. Needs real movement (>= 2 units over >= 5 s) or a
    stage-start average over >= 3 units; otherwise None (the caller
    falls back to the calibrated pace)."""
    first = entry.get("eta_mark_first")
    last = entry.get("eta_mark_last")
    if first and last:
        try:
            d_done = int(last[0]) - int(first[0])
            d_time = float(last[1]) - float(first[1])
            if d_done >= 2 and d_time >= 5.0:
                return d_time / d_done
        except (TypeError, ValueError, IndexError):
            pass
    if stage in ("tts", "sync") and last:
        since = entry.get("eta_stage_since")
        try:
            done = int(last[0])
            if since and done >= 3:
                elapsed = time.time() - float(since)
                if elapsed >= 5.0:
                    return elapsed / done
        except (TypeError, ValueError, IndexError):
            pass
    return None


def _recap_entry_shape(entry: dict) -> tuple:
    """(engine_class, duration_seconds, quality) for a registry entry."""
    engine = recap_eta.engine_class(
        entry.get("tts_engine"), bool(entry.get("voice_ref_used"))
    )
    try:
        duration = float(entry.get("source_duration_seconds") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    quality = str(entry.get("render_quality") or "standard")
    return engine, duration, quality


def _recap_eta_payload(
    *,
    status: str,
    position: Optional[int],
    totals: Optional[tuple],
    submit_totals: Optional[tuple],
    stage=None,
    segments_total=None,
    segments_done=None,
    parts_done=None,
    observed_pace=None,
    stage_frac=None,
    engine: str = "voxcpm",
    duration_seconds=0,
    quality: str = "standard",
    queue_elapsed=None,
    run_elapsed=None,
) -> dict:
    """Assemble {eta_seconds, eta_range_seconds, eta_overrun} for one
    active job from resolved totals. ``totals`` is the samples-gated
    resolution (None -> no numbers at all, Batch L's rule);
    ``submit_totals`` is the calibration-allowed resolution the user
    was shown at submit — the overrun flag compares elapsed against
    ITS high bound (Item 4), so a job is only ever "overrunning" the
    estimate it was actually given."""
    out = {"eta_seconds": None, "eta_range_seconds": None, "eta_overrun": False}
    if submit_totals is not None:
        if status == "QUEUED":
            expected_high = recap_eta.queue_waves(position) * submit_totals[1]
            if queue_elapsed is not None and queue_elapsed > expected_high:
                out["eta_overrun"] = True
        elif status == "RUNNING":
            if run_elapsed is not None and run_elapsed > submit_totals[1]:
                out["eta_overrun"] = True
    if totals is None:
        return out
    total_low, total_high, total_point, _source = totals
    if status == "QUEUED":
        waves = recap_eta.queue_waves(position)
        out["eta_seconds"] = int(round(waves * total_point))
        out["eta_range_seconds"] = [
            int(round(waves * total_low)),
            int(round(waves * total_high)),
        ]
        return out
    if status == "RUNNING":
        low, high, point = recap_eta.remaining_range(
            stage,
            engine=engine,
            duration_seconds=duration_seconds,
            quality=quality,
            segments_total=segments_total,
            segments_done=segments_done,
            parts_done=parts_done,
            observed_pace=observed_pace,
            stage_frac=stage_frac,
            total_override=(total_low, total_high),
        )
        out["eta_seconds"] = int(round(point))
        out["eta_range_seconds"] = [int(round(low)), int(round(high))]
        return out
    return out


def _recap_eta_from_entry(
    entry: dict, medians: dict, position: Optional[int], now: Optional[float] = None
) -> dict:
    """ETA payload for a registry-backed job (status endpoint hot
    path + live-jobs bulk path). Pure apart from the registry read
    the caller passes in."""
    now = now if now is not None else time.time()
    status = str(entry.get("status") or "")
    if status not in ("QUEUED", "RUNNING"):
        return {"eta_seconds": None, "eta_range_seconds": None, "eta_overrun": False}
    engine, duration, quality = _recap_entry_shape(entry)
    totals = _recap_resolve_total_range(
        medians, engine, duration, quality, require_samples=True
    )
    submit_totals = _recap_resolve_total_range(
        medians, engine, duration, quality, require_samples=False
    )
    created = entry.get("created_at")
    queue_elapsed = (
        max(0.0, now - float(created))
        if isinstance(created, (int, float))
        else None
    )
    run_started = entry.get("eta_run_started_at")
    run_elapsed = (
        max(0.0, now - float(run_started))
        if isinstance(run_started, (int, float))
        else queue_elapsed
    )
    stage = entry.get("eta_stage") or entry.get("progress_stage")
    sub_done = sub_total = None
    sub = entry.get("progress_sub")
    if sub:
        m = _RECAP_SUB_PROGRESS_RE.match(str(sub).strip())
        if m:
            sub_done, sub_total = int(m.group(1)), int(m.group(2))
    segments_total = sub_total or entry.get("segments_count")
    stage_frac = None
    pct = entry.get("progress_percent")
    if stage in recap_eta.PROGRESS_BANDS and isinstance(pct, (int, float)):
        lo_b, hi_b = recap_eta.PROGRESS_BANDS[stage]
        stage_frac = max(0.0, min(1.0, (float(pct) - lo_b) / max(1.0, hi_b - lo_b)))
    return _recap_eta_payload(
        status=status,
        position=position,
        totals=totals,
        submit_totals=submit_totals,
        stage=stage,
        segments_total=segments_total,
        segments_done=sub_done if stage == "tts" else None,
        parts_done=sub_done if stage == "sync" else None,
        observed_pace=_recap_observed_pace(entry, stage) if stage == "tts" else None,
        stage_frac=stage_frac,
        engine=engine,
        duration_seconds=duration,
        quality=quality,
        queue_elapsed=queue_elapsed,
        run_elapsed=run_elapsed,
    )


def _recap_eta_from_row(
    job, medians: dict, position: Optional[int], now: Optional[datetime] = None
) -> dict:
    """ETA payload for a Job row with no registry entry (the status
    endpoint's cold path + live-jobs DB rows). The row's stage is
    unknown, so a RUNNING row's stage is inferred from its elapsed
    share of the resolved total — the honest best the surviving
    signals allow."""
    now = now or datetime.now(timezone.utc)
    status = str(job.status or "")
    if status not in (JobStatus.QUEUED.value, JobStatus.RUNNING.value):
        return {"eta_seconds": None, "eta_range_seconds": None, "eta_overrun": False}
    engine, duration = _recap_row_shape(job)
    engine = engine or "voxcpm"
    state = _load_persisted_recap_state(job)
    quality = "standard"
    if state is not None:
        quality = str((state.get("config") or {}).get("render_quality") or "standard")
    totals = _recap_resolve_total_range(
        medians, engine, duration, quality, require_samples=True
    )
    submit_totals = _recap_resolve_total_range(
        medians, engine, duration, quality, require_samples=False
    )

    def _elapsed_since(dt) -> Optional[float]:
        if dt is None:
            return None
        try:
            ref = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
            return max(0.0, (now - ref).total_seconds())
        except Exception:
            return None

    queue_elapsed = _elapsed_since(job.created_at)
    run_elapsed = _elapsed_since(job.started_at)
    if status == JobStatus.RUNNING.value and totals is not None and run_elapsed is not None:
        total_point = totals[2]
        pct = 100.0 * run_elapsed / total_point if total_point > 0 else 0.0
        stage, stage_frac = recap_eta.infer_stage_from_progress(pct)
        segments_total = recap_eta.estimate_segments(duration) if duration else None
        return _recap_eta_payload(
            status="RUNNING",
            position=position,
            totals=totals,
            submit_totals=submit_totals,
            stage=stage,
            segments_total=segments_total,
            stage_frac=stage_frac,
            engine=engine,
            duration_seconds=duration or 0,
            quality=quality,
            queue_elapsed=queue_elapsed,
            run_elapsed=run_elapsed,
        )
    return _recap_eta_payload(
        status=status,
        position=position,
        totals=totals,
        submit_totals=submit_totals,
        engine=engine,
        duration_seconds=duration or 0,
        quality=quality,
        queue_elapsed=queue_elapsed,
        run_elapsed=run_elapsed,
    )


def _recap_registry_queue_position(job_id: str, entry: dict) -> Optional[int]:
    """Active recap jobs ahead of this one, from the live registry.

    Counts QUEUED/RUNNING entries created earlier than this job — the
    jobs that must finish (or start) before it on the shared FIFO
    execution path. Registry-only: the hot status path opens no DB
    session (see _deferred_db_session).
    """
    created = entry.get("created_at")
    if not isinstance(created, (int, float)):
        return None
    ahead = 0
    for other_id, other in RECAP_JOBS_REGISTRY.items():
        if other_id == job_id:
            continue
        if other.get("status") not in ("QUEUED", "RUNNING"):
            continue
        other_created = other.get("created_at")
        if isinstance(other_created, (int, float)) and other_created < created:
            ahead += 1
    return ahead


async def _recap_db_queue_position(job) -> Optional[int]:
    """Active recap jobs ahead of this one, counted on the Job table
    (cold path: the registry has no entry for this job)."""
    if job is None or job.created_at is None:
        return None
    try:
        from sqlalchemy import func as _func

        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            res = await session.execute(
                select(_func.count(Job.id)).where(
                    Job.id != job.id,
                    (Job.job_type == JobType.RECAP.value)
                    | (Job.id.startswith("rc_"))
                    | (Job.engine == "recap"),
                    Job.status.in_(
                        [JobStatus.QUEUED.value, JobStatus.RUNNING.value]
                    ),
                    Job.created_at < job.created_at,
                )
            )
            return int(res.scalar() or 0)
    except Exception as exc:
        logger.warning(f"Could not compute recap queue position for {job.id}: {exc}")
        return None


async def _recap_submit_eta_range(engine, duration_seconds, quality) -> Optional[list]:
    """Batch CB Item 3: the pre-submit TOTAL range for a job shape —
    the shape's bucket/engine/global median with its spread when a
    trusted median exists, else the calibrated stage model
    (require_samples=False). None when the shape carries no duration
    to reason from: no number is ever invented (Batch L's rule)."""
    try:
        d = float(duration_seconds or 0)
    except (TypeError, ValueError):
        d = 0.0
    if d <= 0:
        return None
    medians = await _recap_median_buckets()
    totals = _recap_resolve_total_range(
        medians, engine, d, quality, require_samples=False
    )
    if totals is None:
        return None
    return [int(round(totals[0])), int(round(totals[1]))]


async def _refund_recap_credits(user_id: int, count: int, *, job_id: str | None = None) -> bool:
    """Refund `count` recap credits in its own session/commit.

    Separate from the job-status update so a status-write failure cannot
    silently swallow the refund, and idempotent at the call site: callers
    only invoke it once per failed job (see _run_recap_background_job,
    which marks the registry entry as refunded after a successful commit).
    Returns True when credits were actually added.

    Batch O (billing option C): when `job_id` is given, the refund is
    also written to the recap-credit ledger (CREDIT, reference
    ``REFUND:<job_id>``) in the SAME transaction as the balance change,
    and the amount refunded from here is capped at the job's
    outstanding balance — total DEBITs minus total refunds on the
    ledger (Batch DC) — so a re-charge created by an admin retry
    stays refundable while a true double refund stays impossible.
    """
    if count <= 0:
        return False
    if settings.is_admin(user_id):
        return False
    from sqlalchemy.exc import IntegrityError

    try:
        from app.db.session import AsyncSessionLocal
        from app.services.ledger import (
            recap_debit_total_for_job,
            recap_next_refund_reference,
            recap_refund_event_exists,
            recap_refund_family,
            record_recap_credit_event,
        )

        async with AsyncSessionLocal() as session:
            refund_reference: str | None = None
            if job_id:
                # Batch DC: the guard is the OUTSTANDING amount, not
                # the mere existence of an earlier refund. An admin
                # retry re-charges a fully-refunded job (Batch AJ
                # H6); when the retry also fails, that re-charge must
                # be refundable even though a REFUND row already
                # exists (2026-11-09: two retried jobs lost 5 credits
                # to the old exists-check). Jobs whose charges predate
                # the ledger (no DEBIT rows) keep the exists-check.
                debit_total = await recap_debit_total_for_job(session, job_id)
                if debit_total > 0:
                    refunded_total, _refund_rows = await recap_refund_family(
                        session, job_id
                    )
                    outstanding = debit_total - refunded_total
                    if outstanding <= 0:
                        return False
                    count = min(count, outstanding)
                    refund_reference = await recap_next_refund_reference(
                        session, job_id
                    )
                elif await recap_refund_event_exists(session, user_id, job_id):
                    return False
            # Atomic increment (hunt F5): no read-modify-write window in
            # which a concurrent charge could be overwritten.
            result = await session.execute(
                update(User)
                .where(User.id == user_id)
                .values(recap_credits=User.recap_credits + count)
            )
            if not result.rowcount:
                return False
            if job_id:
                await record_recap_credit_event(
                    session,
                    user_id=user_id,
                    direction="CREDIT",
                    credits=count,
                    reference_id=refund_reference or f"REFUND:{job_id}",
                    note=f"Recap credit refund for job {job_id}",
                )
            await session.commit()
            return True
    except IntegrityError as exc:
        # Batch AJ (H8): the recap REFUND unique index fired — a
        # concurrent refund for this job committed first. That is
        # "already refunded", not a failure: log it distinctly so it
        # is never mistaken for a lost refund in the logs.
        logger.info(
            f"Recap credit refund for user {user_id} job {job_id} already on the ledger (unique index): {exc}"
        )
        return False
    except Exception as exc:
        logger.warning(f"Recap credit refund failed for user {user_id}: {exc}")
        return False


async def _charge_recap_credits(user_id: int, count: int, *, job_id: str | None = None) -> bool:
    """Deduct `count` recap credits in its own session/commit.

    All-or-nothing: when the balance cannot cover the full amount,
    NOTHING is deducted and False is returned. Admins are never charged
    (returns True without touching the balance). Used for the segmented
    recap per-part charge; the normal 1-credit submit deduction stays
    inline in create_recap_job.
    """
    if count <= 0:
        return True
    if settings.is_admin(user_id):
        return True
    try:
        from app.db.session import AsyncSessionLocal
        from app.services.ledger import record_recap_credit_event

        async with AsyncSessionLocal() as session:
            # Atomic all-or-nothing charge (hunt F5): the WHERE clause
            # is the balance check, so two concurrent charges can no
            # longer both pass a stale read and drive the balance
            # negative / hand out a free job. 0 rows updated means the
            # user is missing or the balance cannot cover the charge.
            result = await session.execute(
                update(User)
                .where(User.id == user_id, User.recap_credits >= count)
                .values(recap_credits=User.recap_credits - count)
            )
            if not result.rowcount:
                return False
            if job_id:
                # Batch O (billing option C): the charge lands on the
                # recap-credit ledger in the same transaction.
                await record_recap_credit_event(
                    session,
                    user_id=user_id,
                    direction="DEBIT",
                    credits=count,
                    reference_id=job_id,
                    note=f"Recap credit charge for job {job_id}",
                )
            await session.commit()
            return True
    except Exception as exc:
        logger.warning(f"Recap credit charge failed for user {user_id}: {exc}")
        return False


def _segmented_credits_message(total_parts: int, balance: int | None = None) -> str:
    """R40 deck M6: ONE builder for the segmented insufficient-credits
    message — the worker exception and the submit-time pre-check used
    to carry near-identical copies that could drift apart."""
    base = (
        f"ဒီဗီဒီယိုရှည်ကို အပိုင်း {int(total_parts)} ပိုင်း "
        f"ခွဲထုတ်ရမှာဖြစ်ပြီး အပိုင်းတစ်ခုစီ 1 Credit နှုန်းနဲ့ စုစုပေါင်း {int(total_parts)} Credits "
        "လိုအပ်ပါတယ်။ "
    )
    if balance is not None:
        return f"💳 {base}လက်ကျန် Credit ({balance}c) မလုံလောက်ပါ — Credit ထပ်ဖြည့်ပြီးမှ ပြန်စမ်းပါခင်ဗျာ။"
    return f"💳 Recap Credit မလုံလောက်ပါ — {base}Credit ထပ်ဖြည့်ပြီးမှ ပြန်စမ်းပါခင်ဗျာ။"


def _tiered_credits_message(total_credits: int, balance: int | None = None) -> str:
    """Batch AO: the tiered-pricing sibling of _segmented_credits_message
    — same shape and suffixes, but the amount is the duration-band total
    (1 Credit per started 3-minute band), not a per-part count. Used by
    the submit-time pre-check and the worker top-up exception whenever
    tiered pricing priced the job."""
    base = (
        f"ဒီဗီဒီယိုရဲ့ ကြာချိန်အလိုက် (၃ မိနစ် တစ်ပိုင်း 1 Credit နှုန်း) "
        f"စုစုပေါင်း {int(total_credits)} Credits "
        "လိုအပ်ပါတယ်။ "
    )
    if balance is not None:
        return f"💳 {base}လက်ကျန် Credit ({balance}c) မလုံလောက်ပါ — Credit ထပ်ဖြည့်ပြီးမှ ပြန်စမ်းပါခင်ဗျာ။"
    return f"💳 Recap Credit မလုံလောက်ပါ — {base}Credit ထပ်ဖြည့်ပြီးမှ ပြန်စမ်းပါခင်ဗျာ။"


# R40 deck M6: one canonical script-review + split hint (the submit
# path and the background-job path worded the same rule two ways).
_SCRIPT_REVIEW_SPLIT_HINT = (
    "📝 Script ကို အရင်စစ်မယ် ရွေးထားရင် ၁၀ မိနစ်အထိပဲ ရပါတယ် — "
    "အပိုင်းခွဲပြီး လုပ်ချင်ရင် Script review ကို ပိတ်ပြီး ပြန်စမ်းပါခင်ဗျာ။"
)


class _RecapInsufficientCreditsError(Exception):
    """A segmented recap job whose exact per-part charge the user's
    balance cannot cover. Raised in the worker BEFORE any part renders;
    nothing beyond the submit-time credit has been taken, and the
    normal failure path refunds that, leaving the user exactly whole.
    """

    def __init__(self, total_parts: int, *, total_credits: int | None = None):
        self.total_parts = int(total_parts)
        # Batch AO: when tiered pricing priced the job, the message
        # names the band total instead of the per-part count.
        self.total_credits = (
            int(total_credits) if total_credits is not None else None
        )
        if self.total_credits is not None:
            super().__init__(_tiered_credits_message(self.total_credits))
        else:
            super().__init__(_segmented_credits_message(self.total_parts))


def _persisted_recap_state(
    req: "RecapConfigRequest",
    *,
    credit_deducted: bool,
    credits_charged: int,
    is_vip: bool,
    phase: str,
    estimated_parts=None,
    credit_refunded: bool = False,
    credits_refunded: int = 0,
    source_duration_seconds: float = 0.0,
    tiered_pricing: bool = False,
    max_quality_surcharge: int = 0,
    billing_mode: Optional[str] = None,
    voice_seconds_held: int = 0,
    voice_seconds_settled: Optional[int] = None,
    voice_settled: bool = False,
    voice_refunded: bool = False,
    voice_seconds_refunded: int = 0,
) -> str:
    """Serialize everything a recap job needs to survive a restart.

    Stored on the Job row (jobs.recap_config) at submit time. Before
    this existed, the row carried only URL/title/voice, so the queue
    worker's post-restart re-dispatch rebuilt the job with DEFAULT
    settings (ratio, subtitle style, TTS engine, split choices and the
    script-review phase all lost), no record of the submit credit
    (refunds silently vanished) and a zeroed per-part charge counter
    (the segment top-up then charged the full N parts a second time).

    ``credit_refunded`` / ``credits_refunded`` (hunt #5): the refund
    state rides along too, so a restart between a refund and a later
    re-dispatch (or a registry-less admin cancel) can neither lose the
    charge amount nor refund the same charge twice.

    ``billing_mode`` / ``voice_seconds_*`` (Phase 5 Option B): pins the
    submit-time billing contract so in-flight jobs always settle or
    refund according to how they were charged, never under later rule changes.
    """
    resolved_billing_mode = billing_mode or (
        _RECAP_BILLING_ADMIN_FREE if not credit_deducted else _RECAP_BILLING_CREDIT_ONLY
    )
    return json.dumps(
        {
            "config": req.model_dump(),
            "credit_deducted": bool(credit_deducted),
            "credits_charged": int(credits_charged),
            "is_vip": bool(is_vip),
            "phase": phase,
            "credits_estimated": estimated_parts,
            "credit_refunded": bool(credit_refunded),
            "credits_refunded": int(credits_refunded),
            "billing_mode": resolved_billing_mode,
            "voice_seconds_held": int(voice_seconds_held or 0),
            "voice_seconds_settled": (
                int(voice_seconds_settled) if voice_seconds_settled is not None else None
            ),
            "voice_settled": bool(voice_settled),
            "voice_refunded": bool(voice_refunded),
            "voice_seconds_refunded": int(voice_seconds_refunded or 0),
            # Batch AO: the submit-resolved source duration and the
            # job's pinned pricing mode ride along, so a re-dispatched
            # worker prices the band from the SAME duration the
            # submit quote used (and under the mode in force at
            # submit), never a second probe or a flipped switch.
            "source_duration_seconds": float(source_duration_seconds or 0),
            "tiered_pricing": bool(tiered_pricing),
            # Batch BE: the Max render-quality surcharge pinned at
            # submit (1 when tiered pricing was armed AND the clamped
            # quality was Max, else 0) rides along too, so a
            # re-dispatched worker charges — and refunds — the exact
            # total the submit quote showed.
            "max_quality_surcharge": int(max_quality_surcharge or 0),
        },
        ensure_ascii=False,
    )


def _load_persisted_recap_state(job) -> Optional[dict]:
    """Parse a Job row's persisted recap state, or None when the row
    predates persistence (or the blob is unusable) — callers then fall
    back to the historical reconstruction."""
    raw = getattr(job, "recap_config", None)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("config"), dict):
        return None
    return data


async def _persist_recap_credits_charged(entry: dict) -> None:
    """Write a job's credit state back to its Job row: the topped-up
    charge total (so a restart AFTER the segment top-up restores the
    real total instead of the submit-time 1, which would charge the
    same parts twice and over-refund on failure) and, once issued, the
    refund flag + amount (hunt #5: so a restart after a refund cannot
    lose the fact and refund a second time). Best-effort: the registry
    entry remains authoritative for the live process."""
    job_id = entry.get("id")
    if not job_id:
        return
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            job = await session.get(Job, job_id)
            state = _load_persisted_recap_state(job) if job is not None else None
            if state is None:
                return
            state["credits_charged"] = int(entry.get("credits_charged") or 0)
            state["credit_deducted"] = bool(entry.get("credit_deducted"))
            state["credit_refunded"] = bool(entry.get("credit_refunded"))
            state["credits_refunded"] = int(entry.get("credits_refunded") or 0)
            if entry.get("billing_mode") is not None:
                state["billing_mode"] = entry.get("billing_mode")
            if entry.get("voice_seconds_held") is not None:
                state["voice_seconds_held"] = int(entry.get("voice_seconds_held") or 0)
            if entry.get("voice_seconds_settled") is not None:
                state["voice_seconds_settled"] = int(entry["voice_seconds_settled"])
            if entry.get("voice_settled") is not None:
                state["voice_settled"] = bool(entry.get("voice_settled"))
            if entry.get("voice_refunded") is not None:
                state["voice_refunded"] = bool(entry.get("voice_refunded"))
            if entry.get("voice_seconds_refunded") is not None:
                state["voice_seconds_refunded"] = int(entry.get("voice_seconds_refunded") or 0)
            # Batch AO: keep the pinned pricing mode + pricing
            # duration in step with the live entry when it carries
            # them (the segmented path stamps the worker-probed
            # duration as a fallback pricing source).
            if entry.get("tiered_pricing") is not None:
                state["tiered_pricing"] = bool(entry.get("tiered_pricing"))
            # Batch BE: same in-step treatment for the pinned Max
            # quality surcharge.
            if entry.get("max_quality_surcharge") is not None:
                state["max_quality_surcharge"] = int(
                    entry.get("max_quality_surcharge") or 0
                )
            if entry.get("probed_duration_seconds"):
                state["probed_duration_seconds"] = float(
                    entry["probed_duration_seconds"]
                )
            job.recap_config = json.dumps(state, ensure_ascii=False)
            await session.commit()
    except Exception as exc:
        logger.warning(f"Could not persist recap charge state for {job_id}: {exc}")


async def _clear_recap_stranded_reset_count(job_id: str) -> None:
    """Zero a recap job's persisted stranded-reset counter (Batch AB).

    The orphan sweep (app.webapp.server.reset_stale_running_recap_jobs)
    counts how many times a RUNNING row had to be rescued after a
    server restart/OOM kill and fails the job at the limit, so a job
    whose render reliably kills the instance cannot loop forever.
    Every terminal or reactivation path clears the count again: a job
    that finished (or was deliberately retried) must start its next
    life from zero. Best-effort, like the other _persist_recap_*
    helpers: a failure here only leaves a stale count, never breaks
    the path that called it.
    """
    if not job_id:
        return
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            job = await session.get(Job, job_id)
            state = _load_persisted_recap_state(job) if job is not None else None
            if state is None:
                return
            if not state.get("stranded_reset_count") and not state.get("stranded_stage"):
                return
            state["stranded_reset_count"] = 0
            # Batch CC: the stage the count belonged to clears too,
            # and the job's next life starts with no stage witness.
            state["stranded_stage"] = None
            job.recap_config = json.dumps(state, ensure_ascii=False)
            await session.commit()
        _STAGE_WITNESS_RANKS.pop(job_id, None)
        try:
            _recap_stage_witness_path(job_id).unlink(missing_ok=True)
        except Exception:
            pass
    except Exception as exc:
        logger.warning(
            f"Could not clear stranded reset count for {job_id}: {exc}"
        )


# Batch CC: stage-aware stranded counting needs to know the stage a
# crashed run had reached — the registry entry dies with the process,
# so the progress seams also mirror the FURTHEST stage reached into a
# tiny witness file in the job dir (sync file IO only: the seams run
# on executor threads). The orphan sweep (app.webapp.server) reads it
# back: a rescue whose job had advanced past the stage recorded at
# the previous rescue restarts the count for the new stage instead of
# marching to the fail limit — successful progress is not a loop.
_RECAP_STAGE_RANK = {
    "download": 0,
    "transcribe": 1,
    "context": 2,
    "translate": 3,
    "plan": 4,
    "script_ready": 4,
    "tts": 5,
    "sync": 6,
    "split": 6,
    "png": 7,
    "overlay": 8,
    "mux": 9,
    "finalize": 10,
}
_RECAP_STAGE_WITNESS_FILE = "progress_stage.json"
_STAGE_WITNESS_RANKS: dict[str, int] = {}


def _recap_stage_witness_path(job_id: str) -> Path:
    return (
        settings.storage_root
        / "recap_output"
        / str(job_id)
        / _RECAP_STAGE_WITNESS_FILE
    )


def _read_recap_stage_witness(job_id: str) -> Optional[str]:
    """Furthest pipeline stage a previous run of this job reached,
    per its witness file — None when no witness exists (rows/runs
    predating Batch CC keep the Batch AB stage-blind counting)."""
    try:
        data = json.loads(
            _recap_stage_witness_path(job_id).read_text(encoding="utf-8")
        )
        stage = data.get("stage") if isinstance(data, dict) else None
        return str(stage) if stage in _RECAP_STAGE_RANK else None
    except Exception:
        return None


def _note_recap_progress_stage(entry: dict, stage) -> None:
    """Mirror a run's furthest-reached stage into its witness file.

    Best-effort and advance-only: the file always names the furthest
    stage ANY run of this job reached, never a retry's early stages,
    so a rescue can tell "died at the same wall again" apart from
    "progressed past the old wall and died somewhere new"."""
    if not stage or stage not in _RECAP_STAGE_RANK:
        return
    if not isinstance(entry, dict):
        return
    job_id = entry.get("id")
    if not job_id:
        return
    job_id = str(job_id)
    rank = _RECAP_STAGE_RANK[stage]
    try:
        if rank <= _STAGE_WITNESS_RANKS.get(job_id, -1):
            return
        job_dir = settings.storage_root / "recap_output" / job_id
        if not job_dir.is_dir():
            return
        prev = _read_recap_stage_witness(job_id)
        if prev is not None and _RECAP_STAGE_RANK[prev] >= rank:
            _STAGE_WITNESS_RANKS[job_id] = _RECAP_STAGE_RANK[prev]
            return
        path = job_dir / _RECAP_STAGE_WITNESS_FILE
        tmp = path.with_name(_RECAP_STAGE_WITNESS_FILE + ".tmp")
        tmp.write_text(json.dumps({"stage": stage}), encoding="utf-8")
        tmp.replace(path)
        _STAGE_WITNESS_RANKS[job_id] = rank
    except Exception:
        pass


SCRIPT_READY_TTL_SECONDS = 24 * 3600

_SCRIPT_READY_EXPIRED_MESSAGE = (
    "Script review expired: the script was not rendered within 24 hours, "
    "so the job was closed and its recap credit was refunded."
)


async def _persist_recap_script_ready(entry: dict) -> None:
    """Persist the SCRIPT_READY parking state onto the Job row.

    Until now a parked script-review job existed only in the in-memory
    registry: a restart lost the parked state, and the queue worker
    re-dispatched the job from the script phase — re-running
    transcription + translation and overwriting the plan file the user
    had already edited. Recording the parking facts (phase ``render``,
    ``script_ready``, the downloaded ``source_path``, the segment count
    and the parking timestamp) in the same persisted blob as the credit
    state lets a restart restore the job exactly where it parked.
    Best-effort, like _persist_recap_credits_charged: the registry
    entry remains authoritative for the live process.
    """
    job_id = entry.get("id")
    if not job_id:
        return
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            job = await session.get(Job, job_id)
            state = _load_persisted_recap_state(job) if job is not None else None
            if state is None:
                return
            state["phase"] = "render"
            state["script_ready"] = True
            state["source_path"] = entry.get("source_path")
            state["segments_count"] = int(entry.get("segments_count") or 0)
            state["script_ready_at"] = entry.get("script_ready_at")
            job.recap_config = json.dumps(state, ensure_ascii=False)
            await session.commit()
    except Exception as exc:
        logger.warning(f"Could not persist script-ready state for {job_id}: {exc}")


async def _persist_recap_warnings(entry: dict) -> None:
    """Persist a finished job's result warnings onto its Job row.

    The registry entry carries the pipeline's warnings (e.g.
    ``blur_no_source_band`` — blur safefix 2026-11-03) for the live
    result screen; merging them into the same persisted recap_config
    blob as the credit/script state lets the DB branch of the status
    endpoint keep showing the warning after a restart. Best-effort,
    like the other _persist_recap_* helpers: the registry entry
    remains authoritative for the live process.
    """
    job_id = entry.get("id")
    if not job_id:
        return
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            job = await session.get(Job, job_id)
            state = _load_persisted_recap_state(job) if job is not None else None
            if state is None:
                return
            state["warnings"] = list(entry.get("warnings") or [])
            if entry.get("stage_timings"):
                state["stage_timings"] = dict(entry.get("stage_timings") or {})
            job.recap_config = json.dumps(state, ensure_ascii=False)
            await session.commit()
    except Exception as exc:
        logger.warning(f"Could not persist recap warnings for {job_id}: {exc}")


def _restore_script_ready_from_db(job, user=None) -> Optional[dict]:
    """Build a parked SCRIPT_READY registry entry from a Job row.

    Returns None when the row carries no usable script_ready state.
    The user's script edits live in the workspace plan file on disk
    (the script endpoints read and write that file, not the registry),
    so rebuilding the entry is all a restart needs: no transcription
    or translation is re-run and the edits survive untouched. The
    ``user`` argument is accepted for symmetry with the re-dispatch
    plan builder; the parked state restores entirely from the row.
    """
    state = _load_persisted_recap_state(job)
    if state is None or not state.get("script_ready"):
        return None
    try:
        cfg = RecapConfigRequest(**state["config"])
    except Exception:
        return None
    source_url = getattr(job, "source_audio_path", None) or job.input_text or ""
    clean_title = _clean_recap_display_title(
        convert_zawgyi_to_unicode(job.input_text or ""), source_url
    ) or "Movie Recap Video"
    entry = {
        "id": job.id,
        "user_id": job.user_id,
        "title": clean_title,
        "status": "SCRIPT_READY",
        "progress_percent": 100,
        "progress_step": "2/4",
        "created_at": time.time(),
        "video_path": None,
        "audio_path": None,
        "srt_path": None,
        "script_path": None,
        "download_urls": {},
        "error_message": None,
        "script_review": True,
        "segments_count": state.get("segments_count"),
        # Batch CB: the ETA shape, restored from the persisted config.
        "tts_engine": cfg.tts_engine,
        "render_quality": cfg.render_quality,
        "split_video": bool(cfg.split_video),
        "parts": [],
        "split_error": None,
        "split_note": None,
        "credit_deducted": bool(state.get("credit_deducted")),
        "credits_charged": int(state.get("credits_charged") or 0),
        "credit_refunded": bool(state.get("credit_refunded")),
        "credits_refunded": int(state.get("credits_refunded") or 0),
        "credits_estimated": state.get("credits_estimated"),
        # Batch AO: pricing mode + durations (see _recap_redispatch_plan).
        "source_duration_seconds": float(state.get("source_duration_seconds") or 0),
        "probed_duration_seconds": float(state.get("probed_duration_seconds") or 0),
        "tiered_pricing": state.get("tiered_pricing"),
        # Batch BE: the pinned Max surcharge (None when the blob
        # predates it — the execution entry point derives it then).
        "max_quality_surcharge": state.get("max_quality_surcharge"),
        "parts_count": None,
        "source_path": state.get("source_path"),
        "req_snapshot": state["config"],
        "is_vip": bool(state.get("is_vip")),
        "script_ready_at": state.get("script_ready_at"),
    }
    _recap_set_entry_label(
        entry, "📝 Script ready — review & edit before rendering", stage="script_ready"
    )
    return entry


async def restore_script_ready_jobs() -> int:
    """Restore every parked SCRIPT_READY recap job into the registry.

    Runs at startup (and is safe to re-run): any recap row still in a
    live status whose persisted state says it parked for script review
    gets its registry entry rebuilt from the row, unless this process
    already tracks the job. Returns how many entries were restored.
    """
    restored = 0
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            res = await session.execute(
                select(Job).where(
                    Job.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
                    (Job.job_type == JobType.RECAP.value)
                    | (Job.id.startswith("rc_"))
                    | (Job.engine == "recap"),
                )
            )
            for job in res.scalars().all():
                existing = RECAP_JOBS_REGISTRY.get(str(job.id))
                if existing and existing.get("status") in (
                    "QUEUED",
                    "RUNNING",
                    "SCRIPT_READY",
                ):
                    continue
                entry = _restore_script_ready_from_db(job)
                if entry is None:
                    continue
                RECAP_JOBS_REGISTRY[str(job.id)] = entry
                restored += 1
    except Exception as exc:
        logger.warning(f"Could not restore script-ready recap jobs: {exc}")
    if restored:
        logger.info(f"Restored {restored} parked SCRIPT_READY recap job(s) from DB")
    return restored


def _parse_script_ready_at(value) -> Optional[datetime]:
    """Parse the persisted script_ready_at ISO timestamp (UTC)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


async def _claim_persisted_recap_refund(job_id: str, amount: int) -> tuple[str, int]:
    """Claim a recap refund in the persisted Job-row state (batch M).

    The SCRIPT_READY expiry sweep refunds through a synthetic entry
    built from the persisted blob, so the in-memory claim flag in
    _refund_recap_job_charge cannot coordinate it. Claiming in the
    blob itself — re-read fresh, skip when it already records the
    refund, otherwise write the claim BEFORE the balance refund is
    issued — closes that gap. Returns ("claimed", amount),
    ("already", recorded_amount) or ("error", 0).

    Batch AJ (H8): the write is a COMPARE-AND-SWAP — the UPDATE only
    lands when the row's recap_config is byte-identical to the blob
    just read. The old read-modify-write let two concurrent claimers
    (a user cancel racing the runner's failure tail across a restart
    boundary) both read credit_refunded=False and both refund. The
    loser of the CAS re-reads: the winner's claim then reads as
    "already". Bounded retries cover unrelated blob writes (progress
    persists) landing between the read and the write.
    """
    for _attempt in range(3):
        try:
            from app.db.session import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                job = await session.get(Job, job_id)
                if job is None:
                    return ("error", 0)
                raw = job.recap_config
                state = _load_persisted_recap_state(job)
                if state is None:
                    return ("error", 0)
                if state.get("credit_refunded"):
                    return ("already", int(state.get("credits_refunded") or 0))
                state["credit_refunded"] = True
                state["credits_refunded"] = int(amount)
                new_raw = json.dumps(state, ensure_ascii=False)
                res = await session.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.recap_config == raw)
                    .values(recap_config=new_raw)
                    .execution_options(synchronize_session=False)
                )
                if res.rowcount:
                    await session.commit()
                    return ("claimed", int(amount))
                await session.rollback()
        except Exception as exc:
            logger.warning(f"Could not claim persisted recap refund for {job_id}: {exc}")
            return ("error", 0)
    # Three lost races without ever seeing a claim: another writer is
    # actively rewriting this blob. Refusing (as "already") is the
    # safe direction — a missed refund is recoverable by a later
    # sweep; a double refund is not.
    logger.warning(f"Persisted recap refund claim for {job_id} lost repeated CAS races; treating as already-claimed.")
    return ("already", 0)


async def _release_persisted_recap_refund_claim(job_id: str) -> None:
    """Undo a _claim_persisted_recap_refund claim whose refund failed,
    so a later sweep run can retry it.

    Batch AJ (H8): CAS-guarded like the claim — if the blob moved on
    since the read (e.g. a fresh claim by another path), the release
    leaves it alone instead of clearing someone else's claim with a
    stale whole-blob write.
    """
    _claim_signature: int | None = None
    for _attempt in range(3):
        try:
            from app.db.session import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                job = await session.get(Job, job_id)
                if job is None:
                    return
                raw = job.recap_config
                state = _load_persisted_recap_state(job)
                if state is None:
                    return
                if not state.get("credit_refunded"):
                    return
                if _claim_signature is None:
                    _claim_signature = int(state.get("credits_refunded") or 0)
                elif int(state.get("credits_refunded") or 0) != _claim_signature:
                    # The blob now carries a DIFFERENT claim than the
                    # one this release belongs to — not ours to clear.
                    return
                state["credit_refunded"] = False
                state["credits_refunded"] = 0
                new_raw = json.dumps(state, ensure_ascii=False)
                res = await session.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.recap_config == raw)
                    .values(recap_config=new_raw)
                    .execution_options(synchronize_session=False)
                )
                if res.rowcount:
                    await session.commit()
                    return
                await session.rollback()
        except Exception as exc:
            logger.warning(
                f"Could not release persisted recap refund claim for {job_id}: {exc}"
            )
            return


async def expire_abandoned_script_ready_jobs() -> int:
    """Expire SCRIPT_READY jobs parked longer than the TTL.

    A parked job holds the user's concurrency slot and keeps its recap
    credit captured. Once it has waited longer than
    SCRIPT_READY_TTL_SECONDS (24h) it is marked FAILED, its charged
    credits are refunded exactly once through the existing refund path
    (the persisted credit_refunded flag makes a second run a no-op),
    and its persisted script_ready flag is cleared so it is never
    restored or expired twice. Jobs younger than the TTL — and rows
    without a parking timestamp — are left untouched. Returns how
    many jobs were expired.
    """
    now = datetime.now(timezone.utc)
    candidates: list[tuple[str, int, dict]] = []
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            res = await session.execute(
                select(Job).where(
                    Job.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
                    (Job.job_type == JobType.RECAP.value)
                    | (Job.id.startswith("rc_"))
                    | (Job.engine == "recap"),
                )
            )
            for job in res.scalars().all():
                state = _load_persisted_recap_state(job)
                if state is None or not state.get("script_ready"):
                    continue
                parked_at = _parse_script_ready_at(state.get("script_ready_at"))
                if parked_at is None:
                    continue
                if (now - parked_at).total_seconds() <= SCRIPT_READY_TTL_SECONDS:
                    continue
                candidates.append((str(job.id), int(job.user_id), state))
    except Exception as exc:
        logger.warning(f"Could not scan for abandoned script-ready jobs: {exc}")
        return 0

    expired = 0
    for job_id, user_id, state in candidates:
        live_entry = RECAP_JOBS_REGISTRY.get(job_id)
        refund_entry = {
            "id": job_id,
            "user_id": user_id,
            "credits_charged": int(
                (live_entry or state).get("credits_charged") or 0
            ),
            "credit_deducted": bool(
                (live_entry or state).get("credit_deducted")
            ),
            "credit_refunded": bool(
                (live_entry or state).get("credit_refunded")
            ),
            "credits_refunded": int(
                (live_entry or state).get("credits_refunded") or 0
            ),
        }
        # Refund first (idempotent via the persisted flag): if the
        # process dies after this, the next run sees credit_refunded
        # and only repeats the status write below.
        if live_entry is not None:
            # Refund through the live registry entry itself, so the
            # claim-before-await flag in _refund_recap_job_charge
            # coordinates with a racing runner/cancel refund of the
            # same job (batch M).
            refund_entry = live_entry
            await _refund_recap_job_charge(user_id, refund_entry)
        elif not refund_entry.get("credit_refunded"):
            _amount = int(refund_entry.get("credits_charged") or 0)
            if _amount <= 0 and refund_entry.get("credit_deducted"):
                _amount = 1
            if _amount > 0:
                # No live entry: claim through the persisted blob
                # BEFORE issuing the balance refund (batch M), so a
                # racing refund that persists first is never doubled.
                _claim, _recorded = await _claim_persisted_recap_refund(
                    job_id, _amount
                )
                if _claim == "claimed":
                    # Batch O: refund directly — routing this through
                    # _refund_recap_job_charge would re-claim the blob
                    # this sweep just claimed and read its own claim
                    # as "already refunded". The claim is released if
                    # the balance refund itself fails.
                    if await _refund_recap_credits(user_id, _amount, job_id=job_id):
                        refund_entry["credit_refunded"] = True
                        refund_entry["credits_refunded"] = _amount
                    else:
                        await _release_persisted_recap_refund_claim(job_id)
                elif _claim == "already":
                    refund_entry["credit_refunded"] = True
                    refund_entry["credits_refunded"] = _recorded
        if not refund_entry.get("voice_refunded"):
            _v_bm = (live_entry or state).get("billing_mode")
            _v_ref = await _refund_recap_voice_minutes(
                user_id, job_id, billing_mode=_v_bm, entry=refund_entry
            )
            if _v_ref > 0 and live_entry is not None:
                live_entry["voice_refunded"] = True
                live_entry["voice_seconds_refunded"] = int(
                    refund_entry.get("voice_seconds_refunded") or _v_ref
                )
        try:
            from app.db.session import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                job = await session.get(Job, job_id)
                if job is None:
                    continue
                fresh_state = _load_persisted_recap_state(job)
                if fresh_state is not None:
                    fresh_state["script_ready"] = False
                    fresh_state["script_ready_expired"] = True
                    # Batch AB: terminal state clears the stranded count.
                    fresh_state["stranded_reset_count"] = 0
                    fresh_state["credit_refunded"] = bool(
                        refund_entry.get("credit_refunded")
                    )
                    fresh_state["credits_refunded"] = int(
                        refund_entry.get("credits_refunded") or 0
                    )
                    if refund_entry.get("voice_refunded"):
                        fresh_state["voice_refunded"] = True
                        fresh_state["voice_seconds_refunded"] = int(
                            refund_entry.get("voice_seconds_refunded") or 0
                        )
                    job.recap_config = json.dumps(fresh_state, ensure_ascii=False)
                job.status = JobStatus.FAILED.value
                job.error_message = _SCRIPT_READY_EXPIRED_MESSAGE
                job.finished_at = now
                await session.commit()
        except Exception as exc:
            logger.warning(f"Could not mark script-ready job {job_id} expired: {exc}")
            continue
        if live_entry is not None:
            live_entry["status"] = "FAILED"
            live_entry["error_message"] = _SCRIPT_READY_EXPIRED_MESSAGE
            live_entry["credit_refunded"] = bool(refund_entry.get("credit_refunded"))
            live_entry["credits_refunded"] = int(
                refund_entry.get("credits_refunded") or 0
            )
            if refund_entry.get("voice_refunded"):
                live_entry["voice_refunded"] = True
                live_entry["voice_seconds_refunded"] = int(
                    refund_entry.get("voice_seconds_refunded") or 0
                )
        # Batch K: an abandoned review previously expired silently.
        # The owner now gets the same failure DM the runner sends —
        # curated (localized) reason plus the refund confirmation.
        # Once-only by construction: the persisted script_ready flag
        # flipped above means this job is never a candidate again.
        try:
            _cfg = state.get("config") or {}
            await _send_recap_failure_dm(
                job_id=job_id,
                chat_id=user_id,
                title=_clean_recap_display_title(
                    _cfg.get("title"), _cfg.get("url")
                ),
                reason_key="recap_failure_reason_script_expired",
                credits_refunded=(
                    int(refund_entry.get("credits_refunded") or 0)
                    if refund_entry.get("credit_refunded")
                    else 0
                ),
                entry=live_entry,
            )
        except Exception as dm_err:
            logger.warning(
                f"Could not send expiry DM for recap job {job_id}: {dm_err}"
            )
        expired += 1
        logger.info(f"Recap job {job_id}: script review expired after 24h; closed and refunded.")
    return expired


_STRANDED_RESET_FAILURE_MESSAGE = (
    "The recap render was interrupted by repeated server restarts and "
    "could not be completed. The charged recap credit has been refunded."
)


async def _fail_stranded_recap_job(job_id: str, user_id: int, state: dict) -> bool:
    """Terminally fail a recap job that hit the stranded-reset limit.

    Batch AB: the orphan sweep re-queues a RUNNING row stranded by a
    restart, but a render that reliably kills the instance (e.g. an
    OOM during the overlay pass) would otherwise loop forever —
    restart, re-queue, kill, restart. When the persisted
    ``stranded_reset_count`` reaches the limit the sweep calls this
    instead of re-queueing. Modelled on the no-live-entry branch of
    expire_abandoned_script_ready_jobs: the charge is refunded exactly
    once through the existing persisted-claim path (Batch O option C),
    the row is marked FAILED with the refund outcome and a cleared
    counter written back in the same blob update, the bulky media is
    stripped, and the owner gets the standard failure DM. Once-only by
    construction: after the FAILED write the sweep's RUNNING predicate
    never matches the row again. Best-effort throughout — returns
    False (and leaves the row for a later sweep) on unexpected errors
    instead of raising into the sweep.
    """
    try:
        refunded = bool(state.get("credit_refunded"))
        refunded_amount = (
            int(state.get("credits_refunded") or 0) if refunded else 0
        )
        if not refunded:
            amount = int(state.get("credits_charged") or 0)
            if amount <= 0 and state.get("credit_deducted"):
                amount = 1
            if amount > 0:
                # Claim through the persisted blob BEFORE issuing the
                # balance refund (Batch O), so a racing refund that
                # persists first is never doubled; the claim is
                # released if the balance refund itself fails.
                _claim, _recorded = await _claim_persisted_recap_refund(
                    job_id, amount
                )
                if _claim == "claimed":
                    if await _refund_recap_credits(
                        user_id, amount, job_id=job_id
                    ):
                        refunded = True
                        refunded_amount = amount
                    else:
                        await _release_persisted_recap_refund_claim(job_id)
                elif _claim == "already":
                    refunded = True
                    refunded_amount = int(_recorded or 0)
        voice_refunded_now = 0
        if not state.get("voice_refunded"):
            voice_refunded_now = await _refund_recap_voice_minutes(
                user_id, job_id, billing_mode=state.get("billing_mode")
            )
        try:
            from app.db.session import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                job = await session.get(Job, job_id)
                if job is None:
                    return False
                fresh_state = _load_persisted_recap_state(job)
                if fresh_state is not None:
                    fresh_state["credit_refunded"] = refunded
                    fresh_state["credits_refunded"] = refunded_amount
                    if voice_refunded_now > 0:
                        fresh_state["voice_refunded"] = True
                        fresh_state["voice_seconds_refunded"] = (
                            int(fresh_state.get("voice_seconds_refunded") or 0)
                            + voice_refunded_now
                        )
                    # Terminal state: the counter's job is done.
                    fresh_state["stranded_reset_count"] = 0
                    fresh_state["stranded_stage"] = None
                    job.recap_config = json.dumps(
                        fresh_state, ensure_ascii=False
                    )
                job.status = JobStatus.FAILED.value
                job.error_message = _STRANDED_RESET_FAILURE_MESSAGE
                job.finished_at = utc_now()
                await session.commit()
        except Exception as exc:
            logger.warning(
                f"Could not mark stranded recap job {job_id} failed: {exc}"
            )
            return False
        # Batch W discipline: a failed job's media bulk is waste —
        # strip it now instead of leaving it on the persistent disk.
        try:
            _strip_failed_recap_job_media(
                settings.storage_root / "recap_output" / job_id
            )
        except Exception as strip_err:
            logger.warning(
                f"Recap job {job_id}: stranded-failure media strip failed: {strip_err}"
            )
        # The owner gets the same failure DM the other terminal
        # failure paths send — curated reason plus refund line.
        try:
            _cfg = state.get("config") or {}
            await _send_recap_failure_dm(
                job_id=job_id,
                chat_id=user_id,
                title=_clean_recap_display_title(
                    _cfg.get("title"), _cfg.get("url")
                ),
                reason_key="recap_failure_reason_server_interrupted",
                credits_refunded=refunded_amount if refunded else 0,
            )
        except Exception as dm_err:
            logger.warning(
                f"Could not send stranded-failure DM for recap job {job_id}: {dm_err}"
            )
        logger.info(
            f"Recap job {job_id}: stranded reset limit reached; "
            f"closed as FAILED (refunded={refunded_amount})."
        )
        return True
    except Exception as exc:
        logger.warning(f"Could not fail stranded recap job {job_id}: {exc}")
        return False


def _recap_redispatch_plan(job, user, *, restore_credit_state: bool = True) -> dict:
    """Build the config, registry entry and run kwargs needed to
    (re-)dispatch a persisted recap job — shared by the server queue
    worker (post-restart pickup) and the admin retry endpoint.

    Jobs submitted after the 2026-10-31 persistence fix carry their
    full submit-time config + credit state on the Job row; those are
    restored verbatim. Legacy rows without persisted state fall back
    to the historical reconstruction (URL/title/voice from the row,
    defaults for everything else, no credit state).

    ``restore_credit_state=False`` forces an uncharged entry (kept
    for callers that manage billing themselves). Admin retries pass
    True since Batch AJ H6: retry_queued_job reconciles the persisted
    state with the recap ledger (re-charging a refunded job) BEFORE
    building the plan, so the restored state is the billed truth.
    """
    source_url = getattr(job, "source_audio_path", None) or job.input_text or ""
    # Encoding repair (hunt #4 A1): rows created before the intake
    # conversion may still hold a Zawgyi title — heal them on retry.
    clean_title = _clean_recap_display_title(
        convert_zawgyi_to_unicode(job.input_text or ""), source_url
    ) or "Movie Recap Video"
    state = _load_persisted_recap_state(job)
    if state is not None:
        cfg = RecapConfigRequest(**state["config"])
        is_vip = bool(state.get("is_vip"))
        phase = state.get("phase") or ("script" if cfg.script_review else "full")
        credit_deducted = bool(state.get("credit_deducted")) if restore_credit_state else False
        credits_charged = int(state.get("credits_charged") or 0) if restore_credit_state else 0
        credit_refunded = bool(state.get("credit_refunded")) if restore_credit_state else False
        credits_refunded = int(state.get("credits_refunded") or 0) if restore_credit_state else 0
        credits_estimated = state.get("credits_estimated")
        # Batch AO: the pinned pricing mode + pricing durations are
        # part of the credit state — a re-dispatch must price (and
        # refund) exactly the job that was submitted. Pre-AO blobs
        # lack the keys: durations default to 0 and the mode to None
        # (= no stamp, live-switch fallback).
        source_duration_seconds = float(state.get("source_duration_seconds") or 0) if restore_credit_state else 0.0
        probed_duration_seconds = float(state.get("probed_duration_seconds") or 0) if restore_credit_state else 0.0
        tiered_pricing = state.get("tiered_pricing") if restore_credit_state else None
        # Batch BE: the pinned Max surcharge is part of the credit
        # state too (None = blob predates it; derived at execution).
        max_quality_surcharge = state.get("max_quality_surcharge") if restore_credit_state else None
        # Phase 5 Option B: preserve submit-time billing_mode + voice hold.
        # Legacy blobs lacking billing_mode default safely to credit_only
        # (or admin_free) so in-flight pre-deploy jobs never touch minutes.
        billing_mode = state.get("billing_mode") or (
            _RECAP_BILLING_ADMIN_FREE if not credit_deducted else _RECAP_BILLING_CREDIT_ONLY
        )
        voice_seconds_held = int(state.get("voice_seconds_held") or 0) if restore_credit_state else 0
        voice_seconds_settled = state.get("voice_seconds_settled") if restore_credit_state else None
        voice_settled = bool(state.get("voice_settled")) if restore_credit_state else False
        voice_refunded = bool(state.get("voice_refunded")) if restore_credit_state else False
        voice_seconds_refunded = int(state.get("voice_seconds_refunded") or 0) if restore_credit_state else 0
    else:
        cfg = RecapConfigRequest(
            url=source_url,
            title=clean_title,
            voice_slug=job.preset_voice or "komin",
        )
        is_vip = (
            (user.account_category == AccountCategory.PURCHASED.value) if user else False
        ) or settings.is_admin(job.user_id)
        phase = "full"
        credit_deducted = False
        credits_charged = 0
        credit_refunded = False
        credits_refunded = 0
        credits_estimated = None
        source_duration_seconds = 0.0
        probed_duration_seconds = 0.0
        tiered_pricing = None
        max_quality_surcharge = None
        billing_mode = _RECAP_BILLING_ADMIN_FREE
        voice_seconds_held = 0
        voice_seconds_settled = None
        voice_settled = False
        voice_refunded = False
        voice_seconds_refunded = 0

    entry = {
        "id": job.id,
        "user_id": job.user_id,
        "title": clean_title,
        "status": "QUEUED",
        "progress_percent": 0,
        "progress_step": "0/4",
        "progress_label": t("recap_progress_picking_up", getattr(user, "language_code", None) or "my"),
        "created_at": time.time(),
        "video_path": None,
        "audio_path": None,
        "srt_path": None,
        "script_path": None,
        "download_urls": {},
        "error_message": None,
        "script_review": bool(cfg.script_review),
        "segments_count": None,
        # Batch CB: the ETA shape, restored from the persisted config.
        "tts_engine": cfg.tts_engine,
        "render_quality": cfg.render_quality,
        "split_video": bool(cfg.split_video),
        "parts": [],
        "split_error": None,
        "split_note": None,
        "credit_deducted": credit_deducted,
        "credits_charged": credits_charged,
        "credit_refunded": credit_refunded,
        "credits_refunded": credits_refunded,
        "credits_estimated": credits_estimated,
        "source_duration_seconds": source_duration_seconds,
        "probed_duration_seconds": probed_duration_seconds,
        "tiered_pricing": tiered_pricing,
        "max_quality_surcharge": max_quality_surcharge,
        "billing_mode": billing_mode,
        "voice_seconds_held": voice_seconds_held,
        "voice_seconds_settled": voice_seconds_settled,
        "voice_settled": voice_settled,
        "voice_refunded": voice_refunded,
        "voice_seconds_refunded": voice_seconds_refunded,
        "parts_count": None,
    }
    # Batch DP: a re-dispatched PARKED job resumes past its original
    # 1-hour deadline (dp_resume) and with the owner's recorded voice
    # choice (voice_choice_mode) read from the DB choice-state — the
    # resumer's one source of truth.
    _dp_state = _voice_choice.parse_choice_state(
        getattr(job, "voice_choice_state", None)
    )
    return {
        "cfg": cfg,
        "entry": entry,
        "is_vip": is_vip,
        "credit_deducted": credit_deducted,
        "phase": phase,
        "voice_choice_mode": _voice_choice.choice_mode(_dp_state),
        "dp_resume": _dp_state is not None,
    }


# ── Batch AO: tiered recap pricing (owner decision 2026-11-05) ──────
# Credits = ceil(total source duration / 180) — one credit per started
# 3-minute band — replacing the per-part formula WHEN the
# tiered_recap_pricing_enabled switch is on. The build ships dark:
# with the switch off, every helper below is inert and the charge,
# quote and label behaviour is byte-identical to the legacy per-part
# world. A job's pricing mode is PINNED at submit (stamped on the
# registry entry and the persisted Job-row state), so the price the
# submit flow quoted is the price the worker charges even if the
# switch flips while the job is in flight; entries predating the
# stamp (legacy rows) fall back to the live switch.
#
# Batch BE (arming package, owner decision 2026-11-06 07:28): a
# tier-priced job whose render quality is Max pays ONE extra credit
# on top of the band (owner-approved 2026-11-06 00:28). The surcharge
# is computed once at submit from the AU-clamped request, pinned on
# the entry + persisted state exactly like the band, and included in
# every quote, pre-check, top-up target and refund. With the switch
# off it does not exist: the helpers below are never consulted and
# the legacy totals are byte-identical.

_RECAP_BAND_SECONDS = 180


def _tiered_recap_pricing_enabled() -> bool:
    """The Batch AO arming switch (dynamic setting, default False)."""
    return bool(getattr(settings, "tiered_recap_pricing_enabled", False))


def _recap_band_credits(duration_seconds) -> int:
    """Credits for a source of `duration_seconds` under tiered pricing:
    ceil(duration / 180), minimum 1. Unknown/zero duration defensively
    prices as a single band — the same posture _recap_estimate_part_count
    takes for an unknown duration (1 part)."""
    try:
        duration = float(duration_seconds or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        return 1
    return max(1, math.ceil(duration / _RECAP_BAND_SECONDS))


def _entry_tiered_pricing(entry: dict) -> bool:
    """Whether THIS job is priced by duration bands: the submit-time
    stamp when present, else the live switch (legacy entries)."""
    stamped = entry.get("tiered_pricing")
    if stamped is not None:
        return bool(stamped)
    return _tiered_recap_pricing_enabled()


def _recap_max_quality_surcharge(quality) -> int:
    """Batch BE: the Max render-quality surcharge — 1 extra credit
    when the job's render quality is Max, else 0. Callers pass the
    AU-clamped quality (submit clamps before pricing; execution
    clamps before charging), so a free-tier job can never accrue it.
    Only ever consulted for tier-priced jobs."""
    return 1 if str(quality or "").strip().lower() == "max" else 0


def _entry_max_quality_surcharge(entry: dict) -> int:
    """The Max-quality surcharge pinned on THIS job (0 or 1): the
    submit-time stamp. Entries predating the stamp carry no key and
    price without it here — the execution entry point stamps
    unstamped entries from the job's clamped config before any
    top-up runs (see _execute_recap_background_job)."""
    try:
        return 1 if int(entry.get("max_quality_surcharge") or 0) > 0 else 0
    except (TypeError, ValueError):
        return 0


def _recap_entry_pricing_duration(entry: dict) -> float:
    """The ONE duration a job's band is priced from, in resolution
    order: the submit-resolved duration (client probe verified /
    replaced by the server probe in create_recap_job — the value the
    pre-submit quote was computed from, so quote and charge can never
    disagree), then the worker's own probe of the downloaded source
    (segmented path, stamped on the entry before the top-up). 0 when
    no probe ever produced a value."""
    for key in ("source_duration_seconds", "probed_duration_seconds"):
        try:
            value = float(entry.get(key) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
    return 0.0


async def _top_up_recap_charge_to_total(
    user_id: int,
    entry: dict,
    target_total: int,
    *,
    total_parts: int,
    tiered: bool,
) -> int:
    """Bring a recap job's charge to `target_total` credits — the
    shared core of the segmented per-part top-up (legacy target: the
    part count) and the Batch AO band top-up (target: the duration
    band total). The first credit was already taken at submit; this
    charges only the difference, in ONE ledger action, before any
    part renders. Raises _RecapInsufficientCreditsError — charging
    nothing extra — when the balance cannot cover the difference.
    Returns the job's total charged credits.
    """
    already = int(entry.get("credits_charged") or 0)
    # Batch AJ (H11): the entry/blob total is only a cache. The
    # submit charge commits to the recap ledger in the same
    # transaction as the balance, but the blob persist that records
    # the topped-up total is a separate best-effort write — a restart
    # between them rebuilt the entry with the stale submit-time total
    # and this top-up then charged the difference a SECOND time. The
    # ledger is the durable truth: never charge for parts the ledger
    # says are already paid for (net of refunds).
    _job_id = entry.get("id")
    if _job_id:
        try:
            from app.db.session import AsyncSessionLocal
            from app.services.ledger import (
                recap_debit_total_for_job,
                recap_refund_total_for_job,
            )

            async with AsyncSessionLocal() as _ledger_session:
                _net_charged = await recap_debit_total_for_job(
                    _ledger_session, _job_id
                ) - await recap_refund_total_for_job(_ledger_session, _job_id)
            already = max(already, int(_net_charged))
        except Exception as exc:
            logger.warning(
                f"Could not reconcile recap charge total from the ledger for {_job_id}: {exc}"
            )
    extra = int(target_total) - already
    if extra > 0 and not await _charge_recap_credits(user_id, extra, job_id=entry.get("id")):
        if tiered:
            raise _RecapInsufficientCreditsError(
                total_parts, total_credits=int(target_total)
            )
        raise _RecapInsufficientCreditsError(total_parts)
    entry["credits_charged"] = max(int(target_total), already)
    entry["credit_deducted"] = True
    # Keep the Job row's persisted total in step, so a restart after
    # this top-up restores the real total (not the submit-time 1) and
    # can neither double-charge nor lose track of the refund amount.
    await _persist_recap_credits_charged(entry)
    return int(entry["credits_charged"])


async def _top_up_recap_segment_charge(user_id: int, entry: dict, total_parts: int) -> int:
    """Bring a segmented recap job's charge to its exact total.

    Legacy (switch off / job not tier-stamped): 1 credit PER PART
    (owner decision 2026-10-30) — the target is the part count.
    Tiered (Batch AO): the target is the duration-band total over the
    job's TOTAL source duration — the band replaces the per-part
    formula, so the split size a user picked can no longer change
    the price. Batch BE: a Max-quality tiered job's target also
    carries the pinned +1 surcharge. Either way the charge lands
    before any part renders. Returns the job's total charged credits.
    """
    tiered = _entry_tiered_pricing(entry)
    target = int(total_parts)
    if tiered:
        entry["tiered_pricing"] = True
        duration = _recap_entry_pricing_duration(entry)
        if duration > 0:
            target = _recap_band_credits(duration) + _entry_max_quality_surcharge(entry)
        # else: no probe ever produced a duration — keep the legacy
        # part-count target; an unpriceable job is never charged LESS
        # than the legacy formula would have charged it.
    total = await _top_up_recap_charge_to_total(
        user_id, entry, target, total_parts=int(total_parts), tiered=tiered
    )
    entry["parts_count"] = int(total_parts)
    # The core persisted the credit state before parts_count was set;
    # parts_count is registry/display state (the status endpoint reads
    # the live entry), so no second persist is needed for it.
    return total


async def _apply_recap_band_top_up(user_id: int, entry: dict, *, is_admin_user: bool) -> int:
    """Batch AO: bring a NON-segmented recap job's charge to its
    duration-band total before it renders. The segmented path prices
    itself in _top_up_recap_segment_charge; this is the same top-up
    for the single-job path (and the script-review render phase),
    which the legacy world never topped up at all. Inert unless the
    job is tier-priced: switch off, admin jobs and unpaid entries
    return immediately, and a band of 1 (any source ≤3 min — the free
    lane's ≤2 min included) never reaches the charge helper on its
    own. Batch BE: the target includes the pinned Max-quality
    surcharge, so a ≤3-minute Max job (band 1 + 1) DOES top up by
    the one surcharge credit.
    """
    if is_admin_user or not entry.get("credit_deducted"):
        return int(entry.get("credits_charged") or 0)
    if not _entry_tiered_pricing(entry):
        return int(entry.get("credits_charged") or 0)
    target = _recap_band_credits(
        _recap_entry_pricing_duration(entry)
    ) + _entry_max_quality_surcharge(entry)
    if target <= int(entry.get("credits_charged") or 0):
        return int(entry.get("credits_charged") or 0)
    entry["tiered_pricing"] = True
    return await _top_up_recap_charge_to_total(
        user_id, entry, target, total_parts=0, tiered=True
    )


async def _refund_recap_job_charge(user_id: int, entry: dict) -> bool:
    """Refund whatever a recap job was actually charged — both Recap Credits
    and, for Option B clone jobs (``billing_mode == 'credit_plus_voice_minutes'``),
    any held Voice Clone Minutes — exactly once per job.
    """
    _job_id = entry.get("id")
    _billing_mode = entry.get("billing_mode")
    _voice_refunded_now = 0
    if (
        _job_id
        and _billing_mode == _RECAP_BILLING_CREDIT_PLUS_VOICE
        and not entry.get("voice_refunded")
    ):
        try:
            _voice_refunded_now = await _refund_recap_voice_minutes(
                user_id, _job_id, billing_mode=_billing_mode, entry=entry
            )
        except Exception as _vexc:
            logger.warning(
                f"Voice-minute refund failed for recap job {_job_id}: {_vexc}"
            )
    if entry.get("credit_refunded"):
        if _voice_refunded_now > 0:
            await _persist_recap_credits_charged(entry)
        return _voice_refunded_now > 0
    amount = int(entry.get("credits_charged") or 0)
    if amount <= 0 and entry.get("credit_deducted"):
        amount = 1
    entry["credit_refunded"] = True
    entry["credits_refunded"] = amount
    if _job_id:
        try:
            from app.db.session import AsyncSessionLocal
            from app.services.ledger import (
                recap_debit_total_for_job,
                recap_refund_total_for_job,
            )

            async with AsyncSessionLocal() as _ledger_session:
                _net_charged = await recap_debit_total_for_job(
                    _ledger_session, _job_id
                ) - await recap_refund_total_for_job(_ledger_session, _job_id)
            amount = max(amount, int(_net_charged))
            entry["credits_refunded"] = amount
        except Exception as exc:
            logger.warning(
                f"Could not reconcile recap refund total from the ledger for {_job_id}: {exc}"
            )
    if amount <= 0:
        entry["credit_refunded"] = False
        entry["credits_refunded"] = 0
        if _voice_refunded_now > 0:
            await _persist_recap_credits_charged(entry)
        return _voice_refunded_now > 0
    _claim_state = "error"
    if _job_id:
        _claim_state, _recorded = await _claim_persisted_recap_refund(_job_id, amount)
        if _claim_state == "already":
            entry["credits_refunded"] = int(_recorded or amount)
            if _voice_refunded_now > 0:
                await _persist_recap_credits_charged(entry)
            return _voice_refunded_now > 0
    if await _refund_recap_credits(user_id, amount, job_id=_job_id):
        await _persist_recap_credits_charged(entry)
        return True
    entry["credit_refunded"] = False
    entry["credits_refunded"] = 0
    if _claim_state == "claimed":
        await _release_persisted_recap_refund_claim(_job_id)
    if _voice_refunded_now > 0:
        await _persist_recap_credits_charged(entry)
    return _voice_refunded_now > 0


_CANCELLABLE_JOB_STATUSES = (
    JobStatus.QUEUED.value,
    JobStatus.RUNNING.value,
    JobStatus.WAITING_CHOICE.value,
)


async def _claim_job_cancelled(
    db: AsyncSession,
    job: Job,
    *,
    db_error_message: str,
    recap_config_json: Optional[str] = None,
    conflict_status_code: int = 409,
    conflict_detail: str = "Job is no longer active and cannot be cancelled.",
) -> None:
    """Atomically flip an active job to CANCELLED before refunding.

    Guarding on ``Job.status IN (QUEUED, RUNNING, WAITING_CHOICE)`` prevents a
    late or racing cancel from overwriting a just-committed SUCCESS/FAILED/CANCELLED
    row and prevents concurrent cancels from refunding twice.
    """
    _now = utc_now()
    _values: dict[str, Any] = {
        "status": JobStatus.CANCELLED.value,
        "finished_at": _now,
        "error_message": db_error_message,
    }
    if recap_config_json is not None:
        _values["recap_config"] = recap_config_json
    _upd = await db.execute(
        update(Job)
        .where(Job.id == job.id, Job.status.in_(_CANCELLABLE_JOB_STATUSES))
        .values(**_values)
        .execution_options(synchronize_session=False)
    )
    if not _upd.rowcount:
        await db.rollback()
        raise HTTPException(status_code=conflict_status_code, detail=conflict_detail)
    await db.commit()
    await db.refresh(job)


async def _cancel_recap_job_core(
    db: AsyncSession,
    job: Job,
    *,
    db_error_message: str,
    registry_error_message: str,
    conflict_status_code: int = 409,
    conflict_detail: str = "လုပ်ဆောင်နေဆဲ Recap အလုပ်ကိုသာ ရပ်တန့်နိုင်ပါတယ်။",
) -> dict:
    """Cancel a recap job and refund its charge — the ONE machinery.

    Shared by the admin console endpoint and the Mini App user cancel
    (batch L) so both surfaces have identical semantics (2026-10-31
    admin-cancel rules): the Job row atomically flips to CANCELLED first
    (guarded against overwriting SUCCESS/FAILED/CANCELLED), the registry
    entry gets the FAILED + "Cancelled…" marker those same re-checks
    recognise, and the refund goes through _refund_recap_job_charge when
    a registry entry exists, or the persisted-state fallback when a
    restart lost the entry (proven charge only; an already-refunded blob
    is never refunded twice). Admin-owned jobs were never charged, so
    their refunds are no-ops. Returns the refund_info dict the admin
    endpoint has always reported.
    """
    refund_info: dict = {"refunded": False}

    # Batch N: a parked (SCRIPT_READY) job's persisted script_ready
    # flag is cleared in the SAME commit as the status flip below.
    _persisted_state = _load_persisted_recap_state(job)
    _next_recap_config: Optional[str] = None
    if _persisted_state is not None and (
        _persisted_state.get("script_ready")
        or _persisted_state.get("stranded_reset_count")
    ):
        _persisted_state["script_ready"] = False
        # Batch AB: cancel is terminal — clear the stranded count in
        # the same blob write.
        _persisted_state["stranded_reset_count"] = 0
        _next_recap_config = json.dumps(_persisted_state, ensure_ascii=False)

    await _claim_job_cancelled(
        db,
        job,
        db_error_message=db_error_message,
        recap_config_json=_next_recap_config,
        conflict_status_code=conflict_status_code,
        conflict_detail=conflict_detail,
    )

    try:
        entry = RECAP_JOBS_REGISTRY.get(job.id)
        if entry is not None:
            if job.id in RECAP_JOBS_REGISTRY:
                RECAP_JOBS_REGISTRY[job.id]["status"] = "FAILED"
                RECAP_JOBS_REGISTRY[job.id]["error_message"] = registry_error_message
            if await _refund_recap_job_charge(job.user_id, entry):
                refund_info = {
                    "refunded": True,
                    "recap_credits": int(entry.get("credits_refunded") or 0),
                    "voice_refunded": bool(entry.get("voice_refunded")),
                    "voice_seconds_refunded": int(entry.get("voice_seconds_refunded") or 0),
                }
        else:
            persisted = _load_persisted_recap_state(job)
            proven_credits = 1
            already_refunded = False
            _p_billing_mode = None
            if persisted is not None:
                already_refunded = bool(persisted.get("credit_refunded"))
                _p_billing_mode = persisted.get("billing_mode")
                if persisted.get("credit_deducted"):
                    proven_credits = max(1, int(persisted.get("credits_charged") or 1))
            _v_ref_sec = 0
            if not settings.is_admin(job.user_id):
                _v_ref_sec = await _refund_recap_voice_minutes(
                    job.user_id, job.id, billing_mode=_p_billing_mode
                )
            if already_refunded:
                refund_info = {
                    "refunded": True,
                    "recap_credits": int(persisted.get("credits_refunded") or proven_credits),
                    "voice_refunded": bool(_v_ref_sec > 0 or persisted.get("voice_refunded")),
                    "voice_seconds_refunded": int(_v_ref_sec or persisted.get("voice_seconds_refunded") or 0),
                    "note": "charge already refunded before the restart (persisted state)",
                }
            elif not settings.is_admin(job.user_id):
                _did_refund = False
                if persisted is not None:
                    _claim, _recorded = await _claim_persisted_recap_refund(
                        job.id, proven_credits
                    )
                    if _claim == "already":
                        refund_info = {
                            "refunded": True,
                            "recap_credits": int(_recorded or proven_credits),
                            "voice_refunded": bool(_v_ref_sec > 0 or persisted.get("voice_refunded")),
                            "voice_seconds_refunded": int(_v_ref_sec or persisted.get("voice_seconds_refunded") or 0),
                            "note": "charge already refunded before the restart (persisted state)",
                        }
                    elif _claim == "claimed":
                        if await _refund_recap_credits(job.user_id, proven_credits, job_id=job.id):
                            _did_refund = True
                        else:
                            await _release_persisted_recap_refund_claim(job.id)
                    else:
                        _did_refund = await _refund_recap_credits(
                            job.user_id, proven_credits, job_id=job.id
                        )
                else:
                    _did_refund = await _refund_recap_credits(
                        job.user_id, proven_credits, job_id=job.id
                    )
                if _did_refund:
                    refund_info = {
                        "refunded": True,
                        "recap_credits": proven_credits,
                        "voice_refunded": bool(_v_ref_sec > 0),
                        "voice_seconds_refunded": int(_v_ref_sec),
                        "note": (
                            "registry unavailable; amount restored from persisted recap state"
                            if persisted is not None
                            else "base credit only (registry unavailable)"
                        ),
                    }
                elif _v_ref_sec > 0:
                    refund_info = {
                        "refunded": True,
                        "recap_credits": 0,
                        "voice_refunded": True,
                        "voice_seconds_refunded": int(_v_ref_sec),
                    }
    except Exception as exc:
        logger.warning(f"Cancel refund failed for job {job.id}: {exc}")
        refund_info = {"refunded": False, "error": "refund failed — check the user's ledger manually"}
    return refund_info


def _voice_job_error_status(reason: str) -> int:
    return {
        "MODEL_NOT_AUTHORIZED": 404,
    "LEGACY_MODEL_ENGINE_REMOVED": 400,
        "CONCURRENT_JOB_LIMIT_EXCEEDED": 429,
        "DAILY_JOB_LIMIT_EXCEEDED": 429,
        "DAILY_MINUTE_LIMIT_EXCEEDED": 429,
        "INSUFFICIENT_BALANCE": 403,
    }.get(reason, 400)


def _voice_job_error_detail(reason: str, user: User | None = None) -> str:
    if reason == "DAILY_MINUTE_LIMIT_EXCEEDED":
        # Batch AN: name the caller's OWN tier limit (the endpoints
        # pass their authenticated user; without one, quote the FREE
        # tier, which is the default account category).
        _limit = (
            settings.voice_daily_limit_minutes_paid
            if user is not None and user.account_category == AccountCategory.PURCHASED.value
            else settings.voice_daily_limit_minutes_free
        )
        return (
            f"ယနေ့အတွက် အသံ အသုံးချနိုင်သော မိနစ် ({int(_limit)} မိနစ်) ပြည့်သွားပါပြီ။ "
            "မနက်ဖြန် ပြန်ကြိုးစားပါ။"
        )
    return {
        "MODEL_NOT_AUTHORIZED": "အသံမော်ဒယ် မတွေ့ရှိပါ",
    "LEGACY_MODEL_ENGINE_REMOVED": "ဒီအသံ မော်ဒယ် အဟောင်းကို မသုံးနိုင်တော့ပါ။ VoxCPM2 နဲ့ ကိုးကားအသံ (၃–၃၀ စက္ကန့်) အသစ် ပို့ပေးပါ။",
        "CONCURRENT_JOB_LIMIT_EXCEEDED": "တစ်ပြိုင်တည်း လုပ်ဆောင်နေသော အလုပ်များနေပါသည်။ ခဏစောင့်ပြီးမှ ပြန်ကြိုးစားပါ။",
        "DAILY_JOB_LIMIT_EXCEEDED": "ယနေ့ အခမဲ့ ကန့်သတ်ချက် ပြည့်သွားပါပြီ။",
        "INSUFFICIENT_BALANCE": "လက်ကျန်မိနစ် မလုံလောက်ပါ။",
        "TEXT_EXCEEDS_CHARACTER_LIMIT": "စာသား စာလုံးရေ ကန့်သတ်ချက်ထက် ကျော်လွန်နေပါသည်။",
        "TEXT_EXCEEDS_INFRASTRUCTURE_LIMIT": "စာသား စာလုံးရေ ကန့်သတ်ချက်ထက် ကျော်လွန်နေပါသည်။",
        "AUDIO_FILE_NOT_FOUND": "အသံဖိုင် မတွေ့ရှိပါ။",
    }.get(reason, "အလုပ်ဖန်တီးရာတွင် အမှားဖြစ်ပေါ်ပါသည်။")


_INVALID_PRESET_VOICE_DETAIL = "ရွေးထားတဲ့ အသံ မမှန်ကန်ပါ။ ကျေးဇူးပြု၍ အသံ ပြန်ရွေးပေးပါခင်ဗျာ။"


def _require_valid_preset_voice(preset_voice: Optional[str]) -> None:
    """Allowlist for the client-supplied preset_voice (XSS fix, hunt #2 X1).

    preset_voice is stored verbatim on the Job row and later rendered
    in the admin Mini App, so an arbitrary string here is a stored-XSS
    vector (a JS-string breakout payload survived HTML escaping in the
    old admin onclick handlers). Downstream — queue, worker, engines —
    the value is ONLY ever resolved through get_curated_voice(), so the
    curated persona slugs are the complete legitimate set. Anything
    else is rejected here instead of being stored with a silent
    fallback voice.

    The same invariant is enforced inside the queue service itself
    (hunt #3 E4), which raises ValueError; the endpoints translate
    that into this same clean 400 so behaviour is identical whichever
    layer trips first.
    """
    if preset_voice is not None and get_curated_voice(preset_voice) is None:
        raise HTTPException(
            status_code=400,
            detail=_INVALID_PRESET_VOICE_DETAIL,
        )


# Admin-editable tutorial links beyond the original video/voice/recap
# slots: settings key -> registry default (channel post). Every tutorial
# is editable from Admin → Settings; these ride the same dynamic-settings
# store and the same /api/user/me payload as the original three.
TUTORIAL_EXTRA_LINKS: dict[str, str] = {
    "tutorial_index_link": "https://t.me/VoiceClonePro/38",
    "tutorial_plans_link": "https://t.me/VoiceClonePro/31",
    "tutorial_customvoice_link": "https://t.me/VoiceClonePro/32",
    "tutorial_downloader_link": "https://t.me/VoiceClonePro/33",
    "tutorial_scriptedit_link": "https://t.me/VoiceClonePro/34",
    "tutorial_recapadvanced_link": "https://t.me/VoiceClonePro/35",
    "tutorial_refer_link": "https://t.me/VoiceClonePro/36",
    "tutorial_support_link": "https://t.me/VoiceClonePro/37",
}


# ── 1. User & Wallet Profile ───────────────────────────────────────────────
async def _ensure_referral_code(db: AsyncSession, user: User) -> str:
    """Return the user's referral code, lazily creating one if missing.

    Codes are assigned at user creation in the bot flow
    (ledger.get_or_create_user), but the WebApp auth path creates User
    rows directly (get_current_user) and legacy rows may predate the
    column — so /me heals a NULL code on first read using the same
    collision-safe generator the bot uses.
    """
    code = getattr(user, "referral_code", None)
    if code:
        return code
    from app.services.ledger import _find_available_referral_code
    code = await _find_available_referral_code(db, user.id)
    user.referral_code = code
    await db.commit()
    return code


@api_router.get("/user/me")
async def get_user_profile(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """Retrieve user's VIP status, minute balance, allowance, and platform config."""
    from app.services.dynamic_settings import get_dynamic_setting
    # Monthly free allowance: grant it here too (the bot /start flow
    # and the voice submit path already do) so a Mini-App-first or
    # new-month user sees their REAL free balance — the configured
    # monthly grant — in the header chips, the Plans stats and the
    # voice preflight, instead of a false "0 minutes — buy VIP"
    # state (journey audit 2026-10-31). The grant is idempotent per
    # calendar month, and a grant failure must never break the
    # profile read.
    try:
        from app.services.ledger import grant_monthly_free_allowance

        _granted, _bal = await grant_monthly_free_allowance(db, user.id)
        if _granted:
            await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
    bal = await db.get(MinuteBalance, user.id)
    # Batch AW: the headline balance is the CLONE-SPENDABLE figure —
    # referral seconds are TTS-only since the re-scope and are exposed
    # separately as tts_balance_seconds (the TTS tab's balance), never
    # folded into the voice-clone totals.
    seconds_left = bal.clone_spendable_seconds if bal else 0
    tts_seconds_left = int(bal.referral_seconds or 0) if bal else 0
    is_admin = settings.is_admin(user.id)

    # Batch BZ: today's TTS picture for the header 🎧 chip and the
    # Home "today's free" card — computed through the SAME helpers the
    # TTS submit path enforces with (tts_daily_limit_minutes +
    # tts_cap_consumed_seconds), so the displayed number can never
    # disagree with enforcement. remaining = referral + (cap −
    # consumed): referral seconds are spent before the cap is touched.
    # Admins are exempt from the cap → nulls (the UI paints ∞), the
    # same treatment the header gives admin balances. A quota failure
    # must never break the profile read.
    tts_daily_limit_seconds = None
    tts_today_consumed_seconds = None
    tts_today_remaining_seconds = None
    if not is_admin:
        try:
            from app.services.tts_service import (
                tts_cap_consumed_seconds,
                tts_daily_limit_minutes,
            )

            _tts_limit_min = tts_daily_limit_minutes(user, is_admin=False)
            _tts_consumed = await tts_cap_consumed_seconds(db, user.id)
            tts_daily_limit_seconds = int(_tts_limit_min) * 60
            tts_today_consumed_seconds = round(float(_tts_consumed), 1)
            if _tts_limit_min > 0:
                tts_today_remaining_seconds = round(
                    tts_seconds_left
                    + max(0.0, _tts_limit_min * 60 - float(_tts_consumed)),
                    1,
                )
            else:
                # Cap 0 = unlimited daily allowance on top of referral.
                tts_today_remaining_seconds = None
        except Exception:
            tts_daily_limit_seconds = None
            tts_today_consumed_seconds = None
            tts_today_remaining_seconds = None
    is_vip = (user.account_category == AccountCategory.PURCHASED.value) or is_admin

    char_limit = (
        settings.character_limit_purchased
        if is_vip
        else settings.character_limit_free
    )

    tutorial_link = await get_dynamic_setting(
        db,
        "tutorial_video_link",
        default=getattr(settings, "tutorial_video_link", "https://t.me/VoiceClonePro/28"),
    )
    tutorial_enabled = await get_dynamic_setting(
        db,
        "tutorial_video_enabled",
        default="true",
    )
    tut_voice_title = await get_dynamic_setting(
        db,
        "tutorial_voice_title",
        default=getattr(settings, "tutorial_voice_title", "Voice Clone ပြုလုပ်နည်း လမ်းညွှန်"),
    )
    tut_voice_link = await get_dynamic_setting(
        db,
        "tutorial_voice_link",
        default=tutorial_link,
    )
    tut_voice_enabled = await get_dynamic_setting(
        db,
        "tutorial_voice_enabled",
        default="true",
    )
    tut_recap_title = await get_dynamic_setting(
        db,
        "tutorial_recap_title",
        default=getattr(settings, "tutorial_recap_title", "Movie Recap ပြုလုပ်နည်း လမ်းညွှန်"),
    )
    tut_recap_link = await get_dynamic_setting(
        db,
        "tutorial_recap_link",
        default=tutorial_link,
    )
    tut_recap_enabled = await get_dynamic_setting(
        db,
        "tutorial_recap_enabled",
        default="true",
    )
    # Batch L: the recap demo sample reference (Telegram file_id or
    # URL) — empty means the recap tab's demo slot stays hidden.
    recap_demo_asset = await get_dynamic_setting(
        db,
        "recap_demo_asset",
        default=getattr(settings, "recap_demo_asset", ""),
    )
    tut_extra_links = {
        key: await get_dynamic_setting(
            db,
            key,
            default=getattr(settings, key, default),
        )
        for key, default in TUTORIAL_EXTRA_LINKS.items()
    }

    # Completed-job count: the Home welcome strip's first-run gate
    # (Free plan + zero completed jobs) reads this from the profile.
    completed_jobs = None
    try:
        from sqlalchemy import func as _func

        _cj = await db.execute(
            select(_func.count(Job.id)).where(
                Job.user_id == user.id,
                Job.status == JobStatus.SUCCESS.value,
            )
        )
        completed_jobs = int(_cj.scalar() or 0)
    except Exception:
        completed_jobs = None

    # Check if user has an active or recently finished Recap/Voice job so Mini App can auto-resume
    active_recap_job_id = None
    active_voice_job_id = None
    try:
        recent_cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        stmt_active = (
            select(Job)
            .where(
                Job.user_id == user.id,
                or_(
                    Job.status == JobStatus.WAITING_CHOICE.value,
                    (
                        (Job.created_at >= recent_cutoff)
                        & Job.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value])
                    ),
                ),
            )
            .order_by(Job.created_at.desc())
            .limit(5)
        )
        res_act = await db.execute(stmt_active)
        for aj in res_act.scalars().all():
            is_rc = (aj.job_type == JobType.RECAP.value or aj.id.startswith("rc_") or aj.engine == "recap")
            if is_rc and not active_recap_job_id:
                active_recap_job_id = aj.id
            elif not is_rc and not active_voice_job_id:
                active_voice_job_id = aj.id
    except Exception:
        pass

    # Round-6 P2: Refer & Support exposure. The referral programme is
    # complete in the bot (ledger.py: attribution, 120s TTS-only rewards capped
    # at 20, get_referral_stats) but the Mini App never exposed it —
    # users could only find a tutorial post. Ride the fields on /me so
    # the Home card renders with zero extra fetches. A referral failure
    # must never break the profile read.
    referral_code = ""
    referral_stats = None
    try:
        referral_code = await _ensure_referral_code(db, user)
        from app.services.ledger import get_referral_stats
        referral_stats = await get_referral_stats(db, user.id)
    except Exception:
        referral_code = referral_code or ""
        referral_stats = None

    return {
        "id": user.id,
        "first_name": user.first_name,
        "username": user.username,
        "language_code": user.language_code or "my",
        "account_category": "PURCHASED" if is_vip else user.account_category,
        "is_admin": is_admin,
        "is_vip": is_vip,
        "balance_seconds": seconds_left,
        "balance_minutes": round(seconds_left / 60, 1),
        "tts_balance_seconds": tts_seconds_left,
        "tts_balance_minutes": round(tts_seconds_left / 60, 1),
        # Batch BZ: today's TTS spendable picture (see above). Null
        # for admins (UI paints ∞) and when the tier cap is 0.
        "tts_daily_limit_seconds": tts_daily_limit_seconds,
        "tts_today_consumed_seconds": tts_today_consumed_seconds,
        "tts_referral_seconds": (None if is_admin else tts_seconds_left),
        "tts_today_remaining_seconds": tts_today_remaining_seconds,
        "recap_credits": getattr(user, "recap_credits", 3),
        "character_limit": char_limit,
        # R40 deck I4: the plan "how it works" copy states BOTH limits
        # via placeholders — expose the pair so the client never
        # hardcodes them.
        "character_limit_free": int(getattr(settings, "character_limit_free", 1000) or 1000),
        "character_limit_purchased": int(getattr(settings, "character_limit_purchased", 5000) or 5000),
        "recap_max_duration_seconds": 600 if is_vip else 120,
        "recap_max_duration_minutes": 10 if is_vip else 2,
        # Batch AO: the Mini App's recap quote display follows the
        # tiered-pricing switch (off = the legacy per-part estimate).
        "tiered_pricing_enabled": _tiered_recap_pricing_enabled(),
        "has_recap_watermark": bool(get_watermark_config(is_vip)["watermark"]),
        "watermark_config": get_watermark_config(is_vip),
        # Custom channel logo (owner decision 2026-11-02): paid-only,
        # the user's own upload — see /api/user/logo endpoints.
        "can_use_logo": bool(is_vip),
        "has_custom_logo": _find_user_logo(user.id) is not None,
        "active_recap_job_id": active_recap_job_id,
        "active_voice_job_id": active_voice_job_id,
        "completed_jobs": completed_jobs,
        "referral_code": referral_code,
        "referral_link": (
            f"https://t.me/voiceclonepro_bot?start={referral_code}"
            if referral_code else ""
        ),
        "referral": referral_stats,
        "support_telegram": "@VoiceCloneProAdmin",
        "support_link": "https://t.me/VoiceCloneProAdmin",
        "tutorial_video_link": tutorial_link,
        "tutorial_video_enabled": str(tutorial_enabled).lower() != "false",
        "tutorial_voice_title": tut_voice_title,
        "tutorial_voice_link": tut_voice_link,
        "tutorial_voice_enabled": str(tut_voice_enabled).lower() != "false",
        "tutorial_recap_title": tut_recap_title,
        "tutorial_recap_link": tut_recap_link,
        "tutorial_recap_enabled": str(tut_recap_enabled).lower() != "false",
        "recap_demo_asset": recap_demo_asset or "",
        **tut_extra_links,
        # Gate status for the Mini App join-and-verify UX (2026-10-29).
        # This is the CACHED flag state only; a live check happens on
        # gated writes and via POST /api/user/verify-membership. Admins
        # are always treated as membership-verified (see gate.py).
        "is_banned": bool((user.status or "") == UserStatus.BLOCKED.value and not is_admin),
        "membership_verified": bool(is_admin or (user.channel_verified and user.group_verified)),
        "channel_verified": bool(user.channel_verified),
        "group_verified": bool(user.group_verified),
        "gate_configured": bool(settings.required_channel_id and settings.required_group_id),
        "channel_invite_link": settings.channel_invite_link,
        "group_invite_link": settings.group_invite_link,
    }


@api_router.post("/user/verify-membership")
async def verify_membership_endpoint(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """Live re-check of channel/group membership for the Mini App.

    Mirrors bot /start -> gate:verify: the user joins via the invite
    links from /api/user/me, then calls this endpoint. Unlike the gated
    write endpoints it never raises 403 for a non-member; it returns
    the verified state so the frontend can keep showing the join UI.
    Admins always verify (admin bypass in gate.py).
    """
    from app.services.gate import is_gate_configured, verify_user_membership

    if not is_gate_configured() or not settings.bot_token:
        return {
            "verified": False,
            "channel_verified": bool(user.channel_verified),
            "group_verified": bool(user.group_verified),
            "gate_configured": False,
            "channel_invite_link": settings.channel_invite_link,
            "group_invite_link": settings.group_invite_link,
        }
    from aiogram import Bot

    bot = Bot(token=settings.bot_token)
    try:
        is_verified, _reason = await verify_user_membership(db, user, bot)
    finally:
        try:
            await bot.session.close()
        except Exception:
            pass
    try:
        await db.commit()
    except Exception:
        pass
    return {
        "verified": bool(is_verified),
        "channel_verified": bool(user.channel_verified),
        "group_verified": bool(user.group_verified),
        "gate_configured": True,
        "channel_invite_link": settings.channel_invite_link,
        "group_invite_link": settings.group_invite_link,
    }


@api_router.get("/config")
async def get_public_app_config(db: AsyncSession = Depends(get_db_session)):
    """Retrieve public platform config (tutorial link, channel/group invites, maintenance)."""
    from app.services.dynamic_settings import get_dynamic_setting
    tutorial_link = await get_dynamic_setting(
        db,
        "tutorial_video_link",
        default=getattr(settings, "tutorial_video_link", "https://t.me/VoiceClonePro/28"),
    )
    tutorial_enabled = await get_dynamic_setting(
        db,
        "tutorial_video_enabled",
        default="true",
    )
    tut_voice_title = await get_dynamic_setting(
        db,
        "tutorial_voice_title",
        default=getattr(settings, "tutorial_voice_title", "Voice Clone ပြုလုပ်နည်း လမ်းညွှန်"),
    )
    tut_voice_link = await get_dynamic_setting(
        db,
        "tutorial_voice_link",
        default=tutorial_link,
    )
    tut_voice_enabled = await get_dynamic_setting(
        db,
        "tutorial_voice_enabled",
        default="true",
    )
    tut_recap_title = await get_dynamic_setting(
        db,
        "tutorial_recap_title",
        default=getattr(settings, "tutorial_recap_title", "Movie Recap ပြုလုပ်နည်း လမ်းညွှန်"),
    )
    tut_recap_link = await get_dynamic_setting(
        db,
        "tutorial_recap_link",
        default=tutorial_link,
    )
    tut_recap_enabled = await get_dynamic_setting(
        db,
        "tutorial_recap_enabled",
        default="true",
    )
    # Batch L: recap demo sample reference (see /api/user/me).
    recap_demo_asset = await get_dynamic_setting(
        db,
        "recap_demo_asset",
        default=getattr(settings, "recap_demo_asset", ""),
    )
    maintenance_raw = await get_dynamic_setting(
        db,
        "maintenance_mode",
        default=str(getattr(settings, "maintenance_mode", False)).lower(),
    )
    # Batch AR: the entry gate shows the admin's maintenance_note
    # verbatim (it is public-facing copy by design — the Group AI
    # already quotes it to members, Batch Z), so the unauthenticated
    # config payload carries it next to the flag itself.
    maintenance_note_raw = await get_dynamic_setting(
        db,
        "maintenance_note",
        default=str(getattr(settings, "maintenance_note", "") or ""),
    )
    return {
        "tutorial_video_link": tutorial_link,
        "tutorial_video_enabled": str(tutorial_enabled).lower() != "false",
        "tutorial_voice_title": tut_voice_title,
        "tutorial_voice_link": tut_voice_link,
        "tutorial_voice_enabled": str(tut_voice_enabled).lower() != "false",
        "tutorial_recap_title": tut_recap_title,
        "tutorial_recap_link": tut_recap_link,
        "tutorial_recap_enabled": str(tut_recap_enabled).lower() != "false",
        "recap_demo_asset": recap_demo_asset or "",
        "channel_invite_link": getattr(settings, "channel_invite_link", "https://t.me/VoiceCloneProChannel"),
        "group_invite_link": getattr(settings, "group_invite_link", "https://t.me/VoiceCloneProGroup"),
        # So the Mini App can show its maintenance state instead of
        # letting writes fail one by one (hunt C1).
        "maintenance_mode": str(maintenance_raw).lower() in ("true", "1", "yes"),
        # Batch AR: the note the full-screen maintenance gate renders.
        "maintenance_note": str(maintenance_note_raw or ""),
    }


class LanguageUpdateRequest(BaseModel):
    language: str = Field(..., pattern="^(my|en)$")


@api_router.post("/user/language")
async def update_user_language(
    req: LanguageUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """Persist user's selected language preference (my / en)."""
    user.language_code = req.language
    await db.commit()
    return {"success": True, "language": user.language_code}


# ── 1b. Custom channel logo (paid feature, owner decision 2026-11-02) ─────
@api_router.get("/user/logo")
async def get_user_logo_state(
    user: User = Depends(get_current_user),
    _gate: User = Depends(require_gate_verified),
):
    """Logo state for the Recap settings UI: whether this user may use
    the feature at all (paid/admin) and whether a logo is stored."""
    return _logo_state_payload(user)


@api_router.get("/user/logo/file")
async def get_user_logo_file(
    user: Optional[User] = Depends(get_optional_user),
    t: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db_session),
):
    """Serve the caller's OWN stored logo (settings preview thumbnail).

    Auth mirrors the recap stream endpoint: the initData header OR a
    signed token scoped to ("userlogo", user, "logo") — an <img> tag
    cannot send headers. The file served is always the resolved
    user's own logo; a token never widens access beyond its owner.
    """
    if user is None and t:
        token_uid = _verify_stream_token(t, "userlogo", "logo")
        if token_uid is not None:
            user = await db.get(User, token_uid)
    if user is None:
        raise HTTPException(status_code=401, detail="Telegram authentication required")
    # Batch AR: a signed token resolves the user WITHOUT passing
    # through get_current_user(), so the maintenance entry block is
    # re-checked here (initData callers were already refused inside
    # get_optional_user). Admins exempt, same shape as everywhere.
    _enforce_maintenance_access(user)
    # Batch AJ L11: same ban discipline as the recap token downloads.
    if getattr(user, "status", None) == "BLOCKED" and not settings.is_admin(user.id):
        raise HTTPException(status_code=404, detail="Logo မတင်ရသေးပါ")
    found = _find_user_logo(user.id)
    if found is None:
        raise HTTPException(status_code=404, detail="Logo မတင်ရသေးပါ")
    return FileResponse(
        path=str(found),
        media_type=LOGO_MEDIA_TYPES.get(found.suffix.lower(), "application/octet-stream"),
        content_disposition_type="inline",
    )


@api_router.post("/user/logo")
async def upload_user_logo(
    logo_file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    _gate: User = Depends(require_gate_verified),
):
    """Upload (or replace) the caller's own channel logo. Paid/admin
    only — free users are rejected with the VIP upsell, and nothing is
    stored for them."""
    if not _can_use_custom_logo(user):
        raise HTTPException(status_code=403, detail=LOGO_VIP_ONLY_DETAIL)
    ext = Path(logo_file.filename or "").suffix.lower()
    if ext not in LOGO_IMAGE_FORMATS:
        raise HTTPException(
            status_code=400, detail="Logo ပုံသည် PNG, JPG သို့မဟုတ် WEBP ဖြစ်ရပါမည်"
        )
    logo_dir = _user_logo_dir(user.id)
    logo_dir.mkdir(parents=True, exist_ok=True)
    # Save to a temp name first: a failed validation must never
    # clobber the previously stored (working) logo.
    tmp_path = logo_dir / f".uploading-{uuid.uuid4().hex[:8]}{ext}"
    await _save_upload_streaming(logo_file, tmp_path, MAX_LOGO_UPLOAD_BYTES)
    try:
        _validate_logo_image(tmp_path, LOGO_IMAGE_FORMATS[ext])
    except HTTPException:
        tmp_path.unlink(missing_ok=True)
        raise
    for old_ext in LOGO_FILE_EXTS:
        (logo_dir / f"logo{old_ext}").unlink(missing_ok=True)
    canonical_ext = ".jpg" if ext == ".jpeg" else ext
    tmp_path.replace(logo_dir / f"logo{canonical_ext}")
    return _logo_state_payload(user)


@api_router.delete("/user/logo")
async def delete_user_logo(
    user: User = Depends(get_current_user),
    _gate: User = Depends(require_gate_verified),
):
    """Remove the caller's stored logo. Not tier-gated: a user who was
    demoted to Free can still delete their file (the generate path
    ignores it while they are Free either way)."""
    logo_dir = _user_logo_dir(user.id)
    for ext in LOGO_FILE_EXTS:
        (logo_dir / f"logo{ext}").unlink(missing_ok=True)
    return _logo_state_payload(user)


# ── 2. Voice Models & Curated Personas ─────────────────────────────────────
@api_router.get("/voices/curated")
async def get_curated_personas(user: User | None = Depends(get_optional_user)):
    """Return the 6 Gemini-crafted curated Burmese voice personas."""
    lang = user.language_code if user else "my"
    return list_curated_voices(lang=lang)


@api_router.get("/voices/models")
async def list_voice_models(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """Return all available system and custom voice models."""
    stmt = (
        select(VoiceModel)
        .where(VoiceModel.status == VoiceModelStatus.ACTIVE.value)
        .order_by(VoiceModel.id.asc())
    )
    res = await db.execute(stmt)
    models = res.scalars().all()

    is_vip = (user.account_category == AccountCategory.PURCHASED.value) or settings.is_admin(user.id)

    # Privacy scoping (hunt #2 V2): SYSTEM models stay listed for
    # everyone exactly as before, but a PERSONAL model is only listed
    # for its owner, an admin, or a user holding a grant — the same
    # visibility the bot enforces via get_available_models_for_user.
    # Previously every signed-in user could enumerate (and sample)
    # other users' personal models.
    authorized_ids = {m.id for m in await get_available_models_for_user(db, user.id)}
    models = [
        m for m in models
        # Bot cleanup 2026-11-03: legacy .pth SYSTEM rows (display-only
        # zombies — queue.py maps their jobs onto VoxCPM) are hidden
        # from this catalogue too, same rule as the bot surfaces.
        if is_displayable_model(m)
        and (
            m.type != VoiceModelType.PERSONAL.value
            or m.owner_user_id == user.id
            or m.id in authorized_ids
        )
    ]

    model_list = []
    for m in models:
        allowed = is_vip or m.allowed_for_free
        model_list.append({
            "id": m.id,
            "name": m.name,
            "slug": m.slug,
            "type": m.type,
            "allowed": allowed,
            "is_voxcpm": (m.pth_path == "voxcpm" or "voxcpm" in m.slug.lower()),
            "sample_audio_url": f"/api/voices/sample/{m.slug}",
        })
    return model_list


# ── 3. Voice Generation ───────────────────────────────────────────────────
class VoiceGenerateRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)
    # Length caps aligned to the columns these values land in
    # (hunt #4 A2): an over-long string used to sail through
    # validation and die as a Postgres truncation 500 at insert.
    model_slug: str = Field("voxcpm-cloud-ai", max_length=255)  # VoiceModel.slug String(255)
    speed: str = Field(DEFAULT_SPEED, max_length=20)
    gender_preset: str = Field("m2m", max_length=20)
    engine: str = Field("voxcpm", max_length=20)  # Job.engine String(20)
    preset_voice: Optional[str] = Field(None, max_length=50)  # Job.preset_voice String(50)

    @field_validator("speed")
    @classmethod
    def _norm_voice_speed(cls, v) -> str:
        norm = normalize_api_speed(v)
        if norm is None:
            raise ValueError(f"Unsupported speed: {v!r}")
        return norm


@api_router.post("/voices/generate")
async def create_voice_job(
    req: VoiceGenerateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    _gate: User = Depends(require_gate_verified),
):
    """Submit a voice clone generation job from Mini App."""
    # Encoding repair before the length check below (hunt #4 A1):
    # Zawgyi-typed text becomes Unicode so the counted length is the
    # spoken length; create_voice_clone_job converts again idempotently.
    clean_text = convert_zawgyi_to_unicode(req.text.strip())
    if not clean_text:
        raise HTTPException(status_code=400, detail="စာသား ရိုက်ထည့်ပေးပါ")

    _require_valid_preset_voice(req.preset_voice)

    is_vip = (user.account_category == AccountCategory.PURCHASED.value) or settings.is_admin(user.id)
    char_limit = settings.character_limit_purchased if is_vip else settings.character_limit_free

    if len(clean_text) > char_limit:
        raise HTTPException(
            status_code=400,
            detail=f"စာသား စာလုံးရေ ({len(clean_text)}) ဟာ ကန့်သတ်ချက် ({char_limit} လုံး) ထက် ကျော်လွန်နေပါတယ်။ "
                   + (f"Free အသုံးပြုသူတွေအတွက် တစ်ကြိမ်လျှင် စာလုံးရေ {settings.character_limit_free:,} အထိသာ ကန့်သတ်ထားပါတယ်။ စာလုံးရေ {settings.character_limit_purchased:,} အထိ သုံးလိုပါက Plan ဝယ်ယူနိုင်ပါတယ်။" if not is_vip else ""),
        )

    # Determine engine & resolve authorized model
    is_vox = (
        req.engine == "voxcpm"
        or req.preset_voice is not None
        or "voxcpm" in (req.model_slug or "").lower()
    )
    if not is_vox:
        raise HTTPException(status_code=400, detail="ဒီအသံ မော်ဒယ် အဟောင်းကို မသုံးနိုင်တော့ပါ။ VoxCPM2 နဲ့ ကိုးကားအသံ (၁၀-၃၀ စက္ကန့်) ပို့ပြီး ပြန်ပွားပေးပါ။")
    actual_engine = "voxcpm"

    if actual_engine == "voxcpm":
        stmt_vox = select(VoiceModel).where(VoiceModel.slug == "voxcpm-cloud-ai")
        res_vox = await db.execute(stmt_vox)
        model = res_vox.scalar_one_or_none()
        if not model:
            stmt_vox2 = select(VoiceModel).where(VoiceModel.pth_path == "voxcpm")
            res_vox2 = await db.execute(stmt_vox2)
            model = res_vox2.scalar_one_or_none()
        if not model:
            from app.models.enums import VoiceModelStatus, VoiceModelType
            model = VoiceModel(
                name="🎙️ VoxCPM (Cloud AI)",
                slug="voxcpm-cloud-ai",
                type=VoiceModelType.SYSTEM.value,
                status=VoiceModelStatus.ACTIVE.value,
                allowed_for_free=True,
                allowed_for_purchased=True,
                is_default_free=True,
                pth_path="voxcpm",
                model_dir="cloud",
            )
            db.add(model)
            await db.commit()
            await db.refresh(model)
    else:
        stmt = select(VoiceModel).where(VoiceModel.slug == req.model_slug)
        res = await db.execute(stmt)
        model = res.scalar_one_or_none()
        if not model:
            raise HTTPException(status_code=404, detail="အသံမော်ဒယ် ရှာမတွေ့ပါ")

    # Dynamically resolve edge_voice from preset_voice or gender_preset
    resolved_edge_voice = "my-MM-ThihaNeural"
    if req.preset_voice:
        from app.services.curated_voices import get_curated_voice
        p_info = get_curated_voice(req.preset_voice)
        if p_info:
            resolved_edge_voice = p_info.get("edge_voice") or ("my-MM-NilarNeural" if p_info.get("gender") == "female" else "my-MM-ThihaNeural")
    elif req.gender_preset in ("m2f", "f2f"):
        resolved_edge_voice = "my-MM-NilarNeural"

    # Security fix (audit 2026-10-28): validate through the shared queue
    # validator (balance, FREE daily limit, concurrency, model
    # authorization) before enqueueing, instead of inserting a Job
    # directly with only a character-limit check.
    from app.services.queue import create_voice_clone_job

    try:
        job, _reason = await create_voice_clone_job(
            db,
            user.id,
            text=clean_text,
            model_id=model.id,
            gender_preset=req.gender_preset,
            speed_preset=req.speed,
            edge_voice=resolved_edge_voice,
            engine=actual_engine,
            preset_voice=req.preset_voice,
        )
    except ValueError:
        # Service-level preset_voice invariant (E4): same clean 400
        # the endpoint pre-check above raises.
        raise HTTPException(status_code=400, detail=_INVALID_PRESET_VOICE_DETAIL)
    if not job:
        raise HTTPException(
            status_code=_voice_job_error_status(_reason),
            detail=_voice_job_error_detail(_reason, user),
        )
    await db.commit()
    await db.refresh(job)
    if job.model_id and job.model_id != model.id:
        model = await db.get(VoiceModel, job.model_id) or model

    return {
        "success": True,
        "job_id": job.id,
        "status": "QUEUED",
        "engine": actual_engine,
        "model_name": model.name,
        "is_voxcpm": is_vox,
        "preset_voice": req.preset_voice,
        "message": "အသံထုတ်ယူခြင်း စတင်လုပ်ဆောင်နေပါပြီခင်ဗျာ",
    }


@api_router.post("/voices/generate-with-audio")
async def create_voice_job_with_audio(
    text: str = Form(...),
    model_slug: str = Form("voxcpm-cloud-ai"),
    speed: str = Form(DEFAULT_SPEED),
    gender_preset: str = Form("m2m"),
    engine: str = Form("voxcpm"),
    preset_voice: Optional[str] = Form(None),
    reference_audio: Optional[UploadFile] = File(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    _gate: User = Depends(require_gate_verified),
):
    """Submit a voice clone generation job with uploaded Reference Audio from Mini App."""
    # Encoding repair before the length check below (hunt #4 A1), same
    # as the JSON endpoint above.
    clean_text = convert_zawgyi_to_unicode(text.strip())
    if not clean_text:
        raise HTTPException(status_code=400, detail="စာသား ရိုက်ထည့်ပေးပါ")

    _require_valid_preset_voice(preset_voice)
    norm_speed = normalize_api_speed(speed)
    if norm_speed is None:
        raise HTTPException(status_code=400, detail="Unsupported speed.")

    is_vip = (user.account_category == AccountCategory.PURCHASED.value) or settings.is_admin(user.id)
    char_limit = settings.character_limit_purchased if is_vip else settings.character_limit_free

    if len(clean_text) > char_limit:
        raise HTTPException(
            status_code=400,
            detail=f"စာသား စာလုံးရေ ({len(clean_text)}) ဟာ ကန့်သတ်ချက် ({char_limit} လုံး) ထက် ကျော်လွန်နေပါတယ်။ "
                   + (f"Free အသုံးပြုသူတွေအတွက် တစ်ကြိမ်လျှင် စာလုံးရေ {settings.character_limit_free:,} အထိသာ ကန့်သတ်ထားပါတယ်။ စာလုံးရေ {settings.character_limit_purchased:,} အထိ သုံးလိုပါက Plan ဝယ်ယူနိုင်ပါတယ်။" if not is_vip else ""),
        )

    # Determine engine & resolve authorized model
    is_vox = (
        engine == "voxcpm"
        or preset_voice is not None
        or "voxcpm" in (model_slug or "").lower()
    )
    if not is_vox:
        raise HTTPException(status_code=400, detail="ဒီအသံ မော်ဒယ် အဟောင်းကို မသုံးနိုင်တော့ပါ။ VoxCPM2 နဲ့ ကိုးကားအသံ (၁၀-၃၀ စက္ကန့်) ပို့ပြီး ပြန်ပွားပေးပါ။")
    actual_engine = "voxcpm"

    if actual_engine == "voxcpm":
        stmt_vox = select(VoiceModel).where(VoiceModel.slug == "voxcpm-cloud-ai")
        res_vox = await db.execute(stmt_vox)
        model = res_vox.scalar_one_or_none()
        if not model:
            stmt_vox2 = select(VoiceModel).where(VoiceModel.pth_path == "voxcpm")
            res_vox2 = await db.execute(stmt_vox2)
            model = res_vox2.scalar_one_or_none()
        if not model:
            from app.models.enums import VoiceModelStatus, VoiceModelType
            model = VoiceModel(
                name="🎙️ VoxCPM (Cloud AI)",
                slug="voxcpm-cloud-ai",
                type=VoiceModelType.SYSTEM.value,
                status=VoiceModelStatus.ACTIVE.value,
                allowed_for_free=True,
                allowed_for_purchased=True,
                is_default_free=True,
                pth_path="voxcpm",
                model_dir="cloud",
            )
            db.add(model)
            await db.commit()
            await db.refresh(model)
    else:
        stmt = select(VoiceModel).where(VoiceModel.slug == model_slug)
        res = await db.execute(stmt)
        model = res.scalar_one_or_none()
        if not model:
            raise HTTPException(status_code=404, detail="အသံမော်ဒယ် ရှာမတွေ့ပါ")

    ref_audio_path = None
    if reference_audio and reference_audio.filename:
        # Batch AJ H3 + M1: the old code did `await
        # reference_audio.read()` — the WHOLE upload buffered in RAM —
        # and only then compared its length to 50 MB, so one oversized
        # request could OOM the 2 GB instance before the check ran
        # (this route is also outside the JSON Content-Length
        # middleware's coverage). Stream to disk with the cap enforced
        # DURING the write, behind the disk-headroom gate.
        _require_disk_headroom()
        ref_dir = settings.storage_root / "temp" / "voxcpm_refs"
        ref_dir.mkdir(parents=True, exist_ok=True)
        ext = Path(reference_audio.filename).suffix or ".wav"
        clean_ext = ext if ext.lower() in (".wav", ".mp3", ".ogg", ".m4a") else ".wav"
        save_file = ref_dir / f"webapp_ref_{uuid.uuid4().hex[:12]}{clean_ext}"
        try:
            saved_bytes = await _save_upload_streaming(
                reference_audio, save_file, 50 * 1024 * 1024
            )
        except HTTPException as exc:
            if exc.status_code == 413:
                raise HTTPException(
                    status_code=413,
                    detail="အသံနမူနာဖိုင် အရွယ်အစား 50 MB ထက် ကျော်လွန်နေပါတယ်",
                )
            raise
        if saved_bytes > 100:
            ref_audio_path = str(save_file)
        else:
            save_file.unlink(missing_ok=True)

    # Dynamically resolve edge_voice from preset_voice or gender_preset
    resolved_edge_voice = "my-MM-ThihaNeural"
    if preset_voice:
        from app.services.curated_voices import get_curated_voice
        p_info = get_curated_voice(preset_voice)
        if p_info:
            resolved_edge_voice = p_info.get("edge_voice") or ("my-MM-NilarNeural" if p_info.get("gender") == "female" else "my-MM-ThihaNeural")
    elif gender_preset in ("m2f", "f2f"):
        resolved_edge_voice = "my-MM-NilarNeural"

    # Security fix (audit 2026-10-28): same shared queue validation as
    # /voices/generate (balance, daily limit, concurrency, model
    # authorization) before enqueueing.
    from app.services.queue import create_voice_clone_job

    try:
        job, _reason = await create_voice_clone_job(
            db,
            user.id,
            text=clean_text,
            model_id=model.id,
            gender_preset=gender_preset,
            speed_preset=norm_speed,
            edge_voice=resolved_edge_voice,
            engine=actual_engine,
            preset_voice=preset_voice,
            source_audio_path=ref_audio_path,
        )
    except ValueError:
        # Service-level preset_voice invariant (E4): clean up the
        # uploaded reference audio, then the same clean 400 the
        # endpoint pre-check above raises.
        if ref_audio_path:
            try:
                Path(ref_audio_path).unlink()
            except OSError:
                pass
        raise HTTPException(status_code=400, detail=_INVALID_PRESET_VOICE_DETAIL)
    if not job:
        # Do not leave the uploaded reference audio behind on failure.
        if ref_audio_path:
            try:
                Path(ref_audio_path).unlink()
            except OSError:
                pass
        raise HTTPException(
            status_code=_voice_job_error_status(_reason),
            detail=_voice_job_error_detail(_reason, user),
        )
    await db.commit()
    await db.refresh(job)
    if job.model_id and job.model_id != model.id:
        model = await db.get(VoiceModel, job.model_id) or model

    return {
        "success": True,
        "job_id": job.id,
        "status": "QUEUED",
        "engine": actual_engine,
        "model_name": model.name,
        "has_reference_audio": bool(ref_audio_path),
        "is_voxcpm": is_vox,
        "preset_voice": preset_voice,
        "message": "အသံထုတ်ယူခြင်း စတင်လုပ်ဆောင်နေပါပြီခင်ဗျာ",
    }


@api_router.get("/jobs/{job_id}/status")
async def get_job_status(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """Poll job status for real-time progress & audio delivery."""
    job = await db.get(Job, str(job_id))
    if not job or (job.user_id != user.id and not settings.is_admin(user.id)):
        raise HTTPException(status_code=404, detail="Job not found")

    is_waiting_laptop = False  # Voice v2: no laptop engine; all jobs run on VoxCPM2 cloud.

    # Over-50MB rule (2026-11-03): when the result WAV exceeds the
    # Telegram bot send cap, the Mini App hides "Send to Telegram" and
    # offers Download only — same telegram_sendable pattern as the
    # downloader. Only computed for SUCCESS jobs (the only state whose
    # result screen shows the button).
    if job.status == JobStatus.SUCCESS.value:
        _voice_size, _voice_sendable = _voice_output_size_and_sendable(job)
    else:
        _voice_size, _voice_sendable = None, True

    # Batch BJ: sign the audio URL for its requester. The voice/TTS
    # result screens' download buttons hand this URL to the device
    # browser via Telegram openLink, where no initData header exists;
    # the audio route verifies this exact (job, "audio", user) scope.
    # With no bot token configured the URL passes through unsigned,
    # as before. In-app playback is unaffected: it fetches with the
    # header, and when both are sent the header user wins.
    _output_audio_url = None
    if job.status == JobStatus.SUCCESS.value:
        _output_audio_url = _append_stream_token(
            f"/api/audio/download/{job.id}",
            _make_stream_token(job.id, user.id, "audio"),
        )

    return {
        "id": job.id,
        "status": job.status,
        "engine": job.engine,
        "fallback_used": job.engine == "edge_fallback",
        # Batch BU: persisted auto-trim disclosure (voice jobs only —
        # the column is set by the voice worker; recap rows stay False).
        "reference_trimmed": bool(getattr(job, "reference_trimmed", False)),
        "is_waiting_laptop_worker": is_waiting_laptop,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "error_message": job.error_message,
        "output_audio_url": _output_audio_url,
        "output_size_bytes": _voice_size,
        "telegram_sendable": _voice_sendable,
    }


# ── Batch AC: account-bound preferences ─────────────────────────────────
# The user's recap + voice PREFERENCES live on the server, bound to
# the account, so they survive a new device / cleared WebView storage
# (owner pain point: "my settings keep disappearing"). localStorage
# is only a write-through cache on the client; the server wins.
#
# The field list below is the COMPLETE stored set. Deliberately NOT
# preferences (Batch AA semantics, respected before AA merges):
# per-source blur-box geometry — blur_y / blur_h (Batch AG: and
# blur_x / blur_w) for a specific source and the blur_box_explicit
# marker — stays per-source and is dropped by the sanitizer if a
# client sends it. The subtitle's horizontal placement (recapSubX /
# recapSubW, Batch AG) is NOT per-source — like recapSubY it follows
# the account.
USER_PREF_ENUMS: Dict[str, List[str]] = {
    "voice_speed": list(SPEED_OPTIONS),
    "recapRatio": ["9:16", "16:9", "1:1", "Original"],
    "recapRatioFill": ["blur", "crop", "black"],
    "recapSpeed": list(SPEED_OPTIONS),
    "recapStoryStyle": ["viral", "dramatic", "funny", "pov", "standard", "narrator"],
    # S4: 12 style presets (10 original + cinematic & glass).
    "recapSubStyle": ["yellow", "white", "boxed", "neon", "band", "heavy", "soft", "rose", "orange", "mint", "cinematic", "glass"],
    "recapProtection": ["off", "standard", "strong"],
    "recapSubFont": ["padauk", "noto_sans", "pyidaungsu", "noto_serif"],
    "recapLogoPos": ["top-right", "top-left", "bottom-right", "bottom-left", "top-center"],
    # Batch AK (premium audit S6/N2): every language the backend
    # accepts (RECAP_SOURCE_LANGUAGE_CODES) is pickable and
    # persistable — the picker used to offer only 6 of the 19.
    "recapSourceLanguage": [
        "auto", "en", "zh", "ko", "th", "ja", "hi", "my", "es",
        "fr", "de", "id", "vi", "ru", "ar", "it", "pt", "tr",
    ],
    "recapRenderQuality": ["standard", "high", "max"],
    "recapTtsEngine": ["voxcpm", "gemini", "edge"],
}
USER_PREF_BOOLS: List[str] = [
    "recapSyncCrop", "recapMirror", "recapBurnSubs", "recapBlurSubs",
    "recapLogo", "recapSplitEnabled",
    # Batch BI: the source-subtitle auto-detect blur switch.
    "recapBlurAutoDetect",
    # Batch DD: free subtitle-style toggles (shadow / bold).
    "recapSubShadow", "recapSubBold",
]
USER_PREF_INTS: Dict[str, tuple] = {
    "recapCropTop": (0, 35), "recapCropBottom": (0, 35),
    "recapCropLeft": (0, 35), "recapCropRight": (0, 35),
    # Batch DD: the size slider runs 10–26pt (schema still accepts
    # stored values up to 30) and Lines gains a 3-line choice.
    "recapFontSize": (10, 30), "recapLines": (1, 3),
    "recapSplitMinutes": (1, 10), "recapSubY": (0, 100),
    # Batch AK: bottom-edge subtitle margin (% of picture height)
    # and the settings-schema version that gates the one-time
    # centre-% → margin conversion in _effective_user_prefs.
    "recapSubMargin": (0, 95), "recapSettingsVersion": (0, 10),
    "recapSubX": (10, 90), "recapSubW": (10, 100),
    "recapBlurStrength": (4, 40),
    # Batch DD: background-box opacity for the free style controls.
    "recapBgOpacity": (0, 100),
}
# Free-string preferences, length-capped; voice slugs are additionally
# checked against the curated registry when one matches that role.
USER_PREF_STRINGS: Dict[str, int] = {
    "voice_persona": 50, "recapVoice": 50, "recapGeminiVoice": 100,
    # Batch DD: free style colours (#RRGGBB or "" = preset colour),
    # the manual blur-box list as JSON (≤5 boxes), and the custom
    # subtitle-font upload handle. (recapTitleText is gone with the
    # title feature — legacy stored values are dropped on read.)
    "recapTextColor": 7, "recapOutlineColor": 7, "recapBgColor": 7,
    "recapBlurBoxesJson": 2000, "recapSubFontCustomId": 80,
    "recap_presets": 4000,
}
USER_PREF_FLOATS: Dict[str, tuple] = {
    "recapZoom": (1.0, 1.2),
    # Batch DD: outline width in ASS units; -1 = the preset's own.
    "recapOutlineWidth": (-1.0, 4.0),
}

DEFAULT_USER_PREFS: Dict[str, Any] = {
    "voice_persona": "komin",
    "voice_speed": DEFAULT_SPEED,
    "recapRatio": "9:16",
    "recapRatioFill": "blur",
    "recapSpeed": DEFAULT_SPEED,
    "recapStoryStyle": "viral",
    "recapSubStyle": "yellow",
    "recapSubFont": "padauk",
    "recapLogoPos": "top-right",
    "recapSourceLanguage": "auto",
    "recapRenderQuality": "standard",
    "recapTtsEngine": "voxcpm",
    "recapSyncCrop": True,
    "recapMirror": True,
    "recapBurnSubs": True,
    "recapBlurSubs": True,
    "recapLogo": True,
    "recapSplitEnabled": False,
    "recapCropTop": 0,
    "recapCropBottom": 0,
    "recapCropLeft": 0,
    "recapCropRight": 0,
    "recapFontSize": 16,
    "recapLines": 1,
    "recapSplitMinutes": 3,
    "recapSubY": 85,
    "recapSubMargin": 5,
    "recapSettingsVersion": 2,
    "recapSubX": 50,
    "recapSubW": 92,
    # Batch DD defaults: protection Standard (today's behaviour),
    # free style controls unset (preset look), bold on, shadow off.
    "recapProtection": "standard",
    "recapTextColor": "",
    "recapOutlineColor": "",
    "recapBgColor": "",
    "recapBgOpacity": 75,
    "recapOutlineWidth": -1.0,
    "recapSubShadow": False,
    "recapSubBold": True,
    "recapBlurBoxesJson": "",
    "recapSubFontCustomId": "",
    "recapBlurStrength": 18,
    # Batch BI: auto-detect blur defaults ON (historical behaviour).
    "recapBlurAutoDetect": True,
    # Batch AK (premium audit S1): the shipped default Micro-Zoom
    # is 1.00 — 1.06 silently cropped ≈11% of the picture in the
    # fill modes. Saved user values persist untouched.
    "recapZoom": 1.0,
    "recapVoice": "komin",
    "recapGeminiVoice": "",
    "recap_presets": "",
}


def _sanitize_user_prefs(raw) -> Dict[str, Any]:
    """Keep only known preference fields with valid values.

    Unknown keys (including per-source blur geometry: recapBlurY,
    recapBlurH, recapBlurX, recapBlurW, blur_box_explicit) are
    dropped, never stored. Legacy subtitle-font aliases migrate
    exactly like the client does.
    """
    out: Dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    raw = dict(raw)
    if isinstance(raw.get("recap_presets"), str) and raw["recap_presets"]:
        try:
            _parsed_presets = json.loads(raw["recap_presets"])
            if isinstance(_parsed_presets, list):
                raw["recap_presets"] = _parsed_presets
        except Exception:
            pass
    if isinstance(raw.get("recap_presets"), list):
        valid_presets = [
            p for p in raw["recap_presets"][:3]
            if isinstance(p, dict) and isinstance(p.get("name"), str) and isinstance(p.get("settings"), dict)
        ]
        for _p in valid_presets:
            _st = _p.get("settings")
            if isinstance(_st, dict) and "recapSpeed" in _st:
                _mig = migrate_speed_pref(_st["recapSpeed"])
                if _mig is not None:
                    _st["recapSpeed"] = _mig
        raw["recap_presets"] = json.dumps(valid_presets, ensure_ascii=False)
    for _spk in ("voice_speed", "recapSpeed"):
        if _spk in raw:
            _mig_sp = migrate_speed_pref(raw[_spk])
            if _mig_sp is not None:
                raw[_spk] = _mig_sp
    _legacy_fonts = {"default": "padauk", "serif": "noto_serif"}
    if raw.get("recapSubFont") in _legacy_fonts:
        raw["recapSubFont"] = _legacy_fonts[raw["recapSubFont"]]
    for key, allowed in USER_PREF_ENUMS.items():
        if raw.get(key) in allowed:
            out[key] = raw[key]
    for key in USER_PREF_BOOLS:
        if isinstance(raw.get(key), bool):
            out[key] = raw[key]
    for key, (lo, hi) in USER_PREF_INTS.items():
        v = raw.get(key)
        if isinstance(v, bool):
            continue
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if lo <= iv <= hi:
            out[key] = iv
    for key, (lo, hi) in USER_PREF_FLOATS.items():
        v = raw.get(key)
        if isinstance(v, bool):
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if lo <= fv <= hi:
            out[key] = fv
    for key, max_len in USER_PREF_STRINGS.items():
        v = raw.get(key)
        if isinstance(v, str) and len(v) <= max_len:
            out[key] = v
    # Voice slugs must be real curated personas when they name a voice.
    for key in ("voice_persona", "recapVoice"):
        if key in out and out[key] and get_curated_voice(out[key]) is None:
            del out[key]
    return out


def _effective_user_prefs(stored) -> Dict[str, Any]:
    """Server-wins merge: today's defaults under the stored values."""
    clean = _sanitize_user_prefs(stored)
    merged = dict(DEFAULT_USER_PREFS)
    merged.update(clean)
    # Batch AK settings migration — runs once, versioned. Pre-AK
    # accounts stored the subtitle anchor as a centre-% of the
    # picture (recapSubY); the anchor is now a bottom-edge margin
    # (recapSubMargin, % of picture height). Convert ONLY when the
    # account actually stored a recapSubY and has not been migrated
    # (stored recapSettingsVersion < 2): the conversion is
    # plate-height-aware (the user's own font size / line count),
    # so the subtitle stays at the same visual position. Accounts
    # that never stored a position keep the fresh 5% default, and
    # migrated accounts are stamped version 2 so it never re-runs.
    if int(clean.get("recapSettingsVersion") or 0) < 2 and "recapSubY" in clean:
        from app.services.recap_engine.core import sub_centre_to_bottom_margin

        merged["recapSubMargin"] = int(round(sub_centre_to_bottom_margin(
            float(clean["recapSubY"]) / 100.0,
            raw_font_size=int(merged.get("recapFontSize") or 16),
            sub_lines=int(merged.get("recapLines") or 1),
        ) * 100))
    merged["recapSettingsVersion"] = 2
    return merged


class UserPreferencesPayload(BaseModel):
    preferences: Dict[str, Any] = Field(default_factory=dict)


@api_router.get("/user/preferences")
async def get_user_preferences(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """The calling user's own account-bound preferences.

    A brand-new user (no row yet) gets today's defaults with
    ``is_new: true`` — nothing is written on a read.
    """
    row = await db.get(UserPreference, user.id)
    stored = None
    if row is not None and row.prefs_json:
        try:
            stored = json.loads(row.prefs_json)
        except (TypeError, ValueError):
            stored = None
    prefs = _effective_user_prefs(stored)
    # D2 / S4: brand-new accounts (no saved UserPreference row yet) receive
    # Cinematic White by default; existing saved rows keep stored style / yellow.
    if row is None:
        prefs["recapSubStyle"] = "cinematic"
    return {
        "success": True,
        "is_new": row is None,
        "preferences": prefs,
    }


@api_router.put("/user/preferences")
async def put_user_preferences(
    payload: UserPreferencesPayload,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """Save a partial preference update to the caller's OWN record.

    The update merges over the stored record (itself merged over the
    defaults), so a client may send only the fields that changed.
    """
    clean = _sanitize_user_prefs(payload.preferences)
    row = await db.get(UserPreference, user.id)
    stored = None
    if row is not None and row.prefs_json:
        try:
            stored = json.loads(row.prefs_json)
        except (TypeError, ValueError):
            stored = None
    merged_stored = _sanitize_user_prefs(stored)
    merged_stored.update(clean)
    if row is None:
        row = UserPreference(user_id=user.id, prefs_json=json.dumps(merged_stored))
        db.add(row)
    else:
        row.prefs_json = json.dumps(merged_stored)
        row.updated_at = utc_now()
    await db.commit()
    return {
        "success": True,
        "preferences": _effective_user_prefs(merged_stored),
    }


# ── Batch AC: bulk live-jobs layer ──────────────────────────────────────
# ONE response carrying ALL the calling user's non-terminal voice +
# recap jobs, plus jobs that reached a terminal state within the last
# LIVE_JOBS_RECENT_TERMINAL_MINUTES minutes (so a client that was
# tracking a card can flip it). Strictly the caller's own jobs; the
# query is bounded.
LIVE_JOBS_RECENT_TERMINAL_MINUTES = 10
LIVE_JOBS_LIMIT = 20
_LIVE_TERMINAL_STATUSES = (
    JobStatus.SUCCESS.value,
    JobStatus.FAILED.value,
    JobStatus.CANCELLED.value,
)
_LIVE_ACTIVE_STATUSES = (
    JobStatus.PENDING.value,
    JobStatus.QUEUED.value,
    JobStatus.RUNNING.value,
    JobStatus.WAITING_CHOICE.value,
)


def _live_stage_from_step(step, status) -> tuple:
    """Parse a recap "x/y" step into (stage_index, stage_total)."""
    m = re.match(r"\s*(\d+)\s*/\s*(\d+)\s*$", str(step or ""))
    if m:
        return int(m.group(1)), int(m.group(2))
    if status in _LIVE_TERMINAL_STATUSES:
        return 1, 1
    return 0, 4


def _live_job_payload(job, registry_entry=None, now=None, medians=None) -> Dict[str, Any]:
    """Serialize one Job row (+ live recap registry entry) for the
    bulk live-jobs response. Pure apart from the registry read the
    caller passes in, so it is unit-testable without a request.

    Batch CB: when the caller supplies the cached median buckets,
    recap jobs also carry the honest ETA fields (eta_seconds,
    eta_range_seconds, eta_overrun) from the same estimator the
    detail status endpoint uses — the pill/sheet read the overrun
    flag from HERE (the LiveJobs store is their only channel)."""
    now = now or datetime.now(timezone.utc)
    is_recap = (
        job.job_type == JobType.RECAP.value
        or str(job.id).startswith("rc_")
        or job.engine == "recap"
    )
    kind = "recap" if is_recap else "voice"
    status = job.status
    progress_percent = 100 if status == JobStatus.SUCCESS.value else 0
    progress_stage = None
    progress_label = ""
    queue_position = None
    stage_index, stage_total = (1, 1) if status in _LIVE_TERMINAL_STATUSES else (0, 1)

    if is_recap and registry_entry is not None:
        status = str(registry_entry.get("status") or status)
        progress_percent = int(registry_entry.get("progress_percent") or 0)
        progress_stage = registry_entry.get("progress_stage")
        progress_label = str(registry_entry.get("progress_label") or "")
        stage_index, stage_total = _live_stage_from_step(
            registry_entry.get("progress_step"), status
        )
        if status in ("QUEUED", "RUNNING"):
            queue_position = _recap_registry_queue_position(job.id, registry_entry)
    elif is_recap:
        if status == JobStatus.SUCCESS.value:
            progress_percent, stage_index, stage_total = 100, 4, 4
        elif status in (JobStatus.QUEUED.value, JobStatus.RUNNING.value, JobStatus.WAITING_CHOICE.value):
            progress_percent, stage_index, stage_total = 0, 0, 4
    else:
        if status == JobStatus.SUCCESS.value:
            progress_percent = 100

    created = job.created_at
    finished = job.finished_at
    end = finished or now
    elapsed = None
    if created is not None:
        try:
            elapsed = max(0, int((end - created).total_seconds()))
        except Exception:
            elapsed = None
    raw_title = _clean_recap_display_title(job.input_text) if is_recap else (job.input_text or "")
    title = (raw_title or "")[:70]
    eta_payload = {
        "eta_seconds": None,
        "eta_range_seconds": None,
        "eta_overrun": False,
    }
    if is_recap and medians is not None and status in ("QUEUED", "RUNNING"):
        try:
            if registry_entry is not None:
                eta_payload = _recap_eta_from_entry(
                    registry_entry, medians, queue_position, now=now.timestamp()
                )
            else:
                eta_payload = _recap_eta_from_row(job, medians, None, now=now)
        except Exception:
            pass
    return {
        "id": job.id,
        "kind": kind,
        "status": status,
        "stage_index": stage_index,
        "stage_total": stage_total,
        "progress_percent": progress_percent,
        "progress_stage": progress_stage,
        "progress_label": progress_label,
        "queue_position": queue_position,
        "elapsed_seconds": elapsed,
        "eta_seconds": eta_payload["eta_seconds"],
        "eta_range_seconds": eta_payload["eta_range_seconds"],
        "eta_overrun": eta_payload["eta_overrun"],
        "title": title,
        "created_at": created.isoformat() if created else None,
        "finished_at": finished.isoformat() if finished else None,
    }


@api_router.get("/jobs/live")
async def get_live_jobs(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """Bulk live state for ALL the calling user's jobs (Batch AC)."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=LIVE_JOBS_RECENT_TERMINAL_MINUTES)
    stmt = (
        select(Job)
        .where(
            Job.user_id == user.id,
            or_(
                Job.status.in_(_LIVE_ACTIVE_STATUSES),
                Job.finished_at >= cutoff,
                # Rows whose finished_at was never stamped still flip
                # their tracked card via the created_at window.
                (Job.status.in_(_LIVE_TERMINAL_STATUSES) & (Job.created_at >= cutoff)),
            ),
        )
        .order_by(Job.created_at.desc())
        .limit(LIVE_JOBS_LIMIT)
    )
    res = await db.execute(stmt)
    jobs = res.scalars().all()

    seen_ids = {j.id for j in jobs}
    out = []
    # Batch CB: the shape medians are 60 s-cached; one fetch serves
    # every recap ETA in this bulk response.
    live_medians = None
    if any(
        j.job_type == JobType.RECAP.value or str(j.id).startswith("rc_")
        for j in jobs
    ) or RECAP_JOBS_REGISTRY:
        try:
            live_medians = await _recap_median_buckets()
        except Exception:
            live_medians = None
    for job in jobs:
        entry = RECAP_JOBS_REGISTRY.get(job.id) if (
            job.job_type == JobType.RECAP.value or str(job.id).startswith("rc_")
        ) else None
        payload = _live_job_payload(job, entry, now=now, medians=live_medians)
        # Voice queue position: the shared truthful counter (older
        # active jobs ahead of this one, across kinds — the voice
        # concurrency gate counts the same pool).
        if payload["kind"] == "voice" and payload["status"] == JobStatus.QUEUED.value:
            try:
                from app.services.queue import get_queue_position

                payload["queue_position"] = await get_queue_position(db, job.id)
            except Exception:
                payload["queue_position"] = None
        out.append(payload)

    # Registry-only recap jobs (their DB insert failed at submit):
    # still the user's live jobs, still tracked by the client.
    for job_id, entry in RECAP_JOBS_REGISTRY.items():
        if job_id in seen_ids or entry.get("user_id") != user.id:
            continue
        e_status = str(entry.get("status") or "")
        e_created = entry.get("created_at")
        recent_terminal = False
        if e_status in _LIVE_TERMINAL_STATUSES and isinstance(e_created, (int, float)):
            recent_terminal = (time.time() - float(e_created)) <= LIVE_JOBS_RECENT_TERMINAL_MINUTES * 60
        if e_status not in _LIVE_ACTIVE_STATUSES and e_status != "SCRIPT_READY" and not recent_terminal:
            continue
        stage_index, stage_total = _live_stage_from_step(entry.get("progress_step"), e_status)
        e_position = (
            _recap_registry_queue_position(job_id, entry)
            if e_status in ("QUEUED", "RUNNING") else None
        )
        e_eta = {
            "eta_seconds": None,
            "eta_range_seconds": None,
            "eta_overrun": False,
        }
        if live_medians is not None and e_status in ("QUEUED", "RUNNING"):
            try:
                e_eta = _recap_eta_from_entry(entry, live_medians, e_position)
            except Exception:
                pass
        out.append({
            "id": job_id,
            "kind": "recap",
            "status": e_status or "PENDING",
            "stage_index": stage_index,
            "stage_total": stage_total,
            "progress_percent": int(entry.get("progress_percent") or 0),
            "progress_stage": entry.get("progress_stage"),
            "progress_label": str(entry.get("progress_label") or ""),
            "queue_position": e_position,
            "eta_seconds": e_eta["eta_seconds"],
            "eta_range_seconds": e_eta["eta_range_seconds"],
            "eta_overrun": e_eta["eta_overrun"],
            "elapsed_seconds": (
                max(0, int(time.time() - float(e_created)))
                if isinstance(e_created, (int, float)) else None
            ),
            "title": str(entry.get("title") or "")[:70],
            "created_at": None,
            "finished_at": None,
        })
        if len(out) >= LIVE_JOBS_LIMIT:
            break

    active_count = sum(
        1 for p in out
        if p["status"] in _LIVE_ACTIVE_STATUSES or p["status"] == "SCRIPT_READY"
    )
    return {
        "success": True,
        "jobs": out,
        "active_count": active_count,
        "recent_terminal_minutes": LIVE_JOBS_RECENT_TERMINAL_MINUTES,
        "server_time": now.isoformat(),
    }


@api_router.post("/jobs/{job_id}/cancel")
async def cancel_voice_job(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """User cancel for their OWN queued / running VOICE job (Batch AC).

    Recap jobs keep their dedicated endpoint (/recap/{id}/cancel with
    its credit machinery); anything that is not a voice row is a 404
    here, with the same owner-only discipline as every other job
    endpoint. Refund semantics mirror the admin console's voice
    branch exactly: flip to CANCELLED, then the idempotent ledger
    refund (a job the worker already charged gets its seconds back;
    an uncharged job refunds nothing).
    """
    job = await db.get(Job, str(job_id))
    if not job or job.user_id != user.id:
        raise HTTPException(status_code=404, detail="Job not found")
    if (
        job.job_type == JobType.RECAP.value
        or job.id.startswith("rc_")
        or job.engine == "recap"
    ):
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in _CANCELLABLE_JOB_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="လုပ်ဆောင်နေဆဲ အသံ အလုပ်ကိုသာ ရပ်တန့်နိုင်ပါတယ်။",
        )
    await _claim_job_cancelled(
        db,
        job,
        db_error_message="Cancelled by user via Mini App",
        conflict_status_code=409,
        conflict_detail="လုပ်ဆောင်နေဆဲ အသံ အလုပ်ကိုသာ ရပ်တန့်နိုင်ပါတယ်။",
    )

    refund_info: Dict[str, Any] = {"refunded": False}
    try:
        from app.services.ledger import refund_job_seconds

        before = (await db.execute(
            select(MinuteLedgerEvent).where(
                MinuteLedgerEvent.user_id == job.user_id,
                MinuteLedgerEvent.reference_id == f"REFUND:{job_id}",
            )
        )).first()
        await refund_job_seconds(db, job.user_id, str(job.id), note="Refund: voice job cancelled by user")
        await db.commit()
        if not before:
            refund_info = {"refunded": True, "voice_seconds": "ledger debits for this job (if any)"}
    except Exception as exc:
        logger.warning(f"User cancel refund failed for voice job {job_id}: {exc}")
        refund_info = {"refunded": False, "error": "refund failed — check the user's ledger manually"}
    return {"success": True, "status": "CANCELLED", "refund": refund_info}


@api_router.get("/audio/stream/{job_id}")
@api_router.get("/audio/download/{job_id}")
async def download_job_audio(
    job_id: str,
    request: Request,
    user: Optional[User] = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db_session),
    t: Optional[str] = Query(None),
):
    """Stream or download the completed generated voice clone audio file."""
    # Batch BJ: auth mirrors the recap download route — the initData
    # header (resolved by get_optional_user) OR a signed stream token
    # minted for this exact job/audio/user. The Mini App's download
    # buttons hand the URL to the device browser via Telegram
    # openLink, where no initData header exists, and an <audio>
    # element cannot send one either; the scoped token is what
    # authenticates those opens. Either way the resolved user goes
    # through the same ownership check below; a token never creates
    # a user and never widens access beyond its own job scope.
    # (The token parameter sits after db so the existing direct-call
    # shape download_job_audio(job_id, request, user, db) is kept.)
    if user is None and t:
        token_uid = _verify_stream_token(t, job_id, "audio")
        if token_uid is not None:
            user = await db.get(User, token_uid)
    if user is None:
        raise HTTPException(status_code=401, detail="Telegram authentication required")
    # Batch AJ L11 (mirrored from the recap route): a stream token is
    # a 2 h bearer credential — a user banned after it was minted must
    # not keep downloading with it.
    if getattr(user, "status", None) == "BLOCKED" and not settings.is_admin(user.id):
        raise HTTPException(status_code=404, detail="Audio file not found")
    # Batch AR (mirrored): a token minted before maintenance was
    # switched on must not keep serving files to a non-admin while
    # the Mini App is closed. (Header callers were already refused
    # inside get_optional_user.)
    _enforce_maintenance_access(user)
    job = await _require_job_owner(db, job_id, user)

    # Batch AJ (H7): only a SUCCESS job has a deliverable. The worker
    # now charges before it persists any result, but rows written by
    # older paths (and any future regression) must not leak audio for
    # jobs whose charge failed, that were cancelled, or that never
    # ran — same 404 discipline as a missing file.
    if job.status != JobStatus.SUCCESS.value:
        raise HTTPException(status_code=404, detail="Audio file not ready or expired")

    target_path = None
    if job.result_path and Path(job.result_path).is_file():
        target_path = Path(job.result_path)
    else:
        outputs_dir = settings.storage_root / "outputs"
        if outputs_dir.is_dir():
            matches = list(outputs_dir.glob(f"{job.id}*"))
            if matches and matches[0].is_file():
                target_path = matches[0]

    if not target_path or not target_path.is_file():
        # Also check local output variations or storage subdirs
        for cand_dir in [settings.storage_root / "outputs", settings.storage_root / "temp", settings.storage_root / "voices", settings.storage_root / "recap_output"]:
            if cand_dir.is_dir():
                m = list(cand_dir.glob(f"*{job.id}*"))
                if m and m[0].is_file():
                    target_path = m[0]
                    break

    if not target_path or not target_path.is_file():
        raise HTTPException(status_code=404, detail="Audio file not ready or expired")

    ext = target_path.suffix.lower()
    media_type = "audio/wav"
    if ext == ".mp3":
        media_type = "audio/mpeg"
    elif ext in [".ogg", ".oga"]:
        media_type = "audio/ogg"
    elif ext in [".m4a", ".aac"]:
        media_type = "audio/mp4"

    is_stream = "/stream/" in request.url.path
    disposition = "inline" if is_stream else "attachment"

    return FileResponse(
        path=str(target_path),
        media_type=media_type,
        filename=branded_download_filename(
            # Batch AW: TTS-tab downloads carry the TTS brand segment.
            "TTS" if job.job_type == JobType.TTS.value else "Voice",
            (ext or ".wav").lstrip("."), title=job.preset_voice, job_id=job.id
        ),
        content_disposition_type=disposition,
    )


@api_router.post("/internal/worker/upload-result/{job_id}")
async def upload_worker_job_result(
    job_id: str,
    file: UploadFile = File(...),
    auth_token: str = Header(None, alias="X-Worker-Token"),
    db: AsyncSession = Depends(get_db_session),
):
    """Internal sync endpoint for Laptop Worker to upload converted audio results to Render Cloud."""
    # Hardening (hunt F8): the endpoint lives ONLY when a dedicated
    # WORKER_TOKEN is configured. The old fallback accepted the
    # Telegram bot token — readable by every code path — as the
    # upload credential; that is gone. Unset → 404 (the endpoint is
    # effectively absent; co-located mode needs no upload at all).
    worker_token = (getattr(settings, "worker_token", "") or "").strip()
    if not worker_token:
        raise HTTPException(status_code=404, detail="Not found")
    import hmac as _hmac

    supplied_token = (auth_token or "").strip()
    if not supplied_token or not _hmac.compare_digest(supplied_token, worker_token):
        raise HTTPException(status_code=403, detail="Unauthorized worker upload")

    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Batch AJ (H5): a terminal job must not be resurrected by a late
    # upload — the user may have cancelled (and been refunded) while
    # the laptop worker was rendering. Only a live job accepts a
    # result; anything else is a conflict, and nothing is written.
    if job.status in (
        JobStatus.CANCELLED.value,
        JobStatus.FAILED.value,
        JobStatus.SUCCESS.value,
    ):
        raise HTTPException(
            status_code=409,
            detail=f"Job is {job.status}; upload rejected",
        )

    # Batch AJ M1: no new result bytes land on a nearly-full disk.
    _require_disk_headroom()

    outputs_dir = settings.storage_root / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    # Security fix (audit 2026-10-28): sanitize the uploaded filename
    # (basename + uuid + whitelisted audio extension) and enforce a size
    # limit while copying, so a crafted filename cannot traverse out of
    # outputs/ and an unbounded upload cannot fill the disk.
    _allowed_audio_exts = {".wav", ".mp3", ".ogg", ".m4a"}
    _raw_ext = Path(file.filename or "").suffix.lower()
    _safe_ext = _raw_ext if _raw_ext in _allowed_audio_exts else ".wav"
    dest_filename = f"{job_id}_{uuid.uuid4().hex[:8]}{_safe_ext}"
    dest_path = outputs_dir / dest_filename
    _max_worker_upload_bytes = 100 * 1024 * 1024
    _written = 0
    try:
        with open(dest_path, "wb") as f_out:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                _written += len(chunk)
                if _written > _max_worker_upload_bytes:
                    raise HTTPException(
                        status_code=413, detail="Uploaded file exceeds the 100 MB limit"
                    )
                f_out.write(chunk)
    except Exception:
        try:
            dest_path.unlink()
        except OSError:
            pass
        raise

    job.result_path = str(dest_path)
    job.status = JobStatus.SUCCESS.value
    await db.commit()

    return {
        "status": "success",
        "job_id": job_id,
        "saved_path": str(dest_path),
        "download_url": f"/api/audio/download/{job_id}",
    }


@api_router.get("/voices/sample/{slug}")
@api_router.get("/voices/preview/{slug}")
async def get_voice_sample_audio(
    slug: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """Serve sample audio preview for curated VoxCPM2 voice personas.

    Authenticated (privacy fix, hunt #2 V2): samples used to be served
    to anyone who knew or guessed a slug — including other users'
    PERSONAL model samples. Curated / SYSTEM samples stay available to
    any signed-in user; a PERSONAL model's sample requires ownership
    or a grant (get_authorized_model semantics) and is denied
    404-style, mirroring the other media endpoints.
    """
    clean_id = slug.strip().lower()

    # Curated personas that were remapped to a human voice (see
    # app/services/curated_voices.py) preview the human voice's sample,
    # so the preview always matches what generation actually uses.
    try:
        from app.services.curated_voices import get_curated_voice

        _persona = get_curated_voice(clean_id)
        if _persona and _persona.get("reference_id"):
            clean_id = str(_persona["reference_id"])
    except Exception:
        pass

    # Legacy system-model sample slugs: the three character samples are
    # no longer served as voices — they now resolve to the default human
    # sample (komin), the same fallback used below. myanmar-voice-1 is a
    # human voice and keeps its own sample. The old mp3 files remain on
    # disk but are no longer reachable through this endpoint or the picker.
    alias_map = {
        "system-models-system-kartoon_150e_1950s-pth": "komin",
        "system-models-system-ant_150e_11700s-pth": "komin",
        "system-models-system-myanmar_voice_1_100e_6600s-pth": "myanmar-voice-1",
        "system-models-system-the_light_150e_450s-pth": "komin",
    }
    target_id = alias_map.get(clean_id, clean_id)

    # Personal-model guard, resolved BEFORE anything is served: if
    # this slug names a PERSONAL model, only its owner / a grantee /
    # an admin may hear its sample, and a personal slug never falls
    # through to the shared fallback sample below.
    stmt = select(VoiceModel).where(VoiceModel.slug == slug)
    res = await db.execute(stmt)
    m = res.scalar_one_or_none()
    if m is not None and m.type == VoiceModelType.PERSONAL.value:
        if await get_authorized_model(db, user.id, m.id) is None:
            raise HTTPException(status_code=404, detail="Sample audio not found")
        if m.sample_audio_path and Path(m.sample_audio_path).is_file():
            return FileResponse(path=m.sample_audio_path, media_type="audio/wav")
        raise HTTPException(status_code=404, detail="Sample audio not found")

    # 1. Check static sample mp3 in static/audio/samples/
    sample_file = settings.root / "app" / "webapp" / "static" / "audio" / "samples" / f"{target_id}.mp3"
    if sample_file.is_file():
        return FileResponse(
            str(sample_file),
            media_type="audio/mpeg",
            # Batch BG-5: no-cache (ETag revalidation) — the old
            # public max-age=86400 pinned the previous sample file in
            # phone/webview caches for 24h, so after the Batch BF voice
            # swap owners kept hearing the OLD voices for a day.
            headers={"Cache-Control": "no-cache"},
        )

    # 2. Check DB VoiceModel sample_audio_path (SYSTEM models; personal
    # models were already resolved by the guard above)
    if m and m.sample_audio_path and Path(m.sample_audio_path).is_file():
        return FileResponse(path=m.sample_audio_path, media_type="audio/wav")

    # 3. Fallback default sample (komin.mp3)
    fallback = settings.root / "app" / "webapp" / "static" / "audio" / "samples" / "komin.mp3"
    if fallback.is_file():
        return FileResponse(
            str(fallback),
            media_type="audio/mpeg",
            # Batch BG-5: same no-cache rule as the static return above.
            headers={"Cache-Control": "no-cache"},
        )

    raise HTTPException(status_code=404, detail="Sample audio not found")


# ── 4. Quick Downloader & Video Info ───────────────────────────────────────
class ExtractMediaRequest(BaseModel):
    url: str


@api_router.post("/downloader/extract")
async def extract_media_info(
    req: ExtractMediaRequest,
    user: User = Depends(get_current_user),
    _gate: User = Depends(require_gate_verified),
):
    """Extract YouTube/TikTok/Douyin/Facebook video metadata via yt-dlp."""
    from app.services.downloader import extract_first_url, unshorten_url, detect_platform
    raw_url = req.url.strip()
    url = extract_first_url(raw_url) or raw_url
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="မှန်ကန်တဲ့ Video URL ထည့်သွင်းပေးပါ")

    try:
        # unshorten_url follows redirects synchronously — off the
        # event loop (deep-hunt #4 D1), like the download endpoints.
        canonical_url = await asyncio.to_thread(unshorten_url, url)
    except Exception:
        raise HTTPException(status_code=400, detail="ဒီလင့်ခ်ကို အသုံးပြု၍ မရပါခင်ဗျာ")
    # SSRF: unshorten_url now validates every redirect hop, and this
    # re-validates the final URL (DNS resolve + private-range block)
    # before yt-dlp is allowed to fetch it.
    try:
        from app.services.url_safety import validate_public_url

        canonical_url = validate_public_url(canonical_url)
    except Exception:
        raise HTTPException(status_code=400, detail="ဒီလင့်ခ်ကို အသုံးပြု၍ မရပါခင်ဗျာ")

    try:
        from app.services.downloader import MediaDownloader
        downloader = MediaDownloader()
        # yt-dlp metadata fetch is synchronous network work; running it
        # on the event loop froze the bot + Mini App for seconds on
        # every extract (deep-hunt #4 D1). Same to_thread wrap the
        # neighbouring download endpoints already use.
        info = await asyncio.to_thread(downloader.get_info, canonical_url)
        duration_sec = info.get("duration", 0) or 0
        mins, secs = divmod(int(duration_sec), 60)
        duration_str = f"{mins:02d}:{secs:02d}"

        # Real downloadable formats only (journey audit 2026-11-02):
        # the payload used to claim a fixed MP4/MP3/SRT list for EVERY
        # result, so a generic page with no media formats at all (the
        # audit's example.com case) still rendered working download
        # buttons. The fetch cards are offered only when yt-dlp's own
        # format list carries at least one audio/video stream; the
        # SRT card only when subtitles actually exist.
        raw_formats = (info.get("raw_info") or {}).get("formats") or []
        has_media_formats = any(
            (f.get("vcodec") not in (None, "none"))
            or (f.get("acodec") not in (None, "none"))
            for f in raw_formats
        ) or bool((info.get("raw_info") or {}).get("url"))
        formats_available: list[str] = []
        if has_media_formats:
            formats_available = ["MP4 (Full HD Video)", "MP3 (192kbps Audio)"]
            if info.get("has_subtitles"):
                formats_available.append("SRT (Subtitles)")

        return {
            "success": True,
            "url": canonical_url,
            "platform": info.get("platform") or detect_platform(canonical_url),
            "title": info.get("title", "Video Content"),
            "uploader": info.get("uploader", "Creator"),
            "thumbnail": info.get("thumbnail", ""),
            "width": info.get("width"),
            "height": info.get("height"),
            "estimated_duration": duration_str,
            "duration_seconds": duration_sec,
            "has_subtitles": info.get("has_subtitles", False),
            "metadata_verified": True,
            "formats_available": formats_available,
        }
    except Exception as e:
        logger.warning(f"yt-dlp extract error for {canonical_url}: {e}")
        # Graceful fallback response. NOTE: metadata could not be
        # verified, so has_subtitles must NOT claim True here — the SRT
        # card renders its honest per-item state from this flag, and the
        # fetch endpoint re-checks for real before producing any SRT.
        platform = detect_platform(canonical_url)
        return {
            "success": True,
            "url": canonical_url,
            "platform": platform,
            "title": f"{platform} Content",
            "uploader": "Online Creator",
            "thumbnail": "",
            "estimated_duration": "03:30",
            "has_subtitles": False,
            # Shape parity with the verified payload above: unknown
            # dimensions (None) / unverified duration (0) are explicit,
            # so the client never has to guess at missing keys.
            "width": None,
            "height": None,
            "duration_seconds": 0,
            # metadata_verified=False: the payload above is a graceful
            # placeholder (title/duration invented). The client must
            # not present it as verified fact (journey audit
            # 2026-10-31 — the "✅ Ready" + fake 03:30 card sent users
            # into jobs pre-doomed at sources that block downloads).
            "metadata_verified": False,
            # No formats were extracted, so none may be offered: the
            # old invented MP4/MP3/SRT list made the Downloader render
            # working download buttons for unverifiable links (journey
            # audit 2026-11-02). The client routes an empty list (or
            # metadata_verified=False) to the persistent error box.
            "formats_available": [],
        }


# ── 4.1. Downloader format delivery (Video / MP3 / SRT cards) ──────────
# The Downloader tab's format cards used to be toast-only placeholders.
# A card tap now runs a real fetch: the existing MediaDownloader pulls
# the file into the persistent downloads store (storage/downloads/<id>)
# with a meta.json carrying the owner; delivery reuses the established
# patterns — ownership-checked FileResponse download (404 for anyone
# else, same as _require_job_owner) and on-demand Telegram sending
# (same shape as the voice/recap send-telegram endpoints). Downloads
# stay free: no credit/minute is charged, exactly as extract is today.
_DL_ID_RE = re.compile(r"^[a-f0-9]{12}$")
_DL_FORMATS = ("video", "mp3", "srt")
_DL_KEEP_PER_USER = 10
_DL_TELEGRAM_LIMIT = 50 * 1024 * 1024  # Telegram Bot API send cap


def _dl_root() -> Path:
    return settings.storage_root / "downloads"


def _dl_dir(download_id: str) -> Path:
    return _dl_root() / download_id


def _dl_read_meta(download_id: str) -> Optional[Dict[str, Any]]:
    if not download_id or not _DL_ID_RE.match(download_id):
        return None
    try:
        meta = json.loads((_dl_dir(download_id) / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    return meta if isinstance(meta, dict) else None


def _dl_require_owner(download_id: str, user: User) -> Dict[str, Any]:
    """Return the download meta only for its owner (or an admin).

    404 for unknown ids and for other users' downloads, matching the
    job-download ownership semantics (no existence probing).
    """
    meta = _dl_read_meta(download_id)
    if not meta or (meta.get("user_id") != user.id and not settings.is_admin(user.id)):
        raise HTTPException(status_code=404, detail="Download not found")
    return meta


def _dl_display_name(meta: Dict[str, Any]) -> str:
    fmt = meta.get("format")
    dl_id = str(meta.get("download_id") or "")[:8]
    return {"video": f"video_{dl_id}.mp4", "mp3": f"audio_{dl_id}.mp3",
            "srt": f"subtitles_{dl_id}.srt"}.get(fmt, f"file_{dl_id}")


def _dl_prune_user_downloads(user_id: int) -> None:
    """Keep only the newest _DL_KEEP_PER_USER download dirs per user."""
    try:
        entries = []
        for d in _dl_root().iterdir():
            if not d.is_dir():
                continue
            meta = _dl_read_meta(d.name)
            if meta and meta.get("user_id") == user_id:
                entries.append((str(meta.get("created_at") or ""), d))
        entries.sort(key=lambda e: e[0])
        for _, d in entries[: max(0, len(entries) - _DL_KEEP_PER_USER)]:
            shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


class DlFetchRequest(BaseModel):
    url: str
    format: str


@api_router.post("/downloader/fetch")
async def fetch_downloader_format(
    req: DlFetchRequest,
    user: User = Depends(get_current_user),
    _gate: User = Depends(require_gate_verified),
):
    """Fetch one Downloader format (video/mp3/srt) for a pasted link.

    Runs the existing MediaDownloader server-side and stores the result
    for delivery. SRT is produced ONLY from subtitles the source
    actually exposes (native or auto captions via yt-dlp) — the AI
    transcription fallback is deliberately off here, and a source with
    no obtainable subtitles gets an honest 422 the UI renders per-item.
    """
    fmt = (req.format or "").strip().lower()
    if fmt not in _DL_FORMATS:
        raise HTTPException(status_code=400, detail="Unknown format")

    # Batch AJ M1: downloads write real bytes to the same disk.
    _require_disk_headroom()

    from app.services.downloader import MediaDownloader, extract_first_url, unshorten_url

    raw_url = req.url.strip()
    url = extract_first_url(raw_url) or raw_url
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="မှန်ကန်တဲ့ Video URL ထည့်သွင်းပေးပါ")
    try:
        # Same event-loop fix as the extract endpoint (deep-hunt #4 D1):
        # redirect-following is synchronous network work.
        canonical_url = await asyncio.to_thread(unshorten_url, url)
        from app.services.url_safety import validate_public_url

        canonical_url = validate_public_url(canonical_url)
    except Exception:
        raise HTTPException(status_code=400, detail="ဒီလင့်ခ်ကို အသုံးပြု၍ မရပါခင်ဗျာ")

    download_id = uuid.uuid4().hex[:12]
    dest = _dl_dir(download_id)
    dest.mkdir(parents=True, exist_ok=True)
    downloader = MediaDownloader()
    try:
        if fmt == "video":
            file_path, info = await asyncio.to_thread(
                downloader.download_video, canonical_url, str(dest)
            )
        elif fmt == "mp3":
            # yt-dlp's FFmpegExtractAudio postprocessor: ffmpeg-side
            # audio extraction to 192k MP3, same as the recap pipeline.
            file_path, info = await asyncio.to_thread(
                downloader.download_audio, canonical_url, str(dest)
            )
        else:  # srt — native/auto captions only, NO AI fallback
            srt_files, info = await asyncio.to_thread(
                downloader.download_subtitles, canonical_url, str(dest), "en", False
            )
            if not srt_files:
                shutil.rmtree(dest, ignore_errors=True)
                raise HTTPException(status_code=422, detail="no_subtitles_available")
            file_path = srt_files[0]
    except HTTPException:
        raise
    except Exception as e:
        shutil.rmtree(dest, ignore_errors=True)
        # The raw exception (yt-dlp output) stays in the log only —
        # API callers get a clean localized detail (hunt F11). An
        # exception whose text already IS a curated user message (the
        # downloader's classified source errors and its Douyin/YouTube
        # notices all start with "⚠️", hunt #6) is passed through, so
        # the user learns WHY the source failed — blocked, removed,
        # login-walled — instead of one generic line for every cause.
        logger.warning(f"downloader fetch ({fmt}) failed for {canonical_url}: {e}")
        detail = str(e) if str(e).startswith("⚠️") else "ဒေါင်းလုဒ် မအောင်မြင်ပါ။ လင့်ခ်ကို ပြန်စစ်ပြီး ထပ်ကြိုးစားပါ။"
        raise HTTPException(status_code=400, detail=detail)

    if not file_path or not Path(file_path).is_file():
        shutil.rmtree(dest, ignore_errors=True)
        # Localized (audit fix 2026-11-03): was the English string
        # "Download failed" — the only non-localized detail left in
        # this endpoint. Same wording as the fetch-failure detail
        # above so the client renders one consistent message.
        raise HTTPException(status_code=400, detail="ဒေါင်းလုဒ် မအောင်မြင်ပါ။ လင့်ခ်ကို ပြန်စစ်ပြီး ထပ်ကြိုးစားပါ။")

    meta = {
        "download_id": download_id,
        "user_id": user.id,
        "url": canonical_url,
        "platform": info.get("platform") if isinstance(info, dict) else None,
        "title": (info.get("title") if isinstance(info, dict) else None) or "Video Content",
        "format": fmt,
        "filename": Path(file_path).name,
        "size_bytes": Path(file_path).stat().st_size,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (dest / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    _dl_prune_user_downloads(user.id)

    return {
        "success": True,
        "download_id": download_id,
        "format": fmt,
        "title": meta["title"],
        "filename": _dl_display_name(meta),
        "size_bytes": meta["size_bytes"],
        "download_url": f"/api/downloader/file/{download_id}",
        "telegram_sendable": meta["size_bytes"] <= _DL_TELEGRAM_LIMIT,
    }


@api_router.get("/downloader/file/{download_id}")
async def download_downloader_file(
    download_id: str,
    user: User = Depends(get_current_user),
):
    """Ownership-checked download of a fetched Downloader file."""
    meta = _dl_require_owner(download_id, user)
    file_path = _dl_dir(download_id) / str(meta.get("filename") or "")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not ready or no longer available")
    mime = {"video": "video/mp4", "mp3": "audio/mpeg",
            "srt": "text/plain; charset=utf-8"}.get(meta.get("format"), "application/octet-stream")
    return FileResponse(
        path=str(file_path),
        media_type=mime,
        filename=_dl_display_name(meta),
        content_disposition_type="attachment",
    )


@api_router.post("/downloader/{download_id}/send-telegram")
async def send_downloader_to_telegram(
    download_id: str,
    user: User = Depends(get_current_user),
    _gate: User = Depends(require_gate_verified),
):
    """Send a fetched Downloader file to the owner's Telegram chat."""
    meta = _dl_require_owner(download_id, user)
    file_path = _dl_dir(download_id) / str(meta.get("filename") or "")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="ဖိုင် အဆင်သင့်မဖြစ်သေးပါ သို့မဟုတ် ရှာမတွေ့ပါ။")
    if file_path.stat().st_size > _DL_TELEGRAM_LIMIT:
        raise HTTPException(status_code=413, detail="file_too_large_telegram")

    from aiogram import Bot
    from aiogram.types import FSInputFile

    bot = Bot(token=settings.bot_token)
    try:
        caption = f"📥 <b>{html.escape(str(meta.get('title') or 'Video Content'))}</b>"
        upload = FSInputFile(str(file_path), filename=_dl_display_name(meta))
        if meta.get("format") == "video":
            await bot.send_video(chat_id=user.id, video=upload, caption=caption, parse_mode="HTML")
        elif meta.get("format") == "mp3":
            await bot.send_audio(chat_id=user.id, audio=upload, caption=caption, parse_mode="HTML")
        else:
            await bot.send_document(chat_id=user.id, document=upload, caption=caption, parse_mode="HTML")
        return {"success": True, "message": "Telegram ဆီ ဖိုင်ပို့ပြီးပါပြီ!"}
    except Exception as e:
        logger.warning(f"Failed to send downloader file {download_id} to {user.id}: {e}")
        raise HTTPException(status_code=500, detail="Telegram ဆီ ပေးပို့၍ မရပါ — ခဏနေ ပြန်စမ်းကြည့်ပါ။")
    finally:
        await bot.session.close()


@api_router.post("/recap/upload_video")
async def upload_recap_video(
    video_file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    _gate: User = Depends(require_gate_verified),
):
    """Directly upload a video file (MP4/MKV/MOV/WEBM) for Movie Recap."""
    if not video_file.filename:
        raise HTTPException(status_code=400, detail="ဗီဒီယိုဖိုင် ရွေးချယ်ပေးပါ")

    # Batch AJ M1: refuse the upload up front when the disk is nearly
    # full instead of writing up to 1 GB into a dying filesystem.
    _require_disk_headroom()

    clean_name = Path(video_file.filename).name
    ext = Path(clean_name).suffix.lower()
    if ext not in {".mp4", ".mkv", ".mov", ".avi", ".webm", ".ts", ".m4v"}:
        raise HTTPException(status_code=400, detail="MP4, MKV, MOV သို့မဟုတ် WEBM ဗီဒီယိုဖိုင်သာ လက်ခံပါသည်")

    temp_dir = settings.storage_root / "temp" / "recap_uploads"
    temp_dir.mkdir(parents=True, exist_ok=True)

    file_id = f"up_{uuid.uuid4().hex[:10]}"
    saved_path = temp_dir / f"{file_id}{ext}"

    # Streamed, 1 GB cap (polish reference cap): never buffer the whole
    # video in memory. _save_upload_streaming raises 413 and deletes the
    # partial file when the cap is exceeded.
    await _save_upload_streaming(video_file, saved_path, MAX_RECAP_UPLOAD_BYTES)

    # Inspect media duration & metadata and extract the preview
    # thumbnail. These are blocking ffprobe/ffmpeg subprocess calls:
    # run them off the event loop (upload-ux pass) so one upload's
    # processing phase cannot stall every other request. Behaviour and
    # response shape are unchanged.
    from app.services.downloader import probe_local_media, extract_thumbnail_from_media, format_duration, find_best_preview_timestamp

    def _probe_and_thumb():
        meta_local = probe_local_media(str(saved_path))
        best_ts_local = find_best_preview_timestamp(str(saved_path), target_dir=str(temp_dir))
        thumb_path_local = temp_dir / f"{file_id}_thumb.jpg"
        extract_thumbnail_from_media(str(saved_path), str(thumb_path_local), timestamp_sec=best_ts_local)
        return meta_local, best_ts_local, thumb_path_local

    meta, best_ts, thumb_path = await asyncio.to_thread(_probe_and_thumb)
    dur_sec = meta.get("duration", 0.0) or 0.0

    thumb_url = f"/api/recap/frame?path={file_id}_thumb.jpg" if thumb_path.is_file() else ""
    display_title = Path(clean_name).stem or "Uploaded Movie Video"
    # Batch AJ L18: bounded registries (oldest evicted past the cap).
    _remember_bounded(UPLOADED_VIDEO_TITLES, str(saved_path), display_title)
    _remember_bounded(UPLOADED_VIDEO_TITLES, saved_path.name, display_title)
    _remember_bounded(UPLOADED_VIDEO_OWNERS, str(saved_path), user.id)
    _remember_bounded(UPLOADED_VIDEO_OWNERS, str(saved_path.resolve()), user.id)
    _remember_bounded(UPLOADED_VIDEO_OWNERS, saved_path.name, user.id)
    _remember_bounded(UPLOADED_VIDEO_OWNERS, thumb_path.name, user.id)

    return {
        "success": True,
        "file_id": file_id,
        "local_path": str(saved_path),
        "filename": clean_name,
        "title": display_title,
        "width": meta.get("width"),
        "height": meta.get("height"),
        "duration_seconds": int(round(dur_sec)),
        "estimated_duration": format_duration(int(round(dur_sec))),
        "thumbnail": thumb_url,
        "platform": "Uploaded Video",
        "has_subtitles": True,
        # Batch DF: the probe already knows whether the file carries
        # an audio stream — surface it so the app can warn at upload
        # time instead of after a doomed submit.
        "has_audio": bool(meta.get("has_audio")),
        "preview_ts": best_ts,
    }


@api_router.post("/recap/upload_voice_ref")
async def upload_recap_voice_ref(
    reference_audio: UploadFile = File(...),
    user: User = Depends(get_current_user),
    _gate: User = Depends(require_gate_verified),
):
    """Batch BV: upload an own-voice narration sample for ONE recap
    job. The recap submit itself is JSON, so the sample is uploaded
    first and referenced by the returned handle (``ref_id``).

    Save machinery is the Voice tab's (Batch AJ H3): streamed to
    disk with the 50 MB cap enforced DURING the write, behind the
    disk-headroom gate, into the same ``temp/voxcpm_refs`` store.
    The filename embeds the owner id (``webapp_ref_u<uid>_<uuid12>``)
    so submit-time ownership validation is a prefix check — no
    registry table. Purchased + admin only (Option 1): own-voice is
    clone narration, so the upload itself carries the Batch AW tier
    gate — free users get the VIP upsell and nothing is stored.
    """
    if not _can_use_custom_logo(user):
        raise HTTPException(status_code=403, detail=RECAP_OWN_VOICE_VIP_ONLY_DETAIL)
    if not reference_audio.filename:
        raise HTTPException(status_code=400, detail="အသံနမူနာဖိုင် ရွေးချယ်ပေးပါ")

    _require_disk_headroom()
    ref_dir = settings.storage_root / "temp" / "voxcpm_refs"
    ref_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(reference_audio.filename).suffix or ".wav"
    clean_ext = ext if ext.lower() in VOXCPM_REF_AUDIO_EXTS else ".wav"
    ref_id = f"webapp_ref_u{user.id}_{uuid.uuid4().hex[:12]}"
    save_file = ref_dir / f"{ref_id}{clean_ext}"
    try:
        saved_bytes = await _save_upload_streaming(
            reference_audio, save_file, 50 * 1024 * 1024
        )
    except HTTPException as exc:
        if exc.status_code == 413:
            raise HTTPException(
                status_code=413,
                detail="အသံနမူနာဖိုင် အရွယ်အစား 50 MB ထက် ကျော်လွန်နေပါတယ်",
            )
        raise
    if saved_bytes <= 100:
        # Same ≤100-byte discard as the voice route — an empty stub
        # is not a usable sample, and here the upload IS the request,
        # so answer honestly instead of proceeding without a ref.
        save_file.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail="အသံနမူနာဖိုင် မမှန်ကန်ပါ — ဖိုင် အလွတ် ဖြစ်နေပါသည်။",
        )
    return {
        "success": True,
        "ref_id": ref_id,
        "filename": Path(reference_audio.filename).name,
        "size_bytes": saved_bytes,
    }


@api_router.post("/recap/upload_sub_font")
async def upload_recap_sub_font(
    font_file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    _gate: User = Depends(require_gate_verified),
):
    """Batch DD: upload a custom subtitle font (.ttf/.otf, ≤5 MB)
    for recap jobs. Mirrors upload_voice_ref: streamed save behind
    the disk-headroom gate, owner-embedded handle. The file is
    validated by actually loading it with Pillow's font loader —
    a file the PNG renderer cannot load is rejected here instead
    of failing (or silently falling back) at render time."""
    if not font_file.filename:
        raise HTTPException(status_code=400, detail="Font ဖိုင် ရွေးချယ်ပေးပါ")
    ext = Path(font_file.filename).suffix.lower()
    if ext not in RECAP_SUB_FONT_EXTS:
        raise HTTPException(status_code=400, detail="TTF သို့မဟုတ် OTF font ဖိုင်သာ တင်နိုင်ပါတယ်")
    _require_disk_headroom()
    font_dir = settings.storage_root / "temp" / "sub_fonts"
    font_dir.mkdir(parents=True, exist_ok=True)
    font_id = f"webapp_font_u{user.id}_{uuid.uuid4().hex[:12]}"
    save_file = font_dir / f"{font_id}{ext}"
    try:
        saved_bytes = await _save_upload_streaming(
            font_file, save_file, RECAP_SUB_FONT_MAX_BYTES
        )
    except HTTPException as exc:
        if exc.status_code == 413:
            raise HTTPException(
                status_code=413,
                detail="Font ဖိုင် အရွယ်အစား 5 MB ထက် ကျော်လွန်နေပါတယ်",
            )
        raise
    try:
        from PIL import ImageFont

        ImageFont.truetype(str(save_file), 24)
    except Exception:
        save_file.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail="Font ဖိုင် မမှန်ကန်ပါ — ဖတ်ယူ၍ မရပါ။ အခြား font ဖိုင် ပြောင်းစမ်းကြည့်ပါ။",
        )
    return {
        "success": True,
        "font_id": font_id,
        "filename": Path(font_file.filename).name,
        "size_bytes": saved_bytes,
    }


async def _recap_frame_owner_id(db: AsyncSession, resolved_path: Path) -> Optional[int]:
    """Resolve which user owns the file a /recap/frame request targets.

    Ownership sources, in order:
    1. the upload registry (files uploaded via /recap/upload_video —
       including a job's uploaded source after a restart, via source 3);
    2. the recap job whose output directory contains the file
       (storage/recap_output/<job_id>/...), via its DB row or the
       in-memory registry;
    3. any job whose recorded source file this is (Job.source_audio_path).

    Returns None when no owner can be established — callers must deny
    non-admins in that case rather than serve an unowned file.
    """
    owner = UPLOADED_VIDEO_OWNERS.get(str(resolved_path))
    if owner is None:
        owner = UPLOADED_VIDEO_OWNERS.get(resolved_path.name)
    if owner is not None:
        return owner

    res_storage = settings.storage_root.resolve()
    try:
        rel = resolved_path.relative_to(res_storage)
    except ValueError:
        rel = None

    if rel is not None and len(rel.parts) >= 2 and rel.parts[0] == "recap_output":
        job = await db.get(Job, rel.parts[1])
        if job is not None:
            return job.user_id
        entry = RECAP_JOBS_REGISTRY.get(rel.parts[1])
        if entry is not None:
            return entry.get("user_id")

    # Job.source_audio_path stores the path as submitted (unresolved),
    # so match both the resolved and the storage-relative spellings.
    candidates = {str(resolved_path)}
    if rel is not None:
        candidates.add(str(settings.storage_root / rel))
    res = await db.execute(
        select(Job.user_id).where(Job.source_audio_path.in_(candidates))
    )
    return res.scalars().first()


@api_router.get("/recap/frame")
async def get_recap_frame(
    path: str = Query(...),
    t: Optional[float] = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """Serve or dynamically generate video frame snapshot at timestamp t with caching and security containment."""
    clean_p = Path(path).name
    # Search in temp uploads dir
    cand = settings.storage_root / "temp" / "recap_uploads" / clean_p
    direct_thumb = cand.is_file() and t is None

    # If t is given and path points to an uploaded or local video file
    actual_path = cand
    if not actual_path.is_file():
        cand_path = Path(path)
        actual_path = cand_path if cand_path.is_absolute() else (settings.storage_root / cand_path)

    if not actual_path.is_file():
        raise HTTPException(status_code=404, detail="Frame source file not found")

    # Security check: Ensure file is contained within configured storage root
    try:
        res_actual = actual_path.resolve()
        res_storage = settings.storage_root.resolve()
        if not str(res_actual).startswith(str(res_storage)):
            raise HTTPException(status_code=403, detail="Access denied")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=403, detail="Access denied")

    # Ownership check (security fix 2026-10-31): containment alone let
    # ANY signed-in user extract frames from any file in storage —
    # other users' uploads and job outputs included. Resolve the file
    # to its owner and apply the same owner-or-admin rule (with the
    # same 404-style denial) as the job download endpoints.
    if not settings.is_admin(user.id):
        owner_id = await _recap_frame_owner_id(db, res_actual)
        if owner_id is None or owner_id != user.id:
            raise HTTPException(status_code=404, detail="Frame not found")

    if direct_thumb:
        return FileResponse(cand, media_type="image/jpeg")

    if actual_path.is_file():
        seek_sec = max(0.1, float(t or 1.0))
        # Deterministic cache key based on file modification time and seek timestamp
        import hashlib
        mtime = actual_path.stat().st_mtime
        cache_key = hashlib.md5(f"{clean_p}_{mtime}_{seek_sec:.1f}".encode()).hexdigest()[:12]
        out_thumb = settings.storage_root / "temp" / f"frame_{cache_key}.jpg"

        if out_thumb.is_file() and out_thumb.stat().st_size > 500:
            return FileResponse(out_thumb, media_type="image/jpeg")

        from app.services.downloader import extract_thumbnail_from_media
        res = extract_thumbnail_from_media(str(actual_path), str(out_thumb), timestamp_sec=seek_sec)
        if res and Path(res).is_file():
            return FileResponse(res, media_type="image/jpeg")

    raise HTTPException(status_code=404, detail="Frame not found")


# ── 4.05. Recap Live Canvas — server-grabbed frames for URL sources ──
# R40 item 9: for link sources the Live Canvas could only ever show
# the remote thumbnail — no real frame, and nothing at the seek
# position (a 2026-11-02 hands-on audit found the canvas blank in
# every URL case). This endpoint downloads the source ONCE into a
# per-URL preview cache (the same MediaDownloader the render itself
# uses, capped at 720p) and serves real frames at arbitrary
# timestamps through the same extractor as the upload flow. Blocked
# sources fail honestly with the classified message — the client
# then keeps its CSS mockup instead of faking a frame.
_PREVIEW_SOURCE_LOCKS: Dict[str, asyncio.Lock] = {}


class RecapPreviewFrameRequest(BaseModel):
    url: str
    t: float = 5.0


@api_router.post("/recap/preview-frame")
async def recap_preview_frame(
    req: RecapPreviewFrameRequest,
    user: User = Depends(get_current_user),
    _gate: User = Depends(require_gate_verified),
):
    from app.services.downloader import (
        MediaDownloader,
        classify_source_error,
        extract_first_url,
        extract_thumbnail_from_media,
        unshorten_url,
    )

    raw_url = (req.url or "").strip()
    url = extract_first_url(raw_url) or raw_url
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="မှန်ကန်တဲ့ Video URL ထည့်သွင်းပေးပါ")
    try:
        canonical_url = await asyncio.to_thread(unshorten_url, url)
        from app.services.url_safety import validate_public_url

        canonical_url = validate_public_url(canonical_url)
    except Exception:
        raise HTTPException(status_code=400, detail="ဒီလင့်ခ်ကို အသုံးပြု၍ မရပါခင်ဗျာ")

    import hashlib

    url_key = hashlib.md5(canonical_url.encode("utf-8")).hexdigest()[:16]
    cache_dir = settings.storage_root / "temp" / "recap_preview_src" / url_key
    cache_dir.mkdir(parents=True, exist_ok=True)
    seek_sec = max(0.1, min(float(req.t or 5.0), 3600.0))
    frame_path = cache_dir / f"frame_{seek_sec:.1f}.jpg"
    if frame_path.is_file() and frame_path.stat().st_size > 500:
        return FileResponse(frame_path, media_type="image/jpeg")

    # Batch AJ L18: bound the per-URL lock registry too (evicting a
    # lock only loses mutual exclusion for an idle URL, never data).
    if url_key not in _PREVIEW_SOURCE_LOCKS:
        _remember_bounded(_PREVIEW_SOURCE_LOCKS, url_key, asyncio.Lock(), cap=512)
    lock = _PREVIEW_SOURCE_LOCKS[url_key]
    async with lock:
        # Re-check inside the lock: a concurrent request may have
        # produced this exact frame while we waited.
        if frame_path.is_file() and frame_path.stat().st_size > 500:
            return FileResponse(frame_path, media_type="image/jpeg")
        source_files = [
            p for p in cache_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".ts"}
        ]
        source_path = source_files[0] if source_files else None
        if source_path is None:
            downloader = MediaDownloader()
            try:
                downloaded, _info = await asyncio.to_thread(
                    downloader.download_video,
                    canonical_url,
                    target_dir=str(cache_dir),
                    max_height=720,
                )
            except Exception as e:
                logger.warning(f"preview-frame download failed for {canonical_url}: {e}")
                detail = classify_source_error(str(e), canonical_url) or (
                    "⚠️ ဒီ လင့်ခ်မှ နမူနာပုံ မရယူနိုင်ပါ။ ဗီဒီယိုဖိုင်ကို Mini App ရှိ "
                    "'📁 Video Upload' မှ တိုက်ရိုက် တင်၍ ပြုလုပ်နိုင်ပါသည် ခင်ဗျာ။"
                )
                raise HTTPException(status_code=422, detail=detail)
            source_path = Path(downloaded)
        res = await asyncio.to_thread(
            extract_thumbnail_from_media, str(source_path), str(frame_path), timestamp_sec=seek_sec
        )
        if res and Path(res).is_file():
            return FileResponse(res, media_type="image/jpeg")
    raise HTTPException(status_code=404, detail="Frame not found")


# ── 4.06. Recap composited preview (Batch AK — preview unification) ──
# The Mini App's Live Canvas used to re-draw the frame with a
# hand-mirrored JS/CSS approximation of the engine: three of the
# seven subtitle styles (band / heavy / soft) had no CSS rendering
# at all, the plate sizing was a second implementation, and the
# title badge never appeared. This endpoint serves the ENGINE's own
# composited preview (the same generate_recap_preview the bot flow
# uses) for the Mini App's exact settings, so what the user sees is
# what the render burns. Source handling mirrors /recap/preview-
# frame (per-URL cache, 720p cap) and /recap/frame (containment +
# owner-or-admin) — no new trust surface.
class RecapBlurBoxIn(BaseModel):
    """Batch DD: one manual blur box in PERCENTS of the picture —
    {x: left, y: CENTRE, w, h} (the legacy single-box basis, so the
    preview editor, this schema and the engine all speak one
    geometry) plus a per-box strength.

    Batch DK: the numeric fields CLAMP into their documented
    ranges instead of rejecting (the blur_strength precedent).
    The box list is a saved preference (recapBlurBoxesJson is an
    opaque string at the prefs layer), so an out-of-range stored
    box must degrade to the nearest supported geometry rather
    than 422 every submit/preview for that account."""

    x: float = Field(...)
    y: float = Field(...)
    w: float = Field(...)
    h: float = Field(...)
    strength: int = 18

    @field_validator("x", "y")
    @classmethod
    def _clamp_box_pos(cls, v) -> float:
        return max(0.0, min(100.0, float(v)))

    @field_validator("w", "h")
    @classmethod
    def _clamp_box_size(cls, v) -> float:
        return max(1.0, min(100.0, float(v)))

    @field_validator("strength")
    @classmethod
    def _clamp_box_strength(cls, v) -> int:
        try:
            strength = int(v)
        except (TypeError, ValueError):
            return 18
        return max(4, min(40, strength))

    def to_fraction_dict(self) -> Dict[str, Any]:
        return {
            "x": float(self.x) / 100.0,
            "y": float(self.y) / 100.0,
            "w": float(self.w) / 100.0,
            "h": float(self.h) / 100.0,
            "strength": int(self.strength),
        }


class RecapPreviewCompositeRequest(BaseModel):
    url: Optional[str] = None
    uploaded_path: Optional[str] = None
    t: float = 5.0
    # The settings subset the composite depends on; field names and
    # bounds mirror RecapConfigRequest so a stored config / submit
    # payload maps 1:1.
    ratio: str = "9:16"
    ratio_fill: str = "blur"
    crop_top: int = Field(0, ge=0, le=35)
    crop_bottom: int = Field(0, ge=0, le=35)
    crop_left: int = Field(0, ge=0, le=35)
    crop_right: int = Field(0, ge=0, le=35)
    mirror_video: bool = True
    zoom_factor: float = 1.0
    blur_subtitles: bool = True
    blur_y: int = Field(85, ge=0, le=100)
    blur_h: int = Field(7, ge=1, le=50)
    blur_x: Optional[int] = Field(None, ge=0, le=100)
    blur_w: Optional[int] = Field(None, ge=10, le=100)
    blur_box_explicit: bool = False
    blur_strength: int = 18
    # Batch BI: the auto-detect blur switch, mirrored from
    # RecapConfigRequest so the composite shows what will ship.
    blur_auto_detect: bool = True
    burn_subtitles: bool = True
    sub_y: int = Field(85, ge=0, le=100)
    sub_bottom_margin: Optional[int] = Field(None, ge=0, le=95)
    sub_x: Optional[int] = Field(None, ge=10, le=90)
    sub_w: Optional[int] = Field(None, ge=10, le=100)
    # Batch DD: Lines 1/2/3; the size slider runs 10–26pt (stored
    # values up to 30 still parse).
    sub_lines: int = Field(1, ge=1, le=3)
    sub_font_size: int = Field(16, ge=10, le=30)
    sub_style: str = "yellow"
    sub_font: str = "padauk"
    sample_text: Optional[str] = Field(None, max_length=300)
    # Batch DD: the title overlay feature is REMOVED — no title
    # fields (legacy clients' title keys are ignored by pydantic).
    # Manual blur-box list (≤5), protection level, free style
    # controls and the custom-font handle mirror RecapConfigRequest
    # so the composite shows exactly what the render will burn.
    blur_boxes: Optional[List[RecapBlurBoxIn]] = Field(None, max_length=5)
    protection_level: str = "standard"
    sub_text_color: Optional[str] = None
    sub_outline_color: Optional[str] = None
    # Batch DK: ge=-1 — the prefs layer's documented sentinel for
    # "the style preset's own outline" (USER_PREF_FLOATS allows
    # [-1, 4], the shipped default is -1.0, and the client forwards
    # the saved value verbatim). The validator below normalises the
    # sentinel to None, so it never reaches a downstream consumer;
    # values in [0, 4] pass through untouched, < -1 still 422s.
    sub_outline_width: Optional[float] = Field(None, ge=-1, le=4)
    sub_bg_color: Optional[str] = None
    sub_bg_opacity: Optional[int] = Field(None, ge=0, le=100)
    sub_shadow: Optional[bool] = None
    sub_bold: bool = True
    sub_font_custom_id: Optional[str] = Field(None, max_length=80)
    include_logo: bool = True
    logo_pos: str = "top-right"

    @field_validator("sub_outline_width")
    @classmethod
    def _norm_sub_outline_width(cls, v):
        # Batch DK: -1 == "the preset's own" == field absent.
        if v is not None and float(v) == -1.0:
            return None
        return v


@api_router.post("/recap/preview-composite")
async def recap_preview_composite(
    req: RecapPreviewCompositeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    _gate: User = Depends(require_gate_verified),
):
    from app.services.downloader import (
        MediaDownloader,
        classify_source_error,
        extract_first_url,
        unshorten_url,
    )

    seek_sec = max(0.1, min(float(req.t or 5.0), 3600.0))
    source_path: Optional[Path] = None

    if req.uploaded_path:
        cand_path = Path(req.uploaded_path)
        actual_path = cand_path if cand_path.is_absolute() else (settings.storage_root / cand_path)
        if not actual_path.is_file():
            raise HTTPException(status_code=404, detail="Frame not found")
        try:
            res_actual = actual_path.resolve()
            res_storage = settings.storage_root.resolve()
            if not str(res_actual).startswith(str(res_storage)):
                raise HTTPException(status_code=403, detail="Access denied")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=403, detail="Access denied")
        if not settings.is_admin(user.id):
            owner_id = await _recap_frame_owner_id(db, res_actual)
            if owner_id is None or owner_id != user.id:
                raise HTTPException(status_code=404, detail="Frame not found")
        source_path = res_actual
    elif req.url:
        raw_url = (req.url or "").strip()
        url = extract_first_url(raw_url) or raw_url
        if not url.startswith("http"):
            raise HTTPException(status_code=400, detail="မှန်ကန်တဲ့ Video URL ထည့်သွင်းပေးပါ")
        try:
            canonical_url = await asyncio.to_thread(unshorten_url, url)
            from app.services.url_safety import validate_public_url

            canonical_url = validate_public_url(canonical_url)
        except Exception:
            raise HTTPException(status_code=400, detail="ဒီလင့်ခ်ကို အသုံးပြု၍ မရပါခင်ဗျာ")

        import hashlib

        url_key = hashlib.md5(canonical_url.encode("utf-8")).hexdigest()[:16]
        cache_dir = settings.storage_root / "temp" / "recap_preview_src" / url_key
        cache_dir.mkdir(parents=True, exist_ok=True)
        if url_key not in _PREVIEW_SOURCE_LOCKS:
            _remember_bounded(_PREVIEW_SOURCE_LOCKS, url_key, asyncio.Lock(), cap=512)
        lock = _PREVIEW_SOURCE_LOCKS[url_key]
        async with lock:
            source_files = [
                p for p in cache_dir.iterdir()
                if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".ts"}
            ]
            source_path = source_files[0] if source_files else None
            if source_path is None:
                downloader = MediaDownloader()
                try:
                    downloaded, _info = await asyncio.to_thread(
                        downloader.download_video,
                        canonical_url,
                        target_dir=str(cache_dir),
                        max_height=720,
                    )
                except Exception as e:
                    logger.warning(f"preview-composite download failed for {canonical_url}: {e}")
                    detail = classify_source_error(str(e), canonical_url) or (
                        "⚠️ ဒီလင့်ခ်ကနေ နမူနာပုံ မရယူနိုင်ပါ။ ဗီဒီယိုဖိုင်ကို Mini App ထဲက "
                        "'📁 Video Upload' ကနေ တိုက်ရိုက်တင်ပြီး ပြုလုပ်နိုင်ပါတယ်။"
                    )
                    raise HTTPException(status_code=422, detail=detail)
                source_path = Path(downloaded)
    else:
        raise HTTPException(status_code=400, detail="မှန်ကန်တဲ့ Video URL ထည့်သွင်းပေးပါ")

    is_vip = (user.account_category == AccountCategory.PURCHASED.value) or settings.is_admin(user.id)
    logo_path_arg = _resolve_recap_logo_path(user.id, req.include_logo, is_vip)
    logo_pos_map = {
        "top-right": [0.82, 0.04],
        "top-left": [0.04, 0.04],
        "bottom-right": [0.82, 0.88],
        "bottom-left": [0.04, 0.88],
        "top-center": [0.42, 0.04],
    }
    logo_pos_coords = logo_pos_map.get(req.logo_pos, [0.82, 0.04])

    # Batch DD: resolve the custom-font handle up front (ownership
    # + existence), like the logo path above — a bad handle is a
    # clean 400 before any rendering starts.
    sub_font_custom_path_arg = (
        str(_resolve_recap_sub_font(req.sub_font_custom_id, user))
        if req.sub_font_custom_id else None
    )

    out_dir = settings.storage_root / "temp" / "recap_preview_composite"
    out_dir.mkdir(parents=True, exist_ok=True)
    downloader = MediaDownloader()
    try:
        out_img = await asyncio.to_thread(
            downloader.generate_recap_preview,
            str(source_path),
            target_dir=str(out_dir),
            blur_subtitles=req.blur_subtitles,
            blur_y=float(req.blur_y) / 100.0,
            blur_h=float(req.blur_h) / 100.0,
            blur_x=(float(req.blur_x) / 100.0) if req.blur_x is not None else None,
            blur_w=(float(req.blur_w) / 100.0) if req.blur_w is not None else None,
            blur_box_explicit=bool(req.blur_box_explicit),
            manual_blur_boxes=_dd_blur_boxes_fractions(req),
            blur_strength=req.blur_strength,
            blur_auto_detect=bool(req.blur_auto_detect),
            burn_subtitles=req.burn_subtitles,
            sub_y=float(req.sub_y) / 100.0,
            sub_margin=(float(req.sub_bottom_margin) / 100.0) if req.sub_bottom_margin is not None else None,
            sub_x=(float(req.sub_x) / 100.0) if req.sub_x is not None else None,
            sub_w=(float(req.sub_w) / 100.0) if req.sub_w is not None else None,
            sub_lines=req.sub_lines,
            sub_font_size=req.sub_font_size,
            sub_style=req.sub_style,
            sub_font=req.sub_font,
            sub_visual_overrides=_dd_overrides_from(req),
            sub_font_custom_path=sub_font_custom_path_arg,
            sub_bold=bool(req.sub_bold),
            protection_level=req.protection_level,
            sample_text=(req.sample_text or ""),
            output_ratio=req.ratio,
            frame_fill=req.ratio_fill,
            crop_top=float(req.crop_top) / 100.0,
            crop_bottom=float(req.crop_bottom) / 100.0,
            crop_left=float(req.crop_left) / 100.0,
            crop_right=float(req.crop_right) / 100.0,
            mirror_video=req.mirror_video,
            zoom_factor=req.zoom_factor,
            at_seconds=seek_sec,
            logo_enabled=bool(logo_path_arg),
            logo_path=logo_path_arg,
            logo_position=logo_pos_coords if logo_path_arg else None,
            is_vip=is_vip,
        )
    except Exception as e:
        logger.warning(f"preview-composite render failed: {e}")
        raise HTTPException(status_code=422, detail="နမူနာပုံ မရယူနိုင်ပါ")
    if out_img and Path(out_img).is_file():
        return FileResponse(out_img, media_type="image/jpeg")
    raise HTTPException(status_code=404, detail="Frame not found")
# ── 5. Recap Studio Configuration & Background Pipeline ─────────────────────
RECAP_JOBS_REGISTRY: Dict[str, Dict[str, Any]] = {}
# Global recap execution cap (deep-hunt #4 D2): the per-user cap (1
# active job) never bounded the SYSTEM — N users submitting together
# meant N simultaneous ffmpeg render chains on the single instance,
# slowing everything to a crawl and risking OOM. At most 2 recap jobs
# execute at once, across EVERY dispatch path (submit, script-render,
# admin retry, bot admin, queue-worker re-dispatch): the semaphore is
# acquired inside _run_recap_background_job itself, so a dispatched
# job whose turn has not come simply waits — still QUEUED in the
# registry, never failed or dropped — until a slot frees.
RECAP_EXECUTION_SEMAPHORE = asyncio.Semaphore(2)
UPLOADED_VIDEO_TITLES: Dict[str, str] = {}
# Upload ownership for /recap/frame (security fix 2026-10-31): maps an
# uploaded file (absolute path, resolved path, and basename — the same
# key shapes UPLOADED_VIDEO_TITLES uses) to the uploading user's id.
# Recap uploads previously had no owner record at all, so the frame
# endpoint could only check storage containment and any signed-in user
# could pull frames from anyone else's upload.
UPLOADED_VIDEO_OWNERS: Dict[str, int] = {}


def _clean_recap_display_title(raw_title: Optional[str], fallback_url: Optional[str] = None) -> str:
    """Sanitize raw local server paths (e.g. /app/storage/temp/recap_uploads/up_xxx.mp4) into clean display titles."""
    for candidate in (raw_title, fallback_url):
        val = str(candidate or "").strip()
        if not val:
            continue
        if val in UPLOADED_VIDEO_TITLES:
            return UPLOADED_VIDEO_TITLES[val]
        base_name = Path(val).name
        if base_name in UPLOADED_VIDEO_TITLES:
            return UPLOADED_VIDEO_TITLES[base_name]
        if "/storage/temp/recap_uploads/" in val.replace("\\", "/") or re.fullmatch(r"up_[0-9a-f]{8,12}\.\w+", base_name):
            stem = Path(base_name).stem
            return f"Uploaded Video ({stem})"
        if not val.startswith("/app/") and not re.match(r"^[A-Za-z]:\\", val):
            cleaned = re.sub(r'#\S+', '', val).strip()
            cleaned = re.sub(r'\s+', ' ', cleaned).strip(' -_,|')
            return cleaned or val
    return "Movie Recap Video"


def _build_clean_recap_script_file(
    job_dir: Path,
    job_id: str,
    story_style: str = "viral",
    srt_path: Optional[str] = None,
) -> Optional[str]:
    """Build a clean, full-sentence Burmese script file from the Smart Sync JSON plan (with SRT fallback)."""
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        script_file = job_dir / f"{job_id}_script.txt"
        plan_candidates = [
            job_dir / "edge_tts_smart_sync" / "smart_sync_plan_edge_tts.json",
            job_dir / "smart_sync_plan_edge_tts.json",
        ]
        timed_lines: List[str] = []
        plain_sentences: List[str] = []

        for plan_file in plan_candidates:
            if plan_file.is_file():
                try:
                    plan_data = json.loads(plan_file.read_text(encoding="utf-8", errors="replace"))
                    timing_segs = plan_data.get("tts_timing_segments") or plan_data.get("segments") or []
                    for seg in timing_segs:
                        txt = str(seg.get("text") or seg.get("narration") or "").strip()
                        if not txt:
                            continue
                        st = float(seg.get("audio_start") if seg.get("audio_start") is not None else seg.get("video_start", 0.0))
                        en = float(seg.get("audio_end") if seg.get("audio_end") is not None else seg.get("video_end", 0.0))
                        st_mm, st_ss = int(st) // 60, int(st) % 60
                        en_mm, en_ss = int(en) // 60, int(en) % 60
                        timed_lines.append(f"[{st_mm:02d}:{st_ss:02d} - {en_mm:02d}:{en_ss:02d}] {txt}")
                        plain_sentences.append(txt)
                    if plain_sentences:
                        break
                except Exception:
                    pass

        if not plain_sentences and srt_path and Path(srt_path).is_file():
            with open(srt_path, "r", encoding="utf-8", errors="replace") as f:
                for l in f:
                    s = l.strip()
                    if s and not s.isdigit() and "-->" not in s and s not in plain_sentences:
                        plain_sentences.append(s)

        if not plain_sentences:
            return None

        header = (
            f"🎬 Pro Movie Recap Script ({story_style.upper()})\n"
            f"🆔 Task: #{job_id}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        )
        full_paragraph = " ".join(plain_sentences)
        body = f"📖 ဇာတ်လမ်းအပြည့်အစုံ (Full Narration Copy):\n{full_paragraph}\n\n"
        if timed_lines:
            body += "⏱️ အခန်းအလိုက် အချိန်နှင့် စာသား (Scene-by-Scene Breakdown):\n" + "\n\n".join(timed_lines) + "\n"
        else:
            body += "📝 စာသားများ:\n" + "\n\n".join(plain_sentences) + "\n"

        script_file.write_text(header + body, encoding="utf-8")
        return str(script_file)
    except Exception as e:
        logger.warning(f"Failed to build clean recap script for {job_id}: {e}")
        return None


def resolve_recap_deliverable_path(
    job_id: str,
    file_type: str,
    result_path: Optional[str] = None,
) -> Optional[str]:
    """Deterministically resolve the final deliverable file for a recap job without matching intermediate work files."""
    entry = RECAP_JOBS_REGISTRY.get(job_id)
    if entry:
        reg_path = entry.get(f"{file_type}_path")
        if reg_path and Path(reg_path).is_file():
            return str(reg_path)

    job_dir = settings.storage_root / "recap_output" / job_id

    _part_match = re.fullmatch(r"part([1-9][0-9]?)", file_type or "")
    if _part_match:
        # Splitter deliverable (registry part{n}_path was checked above).
        part_file = job_dir / "parts" / f"recap_part_{int(_part_match.group(1)):02d}.mp4"
        if part_file.is_file() and part_file.stat().st_size > 1000:
            return str(part_file)
        return None

    if file_type == "video":
        if result_path and Path(result_path).is_file():
            return str(result_path)
        explicit_videos = [
            job_dir / "edge_tts_smart_sync" / "final_edge_tts_smart_sync.mp4",
            job_dir / "final_edge_tts_smart_sync.mp4",
            job_dir / "final_recap.mp4",
        ]
        for cand in explicit_videos:
            if cand.is_file() and cand.stat().st_size > 1000:
                return str(cand)
        if job_dir.is_dir():
            for cand in job_dir.rglob("*.mp4"):
                parts_lower = {p.lower() for p in cand.parts}
                name_lower = cand.name.lower()
                if "video_parts" in parts_lower or "work" in parts_lower:
                    continue
                if name_lower.startswith(("source", "up_", "seg_")) or "noaudio" in name_lower:
                    continue
                if cand.is_file() and cand.stat().st_size > 1000:
                    return str(cand)
        return None

    if file_type == "audio":
        explicit_audios = [
            job_dir / "edge_tts_smart_sync" / "recap_voice_cloned.wav",
            job_dir / "edge_tts_smart_sync" / "voice_edge_tts.wav",
            job_dir / "voice_edge_tts.wav",
            job_dir / "final_audio.mp3",
            job_dir / "final_audio.wav",
        ]
        for cand in explicit_audios:
            if cand.is_file() and cand.stat().st_size > 500:
                return str(cand)
        if job_dir.is_dir():
            for cand in list(job_dir.rglob("*.mp3")) + list(job_dir.rglob("*.wav")):
                parts_lower = {p.lower() for p in cand.parts}
                name_lower = cand.name.lower()
                if "tts_parts" in parts_lower or "work" in parts_lower:
                    continue
                if "whisper" in name_lower or name_lower.startswith("seg_"):
                    continue
                if cand.is_file() and cand.stat().st_size > 500:
                    return str(cand)
        return None

    if file_type == "srt":
        explicit_srts = [
            job_dir / "edge_tts_smart_sync" / "recap_myanmar_synced.srt",
            job_dir / "recap_myanmar_synced.srt",
        ]
        for cand in explicit_srts:
            if cand.is_file():
                return str(cand)
        if job_dir.is_dir():
            srts = [c for c in job_dir.rglob("*.srt") if c.is_file()]
            if srts:
                return str(srts[0])
        return None

    if file_type == "script":
        explicit_scripts = [
            job_dir / f"{job_id}_script.txt",
            job_dir / "edge_tts_smart_sync" / "recap_script_clean.txt",
        ]
        for cand in explicit_scripts:
            if cand.is_file():
                return str(cand)
        if job_dir.is_dir():
            srt_cand = resolve_recap_deliverable_path(job_id, "srt")
            built = _build_clean_recap_script_file(job_dir, job_id, "viral", srt_cand)
            if built and Path(built).is_file():
                return built
            txts = [c for c in job_dir.rglob("*.txt") if c.is_file()]
            if txts:
                return str(txts[0])
        return None

    return None


# Source-language codes offered for recap STT (Whisper language codes).
# "auto" keeps the historical detect-for-me behaviour.
RECAP_SOURCE_LANGUAGE_CODES = frozenset({
    "auto", "en", "zh", "ko", "th", "ja", "hi", "my",
    "es", "fr", "de", "id", "vi", "ru", "ar", "it", "pt", "tr",
})
RECAP_RENDER_QUALITIES = frozenset({"standard", "high", "max"})
# Batch AW: "edge" (Microsoft Edge TTS) is a first-class user-selectable
# recap narration engine alongside voxcpm (clone/persona) and gemini.
RECAP_TTS_ENGINES = frozenset({"voxcpm", "gemini", "edge"})
# Recap narrator slugs the worker still maps even though they are not
# curated personas (legacy Edge female voice). Every other legitimate
# recap voice_slug is a curated persona slug — see create_recap_job.
RECAP_LEGACY_EDGE_VOICE_SLUGS = frozenset({"nilar"})


class RecapConfigRequest(BaseModel):
    # Bounded (hunt #4 A2): the URL feeds yt-dlp and the persisted
    # config; the title feeds display + the persisted config. Real
    # URLs/titles are far below these caps.
    url: str = Field(..., max_length=2048)
    title: Optional[str] = Field(None, max_length=500)
    voice_slug: str = Field("komin", max_length=50)  # Job.preset_voice String(50)
    story_style: str = "viral"        # viral, dramatic, funny, pov, standard
    ratio: str = "9:16"               # Original, 9:16, 16:9, 1:1
    ratio_fill: str = "blur"          # blur, crop, black
    crop_top: int = Field(0, ge=0, le=35)
    crop_bottom: int = Field(0, ge=0, le=35)
    crop_left: int = Field(0, ge=0, le=35)
    crop_right: int = Field(0, ge=0, le=35)
    mirror_video: bool = True
    # Batch AK (premium audit S1): default Micro-Zoom is 1.00 — the
    # old 1.06 default silently cropped ≈11% of the picture area in
    # the Blur/Black fill modes. Explicit client values are
    # honoured exactly as before.
    zoom_factor: float = 1.0
    voice_speed: str = DEFAULT_SPEED   # -6%, +0%, +12%, +20%, +25%
    blur_subtitles: bool = True
    blur_y: int = Field(85, ge=0, le=100)
    blur_h: int = Field(7, ge=1, le=50)
    # Batch AG: horizontal blur-box geometry — percents of the
    # PICTURE width (the blur_y/blur_h basis, rotated). Optional:
    # absent (every pre-AG client and stored config) keeps the
    # legacy centred 92%-of-canvas box byte-identical. blur_w is
    # bounded to [10, 100]; blur_x is CLAMPED against the effective
    # width by the model validator below (the blur_strength
    # precedent) so the box always stays inside the picture instead
    # of failing the submit.
    blur_x: Optional[int] = Field(None, ge=0, le=100)
    blur_w: Optional[int] = Field(None, ge=10, le=100)
    # Batch AA: blur_y/blur_h are percents of the PICTURE height
    # (Batch P's sub_y basis), and this marker records that the user
    # DELIBERATELY set the blur box (slider / drag / resize in the
    # Mini App, or a History prefill of a job that carried it). It
    # rides the persisted recap_config (model_dump) end-to-end: an
    # explicit box is authoritative at render — detection is not
    # consulted for the source band. Absent/False (every pre-AA
    # client and stored config) keeps the legacy detection path.
    blur_box_explicit: bool = False
    # Batch O (DECISION backlog item 5, Ulfa reversed to user-facing):
    # blur strength = boxblur radius over the source's embedded
    # subtitles. Default 18 is the historical hardcoded value; the
    # validator CLAMPS into the engine's 4–40 range instead of
    # rejecting, so an out-of-range client value degrades to the
    # nearest supported strength rather than failing the submit.
    blur_strength: int = 18
    # Batch BI: the user's switch for source-subtitle auto-detection
    # blur (owner request 2026-11-06). Default True — the historical
    # behaviour; False skips the detector entirely, so only the
    # plate / fallback hint / manual box blur. Rides the persisted
    # recap_config end-to-end like blur_box_explicit, so a stored
    # config without the key (every pre-BI job) re-parses to True.
    blur_auto_detect: bool = True
    burn_subtitles: bool = True
    sub_y: int = Field(85, ge=0, le=100)
    # Batch AK (premium audit S3 / Addition D): the subtitle anchor
    # as a BOTTOM-EDGE margin — percent of the picture height from
    # the picture's bottom edge to the subtitle plate's bottom
    # edge (the Recapter standard). Optional like blur_x/sub_x:
    # absent (every pre-AK client and stored config) keeps the
    # legacy centre-% sub_y path byte-identical; present, it wins
    # over sub_y in the engine.
    sub_bottom_margin: Optional[int] = Field(None, ge=0, le=95)
    # Batch AG: subtitle horizontal placement — sub_x is the cue
    # CENTRE as a percent of the picture width (sub_y's centre
    # semantics, rotated; bounded to [10, 90] so a cue can never be
    # centred off-frame), sub_w the wrap/plate width in the same
    # basis. Optional, like blur_x/blur_w: absent keeps today's
    # centred rendering byte-identical.
    sub_x: Optional[int] = Field(None, ge=10, le=90)
    sub_w: Optional[int] = Field(None, ge=10, le=100)
    # Batch DD: the title overlay feature is REMOVED entirely
    # (Batch AH's title_enabled/title_text/title_x/title_y are
    # gone). The model keeps pydantic's default extra="ignore", so
    # a legacy stored config still carrying those keys parses
    # cleanly and the fields are simply dropped.
    # Batch DD: manual blur-box LIST (≤5, percents of the picture).
    # When present it supersedes the legacy single-box fields at
    # render (the pipeline ships it alongside auto-detection); when
    # absent, a legacy blur_box_explicit config migrates to a
    # one-element list at dispatch (_dd_blur_boxes_fractions).
    blur_boxes: Optional[List[RecapBlurBoxIn]] = Field(None, max_length=5)
    # Batch DD: anti-copyright LEVEL — off / standard / strong
    # (validated below; unknown values degrade to "standard").
    protection_level: str = "standard"
    # Batch DD: free subtitle-style controls. Colours are #RRGGBB
    # (invalid → dropped to None by the validator = preset colour);
    # outline_width is in ASS units; bg_opacity is a percent.
    sub_text_color: Optional[str] = None
    sub_outline_color: Optional[str] = None
    # Batch DK: ge=-1 — the prefs layer's documented sentinel for
    # "the style preset's own outline" (USER_PREF_FLOATS allows
    # [-1, 4], the shipped default is -1.0, and the client forwards
    # the saved value verbatim). The validator below normalises the
    # sentinel to None, so it never reaches a downstream consumer;
    # values in [0, 4] pass through untouched, < -1 still 422s.
    sub_outline_width: Optional[float] = Field(None, ge=-1, le=4)
    sub_bg_color: Optional[str] = None
    sub_bg_opacity: Optional[int] = Field(None, ge=0, le=100)
    sub_shadow: Optional[bool] = None
    sub_bold: bool = True
    # Batch DD: custom subtitle font — client handle + SERVER-ONLY
    # resolved path (the voice_ref_id/voice_ref_path pattern:
    # create_recap_job recomputes the path from the handle and
    # overwrites whatever the client sent).
    sub_font_custom_id: Optional[str] = Field(None, max_length=80)
    sub_font_custom_path: Optional[str] = None
    sub_style: str = "yellow"         # 10 presets (Batch DD)
    sub_font_size: int = Field(16, ge=10, le=30)
    sub_lines: int = Field(1, ge=1, le=3)
    sub_font: str = "padauk"          # padauk, noto_sans, pyidaungsu, noto_serif
    include_logo: bool = True
    logo_pos: str = "top-right"       # top-right, top-left, bottom-right, bottom-left, top-center
    duration_seconds: Optional[int] = None
    # Two-phase flow (desktop Recapter "Edit Text" parity): when true, the
    # job stops after transcription + translation with status SCRIPT_READY
    # so the user can review/edit the Burmese narration before rendering.
    script_review: bool = False
    # Production-grade programme (2026-10-29), all optional, defaults keep
    # the previous behaviour exactly:
    # - source_language: STT language hint ("auto" = detect, else a
    #   Whisper language code such as "en", "zh", "ko", "th", "ja").
    source_language: str = "auto"
    # - render_quality: encoder preset for the render (see core
    #   RENDER_QUALITIES): "standard" is the historical default encode.
    render_quality: str = "standard"
    # - tts_engine: narration engine. "voxcpm" (default) keeps the
    #   persona pipeline; "gemini" uses Gemini TTS with per-segment
    #   fallback to the persona engine (fallback stays visible).
    tts_engine: str = "voxcpm"
    gemini_voice: Optional[str] = None
    # Batch BV: own-voice narration. voice_ref_id is the client
    # handle of a sample uploaded via /api/recap/upload_voice_ref
    # (filename stem, ownership embedded). voice_ref_path is
    # SERVER-ONLY: create_recap_job recomputes it from the handle
    # and overwrites whatever the client sent, before anything is
    # persisted or dispatched; internal rebuilds from the persisted
    # config (restart re-dispatch, script-review resume, retry)
    # keep the resolved value. Both ride the persisted recap_config
    # via model_dump, like blur_box_explicit.
    voice_ref_id: Optional[str] = Field(None, max_length=80)
    voice_ref_path: Optional[str] = None
    # - split_video: after a successful render, also cut the finished
    #   video into labelled parts (smart cut at narration pauses) —
    #   that post-split stays included in the job's credit. For an
    #   over-cap source, split_video instead routes the job through
    #   segmented processing, which is charged 1 credit PER PART
    #   (owner decision 2026-10-30; see _top_up_recap_segment_charge).
    split_video: bool = False
    split_minutes: int = Field(3, ge=1, le=10)

    @field_validator("blur_strength")
    @classmethod
    def _clamp_blur_strength(cls, v) -> int:
        try:
            strength = int(v)
        except (TypeError, ValueError):
            return 18
        return max(4, min(40, strength))

    @model_validator(mode="after")
    def _clamp_blur_x(self):
        # Batch AG: keep the blur box inside the picture — blur_x is
        # clamped against the effective width (the submitted blur_w,
        # or the legacy 92 default when blur_w is absent) instead of
        # rejecting the submit.
        if self.blur_x is not None:
            w_eff = self.blur_w if self.blur_w is not None else 92
            self.blur_x = max(0, min(100 - w_eff, int(self.blur_x)))
        return self

    @field_validator("protection_level")
    @classmethod
    def _check_protection_level(cls, v: str) -> str:
        lvl = str(v or "standard").strip().lower()
        return lvl if lvl in ("off", "standard", "strong") else "standard"

    @field_validator("sub_outline_width")
    @classmethod
    def _norm_sub_outline_width(cls, v):
        # Batch DK: -1 == "the preset's own" == field absent (the
        # prefs layer's documented sentinel — see the field comment
        # above). Normalised here so the extras builder, the
        # persisted config and the worker never see the sentinel.
        if v is not None and float(v) == -1.0:
            return None
        return v

    @field_validator("sub_text_color", "sub_outline_color", "sub_bg_color")
    @classmethod
    def _check_hex_color(cls, v):
        # Clamp-never-reject: a malformed colour degrades to the
        # preset's colour (None) instead of failing the submit.
        if v is None:
            return None
        s = str(v).strip()
        return s if _HEX_COLOR_RE.match(s) else None

    @field_validator("voice_speed")
    @classmethod
    def _norm_recap_voice_speed(cls, v) -> str:
        norm = normalize_api_speed(v)
        if norm is None:
            raise ValueError(f"Unsupported voice_speed: {v!r}")
        return norm

    @field_validator("sub_font_custom_id")
    @classmethod
    def _check_sub_font_custom_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not str(v).strip():
            return None
        stem = str(v).strip()
        if not _RECAP_SUB_FONT_ID_RE.match(stem):
            raise ValueError("Unsupported sub_font_custom_id. Upload the font first.")
        return stem

    @field_validator("sub_font")
    @classmethod
    def _check_sub_font(cls, v: str) -> str:
        from app.services.recap_engine.core import resolve_subtitle_font
        key, _ = resolve_subtitle_font(v)
        return key

    @field_validator("source_language")
    @classmethod
    def _check_source_language(cls, v: str) -> str:
        code = str(v or "auto").strip().lower()
        if code in ("", "detect", "automatic"):
            return "auto"
        if code not in RECAP_SOURCE_LANGUAGE_CODES:
            raise ValueError(
                f"Unsupported source_language '{v}'. Use 'auto' or one of: "
                + ", ".join(sorted(RECAP_SOURCE_LANGUAGE_CODES - {"auto"}))
            )
        return code

    @field_validator("render_quality")
    @classmethod
    def _check_render_quality(cls, v: str) -> str:
        q = str(v or "standard").strip().lower()
        if q not in RECAP_RENDER_QUALITIES:
            raise ValueError(f"Unsupported render_quality '{v}'. Use: standard, high, max")
        return q

    @field_validator("tts_engine")
    @classmethod
    def _check_tts_engine(cls, v: str) -> str:
        e = str(v or "voxcpm").strip().lower()
        if e not in RECAP_TTS_ENGINES:
            raise ValueError(f"Unsupported tts_engine '{v}'. Use: voxcpm, gemini, edge")
        return e

    @field_validator("voice_ref_id")
    @classmethod
    def _check_voice_ref_id(cls, v: Optional[str]) -> Optional[str]:
        # Batch BV: the handle is a bare filename stem — never a
        # path. Anything that is not exactly the upload endpoint's
        # stem shape is rejected here; existence + ownership are
        # re-validated at submit (_resolve_recap_voice_ref), before
        # any credit moves.
        if v is None or not str(v).strip():
            return None
        stem = str(v).strip()
        if not _RECAP_VOICE_REF_ID_RE.match(stem):
            raise ValueError("Unsupported voice_ref_id. Upload the sample first.")
        return stem

    @field_validator("gemini_voice")
    @classmethod
    def _check_gemini_voice(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not str(v).strip():
            return None
        name = str(v).strip().removeprefix("gemini:")
        from app.services.recap_engine.gemini_tts_engine import GEMINI_VOICE_NAMES

        if name not in GEMINI_VOICE_NAMES:
            raise ValueError(
                f"Unsupported gemini_voice '{v}'. Use one of: " + ", ".join(GEMINI_VOICE_NAMES)
            )
        return name


class RecapScriptEditItem(BaseModel):
    index: int                        # 1-based segment number from GET script
    narration: str


class RecapScriptUpdateRequest(BaseModel):
    segments: list[RecapScriptEditItem] = []


def _recap_fallback_info(job_dir) -> tuple[bool, list]:
    """Read fallback transparency fields written by the recap engine.

    The engine records ``fallback_used`` / ``fallback_segments`` in the
    final smart-sync plan JSON when one or more narration lines had to
    use Edge TTS after VoxCPM retries were exhausted (fix 2026-10-29).
    """
    try:
        import json as _json
        root = Path(job_dir)
        if not root.is_dir():
            return False, []
        best: list = []
        used = False
        for plan_file in root.rglob("smart_sync_plan*.json"):
            try:
                data = _json.loads(plan_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            segs = data.get("fallback_segments") or []
            if data.get("fallback_used"):
                used = True
                if len(segs) > len(best):
                    best = list(segs)
        return used, best
    except Exception:
        return False, []


def _recap_reference_trimmed(job_dir) -> bool:
    """Batch BV: True when the recap engine's plan JSON records the
    Batch BU reference auto-trim for this job — an own-voice sample
    longer than the 50 s Space cap was cloned from its first 45 s.
    Same on-disk source of truth as _recap_fallback_info; persona
    samples are ≤50 s, so persona jobs always read False."""
    try:
        import json as _json
        root = Path(job_dir)
        if not root.is_dir():
            return False
        for plan_file in root.rglob("smart_sync_plan*.json"):
            try:
                data = _json.loads(plan_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("reference_trimmed"):
                return True
        return False
    except Exception:
        return False


# ── Recap progress stage mapping (round-4 F-C) ─────────────────────
# The recap engine reports progress as (step, total, raw English label)
# triples: planner lines ("Timestamp translation", "Story context
# memory"), pipeline wrappers that stack a prefix onto the engine's own
# label ("Smart Sync: VoxCPM 2 3/10", "Smart Sync: Smart Sync video
# 10/10"), and per-segment counters. The Mini App used to print those
# raw strings verbatim in the Burmese UI, and derived the bar from the
# coarse 4-step counter — which pinned the fill at 75% for the entire
# render phase (round-3 U1). The mapper below converts each raw report
# into a canonical stage (rendered localized by the client via
# recap_stage_* i18n keys), an optional "i/n" sub-progress, and an
# honest percent inside that stage's band. The percent never moves
# backwards within a job: callers pass the entry's current percent and
# keep the returned maximum.
_RECAP_STAGE_BANDS = {
    "download": (5, 14),
    "transcribe": (14, 28),
    "context": (28, 38),
    "translate": (38, 50),
    "plan": (50, 60),
    "tts": (60, 76),
    "sync": (76, 85),
    "png": (85, 89),
    "overlay": (89, 95),
    "mux": (95, 98),
    "finalize": (98, 99),
    "script_ready": (60, 64),
    "split": (90, 95),
}
_RECAP_STAGE_STEPS = {
    "download": "1/4",
    "transcribe": "1/4",
    "context": "2/4",
    "translate": "2/4",
    "plan": "2/4",
    "tts": "3/4",
    "sync": "3/4",
    "png": "3/4",
    "overlay": "4/4",
    "mux": "4/4",
    "finalize": "4/4",
    "script_ready": "2/4",
    "split": "3/4",
}
_RECAP_SUB_PROGRESS_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*$")


def _recap_clean_progress_label(label) -> str:
    """Collapse the pipeline's stacked "Smart Sync: " wrapper prefix.

    The render runner wraps the engine's own label ("Smart Sync video
    3/10") in "Smart Sync: ", producing "Smart Sync: Smart Sync video
    3/10". Strip the wrapper whenever the inner label itself starts
    with "Smart Sync" (repeatedly, for deeper stacks); every other
    label passes through untouched.
    """
    clean = str(label or "").strip()
    while True:
        m = re.match(r"(?i)smart sync:\s+(?=smart sync\b)", clean)
        if not m:
            return clean
        clean = clean[m.end():].strip()


def _recap_stage_progress(label, prev_percent=0):
    """Map one raw engine progress label to (stage, sub, percent, step).

    stage is None for labels with no canonical stage (download phase,
    unknown lines) — callers then keep the previous display state and
    percent. sub is the label's own trailing "i/n" counter ("" when the
    label carries none). percent is monotonic: never below prev_percent.
    """
    prev = int(prev_percent or 0)
    clean = _recap_clean_progress_label(label)
    low = clean.lower()
    sub = ""
    frac = 0.0
    m = _RECAP_SUB_PROGRESS_RE.search(clean)
    if m:
        sub = f"{int(m.group(1))}/{int(m.group(2))}"
        try:
            frac = max(0.0, min(1.0, int(m.group(1)) / max(1, int(m.group(2)))))
        except (TypeError, ValueError):
            frac = 0.0
    tail = low.rsplit(":", 1)[-1].strip()
    if "final recap video ready" in low:
        stage, frac, sub = "finalize", 1.0, ""
    elif "downloading source" in low or "downloading the source" in low:
        # Round-6 P6: the URL-ingest seam ("📥 Downloading source
        # video from URL...") had no stage and reached the Burmese
        # UI as raw English.
        stage = "download"
    elif (
        "transcription" in low
        or "transcribing" in low
        or "transcript" in low
        or "whisper" in low
        or "ingestion" in low
        or "key pool" in low
        or "video duration" in low
        or "🎬 source" in low
    ):
        # "transcript" (noun), the Whisper service's own status lines
        # and the planner's setup lines all belong to this stage;
        # before round 6 they fell through unmapped and the client
        # fell back to the raw English label (round-5 P6).
        stage = "transcribe"
    elif "story context" in low:
        stage = "context"
    elif "translation" in low or "translate batch" in low:
        stage = "translate"
    elif "smart sync plan" in low or "smart sync boundaries" in low:
        stage = "plan"
    elif "plan ready" in low or "plan reused" in low:
        stage, frac = "plan", 1.0
    elif (
        "voxcpm" in low
        or "gemini tts" in low
        or "studio tts" in low
        or "edge tts" in low
        or "& smart sync render" in low
        or tail.startswith("audio ")
    ):
        stage = "tts"
    elif "smart sync video" in low or tail.startswith("video "):
        stage = "sync"
    elif "rendering subtitle png" in low or "subtitle pngs" in low:
        stage = "png"
    elif "compositing video overlay" in low or "production overlay pass" in low:
        stage = "overlay"
    elif "muxing final video" in low or "final mux" in low:
        stage = "mux"
    else:
        return None, "", prev, ""
    lo, hi = _RECAP_STAGE_BANDS[stage]
    pct = int(round(lo + frac * (hi - lo)))
    return stage, sub, max(prev, pct), _RECAP_STAGE_STEPS[stage]


def _recap_set_entry_label(entry: dict, label: str, stage: str = None) -> None:
    """Write an authored progress label and its canonical stage.

    The label remains the API's raw text for other consumers, but the
    Mini App renders ``progress_stage`` through i18n whenever one is
    set — so authored seams must pass their stage (round-6 P6: the
    English download / script-ready / completed labels used to reach
    the Burmese UI verbatim because they cleared the stage). With no
    stage the previous behaviour is kept (stage cleared, label shown
    as-is — used for dynamic Burmese lines like per-part counters).
    """
    entry["progress_label"] = label
    entry["progress_stage"] = stage
    entry["progress_sub"] = ""
    # Batch CB: keep the ETA stage-timing marks in step (reporting
    # only — see _recap_eta_note_progress).
    _recap_eta_note_progress(entry, stage, "")
    # Batch CC: mirror the furthest stage into the crash witness the
    # orphan sweep reads (see _note_recap_progress_stage).
    if stage:
        _note_recap_progress_stage(entry, stage)
    if stage and stage in _RECAP_STAGE_BANDS:
        entry["progress_step"] = _RECAP_STAGE_STEPS[stage]
        lo, _hi = _RECAP_STAGE_BANDS[stage]
        entry["progress_percent"] = max(int(entry.get("progress_percent", 0) or 0), lo)


def _recap_max_duration_allowed(is_vip: bool, is_admin: bool = False) -> int:
    """Per-job source-duration cap (seconds). The 10-minute cap is the
    owner's design and applies to everyone — including VIP. The only
    exemption is ADMIN (same ``settings.is_admin`` identity used by
    /api/user/me and the admin endpoints), who may run a long video as
    one job. Longer sources for everyone else go through the split
    path: the source is cut into ≤cap segments, each processed on its
    own (see _recap_submit_duration_decision / _recap_should_segment).
    """
    if is_admin:
        return 86400  # no practical cap (the 1 GB upload limit still applies)
    if is_vip:
        return int(getattr(settings, "recap_max_duration_seconds", 600) or 600)
    return int(getattr(settings, "recap_free_max_duration_seconds", 120) or 120)


def _recap_segment_target_seconds(split_minutes: float, cap_seconds: float) -> float:
    """Part-length target (seconds) the worker hands the segmenter:
    the user's chosen split size, clamped 30 s below the cap so the
    smart cut's +20 s narration-pause slack still respects the cap.
    This is the exact expression the worker uses (keep them in sync)."""
    return min(float(split_minutes) * 60.0, float(cap_seconds) - 30.0)


def _recap_estimate_part_count(
    duration_seconds: float,
    split_minutes: float,
    cap_seconds: float,
) -> int:
    """Estimated part count for an over-cap source, computed BEFORE
    Start: shown in the Mini App split UI and used for the submit-time
    balance pre-check. It mirrors the segmenter's own planning exactly
    — plan_parts over the same target. Without narration levels the
    cuts land exactly on target; real cuts only move LATER (to a
    narration pause, never earlier), so the actual part count can be
    lower than this estimate, never higher.
    """
    if not duration_seconds or duration_seconds <= 0:
        return 1
    target = _recap_segment_target_seconds(split_minutes, cap_seconds)
    if target <= 0:
        return 1
    from app.services.recap_engine.video_splitter import plan_parts

    return max(1, len(plan_parts(float(duration_seconds), target, smart=False, levels=None)))


def _recap_segment_balance_ok(*, is_admin: bool, balance: int, estimated_parts: int) -> bool:
    """Submit-time pre-check for the segmented path: the user's balance
    must cover the estimated per-part total (1 credit per part).
    Admins are exempt — they are never charged."""
    if is_admin:
        return True
    return int(balance or 0) >= max(1, int(estimated_parts or 1))


def _recap_submit_duration_decision(
    *,
    is_vip: bool,
    is_admin: bool,
    split_video: bool,
    script_review: bool,
    duration_seconds: float,
) -> str:
    """Submit-time verdict for a recap source of a known duration.

    Returns "ok" (single job), "segment" (over the cap, but the split
    path will cut the source into ≤cap segments in the worker), or
    "reject" (over the cap with no sanctioned path).
    """
    cap = _recap_max_duration_allowed(is_vip, is_admin)
    if is_admin and split_video and not script_review:
        # Owner ruling 2026-11-02: admin is exempt from ALL limits —
        # the cap never REJECTS an admin (the branches below), but a
        # VIP-only feature gate must not exclude admin either. With
        # split ON, segmentation engages on the same over-cap trigger
        # a VIP faces (the standard recap cap), using the admin's
        # chosen part length; under that trigger the job stays a
        # single job. Admin is never charged for the parts (the
        # worker skips the per-part top-up — see the split path).
        if duration_seconds and duration_seconds > _recap_max_duration_allowed(True):
            return "segment"
        return "ok"
    if not duration_seconds or duration_seconds <= cap:
        return "ok"
    if is_vip and split_video and not script_review:
        # Script review edits ONE whole-video script, so it cannot be
        # combined with segmented processing of an over-cap source.
        return "segment"
    return "reject"


def _recap_should_segment(
    *,
    is_vip: bool,
    is_admin: bool,
    split_video: bool,
    duration_seconds: float,
) -> bool:
    """Worker-side: should this job's source be cut into ≤cap segments
    and processed part by part? Mirrors the submit decision."""
    if not split_video or not (is_vip or is_admin):
        return False
    if is_admin:
        # Owner ruling 2026-11-02: admin engages segmentation on the
        # same over-cap trigger as a VIP (the standard recap cap).
        # The admin cap (86400 s) is only the single-job LIMIT
        # exemption — it must never act as a feature gate. The part
        # length still comes from the admin's own split_minutes via
        # _recap_segment_target_seconds against the admin cap, i.e.
        # the chosen length itself, unclamped by the VIP cap.
        return duration_seconds > _recap_max_duration_allowed(True)
    return duration_seconds > _recap_max_duration_allowed(is_vip, is_admin)


def resolve_recap_part_paths(job_id: str) -> list[dict]:
    """Splitter deliverables for a recap job: [{n, path, label}], sorted.

    Registry ``part{n}_path`` entries win; otherwise the job's
    ``parts/recap_part_NN.mp4`` files on disk (they survive registry
    restarts because the split naming is deterministic).
    """
    entry = RECAP_JOBS_REGISTRY.get(job_id) or {}
    labels: dict[int, str] = {}
    for part in entry.get("parts") or []:
        try:
            labels[int(part.get("n"))] = str(part.get("label") or "")
        except Exception:
            continue

    found: dict[int, str] = {}
    for key, value in entry.items():
        m = re.fullmatch(r"part([1-9][0-9]?)_path", str(key))
        if m and value and Path(str(value)).is_file():
            found[int(m.group(1))] = str(value)
    if not found:
        parts_dir = settings.storage_root / "recap_output" / job_id / "parts"
        if parts_dir.is_dir():
            for cand in sorted(parts_dir.glob("recap_part_*.mp4")):
                m = re.fullmatch(r"recap_part_(\d+)\.mp4", cand.name)
                if m and cand.stat().st_size > 1000:
                    found[int(m.group(1))] = str(cand)
    if not found:
        return []

    total = max(found)
    out: list[dict] = []
    for n in sorted(found):
        label = labels.get(n) or ""
        if not label:
            try:
                from app.services.recap_engine.video_splitter import (
                    DEFAULT_LABEL_TEMPLATE,
                    part_label,
                )

                label = part_label(DEFAULT_LABEL_TEMPLATE, n, total)
            except Exception:
                label = f"Part {n}/{total}"
        out.append({"n": n, "path": found[n], "label": label})
    return out


def _file_size_or_none(path) -> Optional[int]:
    """Size of an existing file in bytes, else None (never raises)."""
    try:
        if path and Path(path).is_file():
            return Path(path).stat().st_size
    except OSError:
        pass
    return None


def _recap_output_size_and_sendable(job_id: str, result_path: Optional[str] = None):
    """(output_size_bytes, telegram_sendable) for a recap job's video.

    Mirrors the downloader's ``telegram_sendable`` flag: the Mini App
    hides its "Send to Telegram" action entirely when delivery through
    the bot cannot succeed, leaving Download as the only action.

    Sendable when the full video fits Telegram's 50 MB bot cap, or the
    job is segmented and EVERY split part fits (parts delivery is how
    segmented jobs reach Telegram — see _deliver_recap_parts). Unknown
    size keeps the button (True): only a KNOWN oversize hides it.
    """
    video_path = resolve_recap_deliverable_path(job_id, "video", result_path=result_path)
    size = _file_size_or_none(video_path)
    if size is not None and size <= _DL_TELEGRAM_LIMIT:
        return size, True
    entry = RECAP_JOBS_REGISTRY.get(job_id) or {}
    parts = resolve_recap_part_paths(job_id)
    if parts and (entry.get("segmented") or len(parts) >= 2):
        part_sizes = [_file_size_or_none(p.get("path")) for p in parts]
        if all(s is not None and s <= _DL_TELEGRAM_LIMIT for s in part_sizes):
            return size, True
    if size is not None:
        return size, False
    return None, True


def _voice_output_size_and_sendable(job) -> tuple:
    """(output_size_bytes, telegram_sendable) for a voice job's audio.

    Resolution mirrors send_voice_to_telegram: result_path first, then
    the outputs-dir glob, then the canonical audio_out path. Unknown
    size keeps the send button (True), same rule as recap.
    """
    size = _file_size_or_none(getattr(job, "result_path", None))
    if size is None:
        try:
            outputs_dir = settings.storage_root / "outputs"
            if outputs_dir.is_dir():
                matches = [m for m in outputs_dir.glob(f"{job.id}*") if m.is_file()]
                if matches:
                    size = _file_size_or_none(matches[0])
            if size is None:
                size = _file_size_or_none(settings.storage_root / "audio_out" / f"{job.id}.wav")
        except Exception:
            size = None
    if size is None:
        return None, True
    return size, size <= _DL_TELEGRAM_LIMIT


async def _deliver_recap_parts(bot, chat_id: int, job_id: str, lang: str = "my", title: Optional[str] = None) -> int:
    """Send every split part of a recap job to a Telegram chat.

    Parts up to Telegram's 50 MB bot limit go as videos with their part
    label; an oversized part gets a short note pointing at the Mini App
    (where the same part is downloadable). Returns the number of parts
    actually sent as video. Never raises.
    """
    from aiogram.types import FSInputFile
    from app.services.downloader import format_filesize

    sent = 0
    try:
        parts = resolve_recap_part_paths(job_id)
    except Exception:
        return 0
    for part in parts:
        try:
            size = os.path.getsize(part["path"]) if os.path.isfile(part["path"]) else 0
            if 0 < size <= 50 * 1024 * 1024:
                vf = FSInputFile(
                    part["path"],
                    filename=branded_download_filename(
                        "Recap", Path(part["path"]).suffix.lstrip(".") or "mp4",
                        title=title, job_id=job_id, part=part["n"],
                    ),
                )
                caption = t(
                    "recap_part_caption",
                    lang,
                    label=html.escape(str(part["label"])),
                    size_formatted=format_filesize(size),
                )
                await bot.send_video(
                    chat_id=chat_id, video=vf, caption=caption, parse_mode="HTML"
                )
                sent += 1
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    text=t(
                        "recap_part_too_large",
                        lang,
                        label=html.escape(str(part["label"])),
                        size_formatted=format_filesize(size),
                    ),
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.warning(f"Failed to send recap part {part.get('n')} of {job_id}: {e}")
    return sent


async def _send_recap_telegram_delivery(
    chat_id: int,
    video_path: str,
    title: str,
    style: str,
    voice_name: str,
    duration_sec: int,
    job_id: Optional[str] = None,
) -> tuple:
    """Deliver the completed recap VIDEO directly to user's Telegram.

    Batch AP (owner order 2026-11-05): video ONLY — no companion files.
    The SRT, narration audio and script are never pushed with the
    video; the bot completion message carries one button per extra
    and each is sent only when the user presses for that file (the
    ``send_recap_srt_`` / ``send_recap_audio_`` / ``send_recap_script_``
    handlers in app/handlers/clone.py). All three stay downloadable
    from the Mini App exactly as before. The helper no longer accepts
    the extra paths at all — the Batch AJ L12 shape (parameters
    accepted, delivery optional) is what let extras drift in and out
    of this function unnoticed.

    Batch AF: returns ``(delivered, reason)`` instead of swallowing
    every failure into a warning log. ``delivered`` is True only when
    the main delivery (the video, the split parts, or the oversize
    notice) actually went out; ``reason`` is a coarse failure class
    (exception type / gate name) for server logs — never user copy.
    The send-telegram endpoint turns a False into an honest error
    response; before this, it answered success unconditionally and
    the client showed "✅ ပို့ပြီးပါပြီ" for sends that never
    happened (observed live 2026-11-04).
    """
    if not settings.bot_token or not chat_id:
        return False, "bot_not_configured" if not settings.bot_token else "missing_chat_id"
    # chat_id is the user's Telegram id for direct deliveries; resolve their
    # stored language so delivery buttons/captions are not Myanmar-only.
    lang = "my"
    try:
        from app.db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as _s:
            _u = await _s.get(User, chat_id)
            if _u and _u.language_code:
                lang = _u.language_code
    except Exception:
        lang = "my"
    delivered = False
    try:
        from aiogram import Bot
        from aiogram.types import FSInputFile
        from app.services.downloader import format_duration, format_filesize

        bot = Bot(token=settings.bot_token)
        try:
            file_size = os.path.getsize(video_path) if os.path.isfile(video_path) else 0
            dur_formatted = format_duration(duration_sec)
            size_formatted = format_filesize(file_size)
            title = _clean_recap_display_title(title)

            caption = t(
                "recap_delivery_caption",
                lang,
                title=html.escape(title[:60]),
                style=style,
                voice_name=voice_name,
                dur_formatted=dur_formatted,
                size_formatted=size_formatted,
            )
            # Fallback transparency (fix 2026-10-29): tell the user when
            # some narration lines used the backup Edge voice.
            _fb_used, _fb_segs = _recap_fallback_info(Path(video_path).parent)
            if not _fb_used:
                _fb_used, _fb_segs = _recap_fallback_info(Path(video_path).parent.parent)
            if _fb_used:
                caption += t("recap_fallback_note", lang, count=len(_fb_segs) or 1)

            parts_available = bool(job_id) and bool(resolve_recap_part_paths(job_id))
            is_segmented = bool(job_id) and bool(
                (RECAP_JOBS_REGISTRY.get(job_id) or {}).get("segmented")
            )
            if is_segmented:
                # Segmented job: the registered video is only part 1 —
                # the parts together are the whole delivery.
                sent_parts = await _deliver_recap_parts(bot, chat_id, job_id, lang, title=title)
                delivered = sent_parts > 0
            elif file_size <= 50 * 1024 * 1024:
                vf = FSInputFile(
                    video_path,
                    filename=branded_download_filename(
                        "Recap", Path(video_path).suffix.lstrip(".") or "mp4",
                        title=title, job_id=job_id,
                    ),
                )
                await bot.send_video(chat_id=chat_id, video=vf, caption=caption, parse_mode="HTML")
                delivered = True
            elif parts_available:
                # Full render exceeds Telegram's 50 MB bot limit, but the
                # job was split: the parts ARE the Telegram delivery.
                sent_parts = await _deliver_recap_parts(bot, chat_id, job_id, lang, title=title)
                delivered = sent_parts > 0
            else:
                from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
                app_url = settings.mini_app_url
                v_parent = Path(video_path).parent.name
                v_grand = Path(video_path).parent.parent.name
                jid = v_parent if v_parent.startswith("rc_") else (v_grand if v_grand.startswith("rc_") else Path(video_path).stem)
                # No raw download URL button: /api/recap/download requires
                # Telegram init-data auth, so the old url= button only
                # returned 401 in a browser. The Mini App button carries
                # the job id and downloads with proper auth.
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=t("clone_btn_miniapp_watch", lang),
                            web_app=WebAppInfo(url=f"{app_url}?tab=tab-recap&recap_job_id={jid}")
                        )
                    ]
                ])
                over_caption = t(
                    "recap_over_app_caption",
                    lang,
                    jid=jid,
                    size_formatted=size_formatted,
                )
                await bot.send_message(chat_id=chat_id, text=over_caption, reply_markup=kb, parse_mode="HTML")
                delivered = True

            # Batch AP: nothing follows the main delivery. The former
            # duplicate-parts tail (full video AND every part for a
            # non-segmented job whose render fit in 50 MB) is gone too:
            # parts are delivered only when they ARE the delivery —
            # the segmented branch and the oversize-with-parts branch
            # above.

        finally:
            await bot.session.close()
    except Exception as e:
        logger.warning(f"Failed to send recap video to Telegram chat {chat_id}: {e}")
        # Batch AF: a failure AFTER the main delivery still counts as
        # delivered — the video the user asked for did arrive; the
        # reason rides along for the logs.
        return delivered, type(e).__name__
    return delivered, None


def _recap_workspace_dir(job_id: str) -> Path:
    """Engine workspace of a recap job (plan + checkpoints live here)."""
    return settings.storage_root / "recap_output" / job_id / "workspace"


def _load_recap_script_plan(job_id: str):
    """(plan_path, plan dict) for a job whose script phase has finished."""
    import json as _json

    plan_path = _recap_workspace_dir(job_id) / "smart_sync_plan.json"
    if not plan_path.is_file():
        raise HTTPException(status_code=404, detail="Recap script not found")
    try:
        return plan_path, _json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception:
        raise HTTPException(status_code=500, detail="Recap script is unreadable")


async def _recap_job_marked_cancelled(job_id: str, entry: Optional[dict] = None) -> bool:
    """True when a recap job was cancelled while it waited or rendered.

    Two signals, either is authoritative:

    * the registry entry carries the admin cancel path's marker --
      status ``CANCELLED``, or ``FAILED`` with the "Cancelled by
      Administrator" message (that is exactly what the cancel endpoint
      writes into the entry, and no fresh dispatch ever starts from a
      FAILED entry: retries build a new QUEUED one);
    * the Job row's status is CANCELLED (the cancel endpoint writes
      that first, before touching the registry).

    A failed status READ is not treated as a cancellation -- the
    execute path's own DB writes already tolerate transient errors,
    and treating a hiccup as a cancel would silently drop real jobs.
    """
    if entry:
        status = str(entry.get("status") or "")
        if status == "CANCELLED":
            return True
        if status == "FAILED" and str(entry.get("error_message") or "").startswith("Cancelled"):
            return True
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            row = (
                await session.execute(select(Job.status).where(Job.id == job_id))
            ).first()
        return bool(row) and row[0] == JobStatus.CANCELLED.value
    except Exception as exc:
        logger.warning(f"Could not re-check cancel state for recap job {job_id}: {exc}")
        return False


def _build_recap_failure_dm_text(
    lang: str,
    *,
    title: str,
    job_id: str,
    reason: str = "",
    reason_key: Optional[str] = None,
    credits_refunded: int = 0,
) -> str:
    """Compose the recap failure DM (batch K).

    Mirrors the success notification's shape (heading, title, task
    id) but carries the CURATED failure reason — the same user-safe
    string the Mini App surfaces, or a locale key resolved here —
    never raw engine text. The refund line appears only when a
    refund was actually issued (``credits_refunded`` > 0), and the
    upload hint only for jobs whose source was a direct upload.
    Title and reason are HTML-escaped: both can carry user-supplied
    text and the message is sent with parse_mode="HTML".
    """
    clean_title = html.escape(str(title or "").strip()) or "Recap"
    if reason_key:
        reason_text = t(reason_key, lang)
    else:
        reason_text = str(reason or "").strip()
    text = t(
        "recap_failure_notify_text",
        lang,
        clean_title=clean_title,
        reason=html.escape(reason_text),
        job_id=job_id,
    )
    if credits_refunded:
        text += t(
            "recap_failure_refund_note", lang, credits=int(credits_refunded)
        )
    if "Uploaded Video" in str(title or ""):
        text += t("recap_failure_upload_hint", lang)
    return text


async def _send_recap_failure_dm(
    *,
    job_id: str,
    chat_id: Optional[int],
    title: str,
    reason: str = "",
    reason_key: Optional[str] = None,
    credits_refunded: int = 0,
    entry: Optional[dict] = None,
) -> bool:
    """Send the recap failure DM exactly once per job (batch K).

    The success DM is emitted from a single terminal branch of the
    runner; failures mirror that once-only discipline with a
    ``failure_notified`` flag on the registry entry, set BEFORE the
    send attempt so a re-entered handler can never double-notify.
    Callers without a live entry (the SCRIPT_READY expiry sweep)
    are once-only by construction — the sweep flips the persisted
    state that feeds it. Internal stage retries never reach this
    helper: it is called only from terminal failure paths.
    Returns True when a message was sent.
    """
    if entry is not None:
        if entry.get("failure_notified"):
            return False
        entry["failure_notified"] = True
    if not settings.bot_token or not chat_id:
        return False
    lang = "my"
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            _u = await session.get(User, int(chat_id))
            if _u and getattr(_u, "language_code", None):
                lang = _u.language_code
    except Exception:
        pass
    text = _build_recap_failure_dm_text(
        lang,
        title=title,
        job_id=job_id,
        reason=reason,
        reason_key=reason_key,
        credits_refunded=credits_refunded,
    )
    from aiogram import Bot

    bot = Bot(token=settings.bot_token)
    try:
        await bot.send_message(chat_id=int(chat_id), text=text, parse_mode="HTML")
    finally:
        await bot.session.close()
    return True


# ── Batch AJ M10(c): arm the recap job deadline before download ─────
# The engine's 3600 s deadline is cooperative and boundary-only, and
# it arms when the ENGINE starts — the download and planning phases
# before it sat outside any budget, so a slow source could pin one of
# only two recap slots indefinitely before the clock even started.
RECAP_JOB_DEADLINE_SECONDS = 3600


def _recap_deadline_exceeded(created_at, *, now_ts: Optional[float] = None) -> bool:
    """True when a job submitted at *created_at* (the Job row's
    timestamp) is already past the whole-job deadline. The registry
    entry's created_at is NOT used: re-dispatch resets it, which
    would quietly re-arm the budget."""
    if created_at is None:
        return False
    try:
        created_ts = created_at.timestamp()
    except Exception:
        return False
    now = now_ts if now_ts is not None else time.time()
    return (now - created_ts) > RECAP_JOB_DEADLINE_SECONDS


async def _recap_job_deadline_exceeded(job_id: str) -> bool:
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            job = await session.get(Job, job_id)
            created_at = getattr(job, "created_at", None) if job else None
    except Exception:
        return False
    return _recap_deadline_exceeded(created_at)


async def _run_recap_background_job(
    job_id: str,
    req: RecapConfigRequest,
    user_id: int,
    chat_id: int,
    is_vip: bool = False,
    credit_deducted: bool = False,
    phase: str = "full",
    resume_source: Optional[str] = None,
    voice_choice_mode: Optional[str] = None,
    dp_resume: bool = False,
):
    """Capped entry point for every recap execution (deep-hunt #4 D2).

    All dispatch sites (submit, script-render, admin retry, bot admin,
    the server queue worker's re-dispatch) call this name; the global
    semaphore guarantees at most 2 renders execute at once. A job that
    cannot get a slot yet waits inside this coroutine — its registry
    entry stays QUEUED and the queue worker's guard sees that status,
    so it is never re-dispatched, failed, or dropped while waiting.

    Batch DP: ``voice_choice_mode`` / ``dp_resume`` carry a parked
    job's owner choice into the resumed execution (see
    _recap_redispatch_plan); both default to the pre-DP behaviour.
    """
    async with RECAP_EXECUTION_SEMAPHORE:
        await _execute_recap_background_job(
            job_id,
            req,
            user_id,
            chat_id,
            is_vip=is_vip,
            credit_deducted=credit_deducted,
            phase=phase,
            resume_source=resume_source,
            voice_choice_mode=voice_choice_mode,
            dp_resume=dp_resume,
        )


async def _execute_recap_background_job(
    job_id: str,
    req: RecapConfigRequest,
    user_id: int,
    chat_id: int,
    is_vip: bool = False,
    credit_deducted: bool = False,
    phase: str = "full",
    resume_source: Optional[str] = None,
    voice_choice_mode: Optional[str] = None,
    dp_resume: bool = False,
):
    # Batch DP: the park signal raised by the recap engine's TTS stage
    # when a paid job's VoxCPM walk exhausts the pool.
    from app.services.recap_engine.core import RecapParkRequested
    entry = RECAP_JOBS_REGISTRY.get(job_id)
    if not entry:
        return

    # Batch AU: the execution choke point for both render-quality
    # usages below (the main render and the optional post-render
    # split). Clamping the shared request here also covers configs
    # restored from a persisted job or a script-review snapshot.
    _clamp_recap_render_quality(req, user_id=user_id, is_vip=is_vip)
    # Batch AW: the same execution choke point for the narration
    # engine — a free-tier job can never execute with clone narration,
    # however its config was persisted.
    _clamp_recap_tts_engine(req, user_id=user_id, is_vip=is_vip)
    # Batch BV: the same execution choke point for the own-voice
    # reference — a free-tier job can never execute with the user's
    # uploaded sample, however its config was persisted.
    _clamp_recap_voice_ref(req, user_id=user_id, is_vip=is_vip)

    # Credit accounting for this job (older registry entries — e.g.
    # admin re-queues — may predate these keys): the amount actually
    # charged so far, which the failure paths refund in full.
    entry.setdefault("credit_deducted", bool(credit_deducted))
    entry.setdefault(
        "credits_charged",
        int(entry.get("credits_charged") or (1 if credit_deducted else 0)),
    )
    # Batch BE: an entry that predates the submit-time surcharge
    # stamp (legacy rebuilds, admin re-queues) derives it HERE — from
    # the job's pinned pricing mode and the config quality the AU
    # clamp above just settled — before any top-up runs. Stamped
    # entries keep their submit-time value: the pin is never
    # rewritten, so a switch flip mid-flight cannot change the price.
    if entry.get("max_quality_surcharge") is None:
        entry["max_quality_surcharge"] = (
            _recap_max_quality_surcharge(req.render_quality)
            if _entry_tiered_pricing(entry)
            else 0
        )

    # Post-semaphore cancel re-check (hunt #5): this coroutine may have
    # waited inside _run_recap_background_job for an execution slot
    # while an admin cancelled the job -- the cancel path has already
    # written the terminal state and refunded the charge. Flipping to
    # RUNNING here would resurrect a cancelled, refunded job, render
    # it and deliver its video. Abort quietly instead: no status flip,
    # no render, no delivery, no credit movement.
    if await _recap_job_marked_cancelled(job_id, entry):
        logger.info(
            f"Recap job {job_id} was cancelled while waiting for an execution slot; not running."
        )
        return

    entry["status"] = "RUNNING"
    entry["eta_run_started_at"] = time.time()
    entry["progress_percent"] = 0
    entry["progress_step"] = "1/4"
    _recap_set_entry_label(entry, "📥 Downloading source video from URL...", stage="download")

    try:
        from app.services.downloader import MediaDownloader
        from app.db.session import AsyncSessionLocal
        downloader = MediaDownloader()

        # Batch AJ M10(c): the whole-job deadline is armed from the
        # Job row's submit time and checked BEFORE the download /
        # planning phases — previously those phases had no budget at
        # all. Render-resume runs (resume_source set, after SCRIPT_READY
        # parking) are exempt: the parking window has its own 24 h TTL.
        # Batch DP: a parked-and-resumed run (dp_resume) is exempt for
        # the same reason — the park window is bounded by the 48 h
        # safety valve in the resumer, not by this deadline.
        if resume_source is None and not dp_resume and await _recap_job_deadline_exceeded(job_id):
            raise RuntimeError(
                "Recap job exceeded its 1-hour deadline before processing started"
            )

        # Update Job in database
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    update(Job).where(Job.id == job_id).values(status=JobStatus.RUNNING.value, started_at=func.now())
                )
                await session.commit()
        except Exception as db_e:
            logger.warning(f"Failed to update Job {job_id} to RUNNING in DB: {db_e}")

        job_dir = settings.storage_root / "recap_output" / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        def progress_cb(cur: int, tot: int, label: str):
            if entry:
                # Round-4 F-C: raw engine labels become a canonical
                # stage + sub-progress + band percent (monotonic);
                # unmapped labels keep the previous display state.
                stage, sub, pct, step = _recap_stage_progress(
                    label, entry.get("progress_percent", 0)
                )
                if stage:
                    entry["progress_stage"] = stage
                    entry["progress_sub"] = sub
                    entry["progress_step"] = step
                    entry["progress_percent"] = pct
                    # Batch CB: ETA timing/segment marks (reporting
                    # only; read by _recap_eta_from_entry).
                    _recap_eta_note_progress(entry, stage, sub)
                    # Batch CC: furthest-stage witness for the sweep.
                    _note_recap_progress_stage(entry, stage)
                else:
                    entry["progress_step"] = f"{cur}/{tot}"
                entry["progress_label"] = _recap_clean_progress_label(label)

        from app.services.curated_voices import get_curated_voice
        p_info = get_curated_voice(req.voice_slug)
        is_female = False
        voxcpm_control = None
        voxcpm_ref_audio = None
        tts_engine = "edge_tts"

        if p_info:
            is_female = (p_info.get("gender") == "female")
            voxcpm_control = p_info.get("control_instruction")
            selected_voice = f"voxcpm_{req.voice_slug}"
            tts_engine = "voxcpm"
            # Curated personas use direct VoxCPM Voice Design with control_instruction
            voxcpm_ref_audio = None
        elif req.voice_slug in ("mahnin", "nilar", "mamay", "manadi", "mayoon", "machaw", "mathiri"):
            is_female = True
            selected_voice = "my-MM-NilarNeural"
            tts_engine = "edge_tts"
        else:
            selected_voice = "my-MM-ThihaNeural"
            tts_engine = "edge_tts"

        # Batch BV: own-voice narration — the user's uploaded sample
        # replaces the persona reference for VoxCPM cloning (the
        # pipeline already forwards voxcpm_ref_audio; core prefers
        # the overlay reference). The persona resolved above stays
        # (a) the per-segment fallback voice, (b) the gender source
        # for Edge/Gemini fallbacks. A ref whose file has vanished
        # since submit (swept/expired) degrades to the persona path
        # with a log line — never a hard failure of a paid job. The
        # ref only ever drives the clone engine: on gemini/edge it
        # stays unset so the intent is exact.
        if req.tts_engine == "voxcpm" and getattr(req, "voice_ref_path", None):
            if Path(req.voice_ref_path).is_file():
                voxcpm_ref_audio = req.voice_ref_path
            else:
                logger.warning(
                    f"Recap job {job_id}: own-voice reference missing at execution "
                    f"({req.voice_ref_path}) — persona narration"
                )

        # Gemini TTS option (production-grade programme): the persona
        # voice above stays as the per-segment FALLBACK voice, so a
        # Gemini outage degrades to the persona engine line-by-line and
        # is reported via the same fallback transparency fields.
        gemini_voice = req.gemini_voice
        if req.tts_engine == "gemini":
            tts_engine = "gemini"
            if not gemini_voice:
                gemini_voice = "Kore" if is_female else "Charon"

        # Batch AW-1: Edge TTS as a user-selected PRIMARY engine. The
        # persona resolution above already produced an Edge voice in
        # every path (curated persona -> its gender-matched Edge base,
        # legacy female slugs -> Nilar, otherwise Thiha), so selecting
        # Edge simply keeps that voice as the render voice with the
        # plain "edge_tts" engine label. Downstream, the pipeline runs
        # it through the plain-Edge primary path (EdgeSmartSync with a
        # non-voxcpm voice) — it does NOT detour through VoxCPM, so
        # Edge here is never recorded as a fallback.
        if req.tts_engine == "edge":
            tts_engine = "edge_tts"
            if p_info:
                selected_voice = p_info.get("edge_voice") or (
                    "my-MM-NilarNeural" if is_female else "my-MM-ThihaNeural"
                )

        logo_pos_map = {
            "top-right": [0.82, 0.04],
            "top-left": [0.04, 0.04],
            "bottom-right": [0.82, 0.88],
            "bottom-left": [0.04, 0.88],
            "top-center": [0.42, 0.04],
        }
        logo_pos_coords = logo_pos_map.get(req.logo_pos, [0.82, 0.04])
        # Owner decision 2026-11-02 (supersedes the R40 verify-first
        # (i) interim fix): the logo overlay is a PAID feature and
        # stamps the user's OWN uploaded channel logo — the bundled
        # brand asset wiring is removed. _resolve_recap_logo_path
        # returns None for free users (even with include_logo=true),
        # for toggle-off, and for paid users who uploaded nothing.
        logo_path_arg = _resolve_recap_logo_path(user_id, req.include_logo, is_vip)

        loop = asyncio.get_running_loop()
        from app.services.downloader import extract_first_url, unshorten_url, is_local_media
        if phase == "render" and resume_source:
            # Phase 2 of the script-review flow: the source is already on
            # disk inside this job's own directory — no re-download, no
            # URL re-validation (it never leaves the server).
            canonical_source_url = str(resume_source)
        elif is_local_media(req.url):
            canonical_source_url = str(req.url).strip('\'"')
        else:
            clean_source_url = extract_first_url(req.url) or req.url.strip()
            canonical_source_url = unshorten_url(clean_source_url)
            # Re-validate at execution time (DNS may have changed since
            # submission): a blocked target fails the job, which triggers
            # the credit refund below instead of fetching an internal URL.
            if canonical_source_url.startswith(("http://", "https://")):
                from app.services.url_safety import validate_public_url

                validate_public_url(canonical_source_url)

        # Admin Watermark Controls: read active per-tier toggle at render time
        wm_cfg = get_watermark_config(is_vip)
        watermark_text = "VoiceClonePro" if wm_cfg["watermark"] else None

        # Admin exemption: settings.is_admin is the same identity check
        # /api/user/me uses; admins are exempt from the 10-minute cap.
        is_admin_user = settings.is_admin(user_id)
        max_dur_limit = _recap_max_duration_allowed(is_vip, is_admin_user)

        if phase == "script":
            # ── Script-review phase 1: download + plan ONLY (no TTS/render).
            # The job then waits in SCRIPT_READY until the user has reviewed
            # / edited the narration and POSTs /recap/{id}/render.
            _recap_set_entry_label(entry, "🎙️ [1/4] Transcription + translation (script review)...", stage="transcribe")
            if is_local_media(canonical_source_url):
                script_source = str(Path(canonical_source_url).resolve())
                if not Path(script_source).is_file():
                    raise FileNotFoundError(f"Source video not found: {script_source}")
            else:
                script_source, _dl_info = await loop.run_in_executor(
                    None,
                    functools.partial(
                        downloader.download_video,
                        canonical_source_url,
                        target_dir=str(job_dir),
                        max_height=1080 if is_vip else 720,
                    ),
                )

            from app.services.downloader import probe_local_media

            _meta = probe_local_media(script_source)
            _dur = float(_meta.get("duration", 0.0) or 0.0)
            if _dur > max_dur_limit:
                from app.services.downloader import recap_over_limit_message

                raise ValueError(
                    recap_over_limit_message(_dur, max_dur_limit, is_vip)
                    + (f" {_SCRIPT_REVIEW_SPLIT_HINT}" if getattr(req, "split_video", False) else "")
                )

            from app.services.recap_engine.recap_pipeline import build_recap_script_plan

            plan_res = await loop.run_in_executor(
                None,
                functools.partial(
                    build_recap_script_plan,
                    script_source,
                    job_dir=str(job_dir / "workspace"),
                    narrative_style=req.story_style,
                    is_female=is_female,
                    source_title=entry.get("title") or Path(script_source).stem,
                    progress_fn=progress_cb,
                    source_language=req.source_language,
                    voice_speed=req.voice_speed,
                ),
            )

            entry["source_path"] = str(script_source)
            entry["req_snapshot"] = req.model_dump()
            entry["is_vip"] = bool(is_vip)
            entry["credit_deducted"] = bool(credit_deducted)
            entry["segments_count"] = int(plan_res.get("segments") or 0)
            entry["status"] = "SCRIPT_READY"
            entry["progress_percent"] = 100
            entry["progress_step"] = "2/4"
            _recap_set_entry_label(entry, "📝 Script ready — review & edit before rendering", stage="script_ready")
            # Persist the parking state on the Job row so a restart
            # restores this exact parked job (source + segment count +
            # parking time) instead of re-running the script phase and
            # overwriting the user's edits; the timestamp also drives
            # the 24h abandoned-job expiry.
            entry["script_ready_at"] = datetime.now(timezone.utc).isoformat()
            await _persist_recap_script_ready(entry)
            logger.info(f"Recap job {job_id}: script ready ({entry['segments_count']} segments), awaiting user review.")
            return

        # ── Split path for over-cap sources (phase "full" only) ──────
        # The 10-minute cap is NOT raised: when a VIP enables split and
        # the source is over the cap, the source is downloaded first and
        # cut into ≤cap segments at narration pauses; each segment is
        # then processed by the normal pipeline and becomes one
        # deliverable part. Admins engage the same path on the same
        # trigger (owner ruling 2026-11-02 — the admin cap exemption is
        # a limit exemption, not a feature gate) but are never charged
        # per part; script-review jobs never get here over-cap (script
        # phase blocks them), and phase "render" always works from a
        # reviewed whole-video script, so it is never segmented.
        segmented_mode = False
        segment_files: list[dict] = []
        _seg_dl_sec = 0.0
        if phase == "full" and req.split_video:
            if is_local_media(canonical_source_url):
                local_source = str(Path(canonical_source_url).resolve())
            else:
                _seg_dl_t0 = time.monotonic()
                local_source, _dl_info = await loop.run_in_executor(
                    None,
                    functools.partial(
                        downloader.download_video,
                        canonical_source_url,
                        target_dir=str(job_dir),
                        max_height=1080 if is_vip else 720,
                    ),
                )
                _seg_dl_sec = round(max(0.0, time.monotonic() - _seg_dl_t0), 3)
            from app.services.downloader import probe_local_media

            _src_dur = float(
                probe_local_media(local_source).get("duration", 0.0) or 0.0
            )
            # Batch AO: the worker's own probe of the downloaded
            # source is the FALLBACK pricing duration (the submit-
            # resolved duration outranks it — see
            # _recap_entry_pricing_duration).
            entry["probed_duration_seconds"] = _src_dur
            if _recap_should_segment(
                is_vip=is_vip,
                is_admin=is_admin_user,
                split_video=True,
                duration_seconds=_src_dur,
            ):
                from app.services.recap_engine.recap_pipeline import find_binary
                from app.services.recap_engine.video_splitter import (
                    split_source_into_segments,
                )

                _recap_set_entry_label(
                    entry,
                    "✂️ ဗီဒီယိုရှည်လို့ အပိုင်းခွဲနေပါတယ် (အပိုင်းတိုင်း ≤ ၁၀ မိနစ်)...",
                )
                segment_files = await loop.run_in_executor(
                    None,
                    functools.partial(
                        split_source_into_segments,
                        local_source,
                        job_dir / "source_segments",
                        # Target below the cap: plan_parts may extend a
                        # cut by up to 20 s to reach a narration pause.
                        # (Shared helper: the submit-time estimate and
                        # this call must use the exact same target.)
                        part_seconds=_recap_segment_target_seconds(
                            req.split_minutes, max_dur_limit
                        ),
                        ffmpeg=find_binary("ffmpeg"),
                        ffprobe=find_binary("ffprobe"),
                        log=lambda m: logger.info(f"Recap job {job_id}: {m}"),
                    ),
                )
                segmented_mode = len(segment_files) >= 2
                if segmented_mode:
                    total_parts = len(segment_files)
                    if is_admin_user:
                        # Owner ruling 2026-11-02: admin segments like
                        # a VIP but is NEVER charged — the per-part
                        # top-up is skipped entirely, so the job's
                        # credit bookkeeping stays at 0 (the ledger
                        # helper would no-op the deduction, but the
                        # top-up would still stamp credits_charged = N
                        # onto the entry and the Job row). Only the
                        # part count is recorded, so status/history
                        # show the parts exactly as for a VIP.
                        entry["parts_count"] = total_parts
                        _recap_set_entry_label(
                            entry,
                            f"✂️ အပိုင်း {total_parts} ပိုင်း ခွဲပြီးပါပြီ — "
                            "အပိုင်းများ စတင်ထုတ်နေပါတယ်...",
                        )
                    else:
                        # Per-part charging (owner decision 2026-10-30):
                        # the job costs 1 credit PER PART. The exact total
                        # is charged NOW — one top-up ledger action over
                        # the submit-time single credit — BEFORE any part
                        # renders. If the balance cannot cover it, the job
                        # fails here and the failure path refunds the
                        # submit credit, leaving the user exactly whole.
                        await _top_up_recap_segment_charge(user_id, entry, total_parts)
                        credit_deducted = True
                        if entry.get("tiered_pricing"):
                            # Batch AO: the charge followed the
                            # duration band, not the part count — the
                            # label must say what was actually charged.
                            _recap_set_entry_label(
                                entry,
                                f"💳 ဗီဒီယို ကြာချိန်အလိုက် စုစုပေါင်း {int(entry.get('credits_charged') or 0)} Credits "
                                "ကောက်ခံပြီးပါပြီ — အပိုင်းများ စတင်ထုတ်နေပါတယ်...",
                            )
                        else:
                            _recap_set_entry_label(
                                entry,
                                f"💳 အပိုင်း {total_parts} ပိုင်း × 1 Credit = {total_parts} Credits "
                                "ကောက်ခံပြီးပါပြီ — အပိုင်းများ စတင်ထုတ်နေပါတယ်...",
                            )
            canonical_source_url = local_source

        # Batch AO: a NON-segmented tier-priced job is topped up to
        # its duration-band total here — after the split decision (so
        # segmented jobs, priced above, are skipped) and before any
        # render. Inert for legacy-priced jobs, admins and unpaid
        # entries; a band of 1 never moves money (see
        # _apply_recap_band_top_up). A balance that cannot cover the
        # band fails the job here and the failure path refunds the
        # submit credit, exactly like the segmented top-up.
        if not segmented_mode:
            await _apply_recap_band_top_up(
                user_id, entry, is_admin_user=is_admin_user
            )
            credit_deducted = bool(entry.get("credit_deducted"))

        # Batch CG: resolve the BYOK route context for Gemini
        # narration (own key → pool). In-memory only — it never
        # touches the entry dict, the plan JSON or any disk state;
        # a resolution failure falls back to the legacy pool-only
        # engine construction inside the pipeline.
        _cg_route_ctx = None
        if tts_engine == "gemini":
            try:
                from app.services.gemini_key_service import (
                    resolve_gemini_route as _resolve_gemini_route,
                )

                async with AsyncSessionLocal() as _cg_session:
                    # Batch CS: get_or_create_user lives in the ledger
                    # service and was never imported into this module —
                    # every gemini recap since CG died here with a
                    # NameError and silently took the legacy pool path
                    # (no BYOK routing, no taste/budget accounting).
                    from app.services.ledger import (
                        get_or_create_user as _get_or_create_user,
                    )

                    _cg_owner, _ = await _get_or_create_user(_cg_session, user_id)
                    _cg_route_ctx = await _resolve_gemini_route(
                        _cg_session, _cg_owner, is_admin=is_admin_user
                    )
            except Exception:
                logger.warning(
                    "Recap job %s: CG route resolution failed; legacy pool path",
                    job_id, exc_info=True,
                )
                _cg_route_ctx = None

        def _make_recap_fn(source, target_dir, **extra_kw):
            return functools.partial(
            downloader.generate_recap_video,
            url_or_path=source,
            target_dir=target_dir,
            voice=selected_voice,
            narrative_style=req.story_style,
            voice_speed=req.voice_speed,
            tts_engine=tts_engine,
            # Batch DP: paid tier (VIP or admin) drives the reserve
            # walk + park-and-ask; a resumed run carries the owner's
            # voice choice. Free jobs pass paid_tier=False and behave
            # exactly as before.
            paid_tier=bool(is_vip or is_admin_user),
            voice_choice_mode=voice_choice_mode,
            voxcpm_control=voxcpm_control,
            voxcpm_ref_audio=voxcpm_ref_audio,
            is_female=is_female,
            source_language=req.source_language,
            render_quality=req.render_quality,
            gemini_voice=gemini_voice,
            gemini_route=_cg_route_ctx,
            output_ratio=req.ratio,
            frame_fill=req.ratio_fill,
            crop_top=float(req.crop_top) / 100.0,
            crop_bottom=float(req.crop_bottom) / 100.0,
            crop_left=float(req.crop_left) / 100.0,
            crop_right=float(req.crop_right) / 100.0,
            mirror_video=req.mirror_video,
            zoom_factor=req.zoom_factor,
            blur_subtitles=req.blur_subtitles,
            blur_y=float(req.blur_y) / 100.0,
            blur_h=float(req.blur_h) / 100.0,
            blur_x=(float(req.blur_x) / 100.0) if req.blur_x is not None else None,
            blur_w=(float(req.blur_w) / 100.0) if req.blur_w is not None else None,
            # Batch DD: an explicit list (or its legacy migration)
            # supersedes the single-box path at the pipeline.
            blur_boxes=_dd_blur_boxes_fractions(req),
            blur_box_explicit=bool(req.blur_box_explicit) and not req.blur_boxes,
            blur_strength=req.blur_strength,
            blur_auto_detect=bool(req.blur_auto_detect),
            burn_subtitles=req.burn_subtitles,
            sub_y=float(req.sub_y) / 100.0,
            # Batch AK: bottom-edge margin wins over sub_y in the
            # engine when present; None keeps the legacy path.
            sub_margin=(float(req.sub_bottom_margin) / 100.0) if req.sub_bottom_margin is not None else None,
            sub_x=(float(req.sub_x) / 100.0) if req.sub_x is not None else None,
            sub_w=(float(req.sub_w) / 100.0) if req.sub_w is not None else None,
            sub_lines=req.sub_lines,
            sub_font_size=req.sub_font_size,
            sub_style=req.sub_style,
            sub_font=req.sub_font,
            # Batch DD: protection level + free style controls +
            # the server-resolved custom font path.
            protection_level=req.protection_level,
            sub_visual_overrides=_dd_overrides_from(req),
            sub_font_custom_path=req.sub_font_custom_path,
            sub_bold=bool(req.sub_bold),
            logo_path=logo_path_arg,
            logo_position=logo_pos_coords if logo_path_arg else None,
            extra_text=watermark_text,
            is_vip=is_vip,
            max_duration_seconds=max_dur_limit,
            progress_callback=progress_cb,
            **extra_kw,
        )

        part_urls: list[dict] = []
        first_part_dest: Optional[str] = None
        _cg_outcomes: list[dict] = []
        _seg_media_records: list[tuple[int, Optional[str], Optional[str], Optional[float]]] = []
        _seg_stage_timings: list[dict] = []
        if segmented_mode:
            # Process every segment through the normal pipeline; each
            # segment render becomes one deliverable part (≤cap each).
            from app.services.recap_engine.video_splitter import (
                DEFAULT_LABEL_TEMPLATE,
                part_label,
            )

            parts_dir = job_dir / "parts"
            parts_dir.mkdir(parents=True, exist_ok=True)
            total_segs = len(segment_files)
            recap_res = None
            seg_warnings: list[str] = []
            running_prev_summary = ""
            for seg in segment_files:
                _recap_set_entry_label(
                    entry, f"🎬 အပိုင်း {seg['n']}/{total_segs} လုပ်နေပါတယ်..."
                )
                seg_res = await loop.run_in_executor(
                    None,
                    _make_recap_fn(
                        seg["path"],
                        str(job_dir / f"seg{seg['n']:02d}"),
                        part_index=int(seg["n"]),
                        total_parts=total_segs,
                        prev_part_summary=running_prev_summary,
                    ),
                )
                if seg_res.get("part_summary"):
                    running_prev_summary = str(seg_res["part_summary"]).strip()
                if seg_res.get("gemini_route"):
                    _cg_outcomes.append(seg_res["gemini_route"])
                if isinstance(seg_res.get("stage_timings"), dict):
                    _seg_stage_timings.append(seg_res["stage_timings"])
                for _w in (seg_res.get("warnings") or []):
                    if _w not in seg_warnings:
                        seg_warnings.append(_w)
                seg_video = seg_res.get("final_video") or seg_res.get("final_video_path")
                if not seg_video or not Path(seg_video).is_file():
                    raise RuntimeError(
                        f"Segment {seg['n']}/{total_segs} render produced no video"
                    )
                dest = parts_dir / f"recap_part_{seg['n']:02d}.mp4"
                if Path(seg_video).resolve() != dest.resolve():
                    shutil.move(seg_video, dest)
                seg_audio = seg_res.get("audio") or seg_res.get("final_audio_path")
                _seg_explicit_dur = seg_res.get("actual_voice_seconds") or seg_res.get("audio_duration")
                _seg_media_records.append(
                    (
                        int(seg["n"]),
                        str(seg_audio) if seg_audio else None,
                        str(dest),
                        float(_seg_explicit_dur) if _seg_explicit_dur is not None else None,
                    )
                )
                entry[f"part{seg['n']}_path"] = str(dest)
                part_urls.append({
                    "n": seg["n"],
                    "label": part_label(DEFAULT_LABEL_TEMPLATE, seg["n"], total_segs),
                    "url": f"/api/recap/download/{job_id}/part{seg['n']}",
                })
                if recap_res is None:
                    recap_res = seg_res
                    first_part_dest = str(dest)
            logger.info(
                f"Recap job {job_id}: segmented source processed as "
                f"{total_segs} parts (each ≤ cap)"
            )
        else:
            recap_res = await loop.run_in_executor(
                None, _make_recap_fn(canonical_source_url, str(job_dir))
            )
            if recap_res.get("gemini_route"):
                _cg_outcomes.append(recap_res["gemini_route"])

        video_path = recap_res.get("final_video") or recap_res.get("final_video_path")
        if segmented_mode and first_part_dest:
            video_path = first_part_dest
        audio_path = recap_res.get("audio") or recap_res.get("final_audio_path")
        if not segmented_mode:
            _single_explicit_dur = recap_res.get("actual_voice_seconds") or recap_res.get("audio_duration")
            _seg_media_records.append(
                (
                    1,
                    str(audio_path) if audio_path else None,
                    str(video_path) if video_path else None,
                    float(_single_explicit_dur) if _single_explicit_dur is not None else None,
                )
            )
        srt_path = recap_res.get("subtitles_srt")
        script_path = _build_clean_recap_script_file(job_dir, job_id, req.story_style, srt_path)

        _fb_used, _fb_segs = _recap_fallback_info(job_dir)
        entry["fallback_used"] = _fb_used
        entry["fallback_segments"] = _fb_segs
        # Batch BV: the BU auto-trim truth for an own-voice sample,
        # read back from the same plan JSON the fallback fields use.
        entry["reference_trimmed"] = _recap_reference_trimmed(job_dir)
        # Batch CG: settle the BYOK accounting for Gemini narration —
        # debit the SUCCESSFUL pool draws (FREE taste), flag a key
        # Google rejected, and remember the taste block so the
        # completion caption can name the own-key option. Accounting
        # must never fail a finished job, so it is fully isolated.
        entry["gemini_taste_blocked"] = any(
            bool(o.get("taste_blocked")) for o in _cg_outcomes
        )
        entry["gemini_key_broken"] = any(
            bool(o.get("own_key_invalid")) for o in _cg_outcomes
        )
        if _cg_route_ctx is not None and _cg_outcomes:
            try:
                from app.services.gemini_key_service import (
                    debit_pool_usage as _cg_debit,
                    log_route as _cg_log_route,
                    mark_user_key_invalid as _cg_mark_invalid,
                )

                _pool_secs = sum(
                    float(o.get("pool_seconds") or 0.0) for o in _cg_outcomes
                )
                _pool_reqs = sum(
                    int(o.get("pool_requests") or 0) for o in _cg_outcomes
                )
                async with AsyncSessionLocal() as _cg_session2:
                    if entry["gemini_key_broken"]:
                        await _cg_mark_invalid(_cg_session2, user_id)
                    if _pool_reqs:
                        await _cg_debit(
                            _cg_session2,
                            user_id,
                            tier=_cg_route_ctx.tier,
                            seconds=_pool_secs,
                            requests=_pool_reqs,
                        )
                    await _cg_session2.commit()
                _cg_log_route(
                    "pool" if _pool_reqs
                    else "edge" if entry["gemini_taste_blocked"]
                    else "own_key",
                    user_id,
                    _cg_route_ctx.tier,
                )
            except Exception:
                logger.warning(
                    "Recap job %s: CG settle failed", job_id, exc_info=True
                )
        # Result warnings from the pipeline (blur safefix 2026-11-03):
        # segmented jobs union the warnings of every segment render.
        if segmented_mode:
            entry["warnings"] = seg_warnings
            _agg_st: dict[str, float] = {}
            if _seg_dl_sec > 0:
                _agg_st["download"] = _seg_dl_sec
            for _s_res_item in (_seg_stage_timings or []):
                for _sk, _sv in (_s_res_item or {}).items():
                    if isinstance(_sv, (int, float)):
                        _agg_st[_sk] = round(_agg_st.get(_sk, 0.0) + float(_sv), 3)
            entry["stage_timings"] = _agg_st
        else:
            entry["warnings"] = list(recap_res.get("warnings") or [])
            entry["stage_timings"] = dict(recap_res.get("stage_timings") or {})

        entry["status"] = "SUCCESS"
        entry["progress_percent"] = 100
        _recap_set_entry_label(entry, "🎉 Recap Video Generation Completed!", stage="finalize")
        entry["video_path"] = video_path
        entry["audio_path"] = audio_path
        entry["srt_path"] = srt_path
        entry["script_path"] = script_path
        entry["download_urls"] = {
            "video": f"/api/recap/download/{job_id}/video" if video_path else None,
            "audio": f"/api/recap/download/{job_id}/audio" if audio_path else None,
            "srt": f"/api/recap/download/{job_id}/srt" if srt_path else None,
            "script": f"/api/recap/download/{job_id}/script" if script_path else None,
        }
        if segmented_mode:
            entry["segmented"] = True
            entry["parts"] = part_urls
            entry["download_urls"]["parts"] = part_urls
            _seg_charged = int(entry.get("credits_charged") or 0)
            if _seg_charged <= 0:
                # Admin segmented run (owner ruling 2026-11-02):
                # never charged — the note must not claim a cost.
                _seg_cost_note = "(Admin — Credit မကောက်ခံထားပါ။)"
            elif entry.get("tiered_pricing"):
                # Batch AO: the charge followed the duration band,
                # not the part count — the note must not claim a
                # per-part rate the user did not pay.
                _seg_cost_note = (
                    f"(ဗီဒီယို ကြာချိန်အလိုက် စုစုပေါင်း {_seg_charged} Credits သုံးခဲ့ပါတယ်။)"
                )
            else:
                _seg_cost_note = (
                    f"(အပိုင်းတစ်ခုစီ 1 Credit နှုန်းနဲ့ စုစုပေါင်း {_seg_charged} Credits သုံးခဲ့ပါတယ်။)"
                )
            entry["split_note"] = (
                f"မူရင်းဗီဒီယို ၁၀ မိနစ်ထက် ရှည်လို့ အပိုင်း {len(part_urls)} ပိုင်း "
                "ခွဲပြီး လုပ်ပေးထားပါတယ် — အပိုင်းအားလုံး ဒေါင်းလုဒ်ရယူနိုင်ပါတယ်။ "
                + _seg_cost_note
            )

        # Optional Video Splitter post-process (Recapter parity): cut the
        # FINISHED recap into labelled parts at narration pauses. It is
        # part of the same single credit — never charged again. A split
        # failure must not fail the job: the full video above stays the
        # deliverable and the error is surfaced on the status response.
        # Skipped in segmented mode: the parts already ARE the segments.
        if not segmented_mode:
            entry["parts"] = []
        if req.split_video and not segmented_mode and video_path and Path(video_path).is_file():
            try:
                from app.services.recap_engine.recap_pipeline import find_binary
                from app.services.recap_engine.video_splitter import split_rendered_video

                _recap_set_entry_label(entry, "✂️ Cutting into parts (smart cut at narration pauses)...", stage="split")
                split_res = await loop.run_in_executor(
                    None,
                    functools.partial(
                        split_rendered_video,
                        video_path,
                        job_dir / "parts",
                        part_minutes=float(req.split_minutes),
                        smart_cut=True,
                        render_quality=req.render_quality,
                        ffmpeg=find_binary("ffmpeg"),
                        ffprobe=find_binary("ffprobe"),
                        log=lambda m: logger.info(f"Recap job {job_id}: {m}"),
                    ),
                )
                real_parts = [p for p in split_res if p.get("path")]
                if len(real_parts) >= 2:
                    part_urls = []
                    for p in real_parts:
                        entry[f"part{p['n']}_path"] = p["path"]
                        part_urls.append({
                            "n": p["n"],
                            "label": p.get("label") or "",
                            "url": f"/api/recap/download/{job_id}/part{p['n']}",
                        })
                    entry["parts"] = part_urls
                    entry["download_urls"]["parts"] = part_urls
                    logger.info(f"Recap job {job_id}: split into {len(real_parts)} parts")
                else:
                    entry["split_note"] = "Video too short to split — delivered as one full video."
            except Exception as split_err:
                logger.warning(f"Recap job {job_id}: video split failed (full video kept): {split_err}")
                entry["split_error"] = str(split_err)[:300]

        # Delivery-boundary cancel re-check (hunt #5): an admin may
        # have cancelled (and refunded) this job while it rendered.
        # Never write SUCCESS, notify, or deliver in that case --
        # mirror the cancel path's own registry marker and stop. The
        # charge stays exactly as the cancel path left it (refunded).
        if await _recap_job_marked_cancelled(job_id, entry):
            entry["status"] = "FAILED"
            entry["error_message"] = "Cancelled by Administrator"
            logger.info(
                f"Recap job {job_id} was cancelled during rendering; not delivering."
            )
            return

        # Update Job in database
        _succ_written = False
        try:
            # R40 item 15c: record the REAL output duration so history
            # rows show actual seconds instead of the creation estimate.
            _real_seconds = 0
            try:
                from app.services.downloader import probe_local_media as _plm

                _real_seconds = int(round(float(_plm(video_path).get("duration", 0.0) or 0.0)))
            except Exception:
                _real_seconds = 0
            async with AsyncSessionLocal() as session:
                # Batch AJ H5: the SUCCESS write is conditioned on the
                # row still being active — a cancel landing between
                # the check above and this write flips the row to a
                # terminal state first, and the cancel (with its
                # refund) must win over a late SUCCESS + delivery.
                _succ_res = await session.execute(
                    update(Job).where(
                        Job.id == job_id,
                        Job.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
                    ).values(
                        status=JobStatus.SUCCESS.value,
                        result_path=video_path,
                        finished_at=func.now(),
                        **({"seconds_consumed": _real_seconds} if _real_seconds > 0 else {}),
                    )
                )
                await session.commit()
                _succ_written = bool(_succ_res.rowcount)
        except Exception as db_succ_err:
            logger.warning(f"Failed to update Job {job_id} to SUCCESS in DB: {db_succ_err}")

        if not _succ_written:
            entry["status"] = "FAILED"
            entry["error_message"] = "Cancelled by Administrator"
            logger.info(
                f"Recap job {job_id} finished rendering but its row is no "
                "longer active (cancelled); not delivering."
            )
            return

        # Batch AB: terminal state clears the stranded-reset count.
        await _clear_recap_stranded_reset_count(job_id)

        # Phase 5 Option B settlement: use the job's recorded billing_mode
        # from submit time — never re-compute from changed rules/engines.
        if (
            entry.get("billing_mode") == _RECAP_BILLING_CREDIT_PLUS_VOICE
            and not entry.get("voice_settled")
            and not settings.is_admin(user_id)
        ):
            try:
                from app.services.ledger import settle_recap_voice_seconds

                _fb_set = {int(x) for x in (_fb_segs or []) if str(x).isdigit()}
                _total_recs = len(_seg_media_records)
                _dp_switched_away = bool(dp_resume and tts_engine != "voxcpm")
                if _dp_switched_away or (_fb_used and (_total_recs <= 1 or len(_fb_set) >= _total_recs)):
                    _actual_voice_seconds = 0
                else:
                    _vox_recs = [
                        rec
                        for rec in _seg_media_records
                        if not _fb_used or int(rec[0]) not in _fb_set
                    ]
                    _explicit_vals = [rec[3] for rec in _vox_recs if rec[3] is not None]
                    if _explicit_vals and len(_explicit_vals) == len(_vox_recs):
                        _actual_voice_seconds = max(0, math.ceil(sum(_explicit_vals)))
                    else:
                        _measured_voice = _measure_recap_voice_seconds(
                            [rec[1] for rec in _vox_recs],
                            [rec[2] for rec in _vox_recs],
                        )
                        if _measured_voice is not None:
                            _actual_voice_seconds = _measured_voice
                        else:
                            _actual_voice_seconds = int(entry.get("voice_seconds_held") or 0)

                async with AsyncSessionLocal() as _settle_session:
                    _j_row = await _settle_session.get(Job, job_id)
                    _j_state = _load_persisted_recap_state(_j_row) if _j_row is not None else None
                    if _j_state is None or not _j_state.get("voice_settled"):
                        _settle_res = await settle_recap_voice_seconds(
                            _settle_session,
                            user_id,
                            job_id,
                            _actual_voice_seconds,
                            note=f"Recap clone settle ({job_id})",
                        )
                        await _settle_session.commit()
                        entry["voice_settled"] = True
                        entry["voice_seconds_held"] = int(
                            _settle_res["held_seconds"] or entry.get("voice_seconds_held") or 0
                        )
                        entry["voice_seconds_settled"] = int(_settle_res["settled_seconds"])
                        entry["voice_seconds_refunded"] = int(_settle_res["refunded_seconds"])
                    else:
                        entry["voice_settled"] = True
                        entry["voice_seconds_settled"] = _j_state.get("voice_seconds_settled")
                        entry["voice_seconds_refunded"] = int(_j_state.get("voice_seconds_refunded") or 0)
                await _persist_recap_credits_charged(entry)
            except Exception as _settle_err:
                logger.warning(
                    "Recap job %s: Option B voice settlement failed (job remains completed): %s",
                    job_id,
                    _settle_err,
                    exc_info=True,
                )

        await _persist_recap_warnings(entry)

        # Deliverables are stored and ready in Mini App
        # Send Telegram notification alert to user even if they closed the Mini App
        if settings.bot_token and chat_id:
            _recap_notify_lang = "my"
            try:
                from app.db.session import AsyncSessionLocal as _AL
                async with _AL() as _s:
                    _u = await _s.get(User, user_id)
                    if _u and _u.language_code:
                        _recap_notify_lang = _u.language_code
            except Exception:
                _recap_notify_lang = "my"
            try:
                from aiogram import Bot
                from aiogram.client.default import DefaultBotProperties
                from aiogram.enums import ParseMode
                from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
                app_url = settings.mini_app_url
                clean_title = _clean_recap_display_title(entry.get("title"), req.url)
                # Batch AP: the completion message is the ONLY place
                # the companion files are offered, one button per file.
                # Nothing is pushed with the video itself; each extra
                # is sent solely when its button is pressed (handlers
                # in app/handlers/clone.py). The buttons are always
                # shown — a job that never produced a given file gets
                # the honest file-missing reply from its handler.
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=t("clone_btn_miniapp_watch", _recap_notify_lang),
                            web_app=WebAppInfo(url=f"{app_url}?tab=tab-recap&recap_job_id={job_id}")
                        ),
                        InlineKeyboardButton(
                            text=t("clone_btn_send_video", _recap_notify_lang),
                            callback_data=f"send_recap_{job_id}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=t("clone_btn_send_srt", _recap_notify_lang),
                            callback_data=f"send_recap_srt_{job_id}"
                        ),
                        InlineKeyboardButton(
                            text=t("clone_btn_send_recap_audio", _recap_notify_lang),
                            callback_data=f"send_recap_audio_{job_id}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=t("clone_btn_send_script", _recap_notify_lang),
                            callback_data=f"send_recap_script_{job_id}"
                        )
                    ]
                ])
                from app.services.voice_display import (
                    recap_voice_display_name,
                    recap_voice_name_note_params,
                )

                # Batch BK: the completion message names the voice the
                # user actually HEARS (the heard-voice resolver — the
                # pre-BI behaviour; owner decision 2026-11-06 reversing
                # BI-3). When the Gemini engine mapped the picked
                # persona to a differently-named Gemini voice, the
                # polite note below explains the difference; it never
                # stacks on the fallback note, which already explains
                # a substitution when one happened.
                notify_text = t(
                    "recap_notify_text",
                    _recap_notify_lang,
                    clean_title=html.escape(str(clean_title[:60])),
                    voice_name=html.escape(
                        recap_voice_display_name(
                            tts_engine,
                            gemini_voice,
                            req.voice_slug,
                            # Batch BV: an own-voice job names the
                            # user's own voice, not the fallback
                            # persona recorded on the row.
                            own_voice=bool(getattr(req, "voice_ref_path", None)),
                        )
                    ),
                    job_id=job_id,
                )
                if entry.get("fallback_used"):
                    notify_text += t(
                        "recap_fallback_note",
                        _recap_notify_lang,
                        count=len(entry.get("fallback_segments") or []) or 1,
                    )
                else:
                    _voice_name_note = recap_voice_name_note_params(
                        tts_engine, gemini_voice, req.voice_slug
                    )
                    if _voice_name_note:
                        notify_text += t(
                            "recap_voice_name_note",
                            _recap_notify_lang,
                            picked_name=html.escape(_voice_name_note["picked_name"]),
                            heard_name=html.escape(_voice_name_note["heard_name"]),
                            engine_name=_voice_name_note["engine_name"],
                        )
                # Batch BV: disclose the BU auto-trim when the
                # own-voice sample exceeded the 50 s Space cap (the
                # recap twin of worker_ref_trim_note). Orthogonal to
                # the fallback note above — a job can carry both.
                if entry.get("reference_trimmed"):
                    notify_text += t("recap_ref_trim_note", _recap_notify_lang)
                # Batch CG: when Gemini narration ran on the persona
                # fallback because the FREE taste was spent (no route
                # at all), name the own-key way to keep Gemini; and
                # when Google rejected the user's saved key mid-job,
                # say so plainly (the job still delivered).
                if entry.get("gemini_taste_blocked"):
                    notify_text += t("recap_key_hint_note", _recap_notify_lang)
                if entry.get("gemini_key_broken"):
                    notify_text += t("recap_own_key_broken_note", _recap_notify_lang)
                # Batch K: disclose the retention window at delivery
                # time too (it was previously stated only in History).
                # Batch X: the copy states the window in HOURS (24).
                notify_text += t(
                    "recap_retention_note",
                    _recap_notify_lang,
                    retention_hours=_media_retention_days() * 24,
                )
                bot_inst = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
                try:
                    await bot_inst.send_message(
                        chat_id=chat_id,
                        text=notify_text,
                        reply_markup=kb,
                    )
                    # Batch AJ L16: this log line used to sit AFTER the
                    # whole notify try/except, printing "Notification
                    # sent" even when the send had failed (and been
                    # swallowed into the warning below) or was skipped.
                    logger.info(f"Recap job {job_id} successfully finished. Notification sent to Telegram.")
                finally:
                    await bot_inst.session.close()
            except Exception as notify_err:
                logger.warning(f"Could not send recap completion notification to user {chat_id}: {notify_err}")

        # Batch DP: a job that parked earlier and has now completed —
        # retire the choice prompt's buttons so a late tap can't act
        # on a finished job (the callback refuses non-parked jobs
        # anyway; this keeps the chat tidy).
        try:
            async with AsyncSessionLocal() as _dp_session:
                _dp_job = await _dp_session.get(Job, job_id)
            if _dp_job is not None and getattr(_dp_job, "voice_choice_state", None):
                from aiogram import Bot
                from aiogram.client.default import DefaultBotProperties
                from aiogram.enums import ParseMode

                _dp_bot = Bot(token=getattr(settings, "bot" + "_token"), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
                try:
                    await _voice_choice.clear_prompt_for_job(_dp_bot, _dp_job)
                finally:
                    await _dp_bot.session.close()
        except Exception:
            pass

        logger.info(f"Recap job {job_id} successfully finished.")

    except RecapParkRequested:
        # Batch DP (Item 3): a PAID job hit VoxCPM quota exhaustion.
        # PARK — never fail: the charge stays held (NO refund fires),
        # the segments already produced stay in the job dir for the
        # resume, and the owner is asked how to continue. The guarded
        # status write means a cancel that raced the park (its path
        # already settled billing) wins silently instead.
        logger.info(f"Recap job {job_id} parking (WAITING_CHOICE): VoxCPM quota exhausted")
        _dp_state = _voice_choice.new_choice_state(_voice_choice.KIND_RECAP)
        _dp_repark = False
        _dp_lang = "my"
        _dp_parked = False
        try:
            async with AsyncSessionLocal() as session:
                _dp_row = await session.get(Job, job_id)
                _dp_existing = (
                    _voice_choice.parse_choice_state(
                        getattr(_dp_row, "voice_choice_state", None)
                    )
                    if _dp_row is not None
                    else None
                )
                if _dp_existing and _dp_existing.get("prompt_message_id"):
                    # Re-park (spec Item 3): the job resumed on a
                    # wait/no-choice and exhausted again — keep the
                    # recorded state and the SAME prompt message
                    # (refreshed in place below, never re-sent).
                    _dp_state = _dp_existing
                    _dp_state["parked_at"] = time.time()
                    _dp_state.pop("resumed_at", None)
                    _dp_repark = True
                _dp_res = await session.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.status == JobStatus.RUNNING.value)
                    .values(
                        status=JobStatus.WAITING_CHOICE.value,
                        voice_choice_state=_voice_choice.dump_choice_state(_dp_state),
                    )
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
                _dp_parked = _dp_res.rowcount == 1
                if _dp_parked:
                    _dp_user = await session.get(User, user_id)
                    _dp_lang = getattr(_dp_user, "language_code", None) or "my"
        except Exception as park_err:
            logger.warning(f"Recap job {job_id}: park state write failed: {park_err}")
        if _dp_parked:
            entry["status"] = "WAITING_CHOICE"
            entry["progress_label"] = t("dp_parked_label", _dp_lang)
            # Persist the held charge state so a later re-dispatch
            # restores it verbatim (the refund guards read it too).
            await _persist_recap_credits_charged(entry)
            # Ask the owner (best-effort: a prompt-less park still
            # resumes on capacity via the periodic resumer).
            try:
                from aiogram import Bot
                from aiogram.client.default import DefaultBotProperties
                from aiogram.enums import ParseMode

                _dp_bot = Bot(token=getattr(settings, "bot" + "_token"), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
                try:
                    if _dp_repark:
                        await _voice_choice.refresh_choice_prompt(
                            _dp_bot,
                            chat_id=_dp_state.get("prompt_chat_id") or chat_id or user_id,
                            message_id=_dp_state.get("prompt_message_id"),
                            job_id=job_id,
                            kind=_voice_choice.KIND_RECAP,
                            lang=_dp_lang,
                        )
                    else:
                        _dp_msg_id = await _voice_choice.send_choice_prompt(
                            _dp_bot,
                            chat_id=chat_id or user_id,
                            job_id=job_id,
                            kind=_voice_choice.KIND_RECAP,
                            lang=_dp_lang,
                        )
                        if _dp_msg_id:
                            _dp_state["prompt_chat_id"] = chat_id or user_id
                            _dp_state["prompt_message_id"] = _dp_msg_id
                            async with AsyncSessionLocal() as session:
                                await session.execute(
                                    update(Job)
                                    .where(Job.id == job_id)
                                    .values(voice_choice_state=_voice_choice.dump_choice_state(_dp_state))
                                )
                                await session.commit()
                finally:
                    await _dp_bot.session.close()
            except Exception as prompt_err:
                logger.warning(f"Recap job {job_id}: park prompt failed: {prompt_err}")
        return

    except asyncio.CancelledError:
        # Cancellation is BaseException, not Exception: without this the
        # job would vanish with no status and no refund.
        entry["status"] = "FAILED"
        entry["error_message"] = "Cancelled"
        try:
            from app.db.session import AsyncSessionLocal
            async with AsyncSessionLocal() as session:
                await session.execute(
                    update(Job).where(Job.id == job_id).values(
                        status=JobStatus.CANCELLED.value,
                        error_message="Cancelled",
                        finished_at=func.now(),
                    )
                )
                await session.commit()
        except Exception:
            pass
        # Batch AB: terminal state clears the stranded-reset count.
        await _clear_recap_stranded_reset_count(job_id)
        if credit_deducted and not entry.get("credit_refunded"):
            await _refund_recap_job_charge(user_id, entry)
        # Batch W (2026-11-04): a cancelled job's media bulk is waste
        # exactly like a failed one's — strip it now (see the generic
        # failure handler below) instead of at the retention sweep.
        try:
            _strip_failed_recap_job_media(job_dir)
        except Exception as strip_err:
            logger.warning(f"Recap job {job_id}: cancelled-job media strip failed: {strip_err}")
        raise
    except Exception as e:
        logger.error(f"Background recap job {job_id} error: {e}", exc_info=True)
        entry["status"] = "FAILED"
        # Journey audit 2026-11-02: the raw exception was stored
        # verbatim AND quoted to the user in the failure notice below —
        # a yt-dlp "Failed to extract any player response; please
        # report this issue on https://github.com/yt-dlp/…" string
        # reached a user raw. Classify source errors to the curated
        # Burmese copy before storing/sending; the raw text stays in
        # the log line above.
        # Audit fix 2026-11-03: the fallback is now default-deny —
        # an UNclassified error no longer falls back to str(e) (raw
        # ffmpeg stderr, local paths and library internals reached
        # users that way); it becomes the generic technical-problem
        # message below. The refund wording/flow is unchanged.
        from app.services.downloader import classify_source_error

        display_err = classify_source_error(str(e), req.url) or (
            "⚠️ နည်းပညာဆိုင်ရာ အမှားတစ်ခု ဖြစ်ပေါ်ခဲ့ပါသည်။ "
            "ခဏစောင့်ပြီး ပြန်စမ်းကြည့်ပါ ခင်ဗျာ။"
        )
        entry["error_message"] = display_err
        try:
            from app.db.session import AsyncSessionLocal
            async with AsyncSessionLocal() as session:
                await session.execute(
                    update(Job).where(Job.id == job_id).values(
                        status=JobStatus.FAILED.value,
                        error_message=display_err,
                        finished_at=func.now(),
                    )
                )
                await session.commit()
        except Exception as ref_err:
            logger.warning(f"Recap job status update failed: {ref_err}")
        # Batch AB: terminal state clears the stranded-reset count.
        await _clear_recap_stranded_reset_count(job_id)
        # Refund in its own commit, after the status update, so a
        # status-write failure cannot swallow the refund. The registry
        # flag makes a second failure handler for the same job a no-op.
        # The amount is whatever this job was actually charged: 1
        # credit for a normal job, N for a segmented one.
        if credit_deducted and not entry.get("credit_refunded"):
            await _refund_recap_job_charge(user_id, entry)

        # --- Send Telegram DM about Failure (batch K) ---
        # Shared with the SCRIPT_READY expiry sweep so every
        # terminal failure surface carries the same locale copy:
        # job title, the curated display_err (never raw engine
        # text) and a refund line only when a refund was issued.
        # The helper's failure_notified flag keeps it once-only.
        try:
            await _send_recap_failure_dm(
                job_id=job_id,
                chat_id=chat_id,
                title=_clean_recap_display_title(
                    entry.get("title"),
                    entry.get("url") or entry.get("source_path"),
                ),
                reason=display_err,
                credits_refunded=(
                    int(entry.get("credits_refunded") or 0)
                    if entry.get("credit_refunded")
                    else 0
                ),
                entry=entry,
            )
        except Exception as dm_err:
            logger.warning(f"Could not send DM for failed job {job_id}: {dm_err}")

        # Batch W (2026-11-04): strip the failed job's media bulk now.
        # A failed job's downloaded source, split segments and render
        # intermediates are pure waste — user retry creates a NEW job
        # and admin retry re-dispatches without resume_source (it
        # re-downloads and re-renders) — but they used to sit on the
        # persistent disk for the full retention window, which
        # is how the disk filled and every new job started dying on
        # "[Errno 28] No space left on device". The small JSON state
        # (plans, checkpoints, fallback markers) stays for inspection;
        # the record sweep removes the rest with the row at the window.
        try:
            _strip_failed_recap_job_media(job_dir)
        except Exception as strip_err:
            logger.warning(f"Recap job {job_id}: failed-job media strip failed: {strip_err}")


def _sweep_expired_download_dirs(cutoff: datetime) -> int:
    """Remove downloader cache dirs older than the retention cutoff.

    storage/downloads/<download_id>/ dirs were only ever pruned by
    the per-user newest-10 rule at fetch time, so downloads of users
    who never fetched again stayed on disk forever (audit fix
    2026-11-03). Age basis matches the job-media sweep: the dir's
    recorded creation time (meta.json ``created_at``), falling back
    to the directory mtime when the meta is missing or unreadable.
    Whole dirs are removed; the per-user newest-10 pruning in
    _dl_prune_user_downloads is unchanged.
    """
    root = _dl_root()
    if not root.is_dir():
        return 0
    removed = 0
    for d in root.iterdir():
        try:
            if not d.is_dir():
                continue
            created_ts: Optional[float] = None
            meta = _dl_read_meta(d.name)
            if meta and meta.get("created_at"):
                try:
                    created_ts = datetime.fromisoformat(
                        str(meta["created_at"]).replace("Z", "+00:00")
                    ).timestamp()
                except (ValueError, TypeError):
                    created_ts = None
            if created_ts is None:
                created_ts = d.stat().st_mtime
            if created_ts < cutoff.timestamp():
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


# ── Batch W (2026-11-04): filesystem-level storage retention ──────────────
# cleanup_expired_jobs below is record-driven: it removes only the files
# a Job row deleted in the same run points at. The 2026-11-04 disk-full
# incident (10 GB persistent disk exhausted; six recap jobs dead on
# "[Errno 28] No space left on device") showed what accumulates outside
# that view:
#   * recap_output dirs whose Job row is already gone (a purge whose
#     file removal failed, a job that died before its row existed) —
#     never revisited by the record sweep;
#   * FAILED/CANCELLED jobs keep their whole dir — downloaded source,
#     split segments, render intermediates — for the full window;
#   * temp/recap_preview_src (a full source video per previewed URL)
#     had NO cleanup at all;
#   * voice-worker temp dirs orphaned by a dead process, and clone-flow
#     uploads (temp/inputs, temp/voxcpm_refs) abandoned mid-conversation.
# The helpers below sweep exactly those, and only those: fixed roots
# under settings.storage_root, age thresholds, reference checks against
# surviving Job rows, and symlinks never followed or removed through.
_FAILED_STRIP_AGE_SECONDS = 24 * 3600
_PREVIEW_CACHE_AGE_SECONDS = 24 * 3600
_TEMP_JOB_DIR_AGE_SECONDS = 24 * 3600
_ABANDONED_UPLOAD_AGE_SECONDS = 24 * 3600
_JOB_MEDIA_SUFFIXES = {
    ".mp4", ".mkv", ".mov", ".webm", ".m4v", ".ts", ".avi",
    ".mp3", ".wav", ".ogg", ".m4a", ".part", ".ytdl",
}
_FINAL_RENDER_NAME = "final_edge_tts_smart_sync.mp4"
_KNOWN_TEMP_SUBDIRS = {"inputs", "voxcpm_refs", "recap_uploads", "recap_preview_src"}
_TERMINAL_JOB_STATUSES = {
    JobStatus.SUCCESS.value,
    JobStatus.FAILED.value,
    JobStatus.CANCELLED.value,
}


def _dir_size_and_newest_mtime(root: Path) -> tuple[int, float]:
    """(total bytes, newest mtime) under ``root``, never following
    symlinks — a symlinked dir is neither descended into nor counted."""
    total = 0
    newest = 0.0
    try:
        newest = root.lstat().st_mtime
    except OSError:
        pass
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not Path(dirpath, d).is_symlink()]
        for name in filenames:
            p = Path(dirpath, name)
            try:
                if p.is_symlink():
                    continue
                st = p.stat()
            except OSError:
                continue
            total += st.st_size
            if st.st_mtime > newest:
                newest = st.st_mtime
    return total, newest


def _safe_rmtree(target: Path, storage_root: Path) -> bool:
    """Remove a directory tree, guarded: only a real (non-symlink)
    directory strictly inside the storage root is ever removed."""
    try:
        if target.is_symlink() or not target.is_dir():
            return False
        resolved = target.resolve()
        root_resolved = storage_root.resolve()
        if resolved == root_resolved or root_resolved not in resolved.parents:
            return False
        shutil.rmtree(target, ignore_errors=True)
        return not target.exists()
    except Exception:
        return False


def _strip_failed_recap_job_media(job_dir: Path) -> tuple[int, int]:
    """Remove the bulky media of a FAILED/CANCELLED recap job's dir.

    Keeps the small state that is still read afterwards — every *.json
    (script plans, checkpoints, and the fallback-transparency markers
    the status/history serializers read via _recap_fallback_info) and
    a completed final render — and removes the gigabytes: the
    downloaded source, source_segments/ and parts/, the TTS/video
    render intermediates, and partial downloads. No live flow reads
    the removed files: user retry creates a NEW job, and admin retry
    re-dispatches without resume_source, so it re-downloads and
    re-renders into the same dir. Symlinks are never followed.
    Returns (files_removed, bytes_freed).
    """
    removed = 0
    freed = 0
    try:
        if job_dir.is_symlink() or not job_dir.is_dir():
            return removed, freed
        for dirpath, dirnames, filenames in os.walk(job_dir, followlinks=False):
            dirnames[:] = [d for d in dirnames if not Path(dirpath, d).is_symlink()]
            for name in filenames:
                if name == _FINAL_RENDER_NAME:
                    continue
                if Path(name).suffix.lower() not in _JOB_MEDIA_SUFFIXES:
                    continue
                p = Path(dirpath, name)
                try:
                    if p.is_symlink():
                        continue
                    freed += p.stat().st_size
                    p.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    continue
    except Exception:
        pass
    return removed, freed


async def _sweep_storage_orphans(*, retention_cutoff_ts: float) -> dict:
    """Filesystem-level retention for artefacts no Job row covers.

    Companion to cleanup_expired_jobs (which calls this): the record
    sweep handles rows + their files inside the retention window; this
    pass handles the orphans and the waste classes listed above.
    Finished outputs and anything a surviving row still references are
    never touched, and nothing younger than its class threshold is
    removed. Returns {"dirs": n, "files": n, "bytes": n} and logs a
    one-line summary whenever it removes anything.
    """
    stats = {"dirs": 0, "files": 0, "bytes": 0}
    storage_root = settings.storage_root
    now_ts = time.time()
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(
                    Job.id, Job.status, Job.source_audio_path,
                    Job.result_path, Job.recap_ref_audio_path,
                )
            )).all()
            # Batch AJ M1: slip files a surviving PaymentRequest still
            # references are audit evidence, never orphans.
            slip_rows = (await session.execute(
                select(PaymentRequest.slip_url)
            )).all()
    except Exception as e:
        logger.warning(f"Storage orphan sweep skipped (job index unavailable): {e}")
        return stats
    referenced_slip_names: set[str] = {
        Path(url).name for (url,) in slip_rows if url
    }

    status_by_id: dict[str, str] = {}
    referenced_names: set[str] = set()
    referenced_stems: set[str] = set()
    for jid, status, src_path, res_path, ref_audio_path in rows:
        status_by_id[str(jid)] = status
        # Batch BV: recap_ref_audio_path joins the referenced set —
        # without it, the abandoned-upload sweep below could delete
        # an own-voice sample while its job is QUEUED or parked at
        # SCRIPT_READY (parking can hold a job up to 24 h).
        for p in (src_path, res_path, ref_audio_path):
            if p:
                referenced_names.add(Path(p).name)
                referenced_stems.add(Path(p).stem)

    # 1. recap_output: orphan dirs (no surviving row) older than the
    #    retention window go whole; FAILED/CANCELLED dirs older than
    #    24h get their media stripped (their rows — and the small JSON
    #    state — are removed by the record sweep at the window).
    recap_root = storage_root / "recap_output"
    if recap_root.is_dir() and not recap_root.is_symlink():
        for d in list(recap_root.iterdir()):
            try:
                if d.is_symlink() or not d.is_dir():
                    continue
                size, newest = _dir_size_and_newest_mtime(d)
                status = status_by_id.get(d.name)
                if status is None:
                    if newest < retention_cutoff_ts and _safe_rmtree(d, storage_root):
                        stats["dirs"] += 1
                        stats["bytes"] += size
                elif status in (JobStatus.FAILED.value, JobStatus.CANCELLED.value):
                    if now_ts - newest > _FAILED_STRIP_AGE_SECONDS:
                        f_removed, f_bytes = _strip_failed_recap_job_media(d)
                        stats["files"] += f_removed
                        stats["bytes"] += f_bytes
            except OSError:
                continue

    # 2. temp/recap_preview_src: a pure derived cache (frames and the
    #    source are re-fetched on demand), keyed by URL. 24h of quiet
    #    means nobody is previewing it any more.
    preview_root = storage_root / "temp" / "recap_preview_src"
    if preview_root.is_dir() and not preview_root.is_symlink():
        for d in list(preview_root.iterdir()):
            try:
                if d.is_symlink() or not d.is_dir():
                    continue
                size, newest = _dir_size_and_newest_mtime(d)
                if now_ts - newest > _PREVIEW_CACHE_AGE_SECONDS and _safe_rmtree(d, storage_root):
                    stats["dirs"] += 1
                    stats["bytes"] += size
            except OSError:
                continue

    temp_root = storage_root / "temp"
    if temp_root.is_dir() and not temp_root.is_symlink():
        # 3. Voice-worker per-job temp dirs (temp/<job.id>): the worker
        #    removes its own in a finally block, so a survivor older
        #    than 24h belongs to a dead process or a terminal job.
        for d in list(temp_root.iterdir()):
            try:
                if d.is_symlink() or not d.is_dir() or d.name in _KNOWN_TEMP_SUBDIRS:
                    continue
                status = status_by_id.get(d.name)
                if status is not None and status not in _TERMINAL_JOB_STATUSES:
                    continue
                size, newest = _dir_size_and_newest_mtime(d)
                if now_ts - newest > _TEMP_JOB_DIR_AGE_SECONDS and _safe_rmtree(d, storage_root):
                    stats["dirs"] += 1
                    stats["bytes"] += size
            except OSError:
                continue
        # 4. Abandoned clone-flow uploads: files in temp/inputs and
        #    temp/voxcpm_refs that no surviving Job row references and
        #    that have sat untouched for 24h (a live conversation or a
        #    queued job is never that old, and referenced files are
        #    kept regardless of age).
        for sub in ("inputs", "voxcpm_refs"):
            sub_dir = temp_root / sub
            if sub_dir.is_symlink() or not sub_dir.is_dir():
                continue
            for f in list(sub_dir.iterdir()):
                try:
                    if f.is_symlink() or not f.is_file():
                        continue
                    if f.name in referenced_names or f.stem in referenced_stems:
                        continue
                    st = f.stat()
                    if now_ts - st.st_mtime > _ABANDONED_UPLOAD_AGE_SECONDS:
                        f.unlink(missing_ok=True)
                        stats["files"] += 1
                        stats["bytes"] += st.st_size
                except OSError:
                    continue
        # 5. Stray partial downloads at the temp root.
        for f in list(temp_root.iterdir()):
            try:
                if f.is_symlink() or not f.is_file():
                    continue
                if f.suffix.lower() not in (".part", ".ytdl"):
                    continue
                st = f.stat()
                if now_ts - st.st_mtime > _FAILED_STRIP_AGE_SECONDS:
                    f.unlink(missing_ok=True)
                    stats["files"] += 1
                    stats["bytes"] += st.st_size
            except OSError:
                continue

    # 6. Batch AJ M1: receipts/ — slip images no surviving
    #    PaymentRequest references (deleted rows, rejected-then-purged
    #    submissions) older than the retention window. Referenced
    #    slips are kept regardless of age (audit evidence for the
    #    request they belong to).
    receipts_root = storage_root / "receipts"
    if receipts_root.is_dir() and not receipts_root.is_symlink():
        for f in list(receipts_root.iterdir()):
            try:
                if f.is_symlink() or not f.is_file():
                    continue
                if f.name in referenced_slip_names:
                    continue
                st = f.stat()
                if st.st_mtime < retention_cutoff_ts:
                    f.unlink(missing_ok=True)
                    stats["files"] += 1
                    stats["bytes"] += st.st_size
            except OSError:
                continue

    # 7. Batch AJ L6: outputs/ and audio_out/ — result files whose
    #    owning row is gone (e.g. a worker upload-result whose DB
    #    commit then failed) were previously permanent. Same rule:
    #    unreferenced by any surviving Job row AND older than the
    #    retention window.
    for out_name in ("outputs", "audio_out"):
        out_root = storage_root / out_name
        if not out_root.is_dir() or out_root.is_symlink():
            continue
        for f in list(out_root.iterdir()):
            try:
                if f.is_symlink() or not f.is_file():
                    continue
                if f.name in referenced_names or f.stem in referenced_stems:
                    continue
                st = f.stat()
                if st.st_mtime < retention_cutoff_ts:
                    f.unlink(missing_ok=True)
                    stats["files"] += 1
                    stats["bytes"] += st.st_size
            except OSError:
                continue

    if stats["dirs"] or stats["files"]:
        logger.info(
            "Storage orphan sweep (Batch W): removed %d dirs, %d files, freed %.1f MB.",
            stats["dirs"], stats["files"], stats["bytes"] / (1024 * 1024),
        )
    return stats


async def cleanup_expired_jobs(retention_days: Optional[int] = None) -> int:
    """
    Purge jobs and generated media files older than the media retention
    window (settings.media_retention_days, default 1 day = 24 hours) to free disk
    space. Removes DB records and physical files (recap output dirs,
    generated voice clones).

    Ordering is deliberate: the DB record is deleted and committed BEFORE
    any file is touched. The old sweep removed the files first, so a failed
    record delete left a SUCCESS job whose media was already gone — the
    permanent false "Media Expired" state. Now, if the record delete fails,
    the files stay put and the job keeps working; a failed file removal
    only leaves an orphan file on disk, never a broken job.
    """
    days = retention_days if retention_days is not None else _media_retention_days()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    deleted_count = 0
    file_targets: list[tuple[str, list[Path]]] = []

    try:
        from app.db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            stmt = select(Job).where(
                Job.created_at < cutoff,
                # Terminal statuses only (fix 2026-10-31): the sweep
                # previously matched on age alone, so a QUEUED/RUNNING
                # job older than the window — e.g. one re-queued after
                # a restart — could have its record and files deleted
                # mid-flight. PENDING/QUEUED/RUNNING jobs are never
                # swept, however old they are.
                Job.status.in_([
                    JobStatus.SUCCESS.value,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                ]),
            )
            res = await session.execute(stmt)
            expired_jobs = res.scalars().all()

            for j in expired_jobs:
                # Snapshot every on-disk target BEFORE the record goes away.
                targets: list[Path] = []
                if j.job_type == JobType.RECAP.value or j.id.startswith("rc_"):
                    targets.append(settings.storage_root / "recap_output" / j.id)
                if j.result_path:
                    targets.append(Path(j.result_path))
                if j.source_audio_path:
                    targets.append(Path(j.source_audio_path))
                file_targets.append((j.id, targets))
                await session.delete(j)
                deleted_count += 1

            await session.commit()
    except Exception as e:
        logger.error(f"Error during media-retention cleanup (records): {e}")
        return 0

    # Records are gone; now remove the files (best-effort — a failure here
    # can only orphan files, it can never break a live job record).
    for job_id, targets in file_targets:
        for target in targets:
            try:
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                elif target.is_file():
                    target.unlink(missing_ok=True)
            except Exception as file_err:
                logger.warning(f"Error removing media for purged job {job_id}: {file_err}")
        RECAP_JOBS_REGISTRY.pop(job_id, None)

    # Clean memory registry keys older than the retention window
    now_ts = time.time()
    expired_keys = [k for k, v in RECAP_JOBS_REGISTRY.items() if now_ts - v.get("created_at", 0) > days * 86400]
    for k in expired_keys:
        RECAP_JOBS_REGISTRY.pop(k, None)

    # Orphaned temp artefacts (hunt D3): the record sweep above only
    # reaches files a Job row points at. Recap uploads that were
    # previewed but never submitted (or whose job row is long gone)
    # and the derived frame-cache JPGs accumulated forever. Rules:
    # never delete anything younger than the window, and never delete
    # a file (or its _thumb sibling) that ANY surviving job row still
    # references — a non-terminal job may still need its source.
    orphan_count = 0
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(Job.source_audio_path, Job.result_path)
            )).all()
        referenced_names: set[str] = set()
        referenced_stems: set[str] = set()
        for src_path, res_path in rows:
            for p in (src_path, res_path):
                if p:
                    referenced_names.add(Path(p).name)
                    referenced_stems.add(Path(p).stem)
        cutoff_ts = cutoff.timestamp()

        def _is_orphan(f: Path) -> bool:
            stem = f.stem[:-6] if f.stem.endswith("_thumb") else f.stem
            return (
                f.name not in referenced_names
                and stem not in referenced_stems
                and f.stat().st_mtime < cutoff_ts
            )

        uploads_dir = settings.storage_root / "temp" / "recap_uploads"
        if uploads_dir.is_dir():
            for f in uploads_dir.iterdir():
                try:
                    if f.is_file() and _is_orphan(f):
                        f.unlink(missing_ok=True)
                        orphan_count += 1
                except OSError:
                    pass
        # Frame-cache JPGs are a pure derived cache (regenerated from
        # the source file on demand), so age alone decides for them.
        temp_dir = settings.storage_root / "temp"
        if temp_dir.is_dir():
            for f in temp_dir.glob("frame_*.jpg"):
                try:
                    if f.is_file() and f.stat().st_mtime < cutoff_ts:
                        f.unlink(missing_ok=True)
                        orphan_count += 1
                except OSError:
                    pass
    except Exception as e:
        logger.warning(f"Orphan temp cleanup failed: {e}")

    # Downloader cache sweep (audit fix 2026-11-03): same retention
    # window and age basis as the job media above. Best-effort — a
    # failure here must not fail the whole cleanup.
    try:
        downloads_removed = _sweep_expired_download_dirs(cutoff)
        if downloads_removed > 0:
            logger.info(f"Auto-cleaned {downloads_removed} expired downloader caches (>{days}d).")
    except Exception as e:
        logger.warning(f"Downloader cache sweep failed: {e}")

    # Filesystem-level orphan sweep (Batch W, 2026-11-04): artefacts no
    # Job row covers — orphaned recap_output dirs, FAILED-job media,
    # the preview-source cache, dead voice-worker temp dirs, abandoned
    # clone uploads. Best-effort like the sections above: a failure
    # here must not fail the whole cleanup.
    try:
        await _sweep_storage_orphans(retention_cutoff_ts=cutoff.timestamp())
    except Exception as e:
        logger.warning(f"Storage orphan sweep failed: {e}")

    # Batch AJ L17: broadcast delivery rows grew one row per recipient
    # per campaign forever (the FAQ cache in the same family is capped
    # by the GroupAI cluster). Campaign summaries keep the counts;
    # per-recipient rows older than 90 days are purged.
    try:
        from app.db.session import AsyncSessionLocal
        from app.models.schema import BroadcastDelivery

        async with AsyncSessionLocal() as session:
            await session.execute(
                delete(BroadcastDelivery).where(
                    BroadcastDelivery.sent_at.is_not(None),
                    BroadcastDelivery.sent_at < datetime.now(timezone.utc) - timedelta(days=90),
                )
            )
            await session.commit()
    except Exception as e:
        logger.warning(f"Broadcast delivery retention purge failed: {e}")

    if deleted_count > 0:
        logger.info(f"Auto-cleaned {deleted_count} expired (>{days}d) jobs and media files.")
    if orphan_count > 0:
        logger.info(f"Auto-cleaned {orphan_count} orphaned temp artefacts (>{days}d).")

    return deleted_count


@api_router.post("/recap/generate")
async def create_recap_job(
    req: RecapConfigRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    _gate: User = Depends(require_gate_verified),
):
    """Submit 1-Click Pro Recap Job with complete ratio, audio, subtitle, and transformation settings."""
    # Encoding repair at intake (hunt #4 A1): a Zawgyi-typed title is
    # displayed on the output video, so store/speak it as Unicode. The
    # request object is mutated before every downstream use (display
    # title, persisted recap state, registry entry, background job).
    if req.title:
        req.title = convert_zawgyi_to_unicode(req.title)
    # SSRF + local-file containment, before any probe, deduction, or queueing.
    _req_url = (req.url or "").strip()
    if _req_url.startswith(("http://", "https://")):
        try:
            from app.services.downloader import extract_first_url as _extract_first, unshorten_url as _unshorten
            from app.services.url_safety import validate_public_url as _validate_url

            # Redirect-following is synchronous network work — off the
            # event loop like the pre-check below (deep-hunt #4 D1).
            _canon = await asyncio.to_thread(_unshorten, _extract_first(_req_url) or _req_url)
            _validate_url(_canon)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=400, detail="This video URL is not allowed")
    elif _req_url and not _req_url.startswith(("http://", "https://")):
        # Local source: only files the user uploaded into storage are
        # allowed, never arbitrary server paths.
        try:
            _resolved = Path(_req_url.strip("'\"")).resolve()
            _storage = settings.storage_root.resolve()
            if not _resolved.is_relative_to(_storage):
                raise HTTPException(status_code=400, detail="Local video path is not allowed")
            # Batch AJ H4: containment alone is not ownership — any
            # authenticated user could name ANOTHER user's uploaded
            # file (paths are guessable) and recap it. Non-admins may
            # only use sources they own (same resolver the frame
            # endpoint uses); admins keep the service-wide exemption.
            if not settings.is_admin(user.id):
                _owner_id = await _recap_frame_owner_id(db, _resolved)
                if _owner_id != user.id:
                    raise HTTPException(status_code=400, detail="Local video path is not allowed")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=400, detail="Local video path is not allowed")

    # Batch AJ M1: refuse new recap work when the disk is nearly full
    # (before any charge) — see _require_disk_headroom.
    _require_disk_headroom()

    # Batch DF: a local upload with NO audio track can never be
    # recapped — the planner's first step extracts the speech track
    # and ffmpeg dies on the missing stream map. Until now the job
    # was charged, failed in ~1 s behind the generic message, and
    # refunded, so users retried the same silent file blind (four
    # attempts, 2026-11-10). Refuse here, before any credit moves,
    # with the exact reason. Only a POSITIVE probe blocks: a file
    # the probe cannot read keeps the existing behaviour.
    if _req_url and not _req_url.startswith(("http://", "https://")):
        from app.services.downloader import NO_AUDIO_TRACK_MESSAGE, probe_local_media

        _df_meta = await asyncio.to_thread(
            probe_local_media, str(Path(_req_url.strip("'\"")).resolve())
        )
        if _df_meta.get("has_video") and not _df_meta.get("has_audio"):
            raise HTTPException(status_code=400, detail=NO_AUDIO_TRACK_MESSAGE)

    # Narrator slug allowlist (stored-XSS fix, hunt #2 X1): voice_slug
    # is persisted as the job's preset_voice and rendered in the admin
    # Mini App, so it must never be an arbitrary client string. The
    # legitimate set is the curated personas (the only voices the
    # recap UI offers and the worker resolves) plus the legacy Edge
    # slug the worker still maps. Checked before any credit moves.
    _voice_slug = (req.voice_slug or "").strip().lower()
    if get_curated_voice(_voice_slug) is None and _voice_slug not in RECAP_LEGACY_EDGE_VOICE_SLUGS:
        raise HTTPException(
            status_code=400,
            detail="ရွေးထားတဲ့ အသံ မမှန်ကန်ပါ — အသံ ပြန်ရွေးပေးပါ။",
        )

    is_vip = (user.account_category == AccountCategory.PURCHASED.value) or settings.is_admin(user.id)
    # Batch AU: clamp before the config is persisted or dispatched,
    # so the registry, Job row and background runner all carry the
    # quality this tier may actually use.
    _clamp_recap_render_quality(req, user=user, user_id=user.id, is_vip=is_vip)
    # Batch AW: same submit choke point for the narration engine —
    # the persisted config and the dispatched runner carry the engine
    # this tier may actually use (free: gemini/edge, never voxcpm).
    _clamp_recap_tts_engine(req, user=user, user_id=user.id, is_vip=is_vip)
    # Batch BV: own-voice reference. voice_ref_path is server-only —
    # whatever the client sent is discarded here and recomputed from
    # the upload handle below, so a forged path can never take
    # effect. The tier clamp runs at this same choke point (free:
    # ref dropped, job proceeds on the persona); resolution then
    # validates handle + ownership + existence BEFORE any credit
    # moves, so a bad handle is a clean 400, never a charged job.
    # A ref submitted with a non-clone engine is meaningless (the
    # card only exists on the Persona Voice engine) and is dropped
    # the same way.
    req.voice_ref_path = None
    _clamp_recap_voice_ref(req, user=user, user_id=user.id, is_vip=is_vip)
    if req.voice_ref_id:
        if req.tts_engine == "voxcpm":
            req.voice_ref_path = str(_resolve_recap_voice_ref(req.voice_ref_id, user))
        else:
            req.voice_ref_id = None
    # Batch DD: custom subtitle font — server-only path recomputed
    # from the handle at the same choke point, before any credit
    # moves (a bad handle is a clean 400, never a charged job).
    req.sub_font_custom_path = None
    if req.sub_font_custom_id:
        req.sub_font_custom_path = str(
            _resolve_recap_sub_font(req.sub_font_custom_id, user)
        )
    credits = getattr(user, "recap_credits", 0) or 0

    # Credit gate (money fix 2026-10-31): EVERY non-admin needs at
    # least one recap credit to submit. Previously this gate skipped
    # PURCHASED ("VIP") users entirely while the deduction below only
    # ran when the balance was positive — so anyone who had bought a
    # voice-only plan (recap_credits == 0) could run recaps free
    # forever. VIP status keeps its duration-tier benefits; it no
    # longer bypasses the credit check, exactly like the segmented
    # path's _recap_segment_balance_ok pre-check already required.
    if not settings.is_admin(user.id) and credits <= 0:
        raise HTTPException(
            status_code=403,
            detail="Recap Video ဖန်တီးရန် Credit မလုံလောက်တော့ပါ။ VIP tab တွင် Recap Plan ဝယ်ယူနိုင်ပါသည်။",
        )

    # Duration cap (owner's design): 10 min per job for everyone,
    # VIP included; free tier 2 min. Admins are exempt. A VIP source
    # over the cap is accepted ONLY via the split path (no script
    # review): the worker cuts the source into ≤cap segments and
    # processes each part separately.
    is_admin_user = settings.is_admin(user.id)
    max_duration_allowed = _recap_max_duration_allowed(is_vip, is_admin_user)
    checked_duration = req.duration_seconds

    # Early duration validation via yt-dlp or ffprobe before deducting credits or queueing
    if (not checked_duration or checked_duration <= 0) and req.url:
        try:
            if req.url.startswith("http://") or req.url.startswith("https://"):
                from app.services.downloader import extract_first_url, unshorten_url, MediaDownloader
                # Both calls are synchronous network work — keep them
                # off the single shared event loop (deep-hunt #4 D1).
                can_url = await asyncio.to_thread(
                    unshorten_url, extract_first_url(req.url) or req.url
                )
                info = await asyncio.to_thread(MediaDownloader().get_info, can_url)
                checked_duration = info.get("duration", 0) or 0
            elif os.path.isfile(req.url):
                from app.services.recap_engine.core import probe_duration, find_ffprobe
                ffprobe_bin = find_ffprobe()
                checked_duration = probe_duration(ffprobe_bin, Path(req.url))
        except Exception as probe_err:
            logger.warning(f"Could not pre-probe video duration for {req.url}: {probe_err}")

    submit_decision = "ok"
    estimated_parts = None
    # Batch AO: the tiered-pricing switch state at submit pins this
    # job's pricing mode (stamped on the registry entry and the
    # persisted state below); the balance pre-checks and the response
    # quote follow it. While the switch is off, quoted_total_credits
    # stays None and every check below is the legacy one, untouched.
    tiered_on = _tiered_recap_pricing_enabled()
    quoted_total_credits = None
    # Batch BE: the Max render-quality surcharge rides the same pin —
    # computed once here from the AU-clamped request above, so the
    # quote, both balance pre-checks below and the worker's top-up
    # target all include the identical +1. Zero while the switch is
    # off, so every legacy check below is untouched.
    submit_max_surcharge = (
        _recap_max_quality_surcharge(req.render_quality) if tiered_on else 0
    )
    if checked_duration and checked_duration > max_duration_allowed:
        submit_decision = _recap_submit_duration_decision(
            is_vip=is_vip,
            is_admin=is_admin_user,
            split_video=bool(req.split_video),
            script_review=bool(req.script_review),
            duration_seconds=float(checked_duration),
        )
        decision = submit_decision
        if decision != "segment":
            if is_vip and req.split_video and req.script_review:
                hint = _SCRIPT_REVIEW_SPLIT_HINT
            elif is_vip:
                hint = "'အပိုင်းခွဲ ထုတ်မယ်' ကို ဖွင့်ပြီး ၁၀ မိနစ်ထက် ရှည်တဲ့ ဗီဒီယိုတွေကို အပိုင်းလိုက် ထုတ်နိုင်ပါတယ်။"
            else:
                hint = "၁၀ မိနစ်အထိ ဗီဒီယို ရှည်ရှည် ဖန်တီးလိုပါက Plan ဝယ်ယူနိုင်ပါတယ်။"
            from app.services.downloader import recap_over_limit_message

            raise HTTPException(
                status_code=400,
                detail=recap_over_limit_message(float(checked_duration), float(max_duration_allowed), is_vip) + " " + hint,
            )
        # Segmented path (owner decision 2026-10-30): 1 credit PER
        # PART. Estimate the part count from the duration and the
        # chosen split size (the same target the segmenter will use)
        # and pre-check the balance now, so a user who clearly cannot
        # cover the total is blocked here — before any deduction or
        # queueing — instead of failing later inside the worker. The
        # exact charge happens in the worker once the real part count
        # is known; it can only be lower than this estimate.
        estimated_parts = _recap_estimate_part_count(
            float(checked_duration),
            float(req.split_minutes),
            float(max_duration_allowed),
        )
        if tiered_on:
            # Batch AO: the balance must cover the BAND total over the
            # total duration — under tiered pricing the split size
            # (and thus the part estimate) no longer sets the price,
            # so the pre-check and the quote use the band total, the
            # exact number the worker will charge.
            required_credits = _recap_band_credits(float(checked_duration)) + submit_max_surcharge
            if not is_admin_user:
                quoted_total_credits = required_credits
            if not _recap_segment_balance_ok(
                is_admin=is_admin_user,
                balance=credits,
                estimated_parts=required_credits,
            ):
                raise HTTPException(
                    status_code=403,
                    detail=_tiered_credits_message(required_credits, balance=credits),
                )
        elif not _recap_segment_balance_ok(
            is_admin=is_admin_user,
            balance=credits,
            estimated_parts=estimated_parts,
        ):
            raise HTTPException(
                status_code=403,
                detail=_segmented_credits_message(estimated_parts, balance=credits),
            )
    elif tiered_on and checked_duration and not is_admin_user:
        # Batch AO: single-job path (duration within the cap). The
        # worker tops the submit credit up to the band total before
        # rendering, so the balance must cover that total BEFORE any
        # credit moves — the same number the pre-submit quote shows
        # and the worker charges. Free-lane sources (≤2 min) always
        # price as band 1, which the credit gate above already
        # required, so this check can never newly block them.
        required_credits = _recap_band_credits(float(checked_duration)) + submit_max_surcharge
        quoted_total_credits = required_credits
        if credits < required_credits:
            raise HTTPException(
                status_code=403,
                detail=_tiered_credits_message(required_credits, balance=credits),
            )

    # Concurrency: at most settings.max_concurrent_jobs_per_user (default
    # 1, same as voice clones) active recap jobs per user; admins exempt.
    # Checked before any credit is deducted, so a rejected job costs nothing.
    #
    # Batch N: for non-admins the count check, the charge and the Job
    # insert below now run under a User-row lock (SELECT … FOR UPDATE —
    # the same pattern as the voice path in queue.py) inside ONE
    # transaction that commits only after the insert. Two concurrent
    # submits (or a submit racing a retry of a failed job) used to
    # interleave check-pass / check-pass / charge / charge; the loser
    # now blocks on the row lock until the winner commits, then counts
    # the winner's committed job and is rejected BEFORE any credit
    # moves. SQLite (tests) ignores FOR UPDATE; Postgres honours it.
    submit_billing_mode = _recap_billing_mode(req.tts_engine, is_admin=is_admin_user)
    estimated_voice_seconds = _recap_estimated_voice_seconds(
        req.tts_engine,
        checked_duration,
        max_duration_allowed,
        is_admin=is_admin_user,
        segmented=(submit_decision == "segment"),
    )
    job_id = f"rc_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    remaining_voice_seconds: Optional[int] = None

    _submit_locked = not settings.is_admin(user.id)
    if _submit_locked:
        locked_user = (
            await db.execute(
                select(User)
                .where(User.id == user.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if await _recap_active_job_count(db, user.id) >= max(1, settings.max_concurrent_jobs_per_user):
            await db.rollback()
            raise HTTPException(
                status_code=429,
                detail="A recap job is already running for your account. Please wait for it to finish before starting another.",
            )

        locked_credits = int(getattr(locked_user or user, "recap_credits", 0) or 0)
        if tiered_on and checked_duration:
            needed_submit_credits = max(
                1, _recap_band_credits(float(checked_duration)) + submit_max_surcharge
            )
        elif submit_decision == "segment":
            needed_submit_credits = max(1, int(estimated_parts or 1))
        else:
            needed_submit_credits = 1
        if locked_credits < needed_submit_credits:
            await db.rollback()
            if tiered_on and checked_duration:
                raise HTTPException(
                    status_code=403,
                    detail=_tiered_credits_message(needed_submit_credits, balance=locked_credits),
                )
            if submit_decision == "segment":
                raise HTTPException(
                    status_code=403,
                    detail=_segmented_credits_message(needed_submit_credits, balance=locked_credits),
                )
            raise HTTPException(
                status_code=403,
                detail="Recap Video ဖန်တီးရန် Credit မလုံလောက်တော့ပါ။ VIP tab တွင် Recap Plan ဝယ်ယူနိုင်ပါသည်။",
            )

        if (
            submit_billing_mode == _RECAP_BILLING_CREDIT_PLUS_VOICE
            and estimated_voice_seconds > 0
        ):
            from app.services.ledger import get_minute_balance

            bal_row = (
                await db.execute(
                    select(MinuteBalance)
                    .where(MinuteBalance.user_id == user.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if bal_row is None:
                bal_row = await get_minute_balance(db, user.id)
            _avail_clone_sec = int(bal_row.clone_spendable_seconds)
            if _avail_clone_sec < estimated_voice_seconds:
                await db.rollback()
                return _recap_insufficient_voice_minutes_response(
                    required_seconds=estimated_voice_seconds,
                    available_seconds=_avail_clone_sec,
                )

    # Deduct the submit credit: unconditional for non-admins. Admins are
    # never charged. Option B voice minutes are held in this SAME locked
    # transaction via consume_seconds so credits + minutes + job row move
    # atomically or roll back together.
    deducted_credit = False
    if not settings.is_admin(user.id):
        charge_result = await db.execute(
            update(User)
            .where(User.id == user.id, User.recap_credits >= 1)
            .values(recap_credits=User.recap_credits - 1)
        )
        if not charge_result.rowcount:
            await db.rollback()
            raise HTTPException(
                status_code=403,
                detail="Recap Video ဖန်တီးရန် Credit မလုံလောက်တော့ပါ။ VIP tab တွင် Recap Plan ဝယ်ယူနိုင်ပါသည်။",
            )
        deducted_credit = True

        if (
            submit_billing_mode == _RECAP_BILLING_CREDIT_PLUS_VOICE
            and estimated_voice_seconds > 0
        ):
            from app.services.ledger import consume_seconds, get_minute_balance

            try:
                bal_after = await consume_seconds(
                    db,
                    user.id,
                    estimated_voice_seconds,
                    job_id=job_id,
                    note=f"Recap clone voice hold ({job_id})",
                )
            except ValueError:
                await db.rollback()
                bal_now = await get_minute_balance(db, user.id)
                _avail_after_sec = int(bal_now.clone_spendable_seconds)
                return _recap_insufficient_voice_minutes_response(
                    required_seconds=estimated_voice_seconds,
                    available_seconds=_avail_after_sec,
                )
            remaining_voice_seconds = int(bal_after.clone_spendable_seconds)

    clean_display_title = _clean_recap_display_title(req.title, req.url)
    RECAP_JOBS_REGISTRY[job_id] = {
        "id": job_id,
        "user_id": user.id,
        "title": clean_display_title,
        "status": "QUEUED",
        "progress_percent": 0,
        "progress_step": "0/4",
        "progress_label": t("recap_progress_preparing", user.language_code or "my"),
        "created_at": time.time(),
        "video_path": None,
        "audio_path": None,
        "srt_path": None,
        "script_path": None,
        "download_urls": {},
        "error_message": None,
        "script_review": bool(req.script_review),
        "segments_count": None,
        # Batch CB: the ETA shape (engine class + render quality) —
        # read by the shape-conditioned estimator on the status path.
        "tts_engine": req.tts_engine,
        "render_quality": req.render_quality,
        # Batch BV: own-voice truth for the status payload — set from
        # the submit-resolved reference; reference_trimmed is filled
        # in at completion from the engine's plan JSON.
        "voice_ref_used": bool(req.voice_ref_path),
        "reference_trimmed": False,
        "split_video": bool(req.split_video),
        "parts": [],
        "split_error": None,
        "split_note": None,
        # Per-part credit accounting (segmented path): credits_charged
        # starts at the submit-time single credit and is topped up to
        # the exact part count in the worker before any part renders.
        "credit_deducted": deducted_credit,
        "credits_charged": 1 if deducted_credit else 0,
        "credits_estimated": estimated_parts,
        # Batch AO: the pricing inputs — the submit-resolved duration
        # the quote was computed from, and the pinned pricing mode.
        "source_duration_seconds": float(checked_duration or 0),
        "tiered_pricing": tiered_on,
        # Batch BE: the pinned Max-quality surcharge for this job
        # (0 unless tiered pricing was armed AND quality is Max).
        "max_quality_surcharge": submit_max_surcharge,
        "billing_mode": submit_billing_mode,
        "voice_seconds_held": int(estimated_voice_seconds),
        "voice_seconds_settled": None,
        "voice_settled": False,
        "voice_refunded": False,
        "voice_seconds_refunded": 0,
        "parts_count": None,
        "stage_timings": {},
    }

    # Record Recap Job in database table for 24h history and tracking
    db_job = Job(
        id=job_id,
        user_id=user.id,
        chat_id=user.id,
        status=JobStatus.QUEUED.value,
        job_type=JobType.RECAP.value,
        engine="recap",
        preset_voice=req.voice_slug,
        input_text=clean_display_title,
        source_audio_path=req.url,
        # Batch BV: the resolved own-voice sample path (None for
        # persona jobs) — the row-level truth the retention sweep's
        # referenced-paths scan protects while this job is alive.
        recap_ref_audio_path=req.voice_ref_path,
        # R40 item 15c: the old hardcoded 120 made EVERY recap row
        # in /history display "120s" forever. Estimate from the
        # source duration verified above; the real output duration
        # replaces it in seconds_consumed on completion.
        seconds_estimated=(
            int(checked_duration)
            if checked_duration
            else (int(estimated_voice_seconds) if estimated_voice_seconds else 0)
        ),
        # Full config + credit + Option B state, so a restart/deploy
        # re-dispatch restores THIS job and its exact billing contract.
        recap_config=_persisted_recap_state(
            req,
            credit_deducted=deducted_credit,
            credits_charged=1 if deducted_credit else 0,
            is_vip=is_vip,
            phase="script" if req.script_review else "full",
            estimated_parts=estimated_parts,
            source_duration_seconds=float(checked_duration or 0),
            tiered_pricing=tiered_on,
            max_quality_surcharge=submit_max_surcharge,
            billing_mode=submit_billing_mode,
            voice_seconds_held=int(estimated_voice_seconds),
        ),
    )
    if deducted_credit:
        # Batch O (billing option C): the submit charge lands on the
        # recap-credit ledger in the SAME transaction as the charge
        # itself (both branches below commit this session), so the
        # audit row can never exist without the charge or vice versa.
        db.add(RecapCreditLedgerEvent(
            user_id=user.id,
            source="RECAP",
            direction="DEBIT",
            credits=1,
            reference_id=job_id,
            note=f"Recap job {job_id} submit charge",
        ))
    if _submit_locked:
        # Batch N locked path: insert inside the SAME transaction as
        # the row lock + charge. The savepoint keeps a failed insert
        # from discarding the charge (historical behaviour: the job
        # still runs from the registry when the history row cannot be
        # persisted); the single commit below then releases the lock
        # with charge and job row becoming visible together.
        try:
            async with db.begin_nested():
                db.add(db_job)
                await db.flush()
        except Exception as err_db:
            logger.warning(f"Could not persist recap Job record in DB: {err_db}")
        await db.commit()
        # The bulk UPDATE does not sync the in-memory instance; reload
        # so remaining_credits and the persisted job state are exact.
        await db.refresh(user)
    else:
        try:
            db.add(db_job)
            await db.commit()
        except Exception as err_db:
            logger.warning(f"Could not persist recap Job record in DB: {err_db}")

    # Start background task with VIP tier flag for watermark control and credit tracking
    try:
        asyncio.create_task(_run_recap_background_job(
            job_id, req, user.id, user.id,
            is_vip=is_vip, credit_deducted=deducted_credit,
            phase="script" if req.script_review else "full",
        ))
    except Exception:
        # The job never ran, so a deducted credit must not be lost.
        # Batch O: refund under the persisted claim (the Job row and
        # its blob committed above), so this refund is as durable as
        # every other path; "error" (row/blob unexpectedly missing)
        # falls back to the plain refund, still ledger-guarded.
        if deducted_credit:
            _claim, _recorded = await _claim_persisted_recap_refund(job_id, 1)
            if _claim == "claimed":
                if not await _refund_recap_credits(user.id, 1, job_id=job_id):
                    await _release_persisted_recap_refund_claim(job_id)
            elif _claim == "error":
                await _refund_recap_credits(user.id, 1, job_id=job_id)
        if (
            submit_billing_mode == _RECAP_BILLING_CREDIT_PLUS_VOICE
            and estimated_voice_seconds > 0
        ):
            await _refund_recap_voice_minutes(
                user.id,
                job_id,
                billing_mode=submit_billing_mode,
                entry=RECAP_JOBS_REGISTRY.get(job_id),
            )
        raise

    # Batch CB Item 3: the honest total range for THIS job's shape,
    # computed from the same resolver the Step-4 wizard consulted —
    # the client paints it as the initial estimate (and it is the
    # range the overrun flag later compares against). Null when the
    # duration is unknown: no invented number.
    _submit_eta_range = await _recap_submit_eta_range(
        req.tts_engine,
        checked_duration or req.duration_seconds,
        req.render_quality,
    )
    response = {
        "success": True,
        "recap_id": job_id,
        "status": "QUEUED",
        "story_style": req.story_style,
        "ratio": req.ratio,
        "remaining_credits": getattr(user, "recap_credits", 0),
        "estimated_parts": estimated_parts,
        "credits_charged": 1 if deducted_credit else 0,
        "billing_mode": submit_billing_mode,
        "voice_seconds_held": int(estimated_voice_seconds),
        "eta_range_seconds": _submit_eta_range,
        "message": "AI Myanmar Recap ဖန်တီးမှု စတင်လုပ်ဆောင်နေပါပြီခင်ဗျာ!",
    }
    if remaining_voice_seconds is not None:
        response["remaining_voice_seconds"] = remaining_voice_seconds
    if tiered_on:
        # Batch AO: the authoritative total this submission will be
        # charged (the band total, when the duration is known) — the
        # confirmation the Mini App shows quotes exactly this number,
        # computed by the same helper the worker charges with. Batch
        # BE: a Max-quality submission's total includes the +1
        # surcharge, exactly as quoted pre-submit. The key is omitted
        # entirely while the switch is off, so the legacy response
        # shape is byte-identical.
        response["credits_total"] = quoted_total_credits
    return response


async def _deferred_db_session() -> Optional[AsyncSession]:
    """Dependency placeholder that deliberately opens NO session.

    Endpoints whose hot path is served from in-memory state (the
    recap registry) but that fall back to the DB on a cold path used
    to declare Depends(get_db_session) next to get_current_user —
    which itself opens a session — so every request checked out TWO
    pooled connections while using at most one (audit fix
    2026-11-03; the dual checkout fed the production QueuePool
    exhaustion). Depend on this instead and open AsyncSessionLocal
    only inside the branch that actually queries. Direct callers
    (tests) may still pass a real session as ``db`` and it is used
    as-is.
    """
    return None


@api_router.get("/recap/eta-estimate")
async def get_recap_eta_estimate(
    duration_seconds: float = 0,
    tts_engine: str = "voxcpm",
    render_quality: str = "standard",
    user: User = Depends(get_current_user),
):
    """Batch CB Item 3: the Step-4 wizard's honest estimate. The
    client already knows duration / engine / quality before submit;
    this returns the server-computed total range for that shape
    (shape median when trusted samples exist, else the calibrated
    stage model), or null when there is no basis for a number.
    Unknown engine/quality values degrade to the product defaults —
    this is a display hint, never a gate."""
    quality = str(render_quality or "standard").strip().lower()
    if quality not in recap_eta.QUALITY_FACTOR:
        quality = "standard"
    eta_range = await _recap_submit_eta_range(tts_engine, duration_seconds, quality)
    return {"eta_range_seconds": eta_range}


@api_router.get("/recap/{job_id}/status")
async def get_recap_status(
    job_id: str,
    user: User = Depends(get_current_user),
    db: Optional[AsyncSession] = Depends(_deferred_db_session),
):
    """Poll recap job status, progress percentage, and deliverables."""
    # The session factory is imported HERE, unconditionally
    # (final-audit batch M): it used to be imported inside the FAILED
    # registry branch below, which made the name function-local and
    # crashed the cold path with UnboundLocalError for every job
    # missing from the registry (i.e. every job after a restart).
    # Importing at call time also keeps the factory redirectable,
    # like every other helper in this module.
    from app.db.session import AsyncSessionLocal

    entry = RECAP_JOBS_REGISTRY.get(job_id)
    if entry:
        if not _recap_registry_owner_ok(job_id, user):
            raise HTTPException(status_code=404, detail="Recap job not found")
        # Same over-50MB rule as the DB branch below: only a completed
        # job's result screen shows the send button, so only then is
        # the size worth stating.
        if entry.get("status") == "SUCCESS":
            _e_size, _e_sendable = _recap_output_size_and_sendable(
                job_id, entry.get("video_path")
            )
        else:
            _e_size, _e_sendable = None, True
        # Batch L: job-aware progress info. Queue position is counted
        # from the registry alone (no DB session on this hot path);
        # the ETA median is a 60 s-cached aggregate. FAILED is
        # terminal, so its single status read may open one session to
        # answer can_retry exactly (persisted config + live source
        # check) — every earlier poll stays session-free.
        _e_status = entry.get("status")
        if _e_status in ("QUEUED", "RUNNING"):
            _e_position = _recap_registry_queue_position(job_id, entry)
            # Batch CB: shape-conditioned, stage-aware ETA payload
            # (eta_seconds point + eta_range_seconds + eta_overrun).
            _e_eta_payload = _recap_eta_from_entry(
                entry, await _recap_median_buckets(), _e_position
            )
        else:
            _e_position = None
            _e_eta_payload = {
                "eta_seconds": None,
                "eta_range_seconds": None,
                "eta_overrun": False,
            }
        _e_can_retry = None
        if _e_status == "FAILED":
            try:
                async with AsyncSessionLocal() as _session:
                    _e_job = await _session.get(Job, job_id)
                _e_can_retry = (
                    bool(_e_job is not None and _recap_can_retry(_e_job))
                    if _e_job is not None
                    else None
                )
            except Exception:
                _e_can_retry = None
        return {
            "id": job_id,
            "status": entry.get("status", "PENDING"),
            "queue_position": _e_position,
            "eta_seconds": _e_eta_payload["eta_seconds"],
            "eta_range_seconds": _e_eta_payload["eta_range_seconds"],
            "eta_overrun": _e_eta_payload["eta_overrun"],
            "can_retry": _e_can_retry,
            "progress_percent": entry.get("progress_percent", 0),
            "progress_step": entry.get("progress_step", ""),
            "progress_label": entry.get("progress_label", ""),
            "progress_stage": entry.get("progress_stage"),
            "progress_sub": entry.get("progress_sub", ""),
            "error_message": entry.get("error_message"),
            "fallback_used": bool(entry.get("fallback_used")),
            "fallback_segments": entry.get("fallback_segments", []),
            "voice_ref_used": bool(entry.get("voice_ref_used")),
            "reference_trimmed": bool(entry.get("reference_trimmed")),
            "warnings": list(entry.get("warnings") or []),
            "output_size_bytes": _e_size,
            "telegram_sendable": _e_sendable,
            "download_urls": _with_stream_url(entry.get("download_urls", {}), job_id, user.id),
            "script_review": bool(entry.get("script_review")),
            "segments_count": entry.get("segments_count"),
            "parts": entry.get("parts", []),
            "split_error": entry.get("split_error"),
            "split_note": entry.get("split_note"),
            "segmented": bool(entry.get("segmented")),
            "parts_count": entry.get("parts_count") or (len(entry.get("parts") or []) or None),
            "credits_charged": entry.get("credits_charged"),
            "credit_refunded": bool(entry.get("credit_refunded")),
            "credits_refunded": int(entry.get("credits_refunded") or 0),
            "credits_estimated": entry.get("credits_estimated"),
            "billing_mode": entry.get("billing_mode"),
            "voice_seconds_held": int(entry.get("voice_seconds_held") or 0),
            "voice_seconds_settled": entry.get("voice_seconds_settled"),
            "voice_settled": bool(entry.get("voice_settled")),
            "voice_refunded": bool(entry.get("voice_refunded")),
            "voice_seconds_refunded": int(entry.get("voice_seconds_refunded") or 0),
            "stage_timings": entry.get("stage_timings") or {},
        }

    # Cold path only (registry miss): open the one DB session this
    # request actually needs, here, instead of holding a second
    # dependency session for the registry-hit hot path above. The
    # job's loaded column attributes stay readable after the session
    # closes — everything below only reads them.
    if db is not None:
        job = await _require_job_owner(db, job_id, user)
    else:
        async with AsyncSessionLocal() as _session:
            job = await _require_job_owner(_session, job_id, user)

    is_success = (job.status == JobStatus.SUCCESS.value)
    is_waiting_choice = (job.status == JobStatus.WAITING_CHOICE.value)
    is_active = (job.status in (JobStatus.QUEUED.value, JobStatus.RUNNING.value, JobStatus.WAITING_CHOICE.value))
    # Result warnings persisted on the Job row at completion (blur
    # safefix 2026-11-03); rows that predate persistence have none.
    _db_state = _load_persisted_recap_state(job)
    _db_warnings = list((_db_state or {}).get("warnings") or [])
    # Same single availability check as every other serializer: a SUCCESS
    # record alone does not make the media downloadable (e.g. after the
    # retention sweep removed the files).
    media_available = bool(is_success and check_job_file_exists(job_id, True, job.result_path))
    is_expired = bool(is_success and not media_available)
    _fb_used, _fb_segs = _recap_fallback_info(settings.storage_root / "recap_output" / job_id)
    # Split parts survive in the job dir even after the registry entry is
    # gone (retention window), so rebuild their URLs from disk.
    _db_parts: list = []
    if is_success:
        _parts_dir = settings.storage_root / "recap_output" / job_id / "parts"
        if _parts_dir.is_dir():
            for _pf in sorted(_parts_dir.glob("recap_part_*.mp4")):
                try:
                    _pn = int(_pf.stem.rsplit("_", 1)[1])
                except (IndexError, ValueError):
                    continue
                _db_parts.append({
                    "n": _pn,
                    "label": "",
                    "url": f"/api/recap/download/{job_id}/part{_pn}",
                })
    # Over-50MB rule (2026-11-03): see _recap_output_size_and_sendable.
    if is_success:
        _r_size, _r_sendable = _recap_output_size_and_sendable(job_id, job.result_path)
    else:
        _r_size, _r_sendable = None, True
    # Batch K: the persisted recap state is the cold-path source for
    # the receipt fields — charged credits plus the refund proof the
    # refund path wrote back into recap_config.
    _db_state = _load_persisted_recap_state(job) or {}
    # Batch L: same queue/ETA/retry fields as the registry branch,
    # computed from the Job table on this cold path.
    if job.status in (JobStatus.QUEUED.value, JobStatus.RUNNING.value):
        _d_position = await _recap_db_queue_position(job)
        # Batch CB: same ETA payload as the registry branch, derived
        # from the Job row (its persisted config carries the shape;
        # a RUNNING row's stage is inferred from elapsed share).
        _d_eta_payload = _recap_eta_from_row(
            job, await _recap_median_buckets(), _d_position
        )
    else:
        _d_position = None
        _d_eta_payload = {
            "eta_seconds": None,
            "eta_range_seconds": None,
            "eta_overrun": False,
        }
    return {
        "id": job_id,
        "status": job.status,
        "queue_position": _d_position,
        "eta_seconds": _d_eta_payload["eta_seconds"],
        "eta_range_seconds": _d_eta_payload["eta_range_seconds"],
        "eta_overrun": _d_eta_payload["eta_overrun"],
        "can_retry": (
            _recap_can_retry(job)
            if job.status == JobStatus.FAILED.value
            else None
        ),
        "fallback_used": _fb_used,
        "fallback_segments": _fb_segs,
        # Batch BV: own-voice truth on the cold path — the row's
        # resolved ref column (the persisted config carries the same
        # path) and the plan-JSON trim flag, read exactly like the
        # fallback fields above.
        "voice_ref_used": bool(
            getattr(job, "recap_ref_audio_path", None)
            or ((_db_state.get("config") or {}).get("voice_ref_path"))
        ),
        "reference_trimmed": _recap_reference_trimmed(
            settings.storage_root / "recap_output" / job_id
        ),
        "warnings": _db_warnings,
        "output_size_bytes": _r_size,
        "telegram_sendable": _r_sendable,
        "progress_percent": 100 if is_success else 0,
        "progress_step": "4/4" if is_success else ("0/4" if is_active else ""),
        "progress_label": t("webapp_status_completed", user.language_code or "my") if is_success else (t("live_job_status_waiting_choice", user.language_code or "my") if is_waiting_choice else (t("webapp_status_processing", user.language_code or "my") if is_active else (job.error_message or t("webapp_status_failed", user.language_code or "my")))),
        "error_message": job.error_message,
        "parts": _db_parts,
        "segmented": len(_db_parts) >= 2,
        "parts_count": len(_db_parts) or None,
        "credits_charged": (
            int(_db_state["credits_charged"])
            if _db_state.get("credits_charged") is not None
            else None
        ),
        "credit_refunded": bool(_db_state.get("credit_refunded")),
        "credits_refunded": int(_db_state.get("credits_refunded") or 0),
        "billing_mode": _db_state.get("billing_mode"),
        "voice_seconds_held": int(_db_state.get("voice_seconds_held") or 0),
        "voice_seconds_settled": _db_state.get("voice_seconds_settled"),
        "voice_settled": bool(_db_state.get("voice_settled")),
        "voice_refunded": bool(_db_state.get("voice_refunded")),
        "voice_seconds_refunded": int(_db_state.get("voice_seconds_refunded") or 0),
        "is_expired": is_expired,
        "stage_timings": (_db_state.get("stage_timings") or {}),
        "download_urls": _with_stream_url({
            "video": f"/api/recap/download/{job_id}/video" if media_available else None,
            "audio": f"/api/recap/download/{job_id}/audio" if media_available else None,
            "srt": f"/api/recap/download/{job_id}/srt" if media_available else None,
            "script": f"/api/recap/download/{job_id}/script" if media_available else None,
            "parts": _db_parts,
        }, job_id, user.id),
    }


@api_router.get("/recap/{job_id}/script")
async def get_recap_script(
    job_id: str,
    user: User = Depends(get_current_user),
):
    """Script-review phase: the generated Burmese narration, line by line,
    next to its source line (desktop Recapter 'Edit Text' parity)."""
    entry = RECAP_JOBS_REGISTRY.get(job_id)
    if not entry or not _recap_registry_owner_ok(job_id, user):
        raise HTTPException(status_code=404, detail="Recap job not found")
    if entry.get("status") != "SCRIPT_READY":
        raise HTTPException(status_code=409, detail="Recap script is not ready for review")

    from app.services.recap_engine.script_editing import list_script_segments

    _plan_path, plan = _load_recap_script_plan(job_id)
    try:
        segments = list_script_segments(plan)
    except ValueError:
        raise HTTPException(status_code=500, detail="Recap script has no usable segments")
    return {"job_id": job_id, "status": "SCRIPT_READY", "segments": segments}


@api_router.put("/recap/{job_id}/script")
async def update_recap_script(
    job_id: str,
    req: RecapScriptUpdateRequest,
    user: User = Depends(get_current_user),
    _gate: User = Depends(require_gate_verified),
):
    """Save user edits into the plan (+ translation checkpoint). Only the
    edited lines are re-voiced when rendering starts; validation problems
    (English words, foreign letters, notes, mixed digits) come back as
    warnings, mirroring the desktop editor."""
    entry = RECAP_JOBS_REGISTRY.get(job_id)
    if not entry or not _recap_registry_owner_ok(job_id, user):
        raise HTTPException(status_code=404, detail="Recap job not found")
    if entry.get("status") != "SCRIPT_READY":
        raise HTTPException(status_code=409, detail="Recap script is not editable now")
    if not req.segments:
        return {"success": True, "updated": 0, "warnings": {}}

    import json as _json

    from app.services.recap_engine.script_editing import apply_script_edits, atomic_write_json

    plan_path, plan = _load_recap_script_plan(job_id)
    translations = None
    translations_path = _recap_workspace_dir(job_id) / "timestamp_translations.json"
    if translations_path.is_file():
        try:
            translations = _json.loads(translations_path.read_text(encoding="utf-8"))
        except Exception:
            translations = None

    try:
        result = apply_script_edits(
            plan,
            translations,
            [{"index": item.index, "narration": item.narration} for item in req.segments],
        )
    except ValueError as exc:
        # apply_script_edits' ValueErrors are developer-facing
        # validation text; log the specifics, answer cleanly (F11).
        logger.warning(f"Script edit rejected for job {job_id}: {exc}")
        raise HTTPException(status_code=400, detail="Script ပြင်ဆင်မှု မအောင်မြင်ပါ — အကြောင်းအရာကို ပြန်စစ်ပြီး ထပ်ကြိုးစားပါ။")

    if result["updated"]:
        atomic_write_json(plan_path, plan)
        if translations is not None:
            try:
                atomic_write_json(translations_path, translations)
            except Exception as exc:
                logger.warning(f"Recap job {job_id}: plan saved, translation checkpoint not updated: {exc}")
    return {
        "success": True,
        "updated": result["updated"],
        "warnings": {str(k): v for k, v in result["warnings"].items()},
    }


@api_router.post("/recap/{job_id}/render")
async def render_recap_job(
    job_id: str,
    user: User = Depends(get_current_user),
    _gate: User = Depends(require_gate_verified),
):
    """Script-review phase 2: start rendering with the (possibly edited)
    script. No second credit is charged — the credit was taken when the
    job was created; a render failure refunds it via the job runner."""
    entry = RECAP_JOBS_REGISTRY.get(job_id)
    if not entry or not _recap_registry_owner_ok(job_id, user):
        raise HTTPException(status_code=404, detail="Recap job not found")
    if entry.get("status") != "SCRIPT_READY":
        raise HTTPException(status_code=409, detail="Recap job is not waiting for render")

    snapshot = entry.get("req_snapshot")
    source_path = entry.get("source_path")
    if not snapshot or not source_path or not Path(source_path).is_file():
        raise HTTPException(status_code=409, detail="Recap job cannot be resumed (source missing)")

    req = RecapConfigRequest(**snapshot)
    entry["status"] = "RUNNING"
    entry["eta_run_started_at"] = time.time()
    entry["progress_step"] = "3/4"
    _recap_set_entry_label(entry, "🗣️ Rendering with your reviewed script...", stage="tts")
    asyncio.create_task(_run_recap_background_job(
        job_id, req, user.id, user.id,
        is_vip=bool(entry.get("is_vip")),
        credit_deducted=bool(entry.get("credit_deducted")),
        phase="render",
        resume_source=str(source_path),
    ))
    return {"success": True, "status": "RUNNING"}


@api_router.post("/recap/{job_id}/retry")
async def retry_recap_job(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    _gate: User = Depends(require_gate_verified),
):
    """One-tap retry of a FAILED recap job (batch L).

    Creates a NEW job from the failed job's stored configuration and
    runs it through the normal submission path (create_recap_job):
    same balance gate, same atomic charge, same refund-on-failure.
    A retry is a NEW charge at the normal price — the failed job's
    charge was already refunded by the failure path, which the UI
    copy states honestly. (Since Batch AJ H6 the admin queue retry
    matches this honesty: it re-charges a refunded job from the
    ledger before re-dispatching, or refuses with 402.)

    Owner-only: _require_job_owner admits admins for reads, but a
    retry acts (and charges) as the job's owner, so ownership must be
    exact — anyone else gets the same 404 as a missing job.

    Source availability: URL sources are always retryable. Upload
    sources live in storage/temp/recap_uploads and are used in place;
    they survive while the failed job's row does (see
    _recap_source_available), but when the file is already gone the
    retry is refused with the honest re-upload explanation instead of
    charging for a job that cannot run.
    """
    job = await _require_job_owner(db, job_id, user)
    if job.user_id != user.id:
        raise HTTPException(status_code=404, detail="Recap job not found")
    if not (
        job.job_type == JobType.RECAP.value
        or job.id.startswith("rc_")
        or job.engine == "recap"
    ):
        raise HTTPException(status_code=404, detail="Recap job not found")
    if job.status != JobStatus.FAILED.value:
        raise HTTPException(
            status_code=409,
            detail="မအောင်မြင်တဲ့ Recap အလုပ်ကိုသာ ပြန်စမ်းနိုင်ပါတယ်။",
        )
    cfg = _recap_job_config(job)
    if cfg is None:
        raise HTTPException(
            status_code=409,
            detail="ဒီ အလုပ်ရဲ့ ဆက်တင်များကို ပြန်ရှာမရပါ — Recap အသစ် ပြန်ဖန်တီးပေးပါ။",
        )
    if not _recap_source_available(cfg.url):
        raise HTTPException(
            status_code=409,
            detail="မူရင်း ဗီဒီယိုဖိုင်ကို မတွေ့တော့ပါ — ဖိုင်ကို ပြန်တင်ပြီးမှ ပြန်စမ်းနိုင်ပါမယ်။",
        )
    result = await create_recap_job(cfg, user=user, db=db, _gate=user)
    # Batch AB: the retried-from job is terminal; clear any stranded
    # count it accumulated (the NEW job starts from a fresh blob).
    await _clear_recap_stranded_reset_count(job_id)
    result["retried_from"] = job_id
    return result


@api_router.post("/recap/{job_id}/cancel")
async def cancel_recap_job(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """User cancel for their OWN queued / processing recap job (batch L).

    Runs the same cancel machinery as the admin console
    (_cancel_recap_job_core): the row flips to CANCELLED, the registry
    entry gets the marker the runner's re-checks recognise (a queued
    job then never starts; a rendering job stops at the delivery
    boundary), and the exact charged credits are refunded through the
    failure paths' helper. SCRIPT_READY counts as processing: the
    job's row is RUNNING while parked, and cancelling discards the
    held script state (the persisted script_ready flag is cleared,
    like the 24 h expiry sweep does) and refunds by the same rules.

    Deliberately NOT membership-gated: stopping a job and recovering
    your own refund must never be blocked. Owner-only, with the same
    404 discipline as every other recap endpoint.
    """
    entry = RECAP_JOBS_REGISTRY.get(job_id)
    if entry is not None and entry.get("user_id") != user.id:
        raise HTTPException(status_code=404, detail="Recap job not found")
    job = await db.get(Job, job_id)
    if job is not None:
        if job.user_id != user.id:
            raise HTTPException(status_code=404, detail="Recap job not found")
        if not (
            job.job_type == JobType.RECAP.value
            or job.id.startswith("rc_")
            or job.engine == "recap"
        ):
            raise HTTPException(status_code=404, detail="Recap job not found")
    if job is None and entry is None:
        raise HTTPException(status_code=404, detail="Recap job not found")

    entry_status = str((entry or {}).get("status") or "")
    cancellable = entry_status in ("QUEUED", "RUNNING", "SCRIPT_READY", "WAITING_CHOICE") or (
        job is not None
        and job.status in (JobStatus.QUEUED.value, JobStatus.RUNNING.value, JobStatus.WAITING_CHOICE.value)
    )
    if not cancellable:
        raise HTTPException(
            status_code=409,
            detail="လုပ်ဆောင်နေဆဲ Recap အလုပ်ကိုသာ ရပ်တန့်နိုင်ပါတယ်။",
        )

    if job is not None:
        # The core also clears the persisted script_ready flag for a
        # parked job, in the same commit as the status flip (batch N)
        # — the separate post-step this endpoint used to run is gone.
        refund_info = await _cancel_recap_job_core(
            db,
            job,
            db_error_message="Cancelled by user via Mini App",
            registry_error_message="Cancelled by user",
        )
    else:
        # Registry-only job (its DB insert failed at submit): the same
        # marker + refund helpers, minus the row write.
        entry["status"] = "FAILED"
        entry["error_message"] = "Cancelled by user"
        refunded = await _refund_recap_job_charge(user.id, entry)
        refund_info = {
            "refunded": bool(refunded),
            "recap_credits": int(entry.get("credits_refunded") or 0),
            "voice_refunded": bool(entry.get("voice_refunded")),
            "voice_seconds_refunded": int(entry.get("voice_seconds_refunded") or 0),
            "stage_timings": entry.get("stage_timings") or {},
        }

    return {
        "success": True,
        "status": "CANCELLED",
        "refund": refund_info,
        "credit_refunded": bool(refund_info.get("refunded")),
        "credits_refunded": int(refund_info.get("recap_credits") or 0),
        "voice_refunded": bool(refund_info.get("voice_refunded")),
        "voice_seconds_refunded": int(refund_info.get("voice_seconds_refunded") or 0),
    }


@api_router.post("/recap/demo/send")
async def send_recap_demo(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    _gate: User = Depends(require_gate_verified),
):
    """Deliver the admin-configured recap demo sample (batch L).

    The demo asset reference lives in the ``recap_demo_asset`` dynamic
    setting (Telegram file_id or URL). A URL is handed back for the
    Mini App to open; a file_id can only be viewed via the bot, so it
    is sent to the user's own chat — the same Bot pattern the failure
    DM uses. Unset means the demo slot is hidden in the UI and this
    endpoint refuses.
    """
    from app.services.dynamic_settings import get_dynamic_setting

    asset = (
        await get_dynamic_setting(
            db,
            "recap_demo_asset",
            default=getattr(settings, "recap_demo_asset", ""),
        )
        or ""
    ).strip()
    if not asset:
        raise HTTPException(
            status_code=400,
            detail="နမူနာ ဗီဒီယို မသတ်မှတ်ရသေးပါ။",
        )
    if asset.startswith(("http://", "https://")):
        return {"success": True, "url": asset}
    if not settings.bot_token:
        raise HTTPException(
            status_code=400,
            detail="နမူနာ ပို့ရန် Bot မရရှိနိုင်ပါ။",
        )
    from aiogram import Bot

    bot = Bot(token=settings.bot_token)
    try:
        try:
            await bot.send_video(chat_id=user.id, video=asset)
        except Exception:
            await bot.send_document(chat_id=user.id, document=asset)
    except Exception as exc:
        logger.warning(f"Could not send recap demo to user {user.id}: {exc}")
        raise HTTPException(
            status_code=502,
            detail="နမူနာ ပို့တာ မအောင်မြင်ပါ — ခဏနေ ပြန်စမ်းပါ။",
        )
    finally:
        await bot.session.close()
    return {"success": True, "sent": True}


@api_router.get("/recap/stream/{job_id}/{file_type}")
@api_router.get("/recap/download/{job_id}/{file_type}")
async def download_recap_file(
    job_id: str,
    file_type: str,
    request: Request,
    user: Optional[User] = Depends(get_optional_user),
    t: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db_session),
):
    """Download or stream generated recap MP4 video, WAV/MP3 audio, SRT subtitles, or TXT script."""
    # Auth: the initData header (normal API calls, resolved by
    # get_optional_user) OR a signed stream token (R40 (ii)) minted
    # for this exact job/file/user — a <video> element cannot send
    # headers. Either way the resolved user goes through the same
    # ownership checks below; a token never creates a user and never
    # widens access beyond its own job+file scope.
    if user is None and t:
        token_uid = _verify_stream_token(t, job_id, file_type)
        if token_uid is not None:
            user = await db.get(User, token_uid)
    if user is None:
        raise HTTPException(status_code=401, detail="Telegram authentication required")
    # Batch AJ L11: a stream token is a 2 h bearer credential — a user
    # banned AFTER it was minted must not keep downloading with it.
    if getattr(user, "status", None) == "BLOCKED" and not settings.is_admin(user.id):
        raise HTTPException(status_code=404, detail="Recap file not found")
    # Batch AR: same story for maintenance — a token minted before
    # maintenance was switched on must not keep serving files to a
    # non-admin while the Mini App is closed for maintenance.
    # (initData callers were already refused inside get_optional_user.)
    _enforce_maintenance_access(user)
    # Ownership first, before any file is resolved from disk: a registry
    # entry or a DB job must belong to the requester (or the requester
    # must be an admin). Files on disk with no job record are not served.
    if not _recap_registry_owner_ok(job_id, user):
        raise HTTPException(status_code=404, detail="Recap file not found")
    job_row = None
    if job_id not in RECAP_JOBS_REGISTRY:
        job_row = await _require_job_owner(db, job_id, user)
    db_result_path = None
    if file_type == "video":
        try:
            from app.db.session import AsyncSessionLocal
            async with AsyncSessionLocal() as session:
                j = await session.get(Job, job_id)
                if j and j.result_path and Path(j.result_path).is_file():
                    db_result_path = j.result_path
                if job_row is None:
                    job_row = j
        except Exception:
            pass

    # Premium branded download name (2026-11-03): label = the job's
    # title when one is resolvable (registry entry first, then the
    # persisted recap config), else the helper falls back to the job
    # tag. URL-ish leftovers and the title cleaner's generic default
    # are not usable labels.
    _dl_title = None
    _entry_title = (RECAP_JOBS_REGISTRY.get(job_id) or {}).get("title")
    if _entry_title:
        _dl_title = _clean_recap_display_title(_entry_title)
    elif job_row is not None:
        _persisted_cfg = (_load_persisted_recap_state(job_row) or {}).get("config") or {}
        _dl_title = _clean_recap_display_title(
            _persisted_cfg.get("title"), getattr(job_row, "input_text", None)
        )
    if _dl_title and (_dl_title.startswith("http") or _dl_title == "Movie Recap Video"):
        _dl_title = None

    file_path = resolve_recap_deliverable_path(job_id, file_type, result_path=db_result_path)
    if not file_path or not Path(file_path).is_file():
        raise HTTPException(status_code=404, detail=f"{file_type.capitalize()} file not ready or not found")

    actual_ext = Path(file_path).suffix.lower()
    audio_mime = "audio/wav" if actual_ext == ".wav" else "audio/mpeg"
    audio_ext = ".wav" if actual_ext == ".wav" else ".mp3"

    mime_map = {
        "video": ("video/mp4", branded_download_filename("Recap", "mp4", title=_dl_title, job_id=job_id)),
        "audio": (audio_mime, branded_download_filename("RecapAudio", audio_ext.lstrip("."), title=_dl_title, job_id=job_id)),
        "srt": ("text/plain; charset=utf-8", branded_download_filename("Subtitles", "srt", title=_dl_title, job_id=job_id)),
        "script": ("text/plain; charset=utf-8", branded_download_filename("Script", "txt", title=_dl_title, job_id=job_id)),
    }
    _part_dl = re.fullmatch(r"part([1-9][0-9]?)", file_type or "")
    if _part_dl:
        media_type, filename = "video/mp4", branded_download_filename(
            "Recap", "mp4", title=_dl_title, job_id=job_id, part=int(_part_dl.group(1))
        )
    else:
        media_type, filename = mime_map.get(file_type, ("application/octet-stream", f"{file_type}_{job_id[:8]}"))

    # Batch CK: disposition is ROUTE-only, like the audio endpoint —
    # the stream route plays inline (players, Browser Tab) and the
    # download route saves as an attachment for EVERY file type. The
    # old file-type clause forced inline for video/audio even on the
    # download route, so a "Download Video" tap would play the file in
    # the browser instead of saving it.
    is_stream = "/stream/" in request.url.path
    disposition = "inline" if is_stream else "attachment"

    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=filename,
        content_disposition_type=disposition,
    )


@api_router.post("/recap/{job_id}/send-telegram")
async def send_recap_to_telegram(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    _gate: User = Depends(require_gate_verified),
):
    """Deliver the generated recap video directly to user's Telegram chat on demand.

    Batch AP: video only — the SRT / narration audio / script are
    never attached here; they are sent solely from the bot completion
    message's per-file buttons and stay downloadable in the Mini App.
    """
    db_job = await db.get(Job, job_id)
    if not _recap_registry_owner_ok(job_id, user):
        raise HTTPException(status_code=404, detail="Recap job not found")
    if job_id not in RECAP_JOBS_REGISTRY:
        if not db_job or (db_job.user_id != user.id and not settings.is_admin(user.id)):
            raise HTTPException(status_code=404, detail="Recap job not found")
    db_res_path = db_job.result_path if db_job else None

    video_path = resolve_recap_deliverable_path(job_id, "video", result_path=db_res_path)

    if not video_path or not Path(video_path).is_file():
        raise HTTPException(status_code=404, detail="ဗီဒီယိုဖိုင် အဆင်သင့်မဖြစ်သေးပါ သို့မဟုတ် ရှာမတွေ့ပါ။")

    entry = RECAP_JOBS_REGISTRY.get(job_id) or {}
    clean_title = _clean_recap_display_title(entry.get("title"), db_job.input_text if db_job else None)
    # R40 item 1: the caption must name the voice the user actually hears.
    # A Gemini-engine job is voiced by its Gemini voice, not by the
    # persona slug stored on the row — the engine/voice live in the
    # persisted recap_config on the same row.
    from app.services.voice_display import recap_voice_display_name

    _cfg = (_load_persisted_recap_state(db_job) or {}).get("config", {}) if db_job else {}
    voice_label = recap_voice_display_name(
        _cfg.get("tts_engine"),
        _cfg.get("gemini_voice"),
        db_job.preset_voice if db_job else None,
        # Batch BV: the persisted config's resolved ref marks an
        # own-voice job (the row column carries the same truth).
        own_voice=bool(
            _cfg.get("voice_ref_path")
            or (db_job is not None and getattr(db_job, "recap_ref_audio_path", None))
        ),
    ) or "Smart Sync AI"

    delivered, fail_reason = await _send_recap_telegram_delivery(
        chat_id=user.id,
        video_path=video_path,
        title=clean_title,
        style="Pro Dubbed",
        voice_name=voice_label,
        duration_sec=0,
        job_id=job_id,
    )
    if not delivered:
        # Batch AF: this endpoint used to answer success
        # unconditionally because the delivery helper swallowed every
        # error into a warning log — the client then showed
        # "✅ ပို့ပြီးပါပြီ" for sends that never happened (observed
        # live 2026-11-04: toast at ~23:00, nothing in the bot chat
        # 14+ minutes later). Answer honestly with the same
        # coded-detail style as file_too_large_telegram; the client
        # maps the code to localized copy.
        logger.warning(
            f"Recap Telegram delivery failed for job {job_id} "
            f"(user {user.id}): {fail_reason or 'unknown'}"
        )
        raise HTTPException(status_code=502, detail="telegram_delivery_failed")
    return {"success": True, "message": "Telegram သို့ ဗီဒီယို ပို့ပြီးပါပြီခင်ဗျာ!"}


# ── 5.05. Batch AW: standalone TTS tab ─────────────────────────────────
class TTSGenerateRequest(BaseModel):
    text: str = ""
    engine: str = "gemini"
    voice: str = ""
    speed: str = DEFAULT_SPEED


def _tts_error_status(reason: str) -> int:
    if reason in ("TTS_DAILY_LIMIT_EXCEEDED", "CONCURRENT_JOB_LIMIT_EXCEEDED"):
        return 429
    if reason == "USER_BLOCKED":
        return 403
    return 400


def _tts_error_detail(reason: str, user: User) -> str:
    """User-facing detail for a TTS submit rejection — the daily-cap
    message NAMES the caller's own tier limit, mirroring the Batch AN
    voice-cap message."""
    from app.services.tts_service import tts_character_limit, tts_daily_limit_minutes

    if reason == "TTS_DAILY_LIMIT_EXCEEDED":
        limit = tts_daily_limit_minutes(user, is_admin=False)
        return (
            f"🎧 ဒီနေ့အတွက် TTS အသံ ဖန်တီးခွင့် {limit} မိနစ် ပြည့်သွားပါပြီ။ "
            "မနက်ဖြန် ထပ်မံ ကြိုးစားကြည့်ပါခင်ဗျာ။"
        )
    if reason == "TEXT_EXCEEDS_CHARACTER_LIMIT":
        # Batch BW: name the CALLER's own tier limit, exactly like the
        # daily-cap message above does for minutes (before BW this
        # named one flat 5,000 for everyone). FREE users also get the
        # upsell naming the purchased limit, mirroring the voice
        # submit paths' message.
        is_admin = settings.is_admin(user.id)
        limit = tts_character_limit(user, is_admin=is_admin)
        detail = f"စာသား ရှည်လွန်းနေပါတယ်။ စာလုံးရေ {limit:,} လုံးအောက် ထားပေးပါ။"
        if not is_admin and user.account_category != AccountCategory.PURCHASED.value:
            detail += (
                f" Free အသုံးပြုသူတွေအတွက် တစ်ကြိမ်လျှင် စာလုံးရေ {settings.character_limit_free:,} အထိသာ ကန့်သတ်ထားပါတယ်။"
                f" စာလုံးရေ {settings.character_limit_purchased:,} အထိ သုံးလိုပါက VIP Plan ဝယ်ယူနိုင်ပါတယ်။"
            )
        return detail
    if reason == "EMPTY_TEXT":
        return "အသံပြောင်းဖို့ စာသား အရင် ရိုက်ထည့်ပေးပါ။"
    if reason == "CONCURRENT_JOB_LIMIT_EXCEEDED":
        return "လုပ်ဆောင်နေဆဲ အလုပ်များ ရှိနေပါသည်။ ပြီးဆုံးမှ ထပ်မံ ကြိုးစားပါခင်ဗျာ။"
    if reason in ("INVALID_ENGINE", "INVALID_VOICE", "INVALID_SPEED"):
        return "ရွေးချယ်ထားသော TTS ဆက်တင် မမှန်ကန်ပါ။ ပြန်လည် ရွေးချယ်ပေးပါခင်ဗျာ။"
    if reason == "USER_BLOCKED":
        return "ဤအကောင့်ကို ပိတ်ထားပါသည်။"
    return "TTS အလုပ် စတင်၍ မရပါ။ ခဏအကြာ ထပ်မံ ကြိုးစားကြည့်ပါခင်ဗျာ။"


@api_router.post("/tts/generate")
async def generate_tts(
    payload: TTSGenerateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    _gate: User = Depends(require_gate_verified),
):
    """Batch AW: submit a standalone TTS job (Gemini or Edge engine).

    Billing lives in app.services.tts_service.create_tts_job: referral
    seconds are drawn first, only the remainder counts against the
    tier's daily TTS allowance, and purchased voice minutes / recap
    credits are NEVER touched. The job runs in-process (background
    task, the recap-runner pattern) and is polled through the generic
    /jobs/{id}/status endpoint like every other job type.
    """
    from app.services.tts_service import create_tts_job, execute_tts_job

    engine = str(payload.engine or "gemini").strip().lower()
    voice = str(payload.voice or "").strip()
    if not voice:
        voice = "Charon" if engine == "gemini" else "thiha"
    job, reason = await create_tts_job(
        db,
        user.id,
        text=payload.text or "",
        engine=engine,
        voice=voice,
        speed_rate=str(payload.speed or DEFAULT_SPEED),
    )
    if job is None:
        await db.rollback()
        raise HTTPException(
            status_code=_tts_error_status(reason),
            detail=_tts_error_detail(reason, user),
        )
    await db.commit()
    asyncio.create_task(execute_tts_job(job.id))
    return {
        "status": "success",
        "job_id": job.id,
        "job_type": "TTS",
        "engine": job.engine,
        "estimated_seconds": job.seconds_estimated,
    }


@api_router.get("/tts/quota")
async def get_tts_quota(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    _gate: User = Depends(require_gate_verified),
):
    """Batch AW: the TTS tab's balance picture — referral seconds (the
    tab's spendable balance), the tier's daily allowance, and how much
    of today's allowance is already spoken for (net of referral draws).
    """
    from app.services.ledger import get_minute_balance
    from app.services.tts_service import (
        tts_cap_consumed_seconds,
        tts_daily_limit_minutes,
    )

    bal = await get_minute_balance(db, user.id)
    is_admin = settings.is_admin(user.id)
    limit_min = tts_daily_limit_minutes(user, is_admin=is_admin)
    consumed = await tts_cap_consumed_seconds(db, user.id)
    remaining = None
    if limit_min > 0:
        remaining = max(0.0, limit_min * 60 - consumed)
    # Batch CG: the Gemini route picture the TTS tab paints — own-key
    # presence (masked view only) + the FREE taste remainder. Taste
    # fields are None for PURCHASED/admins (no taste governs them).
    from app.services.gemini_key_service import (
        masked_key_view,
        resolve_gemini_route,
    )

    _key_view = masked_key_view(user)
    _route = await resolve_gemini_route(db, user, is_admin=is_admin)
    return {
        "referral_seconds": int(bal.referral_seconds or 0),
        "referral_minutes": round(int(bal.referral_seconds or 0) / 60, 1),
        "daily_limit_minutes": limit_min,
        "unlimited": bool(is_admin or limit_min <= 0),
        "consumed_seconds": round(consumed, 1),
        "remaining_seconds": (round(remaining, 1) if remaining is not None else None),
        "tier": "PURCHASED" if user.account_category == AccountCategory.PURCHASED.value else "FREE",
        "is_admin": is_admin,
        "has_own_key": _key_view["has_key"],
        "own_key_masked": _key_view["masked"],
        "own_key_state": _key_view["state"],
        "gemini_taste_limit_seconds": (
            int(_route.taste_limit_seconds) if _route.taste_remaining_seconds is not None else None
        ),
        "gemini_taste_remaining_seconds": (
            int(round(_route.taste_remaining_seconds))
            if _route.taste_remaining_seconds is not None
            else None
        ),
    }


class GeminiKeyPayload(BaseModel):
    key: str


@api_router.get("/tts/gemini-key")
async def get_tts_gemini_key(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    _gate: User = Depends(require_gate_verified),
):
    """Batch CG: the caller's saved-key state — presence + masked
    view only. The key value NEVER leaves the server."""
    from app.services.gemini_key_service import masked_key_view

    return masked_key_view(user)


@api_router.post("/tts/gemini-key")
async def save_tts_gemini_key(
    payload: GeminiKeyPayload,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    _gate: User = Depends(require_gate_verified),
):
    """Batch CG: validate-then-store a user's own Gemini key. The
    validation makes exactly ONE minimal real TTS call through the
    engine's own client path; an invalid (or unverifiable) key is
    never stored. The stored value is never echoed back — the
    response carries the masked view only."""
    from app.services.gemini_key_service import (
        masked_key_view,
        save_user_key,
        validate_gemini_key,
    )

    candidate = (payload.key or "").strip()
    lang = (getattr(user, "language", None) or "my").lower()
    if not candidate:
        raise HTTPException(
            status_code=400, detail=t("gemini_key_invalid_error", lang)
        )
    accepted, reason = await asyncio.to_thread(validate_gemini_key, candidate)
    if not accepted:
        raise HTTPException(
            status_code=400,
            detail=t(
                "gemini_key_unverifiable_error"
                if reason == "unverifiable"
                else "gemini_key_invalid_error",
                lang,
            ),
        )
    await save_user_key(db, user, candidate)
    await db.commit()
    await db.refresh(user)
    return masked_key_view(user)


@api_router.delete("/tts/gemini-key")
async def delete_tts_gemini_key(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    _gate: User = Depends(require_gate_verified),
):
    """Batch CG: one-tap delete — the key material is cleared from
    the row immediately."""
    from app.services.gemini_key_service import delete_user_key, masked_key_view

    await delete_user_key(db, user)
    await db.commit()
    await db.refresh(user)
    return masked_key_view(user)


@api_router.post("/voices/{job_id}/send-telegram")
async def send_voice_to_telegram(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    _gate: User = Depends(require_gate_verified),
):
    """Deliver synthesized voice audio directly to user's Telegram chat on demand."""
    db_job = await _require_job_owner(db, job_id, user)
    audio_path = None
    if db_job.result_path and Path(db_job.result_path).is_file():
        audio_path = Path(db_job.result_path)
    else:
        outputs_dir = settings.storage_root / "outputs"
        matches = list(outputs_dir.glob(f"{job_id}*"))
        if matches and matches[0].is_file():
            audio_path = matches[0]
        else:
            cand = settings.storage_root / "audio_out" / f"{job_id}.wav"
            if cand.is_file():
                audio_path = cand

    if not audio_path or not audio_path.is_file():
        raise HTTPException(status_code=404, detail="အသံဖိုင် အဆင်သင့်မဖြစ်သေးပါ သို့မဟုတ် ရှာမတွေ့ပါ။")

    # Telegram Bot API send cap (hunt #3 B1): voice WAVs run ~5.8 MB/min,
    # so a ≳9-minute result exceeds 50 MB and the send would fail inside
    # Telegram. Same coded 413 the downloader send endpoint raises; the
    # client turns it into the "download it instead" message and the job
    # itself stays SUCCESS with the file downloadable in the Mini App.
    if audio_path.stat().st_size > _DL_TELEGRAM_LIMIT:
        raise HTTPException(status_code=413, detail="file_too_large_telegram")

    from aiogram import Bot
    from aiogram.types import FSInputFile
    bot = Bot(token=settings.bot_token)
    try:
        # Batch AW: a TTS job's on-demand send carries the TTS brand
        # and caption; voice-clone jobs keep theirs.
        _is_tts_job = db_job.job_type == JobType.TTS.value
        af = FSInputFile(
            str(audio_path),
            filename=branded_download_filename(
                "TTS" if _is_tts_job else "Voice", audio_path.suffix.lstrip(".") or "wav",
                title=db_job.preset_voice, job_id=job_id,
            ),
        )
        caption = (
            f"🎧 <b>TTS အသံ</b>\n"
            f"✨ <i>Mini App TTS Tab ကနေ ဖန်တီးထားတဲ့ အသံဖိုင်ပါ။</i>"
        ) if _is_tts_job else (
            f"🎙️ <b>AI Myanmar Voice Clone</b>\n"
            f"✨ <i>Mini App ကနေ ဖန်တီးထားတဲ့ အသံဖိုင်ပါ။</i>"
        )
        await bot.send_audio(chat_id=user.id, audio=af, caption=caption, parse_mode="HTML")
        return {"success": True, "message": "Telegram ဆီ အသံဖိုင် ပို့ပြီးပါပြီ!"}
    except Exception as e:
        logger.warning(f"Failed to send voice to Telegram chat {user.id}: {e}")
        raise HTTPException(status_code=500, detail="Telegram ဆီ ပေးပို့၍ မရပါ — ခဏနေ ပြန်စမ်းကြည့်ပါ။")
    finally:
        await bot.session.close()


# ── 5.1. Job History & Media Lifecycle Management ──────────────────────────
def _media_retention_days() -> int:
    """Generated-media retention window in days (single source of truth).

    Outputs are stored on the persistent disk and kept for this many days;
    the sweep, the history window and the user-facing copy all derive from
    this one value so they can never disagree. Batch X (2026-11-04): the
    window is 1 day — 24 hours — by the owner's direct order.
    """
    try:
        days = int(getattr(settings, "media_retention_days", 1) or 1)
    except Exception:
        days = 1
    return max(1, days)


def _job_deliverables(job, is_recap: bool, file_exists: bool):
    """The ONE deliverables builder for every job serializer.

    Returns (download_url, deliverables). Media URLs are handed out only
    when the job succeeded AND its output file is actually present, so the
    history list, the admin history, the admin queue and the media modal
    can never disagree about whether a job's downloads are live.
    """
    if getattr(job, "status", None) != JobStatus.SUCCESS.value or not file_exists:
        return None, {}
    if is_recap:
        download_url = f"/api/recap/stream/{job.id}/video"
        return download_url, {
            "video": f"/api/recap/stream/{job.id}/video",
            "audio": f"/api/recap/stream/{job.id}/audio",
            "srt": f"/api/recap/download/{job.id}/srt",
            "script": f"/api/recap/download/{job.id}/script",
        }
    download_url = f"/api/audio/stream/{job.id}"
    return download_url, {
        "audio": f"/api/audio/stream/{job.id}",
        "download": f"/api/audio/download/{job.id}",
    }


_DIR_LISTING_CACHE: Dict[str, tuple] = {}
_DIR_LISTING_TTL_SECONDS = 10.0


def _cached_dir_entries(dir_path: Path) -> List[tuple]:
    """(name, is_file) entries of a directory, cached for a few seconds.

    check_job_file_exists falls back to scanning storage directories
    for legacy rows whose result_path is missing or stale. The admin
    history/queue serializers call it once per row (up to 300 rows),
    the admin console auto-refreshes every few seconds, and every one
    of those scans used to re-glob the same directories synchronously
    on the event loop — an unfiltered admin list full of expired
    SUCCESS rows (the exact rows that miss the result_path fast path)
    meant hundreds of full directory scans per request (hunt #7).
    The cache collapses that to one scan per directory per TTL
    window; the match semantics are identical to the globs replaced.
    """
    key = str(dir_path)
    now = time.monotonic()
    hit = _DIR_LISTING_CACHE.get(key)
    if hit and now - hit[0] < _DIR_LISTING_TTL_SECONDS:
        return hit[1]
    entries: List[tuple] = []
    try:
        with os.scandir(dir_path) as it:
            for entry in it:
                try:
                    entries.append((entry.name, entry.is_file()))
                except OSError:
                    entries.append((entry.name, False))
    except OSError:
        entries = []
    _DIR_LISTING_CACHE[key] = (now, entries)
    return entries


def check_job_file_exists(job_id: str, is_recap: bool, result_path: Optional[str] = None) -> bool:
    """Safely check if the completed job's output media file exists on container disk."""
    try:
        if is_recap:
            vid = resolve_recap_deliverable_path(job_id, "video", result_path=result_path)
            return bool(vid and Path(vid).is_file())
        else:
            if result_path and Path(result_path).is_file():
                return True
            outputs_dir = settings.storage_root / "outputs"
            if outputs_dir.is_dir():
                if any(name.startswith(job_id) and is_file
                       for name, is_file in _cached_dir_entries(outputs_dir)):
                    return True
            for cand_dir in [settings.storage_root / "outputs", settings.storage_root / "temp", settings.storage_root / "voices", settings.storage_root / "recap_output"]:
                if cand_dir.is_dir():
                    if any(job_id in name and is_file
                           for name, is_file in _cached_dir_entries(cand_dir)):
                        return True
            return False
    except Exception:
        return False


def _job_display_names(job, *, is_recap: bool, is_tts: bool):
    """(voice_display, engine_display) for the job serializers (Batch BX).

    Additive display fields — the raw ``preset_voice`` stays untouched:
    * TTS rows get the friendly voice + engine names (the raw values
      are engine ids users should never see: "Charon",
      "my-MM-ThihaNeural").
    * Own-voice recap rows (Batch BV: ``recap_ref_audio_path`` set)
      get the own-voice label in BOTH the admin queue and user
      History — before BX these rows fell back to the job's default
      persona slug ("komin") in the admin list.
    Everything else returns (None, None) and the client keeps its
    existing rendering.
    """
    if is_tts:
        from app.services.voice_display import (
            tts_engine_display_name,
            tts_voice_display_name,
        )

        engine = getattr(job, "tts_engine", None) or job.engine
        return (
            tts_voice_display_name(engine, job.preset_voice) or None,
            tts_engine_display_name(engine) or None,
        )
    if is_recap and getattr(job, "recap_ref_audio_path", None):
        from app.services.voice_display import own_voice_display_name

        return own_voice_display_name(), None
    return None, None


def _time_ago_str(mins_ago: int, lang: str) -> str:
    """Localized relative-time label for history / queue payloads.

    Batch F (i18n completion): this was a hardcoded English f-string
    ("5m ago" / "2h ago") duplicated at three call sites. It now goes
    through the bot-side locales like every other user-visible string.
    ``lang`` is the viewer's stored ``language_code``.
    """
    if mins_ago < 60:
        return t("time_ago_minutes", lang, count=mins_ago)
    return t("time_ago_hours", lang, count=mins_ago // 60)


@api_router.get("/history")
async def get_user_history(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """
    Get user's job generation history (both Voice Clone and Recap) within
    the media retention window (default 24 hours). Older jobs are purged by
    the retention sweep together with their files.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=_media_retention_days())
    stmt = (
        select(Job)
        .where(Job.user_id == user.id, Job.created_at >= cutoff)
        .order_by(Job.created_at.desc())
        .limit(60)
    )
    res = await db.execute(stmt)
    jobs = res.scalars().all()

    out = []
    for j in jobs:
        created_ts = j.created_at.timestamp() if j.created_at else time.time()
        mins_ago = int((time.time() - created_ts) // 60)
        time_ago = _time_ago_str(mins_ago, user.language_code or "my")

        is_recap = (j.job_type == JobType.RECAP.value or j.id.startswith("rc_"))
        # Batch AW: TTS jobs serialize as their own type (they ride the
        # voice-side download/deliverables plumbing, but the client
        # labels them as TTS creations, not voice clones).
        is_tts = (j.job_type == JobType.TTS.value)

        # Verify whether the output file actually exists on the persistent
        # disk — the single availability check every serializer shares.
        file_exists = False
        if j.status == JobStatus.SUCCESS.value:
            file_exists = check_job_file_exists(j.id, is_recap, j.result_path)
        is_expired = bool(j.status == JobStatus.SUCCESS.value and not file_exists)

        # Over-50MB rule (2026-11-03): history items carry the same
        # telegram_sendable flag as the status payloads, so an oversize
        # output's history entry offers Download only (no tg-btn).
        # Unknown size (incl. non-SUCCESS rows) keeps the button.
        if j.status == JobStatus.SUCCESS.value and file_exists:
            if is_recap:
                _h_size, _h_sendable = _recap_output_size_and_sendable(j.id, j.result_path)
            else:
                _h_size, _h_sendable = _voice_output_size_and_sendable(j)
        else:
            _h_size, _h_sendable = None, True

        download_url, deliverables = _job_deliverables(j, is_recap, file_exists)
        if is_recap and download_url:
            # A live registry entry carries the job's own download URLs
            # (e.g. segmented parts); prefer them when present.
            recap_entry = RECAP_JOBS_REGISTRY.get(j.id)
            if recap_entry and recap_entry.get("download_urls"):
                deliverables = recap_entry["download_urls"]
                download_url = deliverables.get("video") or download_url
        if is_recap and deliverables:
            # Sign the recap URLs for their owner: a history download
            # can end up opened in the device browser, where no
            # initData header exists, and the scoped token is what
            # authenticates those opens (same fix as the recap status
            # payloads). Voice/TTS URLs are signed in the Batch BJ
            # block right below.
            deliverables = _with_stream_url(deliverables, j.id, user.id)
            download_url = deliverables.get("video") or download_url
        if not is_recap and download_url:
            # Batch BJ: voice/TTS history URLs are signed too — the
            # audio endpoint now verifies stream tokens (scope
            # (job, "audio", user)), so a History download tapped in
            # the Mini App can be handed to the device browser via
            # Telegram openLink exactly like a recap one. The row URL
            # (the /stream/ form) and both deliverables entries
            # (stream + download forms) carry the same scope's token;
            # both URL forms land on the one audio route.
            _audio_token = _make_stream_token(j.id, user.id, "audio")
            download_url = _append_stream_token(download_url, _audio_token)
            if deliverables:
                deliverables = dict(deliverables)
                for _dk in ("audio", "download"):
                    if deliverables.get(_dk):
                        deliverables[_dk] = _append_stream_token(
                            deliverables[_dk], _audio_token
                        )

        raw_snippet = _clean_recap_display_title(j.input_text) if is_recap else (j.input_text or "")
        _hist_entry = RECAP_JOBS_REGISTRY.get(j.id) if is_recap else None
        _hist_parts = ((_hist_entry or {}).get("parts") or []) if is_recap else []
        _ref_trimmed = False  # Batch BU: set for voice jobs in the else-branch below
        if is_recap:
            # Recap fallback truth lives in the engine's plan JSON on
            # disk — the same source the recap status endpoint reads.
            # The Job row's engine column only records the VOICE
            # fallback, so deriving recap fallback_used from it claimed
            # False for jobs whose completion toast had disclosed Edge
            # segments (round-3 U4). Only completed jobs have a final
            # plan; anything else honestly reports no fallback yet.
            if j.status == JobStatus.SUCCESS.value:
                _fb_used, _fb_segs = _recap_fallback_info(
                    settings.storage_root / "recap_output" / j.id
                )
                # Batch BV: for recap jobs the BU trim truth lives
                # in the plan JSON (the row's reference_trimmed
                # column is a voice-job flag).
                _ref_trimmed = _recap_reference_trimmed(
                    settings.storage_root / "recap_output" / j.id
                )
            else:
                _fb_used, _fb_segs = False, []
        else:
            _fb_used, _fb_segs = j.engine == "edge_fallback", []
            # Batch BU: the persisted reference auto-trim flag (voice
            # jobs only; recap entries never carry one).
            _ref_trimmed = bool(getattr(j, "reference_trimmed", False))
        # R40 item 13: surface the persisted recap settings so the
        # history detail can show WHAT the job was made with (engine,
        # Gemini voice, subtitle position/style) — previously a user
        # (or support) could not tell why a render looked the way it
        # did (see the 2026-11-02 complaint diagnosis).
        _recap_settings = None
        # Batch K: the persisted recap state is also the cold-path
        # source for the receipt fields below (charged credits +
        # refund state); the live registry entry wins while hot.
        _hist_state = _load_persisted_recap_state(j) if is_recap else None
        if is_recap:
            try:
                _cfg = (_hist_state or {}).get("config") or {}
                if _cfg:
                    _recap_settings = {
                        "engine": _cfg.get("tts_engine"),
                        "gemini_voice": _cfg.get("gemini_voice"),
                        "voice": _cfg.get("voice_slug"),
                        "story_style": _cfg.get("story_style"),
                        "sub_y": _cfg.get("sub_y"),
                        "sub_bottom_margin": _cfg.get("sub_bottom_margin"),
                        "blur_y": _cfg.get("blur_y"),
                        "sub_style": _cfg.get("sub_style"),
                        "sub_font": _cfg.get("sub_font"),
                    }
            except Exception:
                _recap_settings = None
        # Batch L: the full rebuildable config rides along so the
        # detail view can offer "re-run with these settings" (prefill,
        # never auto-submit) and an honest retry affordance: a failed
        # job is retryable only while its source still exists (an
        # upload whose file is gone must say so, not silently fail).
        _hist_cfg = None
        _hist_source_available = None
        _hist_can_retry = None
        if is_recap:
            _cfg_obj = _recap_job_config(j)
            if _cfg_obj is not None:
                _hist_cfg = _cfg_obj.model_dump()
                _hist_source_available = _recap_source_available(_cfg_obj.url)
            _hist_can_retry = bool(
                j.status == JobStatus.FAILED.value
                and _hist_cfg is not None
                and _hist_source_available
            )
        _real_dur = int(getattr(j, "seconds_consumed", 0) or 0)
        _est_dur = int(getattr(j, "seconds_estimated", 0) or 0)
        if _est_dur <= 0 and is_recap:
            try:
                _est_dur = int(
                    round(
                        float(
                            (_hist_entry or {}).get("probed_duration_seconds")
                            or (_hist_state or {}).get("probed_duration_seconds")
                            or (_hist_entry or {}).get("source_duration_seconds")
                            or (_hist_state or {}).get("source_duration_seconds")
                            or 0
                        )
                    )
                )
            except Exception:
                _est_dur = 0
        _disp_dur = _real_dur if _real_dur > 0 else _est_dur

        out.append({
            "id": j.id,
            "job_type": "RECAP" if is_recap else ("TTS" if is_tts else "VOICE_CLONE"),
            "seconds_consumed": _real_dur,
            "seconds_estimated": _est_dur,
            "duration_seconds": _disp_dur,
            "parts_count": (len(_hist_parts) or None) if is_recap else None,
            "credits_charged": (
                (_hist_entry or {}).get("credits_charged")
                if (_hist_entry or {}).get("credits_charged") is not None
                else (_hist_state or {}).get("credits_charged")
            ) if is_recap else None,
            "credit_refunded": bool(
                (_hist_entry or {}).get("credit_refunded")
                or (_hist_state or {}).get("credit_refunded")
            ) if is_recap else False,
            "credits_refunded": int(
                (_hist_entry or {}).get("credits_refunded")
                or (_hist_state or {}).get("credits_refunded")
                or 0
            ) if is_recap else 0,
            "engine": j.engine,
            "recap_settings": _recap_settings,
            "recap_config": _hist_cfg,
            "source_available": _hist_source_available,
            "can_retry": _hist_can_retry,
            # Batch N: a recap job PARKED at script review (row still
            # RUNNING; the park flag lives on the live registry entry
            # or, after a restart, in the persisted recap state) is
            # surfaced explicitly so the history detail can offer the
            # review action instead of a bare "running" spinner that
            # waits forever for a render that will never start.
            "script_ready": bool(
                is_recap
                and j.status == JobStatus.RUNNING.value
                and (
                    ((_hist_entry or {}).get("status") == "SCRIPT_READY")
                    or bool((_hist_state or {}).get("script_ready"))
                )
            ),
            "script_ready_at": (
                (_hist_entry or {}).get("script_ready_at")
                or (_hist_state or {}).get("script_ready_at")
            ) if is_recap else None,
            "fallback_used": _fb_used,
            "fallback_segments": _fb_segs,
            "reference_trimmed": _ref_trimmed,
            # Batch BV: the job narrated from the user's own uploaded
            # sample (row column; the live registry entry covers a
            # job whose row write lagged).
            "voice_ref_used": bool(
                is_recap
                and (
                    getattr(j, "recap_ref_audio_path", None)
                    or (_hist_entry or {}).get("voice_ref_used")
                )
            ),
            "preset_voice": j.preset_voice or ("Thiha" if is_recap else "Standard"),
            # Batch BX: friendly display names beside the raw value
            # (TTS engine/voice; own-voice recap label).
            "voice_display": _job_display_names(j, is_recap=is_recap, is_tts=is_tts)[0],
            "engine_display": _job_display_names(j, is_recap=is_recap, is_tts=is_tts)[1],
            "status": j.status,
            "input_snippet": (raw_snippet[:70] + "...") if raw_snippet and len(raw_snippet) > 70 else raw_snippet,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "time_ago": time_ago,
            "download_url": download_url,
            "deliverables": deliverables,
            "output_size_bytes": _h_size,
            "telegram_sendable": _h_sendable,
            "error_message": j.error_message,
            "file_exists": file_exists,
            "is_expired": is_expired,
        })

    return {
        "success": True,
        "count": len(out),
        "history": out,
        "ttl_hours": _media_retention_days() * 24,
        "retention_days": _media_retention_days(),
    }


@api_router.delete("/history/{job_id}")
async def delete_history_item(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """Delete one of the user's OWN history items (batch L).

    Safe variant, mirroring the retention sweep's semantics and
    ordering: only TERMINAL jobs can be deleted (an active job's
    record must never vanish from under its runner); the DB record is
    deleted and committed FIRST, then the job's own files are removed
    best-effort — the recap output directory and the result file,
    which are exclusively this job's. The source upload is NOT
    deleted: it can be shared with other jobs (the same upload
    submitted twice), and the retention sweep's orphan pass already
    reclaims uploads no surviving job row references. Owner-only,
    with the usual 404 discipline.
    """
    job = await db.get(Job, job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status_code=404, detail="History item not found")
    if job.status not in (
        JobStatus.SUCCESS.value,
        JobStatus.FAILED.value,
        JobStatus.CANCELLED.value,
    ):
        raise HTTPException(
            status_code=409,
            detail="လုပ်ဆောင်နေဆဲ အလုပ်ကို ဖျက်လို့ မရပါ — အရင် ရပ်တန့်ပြီးမှ ဖျက်ပါ။",
        )

    is_recap = (
        job.job_type == JobType.RECAP.value
        or job.id.startswith("rc_")
        or job.engine == "recap"
    )
    targets: list[Path] = []
    if is_recap:
        targets.append(settings.storage_root / "recap_output" / job.id)
    if job.result_path:
        result_file = Path(job.result_path)
        try:
            result_file.resolve().relative_to(settings.storage_root.resolve())
            targets.append(result_file)
        except Exception:
            # A result path outside the storage tree is never deleted
            # from a user request, however the row came to hold it.
            logger.warning(
                f"History delete for job {job.id}: result_path outside storage root; file kept"
            )

    await db.delete(job)
    await db.commit()
    RECAP_JOBS_REGISTRY.pop(job_id, None)

    for target in targets:
        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.is_file():
                target.unlink(missing_ok=True)
        except Exception as file_err:
            logger.warning(
                f"History delete for job {job_id}: could not remove {target}: {file_err}"
            )
    return {"success": True}


@api_router.get("/admin/history")
async def get_admin_global_history(
    timeframe: str = "24h",
    status: str = "all",
    job_type: str = "all",
    limit: int = 150,
    user_id: Optional[int] = None,
    job_id: Optional[str] = None,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """
    Admin Only: Filter and view all users' generated jobs with custom timeframe & status filters.

    Optional narrowing (2026-10-31 admin rebuild): ``user_id`` restricts to a
    single user's jobs (per-user job history) and ``job_id`` matches a job ID
    prefix (the UI displays short 8-char IDs, so admins can search by those).
    Both are purely additive filters — existing callers are unaffected.
    """
    # require_admin (route dependency) is the gate; this in-body check
    # is kept as defence in depth for direct (non-HTTP) callers, which
    # the unit tests exercise — it evaluates the same settings.is_admin.
    if not settings.is_admin(user.id):
        raise HTTPException(status_code=403, detail="Admin access required")

    now_utc = datetime.now(timezone.utc)
    stmt = (
        select(Job, User.username, User.first_name)
        .outerjoin(User, Job.user_id == User.id)
    )

    if user_id is not None:
        stmt = stmt.where(Job.user_id == user_id)

    if job_id and job_id.strip():
        stmt = stmt.where(Job.id.like(f"{job_id.strip()}%"))

    if timeframe == "24h":
        stmt = stmt.where(Job.created_at >= now_utc - timedelta(hours=24))
    elif timeframe == "7d":
        stmt = stmt.where(Job.created_at >= now_utc - timedelta(days=7))
    elif timeframe == "older_24h":
        stmt = stmt.where(Job.created_at < now_utc - timedelta(hours=24))

    if status and status != "all":
        stmt = stmt.where(Job.status == status.upper())

    if job_type == "RECAP":
        stmt = stmt.where((Job.job_type == JobType.RECAP.value) | Job.id.startswith("rc_") | (Job.engine == "recap"))
    elif job_type == "VOICE_CLONE":
        stmt = stmt.where((Job.job_type != JobType.RECAP.value) & (~Job.id.startswith("rc_")) & (Job.engine != "recap"))

    stmt = stmt.order_by(Job.created_at.desc()).limit(min(300, max(10, limit)))
    res = await db.execute(stmt)
    rows = res.all()

    total_recap = 0
    total_voice = 0
    total_failed = 0
    total_success = 0
    out = []

    for job, username, first_name in rows:
        is_recap = (job.job_type == JobType.RECAP.value or job.id.startswith("rc_") or job.engine == "recap")
        if is_recap:
            total_recap += 1
        else:
            total_voice += 1

        if job.status == JobStatus.FAILED.value:
            total_failed += 1
        elif job.status == JobStatus.SUCCESS.value:
            total_success += 1

        created_ts = job.created_at.timestamp() if job.created_at else time.time()
        mins_ago = int((time.time() - created_ts) // 60)
        time_ago = _time_ago_str(mins_ago, user.language_code or "my")

        # Verify whether the output file actually exists on the persistent
        # disk — the single availability check every serializer shares.
        file_exists = False
        if job.status == JobStatus.SUCCESS.value:
            file_exists = check_job_file_exists(job.id, is_recap, job.result_path)
        is_expired = bool(job.status == JobStatus.SUCCESS.value and not file_exists)

        download_url, deliverables = _job_deliverables(job, is_recap, file_exists)
        if is_recap and deliverables:
            # Batch CJ: sign the recap URLs for the ADMIN, exactly as
            # the user history serializer signs them for their owner.
            # The admin serializers used to hand out the raw URLs, so
            # downloadAuthFile could not escape to the device browser
            # via Telegram openLink (that path needs the self-
            # authenticating ?t= token) and phone downloads died in
            # the WebView blob path. The token's uid is loaded as the
            # user at the endpoint and the ownership checks accept
            # owner OR admin, so an admin-scoped token validates
            # without widening access.
            deliverables = _with_stream_url(deliverables, job.id, user.id)
            download_url = deliverables.get("video") or download_url
            # Batch CK: the modal's Download buttons must SAVE, so the
            # video/audio deliverables move to the download route;
            # download_url above keeps the signed stream form for the
            # player and the Browser Tab button.
            deliverables = _recap_deliverables_download_form(deliverables)
        if not is_recap and download_url:
            # Batch CJ: voice/TTS admin URLs carry the same Batch BJ
            # audio-scope token, minted for the admin; both URL forms
            # land on the one audio route.
            _audio_token = _make_stream_token(job.id, user.id, "audio")
            download_url = _append_stream_token(download_url, _audio_token)
            if deliverables:
                deliverables = dict(deliverables)
                for _dk in ("audio", "download"):
                    if deliverables.get(_dk):
                        deliverables[_dk] = _append_stream_token(
                            deliverables[_dk], _audio_token
                        )

        raw_snippet = _clean_recap_display_title(job.input_text) if is_recap else (job.input_text or "")
        out.append({
            "id": job.id,
            "user_id": job.user_id,
            "username": username or "Unknown",
            "first_name": first_name or "",
            "job_type": "RECAP" if is_recap else ("TTS" if job.job_type == JobType.TTS.value else "VOICE_CLONE"),
            "engine": job.engine,
            "preset_voice": job.preset_voice or ("Thiha" if is_recap else "Standard"),
            # Batch BX: friendly display names beside the raw value
            # (TTS engine/voice; own-voice recap label — the admin
            # queue showed the default "komin" slug for those rows).
            "voice_display": _job_display_names(job, is_recap=is_recap, is_tts=(job.job_type == JobType.TTS.value))[0],
            "engine_display": _job_display_names(job, is_recap=is_recap, is_tts=(job.job_type == JobType.TTS.value))[1],
            "status": job.status,
            "input_snippet": (raw_snippet[:70] + "...") if raw_snippet and len(raw_snippet) > 70 else raw_snippet,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "time_ago": time_ago,
            "download_url": download_url,
            "deliverables": deliverables,
            "error_message": job.error_message,
            "file_exists": file_exists,
            "is_expired": is_expired,
        })

    return {
        "success": True,
        "total_count": len(out),
        "total_failed": total_failed,
        "total_success": total_success,
        "total_voice": total_voice,
        "total_recap": total_recap,
        "timeframe": timeframe,
        "status_filter": status,
        "user_id_filter": user_id,
        "job_id_filter": job_id,
        "history": out,
    }


class AdminPurgeHistoryRequest(BaseModel):
    mode: str = "failed"  # "failed", "older_24h", "all_completed", "single", "matching"
    job_id: Optional[str] = None
    timeframe: Optional[str] = None
    status: Optional[str] = None
    job_type: Optional[str] = None


@api_router.post("/admin/history/purge")
async def purge_admin_history(
    req: AdminPurgeHistoryRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Admin Only: Selectively purge jobs, physical media files, and DB records by criteria."""
    # Same defence-in-depth note as get_admin_global_history: the
    # require_admin dependency gates HTTP callers; this keeps direct
    # callers (tests, internal tooling) behind the identical check.
    if not settings.is_admin(user.id):
        raise HTTPException(status_code=403, detail="Admin access required")

    now_utc = datetime.now(timezone.utc)
    stmt = select(Job)

    if req.mode == "single" and req.job_id:
        stmt = stmt.where(Job.id == req.job_id)
    elif req.mode == "failed":
        stmt = stmt.where(Job.status == JobStatus.FAILED.value)
    elif req.mode == "older_retention":
        # Beyond the media retention window (default 24 hours) — the same
        # boundary the automatic sweep uses, so a manual purge can never
        # wipe media users are still entitled to download.
        stmt = stmt.where(Job.created_at < now_utc - timedelta(days=_media_retention_days()))
    elif req.mode == "older_24h":
        stmt = stmt.where(Job.created_at < now_utc - timedelta(hours=24))
    elif req.mode == "all_completed":
        stmt = stmt.where(Job.status.in_([JobStatus.SUCCESS.value, JobStatus.FAILED.value]))
    elif req.mode == "matching":
        if req.timeframe == "older_24h":
            stmt = stmt.where(Job.created_at < now_utc - timedelta(hours=24))
        elif req.timeframe == "24h":
            stmt = stmt.where(Job.created_at >= now_utc - timedelta(hours=24))
        elif req.timeframe == "7d":
            stmt = stmt.where(Job.created_at >= now_utc - timedelta(days=7))

        if req.status and req.status != "all":
            stmt = stmt.where(Job.status == req.status.upper())

        if req.job_type == "RECAP":
            stmt = stmt.where((Job.job_type == JobType.RECAP.value) | Job.id.startswith("rc_") | (Job.engine == "recap"))
        elif req.job_type == "VOICE_CLONE":
            stmt = stmt.where((Job.job_type != JobType.RECAP.value) & (~Job.id.startswith("rc_")) & (Job.engine != "recap"))

    res = await db.execute(stmt)
    jobs_to_delete = res.scalars().all()
    deleted_count = 0

    for j in jobs_to_delete:
        try:
            # 1. Clean recap output directory
            if j.job_type == JobType.RECAP.value or j.id.startswith("rc_") or j.engine == "recap":
                job_dir = settings.storage_root / "recap_output" / j.id
                if job_dir.is_dir():
                    shutil.rmtree(job_dir, ignore_errors=True)

            # 2. Clean voice files
            if j.result_path and Path(j.result_path).is_file():
                try:
                    Path(j.result_path).unlink(missing_ok=True)
                except Exception:
                    pass
            if j.source_audio_path and Path(j.source_audio_path).is_file():
                try:
                    Path(j.source_audio_path).unlink(missing_ok=True)
                except Exception:
                    pass

            # 3. Clean memory registry
            RECAP_JOBS_REGISTRY.pop(j.id, None)

            # 4. Delete record
            await db.delete(j)
            deleted_count += 1
        except Exception as e:
            logger.warning(f"Error purging job {j.id}: {e}")

    await db.commit()
    return {
        "success": True,
        "purged_count": deleted_count,
        "message": f"မှတ်တမ်းနှင့် ဖိုင်တွဲ {deleted_count} ခုကို အောင်မြင်စွာ ရှင်းလင်းဖျက်သိမ်းပြီးပါပြီ။",
    }


# ── 6. VIP Pricing Plans & Payments ────────────────────────────────────────
@api_router.get("/vip/plans")
async def get_vip_plans(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db_session)):
    """Return specialized high-value VIP subscription packages and payment accounts."""
    lang = user.language_code or "my"
    is_my = (lang == "my")

    # Batch O (DECISION backlog item 15): the displayed price/minutes
    # for every plan are resolved from the SAME sources the payment
    # path charges from — PLAN_CATALOGUE overlaid with DB PricingPlan
    # rows, DB winning wherever a row exists — so the Mini App can
    # never show a price that payment would not charge. (Before, this
    # list was a third, fully hardcoded copy: an admin price edit in
    # the DB changed what the bot charged but not what the Mini App
    # displayed.) The presentation copy below is unchanged; its
    # numeric fields are overwritten from this resolution at the end.
    from app.models.schema import PricingPlan
    from app.services.payments import PLAN_CATALOGUE

    _db_by_code = {
        row.code: row
        for row in (await db.execute(select(PricingPlan))).scalars().all()
    }
    _resolved: dict[str, dict] = {}
    for _entry in PLAN_CATALOGUE:
        _row = _db_by_code.get(_entry["code"])
        _resolved[_entry["code"]] = {
            "category": _entry["category"],
            "minutes": int(_row.minutes) if _row is not None else int(_entry["minutes"]),
            "recap_credits": int(_entry["recap_credits"]),
            "price": f"{int(_row.price_mmk) if _row is not None else int(_entry['price_mmk']):,}",
        }

    _result = {
        "plans": [
            {
                "id": "MIN_10",
                "name": "🎙️ Voice Pack — 10 Minutes" if is_my else "🎙️ Voice Pack — 10 Minutes",
                "category": "voice",
                "allocation": "10 Voice Minutes" if is_my else "10 Voice Minutes",
                "minutes": 10,
                "recap_credits": 0,
                "price": "8,000",
                "currency": "MMK",
                "features": [
                    "Voice မိနစ် ၁၀" if is_my else "10 Voice Minutes",
                ],
                "badge": "Voice Pack 💎",
            },
            {
                "id": "MIN_30",
                "name": "🎙️ Voice Pack — 30 Minutes" if is_my else "🎙️ Voice Pack — 30 Minutes",
                "category": "voice",
                "allocation": "30 Voice Minutes" if is_my else "30 Voice Minutes",
                "minutes": 30,
                "recap_credits": 0,
                "price": "20,000",
                "currency": "MMK",
                "features": [
                    "Voice မိနစ် ၃၀" if is_my else "30 Voice Minutes",
                ],
                "badge": "Voice Pack ⭐",
            },
            {
                "id": "MIN_60",
                "name": "🎙️ Voice Pack — 60 Minutes" if is_my else "🎙️ Voice Pack — 60 Minutes",
                "category": "voice",
                "allocation": "60 Voice Minutes" if is_my else "60 Voice Minutes",
                "minutes": 60,
                "recap_credits": 0,
                "price": "38,000",
                "currency": "MMK",
                "features": [
                    "Voice မိနစ် ၆၀" if is_my else "60 Voice Minutes",
                ],
                "badge": "Voice Pack 🔥",
            },
            {
                "id": "voice_studio_pro",
                "name": "🎙️ Voice Pro" if is_my else "🎙️ Voice Pro",
                "category": "voice",
                "allocation": "250 Usable Minutes (4 Hours+)" if is_my else "250 Usable Minutes (4 Hours+)",
                "minutes": 250,
                "recap_credits": 0,
                "price": "75,000",
                "currency": "MMK",
                "features": [
                    "VoxCPM Cloud AI မြန်မာအသံ ၁၂ မျိုး (၆ ကျား / ၆ မ) အပြည့်အဝ အသုံးပြုခွင့်" if is_my else "12 VoxCPM Cloud AI Personas (6M / 6F)",
                    "ကိုယ်ပိုင်အသံ Clone — နမူနာ အသံ ၁၀-၃၀ စက္ကန့် တင်၍ ကိုယ့်အသံနဲ့ ပြောခိုင်းနိုင်ပါတယ်" if is_my else "Custom Voice Clone — upload a 10-30s sample and generate in your own voice",
                    "TTS အသံ တစ်ရက် ၆၀ မိနစ် အထိ သုံးနိုင်ပါတယ်" if is_my else "Up to 60 TTS minutes per day",
                    "အရည်အသွေးမြင့် Lossless WAV တိုက်ရိုက်ဒေါင်းလုဒ်" if is_my else "Direct Lossless WAV Audio Downloads",
                    "တစ်ကြိမ်လျှင် စာလုံးရေ ၅,၀၀၀ အထိ ထုတ်လုပ်နိုင်ခွင့်" if is_my else "Up to 5,000 characters per generation",
                ],
                "badge": "Voice Pro",
            },
            {
                "id": "recap_starter",
                "name": "🎬 Recap Starter" if is_my else "🎬 Recap Starter",
                "category": "recap",
                "allocation": "Recap Credit ၅ ခု (၃ မိနစ်ကို ၁ credit နှုန်း)" if is_my else "5 Recap Credits (1 credit per 3 minutes)",
                "minutes": 0,
                "recap_credits": 5,
                "price": "20,000",
                "currency": "MMK",
                "features": [
                    "Recap Credit ၅ ခု — ဗီဒီယို ၃ မိနစ်ကို ၁ credit နှုန်း (၁၀ မိနစ်ဆို ၄ credit၊ တစ်ကားချင်း အများဆုံး ၁၀ မိနစ်)" if is_my else "5 Recap Credits — 1 credit per 3 minutes of video (a 10-minute video = 4 credits; max 10 mins/video)",
                    "🎬 Smart Video Transform (Smart Blur, Mirror & Zoom)" if is_my else "Smart Video Transform (Smart Blur, Mirror & Zoom)",
                    "Watermark လုံးဝမပါ" if is_my else "Zero Watermark",
                ],
                "badge": "Recap Starter",
            },
            {
                "id": "recap_studio_pro",
                "name": "🎬 Recap Pro" if is_my else "🎬 Recap Pro",
                "category": "recap",
                "allocation": "Recap Credit ၂၅ ခု (၃ မိနစ်ကို ၁ credit နှုန်း)" if is_my else "25 Recap Credits (1 credit per 3 minutes)",
                "minutes": 0,
                "recap_credits": 25,
                "price": "90,000",
                "currency": "MMK",
                "features": [
                    "Recap Credit ၂၅ ခု — ဗီဒီယို ၃ မိနစ်ကို ၁ credit နှုန်း (၁၀ မိနစ်ဆို ၄ credit၊ တစ်ကားချင်း အများဆုံး ၁၀ မိနစ်)" if is_my else "25 Recap Credits — 1 credit per 3 minutes of video (a 10-minute video = 4 credits; max 10 mins/video)",
                    "🎬 Smart Video Transform (Smart Blur, Mirror & Zoom)" if is_my else "Smart Video Transform (Smart Blur, Mirror & Zoom)",
                    "ဇာတ်လမ်းစတိုင် အမျိုးမျိုး (Viral Hook, Drama, Comedy စသဖြင့်) & စာတန်းစတိုင် အမျိုးမျိုး" if is_my else "Multiple narration styles (Viral Hook, Drama, Comedy & more) & subtitle styles",
                    "Watermark လုံးဝမပါ & Channel Logo သီးသန့်ထည့်ခွင့်" if is_my else "Zero Watermark & Custom Channel Logo Branding",
                    "ရှည်လျားသော ဗီဒီယိုများ အပိုင်းခွဲ ထုတ်နိုင်ခြင်း (Split)" if is_my else "Split long videos into parts",
                ],
                "badge": "Recap Pro",
            },
            {
                "id": "combined_starter",
                "name": "🎙️🎬 Combined Starter" if is_my else "🎙️🎬 Combined Starter",
                "category": "combined",
                "allocation": "30 Mins + Recap Credit ၅ ခု (၃ မိနစ်ကို ၁ credit နှုန်း)" if is_my else "30 Mins + 5 Recap Credits (1 credit per 3 minutes)",
                "minutes": 30,
                "recap_credits": 5,
                "price": "30,000",
                "currency": "MMK",
                "features": [
                    "Voice မိနစ် ၃၀ + Recap Credit ၅ ခု — နှစ်မျိုးလုံးကို တစ်ပြိုင်တည်း စမ်းသုံးနိုင်သည်" if is_my else "30 Voice Minutes + 5 Recap Credits — try both products in one plan",
                    "ကိုယ်ပိုင်အသံ Clone — နမူနာ အသံ ၁၀-၃၀ စက္ကန့်" if is_my else "Custom Voice Clone — 10-30s sample",
                    "🎬 Smart Video Transform & Watermark လုံးဝမပါ" if is_my else "Smart Video Transform & Zero Watermark",
                ],
                "badge": "Combined Starter",
            },
            {
                "id": "all_in_one_master",
                "name": "👑 Master" if is_my else "👑 Master",
                "category": "combined",
                "allocation": "500 Mins + Recap Credit ၆၀ ခု (၃ မိနစ်ကို ၁ credit နှုန်း)" if is_my else "500 Mins + 60 Recap Credits (1 credit per 3 minutes)",
                "minutes": 500,
                "recap_credits": 60,
                "price": "180,000",
                "currency": "MMK",
                "features": [
                    "Voice Pro အပြည့်အစုံ (မိနစ် ၅၀၀ - ၈ နာရီကျော် အသံသွင်းခွင့်)" if is_my else "Full Voice Pro (500 Mins - 8+ Hours)",
                    "Recap Pro အပြည့်အစုံ (Recap Credit ၆၀ ခု)" if is_my else "Full Recap Pro (60 Recap Credits)",
                    "👑 ကိုယ်ပိုင်အသံ Clone အပြည့်အစုံ (နမူနာ ၁၀-၃၀ စက္ကန့်)" if is_my else "Full Custom Voice Cloning (10-30s sample)",
                    "🎬 Smart Video Transform Suite (Blur, Mirror, Zoom & Crop)" if is_my else "Smart Video Transform Suite (Blur, Mirror, Zoom & Crop)",
                    "Admin ထံ တိုက်ရိုက် VIP Support" if is_my else "Direct VIP support from the admin",
                ],
                "badge": "Master",
            },
        ],
        "categories": [
            {"id": "voice", "codes": ["MIN_10", "MIN_30", "MIN_60", "voice_studio_pro"]},
            {"id": "recap", "codes": ["recap_starter", "recap_studio_pro"]},
            {"id": "combined", "codes": ["combined_starter", "all_in_one_master"]},
        ],
        "payment_accounts": [
            {
                "provider": "KBZPay",
                "number": getattr(settings, "payment_kpay_number", ""),
                "name": getattr(settings, "payment_kpay_name", ""),
                "color": "#0047BA",
            },
            {
                "provider": "WavePay",
                "number": getattr(settings, "payment_wave_number", ""),
                "name": getattr(settings, "payment_wave_name", ""),
                "color": "#FEE600",
            },
        ],
        "support_telegram": "@VoiceCloneProAdmin",
    }
    # Batch O: overwrite the presentation literal's numeric fields
    # from the catalogue+DB resolution computed above (all 8 plan ids
    # exist in PLAN_CATALOGUE, so every plan is covered).
    for _p in _result["plans"]:
        _r = _resolved.get(_p["id"])
        if _r is not None:
            _p["category"] = _r["category"]
            _p["minutes"] = _r["minutes"]
            _p["recap_credits"] = _r["recap_credits"]
            _p["price"] = _r["price"]
    return _result


@api_router.post("/vip/submit-slip")
async def submit_payment_slip(
    package_code: str = Form(...),
    amount_mmk: int = Form(...),
    slip_file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    _gate: User = Depends(require_gate_verified),
):
    """Submit a payment transfer receipt screenshot for VIP upgrade."""
    # Batch AJ M1: refuse the slip up front when the disk is nearly
    # full instead of writing into a dying filesystem.
    _require_disk_headroom()
    receipts_dir = settings.storage_root / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)

    ext = Path(slip_file.filename or "").suffix.lower() or ".jpg"
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(status_code=400, detail="ပြေစာပုံသည် JPG, PNG သို့မဟုတ် WEBP ဖြစ်ရပါမည်")
    clean_name = f"slip_{user.id}_{uuid.uuid4().hex[:8]}{ext}"
    dest_path = receipts_dir / clean_name

    # Streamed, 10 MB cap (polish reference cap): never buffer the whole
    # slip in memory. _save_upload_streaming raises 413 and deletes the
    # partial file when the cap is exceeded.
    await _save_upload_streaming(slip_file, dest_path, MAX_SLIP_UPLOAD_BYTES)

    # Duplicate-slip guard (hunt #7): the identical slip IMAGE already
    # sitting in one of this user's PENDING requests means this is the
    # same payment evidence submitted twice (double-tap / retry after
    # a hang), not a second purchase — two PENDING rows for one real
    # payment get double-credited if an admin approves both. A
    # genuinely separate payment has a different slip image, so it is
    # never blocked by this check. The bot flow has the twin guard on
    # the Telegram file_id (payments.create_payment_request).
    import hashlib

    def _sha256_of(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    new_slip_hash = await asyncio.to_thread(_sha256_of, dest_path)
    slip_fingerprint = f"sha256:{new_slip_hash}"

    # Batch AJ H1: the file-hash guard below only covers this user's
    # PENDING rows whose slip FILES still exist — after an approval
    # the file is deleted and the row is APPROVED, so the identical
    # image minted a second credit; the same image sent from a second
    # account was never checked at all. The stored content
    # fingerprint is checked GLOBALLY: APPROVED anywhere blocks
    # forever; PENDING by another user blocks; REJECTED never blocks
    # (a rejected slip may be legitimately re-sent after fixing the
    # submission). A partial UNIQUE index on the column (created at
    # startup) backstops races the SELECT cannot see.
    prior_rows = (await db.execute(
        select(PaymentRequest).where(
            PaymentRequest.slip_fingerprint == slip_fingerprint,
            PaymentRequest.status != PaymentStatus.REJECTED.value,
        )
    )).scalars().all()
    for prior in prior_rows:
        if prior.status == PaymentStatus.APPROVED.value or prior.user_id != user.id:
            dest_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=409,
                detail="ဒီပြေစာကို ပို့ထားပြီးသား ဖြစ်ပါတယ်။ ထပ်မပို့ဘဲ စောင့်ပေးပါ။",
            )

    pending_slips = (await db.execute(
        select(PaymentRequest).where(
            PaymentRequest.user_id == user.id,
            PaymentRequest.status == PaymentStatus.PENDING.value,
            PaymentRequest.slip_url.is_not(None),
        )
    )).scalars().all()
    for pending in pending_slips:
        prior_path = receipts_dir / Path(pending.slip_url or "").name
        if prior_path.is_file() and await asyncio.to_thread(_sha256_of, prior_path) == new_slip_hash:
            dest_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=409,
                detail="ဒီပြေစာပုံကို ပို့ထားပြီးသား ဖြစ်ပါတယ်။ Admin စစ်ဆေးနေဆဲမို့ ထပ်မပို့ဘဲ ခဏစောင့်ပေးပါ။",
            )

    # Batch AJ M1: everything from plan resolution to the commit is
    # wrapped — the old code saved the slip BEFORE resolving the plan,
    # so a rejected submission (unknown package, DB error) orphaned
    # the image in receipts/ forever.
    from sqlalchemy.exc import IntegrityError as _IntegrityError

    try:
        # Resolve the plan EXACTLY like the bot flow
        # (payments.create_payment_request): the DB PricingPlan row wins,
        # the static catalogue is the fallback. The webapp previously read
        # the catalogue only, so an admin price edit (which writes the DB
        # row) made bot slips and webapp slips for the same code disagree
        # on minutes/price. Unknown codes are rejected instead of silently
        # defaulting to 250 min.
        from app.models.schema import PricingPlan
        from app.services.payments import get_plan_by_code

        _db_plan = (
            await db.execute(select(PricingPlan).where(PricingPlan.code == package_code))
        ).scalar_one_or_none()
        if _db_plan is not None:
            minutes = _db_plan.minutes
            amount_mmk = _db_plan.price_mmk
        else:
            _plan = get_plan_by_code(package_code)
            if _plan is None:
                raise HTTPException(status_code=400, detail="Unknown plan code")
            minutes = _plan["minutes"]
            # The amount is plan-derived (deep-hunt #4 A6), exactly like
            # the bot flow: the client-typed amount_mmk form field is
            # attacker-controlled, and the stored value is the number the
            # admin sees in the pending list and the Telegram alert — so
            # it must be the plan's real price, never what was typed.
            amount_mmk = _plan["price_mmk"]

        pay_req = PaymentRequest(
            id=str(uuid.uuid4()),
            user_id=user.id,
            package_code=package_code,
            minutes=minutes,
            amount_mmk=amount_mmk,
            slip_url=f"/api/receipts/{clean_name}",
            status=PaymentStatus.PENDING.value,
            slip_fingerprint=slip_fingerprint,
        )
        db.add(pay_req)
        await db.commit()
    except HTTPException:
        dest_path.unlink(missing_ok=True)
        raise
    except _IntegrityError:
        # The startup-created partial unique index fired: a concurrent
        # submission of the same slip won the race (AJ H1 backstop).
        await db.rollback()
        dest_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=409,
            detail="ဒီပြေစာကို ပို့ထားပြီးသား ဖြစ်ပါတယ်။ ထပ်မပို့ဘဲ စောင့်ပေးပါ။",
        )
    except Exception:
        dest_path.unlink(missing_ok=True)
        raise

    # Instant alert to admins via Telegram. Batch I: the alert now
    # carries the same three-action review keyboard as the bot slip
    # flow (previously it was an info-only photo — "approve from the
    # Admin Dashboard"), and every sent copy is recorded on the
    # request row so a terminal action from ANY surface syncs them.
    if settings.bot_token and settings.admin_ids:
        try:
            from aiogram import Bot
            from aiogram.enums import ParseMode
            from aiogram.types import FSInputFile
            from app.services.payment_review import add_admin_copy
            from app.ui.keyboards import admin_pay_review_kb
            bot = Bot(token=settings.bot_token)
            try:
                admin_text = (
                    f"🔔 <b>New VIP Payment Slip Submitted!</b>\n\n"
                    f"👤 <b>User:</b> {html.escape(user.first_name or 'User')} (@{html.escape(user.username or 'none')})\n"
                    f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
                    f"📦 <b>Package:</b> <code>{package_code}</code>\n"
                    f"💰 <b>Amount:</b> <b>{amount_mmk:,} MMK</b>\n"
                    f"🧾 <b>Request ID:</b> <code>{pay_req.id}</code>\n\n"
                    f"<i>စစ်ဆေးပြီးပါက အောက်ပါ ခလုတ်များမှ တိုက်ရိုက် အတည်ပြု/ငြင်းပယ်/ပြင်ဆင် နိုင်သလို Admin Dashboard မှလည်း ပြုလုပ်နိုင်ပါသည်။</i>"
                )
                review_kb = admin_pay_review_kb(pay_req.id)
                sent_copies: list[tuple[int, int, str]] = []
                for admin_id in settings.admin_ids:
                    try:
                        if dest_path.is_file():
                            sent = await bot.send_photo(
                                chat_id=admin_id,
                                photo=FSInputFile(str(dest_path)),
                                caption=admin_text,
                                reply_markup=review_kb,
                                parse_mode=ParseMode.HTML,
                            )
                            sent_copies.append((admin_id, sent.message_id, "photo"))
                        else:
                            sent = await bot.send_message(
                                chat_id=admin_id,
                                text=admin_text,
                                reply_markup=review_kb,
                                parse_mode=ParseMode.HTML,
                            )
                            sent_copies.append((admin_id, sent.message_id, "text"))
                    except Exception as send_err:
                        logger.warning(f"Failed to alert admin {admin_id}: {send_err}")
                if sent_copies:
                    for chat_id, message_id, kind in sent_copies:
                        add_admin_copy(
                            pay_req,
                            chat_id=chat_id,
                            message_id=message_id,
                            kind=kind,
                            text=admin_text,
                        )
                    await db.commit()
            finally:
                await bot.session.close()
        except Exception as alert_err:
            logger.warning(f"Admin payment alert error: {alert_err}")

    return {
        "success": True,
        "request_id": pay_req.id,
        # R40 item 6 / deck A10: the buyer confirmation carries the
        # request ID and the pending state, in the message itself.
        "message": f"ငွေလွှဲပြေစာကို တင်သွင်းပြီးပါပြီ။ တောင်းဆိုမှု နံပါတ်: #{pay_req.id} — Admin မှ စစ်ဆေးပြီးပါက Bot မှ အသိပေးပါမည်။",
    }


@api_router.get("/receipts/{filename}")
async def get_receipt_image(
    filename: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """Serve an uploaded payment receipt image to its owner or an admin only.

    Previously this endpoint had no authentication at all, so anyone who
    could guess/brute-force a slip filename could view payment slips
    (names, amounts, account details -- PII). Missing, not-owned, and
    non-existent files all return the same 404 so ownership cannot be
    probed, and the filename is reduced to a basename and contained
    inside the receipts directory.
    """
    clean_fn = Path(filename).name
    receipts_dir = (settings.storage_root / "receipts").resolve()
    p = (receipts_dir / clean_fn).resolve() if clean_fn else receipts_dir
    if not str(p).startswith(str(receipts_dir)) or not p.is_file():
        raise HTTPException(status_code=404, detail="Receipt image not found")
    if settings.is_admin(user.id):
        return FileResponse(str(p))
    slip_url = f"/api/receipts/{clean_fn}"
    res = await db.execute(
        select(PaymentRequest).where(
            PaymentRequest.slip_url == slip_url,
            PaymentRequest.user_id == user.id,
        )
    )
    if res.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Receipt image not found")
    return FileResponse(str(p))


# ── 7. Role-Gated Admin Console ───────────────────────────────────────────
async def _get_laptop_worker_status(db: AsyncSession) -> dict:
    from app.models.schema import BotSetting
    hb_row = (await db.execute(select(BotSetting).where(BotSetting.key == "worker_laptop_heartbeat"))).scalar_one_or_none()
    eng_row = (await db.execute(select(BotSetting).where(BotSetting.key == "worker_laptop_engine"))).scalar_one_or_none()
    host_row = (await db.execute(select(BotSetting).where(BotSetting.key == "worker_laptop_host"))).scalar_one_or_none()
    stat_row = (await db.execute(select(BotSetting).where(BotSetting.key == "worker_laptop_status"))).scalar_one_or_none()

    if not hb_row or not hb_row.value:
        return {
            "is_online": False,
            "status_code": "OFFLINE",
            "status_text": "Offline (Never Connected)",
            "engine": "ALL",
            "host": None,
            "last_seen_ago": "Never",
        }

    try:
        hb_time = datetime.fromisoformat(hb_row.value)
        if hb_time.tzinfo is None:
            hb_time = hb_time.replace(tzinfo=timezone.utc)
        now_utc = datetime.now(timezone.utc)
        diff_sec = max(0, (now_utc - hb_time).total_seconds())

        last_seen_str = f"{int(diff_sec)}s ago" if diff_sec < 60 else f"{int(diff_sec // 60)}m ago"
        engine_mode = (eng_row.value if eng_row else "all").upper()
        host_name = host_row.value if host_row else "Local Laptop"
        explicit_status = stat_row.value if stat_row else "ONLINE"

        if explicit_status == "OFFLINE" or diff_sec > 120:
            return {
                "is_online": False,
                "status_code": "OFFLINE",
                "status_text": f"Offline 🔴 (Last seen {last_seen_str})",
                "engine": engine_mode,
                "host": host_name,
                "last_seen_ago": last_seen_str,
            }
        elif diff_sec > 45:
            return {
                "is_online": True,
                "status_code": "STANDBY",
                "status_text": f"Standby 🟡 ({last_seen_str})",
                "engine": engine_mode,
                "host": host_name,
                "last_seen_ago": last_seen_str,
            }
        else:
            return {
                "is_online": True,
                "status_code": "ONLINE",
                "status_text": f"Online 🟢 (Pulsed {last_seen_str})",
                "engine": engine_mode,
                "host": host_name,
                "last_seen_ago": last_seen_str,
            }
    except Exception as e:
        return {
            "is_online": False,
            "status_code": "OFFLINE",
            "status_text": f"Offline 🔴 ({e})",
            "engine": "ALL",
            "host": None,
            "last_seen_ago": "Unknown",
        }


def _income_window_bounds():
    """Batch BC: the income windows shared by /admin/overview and
    /admin/income-detail so the detail list always reconciles with the
    card. "Today" is the Asia/Rangoon calendar day (fixed UTC+06:30 —
    Myanmar has no DST); 7d/30d are rolling windows ending now.
    Returns (today_start_utc, since_7d_utc, since_30d_utc)."""
    _rangoon = timezone(timedelta(hours=6, minutes=30))
    _now_utc = datetime.now(timezone.utc)
    _today_start_utc = _now_utc.astimezone(_rangoon).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)
    return _today_start_utc, _now_utc - timedelta(days=7), _now_utc - timedelta(days=30)


def _payment_bucket_ts(reviewed_at, created_at):
    """The moment a payment counts as income: approval moment
    (reviewed_at), falling back to created_at for legacy rows.
    Naive values (SQLite) are UTC by construction."""
    ts = reviewed_at or created_at
    if ts is not None and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


async def _test_flagged_user_ids(db: AsyncSession) -> set:
    """Batch BC: ids of users carrying the admin test-account flag.
    Income aggregations exclude these users' payments read-side;
    the payment rows themselves are never touched."""
    rows = (await db.execute(
        select(User.id).where(User.is_test_account.is_(True))
    )).scalars().all()
    return set(rows)


@api_router.get("/admin/overview")
async def get_admin_overview(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Return high-level operations statistics for Admin Console."""
    user_count_res = await db.execute(select(func.count(User.id)))
    total_users = user_count_res.scalar() or 0

    # Batch X (2026-11-04): REAL users — the overview card's headline.
    # A User row is created for anyone who ever opened the bot (gate
    # visitors, one-tap /start, blocked-at-the-gate accounts), so the
    # raw row count overstates the user base — the owner's complaint
    # behind this batch. "Real" follows the Batch R headline pattern:
    # a user counts when they have actually used the bot, defined as
    #   * at least one Job row, any status (a FAILED job is still a
    #     real attempt at using the product), OR
    #   * an APPROVED payment request (a completed purchase), OR
    #   * a purchase/admin credit grant in the minute ledger
    #     (PURCHASE / ADJUST credits — the ledger side of a purchase
    #     or a manual admin grant).
    # Passive ledger credits (WELCOME / MONTHLY_FREE / REFERRAL) do
    # NOT qualify on their own: they are granted automatically and
    # fire for accounts that never did anything. The components ride
    # along (registered / never-used) for the card's sub-line; the
    # legacy total_users field keeps its literal meaning for every
    # other consumer.
    from app.models.enums import LedgerDirection, LedgerSource

    _job_user_ids = {
        r[0] for r in (await db.execute(select(Job.user_id).distinct())).all()
    }
    _buyer_user_ids = {
        r[0]
        for r in (
            await db.execute(
                select(PaymentRequest.user_id)
                .where(PaymentRequest.status == PaymentStatus.APPROVED.value)
                .distinct()
            )
        ).all()
    }
    _granted_user_ids = {
        r[0]
        for r in (
            await db.execute(
                select(MinuteLedgerEvent.user_id)
                .where(
                    MinuteLedgerEvent.direction == LedgerDirection.CREDIT.value,
                    MinuteLedgerEvent.source.in_(
                        [LedgerSource.PURCHASE.value, LedgerSource.ADJUST.value]
                    ),
                )
                .distinct()
            )
        ).all()
    }
    real_users = len(_job_user_ids | _buyer_user_ids | _granted_user_ids)
    never_used_users = max(0, total_users - real_users)

    vip_count_res = await db.execute(select(func.count(User.id)).where(User.account_category == AccountCategory.PURCHASED.value))
    vip_users = vip_count_res.scalar() or 0

    pending_slips_res = await db.execute(select(func.count(PaymentRequest.id)).where(PaymentRequest.status == PaymentStatus.PENDING.value))
    pending_slips = pending_slips_res.scalar() or 0

    queued_total_res = await db.execute(select(func.count(Job.id)).where(Job.status == JobStatus.QUEUED.value))
    queued_jobs = queued_total_res.scalar() or 0

    running_total_res = await db.execute(select(func.count(Job.id)).where(Job.status == JobStatus.RUNNING.value))
    running_jobs = running_total_res.scalar() or 0


    # ── Production ops visibility (2026-10-31) ──────────────────────
    cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    failed_24h = (await db.execute(
        select(func.count(Job.id)).where(Job.status == JobStatus.FAILED.value, Job.created_at >= cutoff_24h)
    )).scalar() or 0
    jobs_by_status = {}
    for st in JobStatus:
        jobs_by_status[st.value] = (await db.execute(
            select(func.count(Job.id)).where(Job.status == st.value)
        )).scalar() or 0
    # Batch R (2026-11-04): the old card counted ONLY admin bans
    # (status == BLOCKED), so it never moved when a broadcast found
    # users who had blocked the BOT (bot_blocked flag) — the owner
    # read that as the status screen lying. Headline is now the
    # combined OR-count (a user who is both counts once); the two
    # components ride along for the card's sub-line. Semantics are
    # unchanged — this is counting/presentation only.
    banned_by_admin = (await db.execute(
        select(func.count(User.id)).where(User.status == UserStatus.BLOCKED.value)
    )).scalar() or 0
    bot_blocked_users = (await db.execute(
        select(func.count(User.id)).where(User.bot_blocked.is_(True))
    )).scalar() or 0
    banned_users = (await db.execute(
        select(func.count(User.id)).where(
            or_(User.status == UserStatus.BLOCKED.value, User.bot_blocked.is_(True))
        )
    )).scalar() or 0
    new_users_24h = (await db.execute(
        select(func.count(User.id)).where(User.created_at >= cutoff_24h)
    )).scalar() or 0

    # Batch R: income (ဝင်ငွေ) from APPROVED payment requests.
    # amount_mmk is the server-derived plan price and every approval
    # funnels through approve_payment_request, so this sum is the
    # real collected revenue; manual admin grants create no
    # PaymentRequest row and stay excluded. Bucket on the approval
    # moment (reviewed_at), falling back to created_at for legacy
    # rows whose reviewed_at was never populated. "Today" is the
    # Asia/Rangoon calendar day (fixed UTC+06:30 — Myanmar has no
    # DST); 7d/30d are rolling windows ending now. Aggregates are
    # computed per-row in Python — the codebase-wide convention for
    # this API (no SQL SUM under app/, pinned by the premium-round
    # D1 sweep test after the old Decimal-sum hazard).
    _today_start_utc, _since_7d, _since_30d = _income_window_bounds()
    income = {
        "today_mmk": 0, "today_count": 0,
        "last_7d_mmk": 0, "last_7d_count": 0,
        "last_30d_mmk": 0, "last_30d_count": 0,
        "all_time_mmk": 0, "all_time_count": 0,
    }
    # Batch BC-2: payments from test-flagged accounts are NOT income —
    # excluded read-side from every window (sums AND counts). The rows
    # themselves stay untouched; unflagging re-includes them.
    _test_user_ids = await _test_flagged_user_ids(db)
    _approved_rows = (await db.execute(
        select(
            PaymentRequest.reviewed_at,
            PaymentRequest.created_at,
            PaymentRequest.amount_mmk,
            PaymentRequest.user_id,
        ).where(PaymentRequest.status == PaymentStatus.APPROVED.value)
    )).all()
    for _reviewed_at, _created_at, _amount, _payer_id in _approved_rows:
        if _payer_id in _test_user_ids:
            continue
        income["all_time_mmk"] += _amount
        income["all_time_count"] += 1
        _bucket_ts = _payment_bucket_ts(_reviewed_at, _created_at)
        if _bucket_ts is None:
            continue
        if _bucket_ts >= _today_start_utc:
            income["today_mmk"] += _amount
            income["today_count"] += 1
        if _bucket_ts >= _since_7d:
            income["last_7d_mmk"] += _amount
            income["last_7d_count"] += 1
        if _bucket_ts >= _since_30d:
            income["last_30d_mmk"] += _amount
            income["last_30d_count"] += 1

    db_size_bytes = None
    try:
        dialect = db.bind.dialect.name if db.bind is not None else ""
        if dialect == "postgresql":
            db_size_bytes = (await db.execute(text("SELECT pg_database_size(current_database())"))).scalar()
        elif dialect == "sqlite":
            import os as _os
            url = str(db.bind.url)
            path = url.split("///", 1)[-1]
            if path and _os.path.exists(path):
                db_size_bytes = _os.path.getsize(path)
    except Exception:
        db_size_bytes = None

    storage_bytes = None
    try:
        import os as _os
        total = 0
        seen_any = False
        for base in ("/app/storage", "storage"):
            if _os.path.isdir(base):
                seen_any = True
                for root, _dirs, files in _os.walk(base):
                    for f in files:
                        try:
                            total += _os.path.getsize(_os.path.join(root, f))
                        except OSError:
                            pass
                break
        storage_bytes = total if seen_any else None
    except Exception:
        storage_bytes = None

    # Disk-space guard (hunt D2): the 10GB persistent disk had NO
    # free-space visibility anywhere — a full disk fails jobs and
    # uploads mysteriously. Surface usage here and warn in the log
    # when free space drops under 1 GB or usage passes 90%.
    disk_info = None
    try:
        import shutil as _shutil

        usage = _shutil.disk_usage(str(settings.storage_root))
        used_pct = round(usage.used / usage.total * 100, 1) if usage.total else 0.0
        disk_low = usage.free < 1024 ** 3 or used_pct > 90.0
        disk_info = {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_percent": used_pct,
            "low": disk_low,
        }
        if disk_low:
            logger.warning(
                "LOW DISK SPACE on storage volume: %.1f%% used, %d bytes free (root=%s)",
                used_pct, usage.free, settings.storage_root,
            )
    except Exception:
        disk_info = None

    recent_actions = []
    try:
        rows = (await db.execute(
            select(AuditLog).order_by(AuditLog.created_at.desc()).limit(8)
        )).scalars().all()
        recent_actions = [
            {
                "action": r.action,
                "admin_id": r.admin_id,
                "target_type": r.target_type,
                "target_id": r.target_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    except Exception:
        recent_actions = []

    return {
        "total_users": total_users,
        # Batch X: headline + split for the users card (additive —
        # total_users above keeps its literal registered-rows meaning).
        "real_users": real_users,
        "registered_users": total_users,
        "never_used_users": never_used_users,
        "vip_users": vip_users,
        "pending_slips": pending_slips,
        "queued_jobs": queued_jobs,
        "running_jobs": running_jobs,
        "waiting_choice_jobs": jobs_by_status.get(JobStatus.WAITING_CHOICE.value, 0),
        "cloud_worker_status": "ONLINE (Render Cloud)",
        "failed_jobs_24h": failed_24h,
        "jobs_by_status": jobs_by_status,
        "banned_users": banned_users,
        "banned_by_admin": banned_by_admin,
        "bot_blocked_users": bot_blocked_users,
        "income": income,
        "new_users_24h": new_users_24h,
        "db_size_bytes": db_size_bytes,
        "storage_bytes": storage_bytes,
        "disk": disk_info,
        "recent_admin_actions": recent_actions,
    }


@api_router.get("/admin/income-detail")
async def get_admin_income_detail(
    period: str = "all",
    include_test: bool = False,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Batch BC-3: per-payment detail behind the Admin income card.

    Returns every APPROVED payment in the selected window — date/time
    (same COALESCE(reviewed_at, created_at) bucket the card sums),
    user (name + @username), package, amount MMK — newest first.
    ``total_mmk``/``count`` are computed over NON-test rows only, so
    they reconcile with the /admin/overview card figure for the same
    period regardless of ``include_test``. Test-flagged payments are
    omitted unless include_test=true, in which case they ride along
    badged (is_test) for display only and never enter the totals."""
    period = (period or "all").strip().lower()
    if period not in ("today", "7d", "30d", "all"):
        raise HTTPException(status_code=400, detail="Unknown income period")
    _today_start_utc, _since_7d, _since_30d = _income_window_bounds()
    _floor = {
        "today": _today_start_utc,
        "7d": _since_7d,
        "30d": _since_30d,
        "all": None,
    }[period]
    _test_user_ids = await _test_flagged_user_ids(db)
    rows = (await db.execute(
        select(PaymentRequest, User)
        .join(User, PaymentRequest.user_id == User.id)
        .where(PaymentRequest.status == PaymentStatus.APPROVED.value)
    )).all()
    payments = []
    total_mmk = 0
    count = 0
    for req, u in rows:
        _ts = _payment_bucket_ts(req.reviewed_at, req.created_at)
        if _floor is not None and (_ts is None or _ts < _floor):
            continue
        _is_test = req.user_id in _test_user_ids
        if not _is_test:
            total_mmk += req.amount_mmk
            count += 1
        elif not include_test:
            continue
        payments.append({
            "id": req.id,
            "user_id": req.user_id,
            "user_name": u.first_name or f"User {u.id}",
            "username": u.username,
            "package_code": req.package_code,
            "minutes": req.minutes,
            "amount_mmk": req.amount_mmk,
            "is_test": _is_test,
            "paid_at": _ts.isoformat() if _ts else None,
            "_sort_ts": _ts,
        })
    # Newest first; a missing bucket timestamp sorts last.
    payments.sort(
        key=lambda p: p["_sort_ts"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    for p in payments:
        p.pop("_sort_ts", None)
    return {
        "period": period,
        "include_test": bool(include_test),
        "total_mmk": total_mmk,
        "count": count,
        "payments": payments,
    }


@api_router.get("/admin/audit-log")
async def get_admin_audit_log(
    limit: int = 20,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Recent admin actions (AuditLog) for the ops panel."""
    limit = max(1, min(int(limit or 20), 100))
    rows = (await db.execute(
        select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    )).scalars().all()
    return [
        {
            "id": r.id,
            "action": r.action,
            "admin_id": r.admin_id,
            "target_type": r.target_type,
            "target_id": r.target_id,
            "details": r.details_json,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@api_router.get("/admin/key-health")
async def get_admin_key_health(
    refresh: int = 0,
    admin: User = Depends(require_admin),
):
    """Batch BN: API key health (HF/Gemini/Groq pools + Space stages)
    for the Admin tab's Key Health screen.

    The check itself lives in app.services.key_health (an in-process
    port of the twice-daily cron checker): ~90 provider probes, so
    results are cached ~15 min server-side; ``?refresh=1`` forces a
    fresh round. The payload carries pool indexes, env-var labels and
    provider-returned account names ONLY — never key material.
    """
    from app.services import key_health as _key_health

    return await asyncio.to_thread(_key_health.get_key_health, bool(refresh))


_RECAP_STAGE_TIMINGS_STAGES: tuple[str, ...] = (
    "download",
    "transcribe",
    "translate",
    "tts",
    "sync",
    "png",
    "overlay",
    "mux",
)
_STAGE_TIMINGS_CACHE_TTL_SECONDS = 900.0
_STAGE_TIMINGS_CACHE: dict[str, Any] = {}


def clear_admin_stage_timings_cache() -> None:
    """Clear the cached B0 recap stage-timings summary."""
    _STAGE_TIMINGS_CACHE.clear()


def _percentile_sorted(sorted_vals: list[float], q: float) -> float:
    """Compute linear-interpolated quantile over a non-empty sorted list."""
    n = len(sorted_vals)
    if n == 1:
        return round(sorted_vals[0], 2)
    idx = q * (n - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    frac = idx - lo
    val = sorted_vals[lo] + frac * (sorted_vals[hi] - sorted_vals[lo])
    return round(val, 2)


def _summarize_timing_samples(vals: list[float]) -> dict[str, Any]:
    if not vals:
        return {"count": 0, "p50": None, "p95": None, "max": None}
    s = sorted(vals)
    return {
        "count": len(s),
        "p50": _percentile_sorted(s, 0.50),
        "p95": _percentile_sorted(s, 0.95),
        "max": round(s[-1], 2),
    }


def _to_utc_aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _aggregate_recap_stage_timings(
    rows: list[tuple[str, Optional[str], Optional[datetime], Optional[datetime]]],
    *,
    sample_limit: int,
) -> dict[str, Any]:
    stage_buckets: dict[str, list[float]] = {k: [] for k in _RECAP_STAGE_TIMINGS_STAGES}
    totals: list[float] = []
    jobs_with_timings = 0

    for _job_id, raw_cfg, started_at, finished_at in rows:
        timings: dict[str, float] = {}
        if raw_cfg:
            try:
                blob = json.loads(raw_cfg)
            except Exception:
                blob = None
            raw_st = blob.get("stage_timings") if isinstance(blob, dict) else None
            if isinstance(raw_st, dict):
                for sk, sv in raw_st.items():
                    if (
                        isinstance(sv, (int, float))
                        and not isinstance(sv, bool)
                        and math.isfinite(sv)
                        and sv >= 0
                    ):
                        timings[str(sk)] = float(sv)

        has_any_stage = False
        for stage in _RECAP_STAGE_TIMINGS_STAGES:
            if stage in timings:
                stage_buckets[stage].append(timings[stage])
                has_any_stage = True

        s_utc = _to_utc_aware(started_at)
        f_utc = _to_utc_aware(finished_at)
        total_sec: Optional[float] = None
        if s_utc is not None and f_utc is not None and f_utc >= s_utc:
            total_sec = (f_utc - s_utc).total_seconds()
        elif "total" in timings:
            total_sec = timings["total"]
        elif timings:
            total_sec = sum(
                val for key, val in timings.items()
                if key != "total"
            )

        if total_sec is not None and math.isfinite(total_sec) and total_sec >= 0:
            totals.append(float(total_sec))

        if has_any_stage or timings:
            jobs_with_timings += 1

    return {
        "sample_limit": sample_limit,
        "jobs_scanned": len(rows),
        "jobs_with_timings": jobs_with_timings,
        "stages": {
            stage: _summarize_timing_samples(stage_buckets[stage])
            for stage in _RECAP_STAGE_TIMINGS_STAGES
        },
        "total": _summarize_timing_samples(totals),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cached": False,
    }


@api_router.get("/admin/stage-timings")
async def get_admin_stage_timings(
    refresh: int = 0,
    limit: int = Query(100, ge=1, le=100),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Part B0: aggregate per-stage and total timings over recent completed recap jobs.

    Reads the last ``limit`` (default 100) completed RECAP jobs' persisted
    ``stage_timings`` without adding work to the recap execution path.
    Cached per DB bind for ~15 min; pass ``?refresh=1`` for on-demand refresh.
    """
    sample_limit = min(max(1, int(limit or 100)), 100)
    try:
        bind_id = id(db.get_bind())
    except Exception:
        bind_id = None
    cache_key = (bind_id, sample_limit)
    now_mono = time.monotonic()

    if not refresh:
        cached_entry = _STAGE_TIMINGS_CACHE.get(cache_key)
        if cached_entry and now_mono < cached_entry.get("expires_at", 0.0):
            return {**cached_entry["payload"], "cached": True}

    stmt = (
        select(Job.id, Job.recap_config, Job.started_at, Job.finished_at)
        .where(
            Job.status == JobStatus.SUCCESS.value,
            or_(
                Job.job_type == JobType.RECAP.value,
                Job.id.startswith("rc_"),
                Job.engine == "recap",
            ),
        )
        .order_by(func.coalesce(Job.finished_at, Job.created_at).desc(), Job.id.desc())
        .limit(sample_limit)
    )
    rows = list((await db.execute(stmt)).all())
    payload = _aggregate_recap_stage_timings(rows, sample_limit=sample_limit)
    _STAGE_TIMINGS_CACHE[cache_key] = {
        "payload": payload,
        "expires_at": time.monotonic() + _STAGE_TIMINGS_CACHE_TTL_SECONDS,
    }
    return payload


def _admin_category_filter(category: str = "") -> str:
    """Normalize the optional users-list category filter.

    Only the two real account categories filter anything; an absent or
    unrecognized value means "no filter" so a stale client can never
    blank the admin's user list by sending garbage.
    """
    cat = (category or "").strip().upper()
    return cat if cat in (AccountCategory.FREE.value, AccountCategory.PURCHASED.value) else ""


@api_router.get("/admin/users/search")
async def search_admin_users(
    q: str = "",
    limit: int = 20,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
    category: str = "",
):
    """Search users by Telegram ID, @username or first name (admin only).

    The optional ``category`` (FREE|PURCHASED) narrows the search to the
    Users screen's active filter chip, same as the browse list.
    """
    query = (q or "").strip().lstrip("@")
    if len(query) < 2:
        raise HTTPException(status_code=400, detail="Search query too short (min 2 characters)")
    limit = max(1, min(int(limit or 20), 50))
    conds = [
        User.username.ilike(f"%{query}%"),
        User.first_name.ilike(f"%{query}%"),
    ]
    if query.isdigit():
        conds.append(User.id == int(query))
    stmt = select(User).where(or_(*conds))
    cat = _admin_category_filter(category)
    if cat:
        stmt = stmt.where(User.account_category == cat)
    rows = (await db.execute(
        stmt.order_by(User.created_at.desc()).limit(limit)
    )).scalars().all()
    out = []
    for u in rows:
        bal = await db.get(MinuteBalance, u.id)
        out.append({
            "id": u.id,
            "username": u.username,
            "first_name": u.first_name,
            "account_category": u.account_category,
            "status": u.status,
            "is_banned": (u.status or "") == UserStatus.BLOCKED.value,
            "bot_blocked": bool(getattr(u, "bot_blocked", False)),
            "is_test_account": bool(getattr(u, "is_test_account", False)),
            "recap_credits": getattr(u, "recap_credits", 0) or 0,
            "balance_seconds": int(bal.total_usable_seconds) if bal else 0,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        })
    return {"users": out, "count": len(out)}


async def _admin_user_row(db: AsyncSession, u: User) -> dict:
    """Canonical admin user payload (same shape as /admin/users/search)."""
    bal = await db.get(MinuteBalance, u.id)
    return {
        "id": u.id,
        "username": u.username,
        "first_name": u.first_name,
        "account_category": u.account_category,
        "status": u.status,
        "is_banned": (u.status or "") == UserStatus.BLOCKED.value,
        "bot_blocked": bool(getattr(u, "bot_blocked", False)),
        "is_test_account": bool(getattr(u, "is_test_account", False)),
        "recap_credits": getattr(u, "recap_credits", 0) or 0,
        "balance_seconds": int(bal.total_usable_seconds) if bal else 0,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


@api_router.get("/admin/users")
async def list_admin_users(
    limit: int = 30,
    offset: int = 0,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
    category: str = "",
    search: str = "",
):
    """Most recently joined users (admin only) — the Users screen landing list.

    Search requires a query of 2+ characters; admins also need a way to
    simply browse who just joined without knowing a name or ID first.
    The optional ``category`` (FREE|PURCHASED) filters server-side (the
    Users screen's filter chips page through the whole user base with
    limit/offset, so filtering must happen here, not over one page);
    ``total`` reports the filtered row count so the client knows when
    "Load more" has reached the end.

    The optional ``search`` narrows the browse list with the SAME
    matching semantics as /admin/users/search (username or first name
    contains the query, a leading "@" is stripped, a numeric query
    also matches the exact Telegram ID) and composes with ``category``.
    Unlike the dedicated search route it never hard-errors: an absent
    or blank ``search`` is simply "no filter", so a client paging the
    browse list can pass the box's contents verbatim. (F4, QA cycle
    2026-11-02: this parameter used to be silently ignored.)
    """
    limit = max(1, min(int(limit or 30), 100))
    offset = max(0, int(offset or 0))
    stmt = select(User)
    count_stmt = select(func.count(User.id))
    cat = _admin_category_filter(category)
    if cat:
        stmt = stmt.where(User.account_category == cat)
        count_stmt = count_stmt.where(User.account_category == cat)
    query = (search or "").strip().lstrip("@")
    if query:
        conds = [
            User.username.ilike(f"%{query}%"),
            User.first_name.ilike(f"%{query}%"),
        ]
        if query.isdigit():
            conds.append(User.id == int(query))
        stmt = stmt.where(or_(*conds))
        count_stmt = count_stmt.where(or_(*conds))
    total = (await db.execute(count_stmt)).scalar() or 0
    rows = (await db.execute(
        stmt.order_by(User.created_at.desc()).limit(limit).offset(offset)
    )).scalars().all()
    out = [await _admin_user_row(db, u) for u in rows]
    return {"users": out, "count": len(out), "limit": limit, "offset": offset, "total": total}


@api_router.get("/admin/users/{user_id}")
async def get_admin_user_detail(
    user_id: int,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Full detail for one user (admin only): profile, balances, job stats.

    Powers the Mini App user-detail screen reached from search results,
    the recent-users list, job rows and slip rows.
    """
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    payload = await _admin_user_row(db, target)
    payload["language_code"] = target.language_code

    async def _count(*conds) -> int:
        return (await db.execute(
            select(func.count(Job.id)).where(Job.user_id == user_id, *conds)
        )).scalar() or 0

    payload["jobs_total"] = await _count()
    payload["jobs_success"] = await _count(Job.status == JobStatus.SUCCESS.value)
    payload["jobs_failed"] = await _count(Job.status == JobStatus.FAILED.value)
    payload["jobs_active"] = await _count(
        Job.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value])
    )
    last_job_at = (await db.execute(
        select(func.max(Job.created_at)).where(Job.user_id == user_id)
    )).scalar()
    payload["last_job_at"] = last_job_at.isoformat() if last_job_at else None

    # ── Batch CD Item 1: the completed detail (read-side only) ──────
    from app.models.schema import (
        CustomVoiceRequest,
        PrivateAiMessage,
        Referral,
        ReferralReward,
    )

    bal = await db.get(MinuteBalance, user_id)
    payload["balance_breakdown"] = {
        "monthly_free_seconds": int(bal.monthly_free_seconds or 0) if bal else 0,
        "welcome_seconds": int(bal.welcome_seconds or 0) if bal else 0,
        "referral_seconds": int(bal.referral_seconds or 0) if bal else 0,
        "purchased_seconds": int(bal.purchased_seconds or 0) if bal else 0,
        "consumed_seconds": int(bal.consumed_seconds or 0) if bal else 0,
    }

    # TTS today, through the SAME helpers /api/me (Batch BZ) and the
    # submit path enforce with — the admin screen can never disagree
    # with the user's own surfaces. Admin targets are cap-exempt, so
    # their fields are null (the client paints ∞), like /api/me.
    tts_today = {
        "daily_limit_seconds": None,
        "consumed_seconds": None,
        "remaining_seconds": None,
    }
    if not settings.is_admin(user_id):
        try:
            from app.services.tts_service import (
                tts_cap_consumed_seconds,
                tts_daily_limit_minutes,
            )

            _limit_min = tts_daily_limit_minutes(target, is_admin=False)
            _consumed = await tts_cap_consumed_seconds(db, user_id)
            _referral = int(bal.referral_seconds or 0) if bal else 0
            tts_today["daily_limit_seconds"] = int(_limit_min) * 60
            tts_today["consumed_seconds"] = round(float(_consumed), 1)
            if _limit_min > 0:
                tts_today["remaining_seconds"] = round(
                    _referral + max(0.0, _limit_min * 60 - float(_consumed)), 1
                )
        except Exception:
            pass  # a quota read must never break the detail screen
    payload["tts_today"] = tts_today

    pay_rows = (await db.execute(
        select(PaymentRequest)
        .where(PaymentRequest.user_id == user_id)
        .order_by(PaymentRequest.created_at.desc())
        .limit(10)
    )).scalars().all()
    payload["payments"] = [
        {
            "id": p.id,
            "plan": p.package_code,
            "amount_mmk": int(p.amount_mmk or 0),
            "status": p.status,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in pay_rows
    ]
    # Per-user truth (this user's approved total) — NOT the income
    # card: test-flag exclusions do not apply to a single user's own
    # record. Aggregated per-row in Python, never SQL SUM (the
    # codebase's standing aggregation rule, Batch R / premium D1).
    approved_amounts = (await db.execute(
        select(PaymentRequest.amount_mmk).where(
            PaymentRequest.user_id == user_id,
            PaymentRequest.status == PaymentStatus.APPROVED.value,
        )
    )).scalars().all()
    payload["payments_total_mmk"] = int(
        sum(int(a or 0) for a in approved_amounts)
    )

    referred_count = (await db.execute(
        select(func.count(Referral.id)).where(Referral.referrer_id == user_id)
    )).scalar() or 0
    reward_seconds = (await db.execute(
        select(ReferralReward.seconds).where(
            ReferralReward.referrer_id == user_id
        )
    )).scalars().all()
    referral_earned = int(sum(int(s or 0) for s in reward_seconds))
    referred_by = None
    own_referral = (await db.execute(
        select(Referral).where(Referral.referred_id == user_id)
    )).scalars().first()
    if own_referral is not None:
        referrer = await db.get(User, own_referral.referrer_id)
        if referrer is not None:
            referred_by = {
                "id": referrer.id,
                "username": referrer.username,
                "first_name": referrer.first_name,
            }
    payload["referrals"] = {
        "referred_count": int(referred_count),
        "referral_earned_seconds": int(referral_earned),
        "referred_by": referred_by,
    }

    job_rows = (await db.execute(
        select(Job)
        .where(Job.user_id == user_id)
        .order_by(Job.created_at.desc())
        .limit(10)
    )).scalars().all()
    payload["recent_jobs"] = [
        {
            "id": j.id,
            "job_type": j.job_type,
            "status": j.status,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
            "seconds_estimated": int(j.seconds_estimated or 0),
            "seconds_consumed": int(j.seconds_consumed or 0),
        }
        for j in job_rows
    ]

    ledger_rows = (await db.execute(
        select(MinuteLedgerEvent)
        .where(MinuteLedgerEvent.user_id == user_id)
        .order_by(MinuteLedgerEvent.created_at.desc(), MinuteLedgerEvent.id.desc())
        .limit(10)
    )).scalars().all()
    payload["ledger_recent"] = [
        {
            "kind": e.source,
            "delta": int(e.seconds or 0) if e.direction == "CREDIT" else -int(e.seconds or 0),
            "reason": e.note,
            "reference_id": e.reference_id,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in ledger_rows
    ]

    # Batch CU: recap-credit movements live in their own ledger table
    # and were never merged in, so recap activity was invisible here.
    from app.models.schema import RecapCreditLedgerEvent as _RecapLedger

    recap_ledger_rows = (await db.execute(
        select(_RecapLedger)
        .where(_RecapLedger.user_id == user_id)
        .order_by(_RecapLedger.created_at.desc(), _RecapLedger.id.desc())
        .limit(10)
    )).scalars().all()
    payload["recap_ledger_recent"] = [
        {
            "kind": e.source,
            "delta": int(e.credits or 0) if e.direction == "CREDIT" else -int(e.credits or 0),
            "reason": e.note,
            "reference_id": e.reference_id,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in recap_ledger_rows
    ]

    # Batch CV: BYOK Gemini key presence — admins need to see whether
    # a user added their own key (support questions hinge on it). Only
    # presence + state + last4 ever leave the server, never the key.
    _own_key = (target.gemini_api_key or "").strip()
    payload["gemini_key"] = {
        "has_key": bool(_own_key),
        "state": target.gemini_key_state or "",
        "last4": _own_key[-4:] if _own_key else None,
    }

    payload["verification"] = {
        "channel_verified": bool(target.channel_verified),
        "group_verified": bool(target.group_verified),
    }
    payload["custom_voices_count"] = (await db.execute(
        select(func.count(CustomVoiceRequest.id)).where(
            CustomVoiceRequest.user_id == user_id
        )
    )).scalar() or 0

    # Recorded private-AI exchanges (Batch CD Item 3): last 50 rows,
    # oldest → newest so the chat log reads top-down.
    chat_rows = (await db.execute(
        select(PrivateAiMessage)
        .where(PrivateAiMessage.user_id == user_id)
        .order_by(PrivateAiMessage.id.desc())
        .limit(50)
    )).scalars().all()
    payload["ai_chats"] = [
        {
            "direction": m.direction,
            "kind": m.kind,
            "text": m.text,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in reversed(chat_rows)
    ]
    return payload


class AdminUserMessageRequest(BaseModel):
    text: str = ""


@api_router.post("/admin/users/{user_id}/message")
async def send_admin_user_message(
    user_id: int,
    req: AdminUserMessageRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Send ONE user a direct bot message from the detail screen
    (Batch CD Item 2). Every attempt — delivered or not — writes an
    AuditLog row (BC-4 conventions: admin id, target, text snippet +
    length in details_json, never a second copy of the full body
    beyond the audit record). A user who blocked the bot gets a
    reportable 409, never a silent 'sent'."""
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message text is empty")
    if len(text) > 4000:
        raise HTTPException(
            status_code=400,
            detail="Message is too long (max 4000 characters)",
        )
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if not settings.bot_token:
        # Same honesty rule as the broadcast path (Batch AJ M2):
        # nothing can be sent without a token — say so, never fake it.
        raise HTTPException(status_code=503, detail="Bot token is not configured")

    from aiogram import Bot
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

    delivered = False
    error_kind = None
    bot = Bot(token=settings.bot_token)
    try:
        await bot.send_message(chat_id=user_id, text=text)
        delivered = True
    except TelegramForbiddenError:
        error_kind = "bot_blocked"
    except TelegramBadRequest:
        error_kind = "chat_not_found"
    except Exception:
        error_kind = "send_failed"
    finally:
        await bot.session.close()

    db.add(AuditLog(
        admin_id=admin.id,
        action="user.message",
        target_type="user",
        target_id=str(user_id),
        details_json=json.dumps({
            "snippet": text[:80],
            "length": len(text),
            "delivered": delivered,
            "error": error_kind,
            "via": "miniapp",
        }),
    ))
    await db.commit()

    if not delivered:
        # 409-style reportable errors the client renders distinctly:
        # the bot-blocked copy is its own i18n key client-side, keyed
        # off this stable detail token.
        raise HTTPException(status_code=409, detail=error_kind or "send_failed")
    return {"ok": True}


class BanUserRequest(BaseModel):
    reason: str = ""


@api_router.post("/admin/users/{user_id}/ban")
async def ban_admin_user(
    user_id: int,
    req: BanUserRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Suspend a user everywhere (bot middleware + Mini App gate)."""
    reason = (req.reason or "").strip()
    if len(reason) < 3:
        raise HTTPException(status_code=400, detail="A ban reason is required (min 3 characters)")
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot ban yourself")
    if settings.is_admin(user_id):
        raise HTTPException(status_code=400, detail="You cannot ban an admin")
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    already = (target.status or "") == UserStatus.BLOCKED.value
    target.status = UserStatus.BLOCKED.value
    db.add(AuditLog(
        admin_id=admin.id,
        action="user.ban",
        target_type="user",
        target_id=str(user_id),
        details_json=json.dumps({"reason": reason, "via": "miniapp", "already_banned": already}),
    ))
    await db.commit()
    return {"success": True, "message": f"အသုံးပြုသူ {user_id} ကို Ban လိုက်ပါပြီ", "already_banned": already}


@api_router.post("/admin/users/{user_id}/unban")
async def unban_admin_user(
    user_id: int,
    req: BanUserRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Lift a suspension."""
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    target.status = UserStatus.ACTIVE.value
    # Lifting a ban also clears any bot-blocked send-suppression flag so
    # the user starts fully clean.
    target.bot_blocked = False
    db.add(AuditLog(
        admin_id=admin.id,
        action="user.unban",
        target_type="user",
        target_id=str(user_id),
        details_json=json.dumps({"reason": (req.reason or "").strip(), "via": "miniapp"}),
    ))
    await db.commit()
    return {"success": True, "message": f"အသုံးပြုသူ {user_id} ၏ Ban ကို ဖယ်ရှားပြီးပါပြီ"}


class TestFlagRequest(BaseModel):
    is_test_account: bool


@api_router.post("/admin/users/{user_id}/test-flag")
async def set_admin_user_test_flag(
    user_id: int,
    req: TestFlagRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Batch BC-1: set/unset the per-user test-account flag.

    Flagged users' APPROVED payments are excluded from the Admin
    income totals (read-side only — records are never altered) and
    surface badged TEST in the income detail view. The toggle is
    audit-logged like the ban/unban actions next to it. No user ID
    is hardcoded anywhere: the flag is the only signal."""
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    target.is_test_account = bool(req.is_test_account)
    db.add(AuditLog(
        admin_id=admin.id,
        action="user.test_flag",
        target_type="user",
        target_id=str(user_id),
        details_json=json.dumps({
            "is_test_account": bool(req.is_test_account),
            "via": "miniapp",
        }),
    ))
    await db.commit()
    return {"success": True, "is_test_account": bool(req.is_test_account)}


@api_router.get("/admin/queue")
async def get_admin_queue(
    status: Optional[str] = "all",
    engine: Optional[str] = "all",
    limit: int = 60,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """
    Real-time Job Queue & Execution Monitor for Admin Console.
    Supports filtering by status (all, queued, running, success, failed, cancelled)
    and by engine (all, voxcpm, recap; legacy rows may still carry the old engine value).
    """
    cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)

    queued_cnt = (await db.execute(select(func.count(Job.id)).where(Job.status == JobStatus.QUEUED.value))).scalar() or 0
    running_cnt = (await db.execute(select(func.count(Job.id)).where(Job.status == JobStatus.RUNNING.value))).scalar() or 0
    waiting_choice_cnt = (await db.execute(select(func.count(Job.id)).where(Job.status == JobStatus.WAITING_CHOICE.value))).scalar() or 0
    success_cnt = (await db.execute(select(func.count(Job.id)).where(Job.status == JobStatus.SUCCESS.value, Job.created_at >= cutoff_24h))).scalar() or 0
    failed_cnt = (await db.execute(select(func.count(Job.id)).where(Job.status == JobStatus.FAILED.value, Job.created_at >= cutoff_24h))).scalar() or 0

    query = select(Job, User).outerjoin(User, Job.user_id == User.id)

    if status and status.lower() != "all":
        query = query.where(Job.status == status.upper())

    if engine and engine.lower() != "all":
        query = query.where(Job.engine == engine.lower())

    query = query.order_by(Job.created_at.desc()).limit(min(limit, 100))
    res = await db.execute(query)
    rows = res.all()

    now_utc = datetime.now(timezone.utc)
    jobs_out = []
    for j, u in rows:
        created_ts = j.created_at.timestamp() if j.created_at else time.time()
        mins_ago = int((time.time() - created_ts) // 60)
        time_ago = _time_ago_str(mins_ago, admin.language_code or "my")

        duration_sec = None
        if j.started_at and j.finished_at:
            duration_sec = round((j.finished_at - j.started_at).total_seconds(), 1)
        elif j.started_at and j.status == JobStatus.RUNNING.value:
            s_ts = j.started_at.replace(tzinfo=timezone.utc) if j.started_at.tzinfo is None else j.started_at
            duration_sec = round((now_utc - s_ts).total_seconds(), 1)

        is_recap = (j.job_type == JobType.RECAP.value or j.id.startswith("rc_") or j.engine == "recap")

        # Verify whether the output file actually exists on the persistent
        # disk — the single availability check every serializer shares.
        # (This endpoint previously handed out download URLs for SUCCESS
        # jobs even when it had just flagged them expired, which is how the
        # media modal ended up with live buttons for dead files.)
        file_exists = False
        if j.status == JobStatus.SUCCESS.value:
            file_exists = check_job_file_exists(j.id, is_recap, j.result_path)
        is_expired = bool(j.status == JobStatus.SUCCESS.value and not file_exists)

        download_url, deliverables = _job_deliverables(j, is_recap, file_exists)
        if is_recap and deliverables:
            # Batch CJ: sign the recap URLs for the ADMIN (see the
            # admin history serializer) — the raw URLs left the Admin
            # queue's media-modal downloads dead on phones because
            # downloadAuthFile only escapes to the device browser via
            # Telegram openLink for token-carrying URLs.
            deliverables = _with_stream_url(deliverables, j.id, admin.id)
            download_url = deliverables.get("video") or download_url
            # Batch CK: video/audio deliverables move to the download
            # route (save, not play) — see the admin history serializer.
            deliverables = _recap_deliverables_download_form(deliverables)
        if not is_recap and download_url:
            # Batch CJ: voice/TTS admin URLs carry the Batch BJ
            # audio-scope token, minted for the admin.
            _audio_token = _make_stream_token(j.id, admin.id, "audio")
            download_url = _append_stream_token(download_url, _audio_token)
            if deliverables:
                deliverables = dict(deliverables)
                for _dk in ("audio", "download"):
                    if deliverables.get(_dk):
                        deliverables[_dk] = _append_stream_token(
                            deliverables[_dk], _audio_token
                        )

        jobs_out.append({
            "id": j.id,
            "user_id": j.user_id,
            "user_name": (u.first_name if u else None) or f"User {j.user_id}",
            "username": u.username if u else None,
            "status": j.status,
            "job_type": "RECAP" if is_recap else ("TTS" if j.job_type == JobType.TTS.value else "VOICE_CLONE"),
            "engine": j.engine or ("recap" if is_recap else "voxcpm"),
            "preset_voice": j.preset_voice or ("Thiha" if is_recap else "Standard"),
            # Batch BX: friendly display names beside the raw value
            # (TTS engine/voice; own-voice recap label).
            "voice_display": _job_display_names(j, is_recap=is_recap, is_tts=(j.job_type == JobType.TTS.value))[0],
            "engine_display": _job_display_names(j, is_recap=is_recap, is_tts=(j.job_type == JobType.TTS.value))[1],
            "input_snippet": (j.input_text[:80] + "...") if j.input_text and len(j.input_text) > 80 else (j.input_text or ""),
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "started_at": j.started_at.isoformat() if j.started_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
            "duration_seconds": duration_sec,
            "created_ago": time_ago,
            "download_url": download_url,
            "deliverables": deliverables,
            "error_message": j.error_message,
            "file_exists": file_exists,
            "is_expired": is_expired,
        })

    worker_info = await _get_laptop_worker_status(db)

    return {
        "success": True,
        "counts": {
            "queued": queued_cnt,
            "running": running_cnt,
            "waiting_choice": waiting_choice_cnt,
            "success": success_cnt,
            "failed": failed_cnt,
            "total_listed": len(jobs_out),
        },
        "worker": worker_info,
        "jobs": jobs_out,
    }


@api_router.post("/admin/queue/{job_id}/retry")
async def retry_queued_job(
    job_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Re-enqueue a failed or cancelled job so it will be claimed and re-executed.

    Batch AJ H6: a recap job whose charge was REFUNDED (failure /
    cancel paths refund in full) is RE-CHARGED from the recap ledger
    before requeueing — the old behaviour re-dispatched it uncharged,
    so every refunded failure could be re-run free from this button.
    If the user can no longer cover the original charge, the retry is
    refused (402) and the job stays FAILED/CANCELLED. A job whose
    charge was never refunded keeps its charge (no double charge).
    Voice jobs need nothing here: the worker's consume is net-aware
    and re-bills exactly the unrefunded remainder on completion.
    """
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Guard (2026-10-31): previously ANY job could be "retried" — including
    # SUCCESS or RUNNING ones, which would double-execute and double-deliver.
    # Only terminally unsuccessful jobs are retryable.
    if job.status not in (JobStatus.FAILED.value, JobStatus.CANCELLED.value):
        raise HTTPException(
            status_code=400,
            detail=f"Only FAILED or CANCELLED jobs can be retried (this job is {job.status})",
        )

    _next_recap_config: Optional[str] = None
    _is_recap_retry = (
        job.job_type == JobType.RECAP.value or job.id.startswith("rc_") or job.engine == "recap"
    )
    if _is_recap_retry:
        from app.services.ledger import (
            recap_debit_total_for_job,
            recap_refund_total_for_job,
            record_recap_credit_event,
        )

        # Lock the owner row before inspecting/re-charging credits or voice
        # minutes so concurrent retries or submits serialize cleanly.
        _owner = (
            await db.execute(
                select(User).where(User.id == job.user_id).with_for_update()
            )
        ).scalar_one_or_none()
        _debit_total = await recap_debit_total_for_job(db, job_id)
        _refund_total = await recap_refund_total_for_job(db, job_id)
        if _debit_total > 0 and _debit_total - _refund_total <= 0:
            # Fully refunded: re-charge the original amount in THIS
            # transaction (same commit as the status flip below).
            if _owner is None or int(_owner.recap_credits or 0) < _debit_total:
                await db.rollback()
                raise HTTPException(
                    status_code=402,
                    detail="အသုံးပြုသူရဲ့ Recap Credit မလုံလောက်တာကြောင့် ဒီအလုပ်ကို ပြန်စလို့ မရပါ။",
                )
            _charge_upd = await db.execute(
                update(User)
                .where(User.id == job.user_id, User.recap_credits >= _debit_total)
                .values(recap_credits=User.recap_credits - _debit_total)
            )
            if not _charge_upd.rowcount:
                await db.rollback()
                raise HTTPException(
                    status_code=402,
                    detail="အသုံးပြုသူရဲ့ Recap Credit မလုံလောက်တာကြောင့် ဒီအလုပ်ကို ပြန်စလို့ မရပါ။",
                )
            await db.refresh(_owner)
            await record_recap_credit_event(
                db,
                user_id=job.user_id,
                direction="DEBIT",
                credits=_debit_total,
                reference_id=job_id,
                note="Admin queue retry re-charge (Batch AJ H6)",
            )
            _state = _load_persisted_recap_state(job)
            if _state is not None:
                _state["credit_deducted"] = True
                _state["credits_charged"] = _debit_total
                _state["credit_refunded"] = False
                _state["credits_refunded"] = 0
                if (
                    _state.get("billing_mode") == _RECAP_BILLING_CREDIT_PLUS_VOICE
                    and int(_state.get("voice_seconds_held") or 0) > 0
                    and not settings.is_admin(job.user_id)
                ):
                    from app.services.ledger import consume_seconds, net_job_seconds_held

                    if await net_job_seconds_held(db, job.user_id, job_id) <= 0:
                        try:
                            await consume_seconds(
                                db,
                                job.user_id,
                                int(_state["voice_seconds_held"]),
                                job_id=job_id,
                                note=f"Admin queue retry voice hold ({job_id})",
                            )
                        except ValueError:
                            await db.rollback()
                            raise HTTPException(
                                status_code=402,
                                detail="အသုံးပြုသူရဲ့ Voice Clone မိနစ် မလုံလောက်တာကြောင့် ဒီအလုပ်ကို ပြန်စလို့ မရပါ။",
                            )
                    _state["voice_refunded"] = False
                    _state["voice_seconds_refunded"] = 0
                    _state["voice_settled"] = False
                    _state["voice_seconds_settled"] = None
                _state["stranded_reset_count"] = 0
                _next_recap_config = json.dumps(_state, ensure_ascii=False)
        elif _debit_total > 0:
            # Charged and NOT (fully) refunded: make the persisted
            # credit state match the ledger truth so the re-dispatched
            # runner neither re-charges nor refunds a phantom amount.
            _state = _load_persisted_recap_state(job)
            if _state is not None:
                _state["credit_deducted"] = True
                _state["credits_charged"] = _debit_total
                _state["credit_refunded"] = _refund_total >= _debit_total
                _state["credits_refunded"] = _refund_total
                if _state.get("stranded_reset_count"):
                    _state["stranded_reset_count"] = 0
                _next_recap_config = json.dumps(_state, ensure_ascii=False)
        else:
            _retry_state = _load_persisted_recap_state(job)
            if _retry_state is not None and _retry_state.get("stranded_reset_count"):
                _retry_state["stranded_reset_count"] = 0
                _next_recap_config = json.dumps(_retry_state, ensure_ascii=False)

    _retry_values: dict[str, Any] = {
        "status": JobStatus.QUEUED.value,
        "started_at": None,
        "finished_at": None,
        "error_message": None,
    }
    if _next_recap_config is not None:
        _retry_values["recap_config"] = _next_recap_config
    _retry_upd = await db.execute(
        update(Job)
        .where(
            Job.id == job_id,
            Job.status.in_((JobStatus.FAILED.value, JobStatus.CANCELLED.value)),
        )
        .values(**_retry_values)
        .execution_options(synchronize_session=False)
    )
    if not _retry_upd.rowcount:
        await db.rollback()
        raise HTTPException(
            status_code=400,
            detail="Only FAILED or CANCELLED jobs can be retried (job state changed concurrently)",
        )
    await db.commit()
    await db.refresh(job)

    if job.job_type == JobType.RECAP.value or job.id.startswith("rc_") or job.engine == "recap":
        try:
            from app.webapp.api import _run_recap_background_job, _recap_redispatch_plan
            u = await db.get(User, job.user_id)
            # Batch AJ H6: restore the credit state — the block above
            # has already reconciled it with the ledger (re-charged if
            # the original charge was refunded, kept if it was not).
            plan = _recap_redispatch_plan(job, u, restore_credit_state=True)
            plan["entry"]["progress_label"] = t("recap_progress_requeued", getattr(u, "language_code", None) or "my")
            RECAP_JOBS_REGISTRY[job_id] = plan["entry"]
            asyncio.create_task(_run_recap_background_job(
                job_id, plan["cfg"], job.user_id, job.chat_id or job.user_id,
                is_vip=plan["is_vip"], credit_deducted=plan["credit_deducted"], phase=plan["phase"],
            ))
        except Exception as e:
            logger.warning(f"Could not re-dispatch recap job background task: {e}")

    return {"success": True, "message": f"အလုပ် {job_id} ကို တန်းစီထဲ ပြန်ထည့်ပြီးပါပြီ"}


@api_router.post("/admin/queue/{job_id}/cancel")
async def cancel_queued_job(
    job_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Cancel a currently queued or running job, refunding its charge.

    Refund semantics mirror the failure paths exactly (2026-10-31):
    voice jobs get their deducted seconds back through the idempotent
    ledger refund; recap jobs get their exact charged credits back
    (1 for a normal job, N for a segmented one) via the registry-based
    helper the worker failure path uses. Admin-owned jobs were never
    charged, so their refunds are no-ops.
    """
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in (
        JobStatus.QUEUED.value,
        JobStatus.RUNNING.value,
        JobStatus.WAITING_CHOICE.value,
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Only QUEUED, RUNNING, or WAITING_CHOICE jobs can be cancelled (this job is {job.status})",
        )

    is_recap = (job.job_type == JobType.RECAP.value or job.id.startswith("rc_") or job.engine == "recap")
    refund_info = {"refunded": False}

    if is_recap:
        # Batch L: the recap cancel machinery (row flip, registry
        # marker, refund incl. the registry-less persisted fallback)
        # lives in _cancel_recap_job_core, shared with the Mini App
        # user cancel so both surfaces behave identically.
        refund_info = await _cancel_recap_job_core(
            db,
            job,
            db_error_message="Cancelled by Administrator via Admin Console",
            registry_error_message="Cancelled by Administrator",
            conflict_status_code=400,
            conflict_detail="Only QUEUED, RUNNING, or WAITING_CHOICE jobs can be cancelled (job state changed concurrently)",
        )
    else:
        await _claim_job_cancelled(
            db,
            job,
            db_error_message="Cancelled by Administrator via Admin Console",
            conflict_status_code=400,
            conflict_detail="Only QUEUED, RUNNING, or WAITING_CHOICE jobs can be cancelled (job state changed concurrently)",
        )

        try:
            from app.services.ledger import refund_job_seconds
            before = (await db.execute(
                select(MinuteLedgerEvent).where(
                    MinuteLedgerEvent.user_id == job.user_id,
                    MinuteLedgerEvent.reference_id == f"REFUND:{job_id}",
                )
            )).first()
            await refund_job_seconds(db, job.user_id, str(job.id), note="Refund: job cancelled by admin")
            await db.commit()
            if not before:
                refund_info = {"refunded": True, "voice_seconds": "ledger debits for this job (if any)"}
        except Exception as exc:
            logger.warning(f"Cancel refund failed for job {job_id}: {exc}")
            refund_info = {"refunded": False, "error": "refund failed — check the user's ledger manually"}

    db.add(AuditLog(
        admin_id=admin.id,
        action="job.cancel",
        target_type="job",
        target_id=str(job_id),
        details_json=json.dumps({"refund": refund_info, "job_type": "recap" if is_recap else "voice"}),
    ))
    await db.commit()
    return {"success": True, "message": f"အလုပ် {job_id} ကို ပယ်ဖျက်ပြီးပါပြီ", "refund": refund_info}


class AdminSettingsPayload(BaseModel):
    payment_kpay_number: Optional[str] = None
    payment_kpay_name: Optional[str] = None
    payment_wave_number: Optional[str] = None
    payment_wave_name: Optional[str] = None
    tutorial_video_link: Optional[str] = None
    tutorial_video_enabled: Optional[bool] = None
    tutorial_voice_title: Optional[str] = None
    tutorial_voice_link: Optional[str] = None
    tutorial_voice_enabled: Optional[bool] = None
    tutorial_recap_title: Optional[str] = None
    tutorial_recap_link: Optional[str] = None
    tutorial_recap_enabled: Optional[bool] = None
    # Batch L: recap demo sample reference — a Telegram file_id OR a
    # URL. Deliberately NOT named *_link: the settings validator
    # forces every *_link value to be a t.me URL, which would reject
    # a file_id outright.
    recap_demo_asset: Optional[str] = None
    tutorial_index_link: Optional[str] = None
    tutorial_plans_link: Optional[str] = None
    tutorial_customvoice_link: Optional[str] = None
    tutorial_downloader_link: Optional[str] = None
    tutorial_scriptedit_link: Optional[str] = None
    tutorial_recapadvanced_link: Optional[str] = None
    tutorial_refer_link: Optional[str] = None
    tutorial_support_link: Optional[str] = None
    maintenance_mode: Optional[bool] = None
    # Batch Z: announcement text (e.g. reopening time) the Group AI
    # quotes verbatim while maintenance mode is on.
    maintenance_note: Optional[str] = None
    group_ai_admin_enabled: Optional[bool] = None
    # Final-audit batch M: the group-AI anti-spam limits join the
    # payload — they were registered dynamic settings read per
    # message, but no admin surface could write them.
    group_ai_user_cooldown_seconds: Optional[int] = None
    group_ai_hourly_budget: Optional[int] = None
    group_ai_daily_budget: Optional[int] = None
    # Batch BX: the bot private-chat AI joins the payload beside its
    # group sibling (registered dynamic settings read per message).
    private_ai_enabled: Optional[bool] = None
    private_ai_user_cooldown_seconds: Optional[int] = None
    private_ai_hourly_budget: Optional[int] = None
    private_ai_daily_budget: Optional[int] = None
    character_limit_free: Optional[int] = None
    character_limit_purchased: Optional[int] = None
    free_monthly_allowance_seconds: Optional[int] = None
    free_daily_job_limit: Optional[int] = None
    voice_daily_limit_minutes_free: Optional[int] = None
    voice_daily_limit_minutes_paid: Optional[int] = None
    tts_daily_limit_minutes_free: Optional[int] = None
    tts_daily_limit_minutes_paid: Optional[int] = None
    # Batch CG: FREE Gemini taste (minutes/day per user) + the global
    # FREE daily pool-request budget.
    free_gemini_taste_minutes: Optional[int] = None
    free_gemini_daily_budget_requests: Optional[int] = None
    # Batch AO: tiered recap pricing arming switch (ships dark; the
    # owner flips it only at the announced effective date).
    tiered_recap_pricing_enabled: Optional[bool] = None
    recap_free_max_duration_seconds: Optional[int] = None
    recap_max_duration_seconds: Optional[int] = None
    required_channel_id: Optional[str] = None
    required_group_id: Optional[str] = None
    channel_invite_link: Optional[str] = None
    group_invite_link: Optional[str] = None
    gemini_primary_model: Optional[str] = None
    gemini_fallback_1: Optional[str] = None
    gemini_fallback_2: Optional[str] = None
    gemini_fallback_3: Optional[str] = None
    gemini_tts_primary_model: Optional[str] = None
    gemini_tts_fallback_1: Optional[str] = None
    gemini_tts_lite_model: Optional[str] = None
    gemini_tts_fallback_2: Optional[str] = None
    voxcpm_timeout: Optional[int] = None
    # Batch BS: the Groq Whisper (speech-to-text) model joins the
    # payload — it was a registered dynamic setting with no admin
    # surface at all, and the transcribers read the env only, so the
    # setting could never take effect. Wired end to end in BS: this
    # payload + the GET prefill below + get_groq_whisper_model().
    groq_whisper_model: Optional[str] = None
    watermark_free_enabled: Optional[bool] = None
    watermark_paid_enabled: Optional[bool] = None
    watermark_style: Optional[str] = None
    watermark_position: Optional[str] = None
    watermark_opacity: Optional[int] = None
    watermark_size: Optional[int] = None


@api_router.get("/admin/settings")
async def get_admin_settings(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Get all dynamic platform settings."""
    from app.config.settings import get_groq_whisper_model, get_watermark_config
    from app.services.dynamic_settings import load_dynamic_settings
    await load_dynamic_settings(db)
    wm_cfg = get_watermark_config(False)
    return {
        "payment_kpay_number": getattr(settings, "payment_kpay_number", ""),
        "payment_kpay_name": getattr(settings, "payment_kpay_name", ""),
        "payment_wave_number": getattr(settings, "payment_wave_number", ""),
        "payment_wave_name": getattr(settings, "payment_wave_name", ""),
        "tutorial_video_link": getattr(settings, "tutorial_video_link", "https://t.me/VoiceClonePro/28"),
        "tutorial_video_enabled": getattr(settings, "tutorial_video_enabled", True),
        "tutorial_voice_title": getattr(settings, "tutorial_voice_title", "Voice Clone ပြုလုပ်နည်း လမ်းညွှန်"),
        "tutorial_voice_link": getattr(settings, "tutorial_voice_link", "https://t.me/VoiceClonePro/29"),
        "tutorial_voice_enabled": getattr(settings, "tutorial_voice_enabled", True),
        "tutorial_recap_title": getattr(settings, "tutorial_recap_title", "Movie Recap ပြုလုပ်နည်း လမ်းညွှန်"),
        "tutorial_recap_link": getattr(settings, "tutorial_recap_link", "https://t.me/VoiceClonePro/30"),
        "tutorial_recap_enabled": getattr(settings, "tutorial_recap_enabled", True),
        "recap_demo_asset": getattr(settings, "recap_demo_asset", ""),
        **{
            key: getattr(settings, key, default)
            for key, default in TUTORIAL_EXTRA_LINKS.items()
        },
        "maintenance_mode": getattr(settings, "maintenance_mode", False),
        "maintenance_note": str(getattr(settings, "maintenance_note", "") or ""),
        "group_ai_admin_enabled": getattr(settings, "group_ai_admin_enabled", False),
        "group_ai_user_cooldown_seconds": int(getattr(settings, "group_ai_user_cooldown_seconds", 60) or 0),
        "group_ai_hourly_budget": int(getattr(settings, "group_ai_hourly_budget", 40) or 40),
        "group_ai_daily_budget": int(getattr(settings, "group_ai_daily_budget", 300) or 300),
        # Batch BX: private-chat AI — the toggle defaults ON (owner
        # order), so the getattr default here is True, not False.
        "private_ai_enabled": bool(getattr(settings, "private_ai_enabled", True)),
        "private_ai_user_cooldown_seconds": int(getattr(settings, "private_ai_user_cooldown_seconds", 60) or 0),
        "private_ai_hourly_budget": int(getattr(settings, "private_ai_hourly_budget", 40) or 40),
        "private_ai_daily_budget": int(getattr(settings, "private_ai_daily_budget", 300) or 300),
        "character_limit_free": int(getattr(settings, "character_limit_free", 1000) or 1000),
        "character_limit_purchased": int(getattr(settings, "character_limit_purchased", 5000) or 5000),
        "free_monthly_allowance_seconds": int(getattr(settings, "free_monthly_allowance_seconds", 180)),
        "free_daily_job_limit": int(getattr(settings, "free_daily_job_limit", 5)),
        "voice_daily_limit_minutes_free": int(getattr(settings, "voice_daily_limit_minutes_free", 0)),
        "voice_daily_limit_minutes_paid": int(getattr(settings, "voice_daily_limit_minutes_paid", 30)),
        "free_gemini_taste_minutes": int(getattr(settings, "free_gemini_taste_minutes", 2)),
        "free_gemini_daily_budget_requests": int(getattr(settings, "free_gemini_daily_budget_requests", 100)),
        "tts_daily_limit_minutes_free": int(getattr(settings, "tts_daily_limit_minutes_free", 15)),
        "tts_daily_limit_minutes_paid": int(getattr(settings, "tts_daily_limit_minutes_paid", 60)),
        "tiered_recap_pricing_enabled": bool(getattr(settings, "tiered_recap_pricing_enabled", False)),
        "recap_free_max_duration_seconds": int(getattr(settings, "recap_free_max_duration_seconds", 120)),
        "recap_max_duration_seconds": int(getattr(settings, "recap_max_duration_seconds", 600)),
        "voxcpm_timeout": int(getattr(settings, "voxcpm_timeout", 60) or 60),
        "required_channel_id": str(getattr(settings, "required_channel_id", "") or ""),
        "required_group_id": str(getattr(settings, "required_group_id", "") or ""),
        "channel_invite_link": str(getattr(settings, "channel_invite_link", "https://t.me/voiceclonepro") or ""),
        "group_invite_link": str(getattr(settings, "group_invite_link", "https://t.me/voiceclonepro_chat") or ""),
        "gemini_primary_model": getattr(settings, "gemini_primary_model", "gemini-3.6-flash"),
        "gemini_fallback_1": getattr(settings, "gemini_fallback_1", "gemini-3.5-flash"),
        "gemini_fallback_2": getattr(settings, "gemini_fallback_2", "gemini-3.5-flash-lite"),
        "gemini_fallback_3": getattr(settings, "gemini_fallback_3", "gemini-flash-lite-latest"),
        "gemini_tts_primary_model": str(getattr(settings, "gemini_tts_primary_model", "gemini-3.8-flash-tts") or "gemini-3.8-flash-tts"),
        "gemini_tts_fallback_1": str(getattr(settings, "gemini_tts_fallback_1", "gemini-3.1-flash-tts-preview") or "gemini-3.1-flash-tts-preview"),
        "gemini_tts_lite_model": str(getattr(settings, "gemini_tts_lite_model", "gemini-3.8-flash-lite-tts") or "gemini-3.8-flash-lite-tts"),
        "gemini_tts_fallback_2": str(getattr(settings, "gemini_tts_fallback_2", "gemini-2.5-flash-preview-tts") or "gemini-2.5-flash-preview-tts"),
        # Batch BS: pre-filled with the CURRENT EFFECTIVE model
        # (dynamic setting > env > default, via the shared resolver
        # the transcribers use) so the field shows what transcription
        # will actually run with.
        "groq_whisper_model": get_groq_whisper_model(),
        "watermark_free_enabled": bool(wm_cfg["watermark_free_enabled"]),
        "watermark_paid_enabled": bool(wm_cfg["watermark_paid_enabled"]),
        "watermark_style": str(wm_cfg["watermark_style"]),
        "watermark_position": str(wm_cfg["watermark_position"]),
        "watermark_opacity": int(wm_cfg["watermark_opacity"]),
        "watermark_size": int(wm_cfg["watermark_size"]),
    }


# Settings-save validation (deep-hunt #4 B1): dynamic settings were
# stored verbatim, so one malformed tutorial link saved here later
# broke the bot's main menu for EVERY user (Telegram rejects a message
# whose inline keyboard carries a bad URL), and a payment number could
# be blanked, leaving invoices with no account to pay into. The update
# endpoint therefore validates the WHOLE payload before applying any
# of it: Telegram links must be real t.me links (the three shapes the
# product uses: channel/group name, name + post id, +invite hash) and
# payment numbers must be non-empty. The regex itself is the shared
# helper in app.services.url_safety -- the bot's tutorial-URL receivers
# validate against the very same pattern (hunt #5 parity).
from app.services.url_safety import TELEGRAM_LINK_RE as _TELEGRAM_LINK_RE
_PAYMENT_NUMBER_KEYS = ("payment_kpay_number", "payment_wave_number")


def _validate_admin_settings_updates(updates: dict) -> dict:
    """Validate an admin-settings payload BEFORE anything is applied.

    Returns the updates with link values whitespace-stripped. Raises
    HTTPException(400) on the first invalid field — the caller applies
    nothing in that case (all-or-nothing), so a half-saved settings
    page can never leave the bot menu or invoices broken.
    """
    cleaned = dict(updates)
    for key, value in updates.items():
        if value is None:
            continue
        if key.endswith("_link"):
            link = str(value).strip()
            if not _TELEGRAM_LINK_RE.match(link):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"လင့်ခ် မမှန်ကန်ပါ ({key}) — Telegram လင့်ခ်သည် "
                        "https://t.me/… ပုံစံသာ ဖြစ်ရပါမည်။ "
                        "မည်သည့် ဆက်တင်မှ မပြောင်းလဲပါ။"
                    ),
                )
            cleaned[key] = link
        elif key in _PAYMENT_NUMBER_KEYS:
            if not str(value).strip():
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"ငွေလွှဲ နံပါတ် ({key}) ကို အလွတ် ထားလို့ မရပါ — "
                        "နံပါတ် ထည့်ပေးပါ။ မည်သည့် ဆက်တင်မှ မပြောင်းလဲပါ။"
                    ),
                )
        elif key == "character_limit_free":
            # Round-6 P5: the free character limit became admin-visible;
            # keep it inside sane bounds so a typo can neither brick the
            # free tier (0) nor silently hand out the farm (huge).
            try:
                limit_val = int(value)
            except (TypeError, ValueError):
                limit_val = -1
            if limit_val < 100 or limit_val > 10000:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "အခမဲ့ စာလုံးရေ ကန့်သတ်ချက် (character_limit_free) သည် "
                        "100 မှ 10000 အတွင်း ဖြစ်ရပါမည်။ "
                        "မည်သည့် ဆက်တင်မှ မပြောင်းလဲပါ။"
                    ),
                )
            cleaned[key] = limit_val
        elif key == "character_limit_purchased":
            # Mini App Admin settings fill (2026-11-02): the purchased
            # character limit joins the payload next to its free sibling
            # (before this it silently no-opped — the key was not on the
            # payload model at all); keep the same sane bounds so a typo
            # can neither brick the paid tier nor hand out the farm.
            try:
                limit_val = int(value)
            except (TypeError, ValueError):
                limit_val = -1
            if limit_val < 100 or limit_val > 10000:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "ဝယ်ယူ စာလုံးရေ ကန့်သတ်ချက် (character_limit_purchased) သည် "
                        "100 မှ 10000 အတွင်း ဖြစ်ရပါမည်။ "
                        "မည်သည့် ဆက်တင်မှ မပြောင်းလဲပါ။"
                    ),
                )
            cleaned[key] = limit_val
        elif key == "group_ai_user_cooldown_seconds":
            # Final-audit batch M: 0 is a real choice (no per-user
            # cooldown); the cap keeps a typo from parking users for
            # a year. get_limits reads this back as a non-negative int.
            try:
                limit_val = int(value)
            except (TypeError, ValueError):
                limit_val = -1
            if limit_val < 0 or limit_val > 86400:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Group AI အသုံးပြုသူ Cooldown (group_ai_user_cooldown_seconds) သည် "
                        "0 မှ 86400 စက္ကန့် အတွင်း ဖြစ်ရပါမည်။ "
                        "မည်သည့် ဆက်တင်မှ မပြောင်းလဲပါ။"
                    ),
                )
            cleaned[key] = limit_val
        elif key == "private_ai_user_cooldown_seconds":
            # Batch BX: same bounds discipline as the group sibling —
            # 0 is a real choice (no per-user cooldown); the cap keeps
            # a typo from parking users for a year.
            try:
                limit_val = int(value)
            except (TypeError, ValueError):
                limit_val = -1
            if limit_val < 0 or limit_val > 86400:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Private AI အသုံးပြုသူ Cooldown (private_ai_user_cooldown_seconds) သည် "
                        "0 မှ 86400 စက္ကန့် အတွင်း ဖြစ်ရပါမည်။ "
                        "မည်သည့် ဆက်တင်မှ မပြောင်းလဲပါ။"
                    ),
                )
            cleaned[key] = limit_val
        elif key == "maintenance_note":
            # Batch Z: free-text announcement the Group AI quotes
            # verbatim from its fact sheet while maintenance is on
            # (e.g. the reopening time). Whitespace-stripped and
            # length-capped so the sheet stays bounded; empty is a
            # real choice (no announced detail for the AI to state).
            note = str(value).strip()
            if len(note) > 200:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Maintenance မှတ်စု (maintenance_note) သည် "
                        "စာလုံး 200 ထက် မရှည်ရပါ။ "
                        "မည်သည့် ဆက်တင်မှ မပြောင်းလဲပါ။"
                    ),
                )
            cleaned[key] = note
        elif key == "groq_whisper_model":
            # Batch BS: a model name is a short token; whitespace is
            # stripped and the length capped like maintenance_note.
            # Empty is a real choice — the resolver then falls back
            # to the GROQ_WHISPER_MODEL env / built-in default.
            model = str(value).strip()
            if len(model) > 200:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Groq Whisper Model (groq_whisper_model) သည် "
                        "စာလုံး 200 ထက် မရှည်ရပါ။ "
                        "မည်သည့် ဆက်တင်မှ မပြောင်းလဲပါ။"
                    ),
                )
            cleaned[key] = model
        elif key in (
            "gemini_tts_primary_model",
            "gemini_tts_fallback_1",
            "gemini_tts_lite_model",
            "gemini_tts_fallback_2",
        ):
            model = str(value).strip()
            if len(model) > 200:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"TTS Model ({key}) သည် စာလုံး 200 ထက် မရှည်ရပါ။ "
                        "မည်သည့် ဆက်တင်မှ မပြောင်းလဲပါ။"
                    ),
                )
            if key == "gemini_tts_primary_model" and not model:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "TTS Primary Model (gemini_tts_primary_model) ကို အလွတ် ထားလို့ မရပါ။ "
                        "မည်သည့် ဆက်တင်မှ မပြောင်းလဲပါ။"
                    ),
                )
            cleaned[key] = model
        elif key in ("group_ai_hourly_budget", "group_ai_daily_budget"):
            # Final-audit batch M: budgets gate real AI spend, so 0
            # (which would silently disable group answers) is rejected
            # here even though the reader would fall back to defaults.
            try:
                limit_val = int(value)
            except (TypeError, ValueError):
                limit_val = -1
            if limit_val < 1 or limit_val > 100000:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Group AI Budget ({key}) သည် "
                        "1 မှ 100000 အတွင်း ဖြစ်ရပါမည်။ "
                        "မည်သည့် ဆက်တင်မှ မပြောင်းလဲပါ။"
                    ),
                )
            cleaned[key] = limit_val
        elif key in ("private_ai_hourly_budget", "private_ai_daily_budget"):
            # Batch BX: same rule as the group budgets — 0 would
            # silently disable private answers, so it is rejected
            # here even though the reader would fall back to defaults.
            try:
                limit_val = int(value)
            except (TypeError, ValueError):
                limit_val = -1
            if limit_val < 1 or limit_val > 100000:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Private AI Budget ({key}) သည် "
                        "1 မှ 100000 အတွင်း ဖြစ်ရပါမည်။ "
                        "မည်သည့် ဆက်တင်မှ မပြောင်းလဲပါ။"
                    ),
                )
            cleaned[key] = limit_val
        elif key in (
            "free_monthly_allowance_seconds",
            "free_daily_job_limit",
            "voice_daily_limit_minutes_free",
            "voice_daily_limit_minutes_paid",
            "tts_daily_limit_minutes_free",
            "tts_daily_limit_minutes_paid",
            "free_gemini_taste_minutes",
            "free_gemini_daily_budget_requests",
            "recap_free_max_duration_seconds",
            "recap_max_duration_seconds",
            "voxcpm_timeout",
        ):
            # Batch AJ M6: the money-adjacent settings were stored
            # verbatim — a free_monthly_allowance_seconds of -1 wrapped
            # the free allowance to an absurd value, and zero/negative
            # recap durations bricked or unbound the recap tiers.
            _bounds = {
                "free_monthly_allowance_seconds": (0, 31_536_000),
                "free_daily_job_limit": (0, 1000),
                "voice_daily_limit_minutes_free": (0, 1440),
                "voice_daily_limit_minutes_paid": (0, 1440),
                "tts_daily_limit_minutes_free": (0, 1440),
                "tts_daily_limit_minutes_paid": (0, 1440),
                "free_gemini_taste_minutes": (0, 1440),
                "free_gemini_daily_budget_requests": (0, 100000),
                "recap_free_max_duration_seconds": (0, 86400),
                "recap_max_duration_seconds": (60, 86400),
                "voxcpm_timeout": (10, 600),
            }
            lo, hi = _bounds[key]
            try:
                limit_val = int(value)
            except (TypeError, ValueError):
                limit_val = lo - 1
            if limit_val < lo or limit_val > hi:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"ဆက်တင် ({key}) သည် {lo} မှ {hi} အတွင်း ဖြစ်ရပါမည်။ "
                        "မည်သည့် ဆက်တင်မှ မပြောင်းလဲပါ။"
                    ),
                )
            cleaned[key] = limit_val
        elif key == "watermark_style":
            style = str(value).strip().lower()
            if style not in ("tiled", "logo", "text"):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Watermark style (watermark_style) သည် tiled, logo သို့မဟုတ် text ဖြစ်ရပါမည်။ "
                        "မည်သည့် ဆက်တင်မှ မပြောင်းလဲပါ။"
                    ),
                )
            cleaned[key] = style
        elif key == "watermark_position":
            pos = str(value).strip().lower()
            if pos == "center":
                pos = "centre"
            if pos not in ("bottom-right", "bottom-left", "top-right", "top-left", "centre"):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Watermark position (watermark_position) မမှန်ကန်ပါ။ "
                        "မည်သည့် ဆက်တင်မှ မပြောင်းလဲပါ။"
                    ),
                )
            cleaned[key] = pos
        elif key == "watermark_opacity":
            try:
                op_val = int(value)
            except (TypeError, ValueError):
                op_val = -1
            if op_val < 20 or op_val > 100:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Watermark opacity (watermark_opacity) သည် 20 မှ 100 အတွင်း ဖြစ်ရပါမည်။ "
                        "မည်သည့် ဆက်တင်မှ မပြောင်းလဲပါ။"
                    ),
                )
            cleaned[key] = op_val
        elif key == "watermark_size":
            try:
                sz_val = int(value)
            except (TypeError, ValueError):
                sz_val = -1
            if sz_val < 5 or sz_val > 40:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Watermark size (watermark_size) သည် 5 မှ 40 အတွင်း ဖြစ်ရပါမည်။ "
                        "မည်သည့် ဆက်တင်မှ မပြောင်းလဲပါ။"
                    ),
                )
            cleaned[key] = sz_val
        elif key in ("watermark_free_enabled", "watermark_paid_enabled"):
            cleaned[key] = bool(value)
    return cleaned


@api_router.post("/admin/settings")
async def update_admin_settings(
    payload: AdminSettingsPayload,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Update dynamic platform settings directly from Admin Console."""
    from app.services.dynamic_settings import set_dynamic_setting
    updates = payload.model_dump(exclude_unset=True)
    # All-or-nothing: validate every field before applying any of them.
    updates = _validate_admin_settings_updates(updates)
    for k, v in updates.items():
        if v is not None:
            await set_dynamic_setting(db, k, v, admin_id=admin.id)
    await db.commit()
    return {"success": True, "message": "ဆက်တင်များ အောင်မြင်စွာ သိမ်းပြီးပါပြီ", "updated": updates}


@api_router.get("/admin/pricing")
async def get_admin_pricing(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """List editable plan prices (admin only).

    Mirrors the bot's pricing manager exactly: prices live on PricingPlan
    rows (seeded defaults) and edits go through the same audited service.
    Catalogue metadata (category/minutes/credits) is attached for display.
    """
    from app.services.payments import get_active_pricing_plans, get_plan_by_code

    plans = await get_active_pricing_plans(db)
    out = []
    for p in plans:
        cat = get_plan_by_code(p.code) or {}
        out.append({
            "code": p.code,
            "category": cat.get("category", "voice"),
            "minutes": p.minutes,
            "recap_credits": cat.get("recap_credits", 0),
            "price_mmk": p.price_mmk,
            "is_active": bool(p.is_active),
            "sort_order": p.sort_order,
        })
    return {"plans": out, "count": len(out)}


class AdminPricingUpdateRequest(BaseModel):
    price_mmk: int


@api_router.post("/admin/pricing/{code}")
async def update_admin_pricing(
    code: str,
    req: AdminPricingUpdateRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Update one plan's price (admin only) via the audited pricing service.

    Bounds mirror the bot's price editor: 100 – 10,000,000 MMK.
    """
    from app.services.payments import update_pricing_plan_price

    price = int(req.price_mmk or 0)
    if price < 100 or price > 10_000_000:
        raise HTTPException(
            status_code=400,
            detail="Price must be between 100 and 10,000,000 MMK",
        )
    ok, plan, _msg = await update_pricing_plan_price(
        db, code, price, admin_id=admin.id
    )
    if not ok or plan is None:
        raise HTTPException(status_code=404, detail="Pricing plan not found")
    await db.commit()
    return {
        "success": True,
        "code": plan.code,
        "price_mmk": plan.price_mmk,
        "message": f"ဈေးနှုန်း ပြင်ပြီးပါပြီ: {plan.code} = {plan.price_mmk:,} MMK",
    }


@api_router.get("/admin/pending-slips")
async def get_pending_payment_slips(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """List all pending payment requests awaiting approval."""
    stmt = (
        select(PaymentRequest, User)
        .join(User, PaymentRequest.user_id == User.id)
        .where(PaymentRequest.status == PaymentStatus.PENDING.value)
        .order_by(PaymentRequest.created_at.desc())
    )
    res = await db.execute(stmt)
    rows = res.all()

    slips = []
    for req, u in rows:
        slips.append({
            "id": req.id,
            "user_id": u.id,
            "user_name": u.first_name or f"User {u.id}",
            "username": u.username,
            "package_code": req.package_code,
            "amount_mmk": req.amount_mmk,
            "minutes": req.minutes,
            "slip_url": req.slip_url,
            "created_at": req.created_at.strftime("%Y-%m-%d %H:%M") if req.created_at else "",
        })
    return slips


class ApproveSlipRequest(BaseModel):
    request_id: str


@api_router.post("/admin/approve-slip")
async def approve_payment_slip(
    req: ApproveSlipRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Approve a payment request: upgrade user to PURCHASED and credit balance.

    Money fix (2026-10-31): this handler previously credited by hand and
    called ledger.credit_purchased_minutes() with keyword arguments that
    function does not accept (payment_request_id= / admin_id=), so EVERY
    approval of a minutes-bearing plan raised TypeError before commit —
    the slip stayed PENDING and the buyer was never credited. It now
    goes through the SAME locked service the bot approval path uses
    (app.services.payments.approve_payment_request: row lock + PENDING
    check + idempotent crediting + audit log), so both admin surfaces
    behave identically and a double approval can never double-credit.
    """
    from app.services.payments import approve_payment_request, get_plan_by_code as _get_plan

    ok, p_req, _code = await approve_payment_request(db, req.request_id, admin_id=admin.id)
    if not ok or p_req is None:
        # Batch AJ M3: a live review claim held by ANOTHER admin is a
        # conflict, not "not found" — surface it as 409 so this admin
        # knows to wait instead of hunting for a vanished slip.
        if _code == "CLAIMED_BY_OTHER":
            raise HTTPException(
                status_code=409,
                detail="အခြား Admin တစ်ဦးက ဒီပြေစာကို စစ်ဆေးနေပါတယ်။ ခဏစောင့်ပေးပါ။",
            )
        raise HTTPException(status_code=404, detail="Pending payment request not found")
    await db.commit()

    _plan = _get_plan(p_req.package_code)
    recap_added = _plan["recap_credits"] if _plan else 0
    # Batch AJ M4: an edited approval grants the CORRECTED recap
    # credits — report what was actually granted.
    if getattr(p_req, "edited_recap_credits", None) is not None:
        recap_added = p_req.edited_recap_credits
    target_user = await db.get(User, p_req.user_id)

    # Instant Telegram notification to the approved user
    if settings.bot_token and p_req.user_id:
        try:
            from aiogram import Bot
            from aiogram.enums import ParseMode
            from app.ui.keyboards import main_menu_kb
            bot = Bot(token=settings.bot_token)
            try:
                # R40 deck A9: single source — the approval notice is
                # the bot locale string (dual minutes + credits, VIP
                # status), not a second hardcoded implementation.
                from app.services.i18n import t as _t

                user_notice = _t(
                    "payment_approved_user_notify",
                    target_user.language_code if target_user else "my",
                    minutes=p_req.minutes,
                    recap_credits=recap_added,
                )
                await bot.send_message(
                    chat_id=p_req.user_id,
                    text=user_notice,
                    reply_markup=main_menu_kb(lang=target_user.language_code if target_user else "my"),
                    parse_mode=ParseMode.HTML,
                )
                # Batch I: this Mini App approval also syncs every
                # admin's Telegram review copy (keyboards cleared +
                # status line appended), exactly like a Telegram-side
                # approval. The service already released any claim.
                try:
                    from app.services.payment_review import (
                        approved_status_line,
                        sync_admin_copies,
                    )

                    await sync_admin_copies(
                        bot, p_req, line=approved_status_line(admin.first_name, p_req)
                    )
                except Exception as sync_err:
                    logger.warning(f"Failed to sync admin copies for payment {p_req.id}: {sync_err}")
            finally:
                await bot.session.close()
        except Exception as notify_err:
            logger.warning(f"Failed to notify user {p_req.user_id} of approval: {notify_err}")

    return {"success": True, "message": f"အသုံးပြုသူ {p_req.user_id} အတွက် ငွေပေးချေမှု အတည်ပြုပြီးပါပြီ (+{p_req.minutes} မိနစ်, +{recap_added} Recap Credits)"}


class RejectSlipRequest(BaseModel):
    request_id: str
    reason: str = "ပြေစာ မမှန်ကန်ပါ"


@api_router.post("/admin/reject-slip")
async def reject_payment_slip(
    req: RejectSlipRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Reject a payment request.

    Parity with the approve path (2026-10-31): rejection also goes
    through the locked payments service the bot uses (row lock + PENDING
    check + audit log) instead of mutating the row by hand here.
    """
    from app.services.payments import reject_payment_request

    ok, p_req, _code = await reject_payment_request(
        db, req.request_id, admin_id=admin.id, reason=req.reason
    )
    if not ok or p_req is None:
        if _code == "CLAIMED_BY_OTHER":
            raise HTTPException(
                status_code=409,
                detail="အခြား Admin တစ်ဦးက ဒီပြေစာကို စစ်ဆေးနေပါတယ်။ ခဏစောင့်ပေးပါ။",
            )
        raise HTTPException(status_code=404, detail="Pending payment request not found")
    await db.commit()

    # Instant Telegram notification to the user about rejection
    if settings.bot_token and p_req.user_id:
        try:
            from aiogram import Bot
            from aiogram.enums import ParseMode
            bot = Bot(token=settings.bot_token)
            try:
                reject_notice = (
                    f"⚠️ <b>ငွေလွှဲပြေစာ အတည်မပြုနိုင်ကြောင်း အသိပေးချက်</b>\n\n"
                    f"သင် တင်သွင်းခဲ့သော ငွေလွှဲပြေစာ (ID: <code>{p_req.id}</code>) အား စစ်ဆေးချက်အရ အတည်မပြုနိုင်သေးပါခင်ဗျာ။\n\n"
                    f"📌 <b>အကြောင်းပြချက်:</b> <i>{html.escape(req.reason)}</i>\n\n"
                    f"💡 <i>အချက်အလက် မှန်ကန်စွာဖြင့် ထပ်မံတင်သွင်းနိုင်သလို အခက်အခဲရှိပါက Support သို့ ဆက်သွယ်မေးမြန်းနိုင်ပါသည်။</i>"
                )
                await bot.send_message(
                    chat_id=p_req.user_id,
                    text=reject_notice,
                    parse_mode=ParseMode.HTML,
                )
                # Batch I: sync the Telegram review copies (see the
                # approve endpoint above).
                try:
                    from app.services.payment_review import (
                        rejected_status_line,
                        sync_admin_copies,
                    )

                    await sync_admin_copies(
                        bot, p_req, line=rejected_status_line(admin.first_name)
                    )
                except Exception as sync_err:
                    logger.warning(f"Failed to sync admin copies for payment {p_req.id}: {sync_err}")
            finally:
                await bot.session.close()
        except Exception as notify_err:
            logger.warning(f"Failed to notify user {p_req.user_id} of rejection: {notify_err}")

    return {"success": True, "message": f"ငွေပေးချေမှု တောင်းဆိုချက် {p_req.id} ကို ငြင်းပယ်ပြီးပါပြီ"}


class AdjustUserRequest(BaseModel):
    user_id: int
    account_category: Optional[str] = None
    # Sanity ceilings (hunt #4 B4): these were unbounded ints, so a
    # huge value overflowed the 32-bit Integer balance columns and
    # 500'd the adjust. The caps sit far above any legitimate manual
    # adjustment (±100k minutes ≈ 69 days of voice) while keeping
    # every derived seconds total comfortably inside Integer range.
    add_minutes: int = Field(0, ge=-100_000, le=100_000)
    add_seconds: int = Field(0, ge=-6_000_000, le=6_000_000)
    add_recap_credits: int = Field(0, ge=-100_000, le=100_000)
    reason: str = ""
    # Batch AJ M5: the Mini App sends one key per adjust-dialog intent;
    # a double-tap / client retry replays the SAME key and must not
    # apply the adjustment twice. None = legacy client (no dedupe).
    idempotency_key: Optional[str] = None


@api_router.post("/admin/adjust-user")
async def adjust_user_status(
    req: AdjustUserRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Admin manually modifies user tier and balances — audited.

    Production hardening (2026-10-31): every adjustment now REQUIRES a
    reason and goes through the same audited ledger path the bot admin
    uses (admin_adjust_balance — MinuteLedgerEvent + AuditLog +
    negative-balance protection), instead of silently mutating fields.
    Negative minute/second deltas are supported (clawbacks).
    """
    target_user = await db.get(User, req.user_id)
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    category_change = (
        req.account_category in (AccountCategory.FREE.value, AccountCategory.PURCHASED.value)
        and req.account_category != target_user.account_category
    )
    seconds_delta = int(req.add_minutes or 0) * 60 + int(req.add_seconds or 0)
    credits_delta = int(req.add_recap_credits or 0)
    if not category_change and seconds_delta == 0 and credits_delta == 0:
        raise HTTPException(status_code=400, detail="Nothing to adjust")
    reason = (req.reason or "").strip()
    if len(reason) < 3:
        raise HTTPException(status_code=400, detail="A reason is required for balance/tier adjustments (min 3 characters)")

    if seconds_delta != 0:
        from app.services.ledger import admin_adjust_balance
        from app.models.enums import LedgerDirection, LedgerSource
        try:
            await admin_adjust_balance(
                session=db,
                user_id=target_user.id,
                admin_id=admin.id,
                seconds=abs(seconds_delta),
                source=LedgerSource.PURCHASE,
                direction=LedgerDirection.CREDIT if seconds_delta > 0 else LedgerDirection.DEBIT,
                reason=reason,
                idempotency_key=req.idempotency_key or None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # Demote durability (owner hit this 2026-11-01: "Free ပြန်ချ Crd
    # နှုတ်တာမှာ VIP ကကျန်ခဲ့တယ်"): account_category is entitlement-derived
    # in the ledger — consume_seconds re-asserts PURCHASED on EVERY job
    # while purchased_seconds > 0, and a PURCHASE-source refund does the
    # same. Setting the field alone therefore does not stick: a demoted
    # user who still holds purchased seconds silently re-promotes to
    # VIP on their next job. Admin intent must win: on a PURCHASED ->
    # FREE change, drain the purchased bucket to zero through the same
    # audited ledger path (which itself flips the category to FREE at
    # exactly zero) BEFORE the category assignment below, so no
    # entitlement fuel remains. Ordering: this runs AFTER the
    # same-request seconds delta, so the zeroing targets the post-delta
    # balance — an explicit delta keeps its existing guard behaviour
    # (an over-large debit still 400s as before), and a same-request
    # credit is drained too, leaving the demote absolute. A zero
    # outstanding balance needs no ledger call (admin_adjust_balance
    # rejects non-positive seconds).
    purchased_seconds_zeroed = 0
    if category_change and req.account_category == AccountCategory.FREE.value:
        from app.services.ledger import admin_adjust_balance, get_minute_balance
        from app.models.enums import LedgerDirection, LedgerSource
        balance_now = await get_minute_balance(db, target_user.id)
        outstanding = int(balance_now.purchased_seconds or 0)
        if outstanding > 0:
            try:
                await admin_adjust_balance(
                    session=db,
                    user_id=target_user.id,
                    admin_id=admin.id,
                    seconds=outstanding,
                    source=LedgerSource.PURCHASE,
                    direction=LedgerDirection.DEBIT,
                    reason=reason,
                    idempotency_key=f"{req.idempotency_key}:drain" if req.idempotency_key else None,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            purchased_seconds_zeroed = outstanding

    if category_change:
        target_user.account_category = req.account_category

    recap_credits_applied = 0
    if credits_delta != 0:
        # Batch AJ M5: recap adjustments previously moved the balance
        # with NO recap-ledger event (admin adjusts were invisible in
        # the recap ledger), and the floor clamp silently applied less
        # than requested while the response/audit reported the FULL
        # delta. Now: dedupe by the client's idempotency key, record
        # the event with the ACTUALLY-applied (floor-clamped) delta,
        # and report that same applied delta.
        from app.services.ledger import record_recap_credit_event

        adj_ref = (
            f"ADMINADJ:{req.idempotency_key}" if req.idempotency_key
            else f"ADMINADJ:{uuid.uuid4().hex}"
        )
        prior_event = (await db.execute(
            select(RecapCreditLedgerEvent).where(
                RecapCreditLedgerEvent.reference_id == adj_ref
            )
        )).scalars().first()
        if prior_event is not None:
            recap_credits_applied = (
                int(prior_event.credits) if prior_event.direction == "CREDIT"
                else -int(prior_event.credits)
            )
        else:
            credits_before = int(target_user.recap_credits or 0)
            # Atomic floor-guarded update (hunt F5): same max(0, …)
            # semantics as before, but as a single conditional UPDATE so a
            # concurrent charge/refund can no longer be overwritten by a
            # stale read-modify-write.
            await db.execute(
                update(User)
                .where(User.id == target_user.id)
                .values(
                    recap_credits=case(
                        (User.recap_credits + credits_delta < 0, 0),
                        else_=User.recap_credits + credits_delta,
                    )
                )
            )
            await db.refresh(target_user)
            recap_credits_applied = int(target_user.recap_credits or 0) - credits_before
            if recap_credits_applied != 0:
                await record_recap_credit_event(
                    db,
                    user_id=target_user.id,
                    direction="CREDIT" if recap_credits_applied > 0 else "DEBIT",
                    credits=abs(recap_credits_applied),
                    reference_id=adj_ref,
                )

    db.add(AuditLog(
        admin_id=admin.id,
        action="user.adjust",
        target_type="user",
        target_id=str(target_user.id),
        details_json=json.dumps({
            "reason": reason,
            "via": "miniapp",
            "seconds_delta": seconds_delta,
            "recap_credits_delta": credits_delta,
            "recap_credits_applied": recap_credits_applied,
            "account_category": req.account_category if category_change else None,
            "purchased_seconds_zeroed": purchased_seconds_zeroed,
        }),
    ))
    await db.commit()
    return {
        "success": True,
        "message": f"အသုံးပြုသူ {req.user_id} ကို ပြင်ဆင်ပြီးပါပြီ",
        "purchased_seconds_zeroed": purchased_seconds_zeroed,
        "recap_credits_applied": recap_credits_applied,
    }


class BroadcastRequest(BaseModel):
    message: str


@api_router.post("/admin/broadcast")
async def send_admin_broadcast(
    req: BroadcastRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Trigger a broadcast message delivery to all registered bot users with comprehensive telemetry."""
    text = req.message.strip()
    if not text or len(text) < 4:
        raise HTTPException(
            status_code=400,
            detail="Broadcast message is too short. Minimum 4 characters required to prevent accidental sends.",
        )
    if len(text) > 4096:
        # Telegram rejects message text over 4096 chars; without this
        # check every per-user send failed silently and the campaign
        # "completed" with 0 delivered (hunt #3 B2). Reject up front,
        # before any delivery is attempted.
        raise HTTPException(
            status_code=400,
            detail="Broadcast စာသား ရှည်လွန်းပါတယ် — Telegram ကန့်သတ်ချက် (အက္ခရာ 4096 လုံး) ထက် ကျော်နေပါတယ်။ တိုအောင် ပြင်ပြီး ပြန်စမ်းကြည့်ပါ။",
        )

    # 1. Total users in database
    total_db_users = (await db.execute(select(func.count(User.id)))).scalar() or 0

    # 2. Targeted active users (bot_blocked users are send-suppressed
    # via their flag only — they are not admin-banned; hunt B1)
    stmt = select(User.id).where(User.status == "ACTIVE", User.bot_blocked.is_not(True))
    res = await db.execute(stmt)
    user_ids = res.scalars().all()
    db_blocked_count = total_db_users - len(user_ids)

    # Deliver via Telegram Bot. Batch AJ M2: with no bot token the
    # endpoint previously fell through to a {"success": True,
    # "queued"} return having sent NOTHING — it now raises 503 at
    # the end of the function instead (see below).
    if settings.bot_token:
        from aiogram import Bot
        from aiogram.exceptions import (
            TelegramForbiddenError,
            TelegramBadRequest,
            TelegramRetryAfter,
            TelegramAPIError,
        )

        # Batch AJ M2: plain text, exactly like the bot broadcast
        # path — the old HTML default parse mode let a stray "<" in
        # the admin's text break or silently reformat the message.
        bot = Bot(token=settings.bot_token)
        sent_count = 0
        blocked_by_user_count = 0
        deactivated_or_not_found_count = 0
        invalid_test_id_count = 0
        other_error_count = 0
        newly_blocked_ids = []
        # Batch AJ M2: per-recipient outcomes, persisted as
        # BroadcastDelivery rows below — the Mini App path previously
        # kept no delivery records at all (the bot path did).
        delivery_outcomes: dict[int, tuple[str, str | None]] = {}

        for uid in user_ids:
            # Detect legacy dummy/test IDs (< 8 digits)
            if len(str(uid)) < 8:
                invalid_test_id_count += 1
                delivery_outcomes[uid] = ("SKIPPED", "invalid test id")
                continue

            try:
                await bot.send_message(chat_id=uid, text=text)
                sent_count += 1
                delivery_outcomes[uid] = ("SENT", None)
                await asyncio.sleep(0.05)
            except TelegramForbiddenError:
                # User has blocked the bot
                blocked_by_user_count += 1
                newly_blocked_ids.append(uid)
                delivery_outcomes[uid] = ("FAILED", "blocked")
            except TelegramBadRequest:
                # Chat not found, user deactivated, or account deleted
                deactivated_or_not_found_count += 1
                delivery_outcomes[uid] = ("FAILED", "deactivated or not found")
            except TelegramRetryAfter as e:
                # Handle flood rate limits gracefully
                await asyncio.sleep(e.retry_after)
                try:
                    await bot.send_message(chat_id=uid, text=text)
                    sent_count += 1
                    delivery_outcomes[uid] = ("SENT", None)
                except TelegramForbiddenError:
                    blocked_by_user_count += 1
                    newly_blocked_ids.append(uid)
                    delivery_outcomes[uid] = ("FAILED", "blocked")
                except Exception:
                    other_error_count += 1
                    delivery_outcomes[uid] = ("FAILED", "error after retry")
            except Exception:
                other_error_count += 1
                delivery_outcomes[uid] = ("FAILED", "error")

        await bot.session.close()

        # Flag users who blocked the bot so subsequent broadcasts do not
        # re-attempt. This sets ONLY the bot_blocked send-suppression flag —
        # status stays untouched (status=BLOCKED is an admin ban; hunt B1).
        if newly_blocked_ids:
            await db.execute(
                update(User)
                .where(User.id.in_(newly_blocked_ids))
                .values(bot_blocked=True)
            )
            await db.commit()

        failed_count = len(user_ids) - sent_count

        # Batch AJ M2: persist the campaign + per-recipient delivery
        # records (the schema the bot broadcast path uses), so Mini
        # App broadcasts are auditable and counted the same way.
        from app.models.schema import BroadcastCampaign, BroadcastDelivery
        from app.models.enums import BroadcastCampaignStatus, BroadcastDeliveryStatus

        campaign = BroadcastCampaign(
            admin_id=admin.id,
            # Batch BS: the token must fit confirmation_token's
            # String(32) column — str(uuid4()) is 36 chars and made
            # this insert fail AFTER the whole delivery loop had run
            # (records rolled back, admin saw a 500 for a delivered
            # broadcast). The bot broadcast path's nonce expression
            # (handlers/admin.py, uuid4().hex[:12]) is mirrored here.
            confirmation_token=uuid.uuid4().hex[:12],
            message_text=text,
            status=BroadcastCampaignStatus.COMPLETED.value,
            total_recipients=len(user_ids),
            sent_count=sent_count,
            failed_count=failed_count,
            skipped_count=invalid_test_id_count,
            finished_at=utc_now(),
        )
        db.add(campaign)
        await db.flush()
        for uid, (d_status, d_err) in delivery_outcomes.items():
            db.add(BroadcastDelivery(
                campaign_id=campaign.id,
                user_id=uid,
                status=d_status,
                attempts=1,
                error_message=d_err,
                sent_at=utc_now() if d_status == BroadcastDeliveryStatus.SENT.value else None,
            ))

        # Log broadcast to audit log with granular delivery telemetry
        db.add(AuditLog(
            admin_id=admin.id,
            action="broadcast.send_webapp",
            target_type="broadcast_campaign",
            target_id=campaign.id,
            details_json=json.dumps({
                "snippet": text[:80],
                "total_db_users": total_db_users,
                "targeted": len(user_ids),
                "sent_count": sent_count,
                "failed_count": failed_count,
                "blocked_by_user": blocked_by_user_count,
                "deactivated_or_not_found": deactivated_or_not_found_count,
                "invalid_test_id": invalid_test_id_count,
                "pre_existing_db_blocked": db_blocked_count,
            }),
        ))
        await db.commit()

        return {
            "success": True,
            "campaign_id": campaign.id,
            "total_users_in_db": total_db_users,
            "total_targeted": len(user_ids),
            "sent_count": sent_count,
            "failed_count": failed_count,
            "breakdown": {
                "delivered": sent_count,
                "blocked_by_user": blocked_by_user_count,
                "deactivated_or_not_found": deactivated_or_not_found_count,
                "invalid_test_id": invalid_test_id_count,
                "other_errors": other_error_count,
                "pre_existing_blocked_in_db": db_blocked_count,
            },
        }

    # Batch AJ M2: no bot token → nothing was sent; say so (503)
    # instead of the old false {"success": True, "queued"}.
    raise HTTPException(
        status_code=503,
        detail="Bot token မရှိသေးသောကြောင့် Broadcast ပို့၍ မရပါ။",
    )
