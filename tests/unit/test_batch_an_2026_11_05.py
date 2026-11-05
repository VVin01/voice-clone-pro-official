"""Batch AN (2026-11-05): per-user DAILY VOICE-MINUTES cap, split by tier.

Owner order: "တစ်နေ့ အသုံးချနိုင်တဲ့ မိနစ် အကန့်အသတ် ထား" + tier split
"free နဲ့ paid ခွဲပေးအုံး". FREE users get
settings.voice_daily_limit_minutes_free (default 5) and PURCHASED users
settings.voice_daily_limit_minutes_paid (default 30); 0 = unlimited for
that tier; admins exempt. Enforced inside create_voice_clone_job's
per-user locked block: today's VOICE_CLONE jobs count SUCCESS actuals
(seconds_consumed, with a text-estimate fallback when no actual was
recorded) + QUEUED/RUNNING estimates; FAILED/CANCELLED consume nothing.
Rejection reason: DAILY_MINUTE_LIMIT_EXCEEDED, strictly when
consumed + new estimate > cap (exactly at the cap is allowed).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config import settings
from app.handlers.clone import voice_queue_failure_message
from app.models.enums import AccountCategory, JobStatus, JobType
from app.models.schema import Job, VoiceModel
from app.services.ledger import credit_purchased_minutes, get_or_create_user
from app.services.queue import create_voice_clone_job, estimate_voice_job_seconds
from app.webapp.api import _voice_job_error_detail, _voice_job_error_status


@pytest.fixture(autouse=True)
def _tier_caps(monkeypatch):
    monkeypatch.setattr(settings, "voice_daily_limit_minutes_free", 5)
    monkeypatch.setattr(settings, "voice_daily_limit_minutes_paid", 30)
    yield


def _today_start() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


async def _make_model(db_session, tmp_path, slug="an-model"):
    pth = tmp_path / f"{slug}.pth"
    pth.write_bytes(b"pth")
    model = VoiceModel(
        name="ANModel", slug=slug, type="SYSTEM", model_dir=str(tmp_path),
        pth_path="voxcpm", status="ACTIVE", is_default_free=True,
        allowed_for_free=True, allowed_for_purchased=True,
    )
    db_session.add(model)
    await db_session.flush()
    return model


async def _seed_job(db_session, user_id, model_id, *, status, seconds_consumed=0,
                    input_text="seed text", minutes_ago=5,
                    job_type=JobType.VOICE_CLONE.value, speed_preset="normal"):
    job = Job(
        id=f"seed-{user_id}-{status}-{minutes_ago}-{len(input_text)}-{seconds_consumed}-{job_type}",
        user_id=user_id,
        chat_id=user_id,
        input_text=input_text,
        edge_voice="my-MM-ThihaNeural",
        model_id=model_id,
        status=status,
        job_type=job_type,
        seconds_consumed=seconds_consumed,
        speed_preset=speed_preset,
        created_at=_today_start() + timedelta(minutes=minutes_ago),
    )
    db_session.add(job)
    await db_session.flush()
    return job


async def _submit(db_session, user_id, model_id, text="hello"):
    return await create_voice_clone_job(
        session=db_session, user_id=user_id, text=text,
        edge_voice="my-MM-ThihaNeural", model_id=model_id,
    )


@pytest.mark.asyncio
async def test_daily_minutes_under_cap_passes(db_session, tmp_path):
    """A fresh FREE user with no usage today submits a small job fine."""
    user, _ = await get_or_create_user(db_session, telegram_id=557001)
    model = await _make_model(db_session, tmp_path)
    job, reason = await _submit(db_session, user.id, model.id, "hello world, this is a small test")
    assert job is not None
    assert reason == "QUEUED"


@pytest.mark.asyncio
async def test_daily_minutes_free_capped_at_5(db_session, tmp_path):
    """FREE cap is 300s: 295 consumed + ~13s new estimate exceeds it."""
    user, _ = await get_or_create_user(db_session, telegram_id=557002)
    assert user.account_category == AccountCategory.FREE.value
    model = await _make_model(db_session, tmp_path)
    await _seed_job(db_session, user.id, model.id, status=JobStatus.SUCCESS.value,
                    seconds_consumed=295)
    job, reason = await _submit(db_session, user.id, model.id, "x" * 200)
    assert job is None
    assert reason == "DAILY_MINUTE_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_daily_minutes_tier_split_same_usage(db_session, tmp_path):
    """Identical usage (295s consumed + ~13s new): FREE is rejected at the
    5-minute cap while PURCHASED is still allowed under the 30-minute cap."""
    free_user, _ = await get_or_create_user(db_session, telegram_id=557003)
    paid_user, _ = await get_or_create_user(db_session, telegram_id=557004)
    await credit_purchased_minutes(db_session, user_id=paid_user.id, minutes=60)
    await db_session.flush()
    assert paid_user.account_category == AccountCategory.PURCHASED.value
    model = await _make_model(db_session, tmp_path)
    for u in (free_user, paid_user):
        await _seed_job(db_session, u.id, model.id, status=JobStatus.SUCCESS.value,
                        seconds_consumed=295)

    job_f, reason_f = await _submit(db_session, free_user.id, model.id, "x" * 200)
    assert job_f is None
    assert reason_f == "DAILY_MINUTE_LIMIT_EXCEEDED"

    job_p, reason_p = await _submit(db_session, paid_user.id, model.id, "x" * 200)
    assert job_p is not None
    assert reason_p == "QUEUED"


@pytest.mark.asyncio
async def test_daily_minutes_paid_capped_at_30(db_session, tmp_path):
    """PURCHASED cap is 1800s: 1795 consumed + ~13s new estimate exceeds it."""
    user, _ = await get_or_create_user(db_session, telegram_id=557005)
    await credit_purchased_minutes(db_session, user_id=user.id, minutes=60)
    await db_session.flush()
    assert user.account_category == AccountCategory.PURCHASED.value
    model = await _make_model(db_session, tmp_path)
    await _seed_job(db_session, user.id, model.id, status=JobStatus.SUCCESS.value,
                    seconds_consumed=1795)
    job, reason = await _submit(db_session, user.id, model.id, "x" * 200)
    assert job is None
    assert reason == "DAILY_MINUTE_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_daily_minutes_admin_exempt(db_session, tmp_path, monkeypatch):
    """Admins skip the cap entirely (hermetic ADMIN_IDS pattern)."""
    monkeypatch.setattr(settings, "admin_ids_raw", "557009")
    admin_id = list(settings.admin_ids)[0]
    admin, _ = await get_or_create_user(db_session, telegram_id=admin_id)
    model = await _make_model(db_session, tmp_path)
    await _seed_job(db_session, admin.id, model.id, status=JobStatus.SUCCESS.value,
                    seconds_consumed=9999)
    job, reason = await _submit(db_session, admin.id, model.id, "admin text")
    assert job is not None
    assert reason == "QUEUED"


@pytest.mark.asyncio
async def test_daily_minutes_zero_free_disables_only_free(db_session, tmp_path, monkeypatch):
    """free=0 unlimits FREE only; PURCHASED stays capped at 30."""
    monkeypatch.setattr(settings, "voice_daily_limit_minutes_free", 0)
    free_user, _ = await get_or_create_user(db_session, telegram_id=557010)
    paid_user, _ = await get_or_create_user(db_session, telegram_id=557011)
    await credit_purchased_minutes(db_session, user_id=paid_user.id, minutes=60)
    await db_session.flush()
    model = await _make_model(db_session, tmp_path)
    await _seed_job(db_session, free_user.id, model.id, status=JobStatus.SUCCESS.value,
                    seconds_consumed=9999)
    job, reason = await _submit(db_session, free_user.id, model.id, "hello")
    assert job is not None
    assert reason == "QUEUED"

    await _seed_job(db_session, paid_user.id, model.id, status=JobStatus.SUCCESS.value,
                    seconds_consumed=1795)
    job, reason = await _submit(db_session, paid_user.id, model.id, "x" * 200)
    assert job is None
    assert reason == "DAILY_MINUTE_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_daily_minutes_zero_paid_disables_only_paid(db_session, tmp_path, monkeypatch):
    """paid=0 unlimits PURCHASED only; FREE stays capped at 5."""
    monkeypatch.setattr(settings, "voice_daily_limit_minutes_paid", 0)
    paid_user, _ = await get_or_create_user(db_session, telegram_id=557012)
    await credit_purchased_minutes(db_session, user_id=paid_user.id, minutes=60)
    await db_session.flush()
    free_user, _ = await get_or_create_user(db_session, telegram_id=557013)
    model = await _make_model(db_session, tmp_path)
    await _seed_job(db_session, paid_user.id, model.id, status=JobStatus.SUCCESS.value,
                    seconds_consumed=9999)
    job, reason = await _submit(db_session, paid_user.id, model.id, "hello")
    assert job is not None
    assert reason == "QUEUED"

    await _seed_job(db_session, free_user.id, model.id, status=JobStatus.SUCCESS.value,
                    seconds_consumed=295)
    job, reason = await _submit(db_session, free_user.id, model.id, "x" * 200)
    assert job is None
    assert reason == "DAILY_MINUTE_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_daily_minutes_failed_and_cancelled_do_not_count(db_session, tmp_path):
    """FAILED and CANCELLED jobs consume nothing against the cap."""
    user, _ = await get_or_create_user(db_session, telegram_id=557014)
    model = await _make_model(db_session, tmp_path)
    await _seed_job(db_session, user.id, model.id, status=JobStatus.FAILED.value,
                    seconds_consumed=9999, minutes_ago=6)
    await _seed_job(db_session, user.id, model.id, status=JobStatus.CANCELLED.value,
                    seconds_consumed=9999, minutes_ago=7)
    job, reason = await _submit(db_session, user.id, model.id, "hello")
    assert job is not None
    assert reason == "QUEUED"


@pytest.mark.asyncio
async def test_daily_minutes_running_job_estimate_counts(db_session, tmp_path, monkeypatch):
    """A RUNNING job's text estimate counts (concurrency limit raised so the
    minutes check — not the concurrency check — is what fires)."""
    monkeypatch.setattr(settings, "max_concurrent_jobs_per_user", 99)
    user, _ = await get_or_create_user(db_session, telegram_id=557015)
    model = await _make_model(db_session, tmp_path)
    long_text = "a" * 30000
    assert estimate_voice_job_seconds(long_text, "normal") > 300
    await _seed_job(db_session, user.id, model.id, status=JobStatus.RUNNING.value,
                    input_text=long_text)
    job, reason = await _submit(db_session, user.id, model.id, "hello")
    assert job is None
    assert reason == "DAILY_MINUTE_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_daily_minutes_success_without_consumed_falls_back_to_estimate(db_session, tmp_path):
    """A SUCCESS row whose seconds_consumed was never recorded (0) is counted
    by its text estimate — the same consumed→estimated fallback the bot's
    history display uses — so unrecorded rows cannot bypass the cap."""
    user, _ = await get_or_create_user(db_session, telegram_id=557016)
    model = await _make_model(db_session, tmp_path)
    long_text = "a" * 30000
    await _seed_job(db_session, user.id, model.id, status=JobStatus.SUCCESS.value,
                    seconds_consumed=0, input_text=long_text)
    job, reason = await _submit(db_session, user.id, model.id, "hello")
    assert job is None
    assert reason == "DAILY_MINUTE_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_daily_minutes_recap_jobs_do_not_count(db_session, tmp_path):
    """Only VOICE_CLONE jobs count — a same-day RECAP job's seconds are ignored."""
    user, _ = await get_or_create_user(db_session, telegram_id=557017)
    model = await _make_model(db_session, tmp_path)
    await _seed_job(db_session, user.id, model.id, status=JobStatus.SUCCESS.value,
                    seconds_consumed=9999, job_type=JobType.RECAP.value)
    job, reason = await _submit(db_session, user.id, model.id, "hello")
    assert job is not None
    assert reason == "QUEUED"


