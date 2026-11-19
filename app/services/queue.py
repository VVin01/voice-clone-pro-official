from __future__ import annotations

import logging
import math
import uuid
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.enums import AccountCategory, JobStatus, JobType
from app.models.schema import Job, User, VoiceModel, utc_now
from app.services.billing import (
    BILLING_MODE_ADMIN_FREE,
    BILLING_MODE_VOICE_ONLY,
    BillingError,
    reserve_for_job,
)
from app.services.ledger import get_minute_balance, get_or_create_user, grant_monthly_free_allowance
from app.services.models import get_authorized_model
from app.services.speed import (
    DEFAULT_SPEED,
    normalize_api_speed,
    resolve_speed_tempo,
)

logger = logging.getLogger(__name__)

# Submit-time duration estimate for the F6 pre-check below.
#
# The old version divided by a flat 20 chars/sec and ignored the
# speed preset entirely. Worker-side production measurements (see
# _implausible_audio_reason in app/worker.py) put the primary engine
# at ≈17.6 chars/sec, so the 20 chars/sec divisor UNDER-estimated
# real durations by ~14% before the preset was even considered:
# jobs whose text "fit" a 180s balance at submit went on to produce
# 197s / 209s of audio and failed at post-generation charge time —
# after a full synthesis, with nothing delivered (production errors
# "Insufficient balance: requested 197s / 209s, available 180s",
# 2026-11-03). The estimate now starts from the measured rate,
# scales it by the job's speed-preset factor, and adds a 15% safety
# margin, so a job that cannot fit the balance is rejected at submit
# with the same mapped insufficient-balance detail. consume_seconds
# remains the billing source of truth.
MEASURED_CHARS_PER_SECOND = 17.6
PRE_CHECK_SAFETY_MARGIN = 1.15


def speed_preset_factor(speed_preset) -> float:
    """Pace multiplier for a speed preset resolved from the unified speed system."""
    return resolve_speed_tempo(speed_preset)


