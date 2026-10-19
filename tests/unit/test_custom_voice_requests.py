import pytest
from sqlalchemy import select
from unittest.mock import AsyncMock, MagicMock

from app.models.enums import AccountCategory, CustomVoiceRequestStatus, VoiceModelStatus, VoiceModelType
from app.models.schema import AuditLog, CustomVoiceRequest, User, UserVoiceModelGrant, VoiceModel
from app.services.custom_voice import (
    assign_model_to_request,
    create_custom_voice_request,
    get_custom_voice_request_by_id,
    get_custom_voice_requests,
    get_user_custom_voice_quota,
    grant_user_model_access,
    reject_custom_voice_request,
    revoke_user_model_access,
)
from app.services.ledger import get_or_create_user
from app.services.models import get_available_models_for_user
from app.handlers.admin import (
    handle_admin_cvr_detail,
    handle_admin_cvr_do_assign,
    handle_admin_cvr_do_reject,
    handle_admin_cvr_list,
)


@pytest.mark.asyncio
async def test_user_quota_free_vs_purchased(db_session):
    # Free user
    free_user, _ = await get_or_create_user(db_session, telegram_id=8801)
    quota_free = await get_user_custom_voice_quota(db_session, free_user.id)
    assert quota_free["is_purchased"] is False
    assert quota_free["is_admin"] is False
    assert quota_free["can_request"] is False
    assert quota_free["remaining_slots"] == 2

    # Purchased user
    purchased_user, _ = await get_or_create_user(db_session, telegram_id=8802)
    purchased_user.account_category = AccountCategory.PURCHASED.value
    await db_session.commit()

    quota_purchased = await get_user_custom_voice_quota(db_session, purchased_user.id)
    assert quota_purchased["is_purchased"] is True
    assert quota_purchased["can_request"] is True
    assert quota_purchased["max_allowed"] == 2
    assert quota_purchased["slots_used"] == 0
    assert quota_purchased["remaining_slots"] == 2


@pytest.mark.asyncio
async def test_free_user_cannot_create_request(db_session):
    free_user, _ = await get_or_create_user(db_session, telegram_id=8803)
    ok, req, err = await create_custom_voice_request(
        session=db_session,
        user_id=free_user.id,
        model_name="Nay Toe Voice",
        sample_file_id="audio_sample_001",
    )
    assert ok is False
    assert req is None
    assert err == "PURCHASED_TIER_REQUIRED"


@pytest.mark.asyncio
async def test_purchased_user_quota_enforcement(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=8804)
    user.account_category = AccountCategory.PURCHASED.value
    await db_session.commit()

    # 1. Create request 1
    ok1, req1, err1 = await create_custom_voice_request(
        session=db_session,
        user_id=user.id,
        model_name="Nay Toe",
        sample_file_id="sample_file_1",
        sample_file_name="nay_toe.mp3",
        sample_duration_seconds=60,
    )
    assert ok1 is True
    assert req1 is not None
    assert req1.status == CustomVoiceRequestStatus.PENDING.value
    assert req1.id.startswith("CVR-")

    q1 = await get_user_custom_voice_quota(db_session, user.id)
    assert q1["slots_used"] == 1
    assert q1["remaining_slots"] == 1
    assert q1["can_request"] is True

    # 2. Create request 2
    ok2, req2, err2 = await create_custom_voice_request(
        session=db_session,
        user_id=user.id,
        model_name="Rose",
        sample_file_id="sample_file_2",
        sample_file_name="rose.ogg",
        sample_duration_seconds=120,
    )
    assert ok2 is True
    assert req2 is not None

    q2 = await get_user_custom_voice_quota(db_session, user.id)
    assert q2["slots_used"] == 2
    assert q2["remaining_slots"] == 0
    assert q2["can_request"] is False

    # 3. Request 3 should be rejected due to quota limit
    ok3, req3, err3 = await create_custom_voice_request(
        session=db_session,
        user_id=user.id,
        model_name="Iron Man",
        sample_file_id="sample_file_3",
    )
    assert ok3 is False
    assert req3 is None
    assert err3 == "QUOTA_LIMIT_REACHED"


