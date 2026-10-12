import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from app.models.enums import JobStatus
from app.models.schema import Job, VoiceModel
from app.services.ledger import get_minute_balance, get_or_create_user
from app.services.progress import (
    JobProgressTracker,
    render_stepper_progress,
    safe_edit_status,
)
from app.services.queue import create_voice_clone_job
from app.worker import process_single_job


def test_render_stepper_progress_burmese():
    """Verify Burmese stepper accurately marks done, active, and waiting steps."""
    # Step 1 active
    text_s1 = render_stepper_progress(step=1, model_name="တွတ်ပီ", elapsed_seconds=10, spinner="⏳", lang="my")
    assert "Voice Studio — အသံဖန်တီးနေသည်" in text_s1
    assert "မော်ဒယ်လ်: <b>တွတ်ပီ</b>" in text_s1
    assert "ကြာချိန်: <b>00:10</b>" in text_s1
    assert "🔄 <b>[1/4] စစ်ဆေးခြင်းနှင့် Queue စာရင်း:</b> <b>စစ်ဆေးနေသည်</b>" in text_s1
    assert "⏳ <b>[2/4] မူရင်းအသံ:</b> <i>စောင့်ဆိုင်းနေသည်</i>" in text_s1
    assert "⏳ <b>[3/4] AI အသံတုပြောင်းလဲခြင်း:</b> <i>စောင့်ဆိုင်းနေသည်</i>" in text_s1
    assert "⏳ <b>[4/4] စစ်ဆေးခြင်းနှင့် ပေးပို့ခြင်း:</b> <i>စောင့်ဆိုင်းနေသည်</i>" in text_s1

    # Step 3 active (during Applio conversion with custom spinner)
    text_s3 = render_stepper_progress(step=3, model_name="တွတ်ပီ", elapsed_seconds=85, spinner="⠹", lang="my")
    assert "ကြာချိန်: <b>01:25</b>" in text_s3
    assert "✅ <b>[1/4] စစ်ဆေးခြင်းနှင့် Queue စာရင်း:</b> <i>ပြီးစီးပါပြီ</i>" in text_s3
    assert "✅ <b>[2/4] မူရင်းအသံ:</b> <i>ပြီးစီးပါပြီ</i>" in text_s3
    assert "🔄 <b>[3/4] AI အသံတုပြောင်းလဲခြင်း:</b> <b>ပြောင်းလဲနေသည် ⠹</b>" in text_s3
    assert "⏳ <b>[4/4] စစ်ဆေးခြင်းနှင့် ပေးပို့ခြင်း:</b> <i>စောင့်ဆိုင်းနေသည်</i>" in text_s3


def test_render_stepper_progress_english():
    """Verify English stepper renders proper titles, model, elapsed time and steps."""
    text = render_stepper_progress(step=2, model_name="Zawgyi", elapsed_seconds=12, spinner="⏳", lang="en")
    assert "Voice Studio — Processing" in text
    assert "Model: <b>Zawgyi</b>" in text
    assert "Elapsed Time: <b>00:12</b>" in text
    assert "✅ <b>[1/4] Validation & Queue:</b> <i>Completed</i>" in text
    assert "🔄 <b>[2/4] Base Audio:</b> <b>Generating</b>" in text
    assert "⏳ <b>[3/4] AI Voice Conversion:</b> <i>Waiting</i>" in text


@pytest.mark.asyncio
async def test_job_progress_tracker_step_progression():
    """Verify JobProgressTracker updates message step by step."""
    mock_bot = AsyncMock()
    tracker = JobProgressTracker(
        bot=mock_bot,
        chat_id=12345,
        message_id=67890,
        model_name="တွတ်ပီ",
        lang="my",
    )

    await tracker.update_step(1)
    assert mock_bot.edit_message_text.call_count == 1
    call_args_1 = mock_bot.edit_message_text.call_args[1]
    assert call_args_1["chat_id"] == 12345
    assert call_args_1["message_id"] == 67890
    assert "[1/4]" in call_args_1["text"]

    await tracker.update_step(2)
    assert mock_bot.edit_message_text.call_count == 2
    assert "[2/4]" in mock_bot.edit_message_text.call_args[1]["text"]

    await tracker.update_step(4)
    assert mock_bot.edit_message_text.call_count == 3
    assert "[4/4]" in mock_bot.edit_message_text.call_args[1]["text"]


