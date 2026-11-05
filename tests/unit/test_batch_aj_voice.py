"""Batch AJ — adversarial weakness-hunt fixes: VOICE cluster.

Findings covered: H5 (a voice job cancelled mid-flight was still
charged in full and overwritten to SUCCESS), H7 (voice audio was
persisted + downloadable BEFORE the charge, and the download endpoint
never checked the job status — failed-charge jobs were downloadable
free), M11 worker slice (no deadline on a voice job — a hung
synthesis pinned the single worker slot forever).

Written against the UNFIXED tree first: each test fails there for the
reason stated in its docstring.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import session as session_mod
from app.db.base import Base
from app.models.schema import Job, VoiceModel
from app.services.ledger import get_minute_balance, get_or_create_user
from app.services.queue import create_voice_clone_job


async def _make_voice_job(db_session, tmp_path, monkeypatch, *, welcome_seconds=300, telegram_id=9201):
    """A real QUEUED voice job + funded user, on the shared harness
    the existing worker tests use."""
    from app.config import settings

    monkeypatch.setattr(settings, "storage_root", tmp_path)
    user, _ = await get_or_create_user(db_session, telegram_id=telegram_id)

    pth_file = tmp_path / "test.pth"
    pth_file.write_bytes(b"pth_content")
    model = VoiceModel(
        name="Test Model",
        slug=f"test-model-{telegram_id}",
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
        text="Batch AJ voice test",
        edge_voice="my-MM-ThihaNeural",
        model_id=model.id,
        chat_id=telegram_id,
    )
    # Fund AFTER creation: create_voice_clone_job itself grants the
    # monthly-free allowance (queue.py), so bucket control must be
    # asserted from this point. Zero every bucket, then fund welcome
    # only — the assertions below track exact bucket movements.
    bal = await get_minute_balance(db_session, user.id)
    bal.monthly_free_seconds = 0
    bal.referral_seconds = 0
    bal.purchased_seconds = 0
    bal.welcome_seconds = welcome_seconds
    await db_session.commit()
    return user, job


def _mock_synthesis(tmp_path):
    def _synth(self, **kwargs):
        out = Path(kwargs["output_wav"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake_voxcpm_wav")
        return out

    return _synth


def _mock_bot():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_audio = AsyncMock()
    bot.edit_message_text = AsyncMock()
    return bot


# ── H5: cancel landing after synthesis, before billing ───────────────


@pytest.mark.asyncio
async def test_batch_aj_h5_cancel_before_billing_never_charges(db_session, tmp_path, monkeypatch):
    """Unfixed hole: the cancel endpoint flipped the row to CANCELLED
    and refunded, but the worker never re-read the status — it billed
    the full duration anyway and overwrote the row to SUCCESS. The
    user was told "cancelled + refunded" and charged in full."""
    from app.worker import process_single_job

    user, job = await _make_voice_job(db_session, tmp_path, monkeypatch, telegram_id=9202)

    # The cancel lands while the job renders: emulate the endpoint's
    # DB write at the moment stage 4 begins (the tracker step hook is
    # the last event-loop point before duration + billing).
    import app.worker as worker_mod

    orig_update_step = worker_mod.JobProgressTracker.update_step

    async def _cancel_at_step4(self, step, *a, **k):
        if step == 4:
            await db_session.execute(
                update(Job).where(Job.id == job.id).values(status="CANCELLED")
            )
            await db_session.flush()
        return await orig_update_step(self, step, *a, **k)

    with (
        patch("app.services.voxcpm_engine.VoxCPMEngine.synthesize", _mock_synthesis(tmp_path)),
        patch("app.worker.get_wav_duration_seconds", return_value=15),
        patch("app.worker.sync_output_to_cloud", new=AsyncMock()),
        patch.object(worker_mod.JobProgressTracker, "update_step", _cancel_at_step4),
    ):
        processed = await process_single_job(
            session=db_session, job=job, bot=_mock_bot(), chat_id=9202, status_msg_id=None
        )

    assert processed is False
    await db_session.refresh(job)
    assert job.status == "CANCELLED"  # not overwritten to SUCCESS
    bal = await get_minute_balance(db_session, user.id)
    assert bal.welcome_seconds == 300  # never charged


@pytest.mark.asyncio
async def test_batch_aj_h5_cancel_after_billing_refunds_and_stays_cancelled(db_session, tmp_path, monkeypatch):
    """The second window: cancel lands after the billing commit but
    before delivery. The charge must be refunded and the row must
    stay CANCELLED — never SUCCESS."""
    from app.worker import process_single_job

    user, job = await _make_voice_job(db_session, tmp_path, monkeypatch, telegram_id=9203)

    async def _cancel_during_cloud_sync(job_id, path):
        await db_session.execute(
            update(Job).where(Job.id == job_id).values(status="CANCELLED")
        )
        await db_session.flush()

    with (
        patch("app.services.voxcpm_engine.VoxCPMEngine.synthesize", _mock_synthesis(tmp_path)),
        patch("app.worker.get_wav_duration_seconds", return_value=15),
        patch("app.worker.sync_output_to_cloud", new=AsyncMock(side_effect=_cancel_during_cloud_sync)),
    ):
        processed = await process_single_job(
            session=db_session, job=job, bot=_mock_bot(), chat_id=9203, status_msg_id=None
        )

    assert processed is False
    await db_session.refresh(job)
    assert job.status == "CANCELLED"
    bal = await get_minute_balance(db_session, user.id)
    assert bal.welcome_seconds == 300  # billed, then refunded in full


# ── H7: charge first, persist second; download requires SUCCESS ─────


@pytest.mark.asyncio
async def test_batch_aj_h7_failed_charge_leaves_no_downloadable_result(db_session, tmp_path, monkeypatch):
    """Unfixed hole: stage 4 copied the audio into outputs/ and set
    result_path (and uploaded it to the cloud) BEFORE consume_seconds
    ran — a job whose charge then failed still carried a result_path,
    and the download endpoint served it to the owner for free."""
    from app.worker import process_single_job

    # No balance at all: the charge must fail.
    user, job = await _make_voice_job(
        db_session, tmp_path, monkeypatch, welcome_seconds=0, telegram_id=9204
    )

    with (
        patch("app.services.voxcpm_engine.VoxCPMEngine.synthesize", _mock_synthesis(tmp_path)),
        patch("app.worker.get_wav_duration_seconds", return_value=15),
        patch("app.worker.sync_output_to_cloud", new=AsyncMock()),
    ):
        processed = await process_single_job(
            session=db_session, job=job, bot=_mock_bot(), chat_id=9204, status_msg_id=None
        )

    assert processed is False
    await db_session.refresh(job)
    assert job.status == "FAILED"
    assert not job.result_path  # nothing persisted as a deliverable
    outputs_dir = tmp_path / "outputs"
    leftovers = list(outputs_dir.glob(f"{job.id}_*")) if outputs_dir.is_dir() else []
    assert leftovers == []


@pytest.mark.asyncio
async def test_batch_aj_h7_download_endpoint_requires_success_status(db_session, tmp_path, monkeypatch):
    """The download gate itself: a non-SUCCESS job's audio 404s even
    when the file exists and the requester owns the job."""
    from fastapi import HTTPException

    from app.webapp import api as api_mod

    user, job = await _make_voice_job(db_session, tmp_path, monkeypatch, telegram_id=9205)
    leaked = tmp_path / "outputs" / f"{job.id}_leaked.wav"
    leaked.parent.mkdir(parents=True, exist_ok=True)
    leaked.write_bytes(b"fake")
    job.result_path = str(leaked)
    job.status = "FAILED"
    await db_session.commit()

    request = SimpleNamespace(headers={}, url=SimpleNamespace(path="/api/audio/download/x"))
    with pytest.raises(HTTPException) as excinfo:
        await api_mod.download_job_audio(job.id, request, user, db_session)
    assert excinfo.value.status_code == 404

    # And the happy path still works for a genuinely successful job.
    job.status = "SUCCESS"
    await db_session.commit()
    resp = await api_mod.download_job_audio(job.id, request, user, db_session)
    assert resp is not None


@pytest.mark.asyncio
async def test_batch_aj_h5_worker_upload_result_cannot_resurrect_cancelled(db_session, tmp_path, monkeypatch):
    """The laptop-worker upload endpoint flipped ANY job to SUCCESS
    on upload — including one the user/admin had cancelled (and been
    refunded for) while the laptop was rendering it."""
    from fastapi import HTTPException, UploadFile

    from app.config import settings
    from app.webapp import api as api_mod

    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "worker_token", "aj-worker-token")
    user, job = await _make_voice_job(db_session, tmp_path, monkeypatch, telegram_id=9206)
    job.status = "CANCELLED"
    await db_session.commit()

    import io

    upload = UploadFile(filename="result.wav", file=io.BytesIO(b"fake wav bytes"))
    with pytest.raises(HTTPException) as excinfo:
        await api_mod.upload_worker_job_result(
            job.id, upload, "aj-worker-token", db_session
        )
    assert excinfo.value.status_code == 409
    await db_session.refresh(job)
    assert job.status == "CANCELLED"


# ── M11 (worker slice): a hung job hits a deadline ───────────────────


@pytest.mark.asyncio
async def test_batch_aj_m11_worker_deadline_fails_hung_job(tmp_path, monkeypatch):
    """Unfixed: claim_and_process_next_job awaited process_single_job
    forever — one hung synthesis pinned the only worker slot (and the
    job) indefinitely. With the deadline, the job is failed + the
    claim returns so the loop moves on."""
    import app.worker as worker_mod

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/aj_m11.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", maker)
    # app.worker binds AsyncSessionLocal at module import — patch its
    # namespace too, or the claim path uses the real factory.
    monkeypatch.setattr(worker_mod, "AsyncSessionLocal", maker)
    monkeypatch.setattr(worker_mod, "VOICE_JOB_DEADLINE_SECONDS", 0.3)

    async with maker() as s:
        user, _ = await get_or_create_user(s, 9207)
        s.add(Job(
            id="voice-hung-1", user_id=user.id, chat_id=user.id,
            status="QUEUED", job_type="VOICE_CLONE", engine="voxcpm",
            input_text="hung job",
        ))
        await s.commit()

    async def _hang(session, job, bot=None):
        await asyncio.sleep(30)
        return True

    with patch.object(worker_mod, "process_single_job", new=_hang):
        processed = await worker_mod.claim_and_process_next_job(bot=None)

    assert processed is False
    async with maker() as s:
        row = await s.get(Job, "voice-hung-1")
        assert row.status == "FAILED"
    await engine.dispose()
