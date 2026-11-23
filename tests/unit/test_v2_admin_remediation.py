from __future__ import annotations

from contextlib import asynccontextmanager
import datetime
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, User as TgUser
from sqlalchemy import select

from app.config import settings
from app.handlers.admin import (
    handle_admin_pay_approve_confirm,
    handle_admin_pay_approve_prompt,
    handle_admin_pay_reject_confirm,
    handle_admin_pay_reject_prompt,
)
from app.handlers.menu import render_main_menu
from app.models.enums import (
    AccountCategory,
    JobStatus,
    LedgerDirection,
    LedgerSource,
    PaymentStatus,
    UserStatus,
)
from app.models.schema import (
    AuditLog,
    BotSetting,
    Job,
    MinuteLedgerEvent,
    MonthlyFreeAllowanceGrant,
    PaymentRequest,
    User,
    VoiceModel,
    WelcomeBonusAward,
)
from app.handlers.start import handle_gate_verify, handle_set_language, handle_start
from app.services.gate import verify_user_membership
from app.services.i18n import t
from app.services.ledger import (
    award_welcome_bonus,
    consume_seconds,
    credit_purchased_minutes,
    get_current_daily_period,
    get_current_monthly_period,
    get_minute_balance,
    get_or_create_user,
    grant_daily_free_allowance,
    grant_monthly_free_allowance,
)
from app.services.queue import create_voice_clone_job


@pytest.mark.asyncio
async def test_free_user_welcome_bonus_default_zero(db_session):
    """Under current settings, welcome_bonus_seconds is 0. New Free users receive strictly 180s daily free."""
    assert settings.welcome_bonus_seconds == 0

    user, _ = await get_or_create_user(db_session, telegram_id=987654321)
    # Limits round (2026-11-23): daily free allowance (180s) replaced monthly free allowance.
    await grant_daily_free_allowance(db_session, user.id)

    # Calling award_welcome_bonus directly with 0s returns False
    ok, bal = await award_welcome_bonus(db_session, user.id, seconds=settings.welcome_bonus_seconds)
    assert ok is False
    assert bal.welcome_seconds == 0
    assert bal.daily_free_seconds == 180
    assert bal.purchased_seconds == 0
    assert bal.total_usable_seconds == 180  # Exactly 3.0 minutes, NOT 8.0 minutes!

    # Gate verification also must not award welcome bonus when welcome_bonus_seconds == 0
    # Fail-closed gate (security branch): gate IDs must be set in the test,
    # otherwise verification correctly returns False for a missing config.
    settings.required_channel_id = "-100111222"
    settings.required_group_id = "-100333444"
    bot_mock = AsyncMock()
    channel_member = MagicMock()
    channel_member.status = "member"
    bot_mock.get_chat_member.return_value = channel_member

    gate_ok, _ = await verify_user_membership(db_session, user, bot_mock)
    assert gate_ok is True

    balance_after = await get_minute_balance(db_session, user.id)
    assert balance_after.welcome_seconds == 0
    assert balance_after.total_usable_seconds == 180




