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
        model = await get_authorized_model(session, job.user_id, job.model_id)
        if not model or not Path(model.pth_path).is_file():
            raise RuntimeError("Model is missing or not authorized")

        tracker = JobProgressTracker(
            bot=bot,
            chat_id=target_chat_id,
            message_id=target_msg_id,
            model_name=model.name,
            lang=lang,
        )
        await tracker.update_step(1)

        job.status = JobStatus.RUNNING.value
        job.started_at = utc_now()
        await session.flush()

        # Stage 2: Generate base audio (Edge TTS) & Normalize
        await tracker.update_step(2)
        base_tts_path = temp_dir / "base.mp3"
        await generate_edge_tts(
            text=job.input_text or "",
            out_path=base_tts_path,
            voice=job.edge_voice or "my-MM-ThihaNeural",
            speed=job.speed_preset or "normal",
        )
        norm_wav_path = temp_dir / "norm.wav"
        await normalize_audio_to_wav(base_tts_path, norm_wav_path)

        # Stage 3: Run Applio Voice Conversion with Live Ticker
        out_filename = get_output_filename(model.name)
        converted_wav_path = temp_dir / out_filename
        pitch = GENDER_PITCH_MAP.get(job.gender_preset or "m2m", 0)

        await tracker.update_step(3)
        await tracker.start_ticker(step=3)
        try:
            await run_applio_conversion(
                model_pth=Path(model.pth_path),
                index_path=Path(model.index_path) if model.index_path else None,
                input_wav=norm_wav_path,
                output_wav=converted_wav_path,
                pitch=pitch,
            )
        finally:
            await tracker.stop_ticker()

        # Stage 4: Preparing Output and Deducting Balance
        await tracker.update_step(4)
        duration_seconds = get_wav_duration_seconds(converted_wav_path)

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

        # Send completed audio to user
        if bot and target_chat_id:
            audio_file = FSInputFile(str(converted_wav_path), filename=out_filename)
            caption = t(
                "status_completed_caption",
                lang,
                completed_status=t("status_completed", lang),
                model_name=html.escape(str(model.name or "Standard")),
                duration_seconds=duration_seconds,
            )
            sent_msg = await bot.send_audio(
                chat_id=target_chat_id,
                audio=audio_file,
                caption=caption,
                parse_mode="HTML",
            )
            if sent_msg and getattr(sent_msg, "audio", None):
                job.result_path = sent_msg.audio.file_id
            await safe_edit_status(bot, target_chat_id, target_msg_id, t("status_completed", lang))

        job.status = JobStatus.SUCCESS.value
        job.finished_at = utc_now()
        await session.flush()

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

        safe_exc = html.escape(str(exc))
        await safe_edit_status(bot, target_chat_id, target_msg_id, f"{t('status_failed', lang)}\n<i>({safe_exc})</i>")
        return False

    finally:
        if tracker:
            await tracker.stop_ticker()
        # Safe cleanup of intermediate files
        shutil.rmtree(temp_dir, ignore_errors=True)


async def claim_and_process_next_job(
    session: AsyncSession,
    bot: Bot | None = None,
) -> bool:
    """Atomically claim the oldest QUEUED job and process it."""
    from app.services.queue import claim_next_queued_job

    job = await claim_next_queued_job(session)
    if not job:
        return False

    return await process_single_job(session=session, job=job, bot=bot)


async def run_worker_loop(bot: Bot | None = None, poll_interval: float = 2.0) -> None:
    """Continuously poll for QUEUED jobs and process them via atomic claim."""
    logger.info("Starting background audio processing worker loop...")

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
                processed = await claim_and_process_next_job(session, bot=bot)
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
