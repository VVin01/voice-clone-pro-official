"""Hardening batch — Family 3 (platform & lifecycle).

B2  payment intent survives restart: the package selection is
    persisted at tap time; a slip photo arriving with no live FSM
    state but a recent intent resumes the flow end-to-end.
F8  worker upload endpoint: no WORKER_TOKEN → 404 (never the bot
    token); token compared with compare_digest; the co-located
    worker's sync is a no-op without a worker token.
F10 recap dispatch claim: two competing claims on one QUEUED recap
    job → exactly one wins; a startup-style reset makes it claimable
    again.
F11 raw exception text no longer leaves the server in API details.
D2  admin overview carries disk usage + a low-space flag.
D3  retention sweep also removes orphaned temp artefacts — never
    young files, never files a surviving job row references.
L1  slip image file is deleted when its request is approved/rejected.
L3  payment_* setting values are masked in logs.
A2  request models bound string lengths to their columns; Telegram
    profile fields are truncated at the get_or_create_user choke point.
"""
from __future__ import annotations

import asyncio
import io
import os
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message
from aiogram.types import User as TgUser
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.handlers.payment as payment_mod
import app.webapp.api as api_mod
from app.config import settings
from app.db.base import Base
from app.handlers.payment import handle_package_selected, handle_receipt_after_restart
from app.models.enums import JobStatus, JobType
from app.models.schema import Job, PaymentIntent, PaymentRequest, User, utc_now
from app.services.dynamic_settings import _loggable_value
from app.services.ledger import get_or_create_user
from app.services.payments import (
    approve_payment_request,
    clear_payment_intent,
    get_active_pricing_plans,
    get_recent_payment_intent,
    reject_payment_request,
    save_payment_intent,
)


def _patch_handler_sessions(monkeypatch, db_session):
    @asynccontextmanager
    async def mock_session_ctx():
        yield db_session

    monkeypatch.setattr(payment_mod, "AsyncSessionLocal", lambda: mock_session_ctx())


def _fake_photo_message(uid=7801, file_id="fid-slip-1"):
    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=uid, is_bot=False, first_name="Buyer", username="buyer")
    msg.photo = [SimpleNamespace(file_id=file_id)]
    msg.document = None
    msg.answer = AsyncMock()
    msg.bot = SimpleNamespace(
        send_photo=AsyncMock(), send_document=AsyncMock(), send_message=AsyncMock()
    )
    return msg


# ── B2: intent persistence + restart resume ───────────────────────────

@pytest.mark.asyncio
async def test_b2_intent_save_get_clear(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=7800)
    await save_payment_intent(db_session, user.id, "MIN_30")
    await db_session.commit()
    intent = await get_recent_payment_intent(db_session, user.id)
    assert intent is not None and intent.package_code == "MIN_30"
    await clear_payment_intent(db_session, user.id)
    await db_session.commit()
    assert await get_recent_payment_intent(db_session, user.id) is None


@pytest.mark.asyncio
async def test_b2_stale_intent_is_ignored(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=7805)
    await save_payment_intent(db_session, user.id, "MIN_10")
    row = await db_session.get(PaymentIntent, user.id)
    row.chosen_at = utc_now() - timedelta(hours=25)
    await db_session.commit()
    assert await get_recent_payment_intent(db_session, user.id) is None


@pytest.mark.asyncio
async def test_b2_package_selection_persists_intent(db_session, monkeypatch):
    await get_active_pricing_plans(db_session)
    user, _ = await get_or_create_user(db_session, telegram_id=7802)
    await db_session.commit()
    _patch_handler_sessions(monkeypatch, db_session)

    cb = MagicMock()
    cb.from_user = TgUser(id=7802, is_bot=False, first_name="Buyer")
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    cb.data = "buy_pkg:MIN_10"
    state = SimpleNamespace(
        set_state=AsyncMock(), update_data=AsyncMock(), get_data=AsyncMock(return_value={})
    )
    await handle_package_selected(cb, state=state)

    intent = await db_session.get(PaymentIntent, 7802)
    assert intent is not None and intent.package_code == "MIN_10"


@pytest.mark.asyncio
async def test_b2_photo_after_restart_resumes_slip_flow(db_session, monkeypatch):
    """The full restart scenario: intent persisted, FSM gone (fresh
    process), photo arrives → payment request created from the
    persisted plan, user confirmed, intent cleared."""
    await get_active_pricing_plans(db_session)
    user, _ = await get_or_create_user(db_session, telegram_id=7801)
    await save_payment_intent(db_session, user.id, "MIN_30")
    await db_session.commit()
    _patch_handler_sessions(monkeypatch, db_session)
    monkeypatch.setattr(settings, "admin_ids_raw", "")

    msg = _fake_photo_message(uid=7801)
    await handle_receipt_after_restart(msg)

    reqs = (await db_session.execute(select(PaymentRequest))).scalars().all()
    assert len(reqs) == 1
    assert reqs[0].package_code == "MIN_30"
    assert reqs[0].minutes == 30
    assert reqs[0].receipt_file_id == "fid-slip-1"
    msg.answer.assert_awaited_once()
    assert await db_session.get(PaymentIntent, 7801) is None


