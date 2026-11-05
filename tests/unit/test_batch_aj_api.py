"""Batch AJ — adversarial weakness-hunt fixes: API cluster.

Findings covered: H1 (Mini App slip path), H3 (reference-audio
upload streaming), H4 (recap local-path ownership), H5 (recap runner
SUCCESS guard — source pin), H6 (admin queue retry re-charges),
M1 (disk headroom gate + slip orphan cleanup + receipts sweep),
M2 (Mini App broadcast records / honesty / plain text),
M3 (Mini App approve surfaces the claim conflict),
M5 (admin adjust idempotency + recap ledger + honest delta),
M6 (dynamic-settings bounds), L5 (streaming cleanup on cancel),
L6 (outputs/ orphan sweep), L11 (banned user + stream token),
L12 (Telegram delivery extras — the send-everything behaviour was
deliberately reversed by Batch AP's video-only order; see the
replacement test below), L13 (active-job count
must not silently degrade), L16 (notification log honesty — source
pin), L17 (broadcast delivery retention), L18 (registry caps).

Written against the UNFIXED tree first.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db import session as session_mod
from app.db.base import Base
from app.models.enums import JobStatus, JobType
from app.models.schema import (
    BroadcastCampaign,
    BroadcastDelivery,
    Job,
    PaymentRequest,
    RecapCreditLedgerEvent,
    User,
)
from app.services.ledger import get_or_create_user, record_recap_credit_event
from app.webapp import api as api_mod


# ── shared fakes ─────────────────────────────────────────────────────


class _FakeUpload:
    """UploadFile-shaped fake serving a virtual byte stream, tracking
    how many bytes the consumer actually pulled."""

    def __init__(self, total_bytes: int, filename: str = "ref.wav", chunk: int = 1 << 20):
        self.filename = filename
        self._remaining = total_bytes
        self._chunk = chunk
        self.served = 0

    async def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        n = self._remaining if size is None or size < 0 else min(size, self._chunk, self._remaining)
        self._remaining -= n
        self.served += n
        return b"x" * n


async def _open_store(tmp_path, monkeypatch, name="aj_api.db"):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/{name}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", maker)
    return engine, maker


# ── H3: reference-audio upload streams with a during-write cap ───────


@pytest.mark.asyncio
async def test_batch_aj_h3_reference_upload_never_fully_buffered(db_session, tmp_path, monkeypatch):
    """Unfixed hole: the endpoint did `await reference_audio.read()`
    (the WHOLE upload into RAM) and only then compared the length to
    50 MB — one 1 GB request could OOM the 2 GB instance before the
    check ever ran. Fixed: streamed to disk, aborted at the cap."""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    # /tmp in the sandbox is a small tmpfs; the disk-headroom gate is
    # not what this test exercises (it has its own test below).
    monkeypatch.setattr(api_mod, "DISK_HEADROOM_MIN_FREE_BYTES", 0)
    user, _ = await get_or_create_user(db_session, telegram_id=9301)
    await db_session.commit()

    fake = _FakeUpload(60 * 1024 * 1024, filename="ref.wav")
    with pytest.raises(HTTPException) as excinfo:
        await api_mod.create_voice_job_with_audio(
            text="hello world",
            model_slug="voxcpm-cloud-ai",
            speed="normal",
            gender_preset="m2m",
            engine="voxcpm",
            preset_voice=None,
            reference_audio=fake,
            user=user,
            db=db_session,
            _gate=user,
        )
    assert excinfo.value.status_code == 413
    # The consumer stopped at the cap instead of pulling all 60 MB.
    assert fake.served <= 52 * 1024 * 1024
    # And no partial file was left behind.
    refs = tmp_path / "temp" / "voxcpm_refs"
    leftovers = list(refs.iterdir()) if refs.is_dir() else []
    assert leftovers == []


# ── H4: recap local sources must belong to the submitter ─────────────


@pytest.mark.asyncio
async def test_batch_aj_h4_local_source_of_another_user_rejected(db_session, tmp_path, monkeypatch):
    """Unfixed hole: any path under storage_root was accepted as a
    recap source — user B could recap (and thereby read/process)
    user A's uploaded video by guessing its path."""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    uploads = tmp_path / "temp" / "recap_uploads"
    uploads.mkdir(parents=True)
    victim_file = uploads / "up_victim001.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=black:size=64x64:rate=10",
         "-t", "1", str(victim_file)],
        check=True, capture_output=True,
    )
    owner, _ = await get_or_create_user(db_session, telegram_id=9302)
    attacker, _ = await get_or_create_user(db_session, telegram_id=9303)
    attacker.recap_credits = 3
    await db_session.commit()
    api_mod.UPLOADED_VIDEO_OWNERS[victim_file.name] = owner.id
    try:
        req = api_mod.RecapConfigRequest(
            url=str(victim_file), title="Stolen", voice_slug="komin",
        )
        with patch.object(api_mod, "_run_recap_background_job", new=AsyncMock()):
            with pytest.raises(HTTPException) as excinfo:
                await api_mod.create_recap_job(req, user=attacker, db=db_session, _gate=attacker)
        assert excinfo.value.status_code == 400
        assert "not allowed" in str(excinfo.value.detail)
    finally:
        api_mod.UPLOADED_VIDEO_OWNERS.pop(victim_file.name, None)