def estimate_voice_job_seconds(text: str, speed_preset=DEFAULT_SPEED) -> float:
    """Estimated spoken duration (seconds) for a voice job's text,
    including the pre-check safety margin."""
    chars = len((text or "").strip())
    if chars <= 0:
        return 0.0
    rate = MEASURED_CHARS_PER_SECOND * speed_preset_factor(speed_preset)
    return chars / rate * PRE_CHECK_SAFETY_MARGIN


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
    speed_preset: str = DEFAULT_SPEED,
    chat_id: int | None = None,
    status_message_id: int | None = None,
    source_audio_path: str | None = None,
    engine: str = "voxcpm",
    preset_voice: str | None = None,
) -> tuple[Job | None, str]:
    """Validate character limits, balance, model authorization and enqueue job."""
    from pathlib import Path

    speed_preset = normalize_api_speed(speed_preset) or DEFAULT_SPEED

    # Encoding repair at intake (hunt #4 A1): Zawgyi-typed text is
    # converted to Unicode BEFORE every length/balance check below, so
    # the character counts (and therefore billing) always match the
    # text that will actually be spoken, and the Job row stores the
    # Unicode form. Genuine Unicode passes through byte-identical;
    # this is the single choke point shared by the bot and Mini App.
    from app.services.myanmar_text import convert_zawgyi_to_unicode

    text = convert_zawgyi_to_unicode(text or "")

    # Invariant (X1 follow-up, hunt #3 E4): preset_voice is stored on
    # the Job row and rendered in the admin Mini App, so only curated
    # persona slugs may ever reach storage. The webapp endpoints
    # pre-validate, but the check lives HERE, in the service, so no
    # present or future caller can store an arbitrary string (the
    # stored-XSS payload class). Bot callers pass None and are
    # unaffected; the webapp translates this ValueError into its
    # clean 400.
    if preset_voice is not None:
        from app.services.curated_voices import get_curated_voice

        if get_curated_voice(preset_voice) is None:
            raise ValueError(f"Unknown preset_voice: {preset_voice!r}")

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
    if model is not None and model.pth_path != "voxcpm":
        # A granted custom model the production engine cannot run
        # (legacy grant from before assign started refusing these —
        # hunt V1): the job falls back to the cloud voice below. The
        # behaviour is unchanged for those rows, but the remap is no
        # longer SILENT — it is logged with the model and user ids.
        logger.warning(
            "Custom voice model %s (slug=%s, pth_path=%r) is not runnable by the "
            "production engine; voice job for user %s falls back to the cloud voice",
            model.id, model.slug, model.pth_path, user_id,
        )
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
        # Serialize per-user job creation (hunt #7): both limit checks
        # below are count-then-insert, so two simultaneous submits
        # (bot + Mini App, or a double-tap) could each count the
        # pre-insert state and both create a job — defeating the
        # concurrent-job limit and the FREE daily cap at once. Locking
        # the creator's own User row makes concurrent creators queue
        # up on Postgres; whoever loses the race then counts the
        # winner's committed job and is rejected properly. The lock is
        # held until the caller's transaction commits (the job insert
        # below is part of the same transaction).
        await session.execute(
            select(User).where(User.id == user_id).with_for_update()
        )
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

        # Batch AJ L14: purchased_daily_job_limit existed as a
        # setting for years but was never read — an admin setting it
        # changed nothing. It is now enforced for PURCHASED users
        # exactly like the free limit (0 keeps meaning "unlimited",
        # so existing deployments are unaffected).
        if user.account_category == AccountCategory.PURCHASED.value and settings.purchased_daily_job_limit > 0:
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
            if daily_count >= settings.purchased_daily_job_limit:
                return None, "DAILY_JOB_LIMIT_EXCEEDED"

        # Daily voice-MINUTES cap (Batch AN, owner order 2026-11-05):
        # the job-count caps above throttle job frequency, but the
        # scarce resource behind voice is HF caller quota measured in
        # audio time — one user submitting a few very long jobs could
        # still burn the pool in a day. The cap is split by tier
        # (FREE 5 min/day, PURCHASED 30 min/day by default; 0 =
        # unlimited for that tier) and counts today's VOICE_CLONE jobs
        # only, inside the same per-user lock and with the same UTC
        # day boundary as the count checks: SUCCESS jobs count their
        # recorded actual seconds (seconds_consumed — the billing
        # source of truth the worker writes on completion), falling
        # back to the submit-time estimate when no actual was recorded
        # (the same consumed→estimated fallback the bot history
        # display uses); QUEUED/RUNNING jobs count their estimate;
        # FAILED and CANCELLED jobs consume nothing; SUCCESS jobs the
        # worker marked engine="edge_fallback" also consume nothing
        # (Batch CH: no clone minutes were charged for them, so they
        # must not eat the cap via the estimate fallback either).
        # Aggregation is per-row in Python (no func.sum under app/,
        # Batch R rule).
        _limit_minutes = (
            settings.voice_daily_limit_minutes_paid
            if user.account_category == AccountCategory.PURCHASED.value
            else settings.voice_daily_limit_minutes_free
        )
        if _limit_minutes > 0:
            today_start = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
            _limit_seconds = _limit_minutes * 60
            _minute_stmt = (
                select(Job)
                .where(
                    Job.user_id == user_id,
                    Job.job_type == JobType.VOICE_CLONE.value,
                    Job.created_at >= today_start,
                    Job.status.in_(
                        [
                            JobStatus.SUCCESS.value,
                            JobStatus.QUEUED.value,
                            JobStatus.RUNNING.value,
                        ]
                    ),
                )
            )
            _consumed_seconds = 0.0
            for _prev in (await session.execute(_minute_stmt)).scalars().all():
                if _prev.status == JobStatus.SUCCESS.value and _prev.engine == "edge_fallback":
                    # Batch CH (Item 2): a voice job delivered via the
                    # Edge fallback consumed no voice minutes, so it
                    # must not eat the daily cap either. Its
                    # seconds_consumed is 0, which the estimate
                    # fallback below would otherwise resurrect as a
                    # full submit-time estimate.
                    continue
                if _prev.status == JobStatus.SUCCESS.value and (_prev.seconds_consumed or 0) > 0:
                    _consumed_seconds += float(_prev.seconds_consumed)
                else:
                    _consumed_seconds += estimate_voice_job_seconds(
                        _prev.input_text or "", _prev.speed_preset
                    )
            _new_estimate = estimate_voice_job_seconds(text or "", speed_preset)
            if _consumed_seconds + _new_estimate > _limit_seconds:
                logger.info(
                    "Voice job daily-minutes cap rejected for user %s: %.1fs consumed + ~%.1fs new > %ss limit",
                    user_id,
                    _consumed_seconds,
                    _new_estimate,
                    _limit_seconds,
                )
                return None, "DAILY_MINUTE_LIMIT_EXCEEDED"

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

    # 4. Check balance & reserve (admins have unlimited access and do not require balance)
    estimated_seconds = 0
    if not is_admin:
        await grant_monthly_free_allowance(session, user_id)
        balance = await get_minute_balance(session, user_id)
        # Batch AW: clone jobs check the CLONE-SPENDABLE balance —
        # referral seconds are TTS-only and no longer count here.
        if balance.clone_spendable_seconds <= 0:
            return None, "INSUFFICIENT_BALANCE"

        if valid_text:
            estimated_seconds = max(1, int(math.ceil(estimate_voice_job_seconds(valid_text, speed_preset))))
            if estimated_seconds > balance.clone_spendable_seconds:
                logger.info(
                    "Voice job pre-check rejected for user %s: ~%ss estimated vs %ss clone-spendable",
                    user_id,
                    int(estimated_seconds),
                    balance.clone_spendable_seconds,
                )
                return None, "INSUFFICIENT_BALANCE"

    if not edge_voice:
        if preset_voice:
            from app.services.curated_voices import get_curated_voice
            p_info = get_curated_voice(preset_voice)
            if p_info:
                edge_voice = p_info.get("edge_voice") or ("my-MM-NilarNeural" if p_info.get("gender") == "female" else "my-MM-ThihaNeural")
        if not edge_voice:
            edge_voice = "my-MM-NilarNeural" if gender_preset in ("m2f", "f2f") else "my-MM-ThihaNeural"

    job_id = str(uuid.uuid4())
    try:
        await reserve_for_job(
            session,
            user_id=user_id,
            job_id=job_id,
            job_type=JobType.VOICE_CLONE.value,
            billing_mode=BILLING_MODE_ADMIN_FREE if is_admin else BILLING_MODE_VOICE_ONLY,
            reserve_recap_seconds=0,
            reserve_voice_seconds=0 if is_admin else estimated_seconds,
            note=f"Voice clone job reserve ({estimated_seconds}s)",
        )
    except BillingError as exc:
        logger.info("Voice job reserve rejected for user %s: %s", user_id, exc)
        return None, "INSUFFICIENT_BALANCE"

    job = Job(
        id=job_id,
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
        seconds_estimated=estimated_seconds,
    )
    session.add(job)
    await session.flush()

    logger.info("Created voice clone job %s for user %s (engine=%s, model=%s, est=%ss, source_audio=%s)", job.id, user_id, actual_engine, model.name, estimated_seconds, bool(source_audio_path))
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


