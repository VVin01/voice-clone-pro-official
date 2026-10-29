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
from pydantic import BaseModel, Field
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import get_db_session
from app.models.enums import AccountCategory, JobStatus, VoiceModelStatus, VoiceModelType, PaymentStatus, JobType
from app.models.schema import User, Job, VoiceModel, MinuteBalance, PaymentRequest, AuditLog, utc_now
from app.services.i18n import t
from app.services.models import get_authorized_model
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
                    raise HTTPException(status_code=413, detail="File size exceeds the allowed limit")
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
        raise HTTPException(status_code=500, detail=f"Failed to save uploaded file: {exc}")
    return total


async def _recap_active_job_count(db, user_id: int) -> int:
    """Unique active recap jobs for a user (registry ∪ DB, not summed).

    Registry entries and DB rows describe the same job, so counting both
    naively would double-count and break limits above 1.
    """
    ids = set()
    for job_id, entry in RECAP_JOBS_REGISTRY.items():
        if entry.get("user_id") == user_id and entry.get("status") in ("QUEUED", "RUNNING"):
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


async def _refund_recap_credit(user_id: int) -> bool:
    """Refund one recap credit in its own session/commit.

    Separate from the job-status update so a status-write failure cannot
    silently swallow the refund, and idempotent at the call site: callers
    only invoke it once per failed job (see _run_recap_background_job,
    which marks the registry entry as refunded after a successful commit).
    Returns True when a credit was actually added.
    """
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            user = await session.get(User, user_id)
            if not user or settings.is_admin(user.id):
                return False
            user.recap_credits = (getattr(user, "recap_credits", 0) or 0) + 1
            await session.commit()
            return True
    except Exception as exc:
        logger.warning(f"Recap credit refund failed for user {user_id}: {exc}")
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


