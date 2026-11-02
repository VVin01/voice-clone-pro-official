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
import html
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, UploadFile, File, Form, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import case, select, func, update, or_, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import get_db_session
from app.models.enums import AccountCategory, JobStatus, UserStatus, VoiceModelStatus, VoiceModelType, PaymentStatus, JobType
from app.models.schema import User, Job, VoiceModel, MinuteBalance, MinuteLedgerEvent, PaymentRequest, AuditLog, utc_now
from app.services.i18n import t
from app.services.models import get_authorized_model, get_available_models_for_user
from app.services.myanmar_text import convert_zawgyi_to_unicode
from app.services.curated_voices import list_curated_voices, get_curated_voice
from app.webapp.auth import parse_and_verify_init_data

logger = logging.getLogger(__name__)
api_router = APIRouter(prefix="/api")


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
        user = User(
            id=user_id,
            first_name=telegram_user_data.get("first_name", "User"),
            username=telegram_user_data.get("username"),
            language_code=telegram_user_data.get("language_code", "my"),
            account_category=AccountCategory.FREE.value,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    else:
        user.first_name = telegram_user_data.get("first_name", user.first_name)
        user.username = telegram_user_data.get("username", user.username)
        await db.commit()

    return user


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
    """
    try:
        return await get_current_user(x_telegram_init_data, db)
    except HTTPException:
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
    # generations, slips and uploads. Admins are exempt (above);
    # reads stay open (this dependency only guards writes).
    if getattr(settings, "maintenance_mode", False):
        raise HTTPException(
            status_code=403,
            detail="MAINTENANCE_MODE: ဆာဗာ ပြုပြင်ထိန်းသိမ်းမှု ဆောင်ရွက်နေပါသည် — မကြာမီ ပြန်လည် အသုံးပြုနိုင်ပါမည်။",
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
    except Exception as exc:
        try:
            dest_path.unlink(missing_ok=True)
        except Exception:
            pass
        # Never leak the raw exception (paths/internals) to the client:
        # log it server-side, return a clean user-safe detail.
        logger.warning("Failed to save uploaded file %s: %s", dest_path, exc)
        raise HTTPException(status_code=500, detail="ဖိုင် သိမ်းဆည်းရာတွင် အဆင်မပြေ ဖြစ်သွားပါသည် — ခဏနေ ပြန်ကြိုးစားပါ")
    return total


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
    except Exception:
        pass
    return len(ids)


async def _refund_recap_credits(user_id: int, count: int) -> bool:
    """Refund `count` recap credits in its own session/commit.

    Separate from the job-status update so a status-write failure cannot
    silently swallow the refund, and idempotent at the call site: callers
    only invoke it once per failed job (see _run_recap_background_job,
    which marks the registry entry as refunded after a successful commit).
    Returns True when credits were actually added.
    """
    if count <= 0:
        return False
    if settings.is_admin(user_id):
        return False
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            # Atomic increment (hunt F5): no read-modify-write window in
            # which a concurrent charge could be overwritten.
            result = await session.execute(
                update(User)
                .where(User.id == user_id)
                .values(recap_credits=User.recap_credits + count)
            )
            if not result.rowcount:
                return False
            await session.commit()
            return True
    except Exception as exc:
        logger.warning(f"Recap credit refund failed for user {user_id}: {exc}")
        return False


async def _refund_recap_credit(user_id: int) -> bool:
    """Refund one recap credit (see _refund_recap_credits)."""
    return await _refund_recap_credits(user_id, 1)


async def _charge_recap_credits(user_id: int, count: int) -> bool:
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
            await session.commit()
            return True
    except Exception as exc:
        logger.warning(f"Recap credit charge failed for user {user_id}: {exc}")
        return False


class _RecapInsufficientCreditsError(Exception):
    """A segmented recap job whose exact per-part charge the user's
    balance cannot cover. Raised in the worker BEFORE any part renders;
    nothing beyond the submit-time credit has been taken, and the
    normal failure path refunds that, leaving the user exactly whole.
    """

    def __init__(self, total_parts: int):
        self.total_parts = int(total_parts)
        super().__init__(
            f"💳 Recap Credit မလုံလောက်ပါ — ဒီဗီဒီယိုရှည်ကို အပိုင်း {self.total_parts} ပိုင်း "
            f"ခွဲထုတ်ရမှာဖြစ်ပြီး အပိုင်းတစ်ခုစီ 1 Credit နှုန်းနဲ့ စုစုပေါင်း {self.total_parts} Credits "
            "လိုအပ်ပါတယ်။ Credit ထပ်ဖြည့်ပြီးမှ ပြန်စမ်းပါခင်ဗျာ။"
        )


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
    """
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
            job.recap_config = json.dumps(state, ensure_ascii=False)
            await session.commit()
    except Exception as exc:
        logger.warning(f"Could not persist recap charge state for {job_id}: {exc}")


def _recap_redispatch_plan(job, user, *, restore_credit_state: bool = True) -> dict:
    """Build the config, registry entry and run kwargs needed to
    (re-)dispatch a persisted recap job — shared by the server queue
    worker (post-restart pickup) and the admin retry endpoint.

    Jobs submitted after the 2026-10-31 persistence fix carry their
    full submit-time config + credit state on the Job row; those are
    restored verbatim. Legacy rows without persisted state fall back
    to the historical reconstruction (URL/title/voice from the row,
    defaults for everything else, no credit state).

    ``restore_credit_state=False`` is for admin retries of FAILED /
    CANCELLED jobs: their original charge was already refunded by the
    failure/cancel paths, so the retry runs uncharged — exactly how
    retries behaved before persistence existed.
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

    entry = {
        "id": job.id,
        "user_id": job.user_id,
        "title": clean_title,
        "status": "QUEUED",
        "progress_percent": 5,
        "progress_step": "0/4",
        "progress_label": "⏳ Picking up from queue...",
        "created_at": time.time(),
        "video_path": None,
        "audio_path": None,
        "srt_path": None,
        "script_path": None,
        "download_urls": {},
        "error_message": None,
        "script_review": bool(cfg.script_review),
        "segments_count": None,
        "split_video": bool(cfg.split_video),
        "parts": [],
        "split_error": None,
        "split_note": None,
        "credit_deducted": credit_deducted,
        "credits_charged": credits_charged,
        "credit_refunded": credit_refunded,
        "credits_refunded": credits_refunded,
        "credits_estimated": credits_estimated,
        "parts_count": None,
    }
    return {
        "cfg": cfg,
        "entry": entry,
        "is_vip": is_vip,
        "credit_deducted": credit_deducted,
        "phase": phase,
    }


async def _top_up_recap_segment_charge(user_id: int, entry: dict, total_parts: int) -> int:
    """Bring a segmented recap job's charge to 1 credit PER PART.

    Owner decision 2026-10-30: a >10-minute recap split into parts
    costs 1 credit per part, not 1 credit for the whole job. The first
    credit was already taken at submit (the normal recap charge); once
    the worker knows the exact part count N this charges the remaining
    N − already_charged in ONE ledger action, before any part renders.
    Raises _RecapInsufficientCreditsError — charging nothing extra —
    when the balance cannot cover the difference. Returns the job's
    total charged credits.
    """
    already = int(entry.get("credits_charged") or 0)
    extra = int(total_parts) - already
    if extra > 0 and not await _charge_recap_credits(user_id, extra):
        raise _RecapInsufficientCreditsError(total_parts)
    entry["credits_charged"] = int(total_parts)
    entry["credit_deducted"] = True
    entry["parts_count"] = int(total_parts)
    # Keep the Job row's persisted total in step, so a restart after
    # this top-up restores N (not the submit-time 1) and can neither
    # double-charge the parts nor lose track of the refund amount.
    await _persist_recap_credits_charged(entry)
    return int(total_parts)


async def _refund_recap_job_charge(user_id: int, entry: dict) -> bool:
    """Refund whatever a recap job was actually charged — 1 credit for
    a normal job, N credits for a segmented one — exactly once per job
    (the registry flag makes repeat failure handlers no-ops). Mirrors
    the normal failed-recap semantics: a failed job's full charge is
    returned, so the user is left exactly whole."""
    if entry.get("credit_refunded"):
        return False
    amount = int(entry.get("credits_charged") or 0)
    if amount <= 0 and entry.get("credit_deducted"):
        amount = 1
    if amount <= 0:
        return False
    if await _refund_recap_credits(user_id, amount):
        entry["credit_refunded"] = True
        entry["credits_refunded"] = amount
        # Persist the refund on the Job row too (hunt #5): if the
        # process restarts now, the re-dispatch plan restores the flag
        # and a second failure cannot refund the same charge again;
        # a registry-less admin cancel likewise sees it was refunded.
        await _persist_recap_credits_charged(entry)
        return True
    return False


def _voice_job_error_status(reason: str) -> int:
    return {
        "MODEL_NOT_AUTHORIZED": 404,
    "LEGACY_MODEL_ENGINE_REMOVED": 400,
        "CONCURRENT_JOB_LIMIT_EXCEEDED": 429,
        "DAILY_JOB_LIMIT_EXCEEDED": 429,
        "INSUFFICIENT_BALANCE": 403,
    }.get(reason, 400)


def _voice_job_error_detail(reason: str) -> str:
    return {
        "MODEL_NOT_AUTHORIZED": "အသံမော်ဒယ် မတွေ့ရှိပါ",
    "LEGACY_MODEL_ENGINE_REMOVED": "ဒီအသံ မော်ဒယ် အဟောင်းကို မသုံးနိုင်တော့ပါ။ VoxCPM2 နဲ့ ကိုးကားအသံ (၃-၃၀ စက္ကန့်) ပို့ပြီး ပြန်ပွားပေးပါ။",
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
    seconds_left = bal.total_usable_seconds if bal else 0
    is_admin = settings.is_admin(user.id)
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
                Job.created_at >= recent_cutoff,
                Job.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
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
    # complete in the bot (ledger.py: attribution, +30s rewards capped
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
        "recap_credits": getattr(user, "recap_credits", 3),
        "character_limit": char_limit,
        "recap_max_duration_seconds": 600 if is_vip else 120,
        "recap_max_duration_minutes": 10 if is_vip else 2,
        "has_recap_watermark": not is_vip,
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
    maintenance_raw = await get_dynamic_setting(
        db,
        "maintenance_mode",
        default=str(getattr(settings, "maintenance_mode", False)).lower(),
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
        "channel_invite_link": getattr(settings, "channel_invite_link", "https://t.me/VoiceCloneProChannel"),
        "group_invite_link": getattr(settings, "group_invite_link", "https://t.me/VoiceCloneProGroup"),
        # So the Mini App can show its maintenance state instead of
        # letting writes fail one by one (hunt C1).
        "maintenance_mode": str(maintenance_raw).lower() in ("true", "1", "yes"),
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
        if m.type != VoiceModelType.PERSONAL.value
        or m.owner_user_id == user.id
        or m.id in authorized_ids
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
    speed: str = Field("normal", max_length=20)
    gender_preset: str = Field("m2m", max_length=20)
    engine: str = Field("voxcpm", max_length=20)  # Job.engine String(20)
    preset_voice: Optional[str] = Field(None, max_length=50)  # Job.preset_voice String(50)


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
        raise HTTPException(status_code=400, detail="စာသား ရိုက်ထည့်ရန် လိုအပ်ပါသည်")

    _require_valid_preset_voice(req.preset_voice)

    is_vip = (user.account_category == AccountCategory.PURCHASED.value) or settings.is_admin(user.id)
    char_limit = settings.character_limit_purchased if is_vip else settings.character_limit_free

    if len(clean_text) > char_limit:
        raise HTTPException(
            status_code=400,
            detail=f"စာသား စာလုံးရေ ({len(clean_text)}) သည် ကန့်သတ်ချက် ({char_limit} လုံး) ထက် ကျော်လွန်နေပါသည်။ "
                   + ("အခမဲ့ (Free) အသုံးပြုသူများအတွက် တစ်ကြိမ်လျှင် စာလုံးရေ ၁၀၀၀ အထိသာ ကန့်သတ်ထားပါသည်။ စာလုံးရေ ၅၀၀၀ အထိ အသုံးပြုလိုပါက VIP သို့ အဆင့်မြှင့်တင်ပါခင်ဗျာ။" if not is_vip else ""),
        )

    # Determine engine & resolve authorized model
    is_vox = (
        req.engine == "voxcpm"
        or req.preset_voice is not None
        or "voxcpm" in (req.model_slug or "").lower()
    )
    if not is_vox:
        raise HTTPException(status_code=400, detail="ဒီအသံ မော်ဒယ် အဟောင်းကို မသုံးနိုင်တော့ပါ။ VoxCPM2 နဲ့ ကိုးကားအသံ (၃-၃၀ စက္ကန့်) ပို့ပြီး ပြန်ပွားပေးပါ။")
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
            raise HTTPException(status_code=404, detail="အသံမော်ဒယ် မတွေ့ရှိပါ")

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
            detail=_voice_job_error_detail(_reason),
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
    speed: str = Form("normal"),
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
        raise HTTPException(status_code=400, detail="စာသား ရိုက်ထည့်ရန် လိုအပ်ပါသည်")

    _require_valid_preset_voice(preset_voice)

    is_vip = (user.account_category == AccountCategory.PURCHASED.value) or settings.is_admin(user.id)
    char_limit = settings.character_limit_purchased if is_vip else settings.character_limit_free

    if len(clean_text) > char_limit:
        raise HTTPException(
            status_code=400,
            detail=f"စာသား စာလုံးရေ ({len(clean_text)}) သည် ကန့်သတ်ချက် ({char_limit} လုံး) ထက် ကျော်လွန်နေပါသည်။ "
                   + ("အခမဲ့ (Free) အသုံးပြုသူများအတွက် တစ်ကြိမ်လျှင် စာလုံးရေ ၁၀၀၀ အထိသာ ကန့်သတ်ထားပါသည်။ စာလုံးရေ ၅၀၀၀ အထိ အသုံးပြုလိုပါက VIP သို့ အဆင့်မြှင့်တင်ပါခင်ဗျာ။" if not is_vip else ""),
        )

    # Determine engine & resolve authorized model
    is_vox = (
        engine == "voxcpm"
        or preset_voice is not None
        or "voxcpm" in (model_slug or "").lower()
    )
    if not is_vox:
        raise HTTPException(status_code=400, detail="ဒီအသံ မော်ဒယ် အဟောင်းကို မသုံးနိုင်တော့ပါ။ VoxCPM2 နဲ့ ကိုးကားအသံ (၃-၃၀ စက္ကန့်) ပို့ပြီး ပြန်ပွားပေးပါ။")
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
            raise HTTPException(status_code=404, detail="အသံမော်ဒယ် မတွေ့ရှိပါ")

    ref_audio_path = None
    if reference_audio and reference_audio.filename:
        ref_dir = settings.storage_root / "temp" / "voxcpm_refs"
        ref_dir.mkdir(parents=True, exist_ok=True)
        ext = Path(reference_audio.filename).suffix or ".wav"
        clean_ext = ext if ext.lower() in (".wav", ".mp3", ".ogg", ".m4a") else ".wav"
        save_file = ref_dir / f"webapp_ref_{uuid.uuid4().hex[:12]}{clean_ext}"
        content = await reference_audio.read()
        if len(content) > 50 * 1024 * 1024:
            raise HTTPException(
                status_code=413,
                detail="အသံနမူနာဖိုင် အရွယ်အစား 50 MB ထက် ကျော်လွန်နေပါသည်",
            )
        if len(content) > 100:
            with open(save_file, "wb") as f:
                f.write(content)
            ref_audio_path = str(save_file)

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
            speed_preset=speed,
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
            detail=_voice_job_error_detail(_reason),
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

    return {
        "id": job.id,
        "status": job.status,
        "engine": job.engine,
        "fallback_used": job.engine == "edge_fallback",
        "is_waiting_laptop_worker": is_waiting_laptop,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "error_message": job.error_message,
        "output_audio_url": f"/api/audio/download/{job.id}" if job.status == JobStatus.SUCCESS.value else None,
    }


@api_router.get("/audio/stream/{job_id}")
@api_router.get("/audio/download/{job_id}")
async def download_job_audio(
    job_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """Stream or download the completed generated voice clone audio file."""
    job = await _require_job_owner(db, job_id, user)

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
        filename=f"voice_clone_{job.id[:8]}{ext or '.wav'}",
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
            headers={"Cache-Control": "public, max-age=86400"},
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
            headers={"Cache-Control": "public, max-age=86400"},
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
        raise HTTPException(status_code=400, detail="မှန်ကန်သော Video URL ထည့်သွင်းပေးပါခင်ဗျာ")

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
            "formats_available": ["MP4 (Full HD Video)", "MP3 (192kbps Audio)", "SRT (Subtitles)"],
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
            "formats_available": ["MP4 (Video)", "MP3 (Audio 192k)", "SRT (Subtitles)"],
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

    from app.services.downloader import MediaDownloader, extract_first_url, unshorten_url

    raw_url = req.url.strip()
    url = extract_first_url(raw_url) or raw_url
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="မှန်ကန်သော Video URL ထည့်သွင်းပေးပါခင်ဗျာ")
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
        detail = str(e) if str(e).startswith("⚠️") else "ဒေါင်းလုဒ် မအောင်မြင်ပါ။ လင့်ခ်ကို ပြန်စစ်ပြီး ထပ်ကြိုးစားပါခင်ဗျာ။"
        raise HTTPException(status_code=400, detail=detail)

    if not file_path or not Path(file_path).is_file():
        shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(status_code=400, detail="Download failed")

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
        raise HTTPException(status_code=404, detail="ဖိုင် အဆင်သင့်မဖြစ်သေးပါ သို့မဟုတ် ရှာမတွေ့ပါခင်ဗျာ။")
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
        return {"success": True, "message": "Telegram သို့ ဖိုင် ပို့ပြီးပါပြီခင်ဗျာ!"}
    except Exception as e:
        logger.warning(f"Failed to send downloader file {download_id} to {user.id}: {e}")
        raise HTTPException(status_code=500, detail="Telegram သို့ ပေးပို့ရာတွင် အမှားဖြစ်ပေါ်ပါသည်")
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
        raise HTTPException(status_code=400, detail="ဗီဒီယိုဖိုင် ရွေးချယ်ပေးပါခင်ဗျာ")

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
    UPLOADED_VIDEO_TITLES[str(saved_path)] = display_title
    UPLOADED_VIDEO_TITLES[saved_path.name] = display_title
    UPLOADED_VIDEO_OWNERS[str(saved_path)] = user.id
    UPLOADED_VIDEO_OWNERS[str(saved_path.resolve())] = user.id
    UPLOADED_VIDEO_OWNERS[saved_path.name] = user.id
    UPLOADED_VIDEO_OWNERS[thumb_path.name] = user.id

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
        "preview_ts": best_ts,
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
            f"🆔 Task: #{job_id[:8]}\n"
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
RECAP_TTS_ENGINES = frozenset({"voxcpm", "gemini"})
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
    zoom_factor: float = 1.06
    voice_speed: str = "+12%"         # +0%, +8%, +12%, +18%, +22%
    blur_subtitles: bool = True
    blur_y: int = Field(85, ge=0, le=100)
    blur_h: int = Field(7, ge=1, le=50)
    burn_subtitles: bool = True
    sub_y: int = Field(85, ge=0, le=100)
    sub_style: str = "yellow"         # yellow, white, boxed, neon
    sub_font_size: int = Field(16, ge=12, le=30)
    sub_lines: int = Field(1, ge=1, le=2)
    sub_font: str = "default"         # default, serif, padauk
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
    # - split_video: after a successful render, also cut the finished
    #   video into labelled parts (smart cut at narration pauses) —
    #   that post-split stays included in the job's credit. For an
    #   over-cap source, split_video instead routes the job through
    #   segmented processing, which is charged 1 credit PER PART
    #   (owner decision 2026-10-30; see _top_up_recap_segment_charge).
    split_video: bool = False
    split_minutes: int = Field(3, ge=1, le=10)

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
            raise ValueError(f"Unsupported tts_engine '{v}'. Use: voxcpm, gemini")
        return e

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
    "download": (5, 15),
    "transcribe": (15, 30),
    "context": (30, 42),
    "translate": (42, 54),
    "plan": (54, 64),
    "tts": (64, 82),
    "sync": (82, 95),
    "finalize": (95, 99),
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


async def _deliver_recap_parts(bot, chat_id: int, job_id: str, lang: str = "my") -> int:
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
                vf = FSInputFile(part["path"], filename=Path(part["path"]).name)
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
    audio_path: Optional[str],
    srt_path: Optional[str],
    script_path: Optional[str],
    title: str,
    style: str,
    voice_name: str,
    duration_sec: int,
    job_id: Optional[str] = None,
):
    """Deliver completed recap video and media files directly to user's Telegram."""
    if not settings.bot_token or not chat_id:
        return
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
                await _deliver_recap_parts(bot, chat_id, job_id, lang)
            elif file_size <= 50 * 1024 * 1024:
                vf = FSInputFile(video_path, filename=Path(video_path).name)
                await bot.send_video(chat_id=chat_id, video=vf, caption=caption, parse_mode="HTML")
            elif parts_available:
                # Full render exceeds Telegram's 50 MB bot limit, but the
                # job was split: the parts ARE the Telegram delivery.
                await _deliver_recap_parts(bot, chat_id, job_id, lang)
            else:
                from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
                app_url = getattr(settings, "mini_app_url", "https://voice-clone-studio-c1er.onrender.com")
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
                    jid=jid[:8],
                    size_formatted=size_formatted,
                )
                await bot.send_message(chat_id=chat_id, text=over_caption, reply_markup=kb, parse_mode="HTML")

            if srt_path and os.path.isfile(srt_path):
                sf = FSInputFile(srt_path, filename=f"subtitles_{Path(srt_path).name}")
                await bot.send_document(chat_id=chat_id, document=sf, caption=t("clone_caption_subtitles", lang), parse_mode="HTML")

            # Split jobs: every part is delivered too (when the full video
            # was oversized, or the job is segmented, the parts already
            # went out above as the main delivery — don't send twice).
            if parts_available and not is_segmented and file_size <= 50 * 1024 * 1024:
                await _deliver_recap_parts(bot, chat_id, job_id, lang)

        finally:
            await bot.session.close()
    except Exception as e:
        logger.warning(f"Failed to send recap video to Telegram chat {chat_id}: {e}")


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


