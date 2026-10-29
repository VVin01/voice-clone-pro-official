from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from datetime import datetime, timezone
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update, User as TgUser
from sqlalchemy import select

from app.config import settings
from app.handlers import setup_handlers
from app.handlers.admin import (
    AdminAdjustFSM,
    AdminBroadcastFSM,
    AdminUserSearchFSM,
    handle_admin_adjust_amount_input,
    handle_admin_adjust_cancel,
    handle_admin_adjust_confirm,
    handle_admin_adjust_reason_input,
    handle_admin_adjust_start,
    handle_admin_broadcast_cancel,
    handle_admin_broadcast_confirm,
    handle_admin_broadcast_group_select,
    handle_admin_broadcast_message_input,
    handle_admin_broadcast_start,
    handle_admin_command,
    handle_admin_dashboard,
    handle_admin_user_search_cancel,
    handle_admin_user_search_query,
    handle_admin_user_search_start,
)
from app.handlers.clone import (
    CloneFSM,
    handle_clone_gender_selected,
    handle_clone_model_selected,
    handle_clone_speed_selected,
    handle_clone_text_input,
    handle_clone_voice_selected,
    handle_voice_clone_start,
)
from app.middlewares import AccessControlMiddleware, setup_middlewares
from app.models.enums import AccountCategory, JobStatus, UserStatus, VoiceModelStatus, VoiceModelType
from app.models.schema import (
    AuditLog,
    BroadcastCampaign,
    Job,
    MinuteBalance,
    MinuteLedgerEvent,
    User,
    VoiceModel,
)
from app.services.ledger import get_minute_balance, get_or_create_user


class FakeFSMContext:
    """In-memory replica of aiogram FSMContext for deterministic state tracking."""

    def __init__(self, initial_state=None, initial_data=None):
        self.current_state = initial_state
        self.data = initial_data or {}

    async def set_state(self, state):
        self.current_state = state

    async def get_state(self):
        return self.current_state

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def clear(self):
        self.current_state = None
        self.data = {}


def create_mock_session_maker(session):
    sm = MagicMock()
    sm.return_value.__aenter__ = AsyncMock(return_value=session)
    sm.return_value.__aexit__ = AsyncMock(return_value=None)
    return sm


def make_message_update(user_id: int, text: str, update_id: int = 1) -> Update:
    chat = Chat(id=user_id, type="private")
    user = TgUser(id=user_id, is_bot=False, first_name="TestUser")
    msg = Message(
        message_id=update_id,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        text=text,
    )
    return Update(update_id=update_id, message=msg)


def make_callback_update(user_id: int, callback_data: str, update_id: int = 1) -> Update:
    chat = Chat(id=user_id, type="private")
    user = TgUser(id=user_id, is_bot=False, first_name="TestUser")
    msg = Message(
        message_id=update_id,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        text="Prompt",
    )
    cb = CallbackQuery(
        id=f"cb_{update_id}",
        from_user=user,
        chat_instance=f"inst_{user_id}",
        message=msg,
        data=callback_data,
    )
    return Update(update_id=update_id, callback_query=cb)



# ===========================================================================
# P0 — BLOCKED USERS MUST NEVER GENERATE VOICE
# ===========================================================================


@pytest.mark.asyncio
async def test_blocked_user_cannot_initiate_voice_clone(db_session):
    """Entry point rejection: BLOCKED user clicking menu:voice_clone is intercepted and dropped."""
    blocked_id = 10001
    user = User(id=blocked_id, username="blocked_entry", status=UserStatus.BLOCKED.value, language_code="my")
    db_session.add(user)
    await db_session.commit()

    sm = create_mock_session_maker(db_session)
    middleware = AccessControlMiddleware(session_factory=sm)
    state = FakeFSMContext()

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=blocked_id, is_bot=False, first_name="Blocked")
    cb.data = "menu:voice_clone"
    cb.answer = AsyncMock()

    handler_called = False

    async def dummy_handler(event, data):
        nonlocal handler_called
        handler_called = True
        return await handle_voice_clone_start(cb, state)

    res = await middleware(dummy_handler, cb, {})
    assert res is None
    assert handler_called is False
    cb.answer.assert_called_once_with(
        "⛔ သင့်အကောင့်ကို ပိတ်ပင်ထားပါသည်။ အကူအညီရယူရန် Admin ထံ ဆက်သွယ်ပါ။",
        show_alert=True,
    )
    # State must not be set
    assert state.current_state is None


@pytest.mark.asyncio
async def test_blocked_user_mid_flight_waiting_for_text(db_session):
    """User became BLOCKED after entering CloneFSM.waiting_for_text; text submission is dropped."""
    blocked_id = 10002
    user = User(id=blocked_id, username="midflight_text", status=UserStatus.BLOCKED.value, language_code="en")
    db_session.add(user)
    await db_session.commit()

    sm = create_mock_session_maker(db_session)
    middleware = AccessControlMiddleware(session_factory=sm)
    state = FakeFSMContext(
        initial_state=CloneFSM.waiting_for_text,
        initial_data={"lang": "en", "max_chars": 120},
    )

    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=blocked_id, is_bot=False, first_name="BlockedMid")
    msg.text = "Attempting to synthesize speech while blocked"
    msg.answer = AsyncMock()

    handler_called = False

    async def dummy_handler(event, data):
        nonlocal handler_called
        handler_called = True
        return await handle_clone_text_input(msg, state)

    res = await middleware(dummy_handler, msg, {})
    assert res is None
    assert handler_called is False
    msg.answer.assert_called_once_with("⛔ Your account has been suspended. Please contact support.")
    # State must not transition to waiting_for_voice
    assert state.current_state == CloneFSM.waiting_for_text
    assert "input_text" not in state.data


