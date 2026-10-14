from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram import Dispatcher
from aiogram.types import CallbackQuery, Message, User as TgUser

from app.config import settings
from app.handlers import setup_handlers
from app.middlewares import AccessControlMiddleware, setup_middlewares
from app.models.enums import AccountCategory, UserStatus
from app.models.schema import User


@pytest.mark.asyncio
async def test_access_control_blocked_user_message_myanmar(db_session):
    settings.admin_ids_raw = "999001"
    blocked_uid = 555001

    # Insert blocked user in Myanmar locale
    db_user = User(
        id=blocked_uid,
        username="blocked_my",
        first_name="Blocked Myanmar",
        language_code="my",
        account_category=AccountCategory.FREE.value,
        status=UserStatus.BLOCKED.value,
    )
    db_session.add(db_user)
    await db_session.commit()

    session_maker = MagicMock()
    session_maker.return_value.__aenter__ = AsyncMock(return_value=db_session)
    session_maker.return_value.__aexit__ = AsyncMock(return_value=None)

    middleware = AccessControlMiddleware(session_factory=session_maker)
    handler = AsyncMock(return_value="OK")

    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=blocked_uid, is_bot=False, first_name="Blocked Myanmar")
    msg.answer = AsyncMock()

    result = await middleware(handler, msg, {})

    assert result is None
    handler.assert_not_called()
    msg.answer.assert_called_once_with(
        "⛔ သင့်အကောင့်ကို ပိတ်ပင်ထားပါသည်။ အကူအညီရယူရန် Admin ထံ ဆက်သွယ်ပါ။"
    )


@pytest.mark.asyncio
async def test_access_control_blocked_user_message_english(db_session):
    settings.admin_ids_raw = "999001"
    blocked_uid = 555002

    # Insert blocked user in English locale
    db_user = User(
        id=blocked_uid,
        username="blocked_en",
        first_name="Blocked English",
        language_code="en",
        account_category=AccountCategory.FREE.value,
        status=UserStatus.BLOCKED.value,
    )
    db_session.add(db_user)
    await db_session.commit()

    session_maker = MagicMock()
    session_maker.return_value.__aenter__ = AsyncMock(return_value=db_session)
    session_maker.return_value.__aexit__ = AsyncMock(return_value=None)

    middleware = AccessControlMiddleware(session_factory=session_maker)
    handler = AsyncMock(return_value="OK")

    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=blocked_uid, is_bot=False, first_name="Blocked English")
    msg.answer = AsyncMock()

    result = await middleware(handler, msg, {})

    assert result is None
    handler.assert_not_called()
    msg.answer.assert_called_once_with(
        "⛔ Your account has been suspended. Please contact support."
    )


@pytest.mark.asyncio
async def test_access_control_blocked_user_callback_myanmar(db_session):
    settings.admin_ids_raw = "999001"
    blocked_uid = 555003

    db_user = User(
        id=blocked_uid,
        username="blocked_cb_my",
        first_name="Blocked Callback Myanmar",
        language_code="my",
        account_category=AccountCategory.FREE.value,
        status=UserStatus.BLOCKED.value,
    )
    db_session.add(db_user)
    await db_session.commit()

    session_maker = MagicMock()
    session_maker.return_value.__aenter__ = AsyncMock(return_value=db_session)
    session_maker.return_value.__aexit__ = AsyncMock(return_value=None)

    middleware = AccessControlMiddleware(session_factory=session_maker)
    handler = AsyncMock(return_value="OK")

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=blocked_uid, is_bot=False, first_name="Blocked Callback Myanmar")
    cb.answer = AsyncMock()

    result = await middleware(handler, cb, {})

    assert result is None
    handler.assert_not_called()
    cb.answer.assert_called_once_with(
        "⛔ သင့်အကောင့်ကို ပိတ်ပင်ထားပါသည်။ အကူအညီရယူရန် Admin ထံ ဆက်သွယ်ပါ။",
        show_alert=True,
    )


@pytest.mark.asyncio
async def test_access_control_blocked_user_callback_english(db_session):
    settings.admin_ids_raw = "999001"
    blocked_uid = 555004

    db_user = User(
        id=blocked_uid,
        username="blocked_cb_en",
        first_name="Blocked Callback English",
        language_code="en",
        account_category=AccountCategory.FREE.value,
        status=UserStatus.BLOCKED.value,
    )
    db_session.add(db_user)
    await db_session.commit()

    session_maker = MagicMock()
    session_maker.return_value.__aenter__ = AsyncMock(return_value=db_session)
    session_maker.return_value.__aexit__ = AsyncMock(return_value=None)

    middleware = AccessControlMiddleware(session_factory=session_maker)
    handler = AsyncMock(return_value="OK")

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=blocked_uid, is_bot=False, first_name="Blocked Callback English")
    cb.answer = AsyncMock()

    result = await middleware(handler, cb, {})

    assert result is None
    handler.assert_not_called()
    cb.answer.assert_called_once_with(
        "⛔ Your account has been suspended. Please contact support.",
        show_alert=True,
    )