async def _run_recap_background_job(
    job_id: str,
    req: RecapConfigRequest,
    user_id: int,
    chat_id: int,
    is_vip: bool = False,
    credit_deducted: bool = False,
    phase: str = "full",
    resume_source: Optional[str] = None,
):
    """Capped entry point for every recap execution (deep-hunt #4 D2).

    All dispatch sites (submit, script-render, admin retry, bot admin,
    the server queue worker's re-dispatch) call this name; the global
    semaphore guarantees at most 2 renders execute at once. A job that
    cannot get a slot yet waits inside this coroutine — its registry
    entry stays QUEUED and the queue worker's guard sees that status,
    so it is never re-dispatched, failed, or dropped while waiting.
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
):
    entry = RECAP_JOBS_REGISTRY.get(job_id)
    if not entry:
        return

    # Credit accounting for this job (older registry entries — e.g.
    # admin re-queues — may predate these keys): the amount actually
    # charged so far, which the failure paths refund in full.
    entry.setdefault("credit_deducted", bool(credit_deducted))
    entry.setdefault(
        "credits_charged",
        int(entry.get("credits_charged") or (1 if credit_deducted else 0)),
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
    entry["progress_percent"] = 15
    entry["progress_step"] = "1/4"
    _recap_set_entry_label(entry, "📥 Downloading source video from URL...", stage="download")

    try:
        from app.services.downloader import MediaDownloader
        from app.db.session import AsyncSessionLocal
        downloader = MediaDownloader()

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

        # Gemini TTS option (production-grade programme): the persona
        # voice above stays as the per-segment FALLBACK voice, so a
        # Gemini outage degrades to the persona engine line-by-line and
        # is reported via the same fallback transparency fields.
        gemini_voice = req.gemini_voice
        if req.tts_engine == "gemini":
            tts_engine = "gemini"
            if not gemini_voice:
                gemini_voice = "Kore" if is_female else "Charon"

        logo_pos_map = {
            "top-right": [0.82, 0.04],
            "top-left": [0.04, 0.04],
            "bottom-right": [0.82, 0.88],
            "bottom-left": [0.04, 0.88],
            "top-center": [0.42, 0.04],
        }
        logo_pos_coords = logo_pos_map.get(req.logo_pos, [0.82, 0.04])

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

        # Free tier watermark branding vs VIP clean output
        watermark_text = None
        if not is_vip:
            watermark_text = "✨ Voice Clone Pro - Recap Studio Ultra Suite တွင် ပြုလုပ်သည်"

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
                _m, _s = int(_dur // 60), int(_dur % 60)
                raise ValueError(
                    f"ဗီဒီယို ကြာချိန် ({_m:02d}:{_s:02d}) သည် {'VIP' if is_vip else 'အခမဲ့ (Free)'} အသုံးပြုသူ ကန့်သတ်ချက် (အများဆုံး {int(max_dur_limit // 60)} မိနစ်) ထက် ကျော်လွန်နေပါသည်။"
                    + (" 📝 Script review နဲ့ ၁၀ မိနစ်ကျော် အပိုင်းခွဲ တွဲသုံးလို့မရပါ — Script review ကို ပိတ်ပြီး ပြန်စမ်းပါခင်ဗျာ။" if getattr(req, "split_video", False) else "")
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
        if phase == "full" and req.split_video:
            if is_local_media(canonical_source_url):
                local_source = str(Path(canonical_source_url).resolve())
            else:
                local_source, _dl_info = await loop.run_in_executor(
                    None,
                    functools.partial(
                        downloader.download_video,
                        canonical_source_url,
                        target_dir=str(job_dir),
                        max_height=1080 if is_vip else 720,
                    ),
                )
            from app.services.downloader import probe_local_media

            _src_dur = float(
                probe_local_media(local_source).get("duration", 0.0) or 0.0
            )
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
                        _recap_set_entry_label(
                            entry,
                            f"💳 အပိုင်း {total_parts} ပိုင်း × 1 Credit = {total_parts} Credits "
                            "ကောက်ခံပြီးပါပြီ — အပိုင်းများ စတင်ထုတ်နေပါတယ်...",
                        )
            canonical_source_url = local_source

        def _make_recap_fn(source, target_dir):
            return functools.partial(
            downloader.generate_recap_video,
            url_or_path=source,
            target_dir=target_dir,
            voice=selected_voice,
            narrative_style=req.story_style,
            voice_speed=req.voice_speed,
            tts_engine=tts_engine,
            voxcpm_control=voxcpm_control,
            voxcpm_ref_audio=voxcpm_ref_audio,
            is_female=is_female,
            source_language=req.source_language,
            render_quality=req.render_quality,
            gemini_voice=gemini_voice,
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
            burn_subtitles=req.burn_subtitles,
            sub_y=float(req.sub_y) / 100.0,
            sub_lines=req.sub_lines,
            sub_font_size=req.sub_font_size,
            sub_style=req.sub_style,
            sub_font=req.sub_font,
            logo_position=logo_pos_coords if req.include_logo else None,
            extra_text=watermark_text,
            is_vip=is_vip,
            max_duration_seconds=max_dur_limit,
            progress_callback=progress_cb,
        )

        part_urls: list[dict] = []
        first_part_dest: Optional[str] = None
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
            for seg in segment_files:
                _recap_set_entry_label(
                    entry, f"🎬 အပိုင်း {seg['n']}/{total_segs} လုပ်နေပါတယ်..."
                )
                seg_res = await loop.run_in_executor(
                    None,
                    _make_recap_fn(seg["path"], str(job_dir / f"seg{seg['n']:02d}")),
                )
                seg_video = seg_res.get("final_video") or seg_res.get("final_video_path")
                if not seg_video or not Path(seg_video).is_file():
                    raise RuntimeError(
                        f"Segment {seg['n']}/{total_segs} render produced no video"
                    )
                dest = parts_dir / f"recap_part_{seg['n']:02d}.mp4"
                if Path(seg_video).resolve() != dest.resolve():
                    shutil.move(seg_video, dest)
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

        video_path = recap_res.get("final_video") or recap_res.get("final_video_path")
        if segmented_mode and first_part_dest:
            video_path = first_part_dest
        audio_path = recap_res.get("audio") or recap_res.get("final_audio_path")
        srt_path = recap_res.get("subtitles_srt")
        script_path = _build_clean_recap_script_file(job_dir, job_id, req.story_style, srt_path)

        _fb_used, _fb_segs = _recap_fallback_info(job_dir)
        entry["fallback_used"] = _fb_used
        entry["fallback_segments"] = _fb_segs

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
            entry["split_note"] = (
                f"မူရင်းဗီဒီယို ၁၀ မိနစ်ထက် ရှည်လို့ အပိုင်း {len(part_urls)} ပိုင်း "
                "ခွဲပြီး လုပ်ပေးထားပါတယ် — အပိုင်းအားလုံး ဒေါင်းလုဒ်ရယူနိုင်ပါတယ်။ "
                + (
                    f"(အပိုင်းတစ်ခုစီ 1 Credit နှုန်းနဲ့ စုစုပေါင်း {_seg_charged} Credits သုံးခဲ့ပါတယ်။)"
                    if _seg_charged > 0
                    # Admin segmented run (owner ruling 2026-11-02):
                    # never charged — the note must not claim a cost.
                    else "(Admin — Credit မကောက်ခံထားပါ။)"
                )
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
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    update(Job).where(Job.id == job_id).values(
                        status=JobStatus.SUCCESS.value,
                        result_path=video_path,
                        finished_at=func.now(),
                    )
                )
                await session.commit()
        except Exception as db_succ_err:
            logger.warning(f"Failed to update Job {job_id} to SUCCESS in DB: {db_succ_err}")

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
                app_url = getattr(settings, "mini_app_url", "https://voice-clone-studio-c1er.onrender.com")
                clean_title = _clean_recap_display_title(entry.get("title"), req.url)
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
                    ]
                ])
                notify_text = t(
                    "recap_notify_text",
                    _recap_notify_lang,
                    clean_title=html.escape(str(clean_title[:60])),
                    voice_slug=html.escape(str(req.voice_slug.capitalize())),
                    job_id=job_id[:8],
                )
                if entry.get("fallback_used"):
                    notify_text += t(
                        "recap_fallback_note",
                        _recap_notify_lang,
                        count=len(entry.get("fallback_segments") or []) or 1,
                    )
                bot_inst = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
                try:
                    await bot_inst.send_message(
                        chat_id=chat_id,
                        text=notify_text,
                        reply_markup=kb,
                    )
                finally:
                    await bot_inst.session.close()
            except Exception as notify_err:
                logger.warning(f"Could not send recap completion notification to user {chat_id}: {notify_err}")

        logger.info(f"Recap job {job_id} successfully finished. Notification sent to Telegram.")

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
        if credit_deducted and not entry.get("credit_refunded"):
            await _refund_recap_job_charge(user_id, entry)
        raise
    except Exception as e:
        logger.error(f"Background recap job {job_id} error: {e}", exc_info=True)
        entry["status"] = "FAILED"
        entry["error_message"] = str(e)
        try:
            from app.db.session import AsyncSessionLocal
            async with AsyncSessionLocal() as session:
                await session.execute(
                    update(Job).where(Job.id == job_id).values(
                        status=JobStatus.FAILED.value,
                        error_message=str(e),
                        finished_at=func.now(),
                    )
                )
                await session.commit()
        except Exception as ref_err:
            logger.warning(f"Recap job status update failed: {ref_err}")
        # Refund in its own commit, after the status update, so a
        # status-write failure cannot swallow the refund. The registry
        # flag makes a second failure handler for the same job a no-op.
        # The amount is whatever this job was actually charged: 1
        # credit for a normal job, N for a segmented one.
        if credit_deducted and not entry.get("credit_refunded"):
            await _refund_recap_job_charge(user_id, entry)

        if settings.bot_token and chat_id:
            try:
                from aiogram import Bot
                from aiogram.client.default import DefaultBotProperties
                from aiogram.enums import ParseMode
                bot_err = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
                try:
                    err_short = html.escape(str(e)[:180])
                    refund_note = f"✅ သင့် Recap Credit ({int(entry.get('credits_refunded') or 1)}c) ကို အကောင့်ထဲသို့ အလိုအလျောက် ပြန်လည်ဖြည့်သွင်းပေးထားပါသည်ခင်ဗျာ။" if credit_deducted else ""
                    await bot_err.send_message(
                        chat_id=chat_id,
                        text=(
                            f"⚠️ <b>Movie Recap ဖန်တီးမှု အဆင်မပြေဖြစ်သွားပါသည်</b>\n"
                            f"🆔 <b>Task:</b> <code>#{job_id[:8]}</code>\n"
                            f"📋 <b>အကြောင်းရင်း:</b> <i>{err_short}</i>\n\n"
                            f"{refund_note}\n"
                            f"💡 <i>လင့်ခ် အကာအကွယ်ရှိနေပါက ဗီဒီယိုဖိုင်ကို ဒေါင်းလုဒ်ဆွဲ၍ Mini App ရှိ '📁 Video Upload' မှ တိုက်ရိုက်တင်သွင်းကာ ပြုလုပ်နိုင်ပါသည်ခင်ဗျာ။</i>"
                        ),
                    )
                finally:
                    await bot_err.session.close()
            except Exception:
                pass


async def cleanup_expired_jobs(retention_days: Optional[int] = None) -> int:
    """
    Purge jobs and generated media files older than the media retention
    window (settings.media_retention_days, default 7 days) to free disk
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
            if not str(_resolved).startswith(str(_storage)):
                raise HTTPException(status_code=400, detail="Local video path is not allowed")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=400, detail="Local video path is not allowed")

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
            detail="ရွေးထားတဲ့ အသံ မမှန်ကန်ပါ။ ကျေးဇူးပြု၍ အသံ ပြန်ရွေးပေးပါခင်ဗျာ။",
        )

    is_vip = (user.account_category == AccountCategory.PURCHASED.value) or settings.is_admin(user.id)
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
            detail="Recap Video ဖန်တီးရန် Credit မလုံလောက်တော့ပါ။ ကျေးဇူးပြု၍ Movie Recap Plan ရယူပါရန်။",
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
            dur_mins = int(checked_duration) // 60
            dur_secs = int(checked_duration) % 60
            max_m = max_duration_allowed // 60
            tier_label = "VIP" if is_vip else "အခမဲ့ (Free)"
            if is_vip and req.split_video and req.script_review:
                hint = "📝 Script ကို အရင်စစ်မယ် ရွေးထားရင် ၁၀ မိနစ်အထိပဲ ရပါတယ် — အပိုင်းခွဲပြီး လုပ်ချင်ရင် Script review ကို ပိတ်ပြီး ပြန်စမ်းပါခင်ဗျာ။"
            elif is_vip:
                hint = "✂️ ၁၀ မိနစ်ထက် ရှည်သော ဗီဒီယိုများကို 'အပိုင်းခွဲ ထုတ်မယ်' ကို ဖွင့်ပြီး အပိုင်းလိုက် ထုတ်နိုင်ပါသည်ခင်ဗျာ။"
            else:
                hint = "ပိုရှည်သော ဗီဒီယိုများ (၁၀ မိနစ်အထိ) ပြုလုပ်လိုပါက VIP သို့ အဆင့်မြှင့်တင်ပါခင်ဗျာ။"
            raise HTTPException(
                status_code=400,
                detail=f"ဗီဒီယို ကြာချိန် ({dur_mins:02d}:{dur_secs:02d}) သည် {tier_label} အသုံးပြုသူ ကန့်သတ်ချက် (အများဆုံး {max_m} မိနစ်) ထက် ကျော်လွန်နေပါသည်။ " + hint,
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
        if not _recap_segment_balance_ok(
            is_admin=is_admin_user,
            balance=credits,
            estimated_parts=estimated_parts,
        ):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"💳 ဒီဗီဒီယိုရှည်ကို အပိုင်း {estimated_parts} ပိုင်း ခွဲထုတ်ရမှာဖြစ်ပြီး "
                    f"အပိုင်းတစ်ခုစီ 1 Credit နှုန်းနဲ့ စုစုပေါင်း {estimated_parts} Credits လိုအပ်ပါတယ်။ "
                    f"လက်ကျန် Credit ({credits}c) မလုံလောက်ပါ — Credit ထပ်ဖြည့်ပြီးမှ ပြန်စမ်းပါခင်ဗျာ။"
                ),
            )

    # Concurrency: at most settings.max_concurrent_jobs_per_user (default
    # 1, same as voice clones) active recap jobs per user; admins exempt.
    # Checked before any credit is deducted, so a rejected job costs nothing.
    if not settings.is_admin(user.id):
        if await _recap_active_job_count(db, user.id) >= max(1, settings.max_concurrent_jobs_per_user):
            raise HTTPException(
                status_code=429,
                detail="A recap job is already running for your account. Please wait for it to finish before starting another.",
            )

    # Deduct the submit credit: unconditional for non-admins. (It used
    # to be conditional on a positive balance, which is what let
    # 0-credit VIP users run free.) Admins are never charged.
    # Atomic conditional UPDATE (hunt F5): the gate above read the
    # balance in this session, but a concurrent submit in another
    # session could have taken the last credit in between — the WHERE
    # clause re-checks at write time, and 0 rows means the race was
    # lost, which is answered exactly like the gate's own rejection.
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
                detail="Recap Video ဖန်တီးရန် Credit မလုံလောက်တော့ပါ။ ကျေးဇူးပြု၍ Movie Recap Plan ရယူပါရန်။",
            )
        await db.commit()
        # The bulk UPDATE does not sync the in-memory instance; reload
        # so remaining_credits and the persisted job state are exact.
        await db.refresh(user)
        deducted_credit = True

    job_id = f"rc_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    clean_display_title = _clean_recap_display_title(req.title, req.url)
    RECAP_JOBS_REGISTRY[job_id] = {
        "id": job_id,
        "user_id": user.id,
        "title": clean_display_title,
        "status": "QUEUED",
        "progress_percent": 5,
        "progress_step": "0/4",
        "progress_label": "⏳ Cloud Queue ပြင်ဆင်နေပါသည်...",
        "created_at": time.time(),
        "video_path": None,
        "audio_path": None,
        "srt_path": None,
        "script_path": None,
        "download_urls": {},
        "error_message": None,
        "script_review": bool(req.script_review),
        "segments_count": None,
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
        "parts_count": None,
    }

    # Record Recap Job in database table for 24h history and tracking
    try:
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
            seconds_estimated=120,
            # Full config + credit state, so a restart/deploy
            # re-dispatch restores THIS job instead of a default one.
            recap_config=_persisted_recap_state(
                req,
                credit_deducted=deducted_credit,
                credits_charged=1 if deducted_credit else 0,
                is_vip=is_vip,
                phase="script" if req.script_review else "full",
                estimated_parts=estimated_parts,
            ),
        )
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
        if deducted_credit:
            await _refund_recap_credit(user.id)
        raise

    return {
        "success": True,
        "recap_id": job_id,
        "status": "QUEUED",
        "story_style": req.story_style,
        "ratio": req.ratio,
        "remaining_credits": getattr(user, "recap_credits", 0),
        "estimated_parts": estimated_parts,
        "credits_charged": 1 if deducted_credit else 0,
        "message": "AI Myanmar Recap ဖန်တီးမှု စတင်လုပ်ဆောင်နေပါပြီခင်ဗျာ!",
    }


