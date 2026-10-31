"""
test_deploy_overlap_reset.py - Regression tests for Task 2 (deploy-overlap orphan-reset guard).

Requirements:
1. Only reset RUNNING recap rows whose last state change is older than 10 minutes.
2. Freshly-RUNNING row (< 10 min) survives the startup reset.
3. Stale RUNNING row (> 10 min) is reset to QUEUED with started_at=None.
4. Non-recap jobs remain untouched.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.enums import JobStatus, JobType
from app.models.schema import Job, User, utc_now
from app.webapp.server import reset_stale_running_recap_jobs, webapp_app


@pytest.mark.asyncio
async def test_fresh_running_survives_and_stale_running_is_reset(db_session):
    """Requirement 2: Freshly-RUNNING row survives; stale row (>10 min) resets to QUEUED."""
    now = utc_now()
    user = User(id=77001, username="test_overlap_user")
    db_session.add(user)
    await db_session.flush()

    # 1. Freshly-running recap job (started 2 minutes ago)
    fresh_recap = Job(
        id="rc_fresh_001",
        user_id=user.id,
        job_type=JobType.RECAP.value,
        engine="recap",
        status=JobStatus.RUNNING.value,
        created_at=now - timedelta(minutes=5),
        started_at=now - timedelta(minutes=2),
    )

    # 2. Stale running recap job (started 15 minutes ago, > 10 min threshold)
    stale_recap = Job(
        id="rc_stale_002",
        user_id=user.id,
        job_type=JobType.RECAP.value,
        engine="recap",
        status=JobStatus.RUNNING.value,
        created_at=now - timedelta(minutes=20),
        started_at=now - timedelta(minutes=15),
    )

    # 3. Non-recap voice clone job (even if stale, should NOT be touched by recap reset)
    voice_job = Job(
        id="vj_voice_003",
        user_id=user.id,
        job_type=JobType.VOICE_CLONE.value,
        engine="voxcpm",
        status=JobStatus.RUNNING.value,
        created_at=now - timedelta(minutes=30),
        started_at=now - timedelta(minutes=25),
    )

    # 4. Legitimate already-QUEUED recap job (should remain QUEUED)
    queued_recap = Job(
        id="rc_queued_004",
        user_id=user.id,
        job_type=JobType.RECAP.value,
        engine="recap",
        status=JobStatus.QUEUED.value,
        created_at=now - timedelta(minutes=1),
        started_at=None,
    )

    db_session.add_all([fresh_recap, stale_recap, voice_job, queued_recap])
    await db_session.commit()

    # Execute the age-aware reset (cutoff: 10 minutes)
    reset_count = await reset_stale_running_recap_jobs(session=db_session, cutoff_minutes=10)

    # Exactly 1 stale recap row should have been reset
    assert reset_count == 1

    # Reload rows from DB to check status
    fresh_db = await db_session.get(Job, "rc_fresh_001")
    stale_db = await db_session.get(Job, "rc_stale_002")
    voice_db = await db_session.get(Job, "vj_voice_003")
    queued_db = await db_session.get(Job, "rc_queued_004")

    # Fresh recap job survived!
    assert fresh_db.status == JobStatus.RUNNING.value
    assert fresh_db.started_at is not None

    # Stale recap job was reset to QUEUED with started_at=None
    assert stale_db.status == JobStatus.QUEUED.value
    assert stale_db.started_at is None

    # Non-recap and already-queued jobs were unaffected
    assert voice_db.status == JobStatus.RUNNING.value
    assert queued_db.status == JobStatus.QUEUED.value


@pytest.mark.asyncio
async def test_null_started_at_falls_back_to_created_at(db_session):
    """Rows without started_at use created_at to determine staleness."""
    now = utc_now()
    user = User(id=77002, username="test_null_started_user")
    db_session.add(user)
    await db_session.flush()

    # Fresh job without started_at (created 3 min ago) -> survives
    fresh_no_start = Job(
        id="rc_no_start_fresh",
        user_id=user.id,
        job_type=JobType.RECAP.value,
        engine="recap",
        status=JobStatus.RUNNING.value,
        created_at=now - timedelta(minutes=3),
        started_at=None,
    )

    # Stale job without started_at (created 12 min ago) -> reset
    stale_no_start = Job(
        id="rc_no_start_stale",
        user_id=user.id,
        job_type=JobType.RECAP.value,
        engine="recap",
        status=JobStatus.RUNNING.value,
        created_at=now - timedelta(minutes=12),
        started_at=None,
    )

    db_session.add_all([fresh_no_start, stale_no_start])
    await db_session.commit()

    reset_count = await reset_stale_running_recap_jobs(session=db_session, cutoff_minutes=10)
    assert reset_count == 1

    r_fresh = await db_session.get(Job, "rc_no_start_fresh")
    r_stale = await db_session.get(Job, "rc_no_start_stale")

    assert r_fresh.status == JobStatus.RUNNING.value
    assert r_stale.status == JobStatus.QUEUED.value


@pytest.mark.asyncio
async def test_lifespan_invokes_age_aware_reset(db_session):
    """Verify webapp lifespan startup executes the age-aware recap reset."""
    now = utc_now()
    user = User(id=77003, username="lifespan_reset_user")
    db_session.add(user)
    await db_session.flush()

    fresh_recap = Job(
        id="rc_life_fresh",
        user_id=user.id,
        job_type=JobType.RECAP.value,
        engine="recap",
        status=JobStatus.RUNNING.value,
        created_at=now - timedelta(minutes=3),
        started_at=now - timedelta(minutes=3),
    )
    stale_recap = Job(
        id="rc_life_stale",
        user_id=user.id,
        job_type=JobType.RECAP.value,
        engine="recap",
        status=JobStatus.RUNNING.value,
        created_at=now - timedelta(minutes=15),
        started_at=now - timedelta(minutes=15),
    )
    db_session.add_all([fresh_recap, stale_recap])
    await db_session.commit()

    # Wrap db_session in a callable context manager for AsyncSessionLocal patch
    class FakeSessionContext:
        def __init__(self, s):
            self.s = s
        async def __aenter__(self):
            return self.s
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    with patch("app.webapp.server.init_db", AsyncMock()), \
         patch("app.webapp.api.cleanup_expired_jobs", AsyncMock()), \
         patch("app.db.session.AsyncSessionLocal", side_effect=lambda: FakeSessionContext(db_session)):

        # Trigger lifespan startup
        from app.webapp.server import lifespan
        async with lifespan(webapp_app):
            pass

    r_fresh = await db_session.get(Job, "rc_life_fresh")
    r_stale = await db_session.get(Job, "rc_life_stale")

    assert r_fresh.status == JobStatus.RUNNING.value
    assert r_stale.status == JobStatus.QUEUED.value
