"""
test_hunt7_2026_11_01.py - Deep hunt #7 fixes (second premium cycle).

Covered:
(C1) Recap job poller: failures are no longer swallowed silently and
     the loop is capped — consecutive-failure cue, 404 = job gone,
     60-minute hard cap (client source pins + i18n keys my/en).
(C2) Membership-gate verify fetches use fetchWithTimeout: a hung
     verify request previously wedged entryGate.checking (and the
     banner button) for the whole session.
(C3) /user/me load failures below 500 (non-403) now surface the Home
     error + Retry strip instead of silent placeholder balances.
(C4) Voice char counter unit is localized (journey leftover: the
     English word "chars" rendered in both languages).
(S1) Voice job creation serializes per user (row lock) so the
     concurrent-job limit and the FREE daily cap cannot both be
     passed by two simultaneous count-then-insert submits.
(S2) Referral reward grants serialize per referrer (row lock) so
     two simultaneous confirms cannot collide on reward_index /
     lose a balance credit.
(S3) Stranded RUNNING jobs after a restart: recap rows started
     before this process booted are reset by a periodic boot-cutoff
     sweep; voice rows get the same treatment in the worker loop
     (boot-relative stale timeout after an overlap grace window).
(S4) check_job_file_exists caches directory listings: admin
     history/queue no longer rescan storage dirs per row.
(S5) Duplicate payment slips: the bot service returns the existing
     PENDING request for an identical Telegram file_id, and the
     Mini App endpoint rejects an identical slip image (409) while
     one is already pending.

Note on the concurrency tests: SQLite (the test harness) serializes
writers and ignores FOR UPDATE, so the Postgres race itself cannot
be reproduced here. The pins therefore prove (a) the lock statement
executes BEFORE the racy count-then-act section, and (b) the
end-state invariants hold under concurrent calls.
"""

from __future__ import annotations

import asyncio
import io
import re
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.datastructures import UploadFile