# ── 1. User & Wallet Profile ───────────────────────────────────────────────
@api_router.get("/user/me")
async def get_user_profile(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """Retrieve user's VIP status, minute balance, allowance, and platform config."""
    from app.services.dynamic_settings import get_dynamic_setting
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
        default=getattr(settings, "tutorial_video_link", "https://t.me/VoiceClonePro/16"),
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
        "tutorial_video_link": tutorial_link,
        "tutorial_video_enabled": str(tutorial_enabled).lower() != "false",
        "tutorial_voice_title": tut_voice_title,
        "tutorial_voice_link": tut_voice_link,
        "tutorial_voice_enabled": str(tut_voice_enabled).lower() != "false",
        "tutorial_recap_title": tut_recap_title,
        "tutorial_recap_link": tut_recap_link,
        "tutorial_recap_enabled": str(tut_recap_enabled).lower() != "false",
        # Gate status for the Mini App join-and-verify UX (2026-10-29).
        # This is the CACHED flag state only; a live check happens on
        # gated writes and via POST /api/user/verify-membership. Admins
        # are always treated as membership-verified (see gate.py).
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
        default=getattr(settings, "tutorial_video_link", "https://t.me/VoiceClonePro/16"),
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
    model_slug: str = "voxcpm-cloud-ai"
    speed: str = "normal"
    gender_preset: str = "m2m"
    engine: str = "voxcpm"
    preset_voice: Optional[str] = None


@api_router.post("/voices/generate")
async def create_voice_job(
    req: VoiceGenerateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    _gate: User = Depends(require_gate_verified),
):
    """Submit a voice clone generation job from Mini App."""
    clean_text = req.text.strip()
    if not clean_text:
        raise HTTPException(status_code=400, detail="စာသား ရိုက်ထည့်ရန် လိုအပ်ပါသည်")

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
    clean_text = text.strip()
    if not clean_text:
        raise HTTPException(status_code=400, detail="စာသား ရိုက်ထည့်ရန် လိုအပ်ပါသည်")

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
    # Security fix (audit 2026-10-28): use a separate worker
    # token. Fall back to the bot token only with a warning so
    # existing deployments do not break immediately.
    worker_token = getattr(settings, "worker_token", "") or settings.bot_token
    if not getattr(settings, "worker_token", ""):
        logger.warning(
            "WORKER_TOKEN is not set; falling back to the Telegram "
            "bot token for worker uploads. Set a separate "
            "WORKER_TOKEN and rotate it."
        )
    expected_token = worker_token
    if not auth_token or auth_token != expected_token:
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
async def get_voice_sample_audio(slug: str, db: AsyncSession = Depends(get_db_session)):
    """Serve sample audio preview for curated VoxCPM2 voice personas."""
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

    # 1. Check static sample mp3 in static/audio/samples/
    sample_file = settings.root / "app" / "webapp" / "static" / "audio" / "samples" / f"{target_id}.mp3"
    if sample_file.is_file():
        return FileResponse(
            str(sample_file),
            media_type="audio/mpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    # 2. Check DB VoiceModel sample_audio_path
    stmt = select(VoiceModel).where(VoiceModel.slug == slug)
    res = await db.execute(stmt)
    m = res.scalar_one_or_none()
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
        canonical_url = unshorten_url(url)
    except Exception:
        raise HTTPException(status_code=400, detail="This URL is not allowed")
    # SSRF: unshorten_url now validates every redirect hop, and this
    # re-validates the final URL (DNS resolve + private-range block)
    # before yt-dlp is allowed to fetch it.
    try:
        from app.services.url_safety import validate_public_url

        canonical_url = validate_public_url(canonical_url)
    except Exception:
        raise HTTPException(status_code=400, detail="This URL is not allowed")

    try:
        from app.services.downloader import MediaDownloader
        downloader = MediaDownloader()
        info = downloader.get_info(canonical_url)
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
            "formats_available": ["MP4 (Full HD Video)", "MP3 (192kbps Audio)", "SRT (Subtitles)"],
        }
    except Exception as e:
        logger.warning(f"yt-dlp extract error for {canonical_url}: {e}")
        # Graceful fallback response
        platform = detect_platform(canonical_url)
        return {
            "success": True,
            "url": canonical_url,
            "platform": platform,
            "title": f"{platform} Content",
            "uploader": "Online Creator",
            "thumbnail": "",
            "estimated_duration": "03:30",
            "has_subtitles": True,
            "formats_available": ["MP4 (Video)", "MP3 (Audio 192k)", "SRT (Subtitles)"],
        }


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

    # Inspect media duration & metadata
    from app.services.downloader import probe_local_media, extract_thumbnail_from_media, format_duration, find_best_preview_timestamp
    meta = probe_local_media(str(saved_path))
    dur_sec = meta.get("duration", 0.0) or 0.0

    # Extract optimal preview thumbnail
    thumb_path = temp_dir / f"{file_id}_thumb.jpg"
    best_ts = find_best_preview_timestamp(str(saved_path), target_dir=str(temp_dir))
    extract_thumbnail_from_media(str(saved_path), str(thumb_path), timestamp_sec=best_ts)

    thumb_url = f"/api/recap/frame?path={file_id}_thumb.jpg" if thumb_path.is_file() else ""
    display_title = Path(clean_name).stem or "Uploaded Movie Video"
    UPLOADED_VIDEO_TITLES[str(saved_path)] = display_title
    UPLOADED_VIDEO_TITLES[saved_path.name] = display_title

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


@api_router.get("/recap/frame")
async def get_recap_frame(
    path: str = Query(...),
    t: Optional[float] = Query(None),
    user: User = Depends(get_current_user),
):
    """Serve or dynamically generate video frame snapshot at timestamp t with caching and security containment."""
    clean_p = Path(path).name
    # Search in temp uploads dir
    cand = settings.storage_root / "temp" / "recap_uploads" / clean_p
    if cand.is_file() and t is None:
        return FileResponse(cand, media_type="image/jpeg")

    # If t is given and path points to an uploaded or local video file
    actual_path = settings.storage_root / "temp" / "recap_uploads" / clean_p
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
UPLOADED_VIDEO_TITLES: Dict[str, str] = {}


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


class RecapConfigRequest(BaseModel):
    url: str
    title: Optional[str] = None
    voice_slug: str = "komin"
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
    include_logo: bool = True
    logo_pos: str = "top-right"       # top-right, top-left, bottom-right, bottom-left, top-center
    duration_seconds: Optional[int] = None


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

            if file_size <= 50 * 1024 * 1024:
                vf = FSInputFile(video_path, filename=Path(video_path).name)
                await bot.send_video(chat_id=chat_id, video=vf, caption=caption, parse_mode="HTML")
            else:
                from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
                app_url = getattr(settings, "mini_app_url", "https://voice-clone-studio-c1er.onrender.com")
                v_parent = Path(video_path).parent.name
                v_grand = Path(video_path).parent.parent.name
                jid = v_parent if v_parent.startswith("rc_") else (v_grand if v_grand.startswith("rc_") else Path(video_path).stem)
                dl_url = f"{app_url}/api/recap/download/{jid}/video"
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=t("clone_btn_miniapp_watch", lang),
                            web_app=WebAppInfo(url=f"{app_url}?tab=tab-recap")
                        ),
                        InlineKeyboardButton(
                            text=t("clone_btn_direct_download", lang),
                            url=dl_url
                        )
                    ]
                ])
                over_caption = t(
                    "recap_over_caption",
                    lang,
                    jid=jid[:8],
                    size_formatted=size_formatted,
                )
                await bot.send_message(chat_id=chat_id, text=over_caption, reply_markup=kb, parse_mode="HTML")

            if srt_path and os.path.isfile(srt_path):
                sf = FSInputFile(srt_path, filename=f"subtitles_{Path(srt_path).name}")
                await bot.send_document(chat_id=chat_id, document=sf, caption=t("clone_caption_subtitles", lang), parse_mode="HTML")

        finally:
            await bot.session.close()
    except Exception as e:
        logger.warning(f"Failed to send recap video to Telegram chat {chat_id}: {e}")


