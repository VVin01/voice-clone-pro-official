"""Voice v2: VoxCPM2 only, Groq STT first, no legacy trained-model engine."""

import pytest

from app.config import settings
from app.models.schema import VoiceModel
from app.services.ledger import get_minute_balance, get_or_create_user
from app.services.queue import create_voice_clone_job


def test_no_legacy_engine_settings():
    assert not hasattr(settings, "legacy_workdir")
    assert not hasattr(settings, "legacy_python")
    assert not hasattr(settings, "legacy_runner")
    assert settings.voxcpm_timeout == 60
    assert settings.groq_whisper_model == "whisper-large-v3-turbo"


def test_voice_clone_bridge_legacy_raises():
    from app.services.recap_engine.voice_clone_bridge import (
        convert_voice_rvc,
        get_available_clone_models,
        resolve_clone_model,
    )

    assert get_available_clone_models() == {}
    assert resolve_clone_model("clone_sys_test") is None
    with pytest.raises(RuntimeError, match="no longer supported"):
        convert_voice_rvc("in.wav", "out.wav", "clone_sys_test")


@pytest.mark.asyncio
async def test_queue_rejects_legacy_model(db_session, tmp_path):
    user, _ = await get_or_create_user(db_session, telegram_id=7001)
    bal = await get_minute_balance(db_session, user.id)
    bal.welcome_seconds = 300
    pth = tmp_path / "Legacy.pth"
    pth.write_bytes(b"legacy")
    model = VoiceModel(
        name="Legacy", slug="system-legacy", type="SYSTEM",
        model_dir=str(tmp_path), pth_path=str(pth),
        status="ACTIVE", is_default_free=True,
    )
    db_session.add(model)
    await db_session.flush()
    job, reason = await create_voice_clone_job(
        session=db_session, user_id=user.id, text="Hello",
        edge_voice="my-MM-ThihaNeural", model_id=model.id, engine="legacy",
    )
    assert job is None
    assert reason == "LEGACY_MODEL_ENGINE_REMOVED"


@pytest.mark.asyncio
async def test_queue_voxcpm_model_queues(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=7002)
    bal = await get_minute_balance(db_session, user.id)
    bal.welcome_seconds = 300
    model = VoiceModel(
        name="VoxCPM (Cloud AI)", slug="voxcpm-cloud-ai", type="SYSTEM",
        model_dir="cloud", pth_path="voxcpm",
        status="ACTIVE", is_default_free=True,
    )
    db_session.add(model)
    await db_session.flush()
    job, reason = await create_voice_clone_job(
        session=db_session, user_id=user.id, text="Hello world",
        edge_voice="my-MM-ThihaNeural", model_id=model.id, engine="voxcpm",
    )
    assert reason == "QUEUED"
    assert job is not None
    assert job.engine == "voxcpm"
