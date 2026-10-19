from pathlib import Path
import pytest
from sqlalchemy import select

from app.models.enums import AccountCategory, VoiceModelStatus, VoiceModelType
from app.models.schema import AuditLog, VoiceModel
from app.services.ledger import credit_purchased_minutes, get_minute_balance, get_or_create_user
from app.services.models import (
    _artifact_pairs,
    activate_model,
    disable_model,
    get_authorized_model,
    get_available_models_for_user,
    scan_root,
    set_free_default_model,
)


def test_artifact_pairs_matching_logic(tmp_path: Path):
    root = tmp_path / "models"
    root.mkdir()

    # Case 1: same stem pairing
    folder1 = root / "VoiceA"
    folder1.mkdir()
    pth1 = folder1 / "VoiceA.pth"
    idx1 = folder1 / "VoiceA.index"
    pth1.write_bytes(b"pth1")
    idx1.write_bytes(b"idx1")

    # Case 2: different stem, but only one index in folder
    folder2 = root / "တွတ်ပီ"
    folder2.mkdir()
    pth2 = folder2 / "cartoon_100e.pth"
    idx2 = folder2 = folder2 / "trained.index"
    pth2.write_bytes(b"pth2")
    idx2.write_bytes(b"idx2")

    # Case 3: model without index
    folder3 = root / "VoiceC"
    folder3.mkdir()
    pth3 = folder3 / "VoiceC.pth"
    pth3.write_bytes(b"pth3")

    pairs = _artifact_pairs(root)
    assert len(pairs) == 3

    pair_dict = {p.name: idx.name if idx else None for p, idx in pairs}
    assert pair_dict["VoiceA.pth"] == "VoiceA.index"
    assert pair_dict["cartoon_100e.pth"] == "trained.index"
    assert pair_dict["VoiceC.pth"] is None