async def _run_recap_background_job(
    job_id: str,
    req: RecapConfigRequest,
    user_id: int,
    chat_id: int,
    is_vip: bool = False,
    credit_deducted: bool = False,
):
    entry = RECAP_JOBS_REGISTRY.get(job_id)
    if not entry:
        return

    entry["status"] = "RUNNING"
    entry["progress_percent"] = 15
    entry["progress_step"] = "1/4"
    entry["progress_label"] = "📥 Downloading source video from URL..."

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
                entry["progress_step"] = f"{cur}/{tot}"
                entry["progress_label"] = label
                pct = int(min(95, max(15, (cur / max(1, tot)) * 100)))
                entry["progress_percent"] = pct

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
        if is_local_media(req.url):
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

        max_dur_limit = 600 if is_vip else getattr(settings, "recap_free_max_duration_seconds", 120)

        recap_fn = functools.partial(
            downloader.generate_recap_video,
            url_or_path=canonical_source_url,
            target_dir=str(job_dir),
            voice=selected_voice,
            narrative_style=req.story_style,
            voice_speed=req.voice_speed,
            tts_engine=tts_engine,
            voxcpm_control=voxcpm_control,
            voxcpm_ref_audio=voxcpm_ref_audio,
            is_female=is_female,
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
            logo_position=logo_pos_coords if req.include_logo else None,
            extra_text=watermark_text,
            is_vip=is_vip,
            max_duration_seconds=max_dur_limit,
            progress_callback=progress_cb,
        )

        recap_res = await loop.run_in_executor(None, recap_fn)

        video_path = recap_res.get("final_video") or recap_res.get("final_video_path")
        audio_path = recap_res.get("audio") or recap_res.get("final_audio_path")
        srt_path = recap_res.get("subtitles_srt")
        script_path = _build_clean_recap_script_file(job_dir, job_id, req.story_style, srt_path)

        _fb_used, _fb_segs = _recap_fallback_info(job_dir)
        entry["fallback_used"] = _fb_used
        entry["fallback_segments"] = _fb_segs

        entry["status"] = "SUCCESS"
        entry["progress_percent"] = 100
        entry["progress_label"] = "🎉 Recap Video Generation Completed!"
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
            if await _refund_recap_credit(user_id):
                entry["credit_refunded"] = True
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
        if credit_deducted and not entry.get("credit_refunded"):
            if await _refund_recap_credit(user_id):
                entry["credit_refunded"] = True

        if settings.bot_token and chat_id:
            try:
                from aiogram import Bot
                from aiogram.client.default import DefaultBotProperties
                from aiogram.enums import ParseMode
                bot_err = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
                try:
                    err_short = html.escape(str(e)[:180])
                    refund_note = "✅ သင့် Recap Credit (1c) ကို အကောင့်ထဲသို့ အလိုအလျောက် ပြန်လည်ဖြည့်သွင်းပေးထားပါသည်ခင်ဗျာ။" if credit_deducted else ""
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


