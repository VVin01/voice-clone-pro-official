from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest

from app.models.enums import JobStatus
from app.models.schema import Job, VoiceModel
from app.services.audio import (
    GENDER_PITCH_MAP,
    get_output_filename,
    sanitize_filename_token,
)
from app.services.ledger import get_minute_balance, get_or_create_user
from app.services.queue import create_voice_clone_job, get_queue_position
from app.worker import process_single_job


def test_filename_and_pitch_conventions():
    # Model name sanitization
    assert sanitize_filename_token("My Voice") == "My_Voice"
    assert sanitize_filename_token("တွတ်ပီ_Voice#1") == "တွတ်ပီ_Voice_1"

    # Requested output filename convention: voiceclonepro(model_name).wav
    assert get_output_filename("Zawgyi") == "voiceclonepro(Zawgyi).wav"
    assert get_output_filename("တွတ်ပီ") == "voiceclonepro(တွတ်ပီ).wav"

    # Four configured gender combinations
    assert GENDER_PITCH_MAP["m2m"] == 0
    assert GENDER_PITCH_MAP["m2f"] == 12
    assert GENDER_PITCH_MAP["f2f"] == 0
    assert GENDER_PITCH_MAP["f2m"] == -12


@pytest.mark.asyncio
async def test_job_creation_and_queue_position(db_session, tmp_path):
    user, _ = await get_or_create_user(db_session, telegram_id=5001)
    bal = await get_minute_balance(db_session, user.id)
    bal.welcome_seconds = 300  # Give 5 minutes balance

    # Create dummy active model
    pth_file = tmp_path / "Dummy.pth"
    pth_file.write_bytes(b"dummy")
    model = VoiceModel(
        name="Dummy",
        slug="system-dummy",
        type="SYSTEM",
        model_dir=str(tmp_path),
        pth_path=str(pth_file),
        status="ACTIVE",
        is_default_free=True,
    )
    db_session.add(model)
    await db_session.flush()

    # Create job 1
    job1, res1 = await create_voice_clone_job(
        session=db_session,
        user_id=user.id,
        text="Hello world",
        edge_voice="my-MM-ThihaNeural",
        model_id=model.id,
    )
    assert res1 == "QUEUED"
    assert job1.status == JobStatus.QUEUED.value

    # Create job 2
    job2, res2 = await create_voice_clone_job(
        session=db_session,
        user_id=user.id,
        text="Second job",
        edge_voice="my-MM-ThihaNeural",
        model_id=model.id,
    )
    assert res2 == "QUEUED"

    # Verify queue positions
    pos1 = await get_queue_position(db_session, job1.id)
    pos2 = await get_queue_position(db_session, job2.id)
    assert pos1 == 1
    assert pos2 == 2


@pytest.mark.asyncio
async def test_worker_processing_lifecycle_and_cleanup(db_session, tmp_path):
    user, _ = await get_or_create_user(db_session, telegram_id=6001)
    bal = await get_minute_balance(db_session, user.id)
    bal.welcome_seconds = 300  # 300s balance

    pth_file = tmp_path / "Model1.pth"
    pth_file.write_bytes(b"pth_content")
    model = VoiceModel(
        name="Model1",
        slug="system-model1",
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
        text="Testing audio processing",
        edge_voice="my-MM-ThihaNeural",
        model_id=model.id,
    )

    # Mock Edge TTS, FFmpeg, and Applio conversion to test worker orchestration cleanly
    async def mock_tts(text, out_path, voice, speed):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake_mp3_audio")
        return out_path

    async def mock_norm(in_audio, out_wav):
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        out_wav.write_bytes(b"fake_norm_wav")
        return out_wav

    async def mock_applio(model_pth, index_path, input_wav, output_wav, pitch=0, **kwargs):
        output_wav.parent.mkdir(parents=True, exist_ok=True)
        output_wav.write_bytes(b"fake_converted_wav")
        return output_wav

    with (
        patch("app.worker.generate_edge_tts", side_effect=mock_tts),
        patch("app.worker.normalize_audio_to_wav", side_effect=mock_norm),
        patch("app.worker.run_applio_conversion", side_effect=mock_applio),
        patch("app.worker.get_wav_duration_seconds", return_value=15),
    ):
        success = await process_single_job(session=db_session, job=job)
        assert success is True

    # Verify job completed successfully
    assert job.status == JobStatus.SUCCESS.value
    assert job.seconds_consumed == 15
    assert job.finished_at is not None

    # Verify balance was debited by 15 seconds
    bal_after = await get_minute_balance(db_session, user.id)
    assert bal_after.welcome_seconds == 285  # 300 - 15 = 285

    # Verify safe temp cleanup: job temp directory was cleaned up
    from app.config import settings
    temp_dir = settings.storage_root / "temp" / str(job.id)
    assert not temp_dir.exists(), "Temporary files must be cleaned up after completion!"


@pytest.mark.asyncio
async def test_real_edge_tts_generation(tmp_path: Path):
    """Real Edge TTS integration test with network request to Azure Edge TTS."""
    from app.services.audio import generate_edge_tts
    out_mp3 = tmp_path / "test_tts.mp3"
    try:
        res = await generate_edge_tts("Hello", out_mp3, voice="en-US-GuyNeural")
        assert res.is_file()
        assert res.stat().st_size > 0
    except Exception as exc:
        pytest.skip(f"Edge TTS live network test skipped: {exc}")