@pytest.mark.asyncio
async def test_job_progress_tracker_ticker_lifecycle_and_cancellation():
    """Verify live ticker animates spinner and cancels cleanly."""
    mock_bot = AsyncMock()
    tracker = JobProgressTracker(
        bot=mock_bot,
        chat_id=12345,
        message_id=67890,
        model_name="တွတ်ပီ",
        lang="my",
        update_interval=0.02,  # fast interval for unit test
    )

    await tracker.start_ticker(step=3)
    assert tracker._ticker_task is not None
    assert not tracker._ticker_task.done()

    # Let ticker run for a few ticks
    await asyncio.sleep(0.07)

    # Stop ticker
    await tracker.stop_ticker()
    assert tracker._ticker_task is None

    # Verify bot.edit_message_text was called during ticker
    assert mock_bot.edit_message_text.call_count >= 2


@pytest.mark.asyncio
async def test_safe_edit_status_telegram_error_handling():
    """Verify safe_edit_status handles flood control and bad requests without crashing."""
    mock_bot = AsyncMock()

    # 1. TelegramRetryAfter (flood control)
    mock_bot.edit_message_text.side_effect = TelegramRetryAfter(method=MagicMock(), message="Too Many Requests", retry_after=1)
    res_retry = await safe_edit_status(mock_bot, 123, 456, "status text")
    assert res_retry is False

    # 2. TelegramBadRequest with 'message is not modified'
    mock_bot.edit_message_text.side_effect = TelegramBadRequest(method=MagicMock(), message="Bad Request: message is not modified")
    res_bad = await safe_edit_status(mock_bot, 123, 456, "status text")
    assert res_bad is True

    # 3. Generic Exception
    mock_bot.edit_message_text.side_effect = RuntimeError("Network dropped")
    res_err = await safe_edit_status(mock_bot, 123, 456, "status text")
    assert res_err is False

    # 4. None bot / chat_id / message_id
    assert await safe_edit_status(None, 123, 456, "status text") is False
    assert await safe_edit_status(mock_bot, None, 456, "status text") is False
    assert await safe_edit_status(mock_bot, 123, None, "status text") is False


@pytest.mark.asyncio
async def test_worker_execution_with_progress_tracker_success(db_session, tmp_path):
    """Verify full worker execution integrates JobProgressTracker and delivers audio."""
    user, _ = await get_or_create_user(db_session, telegram_id=7001)
    bal = await get_minute_balance(db_session, user.id)
    bal.welcome_seconds = 300

    pth_file = tmp_path / "ProgressModel.pth"
    pth_file.write_bytes(b"pth_content")
    model = VoiceModel(
        name="ProgressModel",
        slug="system-progressmodel",
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
        text="Testing progress stepper in worker",
        edge_voice="my-MM-ThihaNeural",
        model_id=model.id,
        chat_id=7001,
        status_message_id=999,
    )

    mock_bot = AsyncMock()
    mock_bot.send_audio.return_value = MagicMock(audio=MagicMock(file_id="tg_test_file_id_7001"))

    async def mock_tts(text, out_path, voice, speed):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake_mp3")
        return out_path

    async def mock_norm(in_audio, out_wav):
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        out_wav.write_bytes(b"fake_wav")
        return out_wav

    async def mock_applio(model_pth, index_path, input_wav, output_wav, pitch=0, **kwargs):
        # Emulate conversion time
        await asyncio.sleep(0.05)
        output_wav.parent.mkdir(parents=True, exist_ok=True)
        output_wav.write_bytes(b"fake_converted")
        return output_wav

    with (
        patch("app.worker.generate_edge_tts", side_effect=mock_tts),
        patch("app.worker.normalize_audio_to_wav", side_effect=mock_norm),
        patch("app.worker.run_applio_conversion", side_effect=mock_applio),
        patch("app.worker.get_wav_duration_seconds", return_value=18),
    ):
        success = await process_single_job(session=db_session, job=job, bot=mock_bot)
        assert success is True

    assert job.status == JobStatus.SUCCESS.value
    assert job.seconds_consumed == 18
    assert job.result_path == "tg_test_file_id_7001"

    # Verify safe_edit_status was called across stages including completion
    assert mock_bot.edit_message_text.call_count >= 4
    mock_bot.send_audio.assert_called_once()