@pytest.mark.asyncio
async def test_daily_minutes_boundary_exactly_at_cap_passes(db_session, tmp_path):
    """Boundary (PAID): consumed + estimate == cap is ALLOWED (rejection is
    strictly >). 352 chars estimates to exactly 23.0s; 1777 + 23.0 == 1800."""
    user, _ = await get_or_create_user(db_session, telegram_id=557018)
    await credit_purchased_minutes(db_session, user_id=user.id, minutes=60)
    await db_session.flush()
    model = await _make_model(db_session, tmp_path)
    await _seed_job(db_session, user.id, model.id, status=JobStatus.SUCCESS.value,
                    seconds_consumed=1777)
    text = "a" * 352
    assert estimate_voice_job_seconds(text, "normal") == 23.0
    job, reason = await _submit(db_session, user.id, model.id, text)
    assert job is not None
    assert reason == "QUEUED"


@pytest.mark.asyncio
async def test_daily_minutes_boundary_one_char_over_rejected(db_session, tmp_path):
    """One more character pushes consumed + estimate past the PAID cap."""
    user, _ = await get_or_create_user(db_session, telegram_id=557019)
    await credit_purchased_minutes(db_session, user_id=user.id, minutes=60)
    await db_session.flush()
    model = await _make_model(db_session, tmp_path)
    await _seed_job(db_session, user.id, model.id, status=JobStatus.SUCCESS.value,
                    seconds_consumed=1777)
    text = "a" * 353
    assert estimate_voice_job_seconds(text, "normal") > 23.0
    job, reason = await _submit(db_session, user.id, model.id, text)
    assert job is None
    assert reason == "DAILY_MINUTE_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_daily_minutes_yesterday_does_not_count(db_session, tmp_path):
    """Usage from before today's UTC midnight is outside the window."""
    user, _ = await get_or_create_user(db_session, telegram_id=557020)
    model = await _make_model(db_session, tmp_path)
    old = Job(
        id="seed-yesterday-557020",
        user_id=user.id,
        chat_id=user.id,
        input_text="old",
        edge_voice="my-MM-ThihaNeural",
        model_id=model.id,
        status=JobStatus.SUCCESS.value,
        job_type=JobType.VOICE_CLONE.value,
        seconds_consumed=9999,
        created_at=_today_start() - timedelta(minutes=1),
    )
    db_session.add(old)
    await db_session.flush()
    job, reason = await _submit(db_session, user.id, model.id, "hello")
    assert job is not None
    assert reason == "QUEUED"


