from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db.base import Base
from app.models.enums import JobStatus, LedgerDirection
from app.models.schema import AuditLog, Job, MinuteLedgerEvent, utc_now
from app.services.ledger import (
    consume_seconds,
    get_minute_balance,
    get_or_create_user,
)
from app.services.recovery import recover_stale_running_jobs


@pytest.mark.asyncio
async def test_stale_job_recovery_marks_failed_and_records_audit(db_session: AsyncSession):
    """Verify that a job older than stale_job_timeout_seconds is marked FAILED with an AuditLog."""
    user, _ = await get_or_create_user(db_session, telegram_id=9001)
    stale_time = utc_now() - timedelta(seconds=700)

    job = Job(
        id="stale-job-basic-1",
        user_id=user.id,
        status=JobStatus.RUNNING.value,
        started_at=stale_time,
    )
    db_session.add(job)
    await db_session.commit()

    recovered_ids = await recover_stale_running_jobs(
        session=db_session,
        stale_timeout_seconds=600,
    )

    assert recovered_ids == ["stale-job-basic-1"]

    # Verify job status & timestamps
    await db_session.refresh(job)
    assert job.status == JobStatus.FAILED.value
    assert "Worker process interrupted or terminated unexpectedly" in job.error_message
    assert job.finished_at is not None

    # Verify audit log was recorded
    audit_res = await db_session.execute(
        select(AuditLog).where(
            AuditLog.target_id == "stale-job-basic-1",
            AuditLog.action == "job.stale_recovery",
        )
    )
    audit = audit_res.scalar_one_or_none()
    assert audit is not None
    assert audit.target_type == "job"


@pytest.mark.asyncio
async def test_fresh_running_job_is_not_recovered(db_session: AsyncSession):
    """Verify that an actively running job within the timeout window is NOT touched by recovery."""
    user, _ = await get_or_create_user(db_session, telegram_id=9002)
    recent_time = utc_now() - timedelta(seconds=30)  # Started 30s ago (fresh)

    job = Job(
        id="fresh-job-running-1",
        user_id=user.id,
        status=JobStatus.RUNNING.value,
        started_at=recent_time,
    )
    db_session.add(job)
    await db_session.commit()

    recovered_ids = await recover_stale_running_jobs(
        session=db_session,
        stale_timeout_seconds=600,
    )

    assert recovered_ids == []

    # Verify job remains actively RUNNING
    unaffected_job = await db_session.get(Job, "fresh-job-running-1")
    assert unaffected_job.status == JobStatus.RUNNING.value
    assert unaffected_job.finished_at is None


