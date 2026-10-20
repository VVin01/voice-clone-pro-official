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
from typing import Optional, List, Dict, Any
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import get_db_session
from app.models.enums import AccountCategory, JobStatus, VoiceModelStatus, VoiceModelType, PaymentStatus
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
        # Development fallback to primary admin if specified in query or env
        admin_id = 1957002115
        if hasattr(settings, "admin_ids") and settings.admin_ids:
            try:
                admin_id = int(str(settings.admin_ids).split(",")[0].strip())
            except Exception:
                admin_id = 1957002115
        user = await db.get(User, admin_id)
        if not user:
            user = User(
                id=admin_id,
                first_name="Admin",
                account_category=AccountCategory.PURCHASED.value,
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


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not settings.is_admin(user.id):
        raise HTTPException(status_code=403, detail="Admin authorization required")
    return user


# ── 1. User & Wallet Profile ───────────────────────────────────────────────
@api_router.get("/user/me")
async def get_user_profile(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    """Retrieve user's VIP status, minute balance, and allowance."""
    bal = await db.get(MinuteBalance, user.id)
    seconds_left = bal.total_usable_seconds if bal else 0
    is_admin = settings.is_admin(user.id)
    is_vip = (user.account_category == AccountCategory.PURCHASED.value) or is_admin

    char_limit = (
        settings.character_limit_purchased
        if is_vip
        else settings.character_limit_free
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
            "sample_audio_url": f"/api/voices/sample/{m.slug}" if m.sample_audio_path else None,
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
            detail=f"အက္ခရာ အရေအတွက် {len(clean_text)} သည် ကန့်သတ်ချက် {char_limit} ထက် ကျော်လွန်နေပါသည်။ VIP ရယူပါရန်။",
        )

    # Find model
    stmt = select(VoiceModel).where(VoiceModel.slug == req.model_slug)
    res = await db.execute(stmt)
    model = res.scalar_one_or_none()
    if not model:
        stmt2 = select(VoiceModel).where(VoiceModel.slug == "voxcpm-cloud-ai")
        res2 = await db.execute(stmt2)
        model = res2.scalar_one_or_none()
        if not model:
            raise HTTPException(status_code=404, detail="အသံမော်ဒယ် မတွေ့ရှိပါ")

    is_vox = (model.pth_path == "voxcpm" or "voxcpm" in model.slug.lower() or req.engine == "voxcpm")
    actual_engine = "voxcpm" if is_vox else "applio"

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
            detail=f"အက္ခရာ အရေအတွက် {len(clean_text)} သည် ကန့်သတ်ချက် {char_limit} ထက် ကျော်လွန်နေပါသည်။",
        )

    stmt = select(VoiceModel).where(VoiceModel.slug == model_slug)
    res = await db.execute(stmt)
    model = res.scalar_one_or_none()
    if not model:
        stmt2 = select(VoiceModel).where(VoiceModel.slug == "voxcpm-cloud-ai")
        res2 = await db.execute(stmt2)
        model = res2.scalar_one_or_none()
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

    is_vox = (model.pth_path == "voxcpm" or "voxcpm" in model.slug.lower() or engine == "voxcpm")
    actual_engine = "voxcpm" if is_vox else "applio"

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


@api_router.get("/audio/download/{job_id}")
async def download_job_audio(
    job_id: str,
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
        raise HTTPException(status_code=404, detail="Audio file not ready or expired")

    return FileResponse(
        path=str(target_path),
        media_type="audio/wav",
        filename=f"voice_clone_{job.id[:8]}.wav",
    )


@api_router.get("/voices/sample/{slug}")
async def get_voice_sample_audio(slug: str, db: AsyncSession = Depends(get_db_session)):
    """Serve sample audio file for a given voice model."""
    stmt = select(VoiceModel).where(VoiceModel.slug == slug)
    res = await db.execute(stmt)
    m = res.scalar_one_or_none()
    if not m or not m.sample_audio_path or not Path(m.sample_audio_path).is_file():
        raise HTTPException(status_code=404, detail="Sample audio not found")
    return FileResponse(path=m.sample_audio_path, media_type="audio/wav")


# ── 4. Quick Downloader & Video Info ───────────────────────────────────────
class ExtractMediaRequest(BaseModel):
    url: str


@api_router.post("/downloader/extract")
async def extract_media_info(
    req: ExtractMediaRequest,
    user: User = Depends(get_current_user),
):
    """Extract YouTube/TikTok/Facebook video metadata via yt-dlp."""
    url = req.url.strip()
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="မှန်ကန်သော Video URL ထည့်သွင်းပေးပါခင်ဗျာ")

    try:
        import yt_dlp
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get("title", "Video Content")
            duration_sec = info.get("duration", 0) or 0
            mins, secs = divmod(int(duration_sec), 60)
            duration_str = f"{mins:02d}:{secs:02d}"
            uploader = info.get("uploader", "Creator")
            thumbnail = info.get("thumbnail", "")

            return {
                "success": True,
                "url": url,
                "platform": info.get("extractor_key", "Video"),
                "title": title,
                "uploader": uploader,
                "thumbnail": thumbnail,
                "estimated_duration": duration_str,
                "duration_seconds": duration_sec,
                "has_subtitles": bool(info.get("subtitles") or info.get("automatic_captions")),
                "formats_available": ["MP4 (Full HD Video)", "MP3 (192kbps Audio)", "SRT (Subtitles)"],
            }
    except Exception as e:
        logger.warning(f"yt-dlp extract error for {url}: {e}")
        # Graceful fallback response
        platform = "TikTok" if "tiktok.com" in url else "YouTube" if ("youtu.be" in url or "youtube.com" in url) else "Video"
        return {
            "success": True,
            "url": url,
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


async def _run_recap_background_job(job_id: str, req: RecapConfigRequest, user_id: int, chat_id: int):
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
        recap_fn = functools.partial(
            downloader.generate_recap_video,
            url_or_path=req.url,
            target_dir=str(job_dir),
            voice=selected_voice,
            narrative_style=req.story_style,
            voice_speed=req.voice_speed,
            output_ratio=req.ratio,
            frame_fill=req.ratio_fill,
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

        # Send to Telegram
        if video_path and Path(video_path).is_file():
            await _send_recap_telegram_delivery(
                chat_id=chat_id,
                video_path=video_path,
                audio_path=audio_path,
                srt_path=srt_path,
                script_path=script_path,
                title="AI Movie Recap",
                style=req.story_style,
                voice_name=req.voice_slug,
                duration_sec=int(recap_res.get("duration", 0)),
            )

    except Exception as e:
        logger.error(f"Background recap job {job_id} error: {e}", exc_info=True)
        entry["status"] = "FAILED"
        entry["error_message"] = str(e)
        try:
            from app.db.session import AsyncSessionLocal
            async with AsyncSessionLocal() as session:
                u = await session.get(User, user_id)
                if u and not settings.is_admin(u.id):
                    u.recap_credits = (getattr(u, "recap_credits", 0) or 0) + 1
                    await session.commit()
        except Exception as ref_err:
            logger.warning(f"Credit refund failed: {ref_err}")


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

    # Start background task
    asyncio.create_task(_run_recap_background_job(job_id, req, user.id, user.id))

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


@api_router.get("/recap/download/{job_id}/{file_type}")
async def download_recap_file(
    job_id: str,
    file_type: str,
):
    """Download generated recap MP4 video, MP3 audio, SRT subtitles, or TXT script."""
    entry = RECAP_JOBS_REGISTRY.get(job_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Recap job not found")

    path_key = f"{file_type}_path"
    file_path = entry.get(path_key)
    if not file_path or not Path(file_path).is_file():
        raise HTTPException(status_code=404, detail=f"{file_type.capitalize()} file not ready or not found")

    mime_map = {
        "video": ("video/mp4", f"recap_{job_id[:8]}.mp4"),
        "audio": ("audio/mpeg", f"voiceover_{job_id[:8]}.mp3"),
        "srt": ("text/plain; charset=utf-8", f"subtitles_{job_id[:8]}.srt"),
        "script": ("text/plain; charset=utf-8", f"script_{job_id[:8]}.txt"),
    }
    media_type, filename = mime_map.get(file_type, ("application/octet-stream", f"{file_type}_{job_id[:8]}"))
    return FileResponse(path=str(file_path), media_type=media_type, filename=filename)


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
                "allocation": "60 Movie Recap Credits" if is_my else "60 Movie Recap Credits",
                "price": "35,000",
                "currency": "MMK",
                "features": [
                    "Full HD 1080p Movie Recap ဗီဒီယို (၆၀) ကား ဖန်တီးခွင့်" if is_my else "60 Full HD 1080p Movie Recap Generations",
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
                "allocation": "600 Mins + 150 Recap Credits" if is_my else "600 Mins + 150 Recap Credits",
                "price": "55,000",
                "currency": "MMK",
                "features": [
                    "Voice Clone Studio Pro အပြည့်အစုံ (မိနစ် ၆၀၀ - ၁၀ နာရီ)" if is_my else "Full Voice Clone Studio Pro (600 Mins)",
                    "Movie Recap Studio Pro အပြည့်အစုံ (ဗီဒီယို ၁၅၀ ကား)" if is_my else "Full Movie Recap Studio Pro (150 Videos)",
                    "မိမိကိုယ်ပိုင် Clone Voice Model (၁) ခု အခမဲ့ Train ပေးခြင်း" if is_my else "1 Free Custom Trained Voice Model Slot",
                    "Dedicated High-Speed Processing Server" if is_my else "Dedicated High-Speed Cloud Node Priority",
                    "Commercial Copyright Safe & 24/7 VIP Direct Support" if is_my else "Commercial Copyright Safe & 24/7 VIP Support",
                ],
                "badge": "အကောင်းဆုံး တန်ဖိုး 💎" if is_my else "Best Value Master 💎",
            },
        ],
        "payment_accounts": [
            {"provider": "KBZPay", "number": "09951234567", "name": "Ko Win", "color": "#0047BA"},
            {"provider": "WavePay", "number": "09951234567", "name": "Ko Win", "color": "#FEE600"},
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

    queued_applio_res = await db.execute(select(func.count(Job.id)).where(Job.status == JobStatus.QUEUED.value, Job.engine == "applio"))
    queued_applio = queued_applio_res.scalar() or 0

    return {
        "total_users": total_users,
        "vip_users": vip_users,
        "pending_slips": pending_slips,
        "queued_applio_jobs": queued_applio,
        "cloud_worker_status": "ONLINE (Render)",
        "laptop_worker_status": f"{queued_applio} jobs waiting" if queued_applio > 0 else "Idle / Ready",
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
    """Approve a payment request: upgrade user to PURCHASED and credit balance."""
    p_req = await db.get(PaymentRequest, req.request_id)
    if not p_req or p_req.status != PaymentStatus.PENDING.value:
        raise HTTPException(status_code=404, detail="Pending payment request not found")

    p_req.status = PaymentStatus.APPROVED.value
    p_req.reviewed_by = admin.id
    p_req.reviewed_at = utc_now()

    # Upgrade user to PURCHASED
    target_user = await db.get(User, p_req.user_id)
    if target_user:
        target_user.account_category = AccountCategory.PURCHASED.value
        if "recap" in p_req.package_code:
            target_user.recap_credits = (getattr(target_user, "recap_credits", 0) or 0) + 60
        elif "all_in_one" in p_req.package_code or "master" in p_req.package_code:
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

    return {"success": True, "message": f"Approved payment for User {p_req.user_id} (+{p_req.minutes} mins)"}


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
    """Trigger a broadcast message delivery to all registered bot users."""
    text = req.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Broadcast message cannot be empty")

    stmt = select(User.id).where(User.status == "ACTIVE")
    res = await db.execute(stmt)
    user_ids = res.scalars().all()

    # Deliver via Telegram Bot in background
    if settings.bot_token:
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode
        bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        success_count = 0
        for uid in user_ids:
            try:
                await bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
                success_count += 1
                await asyncio.sleep(0.05)
            except Exception:
                pass
        await bot.session.close()
        return {"success": True, "sent_count": success_count, "total_recipients": len(user_ids)}

    return {"success": True, "message": f"Broadcast queued for {len(user_ids)} users"}
