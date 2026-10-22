from __future__ import annotations

import asyncio
import html
import logging
from pathlib import Path
import shutil
from aiogram import Bot
from aiogram.types import FSInputFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.models.enums import JobStatus
from app.models.schema import AuditLog, Job, User, VoiceModel, utc_now
from app.services.audio import (
    GENDER_PITCH_MAP,
    generate_edge_tts,
    get_output_filename,
    get_wav_duration_seconds,
    normalize_audio_to_wav,
    run_applio_conversion,
)
from app.services.i18n import t
from app.services.ledger import consume_seconds
from app.services.models import get_authorized_model
from app.services.progress import JobProgressTracker, render_stepper_progress, safe_edit_status

logger = logging.getLogger(__name__)


async def sync_output_to_cloud(job_id: str, file_path: Path) -> None:
    """Upload generated audio file from Local Worker to Render Cloud so Mini App & Bot can serve it."""
    import os
    cloud_url = os.getenv("RENDER_EXTERNAL_URL", "").strip() or os.getenv("MINI_APP_URL", "").strip() or "https://voice-clone-studio-c1er.onrender.com"
    if not file_path or not file_path.is_file():
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=90.0) as client:
            with open(file_path, "rb") as f:
                files = {"file": (file_path.name, f, "audio/wav")}
                headers = {"X-Worker-Token": settings.bot_token}
                url = f"{cloud_url}/api/internal/worker/upload-result/{job_id}"
                resp = await client.post(url, files=files, headers=headers)
                if resp.is_success:
                    logger.info("✅ Audio result for job %s successfully uploaded to Render Cloud server!", job_id)
                else:
                    logger.warning("Cloud upload for job %s returned status %s: %s", job_id, resp.status_code, resp.text)
    except Exception as e:
        logger.warning("Failed to sync output to cloud for job %s: %s", job_id, e)


