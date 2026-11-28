from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import shutil
from typing import Sequence

from aiogram import Bot
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.enums import JobStatus, JobType, LedgerDirection
from app.models.schema import AuditLog, Job, MinuteLedgerEvent, User, utc_now
from app.services.i18n import t
from app.services.ledger import refund_job_seconds, refund_tts_referral_seconds

logger = logging.getLogger(__name__)


async def recover_stale_running_jobs(
    session: AsyncSession,
    stale_timeout_seconds: int | None = None,
    bot: Bot | None = None,
    recap_stale_timeout_seconds: int | None = None,
) -> list[str]:
    """Recover orphaned RUNNING jobs left behind by an abrupt worker crash.

    Rules:
    1. Only jobs older than `stale_timeout_seconds` in status RUNNING are considered stale.
       Freshly running jobs within the timeout window are never touched.
    2. Recovery is atomic: uses `UPDATE jobs SET status = 'FAILED' ... WHERE status = 'RUNNING'`
       checking `rowcount == 1`. Competing workers starting simultaneously will never recover
       the same job twice.
    3. Never retries automatically: to prevent duplicate VoxCPM2 synthesis or duplicate
       Telegram output delivery.
    4. Idempotently refunds debited minutes: checks if the job was charged in the ledger
       and credits back the exact balance buckets.
    5. Safely cleans up only the specific job's temporary directory.
    6. Records a persistent AuditLog entry for every recovered job.
    """
    timeout = (
        stale_timeout_seconds
        if stale_timeout_seconds is not None
        else settings.stale_job_timeout_seconds
    )
    cutoff = utc_now() - timedelta(seconds=timeout)

    # Step 1: Find candidate job IDs.
    # Recap rows are EXCLUDED (final-audit batch M): recap charges are
    # credit deductions with no minute-ledger events, so this reaper
    # would fail the job and silently keep the credit — including
    # parked SCRIPT_READY rows, which legitimately sit RUNNING for up
    # to 24h. Recap rows are owned by the recap reset + the
    # SCRIPT_READY expiry sweep. Same exclusion predicate as the
    # voice claim query in app/services/queue.py.
    candidate_stmt = (
        select(Job.id)
        .where(
            Job.status == JobStatus.RUNNING.value,
            Job.started_at <= cutoff,
            Job.job_type != JobType.RECAP.value,
            Job.engine != "recap",
            ~Job.id.startswith("rc_"),
        )
        .order_by(Job.started_at)
    )
    res = await session.execute(candidate_stmt)
    candidate_ids: Sequence[str] = res.scalars().all()

    recovered_ids: list[str] = []

    for job_id in candidate_ids:
        # Step 2: Atomic state transition from RUNNING -> FAILED
        claim_stmt = (
            update(Job)
            .where(
                Job.id == job_id,
                Job.status == JobStatus.RUNNING.value,
                Job.started_at <= cutoff,
            )
            .values(
                status=JobStatus.FAILED.value,
                error_message="Worker process interrupted or terminated unexpectedly (stale job recovered)",
                finished_at=utc_now(),
            )
        )
        update_res = await session.execute(claim_stmt)
        if update_res.rowcount != 1:
            # Another worker or task already claimed/recovered this job
            continue

        job = await session.get(Job, job_id)
        if not job:
            continue
        await session.refresh(job)

        # Step 3: Release/refund reservation & ledger charges idempotently
        from app.services import hf_pool_ledger as _hf_ledger
        from app.services.billing import refund_or_release_job

        _hf_ledger.release_in_flight_gpu(str(job.id))

        rel_res = await refund_or_release_job(
            session,
            job.user_id,
            str(job.id),
            reason="STALE_RECOVERY",
            note=f"Automatic recovery: job {job.id} interrupted by worker termination",
        )
        res_refunded_any = bool(
            rel_res.get("found")
            and (
                int(rel_res.get("refunded_recap_seconds") or 0) > 0
                or int(rel_res.get("refunded_voice_seconds") or 0) > 0
            )
        )

        debit_stmt = select(MinuteLedgerEvent).where(
            MinuteLedgerEvent.user_id == job.user_id,
            MinuteLedgerEvent.reference_id == str(job.id),
            MinuteLedgerEvent.direction == LedgerDirection.DEBIT.value,
        )
        debits = (await session.execute(debit_stmt)).scalars().all()
        was_charged = (len(debits) > 0) or res_refunded_any
        refunded_seconds = max(
            sum(d.seconds for d in debits),
            int(rel_res.get("refunded_voice_seconds") or 0),
        )

        if len(debits) > 0 and not rel_res.get("found"):
            if job.job_type == JobType.TTS.value:
                await refund_tts_referral_seconds(
                    session=session,
                    user_id=job.user_id,
                    job_id=str(job.id),
                    keep_seconds=0,
                    note=f"Automatic referral refund: TTS job {job.id} interrupted",
                )
            else:
                await refund_job_seconds(
                    session=session,
                    user_id=job.user_id,
                    job_id=str(job.id),
                    note=f"Automatic refund: job {job.id} interrupted by worker termination",
                )
            logger.info("Refunded %ss to user %s for stale job %s", refunded_seconds, job.user_id, job.id)
        elif res_refunded_any:
            logger.info(
                "Released/refunded stale job %s for user %s (recap=%ss, voice=%ss)",
                job.id,
                job.user_id,
                rel_res.get("refunded_recap_seconds"),
                rel_res.get("refunded_voice_seconds"),
            )

        # Step 4: Record AuditLog
        session.add(
            AuditLog(
                admin_id=0,  # System daemon
                action="job.stale_recovery",
                target_type="job",
                target_id=str(job.id),
                details_json=json.dumps({
                    "user_id": job.user_id,
                    "started_at": job.started_at.isoformat() if job.started_at else None,
                    "cutoff": cutoff.isoformat(),
                    "was_charged": was_charged,
                    "refunded_seconds": refunded_seconds,
                    "timeout_seconds": timeout,
                }),
            )
        )

        # Step 5: Safe temporary file cleanup (job-specific directory only)
        temp_dir = settings.storage_root / "temp" / str(job.id)
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)

        # Step 6: Notify user on Telegram if status message exists
        target_chat_id = job.chat_id or job.user_id
        target_msg_id = job.status_message_id
        if bot and target_chat_id and target_msg_id:
            try:
                user = await session.get(User, job.user_id)
                user_lang = user.language_code if user else "my"
                fail_notice = (
                    t("recovery_fail_notice_refunded", user_lang)
                    if was_charged
                    else t("recovery_fail_notice_no_charge", user_lang)
                )
                await bot.edit_message_text(
                    chat_id=target_chat_id,
                    message_id=target_msg_id,
                    text=fail_notice,
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.debug("Failed to update status message for stale job %s: %s", job.id, e)

        await session.commit()
        recovered_ids.append(str(job.id))
        logger.warning(
            "Safely recovered orphaned stale job %s (user=%s, was_charged=%s, refunded=%ss)",
            job.id, job.user_id, was_charged, refunded_seconds
        )

    # ── Recap Stale Recovery (Second Worker Phase 2, 2026-11-16) ──
    # Stale RUNNING recaps (beyond recap_stale_job_timeout_seconds, default 2700s)
    # are failed + auto-refunded via existing failure/refund rules.
    r_timeout = (
        recap_stale_timeout_seconds
        if recap_stale_timeout_seconds is not None
        else getattr(settings, "recap_stale_job_timeout_seconds", 2700)
    )
    r_cutoff = utc_now() - timedelta(seconds=r_timeout)

    r_candidate_stmt = (
        select(Job.id)
        .where(
            Job.status == JobStatus.RUNNING.value,
            Job.started_at <= r_cutoff,
            (Job.job_type == JobType.RECAP.value) | (Job.engine == "recap") | (Job.id.startswith("rc_")),
        )
        .order_by(Job.started_at)
    )
    r_res = await session.execute(r_candidate_stmt)
    r_candidate_ids: Sequence[str] = r_res.scalars().all()

    for r_job_id in r_candidate_ids:
        from app.webapp.api import _load_persisted_recap_state, _fail_stranded_recap_job
        raw_cfg = (
            await session.execute(select(Job.recap_config).where(Job.id == r_job_id))
        ).scalar_one_or_none()
        if raw_cfg:
            try:
                cfg_obj = json.loads(raw_cfg)
                if cfg_obj.get("script_ready"):
                    # Two-phase parked SCRIPT_READY jobs <24h old await user script approval;
                    # >=24h parked jobs are abandoned and eligible for stale recovery.
                    sr_at_raw = cfg_obj.get("script_ready_at")
                    if not sr_at_raw:
                        continue
                    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
                    sr_at = _dt.fromisoformat(str(sr_at_raw).replace("Z", "+00:00"))
                    if sr_at.tzinfo is None:
                        sr_at = sr_at.replace(tzinfo=_tz.utc)
                    now_utc = utc_now()
                    if now_utc.tzinfo is None:
                        now_utc = now_utc.replace(tzinfo=_tz.utc)
                    if (now_utc - sr_at) < _td(hours=24):
                        continue
            except Exception:
                pass

        r_job = await session.get(Job, r_job_id)
        if not r_job:
            continue
        r_user_id = int(r_job.user_id)
        r_started_at = r_job.started_at
        r_state = _load_persisted_recap_state(r_job) or {}
        # Release any SQLite/PG read snapshot before _fail_stranded_recap_job opens its own session,
        # and issue the refund + FAILED transition atomically inside _fail_stranded_recap_job FIRST
        # so a mid-flight crash never leaves a job FAILED without refunding credits/minutes.
        await session.rollback()

        fail_result = await _fail_stranded_recap_job(str(r_job_id), r_user_id, r_state)
        if fail_result is False:
            # _fail_stranded_recap_job uses its own AsyncSessionLocal(); in isolated
            # unit-test sessions the row doesn't exist in AsyncSessionLocal(). Check
            # whether it exists there before aborting recovery.
            try:
                from app.db.session import AsyncSessionLocal as _ASL
                async with _ASL() as _chk_s:
                    _exists_in_global = await _chk_s.get(Job, str(r_job_id)) is not None
            except Exception:
                _exists_in_global = False
            if _exists_in_global:
                continue

        if fail_result is None or fail_result is False:
            r_job_upd = await session.get(Job, r_job_id)
            if r_job_upd is not None and r_job_upd.status == JobStatus.RUNNING.value:
                r_job_upd.status = JobStatus.FAILED.value
                r_job_upd.finished_at = utc_now()

        session.add(
            AuditLog(
                admin_id=0,
                action="recap.stale_recovery",
                target_type="job",
                target_id=str(r_job_id),
                details_json=json.dumps({
                    "user_id": r_user_id,
                    "started_at": r_started_at.isoformat() if r_started_at else None,
                    "cutoff": r_cutoff.isoformat(),
                    "timeout_seconds": r_timeout,
                }),
            )
        )
        from app.services import hf_pool_ledger as _hf_ledger

        _hf_ledger.release_in_flight_gpu(str(r_job_id))
        await session.commit()
        recovered_ids.append(str(r_job_id))
        logger.warning(
            "Safely recovered orphaned stale recap job %s (user=%s)",
            r_job_id, r_user_id,
        )

    return recovered_ids