# ── H1 + M1b: Mini App slip path ─────────────────────────────────────


def _slip_upload(data: bytes) -> UploadFile:
    return UploadFile(filename="slip.jpg", file=io.BytesIO(data))


@pytest.mark.asyncio
async def test_batch_aj_h1_miniapp_slip_replay_after_approval_blocked(db_session, tmp_path, monkeypatch):
    """The Mini App twin of the bot hole: the old guard compared the
    new slip's bytes only against the same user's PENDING rows' files
    — after approval the file is deleted and the row is APPROVED, so
    the identical image minted a fresh request."""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(api_mod, "DISK_HEADROOM_MIN_FREE_BYTES", 0)
    monkeypatch.setattr(settings, "bot_token", "")
    user, _ = await get_or_create_user(db_session, telegram_id=9304)
    await db_session.commit()

    slip_bytes = b"fake-slip-image-bytes-001"
    fingerprint = "sha256:" + hashlib.sha256(slip_bytes).hexdigest()
    db_session.add(PaymentRequest(
        id="PAY-PRIOR", user_id=user.id, package_code="MIN_10", minutes=10,
        amount_mmk=8000, status="APPROVED", slip_fingerprint=fingerprint,
    ))
    await db_session.commit()

    with pytest.raises(HTTPException) as excinfo:
        await api_mod.submit_payment_slip(
            package_code="MIN_10", amount_mmk=8000,
            slip_file=_slip_upload(slip_bytes), user=user, db=db_session, _gate=user,
        )
    assert excinfo.value.status_code == 409
    # The rejected upload did not linger on disk either (M1).
    receipts = tmp_path / "receipts"
    assert not receipts.is_dir() or list(receipts.iterdir()) == []


@pytest.mark.asyncio
async def test_batch_aj_m1_slip_file_removed_when_plan_invalid(db_session, tmp_path, monkeypatch):
    """Unfixed: the slip was saved BEFORE the plan was resolved, so a
    rejected submission (unknown package) orphaned the image in
    receipts/ forever."""
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(api_mod, "DISK_HEADROOM_MIN_FREE_BYTES", 0)
    monkeypatch.setattr(settings, "bot_token", "")
    user, _ = await get_or_create_user(db_session, telegram_id=9305)
    await db_session.commit()

    with pytest.raises(HTTPException):
        await api_mod.submit_payment_slip(
            package_code="NO_SUCH_PLAN", amount_mmk=1,
            slip_file=_slip_upload(b"orphan-slip"), user=user, db=db_session, _gate=user,
        )
    receipts = tmp_path / "receipts"
    assert not receipts.is_dir() or list(receipts.iterdir()) == []


# ── M1: disk headroom gate ───────────────────────────────────────────