async def process_single_job(
    session: AsyncSession,
    job: Job,
    bot: Bot | None = None,
    chat_id: int | None = None,
    status_msg_id: int | None = None,
) -> bool:
    """Execute complete voice clone job lifecycle with safe temp cleanup and idempotency."""
    # Terminal state check: never re-run completed or failed jobs
    if job.status in (JobStatus.SUCCESS.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value):
        logger.warning("Job %s is already in terminal state %s; skipping.", job.id, job.status)
        return False

    temp_dir = settings.storage_root / "temp" / str(job.id)
    temp_dir.mkdir(parents=True, exist_ok=True)

    user = await session.get(User, job.user_id)
    lang = user.language_code if user else "my"
    target_chat_id = chat_id or job.chat_id or job.user_id
    target_msg_id = status_msg_id or job.status_message_id

    tracker: JobProgressTracker | None = None
    try:
        # Stage 1: Validation
        is_voxcpm = (
            getattr(job, "engine", "") in ("voxcpm", "fish_audio", "fishaudio")
            or getattr(job, "preset_voice", None) is not None
        )

        model = None
        if job.model_id is not None:
            model = await get_authorized_model(session, job.user_id, job.model_id)
            if not model:
                model = await session.get(VoiceModel, job.model_id)

        if not model and is_voxcpm:
            stmt_v = select(VoiceModel).where(VoiceModel.slug == "voxcpm-cloud-ai")
            res_v = await session.execute(stmt_v)
            model = res_v.scalar_one_or_none()

        if not model:
            if is_voxcpm:
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
                session.add(model)
                await session.flush()
            else:
                model = await session.get(VoiceModel, job.model_id) if job.model_id else None
                if not model:
                    raise RuntimeError("Model is missing or not authorized")

        if not is_voxcpm:
            is_voxcpm = (
                getattr(model, "pth_path", "") == "voxcpm"
                or "voxcpm" in getattr(model, "slug", "").lower()
            )

        def _resolve_model_path(pth_str: str | None) -> Path | None:
            if not pth_str:
                return None
            p = Path(pth_str)
            if p.is_file():
                return p
            cand = Path(settings.root) / p
            if cand.is_file():
                return cand
            for found in Path(settings.storage_root).rglob(p.name):
                if found.is_file():
                    return found
            return None

        resolved_model_pth = _resolve_model_path(getattr(model, "pth_path", None)) if not is_voxcpm else None
        resolved_index_path = _resolve_model_path(getattr(model, "index_path", None)) if not is_voxcpm else None

        if not is_voxcpm and not resolved_model_pth:
            raise RuntimeError("Model file (.pth) is missing or not authorized on this server")

        display_name = model.name or "Standard"
        if getattr(job, "preset_voice", None):
            try:
                from app.services.curated_voices import get_curated_voice
                p_info = get_curated_voice(job.preset_voice)
                if p_info:
                    display_name = f"VoxCPM ({p_info.get('name_my' if lang == 'my' else 'name_en')})"
            except Exception:
                pass

        tracker = JobProgressTracker(
            bot=bot,
            chat_id=target_chat_id,
            message_id=target_msg_id,
            model_name=display_name,
            lang=lang,
        )
        await tracker.update_step(1)

        job.status = JobStatus.RUNNING.value
        job.started_at = utc_now()
        await session.commit()

        # Stage 2: Prepare base audio & Normalize
        await tracker.update_step(2)
        norm_wav_path = temp_dir / "norm.wav"
        if not is_voxcpm:
            if getattr(job, "source_audio_path", None) and Path(job.source_audio_path).is_file():
                logger.info("Direct audio input mode for job %s: normalizing %s", job.id, job.source_audio_path)
                await normalize_audio_to_wav(Path(job.source_audio_path), norm_wav_path)
            else:
                base_tts_path = temp_dir / "base.mp3"
                await generate_edge_tts(
                    text=job.input_text or "",
                    out_path=base_tts_path,
                    voice=job.edge_voice or "my-MM-ThihaNeural",
                    speed=job.speed_preset or "normal",
                )
                await normalize_audio_to_wav(base_tts_path, norm_wav_path)

        # Stage 3: Run Applio or VoxCPM Voice Conversion with Live Ticker & Real-time ETA
        out_name = f"voxcpm_{job.preset_voice}" if getattr(job, "preset_voice", None) else model.name
        out_filename = get_output_filename(out_name)
        converted_wav_path = temp_dir / out_filename

        await tracker.update_step(3)
        await tracker.start_ticker(step=3)
        try:
            if is_voxcpm:
                logger.info("Starting Fish Audio AI (s2.1-pro-free) synthesis for job %s", job.id)
                import functools
                from app.services.fish_audio_engine import FishAudioEngine
                engine_inst = FishAudioEngine(work_dir=temp_dir)

                ref_wav = None
                if getattr(job, "source_audio_path", None) and Path(job.source_audio_path).is_file():
                    ref_wav = Path(job.source_audio_path)
                elif getattr(model, "sample_audio_path", None) and Path(model.sample_audio_path).is_file():
                    ref_wav = Path(model.sample_audio_path)

                clean_text = job.input_text or "မင်္ဂလာပါခင်ဗျာ"
                control_instruction = None
                if getattr(job, "preset_voice", None):
                    from app.services.curated_voices import get_curated_voice
                    p_info = get_curated_voice(job.preset_voice)
                    if p_info:
                        control_instruction = p_info.get("control_instruction")
                        logger.info("Using curated persona '%s' for Fish Audio job %s", job.preset_voice, job.id)

                try:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(
                        None,
                        functools.partial(
                            engine_inst.synthesize,
                            text=clean_text,
                            output_wav=converted_wav_path,
                            reference_wav=ref_wav,
                            control_instruction=control_instruction or "A clear, natural, emotionally expressive Burmese speaker with warm tone and fluent pronunciation",
                            preset_voice=getattr(job, "preset_voice", None),
                            speed=job.speed_preset or "normal",
                            progress_cb=tracker.set_custom_note,
                        )
                    )
                except Exception as e_vox:
                    logger.warning("Fish Audio primary synthesis failed (%s). Activating resilient High-Fidelity Neural TTS fallback...", e_vox)
                    try:
                        tracker.set_custom_note("🔄 Fish Audio အချိန်ကုန်နေသဖြင့် အရန် Neural Audio ဖြင့် ဖန်တီးပေးနေပါသည်...")
                    except Exception:
                        pass

                    from app.services.curated_voices import synthesize_curated_persona, get_curated_voice
                    persona_id = getattr(job, "preset_voice", None)
                    if not persona_id or not get_curated_voice(persona_id):
                        is_female = False
                        if persona_id:
                            p_info = get_curated_voice(persona_id)
                            if p_info and p_info.get("gender") == "female":
                                is_female = True
                        persona_id = "mahnin" if is_female else "komin"

                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(
                        None,
                        functools.partial(
                            synthesize_curated_persona,
                            text=clean_text,
                            persona_id=persona_id,
                            output_wav=converted_wav_path,
                            speed=job.speed_preset or "normal",
                        )
                    )
                    logger.info("Resilient fallback audio generated successfully for job %s: %s", job.id, converted_wav_path)
            else:
                pitch = GENDER_PITCH_MAP.get(job.gender_preset or "m2m", 0)
                norm_duration = get_wav_duration_seconds(norm_wav_path)
                computed_timeout = max(
                    getattr(settings, "applio_inference_timeout_seconds", 900),
                    int(norm_duration * 6) + 180,
                )
                logger.info(
                    "Starting Applio conversion for job %s (audio_len=%ss, timeout=%ss)",
                    job.id,
                    norm_duration,
                    computed_timeout,
                )
                await run_applio_conversion(
                    model_pth=resolved_model_pth,
                    index_path=resolved_index_path,
                    input_wav=norm_wav_path,
                    output_wav=converted_wav_path,
                    pitch=pitch,
                    timeout_seconds=computed_timeout,
                )
        finally:
            await tracker.stop_ticker()

        # Stage 4: Preparing Output and Deducting Balance
        await tracker.update_step(4)
        duration_seconds = get_wav_duration_seconds(converted_wav_path)

        # Preserve the generated audio in permanent outputs storage for Mini App & API downloads
        outputs_dir = settings.storage_root / "outputs"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        permanent_wav_path = outputs_dir / f"{job.id}_{out_filename}"
        try:
            shutil.copy2(converted_wav_path, permanent_wav_path)
            job.result_path = str(permanent_wav_path)
        except Exception as copy_err:
            logger.warning("Could not persist generated audio to %s: %s", permanent_wav_path, copy_err)
            job.result_path = str(converted_wav_path)

        # Upload audio result to Render Cloud server so Mini App & Bot can stream / download it
        final_audio_path = permanent_wav_path if permanent_wav_path.is_file() else converted_wav_path
        await sync_output_to_cloud(job.id, final_audio_path)

        # Deduct actual generated duration (idempotent per job.id)
        if not settings.is_admin(job.user_id):
            await consume_seconds(
                session=session,
                user_id=job.user_id,
                seconds_to_consume=duration_seconds,
                job_id=str(job.id),
                note=f"Voice clone generation ({model.name}, {duration_seconds}s)",
            )
        else:
            logger.info("Admin voice job %s completed (%ss); skipping balance deduction.", job.id, duration_seconds)
            session.add(
                AuditLog(
                    admin_id=job.user_id,
                    action="admin.voice_clone",
                    target_type="job",
                    target_id=str(job.id),
                    details_json=f'{{"model": "{model.name}", "duration_seconds": {duration_seconds}}}',
                )
            )

        job.seconds_consumed = duration_seconds

        # Send completion audio or notification alert to user via Telegram Bot
        if bot and target_chat_id:
            caption_model_name = display_name or getattr(model, "name", "Standard")
            if getattr(job, "auto_deliver", False):
                try:
                    audio_source_path = permanent_wav_path if permanent_wav_path.is_file() else converted_wav_path
                    audio_file = FSInputFile(str(audio_source_path), filename=out_filename)
                    caption = t(
                        "status_completed_caption",
                        lang,
                        completed_status=t("status_completed", lang),
                        model_name=html.escape(str(caption_model_name)),
                        duration_seconds=duration_seconds,
                    )
                    await bot.send_audio(
                        chat_id=target_chat_id,
                        audio=audio_file,
                        caption=caption,
                        parse_mode="HTML",
                    )
                    if target_msg_id:
                        await safe_edit_status(bot, target_chat_id, target_msg_id, t("status_completed", lang))
                except Exception as send_err:
                    logger.warning("Failed to deliver audio via Telegram for job %s: %s", job.id, send_err)
            else:
                # Mini App Job: Send push notification alert with inline action buttons
                try:
                    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
                    app_url = getattr(settings, "mini_app_url", "https://voice-clone-studio-c1er.onrender.com")
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🎧 Mini App တွင် နားထောင်မည်",
                                web_app=WebAppInfo(url=f"{app_url}?tab=tab-voice")
                            ),
                            InlineKeyboardButton(
                                text="📱 Audio ဖိုင် ပို့ပေးပါ",
                                callback_data=f"send_audio_{job.id}"
                            )
                        ]
                    ])
                    notify_text = (
                        f"🎉 <b>အသံထုတ်ယူမှု အောင်မြင်စွာ ပြီးစီးပါပြီ!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🎙️ <b>အသံမော်ဒယ်:</b> <b>{html.escape(str(caption_model_name))}</b>\n"
                        f"⏱️ <b>အသံကြာချိန်:</b> <b>{duration_seconds:.1f} စက္ကန့်</b>\n"
                        f"🆔 <b>Task:</b> <code>#{job.id[:8]}</code>\n\n"
                        f"✨ သင့်အသံဖိုင် အဆင်သင့်ဖြစ်နေပါပြီခင်ဗျာ။ Mini App ထဲတွင် ချက်ချင်းဖွင့်နားထောင်နိုင်သလို "
                        f"Telegram သို့ တိုက်ရိုက်ရယူလိုပါက အောက်ပါခလုတ်ကို နှိပ်နိုင်ပါသည်!"
                    )
                    await bot.send_message(
                        chat_id=target_chat_id,
                        text=notify_text,
                        reply_markup=kb,
                        parse_mode="HTML",
                    )
                except Exception as notify_err:
                    logger.warning("Could not send completion alert to user %s: %s", target_chat_id, notify_err)

        job.status = JobStatus.SUCCESS.value
        job.finished_at = utc_now()
        await session.commit()

        logger.info("Successfully completed voice job %s (duration=%ss)", job.id, duration_seconds)
        return True

    except Exception as exc:
        logger.exception("Voice job %s failed: %s", job.id, exc)
        job.status = JobStatus.FAILED.value
        job.error_message = str(exc)
        job.finished_at = utc_now()

        # Refund if any debit occurred for this job
        if not settings.is_admin(job.user_id):
            from app.services.ledger import refund_job_seconds
            await refund_job_seconds(session, job.user_id, str(job.id))

        await session.commit()

        safe_exc = html.escape(str(exc))
        await safe_edit_status(bot, target_chat_id, target_msg_id, f"{t('status_failed', lang)}\n<i>({safe_exc})</i>")
        return False

    finally:
        if tracker:
            await tracker.stop_ticker()
        # Safe cleanup of intermediate files
        shutil.rmtree(temp_dir, ignore_errors=True)
        if getattr(job, "source_audio_path", None):
            try:
                p = Path(job.source_audio_path)
                if p.is_file() and "temp" in str(p):
                    p.unlink(missing_ok=True)
            except Exception:
                pass


