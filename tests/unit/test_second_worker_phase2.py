from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db.base import Base
from app.models.enums import JobStatus, JobType
from app.models.schema import AuditLog, Job, RecapCreditLedgerEvent, User, utc_now
from app.services.ledger import get_or_create_user
from app.services.queue import (
    _is_recap_portable,
    claim_next_queued_job,
)
from app.services.recovery import recover_stale_running_jobs
from app.worker import (
    _fail_timed_out_recap_job,
    claim_and_process_next_job,
    process_recap_job,
    process_single_job,
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


# ── 1. Claim Filter & Portability Tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_recap_claim_filter_portable_claimed_by_clean_disk_worker():
    """A URL-source + persona recap is portable: clean-disk worker claims it."""
    engine, session_maker = await _create_test_session_maker()
    try:
        async with session_maker() as s:
            user, _ = await get_or_create_user(s, telegram_id=9101)
            job = Job(
                id="rc_portable_001",
                user_id=user.id,
                status=JobStatus.QUEUED.value,
                job_type=JobType.RECAP.value,
                engine="recap",
                preset_voice="thiha",
                source_audio_path="https://www.youtube.com/watch?v=portable_vid",
                recap_ref_audio_path=None,
                input_text="Portable URL recap",
            )
            s.add(job)
            await s.commit()

        # Clean-disk worker (skip_local_source=True) with allow_recap=True
        async with session_maker() as s:
            claimed = await claim_next_queued_job(s, skip_local_source=True, allow_recap=True)
            assert claimed is not None
            assert claimed.id == "rc_portable_001"
            assert claimed.status == JobStatus.RUNNING.value
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recap_claim_filter_local_source_and_own_voice_skipped_by_clean_disk_worker():
    """Clean-disk workers skip local-upload and own-voice recaps; primary claims them."""
    engine, session_maker = await _create_test_session_maker()
    try:
        async with session_maker() as s:
            user, _ = await get_or_create_user(s, telegram_id=9102)

            # Job A: Local-upload source recap
            job_local_upload = Job(
                id="rc_local_upload_002",
                user_id=user.id,
                status=JobStatus.QUEUED.value,
                job_type=JobType.RECAP.value,
                engine="recap",
                preset_voice="thiha",
                source_audio_path="/storage/temp/uploads/source_vid.mp4",
                recap_ref_audio_path=None,
                input_text="Local upload recap",
            )
            s.add(job_local_upload)

            # Job B: Own-voice recap (needs primary-local reference audio)
            job_own_voice = Job(
                id="rc_own_voice_003",
                user_id=user.id,
                status=JobStatus.QUEUED.value,
                job_type=JobType.RECAP.value,
                engine="recap",
                preset_voice="thiha",
                source_audio_path="https://www.youtube.com/watch?v=some_vid",
                recap_ref_audio_path="/storage/temp/voxcpm_refs/user_ref.wav",
                input_text="Own-voice recap",
            )
            s.add(job_own_voice)

            # Job C: Portable URL + persona recap
            job_portable = Job(
                id="rc_portable_004",
                user_id=user.id,
                status=JobStatus.QUEUED.value,
                job_type=JobType.RECAP.value,
                engine="recap",
                preset_voice="thiha",
                source_audio_path="https://www.youtube.com/watch?v=portable_vid_2",
                recap_ref_audio_path=None,
                input_text="Portable URL recap 2",
            )
            s.add(job_portable)
            await s.commit()

        # Step 1: Clean-disk worker (skip_local_source=True) must skip A and B, claiming C
        async with session_maker() as s:
            claimed = await claim_next_queued_job(s, skip_local_source=True, allow_recap=True)
            assert claimed is not None
            assert claimed.id == "rc_portable_004"
            assert claimed.status == JobStatus.RUNNING.value

        # Step 2: Jobs A and B must remain QUEUED in DB
        async with session_maker() as s:
            ja = await s.get(Job, "rc_local_upload_002")
            jb = await s.get(Job, "rc_own_voice_003")
            assert ja.status == JobStatus.QUEUED.value
            assert jb.status == JobStatus.QUEUED.value

        # Step 3: Primary worker (skip_local_source=False) claims Job A, then Job B
        async with session_maker() as s:
            p_claimed_1 = await claim_next_queued_job(s, skip_local_source=False, allow_recap=True)
            assert p_claimed_1 is not None
            assert p_claimed_1.id == "rc_local_upload_002"

            p_claimed_2 = await claim_next_queued_job(s, skip_local_source=False, allow_recap=True)
            assert p_claimed_2 is not None
            assert p_claimed_2.id == "rc_own_voice_003"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recap_claim_filter_recap_on_queue_flag_off():
    """When allow_recap is False, worker claims skip recap jobs entirely."""
    engine, session_maker = await _create_test_session_maker()
    try:
        async with session_maker() as s:
            user, _ = await get_or_create_user(s, telegram_id=9103)
            job_recap = Job(
                id="rc_flag_off_005",
                user_id=user.id,
                status=JobStatus.QUEUED.value,
                job_type=JobType.RECAP.value,
                engine="recap",
                preset_voice="thiha",
                source_audio_path="https://www.youtube.com/watch?v=flag_off",
                recap_ref_audio_path=None,
                input_text="Flag off recap",
            )
            s.add(job_recap)
            await s.commit()

        # Claim with allow_recap=False
        async with session_maker() as s:
            claimed = await claim_next_queued_job(s, allow_recap=False)
            assert claimed is None

        # Verify recap job is still QUEUED
        async with session_maker() as s:
            j = await s.get(Job, "rc_flag_off_005")
            assert j.status == JobStatus.QUEUED.value
    finally:
        await engine.dispose()


def test_is_recap_portable_helper():
    """Verify _is_recap_portable logic directly."""
    # URLs with no ref audio are portable
    assert _is_recap_portable("https://youtube.com/watch?v=123", None) is True
    assert _is_recap_portable("http://video.site/movie.mp4", "") is True

    # Local source paths are NOT portable
    assert _is_recap_portable("/storage/uploads/video.mp4", None) is False
    assert _is_recap_portable(None, None) is False
    assert _is_recap_portable("", None) is False

    # Own-voice recaps (ref audio set) are NOT portable
    assert _is_recap_portable("https://youtube.com/watch?v=123", "/storage/ref.wav") is False
    assert _is_recap_portable("https://youtube.com/watch?v=123", "ref.wav") is False


# ── 2. Honest-Fail Rule Test ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recap_honest_fail_rule_missing_voice_reference(tmp_path):
    """When an own-voice recap is executed but ref audio is missing on disk,
    it must raise FileNotFoundError and fail + refund honestly, never synthesize
    a substitute persona voice.
    """
    from app.webapp.api import (
        RecapConfigRequest,
        _execute_recap_background_job,
        RECAP_JOBS_REGISTRY,
    )

    engine, session_maker = await _create_test_session_maker()
    try:
        async with session_maker() as s:
            user, _ = await get_or_create_user(s, telegram_id=9104)
            user.recap_credits = 1
            job = Job(
                id="rc_honest_fail_006",
                user_id=user.id,
                status=JobStatus.RUNNING.value,
                job_type=JobType.RECAP.value,
                engine="recap",
                preset_voice="thiha",
                source_audio_path="https://www.youtube.com/watch?v=honest_fail",
                recap_ref_audio_path="/nonexistent/path/to/missing_ref.wav",
                input_text="Honest fail recap",
                recap_config=json.dumps({
                    "credit_deducted": True,
                    "credits_charged": 1,
                    "config": {
                        "url": "https://www.youtube.com/watch?v=honest_fail",
                        "title": "Honest fail recap",
                        "tts_engine": "voxcpm",
                        "voice_slug": "thiha",
                        "voice_ref_path": "/nonexistent/path/to/missing_ref.wav",
                    },
                }),
            )
            s.add(job)
            await s.commit()

        RECAP_JOBS_REGISTRY["rc_honest_fail_006"] = {
            "id": "rc_honest_fail_006",
            "user_id": 9104,
            "status": "RUNNING",
            "credit_deducted": True,
            "credits_charged": 1,
        }

        req = RecapConfigRequest(
            url="https://www.youtube.com/watch?v=honest_fail",
            title="Honest fail recap",
            tts_engine="voxcpm",
            voice_slug="thiha",
            voice_ref_path="/nonexistent/path/to/missing_ref.wav",
        )

        with (
            patch("app.db.session.AsyncSessionLocal", session_maker),
            patch("app.webapp.api.AsyncSessionLocal", session_maker),
            patch("app.services.downloader.unshorten_url", side_effect=lambda u: u),
            patch("app.services.url_safety.validate_public_url", side_effect=lambda u: u),
            patch("app.webapp.api._send_recap_failure_dm", new_callable=AsyncMock, return_value=False),
        ):
            await _execute_recap_background_job(
                job_id="rc_honest_fail_006",
                req=req,
                user_id=user.id,
                chat_id=user.id,
                credit_deducted=True,
            )

        # Job must be marked FAILED
        async with session_maker() as s:
            j = await s.get(Job, "rc_honest_fail_006")
            assert j.status == JobStatus.FAILED.value
            assert j.error_message is not None

        # Registry must reflect FAILED
        reg_entry = RECAP_JOBS_REGISTRY.get("rc_honest_fail_006")
        assert reg_entry["status"] == "FAILED"
    finally:
        await engine.dispose()


# ── 3. Executor End-to-End & Script Persistence ─────────────────────────────────


@pytest.mark.asyncio
async def test_recap_executor_end_to_end_persists_script_and_timings(tmp_path):
    """A queued recap executes via process_recap_job to SUCCESS,
    persisting final narration script on job.final_script and recap_config,
    and recording stage timings.
    """
    fake_video = tmp_path / "final_recap.mp4"
    fake_video.write_bytes(b"dummy mp4 data")
    fake_audio = tmp_path / "final_audio.wav"
    fake_audio.write_bytes(b"dummy audio data")
    fake_srt = tmp_path / "subtitles.srt"
    fake_srt.write_text("1\n00:00:01,000 --> 00:00:04,000\nစမ်းသပ် ဇာတ်လမ်း စာသား ဖြစ်ပါတယ်။\n\n", encoding="utf-8")

    fake_script_file = tmp_path / "rc_exec_007_script.txt"
    fake_script_file.write_text(
        "🎬 Pro Movie Recap Script (VIRAL)\n🆔 Task: #rc_exec_007\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📖 ဇာတ်လမ်းအပြည့်အစုံ (Full Narration Copy):\n"
        "ဒီဇာတ်လမ်းကတော့ လူငယ်တစ်ယောက်ရဲ့ အံ့ဩဖွယ် စွန့်စားခန်း ဖြစ်ပါတယ်။\n\n"
        "📝 စာသားများ:\nစမ်းသပ် ဇာတ်လမ်း စာသား ဖြစ်ပါတယ်။\n",
        encoding="utf-8",
    )

    engine, session_maker = await _create_test_session_maker()
    try:
        async with session_maker() as s:
            user, _ = await get_or_create_user(s, telegram_id=9105)
            job = Job(
                id="rc_exec_007",
                user_id=user.id,
                status=JobStatus.QUEUED.value,
                job_type=JobType.RECAP.value,
                engine="recap",
                preset_voice="thiha",
                source_audio_path="https://www.youtube.com/watch?v=exec_test",
                recap_ref_audio_path=None,
                input_text="E2E test recap",
                recap_config=json.dumps({
                    "credit_deducted": True,
                    "credits_charged": 1,
                    "is_vip": False,
                    "phase": "full",
                    "config": {
                        "url": "https://www.youtube.com/watch?v=exec_test",
                        "title": "E2E test recap",
                        "tts_engine": "edge",
                        "voice_slug": "thiha",
                    },
                }),
            )
            s.add(job)
            await s.commit()

        fake_res = {
            "final_video": str(fake_video),
            "final": str(fake_video),
            "audio": str(fake_audio),
            "subtitles_srt": str(fake_srt),
            "stage_timings": {"download": 1.2, "planner": 2.5, "narration": 3.0, "render": 4.1},
            "warnings": [],
        }

        with (
            patch("app.db.session.AsyncSessionLocal", session_maker),
            patch("app.webapp.api.AsyncSessionLocal", session_maker),
            patch("app.worker.AsyncSessionLocal", session_maker),
            patch("app.services.downloader.MediaDownloader.generate_recap_video", return_value=fake_res),
            patch("app.webapp.api._build_clean_recap_script_file", return_value=str(fake_script_file)),
            patch("app.services.downloader.probe_local_media", return_value={"duration": 15.0}),
            patch("app.services.downloader.unshorten_url", side_effect=lambda u: u),
            patch("app.services.url_safety.validate_public_url", side_effect=lambda u: u),
        ):
            async with session_maker() as s:
                # Step 1: Claim job
                claimed_job = await claim_next_queued_job(s, allow_recap=True)
                assert claimed_job is not None
                assert claimed_job.id == "rc_exec_007"
                assert claimed_job.status == JobStatus.RUNNING.value

                # Step 2: Process job via process_recap_job
                success = await process_recap_job(s, claimed_job)
                assert success is True

            # Step 3: Verify DB state
            async with session_maker() as s:
                final_job = await s.get(Job, "rc_exec_007")
                assert final_job.status == JobStatus.SUCCESS.value
                assert final_job.result_path == str(fake_video)
                # Verify final_script column
                assert final_job.final_script is not None
                assert "ဒီဇာတ်လမ်းကတော့ လူငယ်တစ်ယောက်ရဲ့ အံ့ဩဖွယ် စွန့်စားခန်း ဖြစ်ပါတယ်။" in final_job.final_script
                # Verify recap_config JSON contains final_script and stage_timings
                cfg_data = json.loads(final_job.recap_config)
                assert cfg_data.get("final_script") == final_job.final_script
                assert "stage_timings" in cfg_data
                assert cfg_data["stage_timings"].get("render") == 4.1
    finally:
        await engine.dispose()


# ── 4. Orphan Sweep Test ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recap_orphan_sweep_fails_and_refunds_stale_running_recap():
    """A stale RUNNING recap is failed and auto-refunded by recover_stale_running_jobs;
    a fresh RUNNING recap is not touched.
    """
    engine, session_maker = await _create_test_session_maker()
    try:
        now = utc_now()
        async with session_maker() as s:
            user, _ = await get_or_create_user(s, telegram_id=9106)
            user.recap_credits = 0

            # Job 1: Stale RUNNING recap started 3000 seconds ago (cutoff is 2700s)
            job_stale = Job(
                id="rc_stale_008",
                user_id=user.id,
                status=JobStatus.RUNNING.value,
                job_type=JobType.RECAP.value,
                engine="recap",
                preset_voice="thiha",
                started_at=now - timedelta(seconds=3000),
                input_text="Stale recap",
                recap_config=json.dumps({
                    "credit_deducted": True,
                    "credits_charged": 1,
                    "config": {"title": "Stale recap"},
                }),
            )
            s.add(job_stale)

            # Job 2: Fresh RUNNING recap started 300 seconds ago (< 2700s)
            job_fresh = Job(
                id="rc_fresh_009",
                user_id=user.id,
                status=JobStatus.RUNNING.value,
                job_type=JobType.RECAP.value,
                engine="recap",
                preset_voice="thiha",
                started_at=now - timedelta(seconds=300),
                input_text="Fresh recap",
                recap_config=json.dumps({
                    "credit_deducted": True,
                    "credits_charged": 1,
                    "config": {"title": "Fresh recap"},
                }),
            )
            s.add(job_fresh)
            await s.commit()

        # Run stale recovery with default 2700s timeout
        with (
            patch("app.db.session.AsyncSessionLocal", session_maker),
            patch("app.webapp.api.AsyncSessionLocal", session_maker),
            patch("app.services.downloader.unshorten_url", side_effect=lambda u: u),
            patch("app.services.url_safety.validate_public_url", side_effect=lambda u: u),
            patch("app.webapp.api._send_recap_failure_dm", new_callable=AsyncMock, return_value=False),
        ):
            async with session_maker() as s:
                recovered = await recover_stale_running_jobs(s, recap_stale_timeout_seconds=2700)
                assert "rc_stale_008" in recovered
                assert "rc_fresh_009" not in recovered

        # Verify Job 1 failed and user refunded
        async with session_maker() as s:
            j1 = await s.get(Job, "rc_stale_008")
            assert j1.status == JobStatus.FAILED.value
            j2 = await s.get(Job, "rc_fresh_009")
            assert j2.status == JobStatus.RUNNING.value

            u = await s.get(User, user.id)
            assert u.recap_credits == 1  # 1 credit refunded

            # AuditLog verified
            audit_res = await s.execute(
                select(AuditLog).where(AuditLog.target_id == "rc_stale_008")
            )
            audit_entry = audit_res.scalar_one_or_none()
            assert audit_entry is not None
            assert audit_entry.action == "recap.stale_recovery"
    finally:
        await engine.dispose()


# ── 5. Park-and-Ask Quota Exhaustion Path ───────────────────────────────────────


@pytest.mark.asyncio
async def test_recap_quota_exhaustion_parks_as_waiting_choice(tmp_path):
    """When VoxCPM quota exhaustion occurs inside a worker-run recap,
    it raises RecapParkRequested and parks identically as WAITING_CHOICE.
    """
    from app.services.recap_engine.core import RecapParkRequested
    from app.webapp.api import (
        RecapConfigRequest,
        _execute_recap_background_job,
        RECAP_JOBS_REGISTRY,
    )

    engine, session_maker = await _create_test_session_maker()
    try:
        async with session_maker() as s:
            user, _ = await get_or_create_user(s, telegram_id=9107)
            job = Job(
                id="rc_park_010",
                user_id=user.id,
                status=JobStatus.RUNNING.value,
                job_type=JobType.RECAP.value,
                engine="recap",
                preset_voice="thiha",
                source_audio_path="https://www.youtube.com/watch?v=park_vid",
                recap_ref_audio_path=None,
                input_text="Park test recap",
                recap_config=json.dumps({
                    "credit_deducted": True,
                    "credits_charged": 1,
                    "is_vip": True,
                    "config": {"url": "https://www.youtube.com/watch?v=park_vid", "title": "Park test"},
                }),
            )
            s.add(job)
            await s.commit()

        RECAP_JOBS_REGISTRY["rc_park_010"] = {
            "id": "rc_park_010",
            "user_id": user.id,
            "status": "RUNNING",
            "credit_deducted": True,
            "credits_charged": 1,
        }

        req = RecapConfigRequest(
            url="https://www.youtube.com/watch?v=park_vid",
            title="Park test",
            tts_engine="voxcpm",
            voice_slug="thiha",
        )

        with (
            patch("app.db.session.AsyncSessionLocal", session_maker),
            patch("app.webapp.api.AsyncSessionLocal", session_maker),
            patch(
                "app.services.downloader.MediaDownloader.generate_recap_video",
                side_effect=RecapParkRequested("Quota exhausted"),
            ),
            patch("app.services.downloader.unshorten_url", side_effect=lambda u: u),
            patch("app.services.url_safety.validate_public_url", side_effect=lambda u: u),
            patch("app.services.voice_choice.send_choice_prompt", new_callable=AsyncMock, return_value=12345),
        ):
            await _execute_recap_background_job(
                job_id="rc_park_010",
                req=req,
                user_id=user.id,
                chat_id=user.id,
                is_vip=True,
                credit_deducted=True,
            )

        # Job must be parked as WAITING_CHOICE
        async with session_maker() as s:
            j = await s.get(Job, "rc_park_010")
            assert j.status == JobStatus.WAITING_CHOICE.value
            assert j.voice_choice_state is not None
            state_data = json.loads(j.voice_choice_state)
            assert state_data.get("kind") == "recap"

        # Registry must reflect WAITING_CHOICE
        assert RECAP_JOBS_REGISTRY["rc_park_010"]["status"] == "WAITING_CHOICE"
    finally:
        await engine.dispose()


# ── 6. Worker Concurrency Isolation ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recap_concurrency_isolation_keeps_voice_clone_claiming_unaffected():
    """When a recap task is active (at concurrency limit 1), allow_recap is False,
    so clone/TTS jobs are claimed without waiting for the recap render.
    """
    engine, session_maker = await _create_test_session_maker()
    try:
        async with session_maker() as s:
            user, _ = await get_or_create_user(s, telegram_id=9108)

            # A queued recap job
            job_recap = Job(
                id="rc_concur_011",
                user_id=user.id,
                status=JobStatus.QUEUED.value,
                job_type=JobType.RECAP.value,
                engine="recap",
                preset_voice="thiha",
                source_audio_path="https://www.youtube.com/watch?v=concur_recap",
                input_text="Concurrent recap",
            )
            s.add(job_recap)

            # A queued voice clone job
            job_voice = Job(
                id="voice_concur_012",
                user_id=user.id,
                status=JobStatus.QUEUED.value,
                job_type=JobType.VOICE_CLONE.value,
                engine="voxcpm",
                preset_voice="komin",
                source_audio_path=None,
                input_text="Concurrent voice clone",
            )
            s.add(job_voice)
            await s.commit()

        # Step 1: When concurrency is saturated (can_recap=False), claim voice job
        async with session_maker() as s:
            claimed_voice = await claim_next_queued_job(s, allow_recap=False)
            assert claimed_voice is not None
            assert claimed_voice.id == "voice_concur_012"
            assert claimed_voice.status == JobStatus.RUNNING.value

        # Step 2: The recap job is untouched in QUEUED
        async with session_maker() as s:
            rj = await s.get(Job, "rc_concur_011")
            assert rj.status == JobStatus.QUEUED.value
    finally:
        await engine.dispose()


# ── 7. Rollout Flag Controls ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_web_dispatch_disabled_when_recap_on_queue_is_true():
    """When settings.recap_on_queue is True, _dispatch_oldest_queued_recap returns False immediately."""
    from app.webapp.server import _dispatch_oldest_queued_recap

    with patch.object(settings, "recap_on_queue", True):
        dispatched = await _dispatch_oldest_queued_recap()
        assert dispatched is False


@pytest.mark.asyncio
async def test_admin_settings_validation_and_exposure():
    """Admin settings validate and expose recap_on_queue, recap_stale_job_timeout_seconds, and recap_worker_concurrency."""
    from app.webapp.api import _validate_admin_settings_updates

    # Valid updates
    updates = {
        "recap_on_queue": True,
        "recap_stale_job_timeout_seconds": 1800,
        "recap_worker_concurrency": 2,
    }
    cleaned = _validate_admin_settings_updates(updates)
    assert cleaned["recap_on_queue"] is True
    assert cleaned["recap_stale_job_timeout_seconds"] == 1800
    assert cleaned["recap_worker_concurrency"] == 2

    # Invalid bounds check: timeout too low
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        _validate_admin_settings_updates({"recap_stale_job_timeout_seconds": 10})

    # Invalid bounds check: concurrency too high
    with pytest.raises(HTTPException):
        _validate_admin_settings_updates({"recap_worker_concurrency": 20})
