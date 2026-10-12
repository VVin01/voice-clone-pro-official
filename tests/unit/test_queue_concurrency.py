import asyncio
from pathlib import Path
from unittest.mock import patch
import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.enums import JobStatus, LedgerDirection
from app.models.schema import Job, MinuteLedgerEvent, VoiceModel
from app.services.ledger import (
    consume_seconds,
    get_minute_balance,
    get_or_create_user,
    refund_job_seconds,
)
from app.services.queue import (
    claim_job_for_running,
    claim_next_queued_job,
    create_voice_clone_job,
)
from app.worker import process_single_job


@pytest.mark.asyncio
async def test_concurrent_competing_workers_claim_single_job():
    """Verify that among 10 competing workers, exactly ONE successfully claims a queued job."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        echo=False,
    )
    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with session_maker() as s:
        user, _ = await get_or_create_user(s, telegram_id=3001)
        # Create a queued job
        job = Job(
            id="test-job-claim-1",
            user_id=user.id,
            status=JobStatus.QUEUED.value,
        )
        s.add(job)
        await s.commit()

    async def attempt_claim(worker_id: int):
        async with session_maker() as s:
            try:
                claimed = await claim_job_for_running(s, "test-job-claim-1")
                return claimed
            except Exception:
                await s.rollback()
                return False

    # Run 10 workers simultaneously
    results = await asyncio.gather(*(attempt_claim(i) for i in range(10)))
    claimed_count = sum(1 for r in results if r is True)

    assert claimed_count == 1, f"Expected exactly 1 worker to claim job, got {claimed_count}"

    # Verify persistent state in DB is RUNNING
    async with session_maker() as s:
        final_job = await s.get(Job, "test-job-claim-1")
        assert final_job.status == JobStatus.RUNNING.value
        assert final_job.started_at is not None

    await engine.dispose()


@pytest.mark.asyncio
async def test_idempotent_job_balance_deduction(db_session):
    """Calling consume_seconds twice with the same job_id must deduct exactly once."""
    user, _ = await get_or_create_user(db_session, telegram_id=3002)
    bal = await get_minute_balance(db_session, user.id)
    bal.welcome_seconds = 300  # 300 seconds

    # First deduction for job-xyz
    bal1 = await consume_seconds(db_session, user.id, seconds_to_consume=50, job_id="job-xyz")
    assert bal1.welcome_seconds == 250

    # Second (duplicate/retry) deduction call with identical job_id
    bal2 = await consume_seconds(db_session, user.id, seconds_to_consume=50, job_id="job-xyz")
    assert bal2.welcome_seconds == 250, "Duplicate deduction must be skipped!"

    # Verify only ONE debit event was created
    events = (await db_session.execute(
        select(MinuteLedgerEvent).where(
            MinuteLedgerEvent.user_id == user.id,
            MinuteLedgerEvent.reference_id == "job-xyz",
            MinuteLedgerEvent.direction == LedgerDirection.DEBIT.value,
        )
    )).scalars().all()
    assert len(events) == 1
    assert events[0].seconds == 50


@pytest.mark.asyncio
async def test_terminal_job_cannot_reexecute(db_session, tmp_path):
    """A job already in SUCCESS or FAILED cannot be processed again."""
    user, _ = await get_or_create_user(db_session, telegram_id=3003)
    bal = await get_minute_balance(db_session, user.id)
    bal.welcome_seconds = 300

    job = Job(
        id="job-already-done",
        user_id=user.id,
        status=JobStatus.SUCCESS.value,
    )
    db_session.add(job)
    await db_session.commit()

    # Attempting to process this job must return False immediately
    res = await process_single_job(session=db_session, job=job)
    assert res is False

    # Balance must remain completely untouched
    bal_after = await get_minute_balance(db_session, user.id)
    assert bal_after.welcome_seconds == 300


@pytest.mark.asyncio
async def test_failure_and_idempotent_refund(db_session):
    """When a job fails after deduction, refund_job_seconds restores balance idempotently."""
    user, _ = await get_or_create_user(db_session, telegram_id=3004)
    bal = await get_minute_balance(db_session, user.id)
    bal.welcome_seconds = 300

    # Deduct 60s for job-err
    await consume_seconds(db_session, user.id, seconds_to_consume=60, job_id="job-err")
    assert (await get_minute_balance(db_session, user.id)).welcome_seconds == 240

    # Refund for job-err
    await refund_job_seconds(db_session, user.id, "job-err")
    assert (await get_minute_balance(db_session, user.id)).welcome_seconds == 300

    # Attempt second refund: must be idempotent and not double-credit
    await refund_job_seconds(db_session, user.id, "job-err")
    assert (await get_minute_balance(db_session, user.id)).welcome_seconds == 300