@pytest.mark.asyncio
async def test_admin_assign_existing_model_and_reusability(db_session):
    # Setup 2 purchased users
    u1, _ = await get_or_create_user(db_session, telegram_id=8805)
    u1.account_category = AccountCategory.PURCHASED.value
    u2, _ = await get_or_create_user(db_session, telegram_id=8806)
    u2.account_category = AccountCategory.PURCHASED.value

    # Create VoiceModel in DB (e.g. Nay Toe trained on Colab)
    model = VoiceModel(
        id=7701,
        name="Nay Toe Official",
        slug="nay-toe-official",
        model_dir="storage/models/naytoe",
        pth_path="storage/models/naytoe.pth",
        index_path="storage/models/naytoe.index",
        type=VoiceModelType.SYSTEM.value,
        status=VoiceModelStatus.ACTIVE.value,
        is_default_free=False,
    )
    db_session.add(model)
    await db_session.commit()

    # User 1 requests Nay Toe
    _, req1, _ = await create_custom_voice_request(
        session=db_session,
        user_id=u1.id,
        model_name="Nay Toe",
        sample_file_id="u1_sample",
    )
    assert req1 is not None

    # Admin assigns existing model 7701 to User 1
    ok, approved_req1, msg = await assign_model_to_request(
        session=db_session,
        request_id=req1.id,
        model_id=model.id,
        admin_id=999,
        notes="Model assigned from Colab training",
    )
    assert ok is True
    assert approved_req1.status == CustomVoiceRequestStatus.APPROVED.value
    assert approved_req1.assigned_model_id == model.id

    # Check User 1 can now see Nay Toe in their available models
    u1_models = await get_available_models_for_user(db_session, u1.id)
    assert any(m.id == model.id for m in u1_models)

    # User 2 also requests Nay Toe
    _, req2, _ = await create_custom_voice_request(
        session=db_session,
        user_id=u2.id,
        model_name="Nay Toe (Myanmar actor)",
        sample_file_id="u2_sample",
    )
    assert req2 is not None

    # Admin assigns the SAME existing model to User 2 without retraining!
    ok2, approved_req2, msg2 = await assign_model_to_request(
        session=db_session,
        request_id=req2.id,
        model_id=model.id,
        admin_id=999,
        notes="Reused existing Nay Toe model",
    )
    assert ok2 is True
    assert approved_req2.status == CustomVoiceRequestStatus.APPROVED.value

    # Check User 2 also has access to the same model
    u2_models = await get_available_models_for_user(db_session, u2.id)
    assert any(m.id == model.id for m in u2_models)

    # Verify grants in DB
    grants = (await db_session.execute(
        select(UserVoiceModelGrant).where(UserVoiceModelGrant.model_id == model.id)
    )).scalars().all()
    assert len(grants) == 2
    assert {g.user_id for g in grants} == {u1.id, u2.id}


@pytest.mark.asyncio
async def test_admin_reject_frees_user_slot(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=8807)
    user.account_category = AccountCategory.PURCHASED.value
    await db_session.commit()

    # Fill both quota slots
    _, req1, _ = await create_custom_voice_request(db_session, user.id, "Model A", "file_a")
    _, req2, _ = await create_custom_voice_request(db_session, user.id, "Model B", "file_b")

    q_full = await get_user_custom_voice_quota(db_session, user.id)
    assert q_full["slots_used"] == 2
    assert q_full["can_request"] is False

    # Admin rejects req1
    ok, rej_req, msg = await reject_custom_voice_request(
        session=db_session,
        request_id=req1.id,
        admin_id=999,
        reason="Audio sample was noisy",
    )
    assert ok is True
    assert rej_req.status == CustomVoiceRequestStatus.REJECTED.value
    assert rej_req.admin_notes == "Audio sample was noisy"

    # User slot is now freed
    q_after = await get_user_custom_voice_quota(db_session, user.id)
    assert q_after["slots_used"] == 1
    assert q_after["remaining_slots"] == 1
    assert q_after["can_request"] is True

    # User can now request Model C
    ok_c, req_c, _ = await create_custom_voice_request(db_session, user.id, "Model C", "file_c")
    assert ok_c is True
    assert req_c is not None