async def claim_next_queued_job(
    session: AsyncSession,
    engine_filter: str | None = None,
    *,
    skip_local_source: bool | None = None,
    allow_recap: bool | None = None,
) -> Job | None:
    """Find the oldest QUEUED job matching engine_filter and atomically transition it to RUNNING.

    Guarantees exactly ONE competing worker claims and executes the job.

    When ``skip_local_source`` is True (or unset and settings.worker_claim_local_files
    is False, e.g. WORKER_CLAIM_LOCAL_FILES=0 on a VPS second worker), jobs
    whose ``source_audio_path`` is a local filesystem path (or recap jobs with
    local source / own-voice reference audio) are skipped so the primary instance
    claims them. URL-sourced recaps using persona narration and portable voice jobs
    are claimable by any worker.
    """
    if skip_local_source is None:
        skip_local_source = not getattr(settings, "worker_claim_local_files", True)
    if allow_recap is None:
        allow_recap = getattr(settings, "recap_on_queue", False)

    query = select(
        Job.id,
        Job.source_audio_path,
        Job.job_type,
        Job.engine,
        Job.recap_ref_audio_path,
    ).where(
        Job.status == JobStatus.QUEUED.value,
        # Batch AW: TTS jobs execute in the webapp process (their own
        # background runner), never in the voice-clone worker — keep
        # them out of this queue even for an engine_filter=None caller.
        Job.job_type != JobType.TTS.value,
    )
    if not allow_recap:
        query = query.where(
            Job.job_type != JobType.RECAP.value,
            Job.engine != "recap",
            ~Job.id.startswith("rc_"),
        )
    if engine_filter:
        if allow_recap:
            query = query.where(
                (Job.engine == engine_filter)
                | (Job.job_type == JobType.RECAP.value)
                | (Job.engine == "recap")
                | (Job.id.startswith("rc_"))
            )
        else:
            query = query.where(Job.engine == engine_filter)

    if skip_local_source:
        query = query.where(
            (Job.source_audio_path.is_(None))
            | (Job.source_audio_path == "")
            | (Job.source_audio_path.startswith("http://"))
            | (Job.source_audio_path.startswith("https://"))
        )

    candidate_stmt = query.order_by(Job.created_at).limit(10)
    rows = (await session.execute(candidate_stmt)).all()
    if not rows:
        return None

    for row in rows:
        candidate_id, src_path, j_type, eng, ref_path = row[0], row[1], row[2], row[3], row[4]
        is_recap = (
            j_type == JobType.RECAP.value
            or eng == "recap"
            or str(candidate_id).startswith("rc_")
        )
        if is_recap:
            if skip_local_source and not _is_recap_portable(src_path, ref_path):
                logger.debug(
                    "Skipping recap job %s: not portable for clean-disk worker",
                    candidate_id,
                )
                continue
        else:
            if skip_local_source and _is_local_source(src_path):
                logger.debug(
                    "Skipping job %s: source_audio_path is a local file "
                    "and this instance has skip_local_source=True",
                    candidate_id,
                )
                continue
        claimed = await claim_job_for_running(session, candidate_id)
        if not claimed:
            continue
        return await session.get(Job, candidate_id)

    return None


def _is_recap_portable(source_path: str | None, ref_path: str | None) -> bool:
    """Return True if a recap job is portable to a remote/clean-disk worker.

    A recap is portable when its source is a URL (http/https) AND its narration
    voice needs no local reference file (ref_path is None or empty, e.g. persona voice).
    """
    if not source_path or not (source_path.startswith("http://") or source_path.startswith("https://")):
        return False
    if ref_path and str(ref_path).strip():
        return False
    return True


def _is_local_source(path: str | None) -> bool:
    """Return True when *path* looks like a local filesystem path
    (not a URL and not empty/None).  URL-sourced and preset-voice
    jobs store NULL or a URL string and are portable across instances.
    """
    if not path or not path.strip():
        return False
    p = path.strip()
    # URLs are portable
    if p.startswith("http://") or p.startswith("https://"):
        return False
    return True

