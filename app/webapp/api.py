"""
api.py - Complete REST API Router for Unified Telegram Mini App (Studio Pro).
Supports:
  - User profile & minute balance
  - Curated VoxCPM voices & Applio RVC models
  - Voice generation with reference audio upload & curated personas
  - Audio streaming & downloading
  - Media downloader extraction & conversion (YouTube/TikTok)
  - 1-Click AI Movie Recap Studio configuration
  - VIP packages & Payment slip submission
  - Role-gated Admin Console (Slips approval, user manager, broadcast, worker status)
"""

from __future__ import annotations

import os
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
        # Non-Telegram or unauthenticated web preview session (strictly Guest privileges)
        guest_id = 999999999
        user = await db.get(User, guest_id)
        if not user:
            user = User(
                id=guest_id,
                first_name="Guest",
                account_category=AccountCategory.FREE.value,
                language_code="my",
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
        return user

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
        "tutorial_video_link": tutorial_link,
        "tutorial_video_enabled": str(tutorial_enabled).lower() != "false",
        "tutorial_voice_title": tut_voice_title,
        "tutorial_voice_link": tut_voice_link,
        "tutorial_voice_enabled": str(tut_voice_enabled).lower() != "false",
        "tutorial_recap_title": tut_recap_title,
        "tutorial_recap_link": tut_recap_link,
        "tutorial_recap_enabled": str(tut_recap_enabled).lower() != "false",
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
async def get_curated_personas(user: User = Depends(get_current_user)):
    """Return the 6 Gemini-crafted curated Burmese voice personas."""
    lang = user.language_code or "my"
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
    actual_engine = "voxcpm" if is_vox else "applio"

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

    job = Job(
        id=str(uuid.uuid4()),
        user_id=user.id,
        chat_id=user.id,
        model_id=model.id,
        input_text=clean_text,
        speed_preset=req.speed,
        gender_preset=req.gender_preset,
        edge_voice="my-MM-ThihaNeural",
        engine=actual_engine,
        preset_voice=req.preset_voice,
        status=JobStatus.QUEUED.value,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

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
    actual_engine = "voxcpm" if is_vox else "applio"

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
        if len(content) > 100:
            with open(save_file, "wb") as f:
                f.write(content)
            ref_audio_path = str(save_file)

    job = Job(
        id=str(uuid.uuid4()),
        user_id=user.id,
        chat_id=user.id,
        model_id=model.id,
        input_text=clean_text,
        source_audio_path=ref_audio_path,
        speed_preset=speed,
        gender_preset=gender_preset,
        edge_voice="my-MM-ThihaNeural",
        engine=actual_engine,
        preset_voice=preset_voice,
        status=JobStatus.QUEUED.value,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

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

    is_waiting_laptop = (job.engine == "applio" and job.status == JobStatus.QUEUED.value)

    return {
        "id": job.id,
        "status": job.status,
        "engine": job.engine,
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
    db: AsyncSession = Depends(get_db_session),
):
    """Stream or download the completed generated voice clone audio file."""
    job = await db.get(Job, str(job_id))
    if not job:
        raise HTTPException(status_code=404, detail="Audio job not found")

    target_path = None
    if job.result_path and Path(job.result_path).is_file():
        target_path = Path(job.result_path)
    else:
        outputs_dir = settings.storage_root / "outputs"
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
    expected_token = settings.bot_token
    if not auth_token or auth_token != expected_token:
        raise HTTPException(status_code=403, detail="Unauthorized worker upload")

    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    outputs_dir = settings.storage_root / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    dest_filename = f"{job_id}_{file.filename}"
    dest_path = outputs_dir / dest_filename

    with open(dest_path, "wb") as f_out:
        shutil.copyfileobj(file.file, f_out)

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
    """Serve sample audio preview for curated voice personas and Applio models."""
    clean_id = slug.strip().lower()

    alias_map = {
        "system-models-system-kartoon_150e_1950s-pth": "kartoon",
        "system-models-system-ant_150e_11700s-pth": "ant-movie",
        "system-models-system-myanmar_voice_1_100e_6600s-pth": "myanmar-voice-1",
        "system-models-system-the_light_150e_450s-pth": "the-light",
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
):
    """Extract YouTube/TikTok/Douyin/Facebook video metadata via yt-dlp."""
    from app.services.downloader import extract_first_url, unshorten_url, detect_platform
    raw_url = req.url.strip()
    url = extract_first_url(raw_url) or raw_url
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="မှန်ကန်သော Video URL ထည့်သွင်းပေးပါခင်ဗျာ")

    canonical_url = unshorten_url(url)

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


# ── 5. Recap Studio Configuration & Background Pipeline ─────────────────────
RECAP_JOBS_REGISTRY: Dict[str, Dict[str, Any]] = {}

class RecapConfigRequest(BaseModel):
    url: str
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
    blur_y: int = Field(81, ge=0, le=100)
    blur_h: int = Field(13, ge=1, le=50)
    burn_subtitles: bool = True
    sub_y: int = Field(85, ge=0, le=100)
    sub_style: str = "yellow"         # yellow, white, boxed, neon
    sub_font_size: int = Field(16, ge=12, le=30)
    sub_lines: int = Field(1, ge=1, le=2)
    include_logo: bool = True
    logo_pos: str = "top-right"       # top-right, top-left, bottom-right, bottom-left, top-center
    duration_seconds: Optional[int] = None


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
    try:
        from aiogram import Bot
        from aiogram.types import FSInputFile
        from app.services.downloader import format_duration, format_filesize

        bot = Bot(token=settings.bot_token)
        try:
            file_size = os.path.getsize(video_path) if os.path.isfile(video_path) else 0
            dur_formatted = format_duration(duration_sec)
            size_formatted = format_filesize(file_size)

            caption = (
                f"🇲🇲 <b>Recap English → Myanmar Video (Smart Sync)</b>\n"
                f"🎬 <b>{html.escape(title[:60])}</b>\n"
                f"🎭 <b>စတိုင်:</b> {style}\n"
                f"🎙️ <b>အသံ:</b> {voice_name}\n"
                f"⏱️ <b>ကြာချိန်:</b> <code>{dur_formatted}</code> · 📁 <b>ဖိုင်ဆိုဒ်:</b> <code>{size_formatted}</code>\n\n"
                f"💡 <i>Mini App မှ ဒေါင်းလုဒ်ရယူနိုင်သလို Telegram သို့လည်း တိုက်ရိုက် ပေးပို့လိုက်ပါသည်ခင်ဗျာ။</i>"
            )

            if file_size <= 50 * 1024 * 1024:
                vf = FSInputFile(video_path, filename=Path(video_path).name)
                await bot.send_video(chat_id=chat_id, video=vf, caption=caption, parse_mode="HTML")
            else:
                vf = FSInputFile(video_path, filename=Path(video_path).name)
                await bot.send_document(chat_id=chat_id, document=vf, caption=caption, parse_mode="HTML")

            if srt_path and os.path.isfile(srt_path):
                sf = FSInputFile(srt_path, filename=f"subtitles_{Path(srt_path).name}")
                await bot.send_document(chat_id=chat_id, document=sf, caption="📝 <b>Myanmar Subtitles (SRT)</b>", parse_mode="HTML")

        finally:
            await bot.session.close()
    except Exception as e:
        logger.warning(f"Failed to send recap video to Telegram chat {chat_id}: {e}")


async def _run_recap_background_job(job_id: str, req: RecapConfigRequest, user_id: int, chat_id: int, is_vip: bool = False):
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

        selected_voice = "my-MM-ThihaNeural"
        if req.voice_slug in ("mahnin", "nilar", "mamay", "manadi", "mayoon", "machaw", "mathiri"):
            selected_voice = "my-MM-NilarNeural"
        else:
            selected_voice = "my-MM-ThihaNeural"

        logo_pos_map = {
            "top-right": [0.82, 0.04],
            "top-left": [0.04, 0.04],
            "bottom-right": [0.82, 0.88],
            "bottom-left": [0.04, 0.88],
            "top-center": [0.42, 0.04],
        }
        logo_pos_coords = logo_pos_map.get(req.logo_pos, [0.82, 0.04])

        loop = asyncio.get_running_loop()
        from app.services.downloader import extract_first_url, unshorten_url
        clean_source_url = extract_first_url(req.url) or req.url.strip()
        canonical_source_url = unshorten_url(clean_source_url)

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
        script_path = None

        if srt_path and Path(srt_path).is_file():
            try:
                script_file = job_dir / f"{job_id}_script.txt"
                lines = []
                with open(srt_path, "r", encoding="utf-8", errors="replace") as f:
                    for l in f:
                        s = l.strip()
                        if s and not s.isdigit() and "-->" not in s and s not in lines:
                            lines.append(s)
                with open(script_file, "w", encoding="utf-8") as f:
                    f.write(f"🎬 Recap Script: {req.story_style}\n\n" + "\n\n".join(lines))
                script_path = str(script_file)
            except Exception:
                pass

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
            try:
                from aiogram import Bot
                from aiogram.client.default import DefaultBotProperties
                from aiogram.enums import ParseMode
                from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
                app_url = getattr(settings, "mini_app_url", "https://voice-clone-studio-c1er.onrender.com")
                clean_title = entry.get("title") or "Movie Recap Video"
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🎬 Mini App တွင် ကြည့်ရှုမည်",
                            web_app=WebAppInfo(url=f"{app_url}?tab=tab-recap")
                        ),
                        InlineKeyboardButton(
                            text="📱 Video ဖိုင် ပို့ပေးပါ",
                            callback_data=f"send_recap_{job_id}"
                        )
                    ]
                ])
                notify_text = (
                    f"🎉 <b>Pro Movie Recap ဖန်တီးမှု ပြီးစီးပါပြီ!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎬 <b>ခေါင်းစဉ်:</b> <b>{html.escape(str(clean_title[:60]))}</b>\n"
                    f"🎙️ <b>အသံ:</b> {html.escape(str(req.voice_slug.capitalize()))}\n"
                    f"⏱️ <b>Format:</b> Full HD 1080p • မြန်မာပြန် + စာတန်းထိုး\n"
                    f"🆔 <b>Task:</b> <code>#{job_id[:8]}</code>\n\n"
                    f"✨ သင့် Recap ဗီဒီယို အဆင်သင့်ဖြစ်ပါပြီခင်ဗျာ။ Mini App ကို ပိတ်ထားခဲ့လျှင်ပင် "
                    f"Mini App တွင် ဖွင့်ကြည့်နိုင်သလို Telegram သို့ တိုက်ရိုက်ရယူနိုင်ပါသည်!"
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
                u = await session.get(User, user_id)
                if u and not settings.is_admin(u.id):
                    u.recap_credits = (getattr(u, "recap_credits", 0) or 0) + 1
                await session.commit()
        except Exception as ref_err:
            logger.warning(f"Credit refund failed: {ref_err}")


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
):
    """Submit 1-Click Pro Recap Job with complete ratio, audio, subtitle, and anti-copyright settings."""
    is_vip = (user.account_category == AccountCategory.PURCHASED.value) or settings.is_admin(user.id)
    credits = getattr(user, "recap_credits", 0)

    if not is_vip and credits <= 0:
        raise HTTPException(
            status_code=403,
            detail="Recap Video ဖန်တီးရန် Credit မလုံလောက်တော့ပါ။ ကျေးဇူးပြု၍ Movie Recap Plan ရယူပါရန်။",
        )

    # Free user duration limit: Max 2 minutes (120s); VIP: Max 10 minutes (600s)
    max_duration_allowed = 600 if is_vip else getattr(settings, "recap_free_max_duration_seconds", 120)
    if req.duration_seconds and req.duration_seconds > max_duration_allowed:
        dur_mins = req.duration_seconds // 60
        dur_secs = req.duration_seconds % 60
        max_m = max_duration_allowed // 60
        tier_label = "VIP" if is_vip else "အခမဲ့ (Free)"
        raise HTTPException(
            status_code=400,
            detail=f"ဗီဒီယို ကြာချိန် ({dur_mins:02d}:{dur_secs:02d}) သည် {tier_label} အသုံးပြုသူ ကန့်သတ်ချက် (အများဆုံး {max_m} မိနစ်) ထက် ကျော်လွန်နေပါသည်။ "
                   + ("ပိုရှည်သော ဗီဒီယိုများ (၁၀ မိနစ်အထိ) ပြုလုပ်လိုပါက VIP သို့ အဆင့်မြှင့်တင်ပါခင်ဗျာ။" if not is_vip else ""),
        )

    # Deduct credit if non-admin
    if not settings.is_admin(user.id) and hasattr(user, "recap_credits") and user.recap_credits > 0:
        user.recap_credits -= 1
        await db.commit()

    job_id = f"rc_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    RECAP_JOBS_REGISTRY[job_id] = {
        "id": job_id,
        "user_id": user.id,
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
            input_text=req.url,
            seconds_estimated=120,
        )
        db.add(db_job)
        await db.commit()
    except Exception as err_db:
        logger.warning(f"Could not persist recap Job record in DB: {err_db}")

    # Start background task with VIP tier flag for watermark control
    asyncio.create_task(_run_recap_background_job(job_id, req, user.id, user.id, is_vip=is_vip))

    return {
        "success": True,
        "recap_id": job_id,
        "status": "QUEUED",
        "story_style": req.story_style,
        "ratio": req.ratio,
        "remaining_credits": getattr(user, "recap_credits", 0),
        "message": "AI Myanmar Recap ဖန်တီးမှု စတင်လုပ်ဆောင်နေပါပြီခင်ဗျာ!",
    }

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
):
    """Poll recap job status, progress percentage, and deliverables."""
    entry = RECAP_JOBS_REGISTRY.get(job_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Recap job not found")

    return {
        "id": job_id,
        "status": entry.get("status", "PENDING"),
        "progress_percent": entry.get("progress_percent", 0),
        "progress_step": entry.get("progress_step", ""),
        "progress_label": entry.get("progress_label", ""),
        "error_message": entry.get("error_message"),
        "download_urls": entry.get("download_urls", {}),
    }


@api_router.get("/recap/stream/{job_id}/{file_type}")
@api_router.get("/recap/download/{job_id}/{file_type}")
async def download_recap_file(
    job_id: str,
    file_type: str,
    request: Request,
):
    """Download or stream generated recap MP4 video, MP3 audio, SRT subtitles, or TXT script."""
    entry = RECAP_JOBS_REGISTRY.get(job_id)
    file_path = None
    if entry:
        path_key = f"{file_type}_path"
        file_path = entry.get(path_key)

    # Fallback to checking storage directory directly if in-memory entry evicted
    if not file_path or not Path(file_path).is_file():
        job_dir = settings.storage_root / "recap_output" / job_id
        if file_type == "video":
            cands = list(job_dir.glob("*.mp4"))
            if cands: file_path = str(cands[0])
        elif file_type == "audio":
            cands = list(job_dir.glob("*.mp3")) + list(job_dir.glob("*.wav"))
            if cands: file_path = str(cands[0])
        elif file_type == "srt":
            cands = list(job_dir.glob("*.srt"))
            if cands: file_path = str(cands[0])
        elif file_type == "script":
            cands = list(job_dir.glob("*script*.txt")) + list(job_dir.glob("*.txt"))
            if cands: file_path = str(cands[0])

    if not file_path or not Path(file_path).is_file():
        try:
            from app.db.session import AsyncSessionLocal
            async with AsyncSessionLocal() as session:
                j = await session.get(Job, job_id)
                if j and j.result_path and Path(j.result_path).is_file():
                    file_path = j.result_path
        except Exception:
            pass

    if not file_path or not Path(file_path).is_file():
        raise HTTPException(status_code=404, detail=f"{file_type.capitalize()} file not ready or not found")

    mime_map = {
        "video": ("video/mp4", f"recap_{job_id[:8]}.mp4"),
        "audio": ("audio/mpeg", f"voiceover_{job_id[:8]}.mp3"),
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
):
    """Deliver generated recap video and assets directly to user's Telegram chat on demand."""
    entry = RECAP_JOBS_REGISTRY.get(job_id)
    video_path = entry.get("video_path") if entry else None
    audio_path = entry.get("audio_path") if entry else None
    srt_path = entry.get("srt_path") if entry else None
    script_path = entry.get("script_path") if entry else None

    if not video_path or not Path(video_path).is_file():
        job_dir = settings.storage_root / "recap_output" / job_id
        if job_dir.is_dir():
            cands = list(job_dir.glob("*.mp4"))
            if cands:
                video_path = str(cands[0])
            auds = list(job_dir.glob("*.mp3")) + list(job_dir.glob("*.wav"))
            if auds:
                audio_path = str(auds[0])
            srts = list(job_dir.glob("*.srt"))
            if srts:
                srt_path = str(srts[0])

    if not video_path or not Path(video_path).is_file():
        raise HTTPException(status_code=404, detail="ဗီဒီယို ဖိုင် အဆင်သင့်မဖြစ်သေးပါ သို့မဟုတ် ရှာမတွေ့ပါခင်ဗျာ။")

    await _send_recap_telegram_delivery(
        chat_id=user.id,
        video_path=video_path,
        audio_path=audio_path,
        srt_path=srt_path,
        script_path=script_path,
        title="AI Movie Recap",
        style="Pro Dubbed",
        voice_name="Smart Sync AI",
        duration_sec=0,
    )
    return {"success": True, "message": "Telegram သို့ ဗီဒီယို ပို့ပြီးပါပြီခင်ဗျာ!"}


@api_router.post("/voices/{job_id}/send-telegram")
async def send_voice_to_telegram(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """Deliver synthesized voice audio directly to user's Telegram chat on demand."""
    db_job = await db.get(Job, job_id)
    audio_path = None
    if db_job and db_job.result_path and Path(db_job.result_path).is_file():
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
            download_url = f"/api/audio/stream/{j.id}" if j.status == JobStatus.SUCCESS.value else None
            deliverables = {
                "audio": f"/api/audio/stream/{j.id}",
                "download": f"/api/audio/download/{j.id}",
            }

        out.append({
            "id": j.id,
            "job_type": "RECAP" if is_recap else "VOICE_CLONE",
            "engine": j.engine,
            "preset_voice": j.preset_voice or ("Thiha" if is_recap else "Standard"),
            "status": j.status,
            "input_snippet": (j.input_text[:70] + "...") if j.input_text and len(j.input_text) > 70 else (j.input_text or ""),
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "time_ago": time_ago,
            "download_url": download_url,
            "deliverables": deliverables,
            "error_message": j.error_message,
        })

    return {
        "success": True,
        "count": len(out),
        "history": out,
        "ttl_hours": 24,
    }


@api_router.get("/admin/history")
async def get_admin_global_history(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """
    Admin Only: View all users' generated jobs (and admin's own) within the last 24 hours.
    """
    if not settings.is_admin(user.id):
        raise HTTPException(status_code=403, detail="Admin access required")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    stmt = (
        select(Job, User.username, User.first_name)
        .outerjoin(User, Job.user_id == User.id)
        .where(Job.created_at >= cutoff)
        .order_by(Job.created_at.desc())
        .limit(100)
    )
    res = await db.execute(stmt)
    rows = res.all()

    total_recap = 0
    total_voice = 0
    out = []

    for job, username, first_name in rows:
        is_recap = (job.job_type == JobType.RECAP.value or job.id.startswith("rc_"))
        if is_recap:
            total_recap += 1
        else:
            total_voice += 1

        created_ts = job.created_at.timestamp() if job.created_at else time.time()
        mins_ago = int((time.time() - created_ts) // 60)
        time_ago = f"{mins_ago}m ago" if mins_ago < 60 else f"{mins_ago // 60}h ago"

        deliverables = {}
        download_url = None
        if is_recap:
            download_url = f"/api/recap/stream/{job.id}/video" if job.status == JobStatus.SUCCESS.value else None
            deliverables = {
                "video": f"/api/recap/stream/{job.id}/video",
                "audio": f"/api/recap/stream/{job.id}/audio",
                "srt": f"/api/recap/download/{job.id}/srt",
                "script": f"/api/recap/download/{job.id}/script",
            }
        else:
            download_url = f"/api/audio/stream/{job.id}" if job.status == JobStatus.SUCCESS.value else None
            deliverables = {
                "audio": f"/api/audio/stream/{job.id}",
                "download": f"/api/audio/download/{job.id}",
            }

        out.append({
            "id": job.id,
            "user_id": job.user_id,
            "username": username or "Unknown",
            "first_name": first_name or "",
            "job_type": "RECAP" if is_recap else "VOICE_CLONE",
            "engine": job.engine,
            "preset_voice": job.preset_voice or ("Thiha" if is_recap else "Standard"),
            "status": job.status,
            "input_snippet": (job.input_text[:70] + "...") if job.input_text and len(job.input_text) > 70 else (job.input_text or ""),
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "time_ago": time_ago,
            "download_url": download_url,
            "deliverables": deliverables,
            "error_message": job.error_message,
        })

    return {
        "success": True,
        "total_24h": len(out),
        "total_voice": total_voice,
        "total_recap": total_recap,
        "history": out,
    }


@api_router.post("/admin/cleanup-24h")
async def trigger_admin_cleanup(
    user: User = Depends(get_current_user),
):
    """Admin Only: Manually purge all expired jobs and disk files older than 24 hours."""
    if not settings.is_admin(user.id):
        raise HTTPException(status_code=403, detail="Admin access required")
    count = await cleanup_expired_jobs_24h()
    return {
        "success": True,
        "purged_count": count,
        "message": f"၂၄ နာရီကျော်လွန်သော ဖိုင်နှင့် မှတ်တမ်း {count} ခုကို အောင်မြင်စွာ ရှင်းလင်းပြီးပါပြီ။",
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
                "id": "voice_studio_pro",
                "name": "🎙️ Voice Clone Studio Pro" if is_my else "🎙️ Voice Clone Studio Pro",
                "category": "voice",
                "allocation": "300 Usable Minutes (5 Hours)" if is_my else "300 Usable Minutes (5 Hours)",
                "price": "25,000",
                "currency": "MMK",
                "features": [
                    "VoxCPM Cloud AI အသံ ၁၂ မျိုး အသုံးပြုခွင့်" if is_my else "12 VoxCPM Cloud AI Personas (6M / 6F)",
                    "Custom Voice Clone (အသံဖိုင် Upload တင်၍ တူအောင်ပွားခွင့်)" if is_my else "Custom Voice Clone (Audio Sample Upload)",
                    "Applio RVC Local GPU မော်ဒယ်များ အသုံးပြုခွင့်" if is_my else "Applio RVC Local GPU Trained Models",
                    "High-Speed Priority Cloud Processing Queue" if is_my else "High-Speed Priority Cloud Processing Queue",
                    "အရည်အသွေးမြင့် 24kHz PCM WAV တိုက်ရိုက်ဒေါင်းလုဒ်" if is_my else "Direct 24kHz PCM WAV Audio Downloads",
                ],
                "badge": "Voice Creators 🔥",
            },
            {
                "id": "recap_studio_pro",
                "name": "🎬 Movie Recap Studio Pro" if is_my else "🎬 Movie Recap Studio Pro",
                "category": "recap",
                "allocation": "60 Movie Recap Credits (အများဆုံး ၁၀ မိနစ်/ကား)" if is_my else "60 Movie Recap Credits (Max 10 mins/video)",
                "price": "35,000",
                "currency": "MMK",
                "features": [
                    "Full HD 1080p Movie Recap ဗီဒီယို (၆၀) ကား (အများဆုံး ၁၀ မိနစ်/ကား)" if is_my else "60 Full HD 1080p Movie Recap Generations (Max 10 mins/video)",
                    "ဇာတ်လမ်းစတိုင် ၅ မျိုး (Viral Hook, Dramatic, POV စသဖြင့်)" if is_my else "5 Narration Styles (Viral Hook, Dramatic, POV)",
                    "စာတန်းထိုးစတိုင် ၄ မျိုး (Classic Gold, White, Dark Box, Neon)" if is_my else "4 Subtitle Presets (Gold, White, Dark Box, Neon)",
                    "မူရင်းစာတန်းဖျက် Smart Blur (Y, H စိတ်ကြိုက်ချိန်ညှိ)" if is_my else "Anti-Copyright Custom Blur Box (Y, H Sliders)",
                    "Channel Watermark Logo ထည့်သွင်းခွင့်" if is_my else "Custom Channel Logo Watermark Integration",
                ],
                "badge": "Video Editors 🎬",
            },
            {
                "id": "all_in_one_master",
                "name": "👑 All-in-One Master Suite" if is_my else "👑 All-in-One Master Suite",
                "category": "bundle",
                "allocation": "600 Mins + 150 Recap Credits (အများဆုံး ၁၀ မိနစ်/ကား)" if is_my else "600 Mins + 150 Recap Credits (Max 10 mins/video)",
                "price": "55,000",
                "currency": "MMK",
                "features": [
                    "Voice Clone Studio Pro အပြည့်အစုံ (မိနစ် ၆၀၀ - ၁၀ နာရီ)" if is_my else "Full Voice Clone Studio Pro (600 Mins)",
                    "Movie Recap Studio Pro အပြည့်အစုံ (ဗီဒီယို ၁၅၀ ကား - အများဆုံး ၁၀ မိနစ်/ကား)" if is_my else "Full Movie Recap Studio Pro (150 Videos - Max 10 mins/video)",
                    "မိမိကိုယ်ပိုင် Clone Voice Model (၁) ခု အခမဲ့ Train ပေးခြင်း" if is_my else "1 Free Custom Trained Voice Model Slot",
                    "Dedicated High-Speed Processing Server" if is_my else "Dedicated High-Speed Cloud Node Priority",
                    "Commercial Copyright Safe & 24/7 VIP Direct Support" if is_my else "Commercial Copyright Safe & 24/7 VIP Support",
                ],
                "badge": "အကောင်းဆုံး တန်ဖိုး 💎" if is_my else "Best Value Master 💎",
            },
        ],
        "payment_accounts": [
            {
                "provider": "KBZPay",
                "number": getattr(settings, "payment_kpay_number", "09790760003") or "09790760003",
                "name": getattr(settings, "payment_kpay_name", "Win Kyaw Oo") or "Win Kyaw Oo",
                "color": "#0047BA",
            },
            {
                "provider": "WavePay",
                "number": getattr(settings, "payment_wave_number", "09790760003") or "09790760003",
                "name": getattr(settings, "payment_wave_name", "Win Kyaw Oo") or "Win Kyaw Oo",
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
):
    """Submit a payment transfer receipt screenshot for VIP upgrade."""
    receipts_dir = settings.storage_root / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)

    ext = Path(slip_file.filename).suffix or ".jpg"
    clean_name = f"slip_{user.id}_{uuid.uuid4().hex[:8]}{ext}"
    dest_path = receipts_dir / clean_name

    content = await slip_file.read()
    with open(dest_path, "wb") as f:
        f.write(content)

    if "recap" in package_code:
        minutes = 0
    elif "all_in_one" in package_code or "master" in package_code:
        minutes = 600
    else:
        minutes = 300

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
):
    """Serve uploaded payment receipt image."""
    clean_fn = Path(filename).name
    p = settings.storage_root / "receipts" / clean_fn
    if not p.is_file():
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

    queued_applio_res = await db.execute(select(func.count(Job.id)).where(Job.status == JobStatus.QUEUED.value, Job.engine == "applio"))
    queued_applio = queued_applio_res.scalar() or 0

    worker_info = await _get_laptop_worker_status(db)

    return {
        "total_users": total_users,
        "vip_users": vip_users,
        "pending_slips": pending_slips,
        "queued_jobs": queued_jobs,
        "running_jobs": running_jobs,
        "queued_applio_jobs": queued_applio,
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
    and by engine (all, applio, voxcpm, recap).
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
            cfg = RecapConfigRequest(
                url=job.input_text or "",
                voice_slug=job.preset_voice or "thiha",
            )
            RECAP_JOBS_REGISTRY[job_id] = {
                "id": job_id,
                "status": "QUEUED",
                "progress_percent": 5,
                "progress_step": "0/4",
                "progress_label": "🔄 Re-queued by Administrator...",
            }
            asyncio.create_task(_run_recap_background_job(job_id, cfg, job.user_id, job.chat_id or job.user_id))
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


@api_router.get("/admin/settings")
async def get_admin_settings(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """Get all dynamic platform settings."""
    from app.services.dynamic_settings import load_dynamic_settings
    await load_dynamic_settings(db)
    return {
        "payment_kpay_number": getattr(settings, "payment_kpay_number", "09790760003"),
        "payment_kpay_name": getattr(settings, "payment_kpay_name", "Win Kyaw Oo"),
        "payment_wave_number": getattr(settings, "payment_wave_number", "09790760003"),
        "payment_wave_name": getattr(settings, "payment_wave_name", "Win Kyaw Oo"),
        "tutorial_video_link": getattr(settings, "tutorial_video_link", "https://t.me/VoiceClonePro/16"),
        "tutorial_video_enabled": getattr(settings, "tutorial_video_enabled", True),
        "tutorial_voice_title": getattr(settings, "tutorial_voice_title", "Voice Clone ပြုလုပ်နည်း လမ်းညွှန်"),
        "tutorial_voice_link": getattr(settings, "tutorial_voice_link", "https://t.me/VoiceClonePro/16"),
        "tutorial_voice_enabled": getattr(settings, "tutorial_voice_enabled", True),
        "tutorial_recap_title": getattr(settings, "tutorial_recap_title", "Movie Recap ပြုလုပ်နည်း လမ်းညွှန်"),
        "tutorial_recap_link": getattr(settings, "tutorial_recap_link", "https://t.me/VoiceClonePro/16"),
        "tutorial_recap_enabled": getattr(settings, "tutorial_recap_enabled", True),
        "maintenance_mode": getattr(settings, "maintenance_mode", False),
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
        if "recap" in p_req.package_code:
            recap_added = 60
            target_user.recap_credits = (getattr(target_user, "recap_credits", 0) or 0) + 60
        elif "all_in_one" in p_req.package_code or "master" in p_req.package_code:
            recap_added = 150
            target_user.recap_credits = (getattr(target_user, "recap_credits", 0) or 0) + 150

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