@pytest.mark.asyncio
async def test_admin_cvr_callbacks_workflow(db_session):
    from app.config import settings
    from aiogram.types import User as TgUser

    admin_id = 888111
    settings.admin_ids_raw = str(admin_id)
    admin, _ = await get_or_create_user(db_session, telegram_id=admin_id)

    # Purchased user submits request
    u, _ = await get_or_create_user(db_session, telegram_id=8808)
    u.account_category = AccountCategory.PURCHASED.value
    _, req, _ = await create_custom_voice_request(db_session, u.id, "Test Model", "sample_id_xyz", "sample.mp3", 45)
    await db_session.commit()

    # 1. Admin lists CVRs
    cb_list = MagicMock()
    cb_list.from_user = TgUser(id=admin_id, is_bot=False, first_name="BossAdmin")
    cb_list.data = "admin:cvr"
    cb_list.message = MagicMock()
    cb_list.message.edit_text = AsyncMock()
    cb_list.answer = AsyncMock()

    await handle_admin_cvr_list(cb_list, session=db_session)
    cb_list.message.edit_text.assert_called_once()
    assert "Custom Voice Requests" in cb_list.message.edit_text.call_args[0][0]

    # 2. Admin views CVR detail
    cb_detail = MagicMock()
    cb_detail.from_user = TgUser(id=admin_id, is_bot=False, first_name="BossAdmin")
    cb_detail.data = f"admin:cvr:detail:{req.id}"
    cb_detail.message = MagicMock()
    cb_detail.message.edit_text = AsyncMock()
    cb_detail.answer = AsyncMock()

    await handle_admin_cvr_detail(cb_detail, session=db_session)
    cb_detail.message.edit_text.assert_called_once()
    assert req.id in cb_detail.message.edit_text.call_args[0][0]

    # 3. Create a model in DB and admin assigns it
    m = VoiceModel(
        id=7702,
        name="Test Voice Output",
        slug="test-voice-output",
        model_dir="storage/models/test",
        pth_path="storage/models/test.pth",
        type=VoiceModelType.SYSTEM.value,
        status=VoiceModelStatus.ACTIVE.value,
    )
    db_session.add(m)
    await db_session.commit()

    cb_assign = MagicMock()
    cb_assign.from_user = TgUser(id=admin_id, is_bot=False, first_name="BossAdmin")
    cb_assign.data = f"admin:cvr:do_assign:{req.id}:{m.id}"
    cb_assign.message = MagicMock()
    cb_assign.message.edit_text = AsyncMock()
    cb_assign.answer = AsyncMock()
    cb_assign.bot.send_message = AsyncMock()

    await handle_admin_cvr_do_assign(cb_assign, session=db_session)
    assert cb_assign.bot.send_message.called
    assert cb_assign.answer.called

    # Verify request is approved
    refreshed_req = await get_custom_voice_request_by_id(db_session, req.id)
    assert refreshed_req.status == CustomVoiceRequestStatus.APPROVED.value
    assert refreshed_req.assigned_model_id == m.id


@pytest.mark.asyncio
async def test_user_custom_voice_menu_callbacks(db_session):
    from aiogram.types import User as TgUser, Message as TgMsg
    from app.handlers.custom_voice import handle_custom_voice_menu, handle_cvr_request_start, handle_cvr_cancel

    # 1. Free user opens custom voice menu -> sees VIP promo
    free_u, _ = await get_or_create_user(db_session, telegram_id=8901)
    cb_free = MagicMock()
    cb_free.from_user = TgUser(id=free_u.id, is_bot=False, first_name="FreeUser")
    cb_free.message = MagicMock(spec=TgMsg)
    cb_free.message.edit_text = AsyncMock()
    cb_free.answer = AsyncMock()

    await handle_custom_voice_menu(cb_free, session=db_session)
    cb_free.message.edit_text.assert_called_once()
    assert "VIP" in cb_free.message.edit_text.call_args[0][0]

    # 2. Purchased user opens custom voice menu -> sees Custom Voice Hub
    purchased_u, _ = await get_or_create_user(db_session, telegram_id=8902)
    purchased_u.account_category = AccountCategory.PURCHASED.value
    await db_session.commit()

    cb_purchased = MagicMock()
    cb_purchased.from_user = TgUser(id=purchased_u.id, is_bot=False, first_name="VIPUser")
    cb_purchased.message = MagicMock(spec=TgMsg)
    cb_purchased.message.edit_text = AsyncMock()
    cb_purchased.answer = AsyncMock()

    await handle_custom_voice_menu(cb_purchased, session=db_session)
    cb_purchased.message.edit_text.assert_called_once()
    assert "Custom Voice Model" in cb_purchased.message.edit_text.call_args[0][0] or "စိတ်ကြိုက် Voice" in cb_purchased.message.edit_text.call_args[0][0]

    # 3. User clicks request start -> prompts for model name
    state = AsyncMock()
    cb_start = MagicMock()
    cb_start.from_user = TgUser(id=purchased_u.id, is_bot=False, first_name="VIPUser")
    cb_start.message = MagicMock(spec=TgMsg)
    cb_start.message.edit_text = AsyncMock()
    cb_start.answer = AsyncMock()

    await handle_cvr_request_start(cb_start, state=state, session=db_session)
    cb_start.message.edit_text.assert_called_once()
    assert state.set_state.called

    # 4. User cancels -> returns to hub
    cb_cancel = MagicMock()
    cb_cancel.from_user = TgUser(id=purchased_u.id, is_bot=False, first_name="VIPUser")
    cb_cancel.message = MagicMock(spec=TgMsg)
    cb_cancel.message.edit_text = AsyncMock()
    cb_cancel.answer = AsyncMock()

    await handle_cvr_cancel(cb_cancel, state=state, session=db_session)
    assert state.clear.called