@pytest.mark.asyncio
async def test_blocked_user_mid_flight_voice_and_speed_selection(db_session):
    """User became BLOCKED at voice and speed selection callbacks; all are dropped."""
    blocked_id = 10003
    user = User(id=blocked_id, username="midflight_callbacks", status=UserStatus.BLOCKED.value, language_code="my")
    db_session.add(user)
    await db_session.commit()

    sm = create_mock_session_maker(db_session)
    middleware = AccessControlMiddleware(session_factory=sm)

    # 1. Voice selection attempt
    cb_voice = MagicMock(spec=CallbackQuery)
    cb_voice.from_user = TgUser(id=blocked_id, is_bot=False, first_name="BlockedVoice")
    cb_voice.data = "clone_voice:my-MM-ThihaNeural"
    cb_voice.answer = AsyncMock()

    handler_called = False

    async def handler_voice(event, data):
        nonlocal handler_called
        handler_called = True

    await middleware(handler_voice, cb_voice, {})
    assert handler_called is False
    cb_voice.answer.assert_called_once_with(
        "⛔ သင့်အကောင့်ကို ပိတ်ပင်ထားပါသည်။ အကူအညီရယူရန် Admin ထံ ဆက်သွယ်ပါ။",
        show_alert=True,
    )

    # 2. Speed selection attempt
    cb_speed = MagicMock(spec=CallbackQuery)
    cb_speed.from_user = TgUser(id=blocked_id, is_bot=False, first_name="BlockedSpeed")
    cb_speed.data = "clone_speed:normal"
    cb_speed.answer = AsyncMock()

    await middleware(handler_voice, cb_speed, {})
    assert handler_called is False
    cb_speed.answer.assert_called_once_with(
        "⛔ သင့်အကောင့်ကို ပိတ်ပင်ထားပါသည်။ အကူအညီရယူရန် Admin ထံ ဆက်သွယ်ပါ။",
        show_alert=True,
    )


@pytest.mark.asyncio
async def test_blocked_user_cannot_reach_model_selection_or_create_job(db_session):
    """User became BLOCKED right before final model selection: zero jobs, zero ledger mutations."""
    blocked_id = 10004
    user = User(id=blocked_id, username="blocked_final", status=UserStatus.BLOCKED.value, language_code="en")
    db_session.add(user)
    bal = await get_minute_balance(db_session, blocked_id)
    bal.purchased_seconds = 600
    await db_session.commit()

    sm = create_mock_session_maker(db_session)
    middleware = AccessControlMiddleware(session_factory=sm)
    state = FakeFSMContext(
        initial_state=CloneFSM.waiting_for_model,
        initial_data={
            "input_text": "Final synthesis test",
            "edge_voice": "my-MM-ThihaNeural",
            "speed_preset": "normal",
            "gender_preset": "m2m",
            "lang": "en",
        },
    )

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=blocked_id, is_bot=False, first_name="BlockedFinal")
    cb.data = "clone_model:1"
    cb.answer = AsyncMock()

    handler_called = False

    async def dummy_handler(event, data):
        nonlocal handler_called
        handler_called = True
        return await handle_clone_model_selected(cb, state)

    res = await middleware(dummy_handler, cb, {})
    assert res is None
    assert handler_called is False
    cb.answer.assert_called_once_with(
        "⛔ Your account has been suspended. Please contact support.",
        show_alert=True,
    )

    # Prove zero jobs were created in DB
    jobs = (await db_session.execute(select(Job).where(Job.user_id == blocked_id))).scalars().all()
    assert len(jobs) == 0

    # Prove zero ledger transactions were recorded
    ledgers = (await db_session.execute(select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == blocked_id))).scalars().all()
    assert len(ledgers) == 0

    # Prove balance was NOT deducted
    bal_after = await get_minute_balance(db_session, blocked_id)
    assert bal_after.purchased_seconds == 600


@pytest.mark.asyncio
async def test_blocked_user_stale_buttons_and_direct_commands(db_session):
    """All direct commands (/start, /help, /lang) and stale menu callbacks are dropped."""
    blocked_id = 10005
    user = User(id=blocked_id, username="blocked_stale", status=UserStatus.BLOCKED.value, language_code="my")
    db_session.add(user)
    await db_session.commit()

    sm = create_mock_session_maker(db_session)
    middleware = AccessControlMiddleware(session_factory=sm)

    # Stale callbacks: buy_minutes, refer_earn, tutorial
    for stale_cb_data in ["menu:buy_minutes", "menu:refer_earn", "menu:tutorial"]:
        cb = MagicMock(spec=CallbackQuery)
        cb.from_user = TgUser(id=blocked_id, is_bot=False, first_name="BlockedStale")
        cb.data = stale_cb_data
        cb.answer = AsyncMock()

        handler_mock = AsyncMock()
        await middleware(handler_mock, cb, {})
        handler_mock.assert_not_called()
        cb.answer.assert_called_once_with(
            "⛔ သင့်အကောင့်ကို ပိတ်ပင်ထားပါသည်။ အကူအညီရယူရန် Admin ထံ ဆက်သွယ်ပါ။",
            show_alert=True,
        )

    # Direct commands: /start, /help, /lang
    for cmd in ["/start", "/help", "/lang"]:
        msg = MagicMock(spec=Message)
        msg.from_user = TgUser(id=blocked_id, is_bot=False, first_name="BlockedStale")
        msg.text = cmd
        msg.answer = AsyncMock()

        handler_mock = AsyncMock()
        await middleware(handler_mock, msg, {})
        handler_mock.assert_not_called()
        msg.answer.assert_called_once_with("⛔ သင့်အကောင့်ကို ပိတ်ပင်ထားပါသည်။ အကူအညီရယူရန် Admin ထံ ဆက်သွယ်ပါ။")