def test_batch_aj_m1_disk_headroom_gate(monkeypatch):
    """New uploads/jobs must refuse when the disk is nearly full —
    the 2026-11-04 incident was a full 10 GB disk failing every job
    AFTER charging. The overview only DISPLAYED the state."""
    import shutil as _shutil

    low = SimpleNamespace(total=10 * 2**30, used=9.9 * 2**30, free=100 * 2**20)
    monkeypatch.setattr(_shutil, "disk_usage", lambda _p: low)
    with pytest.raises(HTTPException) as excinfo:
        api_mod._require_disk_headroom()
    assert excinfo.value.status_code == 507

    plenty = SimpleNamespace(total=10 * 2**30, used=1 * 2**30, free=9 * 2**30)
    monkeypatch.setattr(_shutil, "disk_usage", lambda _p: plenty)
    api_mod._require_disk_headroom()  # must not raise


# ── M3: Mini App approve surfaces the claim conflict ─────────────────


@pytest.mark.asyncio
async def test_batch_aj_m3_miniapp_approve_blocked_by_claim(db_session, monkeypatch):
    """With the service-level claim enforcement (money cluster), the
    Mini App endpoint must answer 409 — not a misleading 404."""
    monkeypatch.setattr(settings, "bot_token", "")
    from app.services.payments import create_payment_request
    from app.services.payment_review import claim_payment_review

    user, _ = await get_or_create_user(db_session, telegram_id=9306)
    payment = await create_payment_request(
        session=db_session, user_id=user.id, package_code="MIN_10",
        receipt_file_id="slip-m3-api",
    )
    ok, _, _ = await claim_payment_review(db_session, payment.id, admin_id=111)
    assert ok
    await db_session.commit()

    admin2 = User(id=222, first_name="Admin Two", language_code="my")
    with pytest.raises(HTTPException) as excinfo:
        await api_mod.approve_payment_slip(
            api_mod.ApproveSlipRequest(request_id=payment.id), admin2, db_session
        )
    assert excinfo.value.status_code == 409


# ── M5: admin adjust — idempotency, recap ledger, honest delta ───────


@pytest.mark.asyncio
async def test_batch_aj_m5_adjust_replay_does_not_double_apply(db_session):
    """Unfixed: the adjust endpoint applied whatever it was sent,
    every time — a double-tap / client retry doubled the grant. The
    client now sends an idempotency key per adjust intent."""
    from app.services.ledger import get_minute_balance

    target, _ = await get_or_create_user(db_session, telegram_id=9307)
    await db_session.commit()
    admin = User(id=999, first_name="Admin", language_code="my")
    req = api_mod.AdjustUserRequest(
        user_id=target.id, add_minutes=10, reason="goodwill fix",
        idempotency_key="adj-key-0001",
    )
    await api_mod.adjust_user_status(req, admin, db_session)
    await api_mod.adjust_user_status(req, admin, db_session)  # replay
    bal = await get_minute_balance(db_session, target.id)
    assert bal.purchased_seconds == 600  # NOT 1200


@pytest.mark.asyncio
async def test_batch_aj_m5_recap_adjust_writes_ledger_and_reports_clamped_delta(db_session):
    """Unfixed: recap-credit adjustments moved the balance with NO
    recap-ledger event (the recap ledger simply skipped admin
    adjusts), and a −5 adjust against a 2-credit balance silently
    applied −2 while reporting −5."""
    target, _ = await get_or_create_user(db_session, telegram_id=9308)
    target.recap_credits = 2
    await db_session.commit()
    admin = User(id=999, first_name="Admin", language_code="my")
    req = api_mod.AdjustUserRequest(
        user_id=target.id, add_recap_credits=-5, reason="clawback",
        idempotency_key="adj-key-0002",
    )
    resp = await api_mod.adjust_user_status(req, admin, db_session)
    await db_session.refresh(target)
    assert int(target.recap_credits) == 0
    assert resp["recap_credits_applied"] == -2  # honest clamped delta
    events = (await db_session.execute(
        select(RecapCreditLedgerEvent).where(
            RecapCreditLedgerEvent.user_id == target.id,
            RecapCreditLedgerEvent.reference_id == "ADMINADJ:adj-key-0002",
        )
    )).scalars().all()
    assert len(events) == 1
    assert events[0].direction == "DEBIT"
    assert events[0].credits == 2


# ── M6: dynamic settings bounds ──────────────────────────────────────