@api_router.get("/recap/{job_id}/status")
async def get_recap_status(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """Poll recap job status, progress percentage, and deliverables."""
    entry = RECAP_JOBS_REGISTRY.get(job_id)
    if entry:
        if not _recap_registry_owner_ok(job_id, user):
            raise HTTPException(status_code=404, detail="Recap job not found")
        return {
            "id": job_id,
            "status": entry.get("status", "PENDING"),
            "progress_percent": entry.get("progress_percent", 0),
            "progress_step": entry.get("progress_step", ""),
            "progress_label": entry.get("progress_label", ""),
            "progress_stage": entry.get("progress_stage"),
            "progress_sub": entry.get("progress_sub", ""),
            "error_message": entry.get("error_message"),
            "fallback_used": bool(entry.get("fallback_used")),
            "fallback_segments": entry.get("fallback_segments", []),
            "download_urls": entry.get("download_urls", {}),
            "script_review": bool(entry.get("script_review")),
            "segments_count": entry.get("segments_count"),
            "parts": entry.get("parts", []),
            "split_error": entry.get("split_error"),
            "split_note": entry.get("split_note"),
            "segmented": bool(entry.get("segmented")),
            "parts_count": entry.get("parts_count") or (len(entry.get("parts") or []) or None),
            "credits_charged": entry.get("credits_charged"),
            "credits_estimated": entry.get("credits_estimated"),
        }

    job = await _require_job_owner(db, job_id, user)

    is_success = (job.status == JobStatus.SUCCESS.value)
    is_active = (job.status in (JobStatus.QUEUED.value, JobStatus.RUNNING.value))
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
    return {
        "id": job_id,
        "status": job.status,
        "fallback_used": _fb_used,
        "fallback_segments": _fb_segs,
        "progress_percent": 100 if is_success else (15 if is_active else 0),
        "progress_step": "4/4" if is_success else ("1/4" if is_active else ""),
        "progress_label": "Completed" if is_success else ("Processing in background..." if is_active else (job.error_message or "Failed")),
        "error_message": job.error_message,
        "parts": _db_parts,
        "segmented": len(_db_parts) >= 2,
        "parts_count": len(_db_parts) or None,
        "credits_charged": None,
        "is_expired": is_expired,
        "download_urls": {
            "video": f"/api/recap/download/{job_id}/video" if media_available else None,
            "audio": f"/api/recap/download/{job_id}/audio" if media_available else None,
            "srt": f"/api/recap/download/{job_id}/srt" if media_available else None,
            "script": f"/api/recap/download/{job_id}/script" if media_available else None,
            "parts": _db_parts,
        },
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
        raise HTTPException(status_code=400, detail="Script ပြင်ဆင်မှု မအောင်မြင်ပါ — အကြောင်းအရာကို ပြန်စစ်ပြီး ထပ်ကြိုးစားပါခင်ဗျာ။")

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
    entry["progress_percent"] = 15
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


@api_router.get("/recap/stream/{job_id}/{file_type}")
@api_router.get("/recap/download/{job_id}/{file_type}")
async def download_recap_file(
    job_id: str,
    file_type: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """Download or stream generated recap MP4 video, WAV/MP3 audio, SRT subtitles, or TXT script."""
    # Ownership first, before any file is resolved from disk: a registry
    # entry or a DB job must belong to the requester (or the requester
    # must be an admin). Files on disk with no job record are not served.
    if not _recap_registry_owner_ok(job_id, user):
        raise HTTPException(status_code=404, detail="Recap file not found")
    if job_id not in RECAP_JOBS_REGISTRY:
        await _require_job_owner(db, job_id, user)
    db_result_path = None
    if file_type == "video":
        try:
            from app.db.session import AsyncSessionLocal
            async with AsyncSessionLocal() as session:
                j = await session.get(Job, job_id)
                if j and j.result_path and Path(j.result_path).is_file():
                    db_result_path = j.result_path
        except Exception:
            pass

    file_path = resolve_recap_deliverable_path(job_id, file_type, result_path=db_result_path)
    if not file_path or not Path(file_path).is_file():
        raise HTTPException(status_code=404, detail=f"{file_type.capitalize()} file not ready or not found")

    actual_ext = Path(file_path).suffix.lower()
    audio_mime = "audio/wav" if actual_ext == ".wav" else "audio/mpeg"
    audio_ext = ".wav" if actual_ext == ".wav" else ".mp3"

    mime_map = {
        "video": ("video/mp4", f"recap_{job_id[:8]}.mp4"),
        "audio": (audio_mime, f"voiceover_{job_id[:8]}{audio_ext}"),
        "srt": ("text/plain; charset=utf-8", f"subtitles_{job_id[:8]}.srt"),
        "script": ("text/plain; charset=utf-8", f"script_{job_id[:8]}.txt"),
    }
    _part_dl = re.fullmatch(r"part([1-9][0-9]?)", file_type or "")
    if _part_dl:
        media_type, filename = "video/mp4", f"recap_part_{int(_part_dl.group(1)):02d}_{job_id[:8]}.mp4"
    else:
        media_type, filename = mime_map.get(file_type, ("application/octet-stream", f"{file_type}_{job_id[:8]}"))

    is_stream = "/stream/" in request.url.path or file_type in ["video", "audio"]
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
    """Deliver generated recap video and assets directly to user's Telegram chat on demand."""
    db_job = await db.get(Job, job_id)
    if not _recap_registry_owner_ok(job_id, user):
        raise HTTPException(status_code=404, detail="Recap job not found")
    if job_id not in RECAP_JOBS_REGISTRY:
        if not db_job or (db_job.user_id != user.id and not settings.is_admin(user.id)):
            raise HTTPException(status_code=404, detail="Recap job not found")
    db_res_path = db_job.result_path if db_job else None

    video_path = resolve_recap_deliverable_path(job_id, "video", result_path=db_res_path)
    audio_path = resolve_recap_deliverable_path(job_id, "audio")
    srt_path = resolve_recap_deliverable_path(job_id, "srt")
    script_path = resolve_recap_deliverable_path(job_id, "script")

    if not video_path or not Path(video_path).is_file():
        raise HTTPException(status_code=404, detail="ဗီဒီယို ဖိုင် အဆင်သင့်မဖြစ်သေးပါ သို့မဟုတ် ရှာမတွေ့ပါခင်ဗျာ။")

    entry = RECAP_JOBS_REGISTRY.get(job_id) or {}
    clean_title = _clean_recap_display_title(entry.get("title"), db_job.input_text if db_job else None)
    voice_label = (db_job.preset_voice.capitalize() if (db_job and db_job.preset_voice) else "Smart Sync AI")

    await _send_recap_telegram_delivery(
        chat_id=user.id,
        video_path=video_path,
        audio_path=audio_path,
        srt_path=srt_path,
        script_path=script_path,
        title=clean_title,
        style="Pro Dubbed",
        voice_name=voice_label,
        duration_sec=0,
        job_id=job_id,
    )
    return {"success": True, "message": "Telegram သို့ ဗီဒီယို ပို့ပြီးပါပြီခင်ဗျာ!"}


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
        raise HTTPException(status_code=404, detail="အသံဖိုင် အဆင်သင့်မဖြစ်သေးပါ သို့မဟုတ် ရှာမတွေ့ပါခင်ဗျာ။")

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
        af = FSInputFile(str(audio_path), filename=f"voice_{job_id[:8]}.wav")
        caption = (
            f"🎙️ <b>AI Myanmar Voice Clone</b>\n"
            f"✨ <i>Mini App မှ တောင်းဆိုချက်အရ သင့်ထံ ပေးပို့လိုက်ပါသည်ခင်ဗျာ။</i>"
        )
        await bot.send_audio(chat_id=user.id, audio=af, caption=caption, parse_mode="HTML")
        return {"success": True, "message": "Telegram သို့ အသံဖိုင် ပို့ပြီးပါပြီခင်ဗျာ!"}
    except Exception as e:
        logger.warning(f"Failed to send voice to Telegram chat {user.id}: {e}")
        raise HTTPException(status_code=500, detail="Telegram သို့ ပေးပို့ရာတွင် အမှားဖြစ်ပေါ်ပါသည်")
    finally:
        await bot.session.close()


# ── 5.1. Job History & Media Lifecycle Management ──────────────────────────
def _media_retention_days() -> int:
    """Generated-media retention window in days (single source of truth).

    Outputs are stored on the persistent disk and kept for this many days;
    the sweep, the history window and the user-facing copy all derive from
    this one value so they can never disagree.
    """
    try:
        days = int(getattr(settings, "media_retention_days", 7) or 7)
    except Exception:
        days = 7
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


@api_router.get("/history")
async def get_user_history(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """
    Get user's job generation history (both Voice Clone and Recap) within
    the media retention window (default 7 days). Older jobs are purged by
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
        time_ago = f"{mins_ago}m ago" if mins_ago < 60 else f"{mins_ago // 60}h ago"

        is_recap = (j.job_type == JobType.RECAP.value or j.id.startswith("rc_"))

        # Verify whether the output file actually exists on the persistent
        # disk — the single availability check every serializer shares.
        file_exists = False
        if j.status == JobStatus.SUCCESS.value:
            file_exists = check_job_file_exists(j.id, is_recap, j.result_path)
        is_expired = bool(j.status == JobStatus.SUCCESS.value and not file_exists)

        download_url, deliverables = _job_deliverables(j, is_recap, file_exists)
        if is_recap and download_url:
            # A live registry entry carries the job's own download URLs
            # (e.g. segmented parts); prefer them when present.
            recap_entry = RECAP_JOBS_REGISTRY.get(j.id)
            if recap_entry and recap_entry.get("download_urls"):
                deliverables = recap_entry["download_urls"]
                download_url = deliverables.get("video") or download_url

        raw_snippet = _clean_recap_display_title(j.input_text) if is_recap else (j.input_text or "")
        _hist_entry = RECAP_JOBS_REGISTRY.get(j.id) if is_recap else None
        _hist_parts = ((_hist_entry or {}).get("parts") or []) if is_recap else []
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
            else:
                _fb_used, _fb_segs = False, []
        else:
            _fb_used, _fb_segs = j.engine == "edge_fallback", []
        out.append({
            "id": j.id,
            "job_type": "RECAP" if is_recap else "VOICE_CLONE",
            "parts_count": (len(_hist_parts) or None) if is_recap else None,
            "credits_charged": (_hist_entry or {}).get("credits_charged") if is_recap else None,
            "engine": j.engine,
            "fallback_used": _fb_used,
            "fallback_segments": _fb_segs,
            "preset_voice": j.preset_voice or ("Thiha" if is_recap else "Standard"),
            "status": j.status,
            "input_snippet": (raw_snippet[:70] + "...") if raw_snippet and len(raw_snippet) > 70 else raw_snippet,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "time_ago": time_ago,
            "download_url": download_url,
            "deliverables": deliverables,
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


@api_router.get("/admin/history")
async def get_admin_global_history(
    timeframe: str = "24h",
    status: str = "all",
    job_type: str = "all",
    limit: int = 150,
    user_id: Optional[int] = None,
    job_id: Optional[str] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """
    Admin Only: Filter and view all users' generated jobs with custom timeframe & status filters.

    Optional narrowing (2026-10-31 admin rebuild): ``user_id`` restricts to a
    single user's jobs (per-user job history) and ``job_id`` matches a job ID
    prefix (the UI displays short 8-char IDs, so admins can search by those).
    Both are purely additive filters — existing callers are unaffected.
    """
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
        time_ago = f"{mins_ago}m ago" if mins_ago < 60 else f"{mins_ago // 60}h ago"

        # Verify whether the output file actually exists on the persistent
        # disk — the single availability check every serializer shares.
        file_exists = False
        if job.status == JobStatus.SUCCESS.value:
            file_exists = check_job_file_exists(job.id, is_recap, job.result_path)
        is_expired = bool(job.status == JobStatus.SUCCESS.value and not file_exists)

        download_url, deliverables = _job_deliverables(job, is_recap, file_exists)

        raw_snippet = _clean_recap_display_title(job.input_text) if is_recap else (job.input_text or "")
        out.append({
            "id": job.id,
            "user_id": job.user_id,
            "username": username or "Unknown",
            "first_name": first_name or "",
            "job_type": "RECAP" if is_recap else "VOICE_CLONE",
            "engine": job.engine,
            "preset_voice": job.preset_voice or ("Thiha" if is_recap else "Standard"),
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
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """Admin Only: Selectively purge jobs, physical media files, and DB records by criteria."""
    if not settings.is_admin(user.id):
        raise HTTPException(status_code=403, detail="Admin access required")

    now_utc = datetime.now(timezone.utc)
    stmt = select(Job)

    if req.mode == "single" and req.job_id:
        stmt = stmt.where(Job.id == req.job_id)
    elif req.mode == "failed":
        stmt = stmt.where(Job.status == JobStatus.FAILED.value)
    elif req.mode == "older_retention":
        # Beyond the media retention window (default 7 days) — the same
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
async def get_vip_plans(user: User = Depends(get_current_user)):
    """Return specialized high-value VIP subscription packages and payment accounts."""
    lang = user.language_code or "my"
    is_my = (lang == "my")

    return {
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
                "name": "🎙️ Voice Clone Studio Pro" if is_my else "🎙️ Voice Clone Studio Pro",
                "category": "voice",
                "allocation": "250 Usable Minutes (4 Hours+)" if is_my else "250 Usable Minutes (4 Hours+)",
                "minutes": 250,
                "recap_credits": 0,
                "price": "75,000",
                "currency": "MMK",
                "features": [
                    "VoxCPM Cloud AI မြန်မာအသံ ၁၂ မျိုး (၆ ကျား / ၆ မ) အပြည့်အဝ အသုံးပြုခွင့်" if is_my else "12 VoxCPM Cloud AI Personas (6M / 6F)",
                    "Custom Voice Clone (မိမိနှစ်သက်ရာ အသံဖိုင် တင်၍ အသံတူပွားယူခွင့်)" if is_my else "Custom Voice Clone (Audio Sample Upload)",
                    "VoxCPM2 Reference Audio (၃-၃၀ စက္ကန့်) နဲ့ အသံတူပွားခွင့်" if is_my else "VoxCPM2 Reference Audio Cloning (3-30s sample)",
                    "High-Speed Priority Cloud Processing Queue (စောင့်ဆိုင်းစရာမလို)" if is_my else "High-Speed Priority Cloud Processing Queue",
                    "အရည်အသွေးမြင့် 24kHz PCM Lossless WAV တိုက်ရိုက်ဒေါင်းလုဒ်" if is_my else "Direct 24kHz PCM Lossless WAV Audio Downloads",
                    "တစ်ကြိမ်လျှင် စာလုံးရေ ၅၀၀၀ အထိ အကန့်အသတ်မရှိ ထုတ်လုပ်နိုင်ခွင့်" if is_my else "5,000 Chars per Generation with Unlimited Daily Quota",
                ],
                "badge": "Voice Creators 🔥",
            },
            {
                "id": "recap_starter",
                "name": "🎬 Movie Recap Starter" if is_my else "🎬 Movie Recap Starter",
                "category": "recap",
                "allocation": "5 Movie Recap Credits (အများဆုံး ၁၀ မိနစ်/ကား)" if is_my else "5 Movie Recap Credits (Max 10 mins/video)",
                "minutes": 0,
                "recap_credits": 5,
                "price": "20,000",
                "currency": "MMK",
                "features": [
                    "Full HD 1080p Movie Recap ဗီဒီယို (၅) ကား (၁ ကား အများဆုံး ၁၀ မိနစ်)" if is_my else "5 Full HD 1080p Movie Recap Generations (Max 10 mins/video)",
                    "🎬 Smart Video Transform (Smart Blur, Auto-Mirror & Zoom)" if is_my else "Smart Video Transform (Smart Blur, Auto-Mirror & Zoom)",
                    "Watermark လုံးဝမပါ (Clean 1080p)" if is_my else "Zero Watermark (Clean 1080p)",
                ],
                "badge": "Recap Starter 🎬",
            },
            {
                "id": "recap_studio_pro",
                "name": "🎬 Movie Recap Studio Pro" if is_my else "🎬 Movie Recap Studio Pro",
                "category": "recap",
                "allocation": "25 Movie Recap Credits (အများဆုံး ၁၀ မိနစ်/ကား)" if is_my else "25 Movie Recap Credits (Max 10 mins/video)",
                "minutes": 0,
                "recap_credits": 25,
                "price": "90,000",
                "currency": "MMK",
                "features": [
                    "Full HD 1080p Movie Recap ဗီဒီယို (၂၅) ကား (၁ ကား အများဆုံး ၁၀ မိနစ်)" if is_my else "25 Full HD 1080p Movie Recap Generations (Max 10 mins/video)",
                    "🎬 Smart Video Transform (Smart Blur, Auto-Mirror & Zoom)" if is_my else "Smart Video Transform (Smart Blur, Auto-Mirror & Zoom)",
                    "ဇာတ်လမ်းစတိုင် ၅ မျိုး (Viral Hook, Dramatic, POV စသဖြင့်) & ရွှေရောင်စာတန်းထိုး ၄ မျိုး" if is_my else "5 Narration Styles (Viral Hook, Dramatic, POV) & 4 Subtitle Presets",
                    "Watermark လုံးဝမပါ (Clean 1080p) & Channel Logo သီးသန့်ထည့်ခွင့်" if is_my else "Zero Watermark (Clean 1080p) & Custom Channel Logo Branding",
                    "Multi-Threaded High-Speed Video Rendering Node" if is_my else "Multi-Threaded High-Speed Video Rendering Node",
                ],
                "badge": "Cinema Suite 🎬",
            },
            {
                "id": "combined_starter",
                "name": "🎙️🎬 Voice + Recap Starter" if is_my else "🎙️🎬 Voice + Recap Starter",
                "category": "combined",
                "allocation": "30 Mins + 5 Recap Credits (အများဆုံး ၁၀ မိနစ်/ကား)" if is_my else "30 Mins + 5 Recap Credits (Max 10 mins/video)",
                "minutes": 30,
                "recap_credits": 5,
                "price": "30,000",
                "currency": "MMK",
                "features": [
                    "Voice မိနစ် ၃၀ + Recap ဗီဒီယို ၅ ကား — နှစ်မျိုးလုံးကို တစ်ပြိုင်တည်း စမ်းသုံးနိုင်သည်" if is_my else "30 Voice Minutes + 5 Recap Videos — try both products in one plan",
                    "VoxCPM2 Reference Audio (၃-၃၀ စက္ကန့်) နဲ့ အသံတူပွားခွင့်" if is_my else "VoxCPM2 Reference Audio Cloning (3-30s sample)",
                    "🎬 Smart Video Transform & Watermark လုံးဝမပါ" if is_my else "Smart Video Transform & Zero Watermark",
                ],
                "badge": "Combined Starter ✨",
            },
            {
                "id": "all_in_one_master",
                "name": "👑 All-in-One Master Suite" if is_my else "👑 All-in-One Master Suite",
                "category": "combined",
                "allocation": "500 Mins + 60 Recap Credits (အများဆုံး ၁၀ မိနစ်/ကား)" if is_my else "500 Mins + 60 Recap Credits (Max 10 mins/video)",
                "minutes": 500,
                "recap_credits": 60,
                "price": "180,000",
                "currency": "MMK",
                "features": [
                    "Voice Clone Studio Pro အပြည့်အစုံ (မိနစ် ၅၀၀ - ၈ နာရီကျော် အသံသွင်းခွင့်)" if is_my else "Full Voice Clone Studio Pro (500 Mins - 8+ Hours)",
                    "Movie Recap Studio Pro အပြည့်အစုံ (ဗီဒီယို ၆၀ ကား - အများဆုံး ၁၀ မိနစ်/ကား)" if is_my else "Full Movie Recap Studio Pro (60 Full HD Videos - Max 10 mins/video)",
                    "👑 VoxCPM2 Reference Audio အသံတူပွားမှု (Custom Clone) အပြည့်အစုံ" if is_my else "Full VoxCPM2 Reference Audio Custom Cloning",
                    "🎬 Smart Video Transform Suite (Blur, Mirror, Zoom & Crop)" if is_my else "Smart Video Transform Suite (Blur, Mirror, Zoom & Crop)",
                    "Dedicated High-Speed Node Priority & 24/7 VIP Direct 1-on-1 Support" if is_my else "Dedicated High-Speed Cloud Node & 24/7 VIP Direct 1-on-1 Support",
                ],
                "badge": "Agency Master Suite 👑" if is_my else "Agency Master Suite 👑",
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
                detail="ဒီ ပြေစာပုံကို ပို့ထားပြီးသား ဖြစ်ပါတယ်။ စစ်ဆေးနေဆဲ ဖြစ်လို့ ထပ်မပို့ဘဲ ခဏ စောင့်ပေးပါ ခင်ဗျာ။",
            )

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
    )
    db.add(pay_req)
    await db.commit()

    # Instant alert to admins via Telegram
    if settings.bot_token and settings.admin_ids:
        try:
            from aiogram import Bot
            from aiogram.enums import ParseMode
            from aiogram.types import FSInputFile
            bot = Bot(token=settings.bot_token)
            try:
                admin_text = (
                    f"🔔 <b>New VIP Payment Slip Submitted!</b>\n\n"
                    f"👤 <b>User:</b> {html.escape(user.first_name or 'User')} (@{html.escape(user.username or 'none')})\n"
                    f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
                    f"📦 <b>Package:</b> <code>{package_code}</code>\n"
                    f"💰 <b>Amount:</b> <b>{amount_mmk:,} MMK</b>\n"
                    f"🧾 <b>Request ID:</b> <code>{pay_req.id}</code>\n\n"
                    f"<i>စစ်ဆေးပြီးပါက Admin Dashboard မှ Approve ပြုလုပ်နိုင်ပါသည်။</i>"
                )
                for admin_id in settings.admin_ids:
                    try:
                        if dest_path.is_file():
                            await bot.send_photo(
                                chat_id=admin_id,
                                photo=FSInputFile(str(dest_path)),
                                caption=admin_text,
                                parse_mode=ParseMode.HTML,
                            )
                        else:
                            await bot.send_message(
                                chat_id=admin_id,
                                text=admin_text,
                                parse_mode=ParseMode.HTML,
                            )
                    except Exception as send_err:
                        logger.warning(f"Failed to alert admin {admin_id}: {send_err}")
            finally:
                await bot.session.close()
        except Exception as alert_err:
            logger.warning(f"Admin payment alert error: {alert_err}")

    return {
        "success": True,
        "request_id": pay_req.id,
        "message": "ငွေလွှဲပြေစာကို အောင်မြင်စွာ တင်သွင်းပြီးပါပြီ။ Admin မှ မကြာမီ စစ်ဆေးပေးပါမည်။",
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


@api_router.get("/admin/overview")
async def get_admin_overview(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Return high-level operations statistics for Admin Console."""
    user_count_res = await db.execute(select(func.count(User.id)))
    total_users = user_count_res.scalar() or 0

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
    banned_users = (await db.execute(
        select(func.count(User.id)).where(User.status == UserStatus.BLOCKED.value)
    )).scalar() or 0
    new_users_24h = (await db.execute(
        select(func.count(User.id)).where(User.created_at >= cutoff_24h)
    )).scalar() or 0

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
        "vip_users": vip_users,
        "pending_slips": pending_slips,
        "queued_jobs": queued_jobs,
        "running_jobs": running_jobs,
        "cloud_worker_status": "ONLINE (Render Cloud)",
        "failed_jobs_24h": failed_24h,
        "jobs_by_status": jobs_by_status,
        "banned_users": banned_users,
        "new_users_24h": new_users_24h,
        "db_size_bytes": db_size_bytes,
        "storage_bytes": storage_bytes,
        "disk": disk_info,
        "recent_admin_actions": recent_actions,
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
):
    """Most recently joined users (admin only) — the Users screen landing list.

    Search requires a query of 2+ characters; admins also need a way to
    simply browse who just joined without knowing a name or ID first.
    The optional ``category`` (FREE|PURCHASED) filters server-side (the
    Users screen's filter chips page through the whole user base with
    limit/offset, so filtering must happen here, not over one page);
    ``total`` reports the filtered row count so the client knows when
    "Load more" has reached the end.
    """
    limit = max(1, min(int(limit or 30), 100))
    offset = max(0, int(offset or 0))
    stmt = select(User)
    count_stmt = select(func.count(User.id))
    cat = _admin_category_filter(category)
    if cat:
        stmt = stmt.where(User.account_category == cat)
        count_stmt = count_stmt.where(User.account_category == cat)
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
    return payload


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
        time_ago = f"{mins_ago}m ago" if mins_ago < 60 else f"{mins_ago // 60}h ago"

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

        jobs_out.append({
            "id": j.id,
            "user_id": j.user_id,
            "user_name": (u.first_name if u else None) or f"User {j.user_id}",
            "username": u.username if u else None,
            "status": j.status,
            "job_type": "RECAP" if is_recap else "VOICE_CLONE",
            "engine": j.engine or ("recap" if is_recap else "voxcpm"),
            "preset_voice": j.preset_voice or ("Thiha" if is_recap else "Standard"),
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
    """Re-enqueue a failed or cancelled job so it will be claimed and re-executed."""
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

    job.status = JobStatus.QUEUED.value
    job.started_at = None
    job.finished_at = None
    job.error_message = None
    await db.commit()

    if job.job_type == JobType.RECAP.value or job.id.startswith("rc_") or job.engine == "recap":
        try:
            from app.webapp.api import _run_recap_background_job, _recap_redispatch_plan
            u = await db.get(User, job.user_id)
            # Restore the persisted submit-time config like the queue
            # worker does — but NOT the credit state: a FAILED/CANCELLED
            # job's charge was already refunded by the failure/cancel
            # paths, so the retry runs uncharged (historical behaviour).
            plan = _recap_redispatch_plan(job, u, restore_credit_state=False)
            plan["entry"]["progress_label"] = "🔄 Re-queued by Administrator..."
            RECAP_JOBS_REGISTRY[job_id] = plan["entry"]
            asyncio.create_task(_run_recap_background_job(
                job_id, plan["cfg"], job.user_id, job.chat_id or job.user_id,
                is_vip=plan["is_vip"], credit_deducted=False, phase=plan["phase"],
            ))
        except Exception as e:
            logger.warning(f"Could not re-dispatch recap job background task: {e}")

    return {"success": True, "message": f"အလုပ် {job_id[:8]} ကို တန်းစီထဲ ပြန်ထည့်ပြီးပါပြီ"}


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
    if job.status not in (JobStatus.QUEUED.value, JobStatus.RUNNING.value):
        raise HTTPException(
            status_code=400,
            detail=f"Only QUEUED or RUNNING jobs can be cancelled (this job is {job.status})",
        )

    is_recap = (job.job_type == JobType.RECAP.value or job.id.startswith("rc_") or job.engine == "recap")
    refund_info = {"refunded": False}

    job.status = JobStatus.CANCELLED.value
    job.finished_at = utc_now()
    job.error_message = "Cancelled by Administrator via Admin Console"
    await db.commit()

    try:
        if is_recap:
            entry = RECAP_JOBS_REGISTRY.get(job_id)
            if entry is not None:
                if job_id in RECAP_JOBS_REGISTRY:
                    RECAP_JOBS_REGISTRY[job_id]["status"] = "FAILED"
                    RECAP_JOBS_REGISTRY[job_id]["error_message"] = "Cancelled by Administrator"
                if await _refund_recap_job_charge(job.user_id, entry):
                    refund_info = {"refunded": True, "recap_credits": int(entry.get("credits_refunded") or 0)}
            else:
                # Registry lost (server restart): read the charge from
                # the persisted blob on the Job row (hunt #5) -- a
                # segmented job may have been charged N credits, not
                # the submit-time 1 this fallback used to assume, and
                # a blob that already records the refund must not be
                # refunded a second time. Legacy rows without a blob
                # keep the historical behaviour: the submit-time base
                # credit is the only provable charge. A second cancel
                # is impossible (status is now CANCELLED).
                persisted = _load_persisted_recap_state(job)
                proven_credits = 1
                already_refunded = False
                if persisted is not None:
                    already_refunded = bool(persisted.get("credit_refunded"))
                    if persisted.get("credit_deducted"):
                        proven_credits = max(1, int(persisted.get("credits_charged") or 1))
                if already_refunded:
                    refund_info = {
                        "refunded": True,
                        "recap_credits": int(persisted.get("credits_refunded") or proven_credits),
                        "note": "charge already refunded before the restart (persisted state)",
                    }
                elif not settings.is_admin(job.user_id) and await _refund_recap_credits(job.user_id, proven_credits):
                    refund_info = {
                        "refunded": True,
                        "recap_credits": proven_credits,
                        "note": (
                            "registry unavailable; amount restored from persisted recap state"
                            if persisted is not None
                            else "base credit only (registry unavailable)"
                        ),
                    }
        else:
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
    return {"success": True, "message": f"အလုပ် {job_id[:8]} ကို ပယ်ဖျက်ပြီးပါပြီ", "refund": refund_info}


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
    tutorial_index_link: Optional[str] = None
    tutorial_plans_link: Optional[str] = None
    tutorial_customvoice_link: Optional[str] = None
    tutorial_downloader_link: Optional[str] = None
    tutorial_scriptedit_link: Optional[str] = None
    tutorial_recapadvanced_link: Optional[str] = None
    tutorial_refer_link: Optional[str] = None
    tutorial_support_link: Optional[str] = None
    maintenance_mode: Optional[bool] = None
    group_ai_admin_enabled: Optional[bool] = None
    character_limit_free: Optional[int] = None
    gemini_primary_model: Optional[str] = None
    gemini_fallback_1: Optional[str] = None
    gemini_fallback_2: Optional[str] = None
    gemini_fallback_3: Optional[str] = None


@api_router.get("/admin/settings")
async def get_admin_settings(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Get all dynamic platform settings."""
    from app.services.dynamic_settings import load_dynamic_settings
    await load_dynamic_settings(db)
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
        **{
            key: getattr(settings, key, default)
            for key, default in TUTORIAL_EXTRA_LINKS.items()
        },
        "maintenance_mode": getattr(settings, "maintenance_mode", False),
        "group_ai_admin_enabled": getattr(settings, "group_ai_admin_enabled", False),
        "character_limit_free": int(getattr(settings, "character_limit_free", 1000) or 1000),
        "gemini_primary_model": getattr(settings, "gemini_primary_model", "gemini-3.6-flash"),
        "gemini_fallback_1": getattr(settings, "gemini_fallback_1", "gemini-3.5-flash"),
        "gemini_fallback_2": getattr(settings, "gemini_fallback_2", "gemini-3.5-flash-lite"),
        "gemini_fallback_3": getattr(settings, "gemini_fallback_3", "gemini-flash-lite-latest"),
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
        raise HTTPException(status_code=404, detail="Pending payment request not found")
    await db.commit()

    _plan = _get_plan(p_req.package_code)
    recap_added = _plan["recap_credits"] if _plan else 0
    target_user = await db.get(User, p_req.user_id)

    # Instant Telegram notification to the approved user
    if settings.bot_token and p_req.user_id:
        try:
            from aiogram import Bot
            from aiogram.enums import ParseMode
            from app.ui.keyboards import main_menu_kb
            bot = Bot(token=settings.bot_token)
            try:
                user_notice = (
                    f"🎉 <b>ဂုဏ်ယူပါသည်! သင်၏ VIP ဝယ်ယူမှု အတည်ပြုပြီးပါပြီ</b> ✨\n\n"
                    f"📦 <b>Package:</b> <code>{p_req.package_code}</code>\n"
                    f"🎙️ <b>Voice မိနစ်:</b> <b>+{p_req.minutes} မိနစ်</b>\n"
                    f"🎬 <b>Recap Credits:</b> <b>+{recap_added} Credits</b>\n"
                    f"👑 <b>Status:</b> <b>VIP PURCHASED ⭐</b>\n\n"
                    f"💡 <i>အောက်ပါခလုတ်ကို နှိပ်၍ Studio Pro (Mini App) ကို ချက်ချင်း အသုံးပြုနိုင်ပါပြီခင်ဗျာ။</i>"
                )
                await bot.send_message(
                    chat_id=p_req.user_id,
                    text=user_notice,
                    reply_markup=main_menu_kb(lang=target_user.language_code if target_user else "my"),
                    parse_mode=ParseMode.HTML,
                )
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
                    f"သင် တင်သွင်းခဲ့သော ငွေလွှဲပြေစာ (ID: <code>{p_req.id[:8]}</code>) အား စစ်ဆေးချက်အရ အတည်မပြုနိုင်သေးပါခင်ဗျာ။\n\n"
                    f"📌 <b>အကြောင်းပြချက်:</b> <i>{html.escape(req.reason)}</i>\n\n"
                    f"💡 <i>အချက်အလက် မှန်ကန်စွာဖြင့် ထပ်မံတင်သွင်းနိုင်သလို အခက်အခဲရှိပါက Support သို့ ဆက်သွယ်မေးမြန်းနိုင်ပါသည်။</i>"
                )
                await bot.send_message(
                    chat_id=p_req.user_id,
                    text=reject_notice,
                    parse_mode=ParseMode.HTML,
                )
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
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            purchased_seconds_zeroed = outstanding

    if category_change:
        target_user.account_category = req.account_category

    if credits_delta != 0:
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
            "account_category": req.account_category if category_change else None,
            "purchased_seconds_zeroed": purchased_seconds_zeroed,
        }),
    ))
    await db.commit()
    return {
        "success": True,
        "message": f"အသုံးပြုသူ {req.user_id} ကို ပြင်ဆင်ပြီးပါပြီ",
        "purchased_seconds_zeroed": purchased_seconds_zeroed,
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
            detail="Broadcast စာသား ရှည်လွန်းပါသည် — Telegram ကန့်သတ်ချက် (အက္ခရာ 4096 လုံး) ထက် ကျော်နေပါသည်။ တိုအောင် ပြင်ပြီး ပြန်စမ်းပါခင်ဗျာ။",
        )

    # 1. Total users in database
    total_db_users = (await db.execute(select(func.count(User.id)))).scalar() or 0

    # 2. Targeted active users (bot_blocked users are send-suppressed
    # via their flag only — they are not admin-banned; hunt B1)
    stmt = select(User.id).where(User.status == "ACTIVE", User.bot_blocked.is_not(True))
    res = await db.execute(stmt)
    user_ids = res.scalars().all()
    db_blocked_count = total_db_users - len(user_ids)

    # Deliver via Telegram Bot
    if settings.bot_token:
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode
        from aiogram.exceptions import (
            TelegramForbiddenError,
            TelegramBadRequest,
            TelegramRetryAfter,
            TelegramAPIError,
        )

        bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        sent_count = 0
        blocked_by_user_count = 0
        deactivated_or_not_found_count = 0
        invalid_test_id_count = 0
        other_error_count = 0
        newly_blocked_ids = []

        for uid in user_ids:
            # Detect legacy dummy/test IDs (< 8 digits)
            if len(str(uid)) < 8:
                invalid_test_id_count += 1
                continue

            try:
                await bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
                sent_count += 1
                await asyncio.sleep(0.05)
            except TelegramForbiddenError:
                # User has blocked the bot
                blocked_by_user_count += 1
                newly_blocked_ids.append(uid)
            except TelegramBadRequest:
                # Chat not found, user deactivated, or account deleted
                deactivated_or_not_found_count += 1
            except TelegramRetryAfter as e:
                # Handle flood rate limits gracefully
                await asyncio.sleep(e.retry_after)
                try:
                    await bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
                    sent_count += 1
                except TelegramForbiddenError:
                    blocked_by_user_count += 1
                    newly_blocked_ids.append(uid)
                except Exception:
                    other_error_count += 1
            except Exception:
                other_error_count += 1

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

        # Log broadcast to audit log with granular delivery telemetry
        db.add(AuditLog(
            admin_id=admin.id,
            action="broadcast.send_webapp",
            target_type="broadcast_campaign",
            target_id=f"WEBAPP_BC_{int(time.time())}",
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

    return {"success": True, "message": f"အသုံးပြုသူ {len(user_ids)} ဦးထံ Broadcast ပို့ရန် တန်းစီပြီးပါပြီ"}