async def cleanup_expired_jobs_24h() -> int:
    """
    Purge jobs and generated media files older than 24 hours to enforce privacy and free disk space.
    Removes DB records and physical files (recap output dirs, temporary voice clones).
    """
    deleted_count = 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    try:
        from app.db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            stmt = select(Job).where(Job.created_at < cutoff)
            res = await session.execute(stmt)
            expired_jobs = res.scalars().all()

            for j in expired_jobs:
                try:
                    # 1. Clean recap output directory
                    if j.job_type == JobType.RECAP.value or j.id.startswith("rc_"):
                        job_dir = settings.storage_root / "recap_output" / j.id
                        if job_dir.is_dir():
                            shutil.rmtree(job_dir, ignore_errors=True)

                    # 2. Clean voice clone generated files
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

                    # 3. Clean from memory registry
                    RECAP_JOBS_REGISTRY.pop(j.id, None)

                    # 4. Delete DB record
                    await session.delete(j)
                    deleted_count += 1
                except Exception as del_err:
                    logger.warning(f"Error cleaning job {j.id}: {del_err}")

            await session.commit()

            # Clean memory registry keys older than 24h
            now_ts = time.time()
            expired_keys = [k for k, v in RECAP_JOBS_REGISTRY.items() if now_ts - v.get("created_at", 0) > 86400]
            for k in expired_keys:
                RECAP_JOBS_REGISTRY.pop(k, None)

            if deleted_count > 0:
                logger.info(f"Auto-cleaned {deleted_count} expired (>24h) jobs and media files.")
    except Exception as e:
        logger.error(f"Error during 24h cleanup: {e}")

    return deleted_count


@api_router.post("/recap/generate")
async def create_recap_job(
    req: RecapConfigRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    _gate: User = Depends(require_gate_verified),
):
    """Submit 1-Click Pro Recap Job with complete ratio, audio, subtitle, and anti-copyright settings."""
    # SSRF + local-file containment, before any probe, deduction, or queueing.
    _req_url = (req.url or "").strip()
    if _req_url.startswith(("http://", "https://")):
        try:
            from app.services.downloader import extract_first_url as _extract_first, unshorten_url as _unshorten
            from app.services.url_safety import validate_public_url as _validate_url

            _canon = _unshorten(_extract_first(_req_url) or _req_url)
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

    is_vip = (user.account_category == AccountCategory.PURCHASED.value) or settings.is_admin(user.id)
    credits = getattr(user, "recap_credits", 0)

    if not is_vip and credits <= 0:
        raise HTTPException(
            status_code=403,
            detail="Recap Video ဖန်တီးရန် Credit မလုံလောက်တော့ပါ။ ကျေးဇူးပြု၍ Movie Recap Plan ရယူပါရန်။",
        )

    # Free user duration limit: Max 2 minutes (120s); VIP: Max 10 minutes (600s)
    max_duration_allowed = 600 if is_vip else getattr(settings, "recap_free_max_duration_seconds", 120)
    checked_duration = req.duration_seconds

    # Early duration validation via yt-dlp or ffprobe before deducting credits or queueing
    if (not checked_duration or checked_duration <= 0) and req.url:
        try:
            if req.url.startswith("http://") or req.url.startswith("https://"):
                from app.services.downloader import extract_first_url, unshorten_url, MediaDownloader
                can_url = unshorten_url(extract_first_url(req.url) or req.url)
                info = MediaDownloader().get_info(can_url)
                checked_duration = info.get("duration", 0) or 0
            elif os.path.isfile(req.url):
                from app.services.recap_engine.core import probe_duration, find_ffprobe
                ffprobe_bin = find_ffprobe()
                checked_duration = probe_duration(ffprobe_bin, Path(req.url))
        except Exception as probe_err:
            logger.warning(f"Could not pre-probe video duration for {req.url}: {probe_err}")

    if checked_duration and checked_duration > max_duration_allowed:
        dur_mins = int(checked_duration) // 60
        dur_secs = int(checked_duration) % 60
        max_m = max_duration_allowed // 60
        tier_label = "VIP" if is_vip else "အခမဲ့ (Free)"
        raise HTTPException(
            status_code=400,
            detail=f"ဗီဒီယို ကြာချိန် ({dur_mins:02d}:{dur_secs:02d}) သည် {tier_label} အသုံးပြုသူ ကန့်သတ်ချက် (အများဆုံး {max_m} မိနစ်) ထက် ကျော်လွန်နေပါသည်။ "
                   + ("ပိုရှည်သော ဗီဒီယိုများ (၁၀ မိနစ်အထိ) ပြုလုပ်လိုပါက VIP သို့ အဆင့်မြှင့်တင်ပါခင်ဗျာ။" if not is_vip else ""),
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

    # Deduct credit if non-admin
    deducted_credit = False
    if not settings.is_admin(user.id) and hasattr(user, "recap_credits") and user.recap_credits > 0:
        user.recap_credits -= 1
        await db.commit()
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
        )
        db.add(db_job)
        await db.commit()
    except Exception as err_db:
        logger.warning(f"Could not persist recap Job record in DB: {err_db}")

    # Start background task with VIP tier flag for watermark control and credit tracking
    try:
        asyncio.create_task(_run_recap_background_job(job_id, req, user.id, user.id, is_vip=is_vip, credit_deducted=deducted_credit))
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
            "error_message": entry.get("error_message"),
            "fallback_used": bool(entry.get("fallback_used")),
            "fallback_segments": entry.get("fallback_segments", []),
            "download_urls": entry.get("download_urls", {}),
        }

    job = await _require_job_owner(db, job_id, user)

    is_success = (job.status == JobStatus.SUCCESS.value)
    is_active = (job.status in (JobStatus.QUEUED.value, JobStatus.RUNNING.value))
    _fb_used, _fb_segs = _recap_fallback_info(settings.storage_root / "recap_output" / job_id)
    return {
        "id": job_id,
        "status": job.status,
        "fallback_used": _fb_used,
        "fallback_segments": _fb_segs,
        "progress_percent": 100 if is_success else (15 if is_active else 0),
        "progress_step": "4/4" if is_success else ("1/4" if is_active else ""),
        "progress_label": "Completed" if is_success else ("Processing in background..." if is_active else (job.error_message or "Failed")),
        "error_message": job.error_message,
        "download_urls": {
            "video": f"/api/recap/download/{job_id}/video" if is_success else None,
            "audio": f"/api/recap/download/{job_id}/audio" if is_success else None,
            "srt": f"/api/recap/download/{job_id}/srt" if is_success else None,
            "script": f"/api/recap/download/{job_id}/script" if is_success else None,
        },
    }


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
        raise HTTPException(status_code=500, detail=f"Telegram သို့ ပေးပို့ရာတွင် အမှားဖြစ်ပေါ်ပါသည်: {e}")
    finally:
        await bot.session.close()