def test_daily_minutes_error_surfaces_name_the_tier_limit():
    """Mini App detail + bot message name the CALLER's tier limit; status 429."""
    assert _voice_job_error_status("DAILY_MINUTE_LIMIT_EXCEEDED") == 429

    free_user = SimpleNamespace(account_category=AccountCategory.FREE.value)
    paid_user = SimpleNamespace(account_category=AccountCategory.PURCHASED.value)
    assert "(5 မိနစ်)" in _voice_job_error_detail("DAILY_MINUTE_LIMIT_EXCEEDED", free_user)
    assert "(30 မိနစ်)" in _voice_job_error_detail("DAILY_MINUTE_LIMIT_EXCEEDED", paid_user)
    # No user context: the FREE tier is quoted.
    assert "(5 မိနစ်)" in _voice_job_error_detail("DAILY_MINUTE_LIMIT_EXCEEDED")

    assert "5 မိနစ်" in voice_queue_failure_message(
        "DAILY_MINUTE_LIMIT_EXCEEDED", "my", account_category=AccountCategory.FREE.value)
    assert "30 မိနစ်" in voice_queue_failure_message(
        "DAILY_MINUTE_LIMIT_EXCEEDED", "my", account_category=AccountCategory.PURCHASED.value)
    assert "5 minutes" in voice_queue_failure_message(
        "DAILY_MINUTE_LIMIT_EXCEEDED", "en", account_category=AccountCategory.FREE.value)
