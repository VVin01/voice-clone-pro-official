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
                worker_token = getattr(settings, "worker_token", "") or settings.bot_token
                if not getattr(settings, "worker_token", ""):
                    logger.warning(
                        "WORKER_TOKEN is not set; falling back to the Telegram "
                        "bot token for worker uploads. Set a separate "
                        "WORKER_TOKEN and rotate it."
                    )
                headers = {"X-Worker-Token": worker_token}
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
            getattr(job, "engine", "") == "voxcpm"
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
        if not is_voxcpm:
            raise RuntimeError(
                "Legacy trained voice model is no longer supported. "
                "Please re-clone the voice with a short reference audio using VoxCPM2."
            )

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

        # Stage 3: Run VoxCPM2 Voice Conversion with Live Ticker & Real-time ETA
        out_name = f"voxcpm_{job.preset_voice}" if getattr(job, "preset_voice", None) else model.name
        out_filename = get_output_filename(out_name)
        converted_wav_path = temp_dir / out_filename

        await tracker.update_step(3)
        await tracker.start_ticker(step=3)
        used_fallback = False
        try:
            if is_voxcpm:
                logger.info("Starting VoxCPM 2 Cloud AI synthesis for job %s", job.id)
                import functools
                from app.services.voxcpm_engine import VoxCPMEngine
                engine_inst = VoxCPMEngine(work_dir=temp_dir)

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
                        logger.info("Using curated persona '%s' for VoxCPM 2 job %s", job.preset_voice, job.id)

                if not control_instruction:
                    is_female = (
                        getattr(job, "edge_voice", "") == "my-MM-NilarNeural"
                        or getattr(job, "gender_preset", "") in ("m2f", "f2f")
                    )
                    if is_female:
                        control_instruction = (
                            "A sweet, gentle, natural Burmese female speaker with soft melodious voice, "
                            "clear feminine tone, warm studio acoustics, and fluent authentic native pronunciation."
                        )
                    else:
                        control_instruction = (
                            "A clear, natural, emotionally expressive Burmese male speaker with warm tone, "
                            "deep resonance, velvety cadence, and fluent authentic native pronunciation."
                        )

                try:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(
                        None,
                        functools.partial(
                            engine_inst.synthesize,
                            text=clean_text,
                            output_wav=converted_wav_path,
                            reference_wav=ref_wav,
                            control_instruction=control_instruction,
                            preset_voice=getattr(job, "preset_voice", None),
                            speed=job.speed_preset or "normal",
                            progress_cb=tracker.set_custom_note,
                        )
                    )
                except Exception as e_vox:
                    used_fallback = True
                    logger.warning("VoxCPM 2 primary synthesis failed (%s). Activating resilient High-Fidelity Neural TTS fallback...", e_vox)
                    try:
                        tracker.set_custom_note("⚠️ VoxCPM ZeroGPU Quota ပြည့်သွားသဖြင့် အရန် Neural Audio ဖြင့် ထုတ်ပေးနေပါသည်...")
                    except Exception:
                        pass

                    from app.services.curated_voices import synthesize_curated_persona, get_curated_voice
                    persona_id = getattr(job, "preset_voice", None)
                    if not persona_id or not get_curated_voice(persona_id):
                        is_female = (
                            getattr(job, "edge_voice", "") == "my-MM-NilarNeural"
                            or getattr(job, "gender_preset", "") in ("m2f", "f2f")
                        )
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
                raise RuntimeError(
                    "Legacy trained voice model is no longer supported. "
                    "Please re-clone the voice with a short reference audio using VoxCPM2."
                )
        finally:
            await tracker.stop_ticker()

        # Transparency (fix 2026-10-29): the engine also falls back to
        # Edge TTS internally without raising, so read its flag — the
        # delivery note below must fire for that common path too, not
        # only when the engine raised into the except block above.
        if getattr(engine_inst, "last_fallback_used", False):
            used_fallback = True

        # Stage 4: Preparing Output and Deducting Balance
        await tracker.update_step(4)
        duration_seconds = get_wav_duration_seconds(converted_wav_path)

        # Preserve the generated audio in permanent outputs storage for Mini App & API downloads
        outputs_dir = settings.storage_root / "outputs"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        permanent_wav_path = outputs_dir / f"{job.id}_{out_filename}"
        try:
            if converted_wav_path.resolve() != permanent_wav_path.resolve():
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
        if used_fallback:
            # Persist the fallback flag on the job result so the Mini App
            # job status/history can show the fallback notice as well.
            job.engine = "edge_fallback"

        # Send completion audio or notification alert to user via Telegram Bot
        if bot and target_chat_id:
            caption_model_name = display_name or getattr(model, "name", "Standard")
            fallback_note = t("worker_fallback_note", lang) if used_fallback else ""
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
                    ) + fallback_note
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
                                text=t("clone_btn_miniapp_listen", lang),
                                web_app=WebAppInfo(url=f"{app_url}?tab=tab-voice")
                            ),
                            InlineKeyboardButton(
                                text=t("clone_btn_send_audio", lang),
                                callback_data=f"send_audio_{job.id}"
                            )
                        ]
                    ])
                    notify_text = t(
                        "worker_notify_audio_text",
                        lang,
                        model_name=html.escape(str(caption_model_name)),
                        duration=duration_seconds,
                        job_id=job.id[:8],
                    ) + fallback_note
                    await bot.send_message(
                        chat_id=target_chat_id,
                        text=notify_text,
                        reply_markup=kb,
                        parse_mode="HTML",
                    )
                except Exception as notify_err:
                    logger.warning("Could not send completion alert to user %s: %s", target_chat_id, notify_err)
                # Also flip the job's progress/status card to its completed
                # state. Without this, bot-submitted jobs kept showing the
                # step-4 "in progress" stepper forever, so the chat looked
                # unfinished even though the job had succeeded (the direct
                # delivery branch above already did this edit).
                if target_msg_id:
                    await safe_edit_status(bot, target_chat_id, target_msg_id, t("status_completed", lang))

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