@pytest.mark.asyncio
async def test_scan_and_admin_review_workflow(tmp_path: Path, db_session):
    root = tmp_path / "system_models"
    root.mkdir()

    # Create 2 model folders
    m1_dir = root / "Zawgyi"
    m1_dir.mkdir()
    (m1_dir / "Zawgyi.pth").write_bytes(b"model1")
    (m1_dir / "Zawgyi.index").write_bytes(b"index1")

    m2_dir = root / "Nilar"
    m2_dir.mkdir()
    (m2_dir / "Nilar.pth").write_bytes(b"model2")

    # 1. Run local scan
    scanned = await scan_root(db_session, root, VoiceModelType.SYSTEM)
    assert len(scanned) == 2

    # Verify models are READY, NOT automatically ACTIVE!
    for m in scanned:
        assert m.status == VoiceModelStatus.READY.value
        assert m.is_default_free is False

    zawgyi = next(m for m in scanned if m.name == "Zawgyi")
    nilar = next(m for m in scanned if m.name == "Nilar")

    # 2. Admin reviews and activates Zawgyi
    ok, status = await activate_model(db_session, zawgyi.id, admin_id=999)
    assert ok is True
    assert zawgyi.status == VoiceModelStatus.ACTIVE.value

    # Verify audit log recorded
    logs = (await db_session.execute(
        select(AuditLog).where(AuditLog.target_id == str(zawgyi.id))
    )).scalars().all()
    assert len(logs) == 1
    assert logs[0].action == "voice_model.activate"

    # 3. Admin activates Nilar and sets Nilar as Free Default
    await activate_model(db_session, nilar.id, admin_id=999)
    ok_free, _ = await set_free_default_model(db_session, nilar.id, admin_id=999)
    assert ok_free is True
    assert nilar.is_default_free is True

    # 4. Check model catalog visibility for FREE user vs PURCHASED user (P1-1 alignment)
    free_user, _ = await get_or_create_user(db_session, telegram_id=111)
    purchased_user, _ = await get_or_create_user(db_session, telegram_id=222)
    await credit_purchased_minutes(db_session, purchased_user.id, minutes=5)

    # Free user sees all active admin-approved models
    free_models = await get_available_models_for_user(db_session, free_user.id)
    assert len(free_models) == 2
    assert {m.name for m in free_models} == {"Nilar", "Zawgyi"}

    # Purchased user also sees all active models identically
    purchased_models = await get_available_models_for_user(db_session, purchased_user.id)
    assert len(purchased_models) == 2
    assert {m.name for m in purchased_models} == {"Nilar", "Zawgyi"}

    # Authorized model check (both authorized for active system models)
    assert (await get_authorized_model(db_session, free_user.id, zawgyi.id)) is not None
    assert (await get_authorized_model(db_session, free_user.id, nilar.id)) is not None
    assert (await get_authorized_model(db_session, purchased_user.id, zawgyi.id)) is not None

    # 5. Admin disables Zawgyi
    await disable_model(db_session, zawgyi.id, admin_id=999)
    assert zawgyi.status == VoiceModelStatus.DISABLED.value
    # Disabled model no longer visible to anyone
    p_models_after = await get_available_models_for_user(db_session, purchased_user.id)
    assert len(p_models_after) == 1
    assert p_models_after[0].name == "Nilar"

    # 6. Physical file removal triggers BROKEN status on re-scan
    (nilar_dir := root / "Nilar" / "Nilar.pth").unlink()
    rescan = await scan_root(db_session, root, VoiceModelType.SYSTEM)
    assert nilar.status == VoiceModelStatus.BROKEN.value
    assert nilar.is_default_free is False


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
async def test_scan_discovers_sample_audio(tmp_path: Path, db_session):
    root = tmp_path / "sampled_models"
    root.mkdir()

    m_dir = root / "Kartoon"
    m_dir.mkdir()
    (m_dir / "kartoon.pth").write_bytes(b"pth_bytes")
    (m_dir / "sample.mp3").write_bytes(b"sample_bytes")

    scanned = await scan_root(db_session, root, VoiceModelType.SYSTEM)
    assert len(scanned) == 1
    m = scanned[0]
    assert m.sample_audio_path == str(m_dir / "sample.mp3")


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
async def test_admin_model_sample_management_flow(tmp_path: Path, db_session, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from aiogram.fsm.context import FSMContext
    from aiogram.types import CallbackQuery, Chat, Message, User as TgUser, Voice
    from app.config import settings
    from app.handlers.admin import (
        handle_admin_model_sample_play,
        handle_admin_model_sample_upload_prompt,
        handle_admin_model_sample_received,
        handle_admin_model_sample_delete,
    )

    admin_id = 999111
    monkeypatch.setattr(settings, "admin_ids_raw", str(admin_id))

    model_dir = tmp_path / "AdminTestModel"
    model_dir.mkdir()
    sample_file = model_dir / "sample.mp3"
    sample_file.write_bytes(b"test_sample_data")

    model = VoiceModel(
        name="AdminVoice",
        slug="admin-voice",
        type=VoiceModelType.SYSTEM.value,
        status=VoiceModelStatus.ACTIVE.value,
        model_dir=str(model_dir),
        pth_path=str(model_dir / "m.pth"),
        sample_audio_path=str(sample_file),
    )
    db_session.add(model)
    await db_session.commit()

    # 1. Admin plays sample
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb.data = f"admin:mod_sample_play:{model.id}"
    cb.bot = MagicMock()
    cb.bot.send_audio = AsyncMock()
    cb.answer = AsyncMock()

    await handle_admin_model_sample_play(cb, session=db_session)
    cb.bot.send_audio.assert_awaited_once()

    # 2. Admin prompts upload
    cb_up = MagicMock(spec=CallbackQuery)
    cb_up.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb_up.data = f"admin:mod_sample_up:{model.id}"
    cb_up.message = MagicMock(spec=Message)
    cb_up.message.edit_text = AsyncMock()
    cb_up.answer = AsyncMock()
    fsm = AsyncMock(spec=FSMContext)

    await handle_admin_model_sample_upload_prompt(cb_up, state=fsm, session=db_session)
    fsm.set_state.assert_awaited_once()
    fsm.update_data.assert_awaited_once_with(model_id=model.id)

    # 3. Admin sends new voice message
    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    msg.voice = MagicMock(spec=Voice, file_id="voice_file_123")
    msg.audio = None
    msg.document = None
    msg.bot = MagicMock()
    tg_file = MagicMock(file_path="voice/voice_123.oga")
    msg.bot.get_file = AsyncMock(return_value=tg_file)
    msg.bot.download_file = AsyncMock()
    msg.answer = AsyncMock()

    fsm.get_data = AsyncMock(return_value={"model_id": model.id})
    fsm.clear = AsyncMock()

    await handle_admin_model_sample_received(msg, state=fsm, session=db_session)
    msg.bot.download_file.assert_awaited_once()
    msg.answer.assert_awaited_once()
    assert "အောင်မြင်စွာ" in msg.answer.call_args[0][0] or "Successfully" in msg.answer.call_args[0][0]

    # Verify model in DB updated
    updated_model = await db_session.get(VoiceModel, model.id)
    assert updated_model.sample_audio_path is not None
    assert updated_model.sample_audio_path.endswith(".ogg")

    # 4. Admin deletes sample
    cb_del = MagicMock(spec=CallbackQuery)
    cb_del.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb_del.data = f"admin:mod_sample_del:{model.id}"
    cb_del.message = MagicMock(spec=Message)
    cb_del.message.edit_text = AsyncMock()
    cb_del.answer = AsyncMock()

    await handle_admin_model_sample_delete(cb_del, session=db_session)
    cb_del.answer.assert_awaited_once()
    re_updated = await db_session.get(VoiceModel, model.id)
    assert re_updated.sample_audio_path is None


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
async def test_admin_samples_mgr_and_sample_menu(tmp_path: Path, db_session):
    from unittest.mock import AsyncMock, MagicMock
    from aiogram.types import CallbackQuery, Message, User as TgUser
    from app.config import settings
    from app.handlers.admin import handle_admin_samples_mgr, handle_admin_model_sample_menu

    admin_id = 999901
    settings.admin_ids_raw = str(admin_id)
    await get_or_create_user(db_session, telegram_id=admin_id)

    model = VoiceModel(
        name="Singer1",
        slug="singer-1",
        type=VoiceModelType.SYSTEM.value,
        status=VoiceModelStatus.ACTIVE.value,
        model_dir=str(tmp_path),
        pth_path=str(tmp_path / "m.pth"),
    )
    db_session.add(model)
    await db_session.commit()
    await db_session.refresh(model)

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb.data = "admin:samples_mgr"
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    await handle_admin_samples_mgr(cb, session=db_session)
    cb.message.edit_text.assert_awaited_once()
    assert "Voice Model Samples" in cb.message.edit_text.call_args[0][0]

    cb_menu = MagicMock(spec=CallbackQuery)
    cb_menu.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb_menu.data = f"admin:mod_sample_menu:{model.id}"
    cb_menu.message = MagicMock(spec=Message)
    cb_menu.message.edit_text = AsyncMock()
    cb_menu.answer = AsyncMock()

    await handle_admin_model_sample_menu(cb_menu, session=db_session)
    cb_menu.message.edit_text.assert_awaited_once()
    assert "Voice Sample" in cb_menu.message.edit_text.call_args[0][0]


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