def test_batch_aj_m6_money_settings_are_bounded():
    """Unfixed: free_monthly_allowance_seconds = -1 passed validation
    and was stored, wrapping the free allowance to an absurd value;
    the recap duration settings were likewise unbounded."""
    for payload in (
        {"free_monthly_allowance_seconds": -1},
        {"free_monthly_allowance_seconds": 10**12},
        {"free_daily_job_limit": -5},
        {"recap_max_duration_seconds": 0},
        {"recap_free_max_duration_seconds": -60},
    ):
        with pytest.raises(HTTPException):
            api_mod._validate_admin_settings_updates(payload)
    # Sane values still validate.
    out = api_mod._validate_admin_settings_updates(
        {"free_monthly_allowance_seconds": 180, "recap_max_duration_seconds": 600}
    )
    assert out["free_monthly_allowance_seconds"] == 180


# ── M2: Mini App broadcast — records, honesty, plain text ────────────


@pytest.mark.asyncio
async def test_batch_aj_m2_miniapp_broadcast_without_token_is_not_success(db_session, monkeypatch):
    """Unfixed: with no bot token configured the endpoint returned
    {"success": True, "queued"} having sent NOTHING."""
    monkeypatch.setattr(settings, "bot_token", "")
    admin = User(id=999, first_name="Admin", language_code="my")
    with pytest.raises(HTTPException) as excinfo:
        await api_mod.send_admin_broadcast(
            api_mod.BroadcastRequest(message="hello everyone"), admin, db_session
        )
    assert excinfo.value.status_code == 503


@pytest.mark.asyncio
async def test_batch_aj_m2_miniapp_broadcast_records_deliveries(db_session, monkeypatch):
    """Unfixed: the Mini App broadcast wrote only an AuditLog summary
    — no campaign, no per-recipient delivery records — and sent the
    text as HTML while the bot path sends plain text (a stray "<"
    broke or reformatted Mini App broadcasts)."""
    monkeypatch.setattr(settings, "bot_token", "x")
    u1, _ = await get_or_create_user(db_session, telegram_id=12345678)
    u2, _ = await get_or_create_user(db_session, telegram_id=23456789)
    await db_session.commit()

    sends = []

    class _FakeBot:
        def __init__(self, *a, **k):
            self.session = SimpleNamespace(close=AsyncMock())

        async def send_message(self, chat_id, text, **kwargs):
            sends.append((chat_id, text, kwargs))
            if chat_id == 23456789:
                raise Exception("user blocked the bot")

    with patch("aiogram.Bot", _FakeBot):
        resp = await api_mod.send_admin_broadcast(
            api_mod.BroadcastRequest(message="Plain <b>not bold</b> news"),
            User(id=999, first_name="Admin", language_code="my"),
            db_session,
        )
    assert resp["sent_count"] == 1
    # Plain text parity with the bot path: no HTML parse mode.
    assert all("parse_mode" not in kwargs for _, _, kwargs in sends)
    campaigns = (await db_session.execute(select(BroadcastCampaign))).scalars().all()
    assert len(campaigns) == 1
    deliveries = (await db_session.execute(select(BroadcastDelivery))).scalars().all()
    by_user = {d.user_id: d.status for d in deliveries}
    assert by_user[12345678] == "SENT"
    assert by_user[23456789] != "SENT"


# ── H6: admin queue retry must not resurrect a refunded recap job free ─


async def _seed_refunded_recap_job(maker, *, credits_left: int):
    async with maker() as s:
        user, _ = await get_or_create_user(s, telegram_id=9310)
        user.recap_credits = credits_left
        job = Job(
            id="rc_aj_retry", user_id=user.id, status=JobStatus.FAILED.value,
            job_type=JobType.RECAP.value, engine="recap", error_message="boom",
        )
        s.add(job)
        await s.commit()
        req = api_mod.RecapConfigRequest(url="https://example.com/v", title="T", voice_slug="komin")
        job.recap_config = api_mod._persisted_recap_state(
            req, credit_deducted=True, credits_charged=1,
            is_vip=False, phase="full",
            credit_refunded=True, credits_refunded=1,
        )
        await s.commit()
        await record_recap_credit_event(s, user_id=user.id, direction="DEBIT", credits=1, reference_id="rc_aj_retry")
        await record_recap_credit_event(s, user_id=user.id, direction="CREDIT", credits=1, reference_id="REFUND:rc_aj_retry")
        await s.commit()
        return user.id


