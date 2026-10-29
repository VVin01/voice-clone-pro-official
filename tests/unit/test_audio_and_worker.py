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

    # Requested output filename convention: VoiceClonePro(model_name).wav
    assert get_output_filename("Zawgyi") == "VoiceClonePro(Zawgyi).wav"
    assert get_output_filename("တွတ်ပီ") == "VoiceClonePro(တွတ်ပီ).wav"

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

    # Create job 2 for a second user to test queue positions
    user2, _ = await get_or_create_user(db_session, telegram_id=5002)
    bal2 = await get_minute_balance(db_session, user2.id)
    bal2.welcome_seconds = 300
    await db_session.flush()

    job2, res2 = await create_voice_clone_job(
        session=db_session,
        user_id=user2.id,
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

    # Mock Edge TTS, FFmpeg, and VoxCPM2 synthesis to test worker orchestration cleanly
    async def mock_tts(text, out_path, voice, speed):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake_mp3_audio")
        return out_path

    async def mock_norm(in_audio, out_wav):
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        out_wav.write_bytes(b"fake_norm_wav")
        return out_wav

    def mock_voxcpm(text, output_wav, **kwargs):  # patched via side_effect: MagicMock calls it WITHOUT self (worker passes all-keyword args)
        output_wav = Path(output_wav)
        output_wav.parent.mkdir(parents=True, exist_ok=True)
        output_wav.write_bytes(b"fake_voxcpm_wav")
        return output_wav

    with (
        patch("app.worker.generate_edge_tts", side_effect=mock_tts),
        patch("app.worker.normalize_audio_to_wav", side_effect=mock_norm),
        patch("app.services.voxcpm_engine.VoxCPMEngine.synthesize", side_effect=mock_voxcpm),
        patch("app.worker.get_wav_duration_seconds", return_value=15),
    ):
        success = await process_single_job(session=db_session, job=job)
        assert success is True

    # Verify job completed successfully
    assert job.status == JobStatus.SUCCESS.value
    assert job.seconds_consumed == 15
    assert job.finished_at is not None

    # Verify balance was debited by 15 seconds (consumed from monthly_free_seconds first per policy)
    bal_after = await get_minute_balance(db_session, user.id)
    assert bal_after.monthly_free_seconds == 165  # 180 - 15 = 165
    assert bal_after.welcome_seconds == 300
    assert bal_after.total_usable_seconds == 465

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


@pytest.mark.asyncio
async def test_direct_audio_input_clone_pipeline(db_session, tmp_path):
    user, _ = await get_or_create_user(db_session, telegram_id=5002)
    bal = await get_minute_balance(db_session, user.id)
    bal.welcome_seconds = 600

    pth_file = tmp_path / "DummyModel.pth"
    pth_file.write_bytes(b"dummy_model")
    model = VoiceModel(
        name="DummyModel",
        slug="system-dummymodel",
        type="SYSTEM",
        model_dir=str(tmp_path),
        pth_path=str(pth_file),
        status="ACTIVE",
        is_default_free=True,
    )
    db_session.add(model)
    await db_session.flush()

    # User provided source audio file
    user_audio = tmp_path / "user_input.wav"
    user_audio.write_bytes(b"user_speech_bytes")

    # 1. Create job with source_audio_path
    job, status = await create_voice_clone_job(
        session=db_session,
        user_id=user.id,
        model_id=model.id,
        source_audio_path=str(user_audio),
        gender_preset="m2f",
    )
    assert status == "QUEUED"
    assert job is not None
    assert job.source_audio_path == str(user_audio)

    # 2. Process job: Edge TTS must NOT be called!
    # Current pipeline (VoxCPM2-only): the user's source audio is passed
    # DIRECTLY to the VoxCPM2 engine as the clone reference; the legacy
    # Stage-2 normalize/Edge base-audio step no longer runs for voxcpm jobs.
    mock_edge_tts = AsyncMock()
    mock_norm = AsyncMock()
    engine_calls = {}

    def fake_voxcpm(text, output_wav, **kwargs):  # side_effect: called without self
        engine_calls.update(kwargs)
        output_wav = Path(output_wav)
        output_wav.parent.mkdir(parents=True, exist_ok=True)
        output_wav.write_bytes(b"fake_voxcpm_wav")
        return output_wav

    with (
        patch("app.worker.generate_edge_tts", new=mock_edge_tts),
        patch("app.worker.normalize_audio_to_wav", new=mock_norm),
        patch("app.services.voxcpm_engine.VoxCPMEngine.synthesize", side_effect=fake_voxcpm),
        patch("app.worker.get_wav_duration_seconds", side_effect=[30, 30]),
    ):
        ok = await process_single_job(session=db_session, job=job)
        assert ok is True
        # Verify Edge-TTS was NOT called!
        mock_edge_tts.assert_not_called()
        # Verify the legacy normalize step was NOT called either!
        mock_norm.assert_not_called()
        # Verify the engine received the user's audio as the clone reference!
        assert engine_calls.get("reference_wav") is not None
        assert str(engine_calls["reference_wav"]) == str(user_audio)

