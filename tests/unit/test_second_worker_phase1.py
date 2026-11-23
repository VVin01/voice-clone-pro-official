from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db.base import Base
from app.models.enums import JobStatus, JobType
from app.models.schema import Job, User
from app.services.ledger import get_or_create_user
from app.services.queue import (
    _is_local_source,
    claim_job_for_running,
    claim_next_queued_job,
)


async def _create_test_session_maker():
    """Helper to create an isolated in-memory DB and return session_maker."""
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
    return engine, session_maker


# ── 1. Reference Audio Locality Tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_clean_disk_worker_skips_local_file_job_and_claims_portable_job():
    """Test Phase 1 requirement 1: A clean-disk worker (skip_local_source=True)

    must skip jobs whose reference audio is a local file on another machine,
    and instead claim the next portable job (e.g. preset voice).
    The local-file job must remain QUEUED without being starved, and the
    primary worker (skip_local_source=False) must subsequently claim it.
    """
    engine, session_maker = await _create_test_session_maker()
    try:
        async with session_maker() as s:
            user, _ = await get_or_create_user(s, telegram_id=9001)

            # Job 1: older job with local reference audio that exists only on primary disk
            job_local = Job(
                id="job-primary-local-file",
                user_id=user.id,
                status=JobStatus.QUEUED.value,
                job_type=JobType.VOICE_CLONE.value,
                engine="voxcpm",
                source_audio_path="/storage/temp/inputs/primary_only_reference.wav",
                input_text="Local reference voice test",
            )
            s.add(job_local)

            # Job 2: newer job with portable curated voice (no local source file)
            job_portable = Job(
                id="job-vps-portable-preset",
                user_id=user.id,
                status=JobStatus.QUEUED.value,
                job_type=JobType.VOICE_CLONE.value,
                engine="voxcpm",
                preset_voice="komin",
                source_audio_path=None,
                input_text="Portable preset voice test",
            )
            s.add(job_portable)
            await s.commit()

        # Step A: Clean-disk worker (VPS) claims with skip_local_source=True
        async with session_maker() as s:
            vps_claimed = await claim_next_queued_job(s, skip_local_source=True)
            assert vps_claimed is not None
            # VPS worker must have skipped Job 1 and claimed Job 2
            assert vps_claimed.id == "job-vps-portable-preset"
            assert vps_claimed.status == JobStatus.RUNNING.value

        # Verify Job 1 is NOT starved and remains QUEUED in DB
        async with session_maker() as s:
            j1 = await s.get(Job, "job-primary-local-file")
            assert j1.status == JobStatus.QUEUED.value

        # Step B: Primary worker claims with skip_local_source=False
        async with session_maker() as s:
            primary_claimed = await claim_next_queued_job(s, skip_local_source=False)
            assert primary_claimed is not None
            # Primary worker claims Job 1
            assert primary_claimed.id == "job-primary-local-file"
            assert primary_claimed.status == JobStatus.RUNNING.value

        # Verify both jobs are now RUNNING and neither was starved
        async with session_maker() as s:
            j1_final = await s.get(Job, "job-primary-local-file")
            j2_final = await s.get(Job, "job-vps-portable-preset")
            assert j1_final.status == JobStatus.RUNNING.value
            assert j2_final.status == JobStatus.RUNNING.value
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_env_worker_claim_local_files_governs_default_skip(db_session):
    """When skip_local_source is not explicitly passed, it defaults to

    not settings.worker_claim_local_files (WORKER_CLAIM_LOCAL_FILES env).
    """
    user, _ = await get_or_create_user(db_session, telegram_id=9002)
    job_local = Job(
        id="job-env-test-local",
        user_id=user.id,
        status=JobStatus.QUEUED.value,
        job_type=JobType.VOICE_CLONE.value,
        engine="voxcpm",
        source_audio_path="/storage/inputs/some_file.wav",
    )
    db_session.add(job_local)
    await db_session.commit()

    # Case 1: worker_claim_local_files is False (WORKER_CLAIM_LOCAL_FILES=0)
    with patch.object(settings, "worker_claim_local_files", False):
        claimed = await claim_next_queued_job(db_session)
        # Must skip the local-file job -> returns None
        assert claimed is None

    # Case 2: worker_claim_local_files is True (WORKER_CLAIM_LOCAL_FILES=1, primary default)
    with patch.object(settings, "worker_claim_local_files", True):
        claimed = await claim_next_queued_job(db_session)
        # Primary claims the job
        assert claimed is not None
        assert claimed.id == "job-env-test-local"
        assert claimed.status == JobStatus.RUNNING.value