@pytest.mark.asyncio
async def test_b2_photo_without_intent_stays_silent(db_session, monkeypatch):
    user, _ = await get_or_create_user(db_session, telegram_id=7803)
    await db_session.commit()
    _patch_handler_sessions(monkeypatch, db_session)

    msg = _fake_photo_message(uid=7803)
    await handle_receipt_after_restart(msg)

    reqs = (await db_session.execute(select(PaymentRequest))).scalars().all()
    assert reqs == []
    msg.answer.assert_not_called()


# ── F8: worker upload endpoint ────────────────────────────────────────

class _FakeWorkerFile:
    def __init__(self, content=b"RIFF-fake-wav"):
        self.filename = "result.wav"
        self.file = io.BytesIO(content)


@pytest.mark.asyncio
async def test_f8_endpoint_disabled_without_worker_token(db_session, monkeypatch):
    monkeypatch.setattr(settings, "worker_token", "")
    monkeypatch.setattr(settings, "bot_token", "bot-token-value")
    # Even presenting the BOT token must not open the endpoint.
    with pytest.raises(HTTPException) as exc:
        await api_mod.upload_worker_job_result(
            job_id="job1", file=_FakeWorkerFile(), auth_token="bot-token-value", db=db_session
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_f8_endpoint_rejects_wrong_and_bot_tokens(db_session, monkeypatch):
    monkeypatch.setattr(settings, "worker_token", "wk-secret")
    monkeypatch.setattr(settings, "bot_token", "bot-token-value")
    for bad in ("bot-token-value", "wk-secre", "", None):
        with pytest.raises(HTTPException) as exc:
            await api_mod.upload_worker_job_result(
                job_id="job1", file=_FakeWorkerFile(), auth_token=bad, db=db_session
            )
        assert exc.value.status_code == 403, bad


@pytest.mark.asyncio
async def test_f8_endpoint_accepts_worker_token(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "worker_token", "wk-secret")
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    user, _ = await get_or_create_user(db_session, telegram_id=7810)
    job = Job(id="job-f8", user_id=user.id, status=JobStatus.RUNNING.value)
    db_session.add(job)
    await db_session.commit()

    res = await api_mod.upload_worker_job_result(
        job_id="job-f8", file=_FakeWorkerFile(), auth_token="wk-secret", db=db_session
    )
    assert res["status"] == "success"
    refreshed = await db_session.get(Job, "job-f8")
    await db_session.refresh(refreshed)
    assert refreshed.status == JobStatus.SUCCESS.value
    assert refreshed.result_path and os.path.isfile(refreshed.result_path)


@pytest.mark.asyncio
async def test_f8_sync_noops_without_worker_token(monkeypatch, tmp_path):
    from app import worker as worker_mod

    monkeypatch.setattr(settings, "worker_token", "")
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")

    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("httpx must not be constructed in co-located mode")

    monkeypatch.setattr("httpx.AsyncClient", _Boom)
    # Returns quietly, having attempted no upload with the bot token.
    await worker_mod.sync_output_to_cloud("job-x", audio)


# ── F10: recap claim ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_f10_two_competing_claims_exactly_one_wins(db_session):
    from app.services.queue import claim_job_for_running

    user, _ = await get_or_create_user(db_session, telegram_id=7820)
    db_session.add(Job(
        id="rc_claim1", user_id=user.id, status=JobStatus.QUEUED.value,
        job_type=JobType.RECAP.value, engine="recap",
    ))
    await db_session.commit()

    assert await claim_job_for_running(db_session, "rc_claim1") is True
    assert await claim_job_for_running(db_session, "rc_claim1") is False
    job = await db_session.get(Job, "rc_claim1")
    await db_session.refresh(job)
    assert job.status == JobStatus.RUNNING.value

    # Startup-style reset (RUNNING → QUEUED) makes it claimable again.
    job.status = JobStatus.QUEUED.value
    await db_session.commit()
    assert await claim_job_for_running(db_session, "rc_claim1") is True


# ── D2: disk guard in the admin overview ──────────────────────────────

@pytest.mark.asyncio
async def test_d2_overview_carries_disk_block(db_session):
    admin = User(id=999222, username="adm", first_name="Admin", language_code="my")
    db_session.add(admin)
    await db_session.commit()
    res = await api_mod.get_admin_overview(admin, db_session)
    assert res["disk"] is not None
    assert res["disk"]["total_bytes"] > 0
    assert res["disk"]["free_bytes"] >= 0
    assert isinstance(res["disk"]["low"], bool)


# ── D3: orphan sweep ──────────────────────────────────────────────────

class _SweepStore:
    def __init__(self, db_file: str):
        self._db_file = db_file

    def make_session(self):
        engine = create_async_engine(f"sqlite+aiosqlite:///{self._db_file}", future=True)
        maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        return maker()


@pytest.mark.asyncio
async def test_d3_sweep_removes_only_true_orphans(monkeypatch, tmp_path):
    db_file = str(tmp_path / "sweep.db")
    sync_engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    store = _SweepStore(db_file)
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", store.make_session)
    monkeypatch.setattr(settings, "storage_root", tmp_path)

    uploads = tmp_path / "temp" / "recap_uploads"
    uploads.mkdir(parents=True)
    old = time.time() - 10 * 86400

    def _mk(path, mtime):
        path.write_bytes(b"x")
        os.utime(path, (mtime, mtime))

    orphan_old = uploads / "up_orphan.mp4"
    _mk(orphan_old, old)
    young = uploads / "up_young.mp4"
    _mk(young, time.time())
    live = uploads / "up_live.mp4"
    _mk(live, old)
    live_thumb = uploads / "up_live_thumb.jpg"
    _mk(live_thumb, old)
    frame_old = tmp_path / "temp" / "frame_deadbeef.jpg"
    _mk(frame_old, old)
    frame_new = tmp_path / "temp" / "frame_fresh.jpg"
    _mk(frame_new, time.time())

    async with store.make_session() as s:
        s.add(User(id=7830))
        s.add(Job(
            id="rc_live", user_id=7830, status=JobStatus.RUNNING.value,
            job_type=JobType.RECAP.value, engine="recap",
            source_audio_path=str(live),
        ))
        await s.commit()

    await api_mod.cleanup_expired_jobs()

    assert not orphan_old.exists()          # old + unreferenced → gone
    assert young.exists()                    # younger than window → kept
    assert live.exists()                     # referenced by a live job → kept
    assert live_thumb.exists()               # thumb of a referenced upload → kept
    assert not frame_old.exists()            # stale frame cache → gone
    assert frame_new.exists()                # fresh frame cache → kept


# ── L1: slip file deleted at terminal transitions ─────────────────────

@pytest.mark.asyncio
async def test_l1_slip_file_deleted_on_approve_and_reject(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    user, _ = await get_or_create_user(db_session, telegram_id=7840)
    await db_session.commit()

    async def _mk_req(rid, fname):
        f = receipts / fname
        f.write_bytes(b"slip")
        req = PaymentRequest(
            id=rid, user_id=user.id, package_code="MIN_10", minutes=10,
            amount_mmk=8000, slip_url=f"/api/receipts/{fname}", status="PENDING",
        )
        db_session.add(req)
        await db_session.commit()
        return f

    f1 = await _mk_req("PAY-L1-A", "slip_a.png")
    ok, _, _ = await approve_payment_request(db_session, "PAY-L1-A", admin_id=1)
    await db_session.commit()
    assert ok is True
    assert not f1.exists()  # image gone; the DB row remains (checked below)

    f2 = await _mk_req("PAY-L1-B", "slip_b.png")
    ok, _, _ = await reject_payment_request(db_session, "PAY-L1-B", admin_id=1, reason="blurry")
    await db_session.commit()
    assert ok is True
    assert not f2.exists()

    rows = (await db_session.execute(select(PaymentRequest))).scalars().all()
    assert {r.status for r in rows} == {"APPROVED", "REJECTED"}


# ── L3: log masking ───────────────────────────────────────────────────

def test_l3_payment_values_masked_in_logs():
    masked = _loggable_value("payment_kpay_number", "09123456789")
    assert "09123456789" not in masked
    assert "123456" not in masked
    assert _loggable_value("tutorial_voice_link", "https://t.me/x/29") == "https://t.me/x/29"
    assert _loggable_value("maintenance_mode", "true") == "true"


# ── A2: request bounds + intake truncation ────────────────────────────

def test_a2_request_models_bound_lengths():
    with pytest.raises(ValidationError):
        api_mod.VoiceGenerateRequest(text="hello", preset_voice="x" * 51)
    with pytest.raises(ValidationError):
        api_mod.RecapConfigRequest(url="https://x/" + "a" * 2048)
    with pytest.raises(ValidationError):
        api_mod.RecapConfigRequest(url="https://youtu.be/abc", voice_slug="v" * 51)
    ok = api_mod.VoiceGenerateRequest(text="hello", preset_voice="komin")
    assert ok.preset_voice == "komin"


@pytest.mark.asyncio
async def test_a2_profile_fields_truncated_at_intake(db_session):
    user, _ = await get_or_create_user(
        db_session, telegram_id=7850, username="u" * 200, first_name="n" * 300
    )
    await db_session.commit()
    assert len(user.username) == 100
    assert len(user.first_name) == 150
    # …and on the profile-refresh path too.
    user2, _ = await get_or_create_user(
        db_session, telegram_id=7850, username="v" * 200, first_name="m" * 300
    )
    await db_session.commit()
    assert len(user2.username) == 100
    assert len(user2.first_name) == 150


# ── F11: no raw exception text in API details (source pins) ──────────

def test_f11_raw_detail_patterns_are_gone():
    src = (api_mod.__file__)
    text = open(src, encoding="utf-8").read()
    assert 'detail=str(e)[:300]' not in text
    assert 'အမှားဖြစ်ပေါ်ပါသည်: {e}' not in text
    # The script-edit endpoint logs the ValueError instead of returning it.
    assert "Script edit rejected for job" in text