@pytest.mark.asyncio
async def test_blocked_user_rapid_retries_zero_leakage(db_session):
    """Blocked user sending multiple rapid requests experiences zero leakage or state creation."""
    blocked_id = 10006
    user = User(id=blocked_id, username="rapid_retry", status=UserStatus.BLOCKED.value, language_code="en")
    db_session.add(user)
    await db_session.commit()

    sm = create_mock_session_maker(db_session)
    middleware = AccessControlMiddleware(session_factory=sm)

    handler_mock = AsyncMock()

    for i in range(5):
        msg = MagicMock(spec=Message)
        msg.from_user = TgUser(id=blocked_id, is_bot=False, first_name="Rapid")
        msg.text = f"Spam message {i}"
        msg.answer = AsyncMock()

        await middleware(handler_mock, msg, {})
        handler_mock.assert_not_called()
        msg.answer.assert_called_once_with("⛔ Your account has been suspended. Please contact support.")

    # DB state remains 100% clean
    jobs = (await db_session.execute(select(Job).where(Job.user_id == blocked_id))).scalars().all()
    assert len(jobs) == 0


# ===========================================================================
# P0 — ADMIN FSM WORKFLOWS MUST REMAIN INTACT
# ===========================================================================


@pytest.mark.asyncio
async def test_admin_voice_generation_full_lifecycle(db_session):
    """Configured admin completes entire voice clone wizard and successfully queues a job."""
    admin_id = 888001
    settings.admin_ids_raw = str(admin_id)

    # Create admin user record & active voice model in DB
    admin_user = User(id=admin_id, username="superadmin", status=UserStatus.ACTIVE.value, language_code="en")
    db_session.add(admin_user)
    model = VoiceModel(
        name="TestModel",
        slug="test-model",
        type=VoiceModelType.SYSTEM.value,
        status=VoiceModelStatus.ACTIVE.value,
        model_dir="/tmp/models/test",
        pth_path="/tmp/models/test/test.pth",
    )
    db_session.add(model)
    await db_session.commit()

    sm = create_mock_session_maker(db_session)
    state = FakeFSMContext()

    # Step 1: Admin clicks menu:voice_clone
    cb_start = MagicMock(spec=CallbackQuery)
    cb_start.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb_start.data = "menu:voice_clone"
    cb_start.message = MagicMock(spec=Message)
    cb_start.message.edit_text = AsyncMock()
    cb_start.answer = AsyncMock()

    with patch("app.handlers.clone.AsyncSessionLocal", sm):
        await handle_voice_clone_start(cb_start, state)

    assert state.current_state == CloneFSM.waiting_for_text
    cb_start.message.edit_text.assert_called_once()
    assert "Enter the text" in cb_start.message.edit_text.call_args[0][0]

    # Step 2: Admin inputs text
    msg_text = MagicMock(spec=Message)
    msg_text.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    msg_text.text = "Antigravity voice cloning for admin"
    msg_text.answer = AsyncMock()

    await handle_clone_text_input(msg_text, state)
    assert state.current_state == CloneFSM.waiting_for_voice
    assert state.data["input_text"] == "Antigravity voice cloning for admin"
    msg_text.answer.assert_called_once()

    # Step 3: Admin selects Edge TTS voice
    cb_voice = MagicMock(spec=CallbackQuery)
    cb_voice.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb_voice.data = "clone_voice:my-MM-ThihaNeural"
    cb_voice.message = MagicMock(spec=Message)
    cb_voice.message.edit_text = AsyncMock()
    cb_voice.answer = AsyncMock()

    await handle_clone_voice_selected(cb_voice, state)
    assert state.current_state == CloneFSM.waiting_for_speed
    assert state.data["edge_voice"] == "my-MM-ThihaNeural"

    # Step 4: Admin selects speed
    cb_speed = MagicMock(spec=CallbackQuery)
    cb_speed.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb_speed.data = "clone_speed:normal"
    cb_speed.message = MagicMock(spec=Message)
    cb_speed.message.edit_text = AsyncMock()
    cb_speed.answer = AsyncMock()

    await handle_clone_speed_selected(cb_speed, state)
    assert state.current_state == CloneFSM.waiting_for_gender
    assert state.data["speed_preset"] == "normal"

    # Step 5: Admin selects gender preset
    cb_gender = MagicMock(spec=CallbackQuery)
    cb_gender.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb_gender.data = "clone_gender:m2m"
    cb_gender.message = MagicMock(spec=Message)
    cb_gender.message.edit_text = AsyncMock()
    cb_gender.answer = AsyncMock()

    with patch("app.handlers.clone.AsyncSessionLocal", sm):
        await handle_clone_gender_selected(cb_gender, state)
    assert state.current_state == CloneFSM.waiting_for_model
    assert state.data["gender_preset"] == "m2m"

    # Step 6: Admin selects model -> Job creation
    cb_model = MagicMock(spec=CallbackQuery)
    cb_model.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb_model.data = f"clone_model:{model.id}"
    cb_model.message = MagicMock(spec=Message)
    status_msg = MagicMock(spec=Message)
    status_msg.message_id = 7788
    cb_model.message.edit_text = AsyncMock(return_value=status_msg)
    cb_model.answer = AsyncMock()

    with patch("app.handlers.clone.AsyncSessionLocal", sm):
        await handle_clone_model_selected(cb_model, state)

    # Voice v2 (documented): selecting a legacy trained (.pth) model no
    # longer creates a job. The user is shown the legacy-disabled message
    # pointing to VoxCPM2 reference-audio cloning, and the wizard state
    # is left untouched at waiting_for_model.
    assert state.current_state == CloneFSM.waiting_for_model
    shown_text = cb_model.message.edit_text.call_args[0][0]
    from app.services.i18n import t as _t
    assert shown_text in {_t("legacy_model_disabled", "my"), _t("legacy_model_disabled", "en")}

    # Prove NO job was created from the legacy model
    jobs = (await db_session.execute(select(Job).where(Job.user_id == admin_id))).scalars().all()
    assert len(jobs) == 0


