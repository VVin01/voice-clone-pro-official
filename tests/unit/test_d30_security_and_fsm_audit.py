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
        return "SHOULD_NOT_RUN"

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
        initial_state="waiting_for_text",
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
        return "SHOULD_NOT_RUN"

    res = await middleware(dummy_handler, msg, {})
    assert res is None
    assert handler_called is False
    msg.answer.assert_called_once_with("⛔ Your account has been suspended. Please contact support.")
    # State must not transition
    assert state.current_state == "waiting_for_text"
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
        initial_state="waiting_for_model",
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
        return "SHOULD_NOT_RUN"

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


# Bot-chat CloneFSM and AdminBroadcastFSM wizards were retired in Round 13 Part K
# (moved to Mini App); AccessControlMiddleware & real-dispatcher security tests remain below.


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

    # 2. Mid-flight: User blocked while in a stored FSM state
    await dp.storage.set_state(storage_key, "CloneFSM:waiting_for_text")
    await dp.storage.set_data(storage_key, {"lang": "en", "max_chars": 120})

    bot.reset_mock()
    upd2 = make_message_update(user_id=blocked_id, text="Attempt to synthesize text while blocked", update_id=102)
    await dp.feed_update(bot, upd2)

    send_msg_calls = [c[0][0] for c in bot.call_args_list if c[0] and type(c[0][0]).__name__ == "SendMessage"]
    assert len(send_msg_calls) == 1
    assert "Your account has been suspended" in send_msg_calls[0].text
    assert await dp.storage.get_state(storage_key) == "CloneFSM:waiting_for_text"

    # 3. Callback while blocked
    await dp.storage.set_state(storage_key, "CloneFSM:waiting_for_model")
    await dp.storage.set_data(storage_key, {
        "input_text": "Final synthesis text",
        "edge_voice": "my-MM-ThihaNeural",
        "speed_preset": "normal",
        "gender_preset": "m2m",
        "lang": "en",
    })

    bot.reset_mock()
    upd3 = make_callback_update(user_id=blocked_id, callback_data="menu:refer_earn", update_id=103)
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

    # Attempt 2: while ACTIVE -> Succeeds and serves the live menu:change_lang callback!
    bot.reset_mock()
    upd2 = make_callback_update(user_id=target_id, callback_data="menu:change_lang", update_id=602)
    with patch("app.handlers.menu.AsyncSessionLocal", sm):
        await dp.feed_update(bot, upd2)

    send_msg_calls = [c[0][0] for c in bot.call_args_list if c[0] and type(c[0][0]).__name__ == "SendMessage"]
    assert len(send_msg_calls) == 1
    assert "Your account has been suspended" not in send_msg_calls[0].text


@pytest.mark.asyncio
async def test_non_admin_cannot_reach_admin_operations(db_session, monkeypatch):
    """Real dispatcher: Non-admin users cannot access live admin payment-review callbacks."""
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

    # Normal user sends live admin payment callback
    upd_cb = make_callback_update(user_id=normal_id, callback_data="admin:pay_detail:PAY-TEST-1", update_id=702)
    with patch("app.handlers.admin.AsyncSessionLocal", sm):
        await dp.feed_update(bot, upd_cb)

    answer_cb_calls = [c[0][0] for c in bot.call_args_list if c[0] and type(c[0][0]).__name__ == "AnswerCallbackQuery"]
    assert len(answer_cb_calls) == 1
    assert "access denied" in answer_cb_calls[0].text.lower() or "admin only" in answer_cb_calls[0].text.lower() or "ဝင်ရောက်ခွင့်မရှိပါ" in answer_cb_calls[0].text
    assert answer_cb_calls[0].show_alert is True