@pytest.mark.asyncio
async def test_batch_aj_h6_admin_retry_recharges_refunded_recap(tmp_path, monkeypatch):
    """Unfixed (admitted in the old docstring): the admin queue retry
    re-dispatched recap jobs UNCHARGED — a job whose failure had
    already been refunded ran again for free."""
    engine, maker = await _open_store(tmp_path, monkeypatch)
    user_id = await _seed_refunded_recap_job(maker, credits_left=5)
    admin = User(id=999, first_name="Admin", language_code="my")
    try:
        with patch.object(api_mod, "_run_recap_background_job", new=AsyncMock()):
            async with maker() as s:
                resp = await api_mod.retry_queued_job("rc_aj_retry", admin, s)
        assert resp.get("success") is True
        entry = api_mod.RECAP_JOBS_REGISTRY.get("rc_aj_retry", {})
        assert entry.get("credit_deducted") is True
        async with maker() as s:
            u = await s.get(User, user_id)
            assert int(u.recap_credits) == 4  # re-charged on retry
            debits = (await s.execute(
                select(RecapCreditLedgerEvent).where(
                    RecapCreditLedgerEvent.reference_id == "rc_aj_retry",
                    RecapCreditLedgerEvent.direction == "DEBIT",
                )
            )).scalars().all()
            assert len(debits) == 2  # original submit + admin retry
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop("rc_aj_retry", None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_batch_aj_h6_admin_retry_refunded_recap_without_balance_refused(tmp_path, monkeypatch):
    """If the user cannot pay the re-charge, the retry must refuse
    (402) rather than silently run the job free."""
    engine, maker = await _open_store(tmp_path, monkeypatch)
    await _seed_refunded_recap_job(maker, credits_left=0)
    admin = User(id=999, first_name="Admin", language_code="my")
    try:
        with patch.object(api_mod, "_run_recap_background_job", new=AsyncMock()):
            async with maker() as s:
                with pytest.raises(HTTPException) as excinfo:
                    await api_mod.retry_queued_job("rc_aj_retry", admin, s)
        assert excinfo.value.status_code == 402
        assert "rc_aj_retry" not in api_mod.RECAP_JOBS_REGISTRY
        async with maker() as s:
            job = await s.get(Job, "rc_aj_retry")
            assert job.status == JobStatus.FAILED.value  # not re-queued
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop("rc_aj_retry", None)
        await engine.dispose()


# ── H5 recap side: the runner's SUCCESS write must be conditional ────


def test_batch_aj_h5_recap_success_write_is_status_guarded():
    """A cancel landing between the runner's last cancelled-check and
    its SUCCESS write must win: the write is conditioned on the row
    still being active (the voice worker got the same treatment)."""
    import inspect
    import re

    src = inspect.getsource(api_mod._execute_recap_background_job)
    m = re.search(
        r"update\(Job\)\.where\((.{0,500}?)\)\.values\(\s*status=JobStatus\.SUCCESS\.value",
        src, re.S,
    )
    assert m, "the runner's SUCCESS update statement was not found"
    assert "Job.status.in_" in m.group(1), (
        "the SUCCESS write is not conditioned on the row still being active"
    )
    assert "_succ_written" in src  # rowcount is checked and acted on


# ── L5: streaming upload cleanup on cancellation ─────────────────────


@pytest.mark.asyncio
async def test_batch_aj_l5_streaming_upload_cancel_removes_partial(tmp_path):
    """asyncio.CancelledError is a BaseException: the old helper's
    `except Exception` cleanup let a client disconnect mid-upload
    leave the partial file behind forever."""
    import inspect as _inspect

    src = _inspect.getsource(api_mod._save_upload_streaming)
    assert "BaseException" in src

    class _CancellingUpload:
        filename = "big.mp4"

        def __init__(self):
            self._calls = 0

        async def read(self, size=-1):
            self._calls += 1
            if self._calls > 1:
                raise asyncio.CancelledError()
            return b"x" * 1024

    dest = tmp_path / "partial.mp4"
    with pytest.raises(asyncio.CancelledError):
        await api_mod._save_upload_streaming(_CancellingUpload(), dest, 10 * 1024 * 1024)
    assert not dest.exists()


# ── L11: a stream token must not outlive a ban ───────────────────────


@pytest.mark.asyncio
async def test_batch_aj_l11_blocked_user_token_download_refused(db_session, tmp_path, monkeypatch):
    """Token downloads authenticate by the signed token alone (no
    initData). A user banned AFTER minting the token kept downloading
    for the token's full 2 h TTL."""
    from starlette.requests import Request

    monkeypatch.setattr(settings, "bot_token", "aj" + "-test-token-" + "0" * 8)
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    user, _ = await get_or_create_user(db_session, telegram_id=9311)
    await db_session.commit()
    video = tmp_path / "final.mp4"
    video.write_bytes(b"v" * 2048)
    api_mod.RECAP_JOBS_REGISTRY["rc_aj_l11"] = {
        "user_id": user.id, "status": "COMPLETED",
        "video_path": str(video), "title": "T",
    }
    request = Request({
        "type": "http", "method": "GET",
        "path": "/api/recap/download/rc_aj_l11/video",
        "headers": [], "query_string": b"",
    })
    try:
        token = api_mod._make_stream_token("rc_aj_l11", user.id, "video")
        assert token
        # Sanity: while ACTIVE the token works.
        resp = await api_mod.download_recap_file(
            "rc_aj_l11", "video", request, user=None, t=token, db=db_session
        )
        assert resp is not None

        user.status = "BLOCKED"
        await db_session.commit()
        with pytest.raises(HTTPException) as excinfo:
            await api_mod.download_recap_file(
                "rc_aj_l11", "video", request, user=None, t=token, db=db_session
            )
        assert excinfo.value.status_code in (403, 404)
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop("rc_aj_l11", None)


# ── L12 → Batch AP: Telegram delivery is video only ────────────────


@pytest.mark.asyncio
async def test_batch_aj_l12_superseded_by_batch_ap_video_only(tmp_path, monkeypatch):
    """DELIBERATE BEHAVIOUR REVERSAL (Batch AP, owner order 2026-11-05).

    AJ L12 made this sender deliver the narration audio and script as
    extras (the SRT already went along). The owner has since ordered
    the opposite default: Telegram recap delivery is the video only,
    and each companion file is sent solely when the user presses its
    own button on the bot completion message. The helper therefore no
    longer accepts companion paths at all. Batch AP's own test file
    pins the button handlers that replaced this behaviour.
    """
    import inspect

    assert "audio_path" not in inspect.signature(
        api_mod._send_recap_telegram_delivery
    ).parameters
    assert "srt_path" not in inspect.signature(
        api_mod._send_recap_telegram_delivery
    ).parameters
    assert "script_path" not in inspect.signature(
        api_mod._send_recap_telegram_delivery
    ).parameters

    monkeypatch.setattr(settings, "bot_token", "x")
    delivered_dir = tmp_path / "outputs"
    delivered_dir.mkdir()
    video = delivered_dir / "VoiceClonePro_RecapVideo_x.mp4"
    video.write_bytes(b"v" * 2048)

    calls = []

    class _FakeBot:
        def __init__(self, *a, **k):
            self.session = SimpleNamespace(close=AsyncMock())

        async def _record(self, method, **kwargs):
            paths = [
                str(v.path) for v in kwargs.values()
                if getattr(v, "path", None) is not None
            ]
            calls.append((method, paths))

        async def send_video(self, **kwargs):
            await self._record("send_video", **kwargs)

        async def send_document(self, **kwargs):
            await self._record("send_document", **kwargs)

        async def send_audio(self, **kwargs):
            await self._record("send_audio", **kwargs)

        async def send_message(self, **kwargs):
            await self._record("send_message", **kwargs)

    with patch("aiogram.Bot", _FakeBot):
        delivered, _reason = await api_mod._send_recap_telegram_delivery(
            424242,
            str(video),
            "Some Title", "style", "Ko Min", 60,
            job_id="rc_aj_l12",
        )
    assert delivered is True
    assert calls == [("send_video", [str(video)])]


# ── L13: active-job count must fail visibly, not silently undercount ──


@pytest.mark.asyncio
async def test_batch_aj_l13_active_count_db_error_surfaces():
    """The DB half of _recap_active_job_count swallowed every error
    and returned a registry-only count — exactly when the database
    was struggling, the concurrency cap silently degraded."""

    class _BrokenDB:
        async def execute(self, *a, **k):
            raise RuntimeError("db gone")

    with pytest.raises(Exception):
        await api_mod._recap_active_job_count(_BrokenDB(), 4242)


# ── L16: the "notification sent" log must not lie ────────────────────


def test_batch_aj_l16_notify_success_log_only_after_send():
    import inspect

    src = inspect.getsource(api_mod._execute_recap_background_job)
    marker = "Notification sent to Telegram"
    idx = src.find(marker)
    assert idx > 0, "success log line missing"
    send_idx = src.rfind("await bot_inst.send_message", 0, idx)
    assert send_idx > 0, "the log line is not downstream of the actual send"
    between = src[send_idx:idx]
    assert "finally:" not in between and "except Exception as notify_err" not in between, (
        "the log line must sit inside the send's success path, not after "
        "the try/except that swallows send failures"
    )


# ── Sweeps: L6 outputs orphans, M1 receipts, L17 delivery retention ──


@pytest.mark.asyncio
async def test_batch_aj_sweeps_outputs_receipts_and_deliveries(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch, name="aj_sweep.db")
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    old = time.time() - 100 * 86400

    outputs = tmp_path / "outputs"
    outputs.mkdir(parents=True)
    orphan_out = outputs / "orphan.wav"
    orphan_out.write_bytes(b"x")
    os.utime(orphan_out, (old, old))
    live_out = outputs / "live.wav"
    live_out.write_bytes(b"x")
    os.utime(live_out, (old, old))

    receipts = tmp_path / "receipts"
    receipts.mkdir(parents=True)
    orphan_slip = receipts / "slip_orphan.jpg"
    orphan_slip.write_bytes(b"x")
    os.utime(orphan_slip, (old, old))
    live_slip = receipts / "slip_live.jpg"
    live_slip.write_bytes(b"x")
    os.utime(live_slip, (old, old))

    async with maker() as s:
        user, _ = await get_or_create_user(s, telegram_id=9312)
        s.add(Job(
            id="rc_aj_sweep", user_id=user.id, status=JobStatus.RUNNING.value,
            job_type=JobType.RECAP.value, engine="recap", result_path=str(live_out),
        ))
        s.add(PaymentRequest(
            id="PAY-LIVE", user_id=user.id, package_code="MIN_10", minutes=10,
            amount_mmk=8000, status="PENDING",
            slip_url="/api/receipts/slip_live.jpg",
        ))
        camp = BroadcastCampaign(
            id="bc_aj", message_text="hi", total_recipients=2, admin_id=999,
            confirmation_token="tok-aj",
        )
        s.add(camp)
        await s.flush()
        from datetime import datetime, timedelta, timezone
        s.add(BroadcastDelivery(
            campaign_id="bc_aj", user_id=12345678,
            status="SENT", sent_at=datetime.now(timezone.utc) - timedelta(days=100),
        ))
        s.add(BroadcastDelivery(
            campaign_id="bc_aj", user_id=23456789,
            status="SENT", sent_at=datetime.now(timezone.utc) - timedelta(days=10),
        ))
        await s.commit()

    await api_mod.cleanup_expired_jobs()

    assert not orphan_out.exists()   # L6: old + unreferenced → swept
    assert live_out.exists()         # referenced by a live job → kept
    assert not orphan_slip.exists()  # M1: unreferenced old slip → swept
    assert live_slip.exists()        # referenced by a pending payment → kept
    async with maker() as s:
        rows = (await s.execute(select(BroadcastDelivery))).scalars().all()
        assert len(rows) == 1  # L17: only the recent delivery row remains
    await engine.dispose()


# ── L18: in-memory upload registries are bounded ─────────────────────


def test_batch_aj_l18_upload_registries_bounded():
    cap_fn = getattr(api_mod, "_remember_bounded", None)
    assert cap_fn is not None, "bounded-registry helper missing"
    d: dict = {}
    for i in range(1300):
        cap_fn(d, f"k{i}", i, cap=1000)
    assert len(d) <= 1000
    assert d["k1299"] == 1299