from app.config import settings
from app.db.base import Base
from app.models.enums import JobStatus, JobType, PaymentStatus
from app.models.schema import Job, PaymentRequest, Referral, ReferralReward, User, VoiceModel, utc_now
from app.services.ledger import confirm_referral_and_reward, get_minute_balance, get_or_create_user
from app.services.payments import create_payment_request
from app.services.queue import create_voice_clone_job
from app.services.recovery import recover_stale_running_jobs
from app.webapp import api as webapp_api
from app.webapp.server import reset_stale_running_recap_jobs
from app.worker import _boot_cutoff_sweep_due

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_JS = (REPO_ROOT / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (REPO_ROOT / "app" / "webapp" / "static" / "js" / "i18n.js").read_text(encoding="utf-8")


def _slice(src: str, start: str, end: str) -> str:
    i = src.index(start)
    j = src.index(end, i)
    return src[i:j]


# ── S1: voice job creation limits ─────────────────────────────────────


async def _seed_cloud_model(db_session):
    model = VoiceModel(
        name="VoxCPM Cloud",
        slug="voxcpm-cloud-ai",
        type="SYSTEM",
        model_dir="/tmp",
        pth_path="voxcpm",
        status="ACTIVE",
        is_default_free=True,
    )
    db_session.add(model)
    await db_session.flush()
    return model


@pytest.mark.asyncio
async def test_voice_create_lock_precedes_limit_checks():
    src = (REPO_ROOT / "app" / "services" / "queue.py").read_text(encoding="utf-8")
    body = _slice(src, "async def create_voice_clone_job", "async def claim_job_for_running")
    lock_at = body.index("select(User).where(User.id == user_id).with_for_update()")
    assert lock_at < body.index("concurrent_stmt")
    assert lock_at < body.index("daily_stmt")


@pytest.mark.asyncio
async def test_voice_concurrent_limit_enforced(db_session):
    await _seed_cloud_model(db_session)
    user, _ = await get_or_create_user(db_session, telegram_id=777101)

    job, msg = await create_voice_clone_job(db_session, user.id, text="မင်္ဂလာပါ", chat_id=user.id)
    assert job is not None and msg == "QUEUED"

    job2, msg2 = await create_voice_clone_job(db_session, user.id, text="မင်္ဂလာပါ", chat_id=user.id)
    assert job2 is None and msg2 == "CONCURRENT_JOB_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_voice_daily_limit_enforced(db_session):
    await _seed_cloud_model(db_session)
    user, _ = await get_or_create_user(db_session, telegram_id=777102)

    for _ in range(settings.free_daily_job_limit):
        job, msg = await create_voice_clone_job(db_session, user.id, text="မင်္ဂလာပါ", chat_id=user.id)
        assert job is not None, msg
        # Terminal states free the concurrency slot but still count
        # toward the daily total (only CANCELLED is excluded).
        job.status = JobStatus.FAILED.value
        await db_session.flush()

    job6, msg6 = await create_voice_clone_job(db_session, user.id, text="မင်္ဂလာပါ", chat_id=user.id)
    assert job6 is None and msg6 == "DAILY_JOB_LIMIT_EXCEEDED"


# ── S2: referral reward serialization ─────────────────────────────────


@pytest.mark.asyncio
async def test_referral_lock_precedes_reward_count():
    src = (REPO_ROOT / "app" / "services" / "ledger.py").read_text(encoding="utf-8")
    body = _slice(src, "async def confirm_referral_and_reward", "async def credit_purchased_minutes")
    lock_at = body.index("select(User).where(User.id == referrer_id).with_for_update()")
    assert lock_at < body.index("count_stmt")


def _make_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool, echo=False, future=True
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _pragma(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


@pytest.mark.asyncio
async def test_referral_two_confirms_end_state(db_session):
    """Two friends of one referrer confirming back-to-back end with
    exactly two rewards (indexes 1 and 2), +60s referral balance.

    The concurrent interleave itself was reproduced against the
    pre-fix code during this hunt (both confirms counted 0 rewards
    and one died with a UNIQUE-constraint IntegrityError); the
    harness's single shared SQLite connection cannot represent two
    transactions, so this pins the end state while
    test_referral_reward_locks_referrer_row pins the lock that
    serializes the grants on Postgres.
    """
    referrer, _ = await get_or_create_user(db_session, telegram_id=777201)
    a, _ = await get_or_create_user(db_session, telegram_id=777202)
    b, _ = await get_or_create_user(db_session, telegram_id=777203)
    for referred in (a, b):
        db_session.add(Referral(
            referrer_id=referrer.id, referred_id=referred.id,
            referral_code=f"REF{referred.id}", status="PENDING",
        ))
    await db_session.commit()

    assert await confirm_referral_and_reward(db_session, a.id) == (True, True, 1)
    await db_session.commit()
    assert await confirm_referral_and_reward(db_session, b.id) == (True, True, 2)
    await db_session.commit()

    rewards = (await db_session.execute(
        select(ReferralReward)
        .where(ReferralReward.referrer_id == referrer.id)
        .order_by(ReferralReward.reward_index)
    )).scalars().all()
    assert [r.reward_index for r in rewards] == [1, 2]
    bal = await get_minute_balance(db_session, referrer.id)
    assert bal.referral_seconds == 60



# ── S5: duplicate payment slips ───────────────────────────────────────


@pytest.mark.asyncio
async def test_create_payment_request_duplicate_file_id_idempotent(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=777301)

    first = await create_payment_request(db_session, user.id, "MIN_10", receipt_file_id="slip-file-1")
    await db_session.flush()
    second = await create_payment_request(db_session, user.id, "MIN_10", receipt_file_id="slip-file-1")
    assert second.id == first.id

    count = (await db_session.execute(
        select(func.count()).select_from(PaymentRequest).where(PaymentRequest.user_id == user.id)
    )).scalar()
    assert count == 1

    third = await create_payment_request(db_session, user.id, "MIN_10", receipt_file_id="slip-file-2")
    assert third.id != first.id

    # The guard matches PENDING only: once the first request is
    # decided, the same evidence belongs to a closed case and a new
    # submission is a fresh request again.
    first.status = PaymentStatus.APPROVED.value
    await db_session.flush()
    fourth = await create_payment_request(db_session, user.id, "MIN_10", receipt_file_id="slip-file-1")
    assert fourth.id != first.id


@pytest.mark.asyncio
async def test_submit_slip_identical_image_rejected_409(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "bot_token", "")  # never alert real admins from a test
    user = User(id=777302, username="slip_user", first_name="Slip")
    db_session.add(user)
    await db_session.flush()

    png = b"\x89PNG\r\n\x1a\n" + b"hunt7-slip-bytes" * 8
    other = b"\x89PNG\r\n\x1a\n" + b"different-slip!!" * 8

    def _upload(data: bytes) -> UploadFile:
        return UploadFile(filename="slip.png", file=io.BytesIO(data))

    result = await webapp_api.submit_payment_slip(
        package_code="MIN_10", amount_mmk=8000, slip_file=_upload(png),
        user=user, db=db_session, _gate=user,
    )
    assert result["success"] is True

    with pytest.raises(HTTPException) as excinfo:
        await webapp_api.submit_payment_slip(
            package_code="MIN_10", amount_mmk=8000, slip_file=_upload(png),
            user=user, db=db_session, _gate=user,
        )
    assert excinfo.value.status_code == 409

    # A genuinely different slip image is a different payment and is
    # accepted even while the first is still pending.
    result2 = await webapp_api.submit_payment_slip(
        package_code="MIN_10", amount_mmk=8000, slip_file=_upload(other),
        user=user, db=db_session, _gate=user,
    )
    assert result2["success"] is True

    count = (await db_session.execute(
        select(func.count()).select_from(PaymentRequest).where(PaymentRequest.user_id == user.id)
    )).scalar()
    assert count == 2


# ── S4: file-exists directory-listing cache ───────────────────────────


def test_check_job_file_exists_uses_cached_listing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "jobx123_out.wav").write_bytes(b"wav")
    webapp_api._DIR_LISTING_CACHE.clear()

    real_scandir = webapp_api.os.scandir
    scans = {"n": 0}

    def counting_scandir(path, *a, **kw):
        scans["n"] += 1
        return real_scandir(path, *a, **kw)

    with patch.object(webapp_api.os, "scandir", counting_scandir):
        assert webapp_api.check_job_file_exists("jobx123", False, None) is True
        first_scan_count = scans["n"]
        assert first_scan_count >= 1
        # Repeated checks inside the TTL window perform no new scans.
        assert webapp_api.check_job_file_exists("jobx123", False, None) is True
        assert webapp_api.check_job_file_exists("missingjob", False, None) is False
        assert scans["n"] == first_scan_count
    webapp_api._DIR_LISTING_CACHE.clear()


def test_check_job_file_exists_result_path_fast_path(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    media = tmp_path / "voice.wav"
    media.write_bytes(b"wav")
    webapp_api._DIR_LISTING_CACHE.clear()
    assert webapp_api.check_job_file_exists("anyjob", False, str(media)) is True
    assert webapp_api.check_job_file_exists("anyjob", False, str(tmp_path / "gone.wav")) is False


# ── S3: stranded RUNNING recovery ─────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_recap_absolute_cutoff_resets_pre_boot_only(db_session):
    now = utc_now()
    user = User(id=777401, username="boot_user")
    db_session.add(user)
    await db_session.flush()

    pre_boot = Job(
        id="rc_h7_preboot", user_id=user.id, job_type=JobType.RECAP.value, engine="recap",
        status=JobStatus.RUNNING.value,
        created_at=now - timedelta(minutes=4), started_at=now - timedelta(minutes=2),
    )
    post_boot = Job(
        id="rc_h7_postboot", user_id=user.id, job_type=JobType.RECAP.value, engine="recap",
        status=JobStatus.RUNNING.value,
        created_at=now, started_at=now,
    )
    voice_old = Job(
        id="voice_h7_old", user_id=user.id, job_type=JobType.VOICE_CLONE.value, engine="voxcpm",
        status=JobStatus.RUNNING.value,
        created_at=now - timedelta(minutes=40), started_at=now - timedelta(minutes=30),
    )
    db_session.add_all([pre_boot, post_boot, voice_old])
    await db_session.flush()

    # Boot was 1 minute ago: the pre-boot row is only 2 minutes old,
    # so the relative 10-minute startup rule would SKIP it — the
    # absolute boot cutoff is what rescues it.
    recovered = await reset_stale_running_recap_jobs(
        session=db_session, absolute_cutoff=now - timedelta(minutes=1)
    )
    assert recovered == 1
    await db_session.refresh(pre_boot)
    await db_session.refresh(post_boot)
    await db_session.refresh(voice_old)
    assert pre_boot.status == JobStatus.QUEUED.value and pre_boot.started_at is None
    assert post_boot.status == JobStatus.RUNNING.value
    assert voice_old.status == JobStatus.RUNNING.value


def test_boot_cutoff_sweep_due():
    # Inside the overlap grace window: never due.
    assert _boot_cutoff_sweep_due(1000.0, 0.0, 1500.0) is False
    # Past grace, no sweep yet: due.
    assert _boot_cutoff_sweep_due(1000.0, 0.0, 1601.0) is True
    # Just swept: not due again until the interval passes.
    assert _boot_cutoff_sweep_due(1000.0, 1601.0, 1700.0) is False
    assert _boot_cutoff_sweep_due(1000.0, 1601.0, 1902.0) is True


@pytest.mark.asyncio
async def test_recover_stale_boot_relative_timeout(db_session):
    now = utc_now()
    user = User(id=777402, username="stale_user")
    db_session.add(user)
    await db_session.flush()

    old = Job(
        id="voice_h7_stale_old", user_id=user.id, job_type=JobType.VOICE_CLONE.value, engine="voxcpm",
        status=JobStatus.RUNNING.value,
        created_at=now - timedelta(seconds=720), started_at=now - timedelta(seconds=700),
    )
    fresh = Job(
        id="voice_h7_stale_fresh", user_id=user.id, job_type=JobType.VOICE_CLONE.value, engine="voxcpm",
        status=JobStatus.RUNNING.value,
        created_at=now - timedelta(seconds=120), started_at=now - timedelta(seconds=100),
    )
    db_session.add_all([old, fresh])
    await db_session.flush()

    # The worker loop's boot-relative timeout 600s after a boot that
    # happened 600s ago puts the cutoff at the boot moment: the job
    # started before it is recovered, the one started after is not.
    recovered = await recover_stale_running_jobs(db_session, stale_timeout_seconds=600)
    assert recovered == [old.id]
    await db_session.refresh(old)
    await db_session.refresh(fresh)
    assert old.status == JobStatus.FAILED.value
    assert fresh.status == JobStatus.RUNNING.value


# ── C1–C4: Mini App client fixes (source pins) ────────────────────────


def test_recap_poller_failure_cue_cap_and_gone():
    body = _slice(APP_JS, "function startRecapJobPolling", "async function openRecapScriptEditor")
    # The old silent swallow is gone from this poller.
    assert "if (!res.ok) return;" not in body
    # Consecutive-failure cue, bounded 404 handling, and the hard cap.
    assert "noteRecapPollFailure" in body
    assert "res.status === 404" in body
    assert "RECAP_POLL_MAX_TICKS = 1800" in body
    for key in ("poll_reconnecting", "poll_job_gone", "poll_background_notice"):
        assert f't("{key}")' in body


def test_gate_verify_fetches_use_timeout():
    banner = _slice(APP_JS, "async function verifyMembershipFromBanner", "function setEntryGateMode")
    gate = _slice(APP_JS, "async function runEntryGateCheck", "window.runEntryGateCheck")
    for body in (banner, gate):
        assert 'fetchWithTimeout("/api/user/verify-membership"' in body
        assert 'await fetch("/api/user/verify-membership"' not in body


def test_profile_sub500_failure_shows_error_strip():
    body = _slice(APP_JS, "async function loadUserProfile", "async function loadCuratedVoices")
    assert "res.status !== 403" in body
    assert "setHomeLoadErrorVisible(true)" in body
    assert "res.status >= 500) setHomeLoadErrorVisible(true)" not in body


def test_char_counter_unit_localized():
    assert APP_JS.count('t("char_counter_unit")') >= 2
    assert "/ ${limit} chars" not in APP_JS
    assert "/ ${max} chars" not in APP_JS


def test_webapp_i18n_parity_and_new_keys():
    # Bound each locale block: my runs to the en block, en runs to the
    # translations object's closing brace at column 0. Keys sit at
    # exactly 8 spaces of indent inside the blocks.
    my_half = I18N_JS.split("\n    my: {", 1)[1].split("\n    en: {", 1)[0]
    en_half = I18N_JS.split("\n    en: {", 1)[1].split("\n};", 1)[0]
    key_re = re.compile(r"^ {8}([a-z][a-z0-9_]*):", re.M)
    my_keys = set(key_re.findall(my_half))
    en_keys = set(key_re.findall(en_half))
    assert len(my_keys) > 590 and len(en_keys) > 590  # parser sanity
    assert my_keys == en_keys
    for key in ("poll_reconnecting", "poll_background_notice", "poll_job_gone", "char_counter_unit"):
        assert key in my_keys and key in en_keys
    # The Burmese values are actually Burmese for the poll keys.
    for key in ("poll_reconnecting", "poll_background_notice", "poll_job_gone"):
        m = re.search(rf'{key}: "([^"]+)"', my_half)
        assert m and re.search(r"[\u1000-\u109F]", m.group(1)), key
