from __future__ import annotations

import json
import logging
import math
import time
import uuid
from datetime import timezone
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.enums import AccountCategory, JobStatus, JobType
from app.models.schema import Job, User, VoiceModel, utc_now
from app.services import hf_pool_ledger as _hf_ledger
from app.services import voice_choice as _vc
from app.services.billing import (
    BILLING_MODE_ADMIN_FREE,
    BILLING_MODE_VOICE_ONLY,
    BillingError,
    reserve_for_job,
)
from app.services.ledger import (
    get_minute_balance,
    get_or_create_user,
    grant_daily_free_allowance,
    grant_monthly_free_allowance,
)
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

# HF Pool Phase 1 (Part 6 #4): global VoxCPM concurrency cap, 2:1 Paid/Free
# fairness interleaving, and 15-minute anti-starvation ceiling.
MAX_CONCURRENT_VOXCPM_JOBS = 2
MAX_GLOBAL_FREE_PARKED_JOBS = 20
ANTI_STARVATION_SECONDS = 900.0
_paid_dispatch_streak: int = 0


def reset_fairness_for_tests() -> None:
    """Reset the 2:1 Paid/Free interleaving streak counter between unit tests."""
    global _paid_dispatch_streak
    _paid_dispatch_streak = 0


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
        if source_audio_path:
            raise ValueError(
                "VOICE_CLONE cannot set both preset_voice and source_audio_path"
            )

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
        # Concurrent job limit (1 job; includes WAITING_CHOICE for FREE non-admin users)
        _is_free_user = user.account_category == AccountCategory.FREE.value
        _conc_statuses = (
            [
                JobStatus.QUEUED.value,
                JobStatus.RUNNING.value,
                JobStatus.WAITING_CHOICE.value,
            ]
            if _is_free_user
            else [JobStatus.QUEUED.value, JobStatus.RUNNING.value]
        )
        concurrent_stmt = (
            select(func.count())
            .select_from(Job)
            .where(
                Job.user_id == user_id,
                Job.status.in_(_conc_statuses),
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
            _min_statuses = (
                [
                    JobStatus.SUCCESS.value,
                    JobStatus.QUEUED.value,
                    JobStatus.RUNNING.value,
                    JobStatus.WAITING_CHOICE.value,
                ]
                if _is_free_user
                else [
                    JobStatus.SUCCESS.value,
                    JobStatus.QUEUED.value,
                    JobStatus.RUNNING.value,
                ]
            )
            _minute_stmt = (
                select(Job)
                .where(
                    Job.user_id == user_id,
                    Job.job_type == JobType.VOICE_CLONE.value,
                    Job.created_at >= today_start,
                    Job.status.in_(_min_statuses),
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
        await grant_daily_free_allowance(session, user_id)
        balance = await get_minute_balance(session, user_id)
        # Voice Clone jobs check clone_spendable_seconds (today's daily free +
        # legacy monthly free + welcome + referral_voice + purchased).
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

    is_paid_tier = bool(
        is_admin or user.account_category == AccountCategory.PURCHASED.value
    )
    est_audio_float = float(
        estimated_seconds or estimate_voice_job_seconds(valid_text or "", speed_preset)
    )
    from app.services.voice_chunking import is_clone_session_slices_enabled

    est_units, _gate_s, needed_gpu_s = _hf_ledger.estimate_voxcpm_units_and_budget(
        kind="clone",
        text=valid_text or text or "",
    )
    if is_clone_session_slices_enabled():
        admission = _hf_ledger.evaluate_voxcpm_admission(
            kind="clone",
            is_paid=is_paid_tier,
            units=est_units,
            est_audio_seconds=est_audio_float,
            gate_s=_gate_s,
            needed_gpu_s=needed_gpu_s,
        )
    else:
        admission = _hf_ledger.evaluate_voxcpm_admission(
            kind="clone",
            is_paid=is_paid_tier,
            units=est_units,
            est_audio_seconds=est_audio_float,
        )
    if not admission.admitted and not is_paid_tier:
        # Part 6 (#3): Free submit when pool cannot finish -> 0 deducted, no failed Job row!
        logger.info(
            "Free voice clone job rejected at pre-job gate for user %s (reason=%s, band=%s, needed=%.1fs, avail=%.1fs)",
            user_id,
            admission.reason,
            admission.band,
            admission.needed_gpu_s,
            admission.available_gpu_s,
        )
        return None, "VOXCPM_POOL_EXHAUSTED"

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

    initial_status = JobStatus.QUEUED.value
    initial_choice_state: str | None = None
    if not admission.admitted and is_paid_tier:
        # Part 6 (#3) & Part 7: Paid submit when pool cannot finish -> park immediately
        # in WAITING_CHOICE (reservation held, 0 settled, live refill ETA).
        initial_status = JobStatus.WAITING_CHOICE.value
        initial_choice_state = _vc.dump_choice_state(
            _vc.new_choice_state(
                _vc.KIND_CLONE,
                completed_units=0,
                total_units=est_units,
                needed_gpu_s=needed_gpu_s,
                worker_host=_vc.current_worker_host(),
            )
        )

    job = Job(
        id=job_id,
        user_id=user_id,
        chat_id=chat_id or user_id,
        status_message_id=status_message_id,
        status=initial_status,
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
        voice_choice_state=initial_choice_state,
    )
    session.add(job)
    await session.flush()

    logger.info(
        "Created voice clone job %s for user %s (status=%s, engine=%s, model=%s, est=%ss, source_audio=%s)",
        job.id,
        user_id,
        initial_status,
        actual_engine,
        getattr(model, "name", "voxcpm-cloud-ai"),
        estimated_seconds,
        bool(source_audio_path),
    )
    return job, initial_status


def _is_recap_row(job: Job) -> bool:
    return bool(
        job.job_type == JobType.RECAP.value
        or job.engine == "recap"
        or str(job.id).startswith("rc_")
    )


def _recap_uses_voxcpm(job: Job) -> tuple[bool, bool, dict]:
    """Return `(uses_voxcpm, is_script_ready_parked, parsed_recap_state)`."""
    vc_state = _vc.parse_choice_state(getattr(job, "voice_choice_state", None))
    if _vc.choice_mode(vc_state) == _vc.CHOICE_EDGE_ALL:
        return False, False, {}
    raw = getattr(job, "recap_state_json", None)
    parsed: dict = {}
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except Exception:
            parsed = {}
    if parsed.get("script_ready"):
        return False, True, parsed
    cfg = parsed.get("cfg") if isinstance(parsed.get("cfg"), dict) else {}
    tts_eng = str(parsed.get("tts_engine") or cfg.get("tts_engine") or "").strip().lower()
    if tts_eng in ("gemini", "edge", "edge_tts", "none", "subtitle_only", "no_voice"):
        return False, False, parsed
    if tts_eng == "voxcpm":
        return True, False, parsed
    voice_str = str(parsed.get("voice") or cfg.get("voice") or "").strip().lower()
    if voice_str.startswith("voxcpm") or bool(getattr(job, "recap_ref_audio_path", None)):
        return True, False, parsed
    return False, False, parsed


def is_job_voxcpm_bound(job: Job) -> bool:
    """True when `job` actively consumes VoxCPM ZeroGPU slots if RUNNING."""
    if _is_recap_row(job):
        uses_vox, script_ready, _ = _recap_uses_voxcpm(job)
        return bool(uses_vox and not script_ready)
    return bool(job.engine == "voxcpm" or job.job_type == JobType.VOICE_CLONE.value)


async def count_active_voxcpm_jobs(session: AsyncSession) -> int:
    """Count currently `RUNNING` VoxCPM jobs across standalone clone and recap."""
    from sqlalchemy.orm import defer

    running_rows = (
        await session.execute(
            select(Job)
            .options(
                defer(Job.input_text),
                defer(Job.final_script),
                defer(Job.recap_config),
            )
            .where(Job.status == JobStatus.RUNNING.value)
        )
    ).scalars().all()
    return sum(1 for row in running_rows if is_job_voxcpm_bound(row))


async def count_queued_voxcpm_jobs(session: AsyncSession) -> int:
    """Count currently `QUEUED` VoxCPM jobs across standalone clone and recap."""
    from sqlalchemy.orm import defer

    queued_rows = (
        await session.execute(
            select(Job)
            .options(
                defer(Job.input_text),
                defer(Job.final_script),
                defer(Job.recap_config),
            )
            .where(Job.status == JobStatus.QUEUED.value)
        )
    ).scalars().all()
    return sum(1 for row in queued_rows if is_job_voxcpm_bound(row))


def _estimate_job_voxcpm_budget(
    job: Job,
    *,
    text_override: str | None = None,
) -> tuple[int, float, float]:
    """Return `(units, needed_gpu_s, est_audio_seconds)` for queue admission & sorting."""
    vc_state = _vc.parse_choice_state(getattr(job, "voice_choice_state", None))
    if vc_state and float(vc_state.get("needed_gpu_s") or 0.0) > 0:
        tot = max(1, int(vc_state.get("total_units") or 1))
        done = max(0, int(vc_state.get("completed_units") or 0))
        rem_u = max(1, tot - done)
        return (
            rem_u,
            float(vc_state["needed_gpu_s"]),
            float(job.seconds_estimated or 0.0),
        )
    if _is_recap_row(job):
        _uses, _sr, parsed = _recap_uses_voxcpm(job)
        cfg = parsed.get("cfg") if isinstance(parsed.get("cfg"), dict) else {}
        segs = parsed.get("segments_count") or cfg.get("segments_count")
        dur = float(
            parsed.get("duration_seconds")
            or cfg.get("duration_seconds")
            or job.seconds_estimated
            or 60.0
        )
        u, _g, needed = _hf_ledger.estimate_voxcpm_units_and_budget(
            kind="recap",
            planned_segments=int(segs) if segs else None,
            duration_seconds=dur,
        )
        return u, needed, dur
    raw_text = (job.input_text or "") if text_override is None else text_override
    est_s = float(
        job.seconds_estimated
        or estimate_voice_job_seconds(raw_text, job.speed_preset or DEFAULT_SPEED)
    )
    u, _g, needed = _hf_ledger.estimate_voxcpm_units_and_budget(
        kind="clone",
        text=raw_text,
    )
    return u, needed, est_s


def _job_created_epoch(job: Job, fallback_now: float) -> float:
    dt = getattr(job, "created_at", None)
    if dt is None:
        return fallback_now
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


async def count_free_parked_clone_jobs(session: AsyncSession) -> int:
    """Count global `WAITING_CHOICE` standalone voice-clone jobs owned by non-admin FREE users."""
    stmt = (
        select(Job.user_id)
        .join(User, User.id == Job.user_id)
        .where(
            Job.status == JobStatus.WAITING_CHOICE.value,
            Job.job_type == JobType.VOICE_CLONE.value,
            User.account_category == AccountCategory.FREE.value,
        )
    )
    uids = (await session.execute(stmt)).scalars().all()
    return sum(1 for uid in uids if not settings.is_admin(uid))


def _free_resumed_anchor_epoch(job: Job, fallback_now: float) -> float:
    """For Free jobs resumed from `WAITING_CHOICE`, anchor anti-starvation wait at
    `max(created_at, resumed_at)` so woken Free parks do not jump ahead of Paid jobs."""
    c_ts = _job_created_epoch(job, fallback_now)
    raw_vc = getattr(job, "voice_choice_state", None)
    if not raw_vc:
        return c_ts
    st = _vc.parse_choice_state(raw_vc)
    if not st:
        return c_ts
    r_ts = float(st.get("resumed_at") or 0.0)
    return max(c_ts, r_ts) if r_ts > 0.0 else c_ts


def order_voxcpm_fair_queue(
    candidates: list[Job],
    is_paid_by_job_id: dict[str, bool],
    gpu_s_by_job_id: dict[str, float],
    *,
    paid_streak: int | None = None,
    now_ts: float | None = None,
) -> list[Job]:
    """Order queued VoxCPM jobs per Part 6 (#4):
    1. Anti-starvation ceiling first (`wait >= 15 min`, oldest first).
    2. Shortest-remaining-GPU-first within each tier, interleaved 2 Paid : 1 Free.
    """
    now = time.time() if now_ts is None else float(now_ts)
    streak = _paid_dispatch_streak if paid_streak is None else max(0, int(paid_streak))

    starved: list[Job] = []
    paid_norm: list[Job] = []
    free_norm: list[Job] = []

    for j in candidates:
        is_p = bool(is_paid_by_job_id.get(str(j.id), False))
        wait_anchor = _job_created_epoch(j, now) if is_p else _free_resumed_anchor_epoch(j, now)
        if (now - wait_anchor) >= ANTI_STARVATION_SECONDS:
            starved.append(j)
        elif is_p:
            paid_norm.append(j)
        else:
            free_norm.append(j)

    starved.sort(
        key=lambda j: (
            _job_created_epoch(j, now)
            if is_paid_by_job_id.get(str(j.id), False)
            else _free_resumed_anchor_epoch(j, now)
        )
    )
    paid_norm.sort(
        key=lambda j: (
            round(float(gpu_s_by_job_id.get(str(j.id), 0.0)), 2),
            _job_created_epoch(j, now),
        )
    )
    free_norm.sort(
        key=lambda j: (
            round(float(gpu_s_by_job_id.get(str(j.id), 0.0)), 2),
            _job_created_epoch(j, now),
        )
    )

    ordered: list[Job] = list(starved)
    p_idx = 0
    f_idx = 0
    cur_streak = streak
    while p_idx < len(paid_norm) or f_idx < len(free_norm):
        if p_idx < len(paid_norm) and (cur_streak < 2 or f_idx >= len(free_norm)):
            ordered.append(paid_norm[p_idx])
            p_idx += 1
            cur_streak += 1
        elif f_idx < len(free_norm):
            ordered.append(free_norm[f_idx])
            f_idx += 1
            cur_streak = 0
        else:
            break
    return ordered


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


def _queued_job_eligibility_predicates(
    *,
    engine_filter: str | None = None,
    skip_local_source: bool | None = None,
    allow_recap: bool | None = None,
) -> list:
    """Build the exact SQL-level eligibility predicates shared by pre-probe and claim."""
    if skip_local_source is None:
        skip_local_source = not getattr(settings, "worker_claim_local_files", True)
    if allow_recap is None:
        allow_recap = getattr(settings, "recap_on_queue", False)

    preds: list = [
        Job.status == JobStatus.QUEUED.value,
        # Batch AW: TTS jobs execute in the webapp process (their own
        # background runner), never in the voice-clone worker — keep
        # them out of this queue even for an engine_filter=None caller.
        Job.job_type != JobType.TTS.value,
    ]
    if not allow_recap:
        preds.extend(
            [
                Job.job_type != JobType.RECAP.value,
                Job.engine != "recap",
                ~Job.id.startswith("rc_"),
            ]
        )
    if engine_filter:
        if allow_recap:
            preds.append(
                (Job.engine == engine_filter)
                | (Job.job_type == JobType.RECAP.value)
                | (Job.engine == "recap")
                | (Job.id.startswith("rc_"))
            )
        else:
            preds.append(Job.engine == engine_filter)

    if skip_local_source:
        preds.append(
            (Job.source_audio_path.is_(None))
            | (Job.source_audio_path == "")
            | (Job.source_audio_path.startswith("http://"))
            | (Job.source_audio_path.startswith("https://"))
        )
        preds.append(
            (Job.recap_ref_audio_path.is_(None))
            | (Job.recap_ref_audio_path == "")
        )
    return preds


async def has_queued_job_probe(
    session: AsyncSession,
    engine_filter: str | None = None,
    *,
    skip_local_source: bool | None = None,
    allow_recap: bool | None = None,
) -> bool:
    """Lightweight ``SELECT 1 ... LIMIT 1`` existence probe mirroring claim SQL filters."""
    preds = _queued_job_eligibility_predicates(
        engine_filter=engine_filter,
        skip_local_source=skip_local_source,
        allow_recap=allow_recap,
    )
    stmt = select(1).select_from(Job).where(*preds).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def claim_next_queued_job(
    session: AsyncSession,
    engine_filter: str | None = None,
    *,
    skip_local_source: bool | None = None,
    allow_recap: bool | None = None,
    hydrate_winner: bool = True,
) -> Job | None:
    """Find the next eligible QUEUED job and atomically transition it to RUNNING.

    Enforces (HF Pool Phase 1):
    - Global VoxCPM concurrency cap (`MAX_CONCURRENT_VOXCPM_JOBS = 2`).
    - 15-minute worker host affinity for resumed parked jobs (`Part 3 #3`).
    - Shortest-remaining-GPU-first with 2:1 Paid/Free interleaving and 15-minute
      anti-starvation ceiling (`Part 6 #4`).
    - Pre-execution ledger admission check (`QUEUED -> RUNNING` only when
      `effective_usable_gpu_s - in_flight_gpu_s >= needed_gpu_s` and `solvent_slots >= units`).
    """
    from sqlalchemy.orm import defer

    global _paid_dispatch_streak

    if skip_local_source is None:
        skip_local_source = not getattr(settings, "worker_claim_local_files", True)
    if allow_recap is None:
        allow_recap = getattr(settings, "recap_on_queue", False)

    preds = _queued_job_eligibility_predicates(
        engine_filter=engine_filter,
        skip_local_source=skip_local_source,
        allow_recap=allow_recap,
    )
    query = (
        select(Job)
        .options(
            defer(Job.input_text),
            defer(Job.final_script),
            defer(Job.recap_config),
        )
        .where(*preds)
    )

    candidate_stmt = query.order_by(Job.created_at).limit(25)
    candidates = list((await session.execute(candidate_stmt)).scalars().all())
    if not candidates:
        return None

    active_voxcpm = await count_active_voxcpm_jobs(session)

    user_ids = {int(c.user_id) for c in candidates if c.user_id is not None}
    paid_users: set[int] = set()
    if user_ids:
        u_rows = (
            await session.execute(
                select(User.id, User.account_category).where(User.id.in_(user_ids))
            )
        ).all()
        for uid, cat in u_rows:
            if settings.is_admin(uid) or cat == AccountCategory.PURCHASED.value:
                paid_users.add(int(uid))
        for uid in user_ids:
            if settings.is_admin(uid):
                paid_users.add(int(uid))

    is_paid_map: dict[str, bool] = {}
    gpu_s_map: dict[str, float] = {}
    units_map: dict[str, int] = {}
    est_sec_map: dict[str, float] = {}
    vox_candidates: list[Job] = []
    non_vox_candidates: list[Job] = []

    for cand in candidates:
        jid = str(cand.id)
        is_recap_c = _is_recap_row(cand)
        if _vc.is_host_affinity_blocked(getattr(cand, "voice_choice_state", None)):
            logger.debug(
                "Skipping job %s: worker host affinity active for another host",
                jid,
            )
            continue
        if is_recap_c:
            if skip_local_source and not _is_recap_portable(
                cand.source_audio_path, cand.recap_ref_audio_path
            ):
                logger.debug(
                    "Skipping recap job %s: not portable for clean-disk worker",
                    jid,
                )
                continue
        else:
            if skip_local_source and _is_local_source(cand.source_audio_path):
                logger.debug(
                    "Skipping job %s: source_audio_path is a local file "
                    "and this instance has skip_local_source=True",
                    jid,
                )
                continue

        is_paid_map[jid] = int(cand.user_id) in paid_users
        if is_job_voxcpm_bound(cand):
            vox_candidates.append(cand)
        else:
            non_vox_candidates.append(cand)

    # Non-VoxCPM jobs (e.g. Gemini/Edge recaps) keep their exact FIFO slot positions;
    # VoxCPM slots are filled in `ordered_vox` order (or omitted when VoxCPM cap is full).
    if active_voxcpm >= MAX_CONCURRENT_VOXCPM_JOBS:
        ordered_all = non_vox_candidates
    else:
        need_text_ids: list[str] = []
        for cand in vox_candidates:
            if _is_recap_row(cand):
                continue
            vc_state = _vc.parse_choice_state(getattr(cand, "voice_choice_state", None))
            if vc_state and float(vc_state.get("needed_gpu_s") or 0.0) > 0:
                continue
            need_text_ids.append(str(cand.id))

        prefetched_texts: dict[str, str] = {}
        if need_text_ids:
            text_rows = (
                await session.execute(
                    select(Job.id, Job.input_text).where(Job.id.in_(need_text_ids))
                )
            ).all()
            for tid, raw_txt in text_rows:
                prefetched_texts[str(tid)] = raw_txt or ""
            for tid in need_text_ids:
                prefetched_texts.setdefault(tid, "")

        for cand in vox_candidates:
            jid = str(cand.id)
            override = prefetched_texts[jid] if jid in prefetched_texts else None
            u, needed_s, est_s = _estimate_job_voxcpm_budget(
                cand, text_override=override
            )
            units_map[jid] = u
            gpu_s_map[jid] = needed_s
            est_sec_map[jid] = est_s

        ordered_vox = order_voxcpm_fair_queue(
            vox_candidates, is_paid_map, gpu_s_map
        )
        vox_ids = {str(c.id) for c in vox_candidates}
        vox_iter = iter(ordered_vox)
        ordered_all = []
        for cand in candidates:
            cid = str(cand.id)
            if cid in vox_ids:
                nxt = next(vox_iter, None)
                if nxt is not None:
                    ordered_all.append(nxt)
            elif any(str(nv.id) == cid for nv in non_vox_candidates):
                ordered_all.append(cand)

    for cand in ordered_all:
        candidate_id = str(cand.id)
        is_recap = _is_recap_row(cand)
        is_vox = is_job_voxcpm_bound(cand)

        if is_vox and active_voxcpm >= MAX_CONCURRENT_VOXCPM_JOBS:
            continue

        # Part 3 (#3): prefer the checkpoint owner host for 15 minutes after resume/park.
        if _vc.is_host_affinity_blocked(getattr(cand, "voice_choice_state", None)):
            logger.debug(
                "Skipping job %s: worker host affinity active for another host",
                candidate_id,
            )
            continue

        if is_recap:
            if skip_local_source and not _is_recap_portable(
                cand.source_audio_path, cand.recap_ref_audio_path
            ):
                logger.debug(
                    "Skipping recap job %s: not portable for clean-disk worker",
                    candidate_id,
                )
                continue
        else:
            if skip_local_source and _is_local_source(cand.source_audio_path):
                logger.debug(
                    "Skipping job %s: source_audio_path is a local file "
                    "and this instance has skip_local_source=True",
                    candidate_id,
                )
                continue

        if is_vox:
            adm = _hf_ledger.evaluate_voxcpm_admission(
                kind="recap" if is_recap else "clone",
                is_paid=is_paid_map.get(candidate_id, False),
                units=units_map.get(candidate_id, 1),
                est_audio_seconds=est_sec_map.get(candidate_id, 0.0),
            )
            if not adm.admitted:
                logger.debug(
                    "Holding queued VoxCPM job %s at claim gate (reason=%s, band=%s, needed=%.1fs, avail=%.1fs)",
                    candidate_id,
                    adm.reason,
                    adm.band,
                    adm.needed_gpu_s,
                    adm.available_gpu_s,
                )
                continue

        claimed = await claim_job_for_running(session, candidate_id)
        if not claimed:
            continue
        if is_vox:
            if is_paid_map.get(candidate_id, False):
                _paid_dispatch_streak += 1
            else:
                _paid_dispatch_streak = 0
        if not hydrate_winner:
            cand.status = JobStatus.RUNNING.value
            return cand
        return await session.get(Job, candidate_id, populate_existing=True)

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

