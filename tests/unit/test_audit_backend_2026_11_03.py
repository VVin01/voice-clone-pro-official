"""Audit fix batch A — backend reliability (2026-11-03).

Covers:
1. DB engine pool params (production QueuePool exhaustion).
2. get_current_user commits only when the Telegram profile changed.
3. get_recap_status no longer depends on a second DB session for its
   registry hot path.
5. Voice balance pre-check: measured rate + speed-preset factor +
   safety margin (production 197s / 209s vs 180s regressions).
6a. Worker failure mapping: raw exceptions never reach
    job.error_message / the user's Telegram card.
6b. Recap failure default-deny + new classifier needles (429,
    "Unable to extract").
9. Downloader cache dirs join the retention sweep.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event

import app.db.session as session_mod
import app.services.downloader as downloader_mod
import app.services.queue as queue_mod
import app.webapp.api as api_mod
from app.config import settings
from app.db.base import Base
from app.models.enums import AccountCategory, JobStatus, JobType
from app.models.schema import Job
from app.services.downloader import classify_source_error
from app.services.i18n import t
from app.services.ledger import get_minute_balance, get_or_create_user
from app.services.queue import create_voice_clone_job, estimate_voice_job_seconds
from app.worker import _user_facing_worker_error, process_single_job


# ── 1. DB pool params ────────────────────────────────────────────────

def test_engine_pool_params_present():
    from app.db.session import engine

    assert settings.db_pool_size == 10
    assert settings.db_max_overflow == 20
    assert settings.db_pool_timeout == 30
    assert settings.db_pool_recycle == 1800

    pool = engine.pool
    assert pool.size() == settings.db_pool_size
    assert pool._max_overflow == settings.db_max_overflow
    assert pool._timeout == settings.db_pool_timeout
    assert pool._recycle == settings.db_pool_recycle
    assert pool._pre_ping is True


# ── 2. get_current_user write-on-change only ─────────────────────────

_TOKEN = "unittest-token"


def _signed_init_data(fields, token=None):
    token = token or _TOKEN
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode({**fields, "hash": digest})


def _init_data(uid=940001, first_name="Audit", username="audituser"):
    user = {"id": uid, "first_name": first_name}
    if username is not None:
        user["username"] = username
    return _signed_init_data({
        "user": json.dumps(user),
        "query_id": "AAE",
        "auth_date": str(int(time.time())),
    })


@pytest.mark.asyncio
async def test_get_current_user_commits_only_on_change(db_session, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", _TOKEN)
    user, _ = await get_or_create_user(db_session, telegram_id=940001)
    user.first_name = "Audit"
    user.username = "audituser"
    await db_session.commit()

    commits: list[int] = []
    event.listen(
        db_session.sync_session, "after_commit",
        lambda s: commits.append(1),
    )

    # Same profile as stored: no write, no commit.
    out = await api_mod.get_current_user(_init_data(), db_session)
    assert out.id == user.id
    assert commits == []

    # Changed first_name: exactly one commit, value persisted.
    out = await api_mod.get_current_user(_init_data(first_name="Renamed"), db_session)
    assert out.first_name == "Renamed"
    assert len(commits) == 1


# ── 3. get_recap_status deferred second session ──────────────────────

def test_recap_status_db_param_uses_deferred_dependency():
    param = inspect.signature(api_mod.get_recap_status).parameters["db"]
    assert param.default.dependency is api_mod._deferred_db_session


@pytest.mark.asyncio
async def test_recap_status_registry_branch_needs_no_db(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=940002)
    jid = "rc_audit_defer"
    api_mod.RECAP_JOBS_REGISTRY[jid] = {
        "user_id": user.id,
        "status": "SUCCESS",
        "progress_percent": 100,
        "download_urls": {},
    }
    try:
        res = await api_mod.get_recap_status(jid, user=user, db=None)
        assert res["status"] == "SUCCESS"
        assert res["id"] == jid
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop(jid, None)


# ── 5. Voice balance pre-check ───────────────────────────────────────

def test_estimate_rejects_production_197s_case():
    # Production failure: ~197s of audio charged against a 180s
    # balance. At the measured 17.6 chars/s that is ~3467 chars; the
    # old flat /20 estimate said 173.4s and let it through.
    text = "က" * 3467
    assert len(text) / 20.0 <= 180  # old estimate fit the balance
    assert estimate_voice_job_seconds(text, "normal") > 180


def test_estimate_rejects_production_209s_case():
    # Production failure: ~209s against 180s on a slow preset. At the
    # engine's slow tempo (17.6 * 0.94) that is ~3450 chars; the old
    # estimate ignored the preset and said 172.5s.
    text = "က" * 3450
    assert len(text) / 20.0 <= 180  # old estimate fit the balance
    assert estimate_voice_job_seconds(text, "slow") > 180


def test_estimate_sane_job_still_fits():
    # 1000 chars at normal speed ≈ 65s with margin — must NOT be
    # rejected against a 180s balance.
    assert estimate_voice_job_seconds("က" * 1000, "normal") < 180
    # Fast preset shortens the estimate; slow lengthens it.
    assert estimate_voice_job_seconds("က" * 2000, "fast") < estimate_voice_job_seconds("က" * 2000, "slow")


@pytest.mark.asyncio
async def test_precheck_rejects_both_production_cases_at_submit(db_session, monkeypatch):
    user, _ = await get_or_create_user(db_session, telegram_id=940003)
    user.account_category = AccountCategory.PURCHASED.value
    # The cloud model row init_db seeds in production (the queue's
    # model resolution + logging dereference it).
    from app.models.schema import VoiceModel

    db_session.add(VoiceModel(
        name="VoxCPM (Cloud AI)", slug="voxcpm-cloud-ai", type="SYSTEM",
        status="ACTIVE", pth_path="voxcpm", model_dir="cloud",
        allowed_for_free=True, allowed_for_purchased=True, is_default_free=True,
    ))
    await db_session.commit()

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(queue_mod, "grant_monthly_free_allowance", _noop)
    bal = await get_minute_balance(db_session, user.id)
    bal.purchased_seconds = 180
    await db_session.flush()

    job, reason = await create_voice_clone_job(
        db_session, user.id, text="က" * 3467, speed_preset="normal"
    )
    assert job is None and reason == "INSUFFICIENT_BALANCE"

    job, reason = await create_voice_clone_job(
        db_session, user.id, text="က" * 3450, speed_preset="slow"
    )
    assert job is None and reason == "INSUFFICIENT_BALANCE"

    # A text that genuinely fits is still accepted.
    job, reason = await create_voice_clone_job(
        db_session, user.id, text="က" * 1000, speed_preset="normal"
    )
    assert job is not None and reason == "QUEUED"


# ── 6a. Worker failure mapping ───────────────────────────────────────

def test_worker_error_insufficient_balance_curated_with_numbers():
    raw = "Insufficient balance: requested 197s, available 180s"
    msg = _user_facing_worker_error(ValueError(raw), "my")
    assert msg != raw
    assert "Insufficient balance" not in msg
    assert "197" in msg and "180" in msg
    assert any("\u1000" <= ch <= "\u109f" for ch in msg)  # Burmese script

    msg209 = _user_facing_worker_error(
        ValueError("Insufficient balance: requested 209s, available 180s"), "en"
    )
    assert "209" in msg209 and "180" in msg209


def test_worker_error_classes():
    assert _user_facing_worker_error(
        RuntimeError("VoxCPM synthesis exploded internally"), "my"
    ) == t("worker_error_engine_failure", "my")
    assert _user_facing_worker_error(
        asyncio.TimeoutError(), "my"
    ) == t("worker_error_timeout", "my")
    assert _user_facing_worker_error(
        RuntimeError("totally unknown xyz failure"), "my"
    ) == t("worker_error_generic", "my")
    # Generic fallback must never echo the raw text.
    assert "xyz" not in _user_facing_worker_error(RuntimeError("totally unknown xyz failure"), "my")


@pytest.mark.asyncio
async def test_worker_failure_stores_curated_not_raw(db_session, tmp_path):
    """End to end: charge-time insufficient balance (the production
    197s case) must land on the job as the curated Burmese message."""
    user, _ = await get_or_create_user(db_session, telegram_id=940004)
    from app.services.ledger import grant_monthly_free_allowance

    await grant_monthly_free_allowance(db_session, user.id)
    bal = await get_minute_balance(db_session, user.id)
    assert bal.total_usable_seconds == 180

    from app.models.schema import VoiceModel

    pth_file = tmp_path / "M.pth"
    pth_file.write_bytes(b"pth")
    model = VoiceModel(
        name="AuditModel", slug="audit-model", type="SYSTEM",
        model_dir=str(tmp_path), pth_path=str(pth_file),
        status="ACTIVE", is_default_free=True,
    )
    db_session.add(model)
    await db_session.flush()

    job, reason = await create_voice_clone_job(
        session=db_session, user_id=user.id, text="မင်္ဂလာပါ", model_id=model.id
    )
    assert job is not None and reason == "QUEUED"

    def fake_voxcpm(text, output_wav, **kwargs):
        output_wav = Path(output_wav)
        output_wav.parent.mkdir(parents=True, exist_ok=True)
        output_wav.write_bytes(b"fake_wav")
        return output_wav

    with (
        patch("app.services.voxcpm_engine.VoxCPMEngine.synthesize", side_effect=fake_voxcpm),
        patch("app.worker.get_wav_duration_seconds", return_value=197),
    ):
        ok = await process_single_job(session=db_session, job=job)

    assert ok is False
    assert job.status == JobStatus.FAILED.value
    assert job.error_message != "Insufficient balance: requested 197s, available 180s"
    assert "Insufficient balance" not in (job.error_message or "")
    assert "197" in job.error_message and "180" in job.error_message


# ── 6b. Recap default-deny + classifier needles ──────────────────────

def test_classifier_429_needle():
    msg = classify_source_error(
        "ERROR: HTTP Error 429: Too Many Requests",
        "https://www.youtube.com/watch?v=abc",
    )
    assert msg is not None and msg.startswith("⚠️")


def test_classifier_unable_to_extract_needle():
    msg = classify_source_error(
        "Unable to extract video data",
        "https://www.tiktok.com/@someone/video/7234567890123456789",
    )
    assert msg is not None and msg.startswith("⚠️")


def test_classifier_still_none_for_non_source_errors():
    assert classify_source_error("ffmpeg exited with code 1", "https://x.example/v") is None
    assert classify_source_error(
        "ERROR: some brand-new failure mode",
        "https://www.tiktok.com/@someone/video/7234567890123456789",
    ) is None


class _ExplodingUnclassifiedDownloader:
    def __init__(self):
        raise RuntimeError(
            "ffmpeg exited with code 1 while reading /app/storage/recap_output/secret_internals.mp4"
        )


@pytest.mark.asyncio
async def test_recap_unclassified_error_default_deny(tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/deny.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", maker)
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "bot_token", "")
    monkeypatch.setattr(downloader_mod, "MediaDownloader", _ExplodingUnclassifiedDownloader)
    monkeypatch.setattr(api_mod, "RECAP_EXECUTION_SEMAPHORE", asyncio.Semaphore(2))

    jid = "rc_audit_deny"
    async with maker() as s:
        user, _ = await get_or_create_user(s, telegram_id=940005)
        uid = user.id
        s.add(Job(
            id=jid, user_id=uid, chat_id=uid,
            status=JobStatus.QUEUED.value,
            job_type=JobType.RECAP.value, engine="recap",
            preset_voice="komin", input_text="T",
            source_audio_path="https://www.youtube.com/shorts/dQw4w9WgXcQ",
        ))
        await s.commit()

    entry = {
        "id": jid, "user_id": uid, "title": "T", "status": "QUEUED",
        "progress_percent": 5, "progress_step": "0/4",
        "progress_label": "queued", "error_message": None,
        "credit_deducted": False, "credits_charged": 0,
        "credits_estimated": None, "parts": [],
    }
    api_mod.RECAP_JOBS_REGISTRY[jid] = entry
    req = api_mod.RecapConfigRequest(
        url="https://www.youtube.com/shorts/dQw4w9WgXcQ", title="T",
        voice_slug="komin",
    )
    try:
        await asyncio.wait_for(
            api_mod._run_recap_background_job(jid, req, uid, uid),
            timeout=15,
        )
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop(jid, None)

    stored = entry["error_message"] or ""
    assert entry["status"] == "FAILED"
    assert stored.startswith("⚠️")
    assert "နည်းပညာဆိုင်ရာ" in stored  # the generic technical message
    assert "ffmpeg" not in stored
    assert "/app/storage" not in stored
    assert "secret_internals" not in stored

    async with maker() as s:
        job = await s.get(Job, jid)
        assert job.error_message == stored
    await engine.dispose()


# ── 9. Downloader cache retention sweep ──────────────────────────────

def _make_download_dir(root: Path, dl_id: str, created_at: str | None) -> Path:
    d = root / "downloads" / dl_id
    d.mkdir(parents=True)
    (d / "video.mp4").write_bytes(b"x")
    if created_at is not None:
        (d / "meta.json").write_text(
            json.dumps({"download_id": dl_id, "user_id": 1, "created_at": created_at}),
            encoding="utf-8",
        )
    return d


def test_downloads_sweep_removes_old_keeps_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    now = datetime.now(timezone.utc)
    old = _make_download_dir(
        tmp_path, "aaaa1111bbbb", (now - timedelta(days=20)).isoformat()
    )
    fresh = _make_download_dir(
        tmp_path, "cccc2222dddd", (now - timedelta(days=1)).isoformat()
    )

    removed = api_mod._sweep_expired_download_dirs(now - timedelta(days=7))
    assert removed == 1
    assert not old.exists()
    assert fresh.exists()