def test_is_local_source_helper():
    """Verify _is_local_source classification."""
    assert _is_local_source(None) is False
    assert _is_local_source("") is False
    assert _is_local_source("   ") is False
    assert _is_local_source("http://example.com/audio.wav") is False
    assert _is_local_source("https://example.com/audio.wav") is False

    assert _is_local_source("/storage/temp/inputs/audio.wav") is True
    assert _is_local_source("storage/temp/inputs/audio.wav") is True
    assert _is_local_source("C:\\data\\audio.wav") is True


# ── 2. Concurrency: Two Competing Workers, Exactly One Winner ─────────────────


@pytest.mark.asyncio
async def test_two_competing_workers_exactly_one_winner():
    """Test Phase 1 requirement 5: Two competing workers trying to claim the

    same queued job concurrently result in exactly ONE winner.
    """
    engine, session_maker = await _create_test_session_maker()
    try:
        async with session_maker() as s:
            user, _ = await get_or_create_user(s, telegram_id=9003)
            job = Job(
                id="job-contested-1",
                user_id=user.id,
                status=JobStatus.QUEUED.value,
                job_type=JobType.VOICE_CLONE.value,
                engine="voxcpm",
                preset_voice="mahnin",
                source_audio_path=None,
            )
            s.add(job)
            await s.commit()

        async def worker_attempt():
            async with session_maker() as s:
                return await claim_next_queued_job(s, skip_local_source=False)

        # Worker A (primary) and Worker B (VPS) race to claim
        claimed_a, claimed_b = await asyncio.gather(worker_attempt(), worker_attempt())

        # Exactly one worker claimed the job; the other received None
        winners = [c for c in (claimed_a, claimed_b) if c is not None]
        losers = [c for c in (claimed_a, claimed_b) if c is None]
        assert len(winners) == 1
        assert len(losers) == 1
        assert winners[0].id == "job-contested-1"

        # Confirm database state is RUNNING
        async with session_maker() as s:
            final_job = await s.get(Job, "job-contested-1")
            assert final_job.status == JobStatus.RUNNING.value
    finally:
        await engine.dispose()


# ── 3. Worker-Mode Contract & Safe Startup ────────────────────────────────────


@pytest.mark.asyncio
async def test_worker_mode_contract_no_bot_updates_no_web_server():
    """Test Phase 1 requirement 4: In RUN_MODE=worker:

    - Never starts web server / uvicorn
    - Never calls setWebhook, deleteWebhook, or getUpdates on Bot
    - Never sets up bot commands menu or dispatcher
    """
    mock_bot = MagicMock()
    mock_bot.session = MagicMock()
    mock_bot.session.close = AsyncMock()
    # Explicitly verify webhook/polling methods are NEVER called
    mock_bot.set_webhook = AsyncMock()
    mock_bot.delete_webhook = AsyncMock()
    mock_bot.get_updates = AsyncMock()
    mock_bot.set_my_commands = AsyncMock()

    with (
        patch("main.init_db", new_callable=AsyncMock) as mock_init_db,
        patch("main.Bot", return_value=mock_bot),
        patch("main.run_worker_loop", new_callable=AsyncMock) as mock_worker_loop,
        patch("main.setup_bot_commands", new_callable=AsyncMock) as mock_setup_commands,
        patch("main.Dispatcher") as mock_dispatcher,
        patch("uvicorn.Server") as mock_uvicorn_server,
        patch.object(settings, "bot_token", "fake:token"),
        patch.object(settings, "instance_primary", False),
    ):
        from main import run_bot_and_worker

        mock_worker_loop.side_effect = asyncio.CancelledError()

        try:
            await run_bot_and_worker(mode="worker")
        except asyncio.CancelledError:
            pass

        # 1. DB was initialized
        mock_init_db.assert_awaited_once()

        # 2. Worker loop was launched
        mock_worker_loop.assert_awaited_once_with(bot=mock_bot)

        # 3. Webhook / polling methods NEVER called
        mock_bot.set_webhook.assert_not_called()
        mock_bot.delete_webhook.assert_not_called()
        mock_bot.get_updates.assert_not_called()

        # 4. Bot UI / commands / dispatcher NEVER started
        mock_setup_commands.assert_not_called()
        mock_dispatcher.assert_not_called()

        # 5. Uvicorn server NEVER started
        mock_uvicorn_server.assert_not_called()


