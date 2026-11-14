from pathlib import Path
import pytest

from app.models.enums import AccountCategory, VoiceModelStatus, VoiceModelType
from app.models.schema import VoiceModel
from app.services.ledger import get_minute_balance, get_or_create_user


def test_find_model_sample_audio_discovery(tmp_path: Path):
    from app.services.models import find_model_sample_audio

    # Directory with sample.mp3
    d1 = tmp_path / "model1"
    d1.mkdir()
    (d1 / "sample.mp3").write_bytes(b"sample1")
    assert find_model_sample_audio(d1) == d1 / "sample.mp3"

    # Directory with preview.wav
    d2 = tmp_path / "model2"
    d2.mkdir()
    (d2 / "preview.wav").write_bytes(b"preview2")
    assert find_model_sample_audio(d2) == d2 / "preview.wav"

    # Directory with custom_sample.ogg
    d3 = tmp_path / "model3"
    d3.mkdir()
    (d3 / "my_voice_sample.ogg").write_bytes(b"sample3")
    assert find_model_sample_audio(d3) == d3 / "my_voice_sample.ogg"

    # Directory with no audio
    d4 = tmp_path / "model4"
    d4.mkdir()
    (d4 / "model.pth").write_bytes(b"pth")
    assert find_model_sample_audio(d4) is None


@pytest.mark.asyncio
async def test_user_voice_samples_catalog_and_playback(tmp_path: Path, db_session):
    from unittest.mock import AsyncMock, MagicMock
    from aiogram.types import CallbackQuery, Chat, Message, User as TgUser
    from app.handlers.menu import handle_voice_samples_catalog, handle_play_voice_sample

    sample_file = tmp_path / "demo_sample.mp3"
    sample_file.write_bytes(b"audio_bytes")

    m1 = VoiceModel(
        name="SampledVoice",
        slug="sampled-voice",
        type=VoiceModelType.SYSTEM.value,
        status=VoiceModelStatus.ACTIVE.value,
        model_dir=str(tmp_path),
        pth_path=str(tmp_path / "v1.pth"),
        sample_audio_path=str(sample_file),
    )
    m2 = VoiceModel(
        name="NoSampleVoice",
        slug="no-sample-voice",
        type=VoiceModelType.SYSTEM.value,
        status=VoiceModelStatus.ACTIVE.value,
        model_dir=str(tmp_path),
        pth_path=str(tmp_path / "v2.pth"),
        sample_audio_path=None,
    )
    db_session.add_all([m1, m2])
    await db_session.commit()

    # 1. User opens Voice Samples Catalog
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=7788, is_bot=False, first_name="Auditor")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    await handle_voice_samples_catalog(cb, session=db_session)
    cb.message.edit_text.assert_awaited_once()
    assert "Voice Models" in cb.message.edit_text.call_args[0][0] or "Voice Model Samples" in cb.message.edit_text.call_args[0][0]

    # 2. User plays sample for m1 (sample exists)
    cb_play1 = MagicMock(spec=CallbackQuery)
    cb_play1.from_user = TgUser(id=7788, is_bot=False, first_name="Auditor")
    cb_play1.data = f"sample:play:{m1.id}"
    cb_play1.bot = MagicMock()
    cb_play1.bot.send_audio = AsyncMock()
    cb_play1.answer = AsyncMock()

    await handle_play_voice_sample(cb_play1, session=db_session)
    cb_play1.bot.send_audio.assert_awaited_once()
    assert cb_play1.bot.send_audio.call_args[1]["chat_id"] == 7788

    # 3. User plays sample for m2 (sample does NOT exist)
    cb_play2 = MagicMock(spec=CallbackQuery)
    cb_play2.from_user = TgUser(id=7788, is_bot=False, first_name="Auditor")
    cb_play2.data = f"sample:play:{m2.id}"
    cb_play2.bot = MagicMock()
    cb_play2.bot.send_audio = AsyncMock()
    cb_play2.answer = AsyncMock()

    await handle_play_voice_sample(cb_play2, session=db_session)
    cb_play2.bot.send_audio.assert_not_called()
    cb_play2.answer.assert_awaited_once()
    assert cb_play2.answer.call_args[1].get("show_alert") is True