async def claim_and_process_next_job(
    session: AsyncSession,
    bot: Bot | None = None,
    engine_filter: str | None = "voxcpm",
) -> bool:
    """Atomically claim the oldest QUEUED job matching engine_filter and process it."""
    from app.services.queue import claim_next_queued_job

    job = await claim_next_queued_job(session, engine_filter=engine_filter)
    if not job:
        return False

    return await process_single_job(session=session, job=job, bot=bot)


async def run_worker_loop(bot: Bot | None = None, poll_interval: float = 2.0, engine_filter: str = "voxcpm") -> None:
    """Continuously poll for QUEUED jobs (defaulting to VoxCPM cloud engine) and process them."""
    logger.info("Starting background audio processing worker loop (engine_filter=%s)...", engine_filter)

    # Startup recovery: sweep orphaned RUNNING jobs before starting polling
    from app.services.recovery import recover_stale_running_jobs
    try:
        async with AsyncSessionLocal() as session:
            recovered = await recover_stale_running_jobs(session=session, bot=bot)
            if recovered:
                logger.info("Recovered %d orphaned stale jobs on worker startup: %s", len(recovered), recovered)
    except Exception as e:
        logger.warning("Error during initial stale job recovery sweep: %s", e)

    while True:
        try:
            async with AsyncSessionLocal() as session:
                processed = await claim_and_process_next_job(session, bot=bot, engine_filter=engine_filter)
                if processed:
                    await session.commit()
                else:
                    await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            logger.info("Worker loop cancelled, shutting down.")
            break
        except Exception as e:
            logger.error("Error in worker loop: %s", e)
            await asyncio.sleep(poll_interval)