# ── 4. Singleton-Duty Gating ──────────────────────────────────────────────────


class _NoOpAsyncSessionCtx:
    """Hermetic stand-in for AsyncSessionLocal so worker-loop tests never hit a real DB."""

    async def __aenter__(self):
        return MagicMock()

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_secondary_instance_skips_boot_cutoff_orphan_sweep():
    """Secondary instance (INSTANCE_PRIMARY=0) must skip the boot-cutoff

    orphan sweep in run_worker_loop so it never kills jobs actively rendering
    on the primary instance.
    """
    with (
        patch.object(settings, "instance_primary", False),
        patch.object(settings, "worker_laptop_status", "ONLINE"),
        patch("app.worker.AsyncSessionLocal", return_value=_NoOpAsyncSessionCtx()),
        patch("app.services.dynamic_settings.load_dynamic_settings", new_callable=AsyncMock),
        patch("app.services.dynamic_settings.set_dynamic_setting", new_callable=AsyncMock),
        patch("app.services.recovery.recover_stale_running_jobs", new_callable=AsyncMock) as mock_recover,
        patch("app.worker.claim_and_process_next_job", new_callable=AsyncMock) as mock_claim,
        patch("app.worker._boot_cutoff_sweep_due", return_value=True),
    ):
        mock_claim.side_effect = asyncio.CancelledError()

        from app.worker import run_worker_loop

        try:
            await run_worker_loop(poll_interval=0.01)
        except asyncio.CancelledError:
            pass

        # Because instance_primary is False, recover_stale_running_jobs was NEVER called
        mock_recover.assert_not_called()


@pytest.mark.asyncio
async def test_primary_instance_runs_boot_cutoff_orphan_sweep():
    """Primary instance (INSTANCE_PRIMARY=1, default) DOES run the startup

    and boot-cutoff orphan sweep.
    """
    with (
        patch.object(settings, "instance_primary", True),
        patch("app.worker.AsyncSessionLocal", return_value=_NoOpAsyncSessionCtx()),
        patch("app.services.dynamic_settings.load_dynamic_settings", new_callable=AsyncMock),
        patch("app.services.dynamic_settings.set_dynamic_setting", new_callable=AsyncMock),
        patch("app.services.recovery.recover_stale_running_jobs", new_callable=AsyncMock) as mock_recover,
        patch("app.worker.claim_and_process_next_job", new_callable=AsyncMock) as mock_claim,
        patch("app.worker._boot_cutoff_sweep_due", return_value=False),
    ):
        mock_claim.side_effect = asyncio.CancelledError()

        from app.worker import run_worker_loop

        try:
            await run_worker_loop(poll_interval=0.01)
        except asyncio.CancelledError:
            pass

        # Primary instance ran initial recovery on startup
        assert mock_recover.await_count >= 1


# ── 5. Defense-in-Depth: Missing Local Reference Raises FileNotFoundError ─────


@pytest.mark.asyncio
async def test_process_single_job_raises_when_reference_audio_missing_on_disk(db_session):
    """If a job with a local source_audio_path is processed on a machine

    where the file does not exist, it must fail with FileNotFoundError
    rather than silently falling back to a generic voice design.
    """
    user, _ = await get_or_create_user(db_session, telegram_id=9004)
    job = Job(
        id="job-missing-audio-disk",
        user_id=user.id,
        status=JobStatus.QUEUED.value,
        job_type=JobType.VOICE_CLONE.value,
        engine="voxcpm",
        preset_voice=None,
        source_audio_path="/nonexistent/path/to/missing_ref.wav",
        input_text="Should fail honestly if audio missing",
    )
    db_session.add(job)
    await db_session.commit()

    from app.worker import process_single_job

    j = await db_session.get(Job, "job-missing-audio-disk")
    success = await process_single_job(db_session, j, bot=None)
    assert success is False

    # Check job failed with appropriate error
    final_job = await db_session.get(Job, "job-missing-audio-disk")
    assert final_job.status == JobStatus.FAILED.value
    assert final_job.error_message is not None