@pytest.mark.asyncio
async def test_admin_payment_confirmation_safeguards(db_session):
    """Payment approval and rejection require intermediate confirmation prompts."""
    settings.admin_ids_raw = "888111"
    target_user, _ = await get_or_create_user(db_session, telegram_id=444001)

    payment = PaymentRequest(
        id="PAY-SAFE-10",
        user_id=target_user.id,
        package_code="MIN_30",
        minutes=30,
        amount_mmk=5000,
        status=PaymentStatus.PENDING.value,
    )
    db_session.add(payment)
    await db_session.flush()

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=888111, is_bot=False, first_name="Admin")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    # 1. Approval prompt
    cb.data = f"admin:pay_approve:{payment.id}"
    @asynccontextmanager
    async def mock_session_ctx():
        yield db_session

    with patch("app.handlers.admin.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await handle_admin_pay_approve_prompt(cb)

    prompt_text = cb.message.edit_text.call_args[0][0]
    prompt_kb = cb.message.edit_text.call_args[1].get("reply_markup")
    assert "Confirm Payment Approval" in prompt_text
    assert "PAY-SAFE-10" in prompt_text
    approve_confirm_cbs = [btn.callback_data for row in prompt_kb.inline_keyboard for btn in row]
    assert f"admin:pay_approve_confirm:{payment.id}" in approve_confirm_cbs
    assert f"admin:pay_detail:{payment.id}" in approve_confirm_cbs

    # 2. Rejection prompt
    cb.data = f"admin:pay_reject:{payment.id}"
    with patch("app.handlers.admin.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await handle_admin_pay_reject_prompt(cb)

    reject_text = cb.message.edit_text.call_args[0][0]
    reject_kb = cb.message.edit_text.call_args[1].get("reply_markup")
    assert "Confirm Payment Rejection" in reject_text
    reject_confirm_cbs = [btn.callback_data for row in reject_kb.inline_keyboard for btn in row]
    assert f"admin:pay_reject_confirm:{payment.id}" in reject_confirm_cbs
    assert f"admin:pay_detail:{payment.id}" in reject_confirm_cbs


# Bot-chat tutorial management handlers were retired in Round 13 Part K (tutorials live in Mini App).


@pytest.mark.asyncio
async def test_mandatory_channel_and_group_gate_strictness_and_unlock(db_session):
    """Users must join BOTH Channel and Group; unlocking never awards a welcome bonus."""
    settings.required_channel_id = "-100111222"
    settings.required_group_id = "-100333444"
    settings.welcome_bonus_seconds = 0

    user, _ = await get_or_create_user(db_session, telegram_id=777001)
    assert user.channel_verified is False
    assert user.group_verified is False
    assert user.welcome_bonus_awarded is False

    bot_mock = AsyncMock()

    # Case A: User in Channel only, missing Group
    def side_effect_ch_only(chat_id, user_id):
        m = MagicMock()
        if str(chat_id) == str(settings.required_channel_id):
            m.status = "member"
        else:
            m.status = "left"
        return m

    bot_mock.get_chat_member.side_effect = side_effect_ch_only
    ok_a, reason_a = await verify_user_membership(db_session, user, bot_mock)
    assert ok_a is False
    assert reason_a == "channel_or_group_missing"
    assert user.channel_verified is True
    assert user.group_verified is False

    # Case B: User in Group only, missing Channel
    def side_effect_gp_only(chat_id, user_id):
        m = MagicMock()
        if str(chat_id) == str(settings.required_group_id):
            m.status = "member"
        else:
            m.status = "left"
        return m

    bot_mock.get_chat_member.side_effect = side_effect_gp_only
    ok_b, reason_b = await verify_user_membership(db_session, user, bot_mock)
    assert ok_b is False
    assert reason_b == "channel_or_group_missing"
    assert user.channel_verified is False
    assert user.group_verified is True

    # Case C: User in Neither
    def side_effect_neither(chat_id, user_id):
        m = MagicMock()
        m.status = "left"
        return m

    bot_mock.get_chat_member.side_effect = side_effect_neither
    ok_c, reason_c = await verify_user_membership(db_session, user, bot_mock)
    assert ok_c is False
    assert reason_c == "channel_or_group_missing"

    # Case D: User joins BOTH -> Gate passes and unlocks
    def side_effect_both(chat_id, user_id):
        m = MagicMock()
        m.status = "member"
        return m

    bot_mock.get_chat_member.side_effect = side_effect_both
    ok_d, reason_d = await verify_user_membership(db_session, user, bot_mock)
    assert ok_d is True
    assert reason_d == "verified"
    assert user.channel_verified is True
    assert user.group_verified is True

    # Crucial Rule 3 Check: Zero welcome bonus awarded
    assert user.welcome_bonus_awarded is False
    bal = await get_minute_balance(db_session, user.id)
    assert bal.welcome_seconds == 0


@pytest.mark.asyncio
async def test_repeated_start_and_menu_idempotency_no_duplicate_allowance(db_session):
    """Repeated /start and menu renders grant exactly 180s daily once; duplicate calls safely no-op."""
    user, is_new = await get_or_create_user(db_session, telegram_id=777002)
    current_day = get_current_daily_period()

    # First interaction grants 180s daily free allowance (Limits round 2026-11-23)
    text1, kb1 = await render_main_menu(user.id, "en", session=db_session)
    bal1 = await get_minute_balance(db_session, user.id)
    assert bal1.daily_free_seconds == 180
    assert bal1.total_usable_seconds == 180

    # Repeated calls 5 times safely no-op
    for _ in range(5):
        granted, bal = await grant_daily_free_allowance(db_session, user.id, day=current_day)
        assert granted is False
        assert bal.daily_free_seconds == 180
        assert bal.total_usable_seconds == 180

    # Verify exactly 1 DAILY_FREE ledger event exists
    ledger_events = (await db_session.execute(
        select(MinuteLedgerEvent).where(
            MinuteLedgerEvent.user_id == user.id,
            MinuteLedgerEvent.source == LedgerSource.DAILY_FREE.value,
        )
    )).scalars().all()
    assert len(ledger_events) == 1
    assert ledger_events[0].seconds == 180


@pytest.mark.asyncio
async def test_later_daily_period_grants_new_allowance_once(db_session):
    """A subsequent daily period resets the 180-second non-accumulating allowance once (Limits round)."""
    user, _ = await get_or_create_user(db_session, telegram_id=777003)
    today_dt = datetime.datetime.now(datetime.timezone.utc).date()
    day_curr = today_dt.isoformat()
    day_next = (today_dt + datetime.timedelta(days=1)).isoformat()

    # Grant current day
    granted1, bal1 = await grant_daily_free_allowance(db_session, user.id, day=day_curr)
    assert granted1 is True
    assert bal1.daily_free_seconds == 180

    # Consume 70 seconds
    bal_after_use = await consume_seconds(db_session, user.id, seconds_to_consume=70, job_id="job-day-1")
    assert bal_after_use.daily_free_seconds == 110

    # Next day arrives -> resets to 180 (non-accumulating)
    granted2, bal2 = await grant_daily_free_allowance(db_session, user.id, day=day_next)
    assert granted2 is True
    assert bal2.daily_free_seconds == 180
    assert bal2.last_daily_grant_date == day_next

    # Repeated call on next day returns False
    granted_dup, bal_dup = await grant_daily_free_allowance(db_session, user.id, day=day_next)
    assert granted_dup is False
    assert bal_dup.daily_free_seconds == 180


@pytest.mark.asyncio
async def test_admin_unlimited_generation_no_deduction_and_audit_logging(db_session, tmp_path):
    """Admin generation access is unlimited without deductions, and audit trail is logged."""
    settings.admin_ids_raw = "888111"
    admin_user, _ = await get_or_create_user(db_session, telegram_id=888111)

    # Admin skips monthly allowance grant
    granted, bal = await grant_monthly_free_allowance(db_session, admin_user.id)
    assert granted is False
    assert bal.monthly_free_seconds == 0
    assert bal.total_usable_seconds == 0

    # Create active model
    pth = tmp_path / "admin_test.pth"
    pth.write_bytes(b"content")
    model = VoiceModel(
        name="AdminModel",
        slug="admin-model",
        type="SYSTEM",
        model_dir=str(tmp_path),
        pth_path=str(pth),
        status="ACTIVE",
        is_default_free=True,
    )
    db_session.add(model)
    await db_session.flush()

    # Admin creates voice clone job with 0 balance
    job, reason = await create_voice_clone_job(
        session=db_session,
        user_id=admin_user.id,
        text="Admin prompt test",
        edge_voice="my-MM-ThihaNeural",
        model_id=model.id,
    )
    assert job is not None
    assert reason == "QUEUED"

    # Complete job and consume 45 seconds (admin bypass)
    bal_after_job = await consume_seconds(db_session, user_id=admin_user.id, seconds_to_consume=45, job_id=job.id)
    assert bal_after_job.monthly_free_seconds == 0
    assert bal_after_job.purchased_seconds == 0
    assert bal_after_job.consumed_seconds == 0  # Not deducted!

    # No deduction ledger event created for admin
    events = (await db_session.execute(
        select(MinuteLedgerEvent).where(
            MinuteLedgerEvent.user_id == admin_user.id,
            MinuteLedgerEvent.direction == LedgerDirection.DEBIT.value,
        )
    )).scalars().all()
    assert len(events) == 0


@pytest.mark.asyncio
async def test_purchased_balance_and_ledger_history_immutability(db_session):
    """Purchased balance and full ledger history remain strictly intact across operations."""
    user, _ = await get_or_create_user(db_session, telegram_id=777005)
    await grant_daily_free_allowance(db_session, user.id)

    # Purchase 10 minutes (600s)
    await credit_purchased_minutes(db_session, user.id, minutes=10, payment_id="PAY-AUDIT-100")
    assert user.account_category == AccountCategory.PURCHASED.value

    bal = await get_minute_balance(db_session, user.id)
    assert bal.daily_free_seconds == 180
    assert bal.purchased_seconds == 600
    assert bal.total_usable_seconds == 780

    # Consume 200s: 180s from daily_free, 20s from purchased
    bal_after = await consume_seconds(db_session, user.id, seconds_to_consume=200, job_id="job-purch-1")
    assert bal_after.daily_free_seconds == 0
    assert bal_after.purchased_seconds == 580
    assert bal_after.total_usable_seconds == 580
    assert user.account_category == AccountCategory.PURCHASED.value

    # Verify all ledger events exist and match exactly
    events = (await db_session.execute(
        select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == user.id).order_by(MinuteLedgerEvent.id)
    )).scalars().all()
    assert len(events) == 4
    assert events[0].source == LedgerSource.DAILY_FREE.value and events[0].direction == LedgerDirection.CREDIT.value and events[0].seconds == 180
    assert events[1].source == LedgerSource.PURCHASE.value and events[1].direction == LedgerDirection.CREDIT.value and events[1].seconds == 600 and events[1].reference_id == "PAY-AUDIT-100"
    assert events[2].source == LedgerSource.DAILY_FREE.value and events[2].direction == LedgerDirection.DEBIT.value and events[2].seconds == 180 and events[2].reference_id == "job-purch-1"
    assert events[3].source == LedgerSource.PURCHASE.value and events[3].direction == LedgerDirection.DEBIT.value and events[3].seconds == 20 and events[3].reference_id == "job-purch-1"


@pytest.mark.asyncio
async def test_full_onboarding_flow_zero_welcome_bonus_strict_180s(db_session):
    """End-to-end regression test for the exact onboarding flow:
    /start -> choose language -> channel & group verification -> welcome menu.
    Proves:
    1. Zero welcome bonus is granted (balance.welcome_seconds == 0, welcome_bonus_awarded == False).
    2. Exactly 180s (3.0m) daily free allowance is granted once (Limits round).
    3. Total usable balance is strictly 180s (NOT 480s / 8.0m).
    4. Channel and group membership gate is strictly enforced.
    5. Flow is completely idempotent against repeated /start, repeated gate verification, and repeated menu rendering.
    """
    settings.welcome_bonus_seconds = 0
    settings.free_daily_allowance_seconds = 180
    settings.required_channel_id = "-1001111111"
    settings.required_group_id = "-1002222222"

    test_user_id = 99881122

    @asynccontextmanager
    async def mock_session_ctx():
        yield db_session

    bot_mock = AsyncMock()

    # Step 1: User sends /start
    cmd_mock = MagicMock()
    cmd_mock.args = None
    start_msg = MagicMock(spec=Message)
    start_msg.from_user = TgUser(id=test_user_id, is_bot=False, first_name="OnboardingTester", username="onboard_test")
    start_msg.bot = bot_mock
    start_msg.answer = AsyncMock()

    with patch("app.handlers.start.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await handle_start(start_msg, cmd_mock)

    start_msg.answer.assert_called_once()
    assert "Welcome! Please choose your preferred language" in start_msg.answer.call_args[0][0]

    # Verify user row was created in DB with unverified gate status and zero welcome bonus
    user = await db_session.get(User, test_user_id)
    assert user is not None
    assert user.channel_verified is False
    assert user.group_verified is False
    assert user.welcome_bonus_awarded is False
    assert user.account_category == AccountCategory.FREE.value

    # Balance at this point is completely 0
    bal_initial = await get_minute_balance(db_session, test_user_id)
    assert bal_initial.welcome_seconds == 0
    assert bal_initial.daily_free_seconds == 0
    assert bal_initial.total_usable_seconds == 0

    # Step 2: User selects language ("my") before joining channels/groups (Round 13 Part K: delete picker + send gate)
    left_member = MagicMock()
    left_member.status = "left"
    bot_mock.get_chat_member.return_value = left_member

    lang_cb = MagicMock(spec=CallbackQuery)
    lang_cb.from_user = TgUser(id=test_user_id, is_bot=False, first_name="OnboardingTester")
    lang_cb.bot = bot_mock
    lang_cb.data = "set_lang:my"
    lang_cb.message = MagicMock(spec=Message)
    lang_cb.message.delete = AsyncMock()
    lang_cb.message.answer = AsyncMock()
    lang_cb.answer = AsyncMock()

    with patch("app.handlers.start.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await handle_set_language(lang_cb)

    lang_cb.message.delete.assert_awaited_once()
    lang_cb.message.answer.assert_awaited_once()
    gate_kb = lang_cb.message.answer.call_args[1].get("reply_markup")
    gate_cb_datas = [btn.callback_data for row in gate_kb.inline_keyboard for btn in row if btn.callback_data]
    assert "gate:verify" in gate_cb_datas

    # Re-verify in DB: user language updated, still unverified, still zero welcome bonus
    await db_session.refresh(user)
    assert user.language_code == "my"
    assert user.channel_verified is False
    assert user.group_verified is False
    assert user.welcome_bonus_awarded is False

    # Step 3: User joins Channel and Group, then taps "Verify" (gate:verify)
    joined_member = MagicMock()
    joined_member.status = "member"
    bot_mock.get_chat_member.return_value = joined_member

    verify_cb = MagicMock(spec=CallbackQuery)
    verify_cb.from_user = TgUser(id=test_user_id, is_bot=False, first_name="OnboardingTester")
    verify_cb.bot = bot_mock
    verify_cb.data = "gate:verify"
    verify_cb.message = MagicMock(spec=Message)
    verify_cb.message.delete = AsyncMock()
    verify_cb.message.answer = AsyncMock()
    verify_cb.answer = AsyncMock()

    with patch("app.handlers.start.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await handle_gate_verify(verify_cb)

    verify_cb.answer.assert_awaited_once()
    verify_cb.message.delete.assert_awaited_once()
    verify_cb.message.answer.assert_awaited_once()

    # Step 4: Strict state and ledger verification
    await db_session.refresh(user)
    assert user.channel_verified is True
    assert user.group_verified is True
    assert user.welcome_bonus_awarded is False  # MUST BE FALSE! ZERO welcome bonus!

    bal = await get_minute_balance(db_session, test_user_id)
    assert bal.welcome_seconds == 0  # ZERO welcome seconds!
    assert bal.daily_free_seconds == 180  # Exactly 180 seconds daily free
    assert bal.purchased_seconds == 0
    assert bal.total_usable_seconds == 180  # Exactly 180s (3.0 minutes, strictly NOT 480s / 8.0m)
    assert bal.total_usable_minutes == 3.0

    # Verify ledger events: exactly 1 DAILY_FREE credit event, ZERO WELCOME events
    events = (await db_session.execute(
        select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == test_user_id)
    )).scalars().all()
    assert len(events) == 1
    assert events[0].source == LedgerSource.DAILY_FREE.value
    assert events[0].direction == LedgerDirection.CREDIT.value
    assert events[0].seconds == 180

    # Verify no WelcomeBonusAward row exists
    award_rows = (await db_session.execute(
        select(WelcomeBonusAward).where(WelcomeBonusAward.user_id == test_user_id)
    )).scalars().all()
    assert len(award_rows) == 0

    # Step 5: Verify idempotency across repeated actions
    start_msg_repeat = MagicMock(spec=Message)
    start_msg_repeat.from_user = TgUser(id=test_user_id, is_bot=False, first_name="OnboardingTester", username="onboard_test")
    start_msg_repeat.bot = bot_mock
    start_msg_repeat.answer = AsyncMock()

    with patch("app.handlers.start.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await handle_start(start_msg_repeat, cmd_mock)

    verify_cb_repeat = MagicMock(spec=CallbackQuery)
    verify_cb_repeat.from_user = TgUser(id=test_user_id, is_bot=False, first_name="OnboardingTester")
    verify_cb_repeat.bot = bot_mock
    verify_cb_repeat.data = "gate:verify"
    verify_cb_repeat.message = MagicMock(spec=Message)
    verify_cb_repeat.message.delete = AsyncMock()
    verify_cb_repeat.message.answer = AsyncMock()
    verify_cb_repeat.answer = AsyncMock()

    with patch("app.handlers.start.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await handle_gate_verify(verify_cb_repeat)

    with patch("app.handlers.menu.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await render_main_menu(test_user_id, "my")

    # Final balance check: STILL strictly 180s, zero welcome bonus, still exactly 1 ledger event
    bal_final = await get_minute_balance(db_session, test_user_id)
    assert bal_final.welcome_seconds == 0
    assert bal_final.daily_free_seconds == 180
    assert bal_final.total_usable_seconds == 180
    assert bal_final.total_usable_minutes == 3.0

    events_final = (await db_session.execute(
        select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == test_user_id)
    )).scalars().all()
    assert len(events_final) == 1
    assert events_final[0].source == LedgerSource.DAILY_FREE.value