@pytest.mark.asyncio
async def test_access_control_active_user_allowed(db_session):
    settings.admin_ids_raw = "999001"
    active_uid = 555005

    db_user = User(
        id=active_uid,
        username="active_user",
        first_name="Active User",
        language_code="my",
        account_category=AccountCategory.FREE.value,
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(db_user)
    await db_session.commit()

    session_maker = MagicMock()
    session_maker.return_value.__aenter__ = AsyncMock(return_value=db_session)
    session_maker.return_value.__aexit__ = AsyncMock(return_value=None)

    middleware = AccessControlMiddleware(session_factory=session_maker)
    handler = AsyncMock(return_value="PROCEEDED")

    # Test Message
    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=active_uid, is_bot=False, first_name="Active User")
    msg.answer = AsyncMock()

    res_msg = await middleware(handler, msg, {})
    assert res_msg == "PROCEEDED"
    handler.assert_awaited_once_with(msg, {})
    msg.answer.assert_not_called()

    # Test CallbackQuery
    handler.reset_mock()
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=active_uid, is_bot=False, first_name="Active User")
    cb.answer = AsyncMock()

    res_cb = await middleware(handler, cb, {})
    assert res_cb == "PROCEEDED"
    handler.assert_awaited_once_with(cb, {})
    cb.answer.assert_not_called()


@pytest.mark.asyncio
async def test_access_control_unregistered_user_allowed(db_session):
    settings.admin_ids_raw = "999001"
    unregistered_uid = 555099

    session_maker = MagicMock()
    session_maker.return_value.__aenter__ = AsyncMock(return_value=db_session)
    session_maker.return_value.__aexit__ = AsyncMock(return_value=None)

    middleware = AccessControlMiddleware(session_factory=session_maker)
    handler = AsyncMock(return_value="NEW_USER_ROUTED")

    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=unregistered_uid, is_bot=False, first_name="Newcomer")
    msg.answer = AsyncMock()

    res = await middleware(handler, msg, {})
    assert res == "NEW_USER_ROUTED"
    handler.assert_awaited_once_with(msg, {})
    msg.answer.assert_not_called()


@pytest.mark.asyncio
async def test_access_control_admin_bypass_zero_db():
    settings.admin_ids_raw = "999001"
    admin_uid = 999001

    # Injected session factory raises error if called
    session_maker = MagicMock(side_effect=RuntimeError("DB should not be contacted for admin"))

    middleware = AccessControlMiddleware(session_factory=session_maker)
    handler = AsyncMock(return_value="ADMIN_ACCESS")

    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=admin_uid, is_bot=False, first_name="Master Admin")
    msg.answer = AsyncMock()

    res = await middleware(handler, msg, {})
    assert res == "ADMIN_ACCESS"
    handler.assert_awaited_once_with(msg, {})
    session_maker.assert_not_called()
    msg.answer.assert_not_called()


@pytest.mark.asyncio
async def test_access_control_db_error_fail_closed_for_non_admin():
    settings.admin_ids_raw = "999001"
    user_uid = 555010

    # Injected session factory raises DB failure
    session_maker = MagicMock()
    session_maker.return_value.__aenter__ = AsyncMock(side_effect=ConnectionError("DB connection lost"))
    session_maker.return_value.__aexit__ = AsyncMock(return_value=None)

    middleware = AccessControlMiddleware(session_factory=session_maker)
    handler = AsyncMock(return_value="PROCEEDED")

    # Non-admin Message fails closed gracefully
    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=user_uid, is_bot=False, first_name="User During Outage")
    msg.answer = AsyncMock()

    res_msg = await middleware(handler, msg, {})
    assert res_msg is None
    handler.assert_not_called()
    msg.answer.assert_called_once()
    assert "temporarily unavailable" in msg.answer.call_args[0][0]

    # Non-admin CallbackQuery fails closed gracefully
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=user_uid, is_bot=False, first_name="User During Outage")
    cb.answer = AsyncMock()

    res_cb = await middleware(handler, cb, {})
    assert res_cb is None
    handler.assert_not_called()
    cb.answer.assert_called_once()
    assert cb.answer.call_args[1].get("show_alert") is True
    assert "temporarily unavailable" in cb.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_access_control_event_without_from_user():
    session_maker = MagicMock()
    middleware = AccessControlMiddleware(session_factory=session_maker)
    handler = AsyncMock(return_value="NO_USER_EVENT")

    event = MagicMock(spec=Message)
    event.from_user = None

    res = await middleware(handler, event, {})
    assert res == "NO_USER_EVENT"
    handler.assert_awaited_once_with(event, {})
    session_maker.assert_not_called()