@pytest.mark.asyncio
async def test_concurrent_competing_workers_stale_recovery():
    """Verify that when 5 worker processes concurrently execute recovery, only ONE recovers the job."""
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
        user, _ = await get_or_create_user(s, telegram_id=9003)
        stale_time = utc_now() - timedelta(seconds=1200)
        job = Job(
            id="stale-concurrent-job-1",
            user_id=user.id,
            status=JobStatus.RUNNING.value,
            started_at=stale_time,
        )
        s.add(job)
        await s.commit()

    async def worker_recover(worker_id: int) -> list[str]:
        for _ in range(5):
            async with session_maker() as s:
                try:
                    return await recover_stale_running_jobs(s, stale_timeout_seconds=600)
                except Exception:
                    await s.rollback()
                    await asyncio.sleep(0.02)
        return []

    results = await asyncio.gather(*(worker_recover(i) for i in range(5)))
    # Flatten results
    claimed_lists = [r for r in results if len(r) > 0]
    assert len(claimed_lists) == 1, f"Expected exactly 1 worker to claim stale job, got {claimed_lists}"
    assert claimed_lists[0] == ["stale-concurrent-job-1"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_repeated_recovery_calls_idempotent(db_session: AsyncSession):
    """Calling recovery multiple times sequentially must be idempotent."""
    user, _ = await get_or_create_user(db_session, telegram_id=9004)
    stale_time = utc_now() - timedelta(seconds=900)

    job = Job(
        id="stale-repeat-1",
        user_id=user.id,
        status=JobStatus.RUNNING.value,
        started_at=stale_time,
    )
    db_session.add(job)
    await db_session.commit()

    # First recovery
    res1 = await recover_stale_running_jobs(db_session, stale_timeout_seconds=600)
    assert res1 == ["stale-repeat-1"]

    # Second recovery
    res2 = await recover_stale_running_jobs(db_session, stale_timeout_seconds=600)
    assert res2 == []


@pytest.mark.asyncio
async def test_stale_recovery_with_balance_refund(db_session: AsyncSession):
    """A stale job that was debited before a crash must be automatically and idempotently refunded."""
    user, _ = await get_or_create_user(db_session, telegram_id=9005)
    bal = await get_minute_balance(db_session, user.id)
    bal.welcome_seconds = 300

    job_id = "stale-debited-job-1"
    job = Job(
        id=job_id,
        user_id=user.id,
        status=JobStatus.RUNNING.value,
        started_at=utc_now() - timedelta(seconds=800),
    )
    db_session.add(job)
    await db_session.commit()

    # Simulate debit right before crash
    await consume_seconds(db_session, user.id, seconds_to_consume=75, job_id=job_id)
    bal_after_debit = await get_minute_balance(db_session, user.id)
    assert bal_after_debit.welcome_seconds == 225

    # Worker crashes and restarts: recovery runs
    recovered = await recover_stale_running_jobs(db_session, stale_timeout_seconds=600)
    assert recovered == [job_id]

    # Verify balance was restored to 300
    bal_restored = await get_minute_balance(db_session, user.id)
    assert bal_restored.welcome_seconds == 300

    # Verify refund event in ledger
    refund_events = (
        await db_session.execute(
            select(MinuteLedgerEvent).where(
                MinuteLedgerEvent.user_id == user.id,
                MinuteLedgerEvent.reference_id == f"REFUND:{job_id}",
                MinuteLedgerEvent.direction == LedgerDirection.CREDIT.value,
            )
        )
    ).scalars().all()
    assert len(refund_events) == 1
    assert refund_events[0].seconds == 75

    # Repeated recovery must not refund again
    await recover_stale_running_jobs(db_session, stale_timeout_seconds=600)
    bal_again = await get_minute_balance(db_session, user.id)
    assert bal_again.welcome_seconds == 300


@pytest.mark.asyncio
async def test_stale_recovery_crash_before_debit(db_session: AsyncSession):
    """If crash happened before debit, recovery marks FAILED without phantom refund."""
    user, _ = await get_or_create_user(db_session, telegram_id=9006)
    bal = await get_minute_balance(db_session, user.id)
    bal.welcome_seconds = 300

    job_id = "stale-undebited-job-1"
    job = Job(
        id=job_id,
        user_id=user.id,
        status=JobStatus.RUNNING.value,
        started_at=utc_now() - timedelta(seconds=800),
    )
    db_session.add(job)
    await db_session.commit()

    recovered = await recover_stale_running_jobs(db_session, stale_timeout_seconds=600)
    assert recovered == [job_id]

    # Balance must remain exactly 300
    bal_check = await get_minute_balance(db_session, user.id)
    assert bal_check.welcome_seconds == 300

    # No credit events should exist
    refund_events = (
        await db_session.execute(
            select(MinuteLedgerEvent).where(
                MinuteLedgerEvent.user_id == user.id,
                MinuteLedgerEvent.reference_id == job_id,
            )
        )
    ).scalars().all()
    assert len(refund_events) == 0


@pytest.mark.asyncio
async def test_stale_recovery_job_temp_directory_cleanup(db_session: AsyncSession, tmp_path: Path):
    """Only the recovered job's temp directory should be cleaned; other directories stay intact."""
    with patch.object(settings, "storage_root", tmp_path):
        user, _ = await get_or_create_user(db_session, telegram_id=9007)
        job_id = "stale-temp-job-1"

        # Create job temp directory
        job_temp = tmp_path / "temp" / job_id
        job_temp.mkdir(parents=True, exist_ok=True)
        (job_temp / "temp_audio.wav").write_text("dummy")

        # Create unrelated active job directory
        other_temp = tmp_path / "temp" / "active-other-job"
        other_temp.mkdir(parents=True, exist_ok=True)
        (other_temp / "keep_audio.wav").write_text("keep this")

        job = Job(
            id=job_id,
            user_id=user.id,
            status=JobStatus.RUNNING.value,
            started_at=utc_now() - timedelta(seconds=800),
        )
        db_session.add(job)
        await db_session.commit()

        recovered = await recover_stale_running_jobs(db_session, stale_timeout_seconds=600)
        assert recovered == [job_id]

        # Verify job_temp was deleted
        assert not job_temp.exists()
        # Verify other_temp still exists
        assert other_temp.exists()
        assert (other_temp / "keep_audio.wav").exists()


@pytest.mark.asyncio
async def test_stale_recovery_telegram_notification(db_session: AsyncSession):
    """If Telegram bot and status_message_id exist, status message is edited with notification."""
    user, _ = await get_or_create_user(db_session, telegram_id=9008)
    job_id = "stale-tg-notify-1"

    job = Job(
        id=job_id,
        user_id=user.id,
        chat_id=9008,
        status_message_id=5555,
        status=JobStatus.RUNNING.value,
        started_at=utc_now() - timedelta(seconds=800),
    )
    db_session.add(job)
    await db_session.commit()

    mock_bot = AsyncMock()
    mock_bot.edit_message_text = AsyncMock()

    recovered = await recover_stale_running_jobs(
        session=db_session,
        stale_timeout_seconds=600,
        bot=mock_bot,
    )
    assert recovered == [job_id]

    # Verify mock_bot.edit_message_text was called
    mock_bot.edit_message_text.assert_awaited_once()
    call_kwargs = mock_bot.edit_message_text.call_args.kwargs
    assert call_kwargs["chat_id"] == 9008
    assert call_kwargs["message_id"] == 5555
    assert "လုပ်ငန်းစဉ် ရပ်တန့်သွားခဲ့သည်။" in call_kwargs["text"]


@pytest.mark.asyncio
async def test_worker_delivery_failure_safe_failure_and_refund(db_session: AsyncSession, tmp_path: Path):
    """If Telegram audio delivery crashes/fails, job must NOT be marked SUCCESS and balance must be refunded."""
    from app.models.schema import VoiceModel
    from app.services.queue import create_voice_clone_job
    from app.worker import process_single_job

    user, _ = await get_or_create_user(db_session, telegram_id=9009)
    bal = await get_minute_balance(db_session, user.id)
    bal.welcome_seconds = 300

    pth_file = tmp_path / "test.pth"
    pth_file.write_bytes(b"pth_content")

    # Create dummy model
    model = VoiceModel(
        name="Test Model",
        slug="test-model-deliv",
        type="SYSTEM",
        model_dir=str(tmp_path),
        pth_path=str(pth_file),
        status="ACTIVE",
        is_default_free=True,
    )
    db_session.add(model)
    await db_session.flush()

    job, _ = await create_voice_clone_job(
        session=db_session,
        user_id=user.id,
        text="Testing audio delivery resilience",
        edge_voice="my-MM-ThihaNeural",
        model_id=model.id,
        chat_id=9009,
    )
    await db_session.commit()

    mock_bot = AsyncMock()
    # Mock send_audio raising an exception (e.g. Telegram network failure or process crash simulation)
    mock_bot.send_audio.side_effect = RuntimeError("Telegram connection dropped during send_audio")

    async def mock_tts(text, out_path, voice, speed):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake_mp3_audio")
        return out_path

    async def mock_norm(in_audio, out_wav):
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        out_wav.write_bytes(b"fake_norm_wav")
        return out_wav

    async def mock_applio(model_pth, index_path, input_wav, output_wav, pitch=0, **kwargs):
        output_wav.parent.mkdir(parents=True, exist_ok=True)
        output_wav.write_bytes(b"fake_converted_wav")
        return output_wav

    with (
        patch("app.worker.generate_edge_tts", side_effect=mock_tts),
        patch("app.worker.normalize_audio_to_wav", side_effect=mock_norm),
        patch("app.worker.run_applio_conversion", side_effect=mock_applio),
        patch("app.worker.get_wav_duration_seconds", return_value=15),
    ):
        processed = await process_single_job(session=db_session, job=job, bot=mock_bot)
        assert processed is False

    # Verify job status is FAILED, not SUCCESS!
    await db_session.refresh(job)
    assert job.status == JobStatus.FAILED.value
    assert "Telegram connection dropped" in (job.error_message or "")

    # Verify balance was restored to 300s (no minutes lost on failed delivery)
    bal_after = await get_minute_balance(db_session, user.id)
    assert bal_after.welcome_seconds == 300