@pytest.mark.asyncio
async def test_admin_broadcast_realistic_token_lifecycle(db_session, monkeypatch):
    """Realistic broadcast token lifecycle: preview -> cancel -> stale rejection -> fresh preview -> success -> duplicate rejection."""
    admin_id = 888002
    monkeypatch.setattr(settings, "admin_ids_raw", str(admin_id))

    # Seed 2 active users
    for uid in [3001, 3002]:
        db_session.add(User(id=uid, username=f"u_{uid}", status=UserStatus.ACTIVE.value))
    await db_session.commit()

    sm = create_mock_session_maker(db_session)
    state = FakeFSMContext()

    # Step 1: Admin initiates broadcast and inputs text -> Preview generated with Token A
    cb_init = MagicMock(spec=CallbackQuery)
    cb_init.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb_init.message = MagicMock(spec=Message)
    cb_init.message.edit_text = AsyncMock()
    cb_init.answer = AsyncMock()

    with patch("app.handlers.admin.AsyncSessionLocal", sm):
        await handle_admin_broadcast_start(cb_init, state)
    assert state.current_state == AdminBroadcastFSM.choosing_audience

    cb_grp = MagicMock(spec=CallbackQuery)
    cb_grp.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb_grp.data = "admin:bc_group:REGISTERED_ALL"
    cb_grp.message = MagicMock(spec=Message)
    cb_grp.message.edit_text = AsyncMock()
    cb_grp.answer = AsyncMock()
    with patch("app.handlers.admin.AsyncSessionLocal", sm):
        await handle_admin_broadcast_group_select(cb_grp, state)
    assert state.current_state == AdminBroadcastFSM.waiting_for_message

    msg_a = MagicMock(spec=Message)
    msg_a.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    msg_a.text = "Broadcast Announcement A"
    msg_a.answer = AsyncMock()

    with patch("app.handlers.admin.AsyncSessionLocal", sm):
        await handle_admin_broadcast_message_input(msg_a, state)

    assert state.current_state == AdminBroadcastFSM.confirming
    token_a = state.data.get("token")
    assert token_a is not None

    # Step 2: Admin cancels broadcast -> State cleared
    cb_cancel = MagicMock(spec=CallbackQuery)
    cb_cancel.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb_cancel.data = "admin:broadcast_cancel"
    cb_cancel.message = MagicMock(spec=Message)
    cb_cancel.message.edit_text = AsyncMock()
    cb_cancel.answer = AsyncMock()

    with patch("app.handlers.admin.AsyncSessionLocal", sm):
        await handle_admin_broadcast_cancel(cb_cancel, state)

    assert state.current_state is None
    assert state.data == {}
    cb_cancel.answer.assert_called_once()
    cancel_text = cb_cancel.answer.call_args[0][0]
    assert "Broadcast cancelled" in cancel_text or "ပယ်ဖျက်" in cancel_text

    # Step 3: Stale confirmation with Token A is attempted -> REJECTED (state is cleared)
    cb_stale_a = MagicMock(spec=CallbackQuery)
    cb_stale_a.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb_stale_a.data = f"admin:broadcast_send:{token_a}"
    cb_stale_a.answer = AsyncMock()

    with patch("app.handlers.admin.AsyncSessionLocal", sm):
        await handle_admin_broadcast_confirm(cb_stale_a, state)

    cb_stale_a.answer.assert_called_once()
    stale_text_1 = cb_stale_a.answer.call_args[0][0]
    assert "expired" in stale_text_1.lower() or "invalid" in stale_text_1.lower() or "သက်တမ်းကုန်" in stale_text_1

    # Verify 0 campaigns created
    c0 = (await db_session.execute(select(BroadcastCampaign))).scalars().all()
    assert len(c0) == 0

    # Step 4: Admin initiates fresh broadcast -> Fresh Preview with Token B
    with patch("app.handlers.admin.AsyncSessionLocal", sm):
        await handle_admin_broadcast_start(cb_init, state)
    assert state.current_state == AdminBroadcastFSM.choosing_audience

    cb_grp2 = MagicMock(spec=CallbackQuery)
    cb_grp2.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb_grp2.data = "admin:bc_group:REGISTERED_ALL"
    cb_grp2.message = MagicMock(spec=Message)
    cb_grp2.message.edit_text = AsyncMock()
    cb_grp2.answer = AsyncMock()
    with patch("app.handlers.admin.AsyncSessionLocal", sm):
        await handle_admin_broadcast_group_select(cb_grp2, state)
    assert state.current_state == AdminBroadcastFSM.waiting_for_message
    msg_b = MagicMock(spec=Message)
    msg_b.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    msg_b.text = "Broadcast Announcement B"
    msg_b.answer = AsyncMock()

    with patch("app.handlers.admin.AsyncSessionLocal", sm):
        await handle_admin_broadcast_message_input(msg_b, state)

    assert state.current_state == AdminBroadcastFSM.confirming
    token_b = state.data.get("token")
    assert token_b is not None
    assert token_b != token_a  # Must be a distinct fresh token

    # Step 5: Admin attempts to send stale Token A while Token B is pending -> REJECTED
    cb_stale_a2 = MagicMock(spec=CallbackQuery)
    cb_stale_a2.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb_stale_a2.data = f"admin:broadcast_send:{token_a}"
    cb_stale_a2.answer = AsyncMock()

    with patch("app.handlers.admin.AsyncSessionLocal", sm):
        await handle_admin_broadcast_confirm(cb_stale_a2, state)

    cb_stale_a2.answer.assert_called_once()
    stale_text_2 = cb_stale_a2.answer.call_args[0][0]
    assert "expired" in stale_text_2.lower() or "invalid" in stale_text_2.lower() or "သက်တမ်းကုန်" in stale_text_2
    # Current state still pending Token B
    assert state.current_state == AdminBroadcastFSM.confirming
    assert state.data["token"] == token_b

    # Step 6: Admin confirms with valid Token B -> SUCCEEDS
    cb_confirm_b = MagicMock(spec=CallbackQuery)
    cb_confirm_b.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb_confirm_b.data = f"admin:broadcast_send:{token_b}"
    progress_msg = MagicMock(spec=Message)
    progress_msg.message_id = 9911
    cb_confirm_b.message = MagicMock(spec=Message)
    cb_confirm_b.message.edit_text = AsyncMock(return_value=progress_msg)
    cb_confirm_b.answer = AsyncMock()
    cb_confirm_b.bot = MagicMock()
    cb_confirm_b.bot.send_message = AsyncMock()

    with patch("app.handlers.admin.AsyncSessionLocal", sm):
        await handle_admin_broadcast_confirm(cb_confirm_b, state)

    assert state.current_state is None
    assert state.data == {}

    # Exactly 1 campaign created in DB
    campaigns = (await db_session.execute(select(BroadcastCampaign))).scalars().all()
    assert len(campaigns) == 1
    assert campaigns[0].message_text == "Broadcast Announcement B"

    # Step 7: Duplicate confirmation with Token B sent again -> REJECTED (state already cleared)
    cb_dup = MagicMock(spec=CallbackQuery)
    cb_dup.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb_dup.data = f"admin:broadcast_send:{token_b}"
    cb_dup.answer = AsyncMock()

    with patch("app.handlers.admin.AsyncSessionLocal", sm):
        await handle_admin_broadcast_confirm(cb_dup, state)

    cb_dup.answer.assert_called_once()
    dup_text = cb_dup.answer.call_args[0][0]
    assert "expired" in dup_text.lower() or "invalid" in dup_text.lower() or "သက်တမ်းကုန်" in dup_text

    # Still exactly 1 campaign
    campaigns_after = (await db_session.execute(select(BroadcastCampaign))).scalars().all()
    assert len(campaigns_after) == 1


