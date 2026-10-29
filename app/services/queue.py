from __future__ import annotations

import logging
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.enums import AccountCategory, JobStatus, JobType
from app.models.schema import Job, User, VoiceModel, utc_now
from app.services.ledger import get_minute_balance, get_or_create_user, grant_monthly_free_allowance
from app.services.models import get_authorized_model

logger = logging.getLogger(__name__)


async def get_queue_position(session: AsyncSession, job_id: str) -> int:
    """Calculate truthful queue position: count of active jobs created before this job."""
    job = await session.get(Job, job_id)
    if not job:
        return 0

    count_stmt = select(func.count()).select_from(Job).where(
        Job.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
        Job.created_at < job.created_at,
    )
    res = await session.execute(count_stmt)
    ahead_count = res.scalar() or 0
    return ahead_count + 1


async def create_voice_clone_job(
    session: AsyncSession,
    user_id: int,
    text: str | None = None,
    edge_voice: str | None = None,
    model_id: int | None = None,
    gender_preset: str = "m2m",
    speed_preset: str = "normal",
    chat_id: int | None = None,
    status_message_id: int | None = None,
    source_audio_path: str | None = None,
    engine: str = "voxcpm",
    preset_voice: str | None = None,
) -> tuple[Job | None, str]:
    """Validate character limits, balance, model authorization and enqueue job."""
    from pathlib import Path

    user, _ = await get_or_create_user(session, user_id)

    # 1. Validate model authorization & engine resolution.
    # VoxCPM2 is the only voice-clone engine. Any engine name other than
    # "voxcpm" is a legacy-engine request and is rejected here; bot/API
    # layers already give a user-facing legacy-removed message, and
    # default voxcpm callers are mapped to VoxCPM2 below.
    if engine not in ("voxcpm", "", None):
        return None, "LEGACY_MODEL_ENGINE_REMOVED"
    model = None
    if model_id is not None:
        model = await get_authorized_model(session, user_id, model_id)
    if not model or model.pth_path != "voxcpm":
        stmt_v = select(VoiceModel).where(VoiceModel.slug == "voxcpm-cloud-ai")
        res_v = await session.execute(stmt_v)
        vox_m = res_v.scalar_one_or_none()
        if vox_m:
            model = vox_m
            model_id = vox_m.id
    actual_engine = "voxcpm"

    # 2. Check concurrency and daily job limits (admins exempt)
    is_admin = settings.is_admin(user_id)
    if not is_admin:
        # Concurrent job limit (1 job)
        concurrent_stmt = (
            select(func.count())
            .select_from(Job)
            .where(
                Job.user_id == user_id,
                Job.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
            )
        )
        active_jobs = (await session.execute(concurrent_stmt)).scalar() or 0
        if active_jobs >= settings.max_concurrent_jobs_per_user:
            return None, "CONCURRENT_JOB_LIMIT_EXCEEDED"

        # Daily job limit for FREE users
        if user.account_category == AccountCategory.FREE.value and settings.free_daily_job_limit > 0:
            today_start = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
            daily_stmt = (
                select(func.count())
                .select_from(Job)
                .where(
                    Job.user_id == user_id,
                    Job.created_at >= today_start,
                    Job.status != JobStatus.CANCELLED.value,
                )
            )
            daily_count = (await session.execute(daily_stmt)).scalar() or 0
            if daily_count >= settings.free_daily_job_limit:
                return None, "DAILY_JOB_LIMIT_EXCEEDED"

    # 3. Check character limits and audio file existence defense-in-depth
    valid_text = (text or "").strip()
    if valid_text:
        if is_admin:
            if len(valid_text) > settings.infrastructure_safety_character_limit:
                return None, "TEXT_EXCEEDS_INFRASTRUCTURE_LIMIT"
        else:
            allowed_chars = (
                settings.character_limit_purchased
                if user.account_category == AccountCategory.PURCHASED.value
                else settings.character_limit_free
            )
            if len(valid_text) > allowed_chars:
                return None, "TEXT_EXCEEDS_CHARACTER_LIMIT"
    if source_audio_path and not Path(source_audio_path).is_file():
        return None, "AUDIO_FILE_NOT_FOUND"

    # 4. Check balance (admins have unlimited access and do not require balance)
    if not is_admin:
        await grant_monthly_free_allowance(session, user_id)
        balance = await get_minute_balance(session, user_id)
        if balance.total_usable_seconds <= 0:
            return None, "INSUFFICIENT_BALANCE"

    import uuid
    if not edge_voice:
        if preset_voice:
            from app.services.curated_voices import get_curated_voice
            p_info = get_curated_voice(preset_voice)
            if p_info:
                edge_voice = p_info.get("edge_voice") or ("my-MM-NilarNeural" if p_info.get("gender") == "female" else "my-MM-ThihaNeural")
        if not edge_voice:
            edge_voice = "my-MM-NilarNeural" if gender_preset in ("m2f", "f2f") else "my-MM-ThihaNeural"

    job = Job(
        id=str(uuid.uuid4()),
        user_id=user_id,
        chat_id=chat_id or user_id,
        status_message_id=status_message_id,
        status=JobStatus.QUEUED.value,
        job_type=JobType.VOICE_CLONE.value,
        engine=actual_engine,
        preset_voice=preset_voice,
        input_text=text,
        source_audio_path=source_audio_path,
        edge_voice=edge_voice,
        model_id=model_id,
        gender_preset=gender_preset,
        speed_preset=speed_preset,
    )
    session.add(job)
    await session.flush()

    logger.info("Created voice clone job %s for user %s (engine=%s, model=%s, source_audio=%s)", job.id, user_id, actual_engine, model.name, bool(source_audio_path))
    return job, "QUEUED"


async def claim_job_for_running(session: AsyncSession, job_id: str) -> bool:
    """Atomically transition a job from QUEUED to RUNNING.

    Returns True if this caller exclusively claimed the job.
    Returns False if the job was already claimed, completed, failed, or cancelled.
    """
    from sqlalchemy import update
    stmt = (
        update(Job)
        .where(Job.id == job_id, Job.status == JobStatus.QUEUED.value)
        .values(status=JobStatus.RUNNING.value, started_at=utc_now())
    )
    res = await session.execute(stmt)
    await session.commit()
    return res.rowcount == 1


async def claim_next_queued_job(session: AsyncSession, engine_filter: str | None = None) -> Job | None:
    """Find the oldest QUEUED job matching engine_filter and atomically transition it to RUNNING.

    Guarantees exactly ONE competing worker claims and executes the job.
    """
    query = select(Job.id).where(
        Job.status == JobStatus.QUEUED.value,
        Job.job_type != JobType.RECAP.value,
        Job.engine != "recap",
        ~Job.id.startswith("rc_"),
    )
    if engine_filter:
        query = query.where(Job.engine == engine_filter)

    candidate_stmt = query.order_by(Job.created_at).limit(1)
    candidate_id = (await session.execute(candidate_stmt)).scalar_one_or_none()
    if not candidate_id:
        return None

    claimed = await claim_job_for_running(session, candidate_id)
    if not claimed:
        return None

    return await session.get(Job, candidate_id)