@pytest.mark.asyncio
async def test_user_voice_samples_catalog_and_playback(tmp_path: Path, db_session):
    from aiogram.types import CallbackQuery, Message, User as TgUser
    from unittest.mock import AsyncMock, MagicMock
    from app.handlers.menu import (
        handle_play_voice_sample,
        handle_samples_command,
        handle_voice_samples_catalog,
    )

    user_id = 777001
    await get_or_create_user(db_session, telegram_id=user_id)

    # 1. Create a model with sample file
    sample_file = tmp_path / "preview.mp3"
    sample_file.write_bytes(b"dummy mp3 data")

    model = VoiceModel(
        name="Test Singer",
        slug="test-singer",
        type=VoiceModelType.SYSTEM.value,
        status=VoiceModelStatus.ACTIVE.value,
        model_dir=str(tmp_path),
        pth_path=str(tmp_path / "model.pth"),
        sample_audio_path=str(sample_file),
    )
    db_session.add(model)
    await db_session.commit()
    await db_session.refresh(model)

    # 2. Test user opening voice samples catalog
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=user_id, is_bot=False, first_name="User")
    cb.data = "menu:voice_samples"
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    await handle_voice_samples_catalog(cb, session=db_session)
    cb.message.edit_text.assert_awaited_once()
    call_args = cb.message.edit_text.call_args
    assert "နမူနာအသံများ" in call_args[0][0] or "Voice Samples" in call_args[0][0]

    # 3. Test user /samples command
    cmd_msg = MagicMock(spec=Message)
    cmd_msg.from_user = TgUser(id=user_id, is_bot=False, first_name="User")
    cmd_msg.answer = AsyncMock()

    await handle_samples_command(cmd_msg, session=db_session)
    cmd_msg.answer.assert_awaited_once()

    # 4. Test playing sample audio
    cb_play = MagicMock(spec=CallbackQuery)
    cb_play.from_user = TgUser(id=user_id, is_bot=False, first_name="User")
    cb_play.data = f"sample:play:{model.id}"
    cb_play.bot = MagicMock()
    cb_play.bot.send_audio = AsyncMock()
    cb_play.bot.send_voice = AsyncMock()
    cb_play.answer = AsyncMock()

    await handle_play_voice_sample(cb_play, session=db_session)
    cb_play.bot.send_audio.assert_awaited_once()
    cb_play.answer.assert_awaited_once()




@pytest.mark.asyncio
async def test_user_clone_audio_input_flow(tmp_path: Path, db_session):
    from unittest.mock import AsyncMock, MagicMock
    from aiogram.types import Message, User as TgUser, Voice
    from aiogram.fsm.context import FSMContext
    from app.handlers.clone import handle_clone_audio_input, CloneFSM

    user_id = 888001
    await get_or_create_user(db_session, telegram_id=user_id)
    bal = await get_minute_balance(db_session, user_id)
    bal.welcome_seconds = 300
    await db_session.commit()

    fsm = MagicMock(spec=FSMContext)
    fsm.get_data = AsyncMock(return_value={"lang": "my", "is_admin": False})
    fsm.update_data = AsyncMock()
    fsm.set_state = AsyncMock()

    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=user_id, is_bot=False, first_name="VoiceUser")
    msg.voice = MagicMock(spec=Voice, file_id="voice_input_999", duration=15)
    msg.audio = None
    msg.document = None
    msg.caption = "My speech"
    msg.bot = MagicMock()

    fake_file = tmp_path / "fake_input.ogg"
    fake_file.write_bytes(b"telegram_voice_bytes")
    msg.bot.get_file = AsyncMock(return_value=MagicMock(file_path=str(fake_file)))

    async def fake_download(path, destination):
        Path(destination).write_bytes(b"downloaded_bytes")
    msg.bot.download_file = AsyncMock(side_effect=fake_download)
    msg.answer = AsyncMock()

    await handle_clone_audio_input(msg, state=fsm, session=db_session)

    fsm.update_data.assert_awaited_once()
    saved_data = fsm.update_data.call_args[1]
    assert saved_data["input_type"] == "audio"
    assert "source_audio_path" in saved_data
    fsm.set_state.assert_awaited_once_with(CloneFSM.waiting_for_gender)
    msg.answer.assert_awaited_once()
    assert "အသံကျား/မ" in msg.answer.call_args[0][0] or "Gender" in msg.answer.call_args[0][0]