@pytest.mark.asyncio
async def test_access_control_dynamic_unblock(db_session):
    settings.admin_ids_raw = "999001"
    target_uid = 555020

    # Initially BLOCKED
    db_user = User(
        id=target_uid,
        username="target_user",
        first_name="Target",
        language_code="en",
        account_category=AccountCategory.FREE.value,
        status=UserStatus.BLOCKED.value,
    )
    db_session.add(db_user)
    await db_session.commit()

    session_maker = MagicMock()
    session_maker.return_value.__aenter__ = AsyncMock(return_value=db_session)
    session_maker.return_value.__aexit__ = AsyncMock(return_value=None)

    middleware = AccessControlMiddleware(session_factory=session_maker)
    handler = AsyncMock(return_value="ALLOWED")

    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=target_uid, is_bot=False, first_name="Target")
    msg.answer = AsyncMock()

    # 1. Attempt while BLOCKED -> Rejected
    res1 = await middleware(handler, msg, {})
    assert res1 is None
    handler.assert_not_called()
    assert msg.answer.call_count == 1

    # 2. Admin unblocks user in database
    db_user.status = UserStatus.ACTIVE.value
    await db_session.commit()

    # 3. Next attempt -> Allowed
    res2 = await middleware(handler, msg, {})
    assert res2 == "ALLOWED"
    handler.assert_awaited_once_with(msg, {})
    assert msg.answer.call_count == 1  # No additional rejection message


def test_setup_middlewares_and_handlers_registration():
    dp = Dispatcher()
    setup_handlers(dp)

    # Check that AccessControlMiddleware is registered in message outer_middleware
    msg_middlewares = list(dp.message.outer_middleware)
    assert any(isinstance(m, AccessControlMiddleware) for m in msg_middlewares)

    # Check that AccessControlMiddleware is registered in callback_query outer_middleware
    cb_middlewares = list(dp.callback_query.outer_middleware)
    assert any(isinstance(m, AccessControlMiddleware) for m in cb_middlewares)

    # Verify idempotency: calling setup_middlewares again does not duplicate
    setup_middlewares(dp)
    assert sum(1 for m in dp.message.outer_middleware if isinstance(m, AccessControlMiddleware)) == 1
    assert sum(1 for m in dp.callback_query.outer_middleware if isinstance(m, AccessControlMiddleware)) == 1


@pytest.mark.asyncio
async def test_access_control_prevents_job_creation_and_balance_debit(db_session):
    from sqlalchemy import select
    from app.models.schema import Job, MinuteLedgerEvent

    blocked_uid = 555030

    db_user = User(
        id=blocked_uid,
        username="blocked_cloner",
        first_name="Blocked Cloner",
        language_code="my",
        account_category=AccountCategory.FREE.value,
        status=UserStatus.BLOCKED.value,
    )
    db_session.add(db_user)
    await db_session.commit()

    session_maker = MagicMock()
    session_maker.return_value.__aenter__ = AsyncMock(return_value=db_session)
    session_maker.return_value.__aexit__ = AsyncMock(return_value=None)

    middleware = AccessControlMiddleware(session_factory=session_maker)

    # Simulated clone text handler that would create a job and debit minutes if called
    clone_handler_called = False

    async def mock_clone_handler(event, data):
        nonlocal clone_handler_called
        clone_handler_called = True
        return None

    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=blocked_uid, is_bot=False, first_name="Blocked Cloner")
    msg.text = "Hello world synthesis text"
    msg.answer = AsyncMock()

    await middleware(mock_clone_handler, msg, {})

    # Verify downstream handler was blocked
    assert clone_handler_called is False
    msg.answer.assert_called_once_with("⛔ သင့်အကောင့်ကို ပိတ်ပင်ထားပါသည်။ အကူအညီရယူရန် Admin ထံ ဆက်သွယ်ပါ။")

    # Verify 0 jobs exist for this user in DB
    jobs = (await db_session.execute(select(Job).where(Job.user_id == blocked_uid))).scalars().all()
    assert len(jobs) == 0

    # Verify 0 ledger debits exist for this user
    ledgers = (await db_session.execute(select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == blocked_uid))).scalars().all()
    assert len(ledgers) == 0


@pytest.mark.asyncio
async def test_access_control_prevents_payment_submission(db_session):
    from sqlalchemy import select
    from app.models.schema import PaymentRequest

    blocked_uid = 555040

    db_user = User(
        id=blocked_uid,
        username="blocked_payer",
        first_name="Blocked Payer",
        language_code="en",
        account_category=AccountCategory.FREE.value,
        status=UserStatus.BLOCKED.value,
    )
    db_session.add(db_user)
    await db_session.commit()

    session_maker = MagicMock()
    session_maker.return_value.__aenter__ = AsyncMock(return_value=db_session)
    session_maker.return_value.__aexit__ = AsyncMock(return_value=None)

    middleware = AccessControlMiddleware(session_factory=session_maker)

    payment_handler_called = False

    async def mock_payment_handler(event, data):
        nonlocal payment_handler_called
        payment_handler_called = True
        return None

    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=blocked_uid, is_bot=False, first_name="Blocked Payer")
    msg.photo = [MagicMock(file_id="photo_123")]
    msg.answer = AsyncMock()

    await middleware(mock_payment_handler, msg, {})

    assert payment_handler_called is False
    msg.answer.assert_called_once_with("⛔ Your account has been suspended. Please contact support.")

    # Verify 0 payments exist in DB
    payments = (await db_session.execute(select(PaymentRequest).where(PaymentRequest.user_id == blocked_uid))).scalars().all()
    assert len(payments) == 0
