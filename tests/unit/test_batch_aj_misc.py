"""Batch AJ — weakness-hunt fixes: handlers / services / misc cluster.

Findings covered: H9 (persona reference WAVs out of the public
static mount), M2 (deliver_broadcast RUNNING guard actually
guards), M4 (edited approval can correct recap credits end-to-end),
M7 (send-recap callback fails closed without a Job row),
M10c (recap deadline armed from submit time), H12 (after-restart
slip resume is idempotent under redelivery), L14 (dead settings),
L19 (extractor URL revalidation), L21 (ffmpeg protocol whitelist
at the remaining call sites).
"""
from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, Message
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db import session as session_mod
from app.db.base import Base
from app.models.enums import JobStatus, JobType, PaymentStatus
from app.models.schema import BroadcastCampaign, BroadcastDelivery, Job, PaymentRequest, User
from app.services.ledger import get_or_create_user


# ── H9: persona references are not public static files ───────────────


def test_batch_aj_h9_persona_reference_resolves_outside_static():
    """Resolution prefers the non-static app/assets location (the
    target of the H9 move); the legacy static directory is only an
    on-disk fallback for the WAV binaries, which cannot move
    through the text-only snapshot-push connector yet."""
    import inspect

    from app.services import curated_voices
    from app.services.curated_voices import get_persona_reference

    src = inspect.getsource(curated_voices.get_persona_reference)
    assets_idx = src.find('"assets"')
    static_idx = src.find('"static"')
    assert 0 < assets_idx < static_idx, "assets location must be consulted first"

    path, transcript = get_persona_reference("kothura")
    assert path, "persona reference must still resolve for cloning"
    assert transcript
    assert Path(path).is_file()