@pytest.mark.asyncio
async def test_admin_minute_adjustment_fsm_complete_lifecycle(db_session):
    """Admin minute adjustment multi-step FSM: amount, reason, confirm, and idempotency."""
    admin_id = 888003
    target_id = 4001
    settings.admin_ids_raw = str(admin_id)

    target_user = User(id=target_id, username="target_alice", status=UserStatus.ACTIVE.value, account_category=AccountCategory.FREE.value)
    db_session.add(target_user)
    bal = await get_minute_balance(db_session, target_id)
    bal.purchased_seconds = 300  # 5 min
    await db_session.commit()

    sm = create_mock_session_maker(db_session)
    state = FakeFSMContext()

    # 1. Admin initiates minute adjustment
    cb_init = MagicMock(spec=CallbackQuery)
    cb_init.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb_init.data = f"admin:adjust_start:{target_id}:CREDIT:1"
    cb_init.message = MagicMock(spec=Message)
    cb_init.message.edit_text = AsyncMock()
    cb_init.answer = AsyncMock()

    with patch("app.handlers.admin.AsyncSessionLocal", sm):
        await handle_admin_adjust_start(cb_init, state)

    assert state.current_state == AdminAdjustFSM.waiting_for_amount
    assert state.data["target_user_id"] == target_id

    # 2. Admin inputs amount (10 minutes)
    msg_amt = MagicMock(spec=Message)
    msg_amt.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    msg_amt.text = "10"
    msg_amt.answer = AsyncMock()

    await handle_admin_adjust_amount_input(msg_amt, state)
    assert state.current_state == AdminAdjustFSM.waiting_for_reason
    assert state.data["minutes"] == 10.0
    assert state.data["direction"] == "CREDIT"

    # 3. Admin inputs reason -> Confirmation preview
    msg_rsn = MagicMock(spec=Message)
    msg_rsn.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    msg_rsn.text = "QA test incentive bonus"
    msg_rsn.answer = AsyncMock()

    with patch("app.handlers.admin.AsyncSessionLocal", sm):
        await handle_admin_adjust_reason_input(msg_rsn, state)
    assert state.current_state == AdminAdjustFSM.confirming
    assert state.data["reason"] == "QA test incentive bonus"
    assert "token" in state.data
    adjust_token = state.data["token"]

    # 4. Admin confirms adjustment
    cb_conf = MagicMock(spec=CallbackQuery)
    cb_conf.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb_conf.data = f"admin:adjust_confirm:{adjust_token}"
    cb_conf.message = MagicMock(spec=Message)
    cb_conf.message.edit_text = AsyncMock()
    cb_conf.answer = AsyncMock()

    with patch("app.handlers.admin.AsyncSessionLocal", sm):
        await handle_admin_adjust_confirm(cb_conf, state=state, session=db_session)
    assert state.current_state is None

    # Prove ledger event created and balance incremented (+600 seconds)
    bal_after = await get_minute_balance(db_session, target_id)
    assert bal_after.purchased_seconds == 900  # 300 + 600

    ledgers = (await db_session.execute(select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == target_id))).scalars().all()
    assert any(ev.seconds == 600 and "QA test incentive bonus" in (ev.note or "") for ev in ledgers)

    # Prove audit log was recorded
    audits = (await db_session.execute(select(AuditLog).where(AuditLog.admin_id == admin_id))).scalars().all()
    assert len(audits) >= 1
    assert audits[-1].action == "balance.adjust.credit"


