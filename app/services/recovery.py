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
from app.models.enums import JobStatus, LedgerDirection
from app.models.schema import AuditLog, Job, MinuteLedgerEvent, User, utc_now
from app.services.i18n import t
from app.services.ledger import refund_job_seconds

logger = logging.getLogger(__name__)


async def recover_stale_running_jobs(
    session: AsyncSession,
    stale_timeout_seconds: int | None = None,
    bot: Bot | None = None,
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

    # Step 1: Find candidate job IDs
    candidate_stmt = (
        select(Job.id)
        .where(
            Job.status == JobStatus.RUNNING.value,
            Job.started_at <= cutoff,
        )
        .order_by(Job.started_at)
    )
    res = await session.execute(candidate_stmt)
    candidate_ids: Sequence[str] = res.scalars().all()

    if not candidate_ids:
        return []

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

        # Step 3: Inspect ledger for charges and refund idempotently
        debit_stmt = select(MinuteLedgerEvent).where(
            MinuteLedgerEvent.user_id == job.user_id,
            MinuteLedgerEvent.reference_id == str(job.id),
            MinuteLedgerEvent.direction == LedgerDirection.DEBIT.value,
        )
        debits = (await session.execute(debit_stmt)).scalars().all()
        was_charged = len(debits) > 0
        refunded_seconds = sum(d.seconds for d in debits)

        if was_charged:
            await refund_job_seconds(
                session=session,
                user_id=job.user_id,
                job_id=str(job.id),
                note=f"Automatic refund: job {job.id} interrupted by worker termination",
            )
            logger.info("Refunded %ss to user %s for stale job %s", refunded_seconds, job.user_id, job.id)

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

    return recovered_ids