@pytest.mark.asyncio
async def test_batch_aj_h9_static_references_url_is_404():
    from httpx import ASGITransport, AsyncClient

    from app.webapp.server import webapp_app

    transport = ASGITransport(app=webapp_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/static/audio/references/kothura.wav")
    assert resp.status_code == 404


def test_batch_aj_h9_sample_previews_still_resolve():
    """The Mini App persona picker previews (short sample MP3s via
    the API sample endpoint) are unaffected by the move."""
    from app.services.curated_voices import list_curated_voices

    voices = list_curated_voices("my")
    assert voices, "curated voice list must not be empty"
    samples_dir = Path(__file__).resolve()
    # The sample files the /api/voices/sample/{slug} endpoint serves
    # still exist under static/audio/samples.
    from app.services import curated_voices

    static_samples = (
        Path(curated_voices.__file__).resolve().parent.parent
        / "webapp" / "static" / "audio" / "samples"
    )
    assert static_samples.is_dir()
    assert any(static_samples.iterdir())


# ── M2: deliver_broadcast's RUNNING guard actually guards ────────────


@pytest.mark.asyncio
async def test_batch_aj_m2_running_campaign_refuses_duplicate_delivery(db_session):
    from app.services.broadcast import deliver_broadcast

    camp = BroadcastCampaign(
        id="bc_aj_m2", admin_id=999, confirmation_token="tok",
        message_text="hello", status="RUNNING", total_recipients=2,
        sent_count=0, failed_count=0, skipped_count=0,
    )
    db_session.add(camp)
    await db_session.flush()
    db_session.add(BroadcastDelivery(campaign_id="bc_aj_m2", user_id=12345678, status="PENDING"))
    db_session.add(BroadcastDelivery(campaign_id="bc_aj_m2", user_id=23456789, status="PENDING"))
    await db_session.commit()

    bot = SimpleNamespace(send_message=AsyncMock())
    result = await deliver_broadcast(
        bot=bot, recipient_ids=[12345678, 23456789], text="hello",
        session=db_session, campaign_id="bc_aj_m2", delay_between_sends=0,
    )
    # The duplicate attempt sent NOTHING and reported the live state.
    bot.send_message.assert_not_awaited()
    assert result.status == "RUNNING"


# ── M4: edited approval corrects recap credits through the FSM ───────


class _FakeFSM:
    def __init__(self):
        self.current_state = None
        self.data = {}

    async def set_state(self, state):
        self.current_state = state

    async def get_state(self):
        return self.current_state

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def clear(self):
        self.current_state = None
        self.data = {}


def _admin_message(admin_id: int, text: str):
    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    msg.text = text
    msg.answer = AsyncMock()
    msg.bot = AsyncMock()
    return msg


def _admin_callback(admin_id: int, data: str):
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb.data = data
    cb.bot = AsyncMock()
    cb.bot.send_message = AsyncMock()
    cb.bot.edit_message_text = AsyncMock()
    cb.bot.edit_message_caption = AsyncMock()
    msg = MagicMock(spec=Message)
    msg.chat = SimpleNamespace(id=admin_id)
    msg.message_id = 1
    msg.photo = None
    msg.document = None
    msg.video = None
    msg.animation = None
    msg.audio = None
    msg.voice = None
    msg.caption = None
    msg.edit_text = AsyncMock()
    msg.edit_caption = AsyncMock()
    msg.answer = AsyncMock()
    cb.message = msg
    cb.answer = AsyncMock()
    return cb


@pytest.mark.asyncio
async def test_batch_aj_m4_edit_flow_corrects_recap_credits(db_session, monkeypatch):
    """recap_starter carries 5 recap credits. The edit flow used to
    let an admin correct ONLY the minutes — the credits always
    granted at catalogue value. Now the FSM asks for corrected
    credits and exactly those are granted."""
    from app.handlers.admin import (
        PaymentReviewFSM,
        handle_admin_pay_edit_confirm,
        handle_admin_pay_edit_minutes_input,
        handle_admin_pay_edit_recap_credits_input,
        handle_admin_pay_edit_start,
    )

    monkeypatch.setattr(settings, "admin_ids_raw", "9101")
    buyer, _ = await get_or_create_user(db_session, telegram_id=7001)
    buyer.recap_credits = 0
    payment = PaymentRequest(
        id="PAY-AJ-M4", user_id=buyer.id, package_code="recap_starter",
        minutes=0, amount_mmk=20000, receipt_file_id="slip-m4",
        status=PaymentStatus.PENDING.value,
    )
    db_session.add(payment)
    await db_session.commit()

    fsm = _FakeFSM()
    cb = _admin_callback(9101, f"admin:pay_edit:{payment.id}")
    await handle_admin_pay_edit_start(cb, fsm, session=db_session)
    assert fsm.current_state == PaymentReviewFSM.waiting_for_corrected_minutes

    msg = _admin_message(9101, "0")
    # minutes for recap_starter are 0 in the catalogue but the edit
    # flow requires >= 1; give a nominal corrected minute value.
    msg.text = "5"
    await handle_admin_pay_edit_minutes_input(msg, fsm, session=db_session)
    # Recap-bearing package → the NEW second prompt, not confirm.
    assert fsm.current_state == PaymentReviewFSM.waiting_for_corrected_recap_credits

    msg2 = _admin_message(9101, "3")
    await handle_admin_pay_edit_recap_credits_input(msg2, fsm, session=db_session)
    assert fsm.current_state == PaymentReviewFSM.confirming_edit
    assert fsm.data["corrected_recap_credits"] == 3

    cb2 = _admin_callback(9101, f"admin:pay_edit_confirm:{payment.id}")
    await handle_admin_pay_edit_confirm(cb2, fsm, session=db_session)

    await db_session.refresh(payment)
    assert payment.status == PaymentStatus.APPROVED.value
    assert payment.edited_recap_credits == 3
    await db_session.refresh(buyer)
    assert int(buyer.recap_credits) == 3  # corrected 3, NOT catalogue 5


# ── M7: send-recap callback fails closed without a Job row ───────────


@pytest.mark.asyncio
async def test_batch_aj_m7_recap_callback_refuses_strangers_job(tmp_path, monkeypatch):
    from app.handlers import clone as clone_mod
    from app.webapp import api as api_mod

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/m7.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(clone_mod, "AsyncSessionLocal", maker)
    monkeypatch.setattr(settings, "storage_root", tmp_path)

    video = tmp_path / "final.mp4"
    video.write_bytes(b"v" * 4096)
    api_mod.RECAP_JOBS_REGISTRY["rc_aj_m7"] = {
        "user_id": 111111, "status": "COMPLETED",
        "video_path": str(video), "title": "T",
    }

    def _cb(caller_id: int):
        cb = MagicMock(spec=CallbackQuery)
        cb.from_user = TgUser(id=caller_id, is_bot=False, first_name="U")
        cb.data = "send_recap_rc_aj_m7"
        cb.bot = AsyncMock()
        cb.bot.send_message = AsyncMock()
        cb.bot.send_video = AsyncMock()
        cb.bot.send_document = AsyncMock()
        cb.answer = AsyncMock()
        return cb

    try:
        # Stranger (no Job row; registry proves someone else owns it).
        stranger = _cb(222222)
        await clone_mod.handle_send_recap_callback(stranger)
        stranger.bot.send_video.assert_not_awaited()
        sent_texts = [
            (c.kwargs.get("text") or "") for c in stranger.bot.send_message.call_args_list
        ]
        from app.services.i18n import t as _t
        assert _t("clone_audio_not_found", "my") in sent_texts

        # The owner still gets the video.
        owner = _cb(111111)
        await clone_mod.handle_send_recap_callback(owner)
        owner.bot.send_video.assert_awaited()
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop("rc_aj_m7", None)
        await engine.dispose()


# ── M10c: recap deadline armed from the Job row's submit time ────────


def test_batch_aj_m10c_deadline_helper():
    from app.webapp import api as api_mod

    now = datetime.now(timezone.utc)
    assert api_mod._recap_deadline_exceeded(now - timedelta(hours=2), now_ts=now.timestamp()) is True
    assert api_mod._recap_deadline_exceeded(now - timedelta(minutes=5), now_ts=now.timestamp()) is False
    assert api_mod._recap_deadline_exceeded(None) is False


def test_batch_aj_m10c_runner_checks_deadline_before_download():
    from app.webapp import api as api_mod

    src = inspect.getsource(api_mod._execute_recap_background_job)
    assert "_recap_job_deadline_exceeded" in src


# ── H12: after-restart slip resume is idempotent under redelivery ────


@pytest.mark.asyncio
async def test_batch_aj_h12_resume_redelivery_creates_one_request(tmp_path, monkeypatch):
    """With drop_pending_updates=False, Telegram may redeliver the
    slip update after a restart. The resume path must still produce
    exactly ONE payment request: the first resume consumes the
    persisted intent, the redelivery finds no intent and stays
    silent; even with an intent, the service-level fingerprint guard
    returns the existing request instead of minting a second."""
    from app.handlers import payment as payment_mod
    from app.services.payments import save_payment_intent

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/h12.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(payment_mod, "AsyncSessionLocal", maker)
    monkeypatch.setattr(settings, "bot_token", "")

    async with maker() as s:
        user, _ = await get_or_create_user(s, telegram_id=777001)
        await save_payment_intent(s, user.id, "MIN_10")
        await s.commit()

    def _slip_message():
        msg = MagicMock(spec=Message)
        msg.from_user = TgUser(id=777001, is_bot=False, first_name="Buyer")
        msg.photo = [SimpleNamespace(file_id="slip-h12-file")]
        msg.document = None
        msg.answer = AsyncMock()
        msg.bot = AsyncMock()
        msg.bot.send_photo = AsyncMock()
        msg.bot.send_message = AsyncMock()
        return msg

    await payment_mod.handle_receipt_after_restart(_slip_message())
    await payment_mod.handle_receipt_after_restart(_slip_message())  # redelivery

    async with maker() as s:
        rows = (await s.execute(
            select(PaymentRequest).where(PaymentRequest.user_id == 777001)
        )).scalars().all()
    assert len(rows) == 1
    await engine.dispose()


# ── L14: dead settings ───────────────────────────────────────────────


def test_batch_aj_l14_legacy_database_url_removed():
    assert not hasattr(settings, "legacy_database_url")


@pytest.mark.asyncio
async def test_batch_aj_l14_purchased_daily_limit_enforced(db_session, monkeypatch):
    from app.models.enums import AccountCategory
    from app.models.schema import VoiceModel
    from app.services.queue import create_voice_clone_job

    monkeypatch.setattr(settings, "purchased_daily_job_limit", 1)
    user, _ = await get_or_create_user(db_session, telegram_id=9401)
    user.account_category = AccountCategory.PURCHASED.value
    model = VoiceModel(
        name="Vox", slug="voxcpm-cloud-ai", type="SYSTEM", status="ACTIVE",
        allowed_for_free=True, allowed_for_purchased=True, pth_path="voxcpm",
        model_dir="cloud",
    )
    db_session.add(model)
    db_session.add(Job(
        id="v_aj_l14", user_id=user.id, status=JobStatus.SUCCESS.value,
        job_type=JobType.VOICE_CLONE.value, engine="voxcpm",
        created_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()

    job, reason = await create_voice_clone_job(
        db_session, user.id, text="hello", model_id=model.id,
        gender_preset="m2m", speed_preset="normal",
        edge_voice="my-MM-ThihaNeural", engine="voxcpm",
    )
    assert job is None
    assert reason == "DAILY_JOB_LIMIT_EXCEEDED"


# ── L19: tikwm play URL passes the SSRF validator ────────────────────


def test_batch_aj_l19_tikwm_resolve_revalidates_play_url():
    from app.services import downloader

    src = inspect.getsource(downloader.MediaDownloader._tikwm_resolve)
    assert "validate_public_url" in src


# ── L21: protocol whitelist at the remaining call sites ──────────────


def test_batch_aj_l21_protocol_whitelist_present():
    import app.services.curated_voices as cv
    import app.services.recap_engine.services.whisper_service as ws
    import app.services.transcriber as tr
    import app.services.voxcpm_engine as vx

    assert inspect.getsource(cv).count("protocol_whitelist") >= 3
    assert inspect.getsource(ws).count("protocol_whitelist") >= 2
    assert inspect.getsource(tr).count("protocol_whitelist") >= 1
    assert inspect.getsource(vx).count("protocol_whitelist") >= 1