@pytest.mark.asyncio
async def test_admin_user_search_fsm_complete_lifecycle(db_session):
    """Admin user search multi-step FSM: initiation, search execution, and cancellation."""
    admin_id = 888004
    settings.admin_ids_raw = str(admin_id)

    db_session.add(User(id=5001, username="charlie_test", status=UserStatus.ACTIVE.value))
    await db_session.commit()

    sm = create_mock_session_maker(db_session)
    state = FakeFSMContext()

    # 1. Admin initiates search
    cb_start = MagicMock(spec=CallbackQuery)
    cb_start.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb_start.data = "admin:search_user"
    cb_start.message = MagicMock(spec=Message)
    cb_start.message.edit_text = AsyncMock()
    cb_start.answer = AsyncMock()

    with patch("app.handlers.admin.AsyncSessionLocal", sm):
        await handle_admin_user_search_start(cb_start, state)
    assert state.current_state == AdminUserSearchFSM.waiting_for_query

    # 2. Admin enters query
    msg_q = MagicMock(spec=Message)
    msg_q.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    msg_q.text = "@charlie_test"
    msg_q.answer = AsyncMock()

    await handle_admin_user_search_query(msg_q, state=state, session=db_session)
    assert state.current_state is None
    msg_q.answer.assert_called_once()
    assert "charlie_test" in msg_q.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_admin_bypass_during_database_failure():
    """During DB outage, admin bypasses middleware immediately without granting access to non-admins."""
    admin_id = 888005
    normal_id = 999005
    settings.admin_ids_raw = str(admin_id)

    # Session factory that raises DB connection error
    failing_sm = MagicMock()
    failing_sm.return_value.__aenter__ = AsyncMock(side_effect=ConnectionError("Database offline"))

    middleware = AccessControlMiddleware(session_factory=failing_sm)

    # 1. Non-admin is blocked and receives service unavailable notice
    msg_user = MagicMock(spec=Message)
    msg_user.from_user = TgUser(id=normal_id, is_bot=False, first_name="NormalUser")
    msg_user.answer = AsyncMock()

    user_handler = AsyncMock(return_value="USER_RESULT")
    res_user = await middleware(user_handler, msg_user, {})

    assert res_user is None
    user_handler.assert_not_called()
    msg_user.answer.assert_called_once()
    assert "temporarily unavailable" in msg_user.answer.call_args[0][0]

    # 2. Admin bypasses middleware and reaches downstream handler
    failing_sm.reset_mock()
    msg_admin = MagicMock(spec=Message)
    msg_admin.from_user = TgUser(id=admin_id, is_bot=False, first_name="AdminUser")
    msg_admin.answer = AsyncMock()

    admin_handler = AsyncMock(return_value="ADMIN_RESULT")
    res_admin = await middleware(admin_handler, msg_admin, {})

    assert res_admin == "ADMIN_RESULT"
    admin_handler.assert_awaited_once_with(msg_admin, {})
    msg_admin.answer.assert_not_called()
    failing_sm.assert_not_called()  # Proves zero DB connection attempted for admin


# ===========================================================================
# DISPATCHER REGISTRATION AUDIT
# ===========================================================================


def test_full_application_dispatcher_middleware_registration():
    """Verify dispatcher startup registers both message & callback outer middleware."""
    dp = Dispatcher(storage=MemoryStorage())
    setup_handlers(dp)

    msg_outers = list(dp.message.outer_middleware)
    cb_outers = list(dp.callback_query.outer_middleware)

    assert any(isinstance(m, AccessControlMiddleware) for m in msg_outers), "Message outer middleware missing"
    assert any(isinstance(m, AccessControlMiddleware) for m in cb_outers), "CallbackQuery outer middleware missing"

    # Verify idempotency
    setup_middlewares(dp)
    assert sum(1 for m in dp.message.outer_middleware if isinstance(m, AccessControlMiddleware)) == 1
    assert sum(1 for m in dp.callback_query.outer_middleware if isinstance(m, AccessControlMiddleware)) == 1


# ===========================================================================
# REAL DISPATCHER END-TO-END PIPELINE & AUTHORIZATION TESTS
# ===========================================================================


@pytest.mark.asyncio
async def test_real_dispatcher_blocked_user_cannot_generate_voice(db_session, monkeypatch):
    """Real dispatcher: BLOCKED user cannot initiate, advance, or finalize voice generation; 0 jobs, 0 debits."""
    blocked_id = 20001
    monkeypatch.setattr(settings, "admin_ids_raw", "999000")

    user = User(id=blocked_id, username="real_dp_blocked", status=UserStatus.BLOCKED.value, language_code="en")
    db_session.add(user)
    bal = await get_minute_balance(db_session, blocked_id)
    bal.purchased_seconds = 300
    await db_session.commit()

    sm = create_mock_session_maker(db_session)
    dp = Dispatcher(storage=MemoryStorage())
    setup_handlers(dp, session_factory=sm)

    bot = AsyncMock(spec=Bot)
    bot.id = 999888
    storage_key = StorageKey(bot_id=bot.id, chat_id=blocked_id, user_id=blocked_id)

    # 1. Entry Point: Blocked user sends menu:voice_clone callback update
    upd1 = make_callback_update(user_id=blocked_id, callback_data="menu:voice_clone", update_id=101)
    await dp.feed_update(bot, upd1)

    answer_cb_calls = [c[0][0] for c in bot.call_args_list if c[0] and type(c[0][0]).__name__ == "AnswerCallbackQuery"]
    assert len(answer_cb_calls) == 1
    assert "Your account has been suspended" in answer_cb_calls[0].text
    assert answer_cb_calls[0].show_alert is True
    assert await dp.storage.get_state(storage_key) is None

    # 2. Mid-flight: User blocked while at CloneFSM.waiting_for_text
    await dp.storage.set_state(storage_key, CloneFSM.waiting_for_text)
    await dp.storage.set_data(storage_key, {"lang": "en", "max_chars": 120})

    bot.reset_mock()
    upd2 = make_message_update(user_id=blocked_id, text="Attempt to synthesize text while blocked", update_id=102)
    await dp.feed_update(bot, upd2)

    send_msg_calls = [c[0][0] for c in bot.call_args_list if c[0] and type(c[0][0]).__name__ == "SendMessage"]
    assert len(send_msg_calls) == 1
    assert "Your account has been suspended" in send_msg_calls[0].text
    # State remains waiting_for_text and does not advance to waiting_for_voice
    assert await dp.storage.get_state(storage_key) == CloneFSM.waiting_for_text

    # 3. Final Model Selection: User blocked while at CloneFSM.waiting_for_model
    await dp.storage.set_state(storage_key, CloneFSM.waiting_for_model)
    await dp.storage.set_data(storage_key, {
        "input_text": "Final synthesis text",
        "edge_voice": "my-MM-ThihaNeural",
        "speed_preset": "normal",
        "gender_preset": "m2m",
        "lang": "en",
    })

    bot.reset_mock()
    upd3 = make_callback_update(user_id=blocked_id, callback_data="clone_model:1", update_id=103)
    await dp.feed_update(bot, upd3)

    answer_cb_calls_3 = [c[0][0] for c in bot.call_args_list if c[0] and type(c[0][0]).__name__ == "AnswerCallbackQuery"]
    assert len(answer_cb_calls_3) == 1
    assert "Your account has been suspended" in answer_cb_calls_3[0].text

    # Verify ZERO jobs created in database
    jobs = (await db_session.execute(select(Job).where(Job.user_id == blocked_id))).scalars().all()
    assert len(jobs) == 0

    # Verify ZERO minute ledger debits in database
    ledgers = (await db_session.execute(select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == blocked_id))).scalars().all()
    assert len(ledgers) == 0

    # Verify balance was completely unmutated
    bal_after = await get_minute_balance(db_session, blocked_id)
    assert bal_after.purchased_seconds == 300