# ── 5.1. 24-Hour Job History & Lifecycle Management ─────────────────────────
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
                matches = list(outputs_dir.glob(f"{job_id}*"))
                if matches and matches[0].is_file():
                    return True
            for cand_dir in [settings.storage_root / "outputs", settings.storage_root / "temp", settings.storage_root / "voices", settings.storage_root / "recap_output"]:
                if cand_dir.is_dir():
                    m = list(cand_dir.glob(f"*{job_id}*"))
                    if m and m[0].is_file():
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
    Get user's 24-hour job generation history (both Voice Clone and Recap).
    Jobs older than 24 hours are automatically purged.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
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

        deliverables = {}
        download_url = None
        is_recap = (j.job_type == JobType.RECAP.value or j.id.startswith("rc_"))

        # Verify whether output file actually exists on current container disk
        file_exists = False
        if j.status == JobStatus.SUCCESS.value:
            file_exists = check_job_file_exists(j.id, is_recap, j.result_path)
        is_expired = bool(j.status == JobStatus.SUCCESS.value and not file_exists)

        if j.status == JobStatus.SUCCESS.value and not is_expired:
            if is_recap:
                recap_entry = RECAP_JOBS_REGISTRY.get(j.id)
                if recap_entry and recap_entry.get("download_urls"):
                    deliverables = recap_entry["download_urls"]
                    download_url = deliverables.get("video")
                else:
                    download_url = f"/api/recap/stream/{j.id}/video"
                    deliverables = {
                        "video": f"/api/recap/stream/{j.id}/video",
                        "audio": f"/api/recap/stream/{j.id}/audio",
                        "srt": f"/api/recap/download/{j.id}/srt",
                        "script": f"/api/recap/download/{j.id}/script",
                    }
            else:
                download_url = f"/api/audio/stream/{j.id}"
                deliverables = {
                    "audio": f"/api/audio/stream/{j.id}",
                    "download": f"/api/audio/download/{j.id}",
                }

        raw_snippet = _clean_recap_display_title(j.input_text) if is_recap else (j.input_text or "")
        out.append({
            "id": j.id,
            "job_type": "RECAP" if is_recap else "VOICE_CLONE",
            "engine": j.engine,
            "fallback_used": (not is_recap) and j.engine == "edge_fallback",
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
        "ttl_hours": 24,
    }


@api_router.get("/admin/history")
async def get_admin_global_history(
    timeframe: str = "24h",
    status: str = "all",
    job_type: str = "all",
    limit: int = 150,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """
    Admin Only: Filter and view all users' generated jobs with custom timeframe & status filters.
    """
    if not settings.is_admin(user.id):
        raise HTTPException(status_code=403, detail="Admin access required")

    now_utc = datetime.now(timezone.utc)
    stmt = (
        select(Job, User.username, User.first_name)
        .outerjoin(User, Job.user_id == User.id)
    )

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

        # Verify whether output file actually exists on current container disk
        file_exists = False
        if job.status == JobStatus.SUCCESS.value:
            file_exists = check_job_file_exists(job.id, is_recap, job.result_path)
        is_expired = bool(job.status == JobStatus.SUCCESS.value and not file_exists)

        deliverables = {}
        download_url = None
        if job.status == JobStatus.SUCCESS.value and not is_expired:
            if is_recap:
                download_url = f"/api/recap/stream/{job.id}/video"
                deliverables = {
                    "video": f"/api/recap/stream/{job.id}/video",
                    "audio": f"/api/recap/stream/{job.id}/audio",
                    "srt": f"/api/recap/download/{job.id}/srt",
                    "script": f"/api/recap/download/{job.id}/script",
                }
            else:
                download_url = f"/api/audio/stream/{job.id}"
                deliverables = {
                    "audio": f"/api/audio/stream/{job.id}",
                    "download": f"/api/audio/download/{job.id}",
                }

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


@api_router.post("/admin/cleanup-24h")
async def trigger_admin_cleanup(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """Admin Only: Manually purge all expired jobs and disk files older than 24 hours."""
    if not settings.is_admin(user.id):
        raise HTTPException(status_code=403, detail="Admin access required")
    return await purge_admin_history(AdminPurgeHistoryRequest(mode="older_24h"), user=user, db=db)


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
                    "🛡️ 100% Anti-Copyright Smart Shield (Smart Blur, Auto-Mirror & Zoom)" if is_my else "100% Anti-Copyright Smart Shield (Smart Blur, Auto-Mirror & Zoom)",
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
                    "🛡️ 100% Anti-Copyright Smart Shield (Smart Blur, Auto-Mirror & Zoom)" if is_my else "100% Anti-Copyright Smart Shield (Smart Blur, Auto-Mirror & Zoom)",
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
                    "🛡️ 100% Anti-Copyright Smart Shield & Watermark လုံးဝမပါ" if is_my else "100% Anti-Copyright Smart Shield & Zero Watermark",
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
                    "🛡️ 100% Commercial Copyright Safe & Monetization Ready" if is_my else "100% Commercial Copyright Safe & Monetization Ready",
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

    # Plan catalogue is the single source of truth for what a code buys.
    # Unknown codes are rejected instead of silently defaulting to 250 min.
    from app.services.payments import get_plan_by_code

    _plan = get_plan_by_code(package_code)
    if _plan is None:
        raise HTTPException(status_code=400, detail="Unknown plan code")
    minutes = _plan["minutes"]

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


    worker_info = await _get_laptop_worker_status(db)

    return {
        "total_users": total_users,
        "vip_users": vip_users,
        "pending_slips": pending_slips,
        "queued_jobs": queued_jobs,
        "running_jobs": running_jobs,
        "cloud_worker_status": "ONLINE (Render Cloud)",
        "laptop_worker": worker_info,
        "laptop_worker_status": f"{worker_info['status_text']} [Engine: {worker_info['engine']}]",
    }


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

        # Verify whether output file actually exists on current container disk
        file_exists = False
        if j.status == JobStatus.SUCCESS.value:
            file_exists = check_job_file_exists(j.id, is_recap, j.result_path)
        is_expired = bool(j.status == JobStatus.SUCCESS.value and not file_exists)

        download_url = None
        deliverables = {}
        if is_recap:
            download_url = f"/api/recap/stream/{j.id}/video" if j.status == JobStatus.SUCCESS.value else None
            if j.status == JobStatus.SUCCESS.value:
                deliverables = {
                    "video": f"/api/recap/stream/{j.id}/video",
                    "audio": f"/api/recap/stream/{j.id}/audio",
                    "srt": f"/api/recap/download/{j.id}/srt",
                    "script": f"/api/recap/download/{j.id}/script",
                }
        else:
            download_url = f"/api/audio/stream/{j.id}" if j.status == JobStatus.SUCCESS.value else None
            if j.status == JobStatus.SUCCESS.value:
                deliverables = {
                    "audio": f"/api/audio/stream/{j.id}",
                    "download": f"/api/audio/download/{j.id}",
                }

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

    job.status = JobStatus.QUEUED.value
    job.started_at = None
    job.finished_at = None
    job.error_message = None
    await db.commit()

    if job.job_type == JobType.RECAP.value or job.id.startswith("rc_") or job.engine == "recap":
        try:
            from app.webapp.api import _run_recap_background_job, RecapConfigRequest
            u = await db.get(User, job.user_id)
            is_vip = ((u.account_category == AccountCategory.PURCHASED.value) if u else False) or settings.is_admin(job.user_id)
            source_url = job.source_audio_path or job.input_text or ""
            clean_title = _clean_recap_display_title(job.input_text, source_url)
            cfg = RecapConfigRequest(
                url=source_url,
                title=clean_title,
                voice_slug=job.preset_voice or "komin",
            )
            RECAP_JOBS_REGISTRY[job_id] = {
                "id": job_id,
                "user_id": job.user_id,
                "title": clean_title,
                "status": "QUEUED",
                "progress_percent": 5,
                "progress_step": "0/4",
                "progress_label": "🔄 Re-queued by Administrator...",
                "created_at": time.time(),
                "video_path": None,
                "audio_path": None,
                "srt_path": None,
                "script_path": None,
                "download_urls": {},
                "error_message": None,
            }
            asyncio.create_task(_run_recap_background_job(job_id, cfg, job.user_id, job.chat_id or job.user_id, is_vip=is_vip))
        except Exception as e:
            logger.warning(f"Could not re-dispatch recap job background task: {e}")

    return {"success": True, "message": f"Job {job_id[:8]} re-queued successfully!"}


@api_router.post("/admin/queue/{job_id}/cancel")
async def cancel_queued_job(
    job_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Cancel a currently queued or running job."""
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.status = JobStatus.CANCELLED.value
    job.finished_at = utc_now()
    job.error_message = "Cancelled by Administrator via Admin Console"

    if job_id in RECAP_JOBS_REGISTRY:
        RECAP_JOBS_REGISTRY[job_id]["status"] = "FAILED"
        RECAP_JOBS_REGISTRY[job_id]["error_message"] = "Cancelled by Administrator"

    await db.commit()
    return {"success": True, "message": f"Job {job_id[:8]} marked as CANCELLED."}


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
    maintenance_mode: Optional[bool] = None
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
        "tutorial_video_link": getattr(settings, "tutorial_video_link", "https://t.me/VoiceClonePro/16"),
        "tutorial_video_enabled": getattr(settings, "tutorial_video_enabled", True),
        "tutorial_voice_title": getattr(settings, "tutorial_voice_title", "Voice Clone ပြုလုပ်နည်း လမ်းညွှန်"),
        "tutorial_voice_link": getattr(settings, "tutorial_voice_link", "https://t.me/VoiceClonePro/16"),
        "tutorial_voice_enabled": getattr(settings, "tutorial_voice_enabled", True),
        "tutorial_recap_title": getattr(settings, "tutorial_recap_title", "Movie Recap ပြုလုပ်နည်း လမ်းညွှန်"),
        "tutorial_recap_link": getattr(settings, "tutorial_recap_link", "https://t.me/VoiceClonePro/16"),
        "tutorial_recap_enabled": getattr(settings, "tutorial_recap_enabled", True),
        "maintenance_mode": getattr(settings, "maintenance_mode", False),
        "gemini_primary_model": getattr(settings, "gemini_primary_model", "gemini-3.6-flash"),
        "gemini_fallback_1": getattr(settings, "gemini_fallback_1", "gemini-3.5-flash"),
        "gemini_fallback_2": getattr(settings, "gemini_fallback_2", "gemini-3.5-flash-lite"),
        "gemini_fallback_3": getattr(settings, "gemini_fallback_3", "gemini-flash-lite-latest"),
    }


@api_router.post("/admin/settings")
async def update_admin_settings(
    payload: AdminSettingsPayload,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Update dynamic platform settings directly from Admin Console."""
    from app.services.dynamic_settings import set_dynamic_setting
    updates = payload.model_dump(exclude_unset=True)
    for k, v in updates.items():
        if v is not None:
            await set_dynamic_setting(db, k, v, admin_id=admin.id)
    await db.commit()
    return {"success": True, "message": "Settings updated successfully!", "updated": updates}


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
    """Approve a payment request: upgrade user to PURCHASED and credit balance."""
    p_req = await db.get(PaymentRequest, req.request_id)
    if not p_req or p_req.status != PaymentStatus.PENDING.value:
        raise HTTPException(status_code=404, detail="Pending payment request not found")

    p_req.status = PaymentStatus.APPROVED.value
    p_req.reviewed_by = admin.id
    p_req.reviewed_at = utc_now()

    recap_added = 0
    # Upgrade user to PURCHASED
    target_user = await db.get(User, p_req.user_id)
    if target_user:
        target_user.account_category = AccountCategory.PURCHASED.value
        from app.services.payments import get_plan_by_code as _get_plan

        _plan = _get_plan(p_req.package_code)
        recap_added = _plan["recap_credits"] if _plan else 0
        if recap_added:
            target_user.recap_credits = (getattr(target_user, "recap_credits", 0) or 0) + recap_added

    # Credit minutes
    if p_req.minutes > 0:
        from app.services.ledger import credit_purchased_minutes
        await credit_purchased_minutes(
            session=db,
            user_id=p_req.user_id,
            minutes=p_req.minutes,
            payment_request_id=p_req.id,
            admin_id=admin.id,
        )
    await db.commit()

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

    return {"success": True, "message": f"Approved payment for User {p_req.user_id} (+{p_req.minutes} mins, +{recap_added} recap credits)"}


class RejectSlipRequest(BaseModel):
    request_id: str
    reason: str = "Invalid payment slip"


@api_router.post("/admin/reject-slip")
async def reject_payment_slip(
    req: RejectSlipRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Reject a payment request."""
    p_req = await db.get(PaymentRequest, req.request_id)
    if not p_req or p_req.status != PaymentStatus.PENDING.value:
        raise HTTPException(status_code=404, detail="Pending payment request not found")

    p_req.status = PaymentStatus.REJECTED.value
    p_req.reviewed_by = admin.id
    p_req.reviewed_at = utc_now()
    p_req.rejection_reason = req.reason
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

    return {"success": True, "message": f"Rejected payment request {p_req.id}"}


class AdjustUserRequest(BaseModel):
    user_id: int
    account_category: Optional[str] = None
    add_minutes: int = 0
    add_recap_credits: int = 0


@api_router.post("/admin/adjust-user")
async def adjust_user_status(
    req: AdjustUserRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Admin manually modifies user tier, adds minutes, or adds recap credits."""
    target_user = await db.get(User, req.user_id)
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    if req.account_category in (AccountCategory.FREE.value, AccountCategory.PURCHASED.value):
        target_user.account_category = req.account_category

    if req.add_recap_credits != 0:
        target_user.recap_credits = max(0, (getattr(target_user, "recap_credits", 0) or 0) + req.add_recap_credits)

    if req.add_minutes > 0:
        from app.services.ledger import credit_purchased_minutes
        await credit_purchased_minutes(
            session=db,
            user_id=target_user.id,
            minutes=req.add_minutes,
            payment_request_id=f"ADMIN_GRANT_{int(time.time())}",
            admin_id=admin.id,
        )

    await db.commit()
    return {"success": True, "message": f"User {req.user_id} updated successfully"}


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

    # 1. Total users in database
    total_db_users = (await db.execute(select(func.count(User.id)))).scalar() or 0

    # 2. Targeted active users
    stmt = select(User.id).where(User.status == "ACTIVE")
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

        # Update newly blocked users in database so subsequent broadcasts do not re-attempt
        if newly_blocked_ids:
            await db.execute(
                update(User)
                .where(User.id.in_(newly_blocked_ids))
                .values(status="BLOCKED")
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

    return {"success": True, "message": f"Broadcast queued for {len(user_ids)} users"}
