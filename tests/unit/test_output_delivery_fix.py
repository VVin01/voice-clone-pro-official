"""Output-delivery fixes (2026-10-30 QA sweep).

Live evidence: the Render service has no persistent disk, so generated
outputs vanish on every redeploy/restart, and every post-loss fallback
path was broken — the bot showed raw /api/... download URLs that require
Telegram init-data auth (401 in a browser), the send_audio handler's own
HTTP fallback called the same URL without auth, the bot progress card
never flipped to "completed", and the admin main-menu Broadcast button
emitted callback data with no registered handler.
"""
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models.schema  # noqa: F401  (register models before db fixtures)


def _mk_cb(data, uid=7788):
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=uid, is_bot=False, first_name="QA")
    cb.data = data
    cb.bot = MagicMock()
    cb.bot.send_audio = AsyncMock()
    cb.bot.send_video = AsyncMock()
    cb.bot.send_document = AsyncMock()
    cb.bot.send_message = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _patch_clone_sessions(monkeypatch, db_session):
    import app.handlers.clone as clone_mod

    maker = async_sessionmaker(
        bind=db_session.bind, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(clone_mod, "AsyncSessionLocal", maker)
    return clone_mod


async def _seed_user_job(db_session, uid=7788, result_path=None, job_type="VOICE_CLONE"):
    from app.models.enums import JobStatus
    from app.models.schema import Job
    from app.services.ledger import get_or_create_user

    user, _ = await get_or_create_user(db_session, uid)
    job_id = str(uuid.uuid4())
    job = Job(
        id=job_id,
        user_id=uid,
        status=JobStatus.SUCCESS.value,
        result_path=str(result_path) if result_path else None,
        engine="voxcpm",
        job_type=job_type,
    )
    db_session.add(job)
    await db_session.commit()
    return job_id


def _all_button_targets(kb):
    targets = []
    for row in kb.inline_keyboard:
        for btn in row:
            if btn.url:
                targets.append(("url", btn.url))
            if btn.web_app:
                targets.append(("web_app", btn.web_app.url))
            if btn.callback_data:
                targets.append(("callback", btn.callback_data))
    return targets


def test_admin_main_menu_broadcast_button_has_handler():
    """The admin entry in the main menu must emit data a router handles."""
    from app.ui.keyboards import main_menu_kb

    kb = main_menu_kb(is_admin=True, lang="my")
    datas = [d for kind, d in _all_button_targets(kb) if kind == "callback"]
    assert "admin:broadcast_prompt" not in datas
    assert "admin:broadcast" in datas


@pytest.mark.asyncio
async def test_send_audio_callback_delivers_when_file_present(db_session, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "storage_root", tmp_path)
    out_dir = tmp_path / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    f = out_dir / "result.wav"
    f.write_bytes(b"RIFF" + b"\x00" * 5000)
    job_id = await _seed_user_job(db_session, result_path=f)
    clone_mod = _patch_clone_sessions(monkeypatch, db_session)

    cb = _mk_cb(f"send_audio_{job_id}")
    await clone_mod.handle_send_audio_callback(cb)
    cb.bot.send_audio.assert_awaited_once()
    assert cb.bot.send_audio.call_args[1]["chat_id"] == 7788


@pytest.mark.asyncio
async def test_send_audio_callback_missing_file_no_dead_link(db_session, tmp_path, monkeypatch):
    """File wiped (redeploy): the reply must not contain the raw
    auth-protected /api/audio/download URL anywhere."""
    from app.config import settings

    monkeypatch.setattr(settings, "storage_root", tmp_path)
    (tmp_path / "outputs").mkdir(parents=True, exist_ok=True)
    job_id = await _seed_user_job(
        db_session, result_path=tmp_path / "outputs" / "gone.wav"
    )
    clone_mod = _patch_clone_sessions(monkeypatch, db_session)

    cb = _mk_cb(f"send_audio_{job_id}")
    await clone_mod.handle_send_audio_callback(cb)
    cb.bot.send_audio.assert_not_called()
    cb.bot.send_message.assert_awaited_once()
    kwargs = cb.bot.send_message.call_args[1]
    assert "/api/audio/download" not in kwargs["text"]
    targets = _all_button_targets(kwargs["reply_markup"])
    assert targets, "expected a Mini App button"
    for kind, url in targets:
        assert "/api/" not in url


@pytest.mark.asyncio
async def test_send_audio_callback_rejects_non_owner(db_session, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "storage_root", tmp_path)
    out_dir = tmp_path / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    f = out_dir / "result.wav"
    f.write_bytes(b"RIFF" + b"\x00" * 5000)
    job_id = await _seed_user_job(db_session, uid=7788, result_path=f)
    clone_mod = _patch_clone_sessions(monkeypatch, db_session)

    cb = _mk_cb(f"send_audio_{job_id}", uid=9999)  # different user taps
    await clone_mod.handle_send_audio_callback(cb)
    cb.bot.send_audio.assert_not_called()
    cb.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_recap_callback_large_file_uses_miniapp_not_dead_url(
    db_session, tmp_path, monkeypatch
):
    """>50MB recap: must route via Mini App button, never a raw
    /api/recap/download URL button (401 without init-data auth)."""
    from app.config import settings

    monkeypatch.setattr(settings, "storage_root", tmp_path)
    big = tmp_path / "big_recap.mp4"
    with open(big, "wb") as fh:
        fh.truncate(51 * 1024 * 1024)
    job_id = "rc_" + uuid.uuid4().hex[:12]
    from app.models.enums import JobStatus
    from app.models.schema import Job
    from app.services.ledger import get_or_create_user

    await get_or_create_user(db_session, 7788)
    db_session.add(
        Job(
            id=job_id,
            user_id=7788,
            status=JobStatus.SUCCESS.value,
            result_path=str(big),
            job_type="RECAP",
        )
    )
    await db_session.commit()
    clone_mod = _patch_clone_sessions(monkeypatch, db_session)

    # resolve_recap_deliverable_path returns the DB result_path for video
    # when the file exists; no srt exists, so only the >50MB message sends.
    cb = _mk_cb(f"send_recap_{job_id}")
    await clone_mod.handle_send_recap_callback(cb)
    cb.bot.send_video.assert_not_called()
    cb.bot.send_message.assert_awaited_once()
    kwargs = cb.bot.send_message.call_args[1]
    assert "/api/recap/download" not in kwargs["text"]
    targets = _all_button_targets(kwargs["reply_markup"])
    assert any(kind == "web_app" for kind, _ in targets)
    for kind, url in targets:
        assert "/api/" not in url


@pytest.mark.asyncio
async def test_worker_completion_flips_status_card_and_notifies(db_session, tmp_path, monkeypatch):
    """Bot/Mini App job completion (notify branch — the only branch
    production jobs can take, since Job has no auto_deliver column):
    the progress card must be edited to the completed text AND the
    notification with the send_audio_ button must be sent."""
    from app.config import settings
    from app.models.schema import VoiceModel
    from app.services.ledger import get_minute_balance, get_or_create_user
    from app.services.queue import create_voice_clone_job
    from app.worker import process_single_job

    monkeypatch.setattr(settings, "storage_root", tmp_path)

    user, _ = await get_or_create_user(db_session, telegram_id=9009)
    bal = await get_minute_balance(db_session, user.id)
    bal.welcome_seconds = 300

    pth_file = tmp_path / "test.pth"
    pth_file.write_bytes(b"pth_content")
    model = VoiceModel(
        name="Test Model",
        slug="test-model-statusflip",
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
        text="Status card flip test",
        edge_voice="my-MM-ThihaNeural",
        model_id=model.id,
        chat_id=9009,
    )
    assert job is not None
    job.status_message_id = 5555
    await db_session.commit()

    mock_bot = AsyncMock()

    def mock_voxcpm(text, output_wav, **kwargs):
        output_wav = Path(output_wav)
        output_wav.parent.mkdir(parents=True, exist_ok=True)
        output_wav.write_bytes(b"fake_voxcpm_wav")
        return output_wav

    with (
        patch("app.services.voxcpm_engine.VoxCPMEngine.synthesize", side_effect=mock_voxcpm),
        patch("app.worker.get_wav_duration_seconds", return_value=15),
        patch("app.worker.sync_output_to_cloud", new=AsyncMock()),
    ):
        processed = await process_single_job(
            session=db_session, job=job, bot=mock_bot, chat_id=9009, status_msg_id=5555
        )
    assert processed is True

    # Notification with the on-demand delivery button was sent
    mock_bot.send_message.assert_awaited()
    notify_kwargs = mock_bot.send_message.call_args[1]
    targets = _all_button_targets(notify_kwargs["reply_markup"])
    assert ("callback", f"send_audio_{job.id}") in targets

    # Status card flipped to the completed text
    edit_texts = [
        (c.kwargs.get("text") or (c.args[0] if c.args else ""))
        for c in mock_bot.edit_message_text.call_args_list
    ]
    assert any("အောင်မြင်စွာ ဖန်တီးပြီးပါပြီ" in (tx or "") for tx in edit_texts), edit_texts