@pytest.mark.asyncio
async def test_real_dispatcher_admin_voice_generation_end_to_end(db_session, monkeypatch):
    """Real dispatcher: Configured Admin completes 6-step voice clone flow through registered filters into a queued Job."""
    admin_id = 888010
    monkeypatch.setattr(settings, "admin_ids_raw", str(admin_id))

    admin_user = User(id=admin_id, username="admin_e2e", status=UserStatus.ACTIVE.value, language_code="en")
    db_session.add(admin_user)
    model = VoiceModel(
        name="RealModel",
        slug="real-model",
        type=VoiceModelType.SYSTEM.value,
        status=VoiceModelStatus.ACTIVE.value,
        model_dir="/tmp/models/real",
        pth_path="/tmp/models/real/real.pth",
    )
    db_session.add(model)
    await db_session.commit()

    sm = create_mock_session_maker(db_session)
    dp = Dispatcher(storage=MemoryStorage())
    setup_handlers(dp, session_factory=sm)

    bot = AsyncMock(spec=Bot)
    bot.id = 999888
    mock_status_msg = MagicMock(spec=Message)
    mock_status_msg.message_id = 9988
    bot.return_value = mock_status_msg
    storage_key = StorageKey(bot_id=bot.id, chat_id=admin_id, user_id=admin_id)

    # Step 1: Admin clicks menu:voice_clone
    with patch("app.handlers.clone.AsyncSessionLocal", sm):
        await dp.feed_update(bot, make_callback_update(admin_id, "menu:voice_clone", 201))
    assert await dp.storage.get_state(storage_key) == CloneFSM.waiting_for_text

    # Step 2: Admin inputs text
    with patch("app.handlers.clone.AsyncSessionLocal", sm):
        await dp.feed_update(bot, make_message_update(admin_id, "Hello from real dispatcher admin voice clone", 202))
    assert await dp.storage.get_state(storage_key) == CloneFSM.waiting_for_voice

    # Step 3: Admin selects voice
    with patch("app.handlers.clone.AsyncSessionLocal", sm):
        await dp.feed_update(bot, make_callback_update(admin_id, "clone_voice:my-MM-ThihaNeural", 203))
    assert await dp.storage.get_state(storage_key) == CloneFSM.waiting_for_speed

    # Step 4: Admin selects speed
    with patch("app.handlers.clone.AsyncSessionLocal", sm):
        await dp.feed_update(bot, make_callback_update(admin_id, "clone_speed:normal", 204))
    assert await dp.storage.get_state(storage_key) == CloneFSM.waiting_for_gender

    # Step 5: Admin selects gender
    with patch("app.handlers.clone.AsyncSessionLocal", sm):
        await dp.feed_update(bot, make_callback_update(admin_id, "clone_gender:m2m", 205))
    assert await dp.storage.get_state(storage_key) == CloneFSM.waiting_for_model

    # Step 6: Admin selects model -> legacy .pth flow is disabled (voice v2)
    with patch("app.handlers.clone.AsyncSessionLocal", sm):
        await dp.feed_update(bot, make_callback_update(admin_id, f"clone_model:{model.id}", 206))

    # State is NOT cleared and NO job is created: the legacy trained-model
    # path is closed in favour of VoxCPM2 reference-audio cloning, and the
    # user is shown the legacy-disabled message instead.
    assert await dp.storage.get_state(storage_key) == CloneFSM.waiting_for_model

    jobs = (await db_session.execute(select(Job).where(Job.user_id == admin_id))).scalars().all()
    assert len(jobs) == 0