@pytest.mark.asyncio
async def test_worker_execution_with_progress_tracker_failure_stops_ticker(db_session, tmp_path):
    """Verify when Applio conversion fails, ticker is cancelled and job is marked FAILED."""
    user, _ = await get_or_create_user(db_session, telegram_id=7002)
    bal = await get_minute_balance(db_session, user.id)
    bal.welcome_seconds = 300

    pth_file = tmp_path / "FailProgressModel.pth"
    pth_file.write_bytes(b"pth_content")
    model = VoiceModel(
        name="FailProgressModel",
        slug="system-failprogressmodel",
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
        text="Testing failure progress cleanup",
        edge_voice="my-MM-ThihaNeural",
        model_id=model.id,
        chat_id=7002,
        status_message_id=888,
    )

    mock_bot = AsyncMock()

    async def mock_tts(text, out_path, voice, speed):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake_mp3")
        return out_path

    async def mock_norm(in_audio, out_wav):
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        out_wav.write_bytes(b"fake_wav")
        return out_wav

    async def mock_applio_fail(*args, **kwargs):
        raise RuntimeError("Applio inference GPU/CPU out of memory")

    with (
        patch("app.worker.generate_edge_tts", side_effect=mock_tts),
        patch("app.worker.normalize_audio_to_wav", side_effect=mock_norm),
        patch("app.worker.run_applio_conversion", side_effect=mock_applio_fail),
    ):
        success = await process_single_job(session=db_session, job=job, bot=mock_bot)
        assert success is False

    assert job.status == JobStatus.FAILED.value
    assert "Applio inference GPU/CPU out of memory" in job.error_message

    # Verify failure notice was edited into message
    last_edit = mock_bot.edit_message_text.call_args[1]["text"]
    assert "Applio inference GPU/CPU out of memory" in last_edit


@pytest.mark.asyncio
async def test_concurrent_jobs_progress_isolation():
    """Verify multiple trackers targeting different messages operate independently."""
    bot = AsyncMock()
    tracker_a = JobProgressTracker(bot=bot, chat_id=100, message_id=1, model_name="ModelA")
    tracker_b = JobProgressTracker(bot=bot, chat_id=200, message_id=2, model_name="ModelB")

    await tracker_a.update_step(1)
    await tracker_b.update_step(2)

    calls = bot.edit_message_text.call_args_list
    assert len(calls) == 2
    assert calls[0][1]["chat_id"] == 100
    assert calls[0][1]["message_id"] == 1
    assert "ModelA" in calls[0][1]["text"]

    assert calls[1][1]["chat_id"] == 200
    assert calls[1][1]["message_id"] == 2
    assert "ModelB" in calls[1][1]["text"]


# ---------------------------------------------------------------------------
# Phase D6-L1 Regression Tests — HTML Escaping in render_stepper_progress
# ---------------------------------------------------------------------------

def test_render_stepper_progress_html_escaping_burmese():
    """Regression: model names with HTML special chars must be escaped in Burmese render."""
    unsafe_name = "<Test & Model>"
    text = render_stepper_progress(
        step=1,
        model_name=unsafe_name,
        elapsed_seconds=5,
        spinner="⏳",
        lang="my",
    )
    # Escaped entities must appear
    assert "&lt;Test &amp; Model&gt;" in text
    # Raw unescaped tag and ampersand must NOT appear in model label
    assert "<Test" not in text
    assert "& Model>" not in text
    # Ordinary structure must still be intact
    assert "Voice Studio — အသံဖန်တီးနေသည်" in text
    assert "မော်ဒယ်လ်: <b>&lt;Test &amp; Model&gt;</b>" in text


def test_render_stepper_progress_html_escaping_english():
    """Regression: model names with HTML special chars must be escaped in English render."""
    unsafe_name = "<Bold> & <Italic>"
    text = render_stepper_progress(
        step=2,
        model_name=unsafe_name,
        elapsed_seconds=30,
        spinner="⏳",
        lang="en",
    )
    # Escaped entities must appear
    assert "&lt;Bold&gt; &amp; &lt;Italic&gt;" in text
    # Raw tags must NOT appear in the model label context
    assert "Model: <b><Bold>" not in text
    # Ordinary structure must still be intact
    assert "Voice Studio — Processing" in text
    assert "Model: <b>&lt;Bold&gt; &amp; &lt;Italic&gt;</b>" in text


def test_render_stepper_progress_ordinary_name_unaffected():
    """Regression: ordinary model names without HTML chars must render unchanged."""
    text_my = render_stepper_progress(step=1, model_name="တွတ်ပီ", elapsed_seconds=0, lang="my")
    assert "မော်ဒယ်လ်: <b>တွတ်ပီ</b>" in text_my

    text_en = render_stepper_progress(step=1, model_name="Zawgyi", elapsed_seconds=0, lang="en")
    assert "Model: <b>Zawgyi</b>" in text_en

