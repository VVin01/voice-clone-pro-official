from __future__ import annotations

import logging
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.enums import AccountCategory, JobStatus, JobType
from app.models.schema import Job, User, utc_now
from app.services.ledger import get_minute_balance, get_or_create_user
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
    text: str,
    edge_voice: str,
    model_id: int,
    gender_preset: str = "m2m",
    speed_preset: str = "normal",
    chat_id: int | None = None,
    status_message_id: int | None = None,
) -> tuple[Job | None, str]:
    """Validate character limits, balance, model authorization and enqueue job."""
    user, _ = await get_or_create_user(session, user_id)

    # 1. Validate model authorization
    model = await get_authorized_model(session, user_id, model_id)
    if not model:
        return None, "MODEL_NOT_AUTHORIZED"

    # 2. Check character limits defense-in-depth
    is_admin = settings.is_admin(user_id)
    if is_admin:
        if len(text) > settings.infrastructure_safety_character_limit:
            return None, "TEXT_EXCEEDS_INFRASTRUCTURE_LIMIT"
    else:
        allowed_chars = (
            settings.character_limit_purchased
            if user.account_category == AccountCategory.PURCHASED.value
            else settings.character_limit_free
        )
        if len(text) > allowed_chars:
            return None, "TEXT_EXCEEDS_CHARACTER_LIMIT"

    # 3. Check balance (admins have unlimited access and do not require balance)
    if not is_admin:
        balance = await get_minute_balance(session, user_id)
        if balance.total_usable_seconds <= 0:
            return None, "INSUFFICIENT_BALANCE"

    job = Job(
        user_id=user_id,
        chat_id=chat_id or user_id,
        status_message_id=status_message_id,
        status=JobStatus.QUEUED.value,
        job_type=JobType.VOICE_CLONE.value,
        input_text=text,
        edge_voice=edge_voice,
        model_id=model_id,
        gender_preset=gender_preset,
        speed_preset=speed_preset,
    )
    session.add(job)
    await session.flush()

    logger.info("Created voice clone job %s for user %s (model=%s)", job.id, user_id, model.name)
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


async def claim_next_queued_job(session: AsyncSession) -> Job | None:
    """Find the oldest QUEUED job and atomically transition it to RUNNING.

    Guarantees exactly ONE competing worker claims and executes the job.
    """
    candidate_stmt = (
        select(Job.id)
        .where(Job.status == JobStatus.QUEUED.value)
        .order_by(Job.created_at)
        .limit(1)
    )
    candidate_id = (await session.execute(candidate_stmt)).scalar_one_or_none()
    if not candidate_id:
        return None

    claimed = await claim_job_for_running(session, candidate_id)
    if not claimed:
        return None

    return await session.get(Job, candidate_id)