@pytest.mark.asyncio
async def test_blocked_user_all_voice_entry_points_matrix(db_session, monkeypatch):
    """Real dispatcher: Every registered voice callback and message entry point drops blocked user events."""
    blocked_id = 20050
    monkeypatch.setattr(settings, "admin_ids_raw", "999000")

    user = User(id=blocked_id, username="matrix_blocked", status=UserStatus.BLOCKED.value, language_code="en")
    db_session.add(user)
    await db_session.commit()

    sm = create_mock_session_maker(db_session)
    dp = Dispatcher(storage=MemoryStorage())
    setup_handlers(dp, session_factory=sm)

    bot = AsyncMock(spec=Bot)
    bot.id = 999888

    # All voice generation callback entry points
    callbacks = [
        "menu:voice_clone",
        "menu:start",
        "clone_voice:my-MM-ThihaNeural",
        "clone_speed:normal",
        "clone_gender:m2m",
        "clone_model:1",
    ]

    for i, cb_data in enumerate(callbacks):
        bot.reset_mock()
        upd = make_callback_update(user_id=blocked_id, callback_data=cb_data, update_id=400 + i)
        await dp.feed_update(bot, upd)

        answer_cb_calls = [c[0][0] for c in bot.call_args_list if c[0] and type(c[0][0]).__name__ == "AnswerCallbackQuery"]
        assert len(answer_cb_calls) == 1, f"Expected 1 AnswerCallbackQuery for {cb_data}"
        assert "Your account has been suspended" in answer_cb_calls[0].text
        assert answer_cb_calls[0].show_alert is True

    # Message entry points
    messages = [
        "Synthesize this voice text",
        "/start",
        "/help",
        "/lang",
    ]

    for j, msg_text in enumerate(messages):
        bot.reset_mock()
        upd = make_message_update(user_id=blocked_id, text=msg_text, update_id=500 + j)
        await dp.feed_update(bot, upd)

        send_msg_calls = [c[0][0] for c in bot.call_args_list if c[0] and type(c[0][0]).__name__ == "SendMessage"]
        assert len(send_msg_calls) == 1, f"Expected 1 SendMessage for {msg_text}"
        assert "Your account has been suspended" in send_msg_calls[0].text

    # Zero jobs or ledger entries created
    jobs = (await db_session.execute(select(Job).where(Job.user_id == blocked_id))).scalars().all()
    assert len(jobs) == 0

    ledgers = (await db_session.execute(select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == blocked_id))).scalars().all()
    assert len(ledgers) == 0


@pytest.mark.asyncio
async def test_real_dispatcher_dynamic_unblock_recovery(db_session, monkeypatch):
    """Real dispatcher: Unblocking user in database restores normal access immediately without bot reboot."""
    target_id = 20060
    monkeypatch.setattr(settings, "admin_ids_raw", "999000")

    # Initially BLOCKED
    user = User(id=target_id, username="dyn_recovery", status=UserStatus.BLOCKED.value, language_code="en")
    db_session.add(user)
    await db_session.commit()

    sm = create_mock_session_maker(db_session)
    dp = Dispatcher(storage=MemoryStorage())
    setup_handlers(dp, session_factory=sm)

    bot = AsyncMock(spec=Bot)
    bot.id = 999888
    storage_key = StorageKey(bot_id=bot.id, chat_id=target_id, user_id=target_id)

    # Attempt 1: while BLOCKED -> Rejected
    upd1 = make_callback_update(user_id=target_id, callback_data="menu:voice_clone", update_id=601)
    with patch("app.handlers.clone.AsyncSessionLocal", sm):
        await dp.feed_update(bot, upd1)

    answer_cb_calls = [c[0][0] for c in bot.call_args_list if c[0] and type(c[0][0]).__name__ == "AnswerCallbackQuery"]
    assert len(answer_cb_calls) == 1
    assert "Your account has been suspended" in answer_cb_calls[0].text
    assert await dp.storage.get_state(storage_key) is None

    # Admin unblocks user in database
    user.status = UserStatus.ACTIVE.value
    bal = await get_minute_balance(db_session, target_id)
    bal.welcome_seconds = 300
    await db_session.commit()

    # Attempt 2: while ACTIVE -> Succeeds and enters CloneFSM.waiting_for_text!
    bot.reset_mock()
    upd2 = make_callback_update(user_id=target_id, callback_data="menu:voice_clone", update_id=602)
    with patch("app.handlers.clone.AsyncSessionLocal", sm):
        await dp.feed_update(bot, upd2)

    assert await dp.storage.get_state(storage_key) == CloneFSM.waiting_for_text


@pytest.mark.asyncio
async def test_non_admin_cannot_reach_admin_operations(db_session, monkeypatch):
    """Real dispatcher: Non-admin users cannot access admin commands or callbacks."""
    admin_id = 888030
    normal_id = 777030
    monkeypatch.setattr(settings, "admin_ids_raw", str(admin_id))

    normal_user = User(id=normal_id, username="normal_eve", status=UserStatus.ACTIVE.value)
    db_session.add(normal_user)
    await db_session.commit()

    sm = create_mock_session_maker(db_session)
    dp = Dispatcher(storage=MemoryStorage())
    setup_handlers(dp, session_factory=sm)

    bot = AsyncMock(spec=Bot)
    bot.id = 999888

    # 1. Normal user sends /admin command
    upd_cmd = make_message_update(user_id=normal_id, text="/admin", update_id=701)
    with patch("app.handlers.admin.AsyncSessionLocal", sm):
        await dp.feed_update(bot, upd_cmd)

    send_msg_calls = [c[0][0] for c in bot.call_args_list if c[0] and type(c[0][0]).__name__ == "SendMessage"]
    assert len(send_msg_calls) == 1
    assert "access denied" in send_msg_calls[0].text.lower() or "admin only" in send_msg_calls[0].text.lower() or "ခွင့်ပြုချက်မရှိ" in send_msg_calls[0].text

    # 2. Normal user sends admin:dashboard callback
    bot.reset_mock()
    upd_cb = make_callback_update(user_id=normal_id, callback_data="admin:dashboard", update_id=702)
    with patch("app.handlers.admin.AsyncSessionLocal", sm):
        await dp.feed_update(bot, upd_cb)

    answer_cb_calls = [c[0][0] for c in bot.call_args_list if c[0] and type(c[0][0]).__name__ == "AnswerCallbackQuery"]
    assert len(answer_cb_calls) == 1
    assert "access denied" in answer_cb_calls[0].text.lower() or "admin only" in answer_cb_calls[0].text.lower() or "ခွင့်ပြုချက်မရှိ" in answer_cb_calls[0].text
    assert answer_cb_calls[0].show_alert is True
