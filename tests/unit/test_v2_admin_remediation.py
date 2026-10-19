from __future__ import annotations

from contextlib import asynccontextmanager
import datetime
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, User as TgUser
from sqlalchemy import select

from app.config import settings
from app.handlers.admin import (
    evaluate_job_retry_eligibility,
    handle_admin_job_detail,
    handle_admin_job_retry_confirm,
    handle_admin_job_retry_prompt,
    handle_admin_pay_approve_confirm,
    handle_admin_pay_approve_prompt,
    handle_admin_pay_reject_confirm,
    handle_admin_pay_reject_prompt,
    handle_admin_settings,
    handle_admin_tutorial,
    handle_admin_tutorial_toggle,
    handle_admin_tutorial_url_input,
    handle_admin_tutorial_remove_confirm,
    handle_admin_user_ledger,
)
from app.handlers.menu import handle_menu_tutorial, render_main_menu
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
    get_current_monthly_period,
    get_minute_balance,
    get_or_create_user,
    grant_monthly_free_allowance,
)
from app.services.queue import create_voice_clone_job


@pytest.mark.asyncio
async def test_free_user_welcome_bonus_default_zero(db_session):
    """Under V2 default settings, welcome_bonus_seconds is 0. New Free users receive strictly 180s."""
    assert settings.welcome_bonus_seconds == 0

    user, _ = await get_or_create_user(db_session, telegram_id=987654321)
    await grant_monthly_free_allowance(db_session, user.id)

    # Calling award_welcome_bonus directly with 0s returns False
    ok, bal = await award_welcome_bonus(db_session, user.id, seconds=settings.welcome_bonus_seconds)
    assert ok is False
    assert bal.welcome_seconds == 0
    assert bal.monthly_free_seconds == 180
    assert bal.purchased_seconds == 0
    assert bal.total_usable_seconds == 180  # Exactly 3.0 minutes, NOT 8.0 minutes!

    # Gate verification also must not award welcome bonus when welcome_bonus_seconds == 0
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
async def test_admin_settings_and_policies_canonical_panel(db_session):
    """admin:settings and alias admin:policies render combined infrastructure and policy details."""
    settings.admin_ids_raw = "888111"
    admin_user, _ = await get_or_create_user(db_session, telegram_id=888111)

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=888111, is_bot=False, first_name="Admin")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    # Test canonical callback
    cb.data = "admin:settings"
    await handle_admin_settings(cb, session=db_session)

    cb.message.edit_text.assert_called_once()
    rendered_text = cb.message.edit_text.call_args[0][0]
    kb = cb.message.edit_text.call_args[1].get("reply_markup")

    assert "Bot Runtime Settings & System Policies" in rendered_text
    assert "Infrastructure & Runtime:" in rendered_text
    assert "Free Tier Policy:" in rendered_text
    assert "Purchased Tier Policy:" in rendered_text
    assert "Minute Packages:" in rendered_text
    assert "Deduction & Billing Rules:" in rendered_text

    # Verify buttons
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "admin:settings" in callbacks
    assert "admin:dashboard" in callbacks


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


@pytest.mark.asyncio
async def test_admin_tutorial_crud_and_user_menu_sync(db_session):
    """Guide video CRUD via BotSetting updates user-facing tutorial behavior dynamically."""
    settings.admin_ids_raw = "888111"
    admin_user, _ = await get_or_create_user(db_session, telegram_id=888111)

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=888111, is_bot=False, first_name="Admin")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()

    @asynccontextmanager
    async def mock_session_ctx():
        yield db_session

    # 1. Initially enabled with default link
    await handle_admin_tutorial(cb, session=db_session)
    admin_text = cb.message.edit_text.call_args[0][0]
    assert "Tutorial Manager" in admin_text
    assert "Enabled" in admin_text

    # User menu sees the video link
    target_user, _ = await get_or_create_user(db_session, telegram_id=333001)
    target_user.language_code = "en"
    await db_session.flush()

    user_cb = MagicMock(spec=CallbackQuery)
    user_cb.from_user = TgUser(id=333001, is_bot=False, first_name="NormalUser")
    user_cb.message = MagicMock(spec=Message)
    user_cb.message.edit_text = AsyncMock()
    user_cb.answer = AsyncMock()

    await handle_menu_tutorial(user_cb, session=db_session)
    user_kb = user_cb.message.edit_text.call_args[1].get("reply_markup")
    assert any(btn.url == settings.tutorial_video_link for row in user_kb.inline_keyboard for btn in row)

    # 2. Toggle disabled
    with patch("app.handlers.admin.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await handle_admin_tutorial_toggle(cb)

    # Now user menu shows unavailable
    await handle_menu_tutorial(user_cb, session=db_session)
    user_unavailable_text = user_cb.message.edit_text.call_args[0][0]
    user_kb2 = user_cb.message.edit_text.call_args[1].get("reply_markup")
    assert "updated" in user_unavailable_text.lower() or "temporarily" in user_unavailable_text.lower()
    # No external URL button
    assert not any(btn.url for row in user_kb2.inline_keyboard for btn in row)

    # 3. Toggle back to enabled
    with patch("app.handlers.admin.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await handle_admin_tutorial_toggle(cb)

    # 4. Remove URL
    with patch("app.handlers.admin.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await handle_admin_tutorial_remove_confirm(cb)

    # With empty URL, user menu also shows unavailable
    await handle_menu_tutorial(user_cb, session=db_session)
    user_empty_url_text = user_cb.message.edit_text.call_args[0][0]
    assert "updated" in user_empty_url_text.lower() or "temporarily" in user_empty_url_text.lower()


@pytest.mark.asyncio
async def test_admin_job_retry_eligibility_classification(db_session):
    """Failure classification correctly distinguishes non-retryable user/policy errors from retryable system errors."""
    u, _ = await get_or_create_user(db_session, telegram_id=777888)
    u.status = UserStatus.ACTIVE.value
    await db_session.flush()

    # 1. Non-retryable policy error: insufficient balance
    j_bal = Job(id="job-bal-fail", user_id=u.id, status=JobStatus.FAILED.value, error_message="INSUFFICIENT_BALANCE: Need 60s, have 10s")
    can_retry, cat, reason = evaluate_job_retry_eligibility(j_bal, u)
    assert can_retry is False
    assert cat == "POLICY_USER_ERROR"
    assert "INSUFFICIENT_BALANCE" in reason

    # 2. Non-retryable policy error: model not authorized
    j_model = Job(id="job-model-fail", user_id=u.id, status=JobStatus.FAILED.value, error_message="MODEL_NOT_AUTHORIZED: Model 5 is purchased only")
    can_retry, cat, reason = evaluate_job_retry_eligibility(j_model, u)
    assert can_retry is False
    assert cat == "POLICY_USER_ERROR"

    # 3. Non-retryable user error: text limit exceeded
    j_text = Job(id="job-text-fail", user_id=u.id, status=JobStatus.FAILED.value, error_message="TEXT_EXCEEDS_FREE_LIMIT: 250 > 100")
    can_retry, cat, reason = evaluate_job_retry_eligibility(j_text, u)
    assert can_retry is False
    assert cat == "POLICY_USER_ERROR"

    # 4. Inactive/suspended user
    u_blocked, _ = await get_or_create_user(db_session, telegram_id=999111)
    u_blocked.status = UserStatus.BLOCKED.value
    await db_session.flush()

    j_sys_blocked = Job(id="job-sys-blocked", user_id=u_blocked.id, status=JobStatus.FAILED.value, error_message="Applio worker crash")
    can_retry, cat, reason = evaluate_job_retry_eligibility(j_sys_blocked, u_blocked)
    assert can_retry is False
    assert cat == "USER_INACTIVE"

    # 5. Retryable transient system error on active user
    j_transient = Job(id="job-transient-1", user_id=u.id, status=JobStatus.FAILED.value, error_message="Applio inference failed (exit 1): DLL load error")
    can_retry, cat, reason = evaluate_job_retry_eligibility(j_transient, u)
    assert can_retry is True
    assert cat == "SYSTEM_FAILURE"


@pytest.mark.asyncio
async def test_admin_job_retry_execution(db_session):
    """Admin job retry confirms revalidation, resets status to QUEUED, and logs audit record."""
    settings.admin_ids_raw = "888111"
    admin_user, _ = await get_or_create_user(db_session, telegram_id=888111)

    target_user, _ = await get_or_create_user(db_session, telegram_id=888222)
    target_user.status = UserStatus.ACTIVE.value
    await db_session.flush()

    failed_job = Job(
        id="job-retryable-99",
        user_id=target_user.id,
        status=JobStatus.FAILED.value,
        error_message="Applio worker timeout after 180s",
    )
    db_session.add(failed_job)
    await db_session.flush()

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=888111, is_bot=False, first_name="Admin")
    cb.data = f"admin:job_retry_confirm:{failed_job.id}"
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    await handle_admin_job_retry_confirm(cb, session=db_session)

    # Job is now QUEUED and error cleared
    await db_session.refresh(failed_job)
    assert failed_job.status == JobStatus.QUEUED.value
    assert failed_job.error_message is None

    # Alert sent
    assert any("re-queued" in str(c).lower() for c in cb.answer.call_args_list)


@pytest.mark.asyncio
async def test_admin_user_ledger_history_display(db_session):
    """Admin can inspect user ledger history with chronological entries, amounts, directions, and notes."""
    settings.admin_ids_raw = "888111"
    admin_user, _ = await get_or_create_user(db_session, telegram_id=888111)

    target_user, _ = await get_or_create_user(db_session, telegram_id=654321)
    ev1 = MinuteLedgerEvent(
        user_id=target_user.id,
        direction="CREDIT",
        source="MONTHLY_FREE",
        seconds=180,
        note="Monthly allowance grant",
    )
    ev2 = MinuteLedgerEvent(
        user_id=target_user.id,
        direction="DEBIT",
        source="CONSUME",
        seconds=30,
        reference_id="job-test-11",
        note="Deduction for voice generation job",
    )
    db_session.add_all([ev1, ev2])
    await db_session.flush()

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=888111, is_bot=False, first_name="Admin")
    cb.data = f"admin:user_ledger:{target_user.id}:1"
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    await handle_admin_user_ledger(cb, session=db_session)

    cb.message.edit_text.assert_called_once()
    rendered_text = cb.message.edit_text.call_args[0][0]
    assert f"Ledger History for User <code>{target_user.id}</code>" in rendered_text
    assert "CREDIT" in rendered_text
    assert "DEBIT" in rendered_text
    assert "MONTHLY_FREE" in rendered_text
    assert "CONSUME" in rendered_text
    assert "job-test-11" in rendered_text


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
    """Repeated /start and menu renders grant exactly 180s once; duplicate calls safely no-op."""
    user, is_new = await get_or_create_user(db_session, telegram_id=777002)
    current_period = get_current_monthly_period()

    # First interaction grants 180s
    text1, kb1 = await render_main_menu(user.id, "en", session=db_session)
    bal1 = await get_minute_balance(db_session, user.id)
    assert bal1.monthly_free_seconds == 180
    assert bal1.total_usable_seconds == 180

    # Repeated calls 5 times (simulating user tapping /start or menu buttons repeatedly)
    for _ in range(5):
        granted, bal = await grant_monthly_free_allowance(db_session, user.id, period=current_period)
        assert granted is False
        assert bal.monthly_free_seconds == 180
        assert bal.total_usable_seconds == 180

    # Verify only 1 grant row exists in DB
    grants = (await db_session.execute(
        select(MonthlyFreeAllowanceGrant).where(
            MonthlyFreeAllowanceGrant.user_id == user.id,
            MonthlyFreeAllowanceGrant.period == current_period,
        )
    )).scalars().all()
    assert len(grants) == 1
    assert grants[0].seconds == 180

    # Verify exactly 1 ledger event exists for this allowance
    ledger_events = (await db_session.execute(
        select(MinuteLedgerEvent).where(
            MinuteLedgerEvent.user_id == user.id,
            MinuteLedgerEvent.source == LedgerSource.MONTHLY_FREE.value,
        )
    )).scalars().all()
    assert len(ledger_events) == 1
    assert ledger_events[0].seconds == 180


@pytest.mark.asyncio
async def test_later_monthly_period_grants_new_allowance_once(db_session):
    """A subsequent monthly period replenishes the 180-second allowance once."""
    user, _ = await get_or_create_user(db_session, telegram_id=777003)
    period_curr = "2026-10"
    period_next = "2026-11"

    # Grant current period
    granted1, bal1 = await grant_monthly_free_allowance(db_session, user.id, period=period_curr)
    assert granted1 is True
    assert bal1.monthly_free_seconds == 180

    # Consume 70 seconds
    bal_after_use = await consume_seconds(db_session, user.id, seconds_to_consume=70, job_id="job-month-1")
    assert bal_after_use.monthly_free_seconds == 110

    # Next month arrives
    granted2, bal2 = await grant_monthly_free_allowance(db_session, user.id, period=period_next)
    assert granted2 is True
    assert bal2.monthly_free_seconds == 180
    assert bal2.last_monthly_grant_period == period_next

    # Repeated call in next month returns False
    granted_dup, bal_dup = await grant_monthly_free_allowance(db_session, user.id, period=period_next)
    assert granted_dup is False
    assert bal_dup.monthly_free_seconds == 180


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
    await grant_monthly_free_allowance(db_session, user.id)

    # Purchase 10 minutes (600s)
    await credit_purchased_minutes(db_session, user.id, minutes=10, payment_id="PAY-AUDIT-100")
    assert user.account_category == AccountCategory.PURCHASED.value

    bal = await get_minute_balance(db_session, user.id)
    assert bal.monthly_free_seconds == 180
    assert bal.purchased_seconds == 600
    assert bal.total_usable_seconds == 780

    # Consume 200s: 180s from monthly_free, 20s from purchased
    bal_after = await consume_seconds(db_session, user.id, seconds_to_consume=200, job_id="job-purch-1")
    assert bal_after.monthly_free_seconds == 0
    assert bal_after.purchased_seconds == 580
    assert bal_after.total_usable_seconds == 580
    assert user.account_category == AccountCategory.PURCHASED.value

    # Verify all ledger events exist and match exactly
    events = (await db_session.execute(
        select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == user.id).order_by(MinuteLedgerEvent.id)
    )).scalars().all()
    assert len(events) == 4
    assert events[0].source == LedgerSource.MONTHLY_FREE.value and events[0].direction == LedgerDirection.CREDIT.value and events[0].seconds == 180
    assert events[1].source == LedgerSource.PURCHASE.value and events[1].direction == LedgerDirection.CREDIT.value and events[1].seconds == 600 and events[1].reference_id == "PAY-AUDIT-100"
    assert events[2].source == LedgerSource.MONTHLY_FREE.value and events[2].direction == LedgerDirection.DEBIT.value and events[2].seconds == 180 and events[2].reference_id == "job-purch-1"
    assert events[3].source == LedgerSource.PURCHASE.value and events[3].direction == LedgerDirection.DEBIT.value and events[3].seconds == 20 and events[3].reference_id == "job-purch-1"


@pytest.mark.asyncio
async def test_full_onboarding_flow_zero_welcome_bonus_strict_180s(db_session):
    """End-to-end regression test for the exact onboarding flow:
    /start -> choose language -> channel & group verification -> main menu.
    Proves:
    1. Zero welcome bonus is granted (balance.welcome_seconds == 0, welcome_bonus_awarded == False).
    2. Exactly 180s (3.0m) monthly free allowance is granted once.
    3. Total usable balance is strictly 180s (NOT 480s / 8.0m).
    4. Channel and group membership gate is strictly enforced.
    5. Flow is completely idempotent against repeated /start, repeated gate verification, and repeated menu rendering.
    """
    settings.welcome_bonus_seconds = 0
    settings.free_monthly_allowance_seconds = 180
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
    assert bal_initial.monthly_free_seconds == 0
    assert bal_initial.total_usable_seconds == 0

    # Step 2: User selects language ("my") before joining channels/groups
    # Bot mock reports user has NOT joined yet ("left")
    left_member = MagicMock()
    left_member.status = "left"
    bot_mock.get_chat_member.return_value = left_member

    lang_cb = MagicMock(spec=CallbackQuery)
    lang_cb.from_user = TgUser(id=test_user_id, is_bot=False, first_name="OnboardingTester")
    lang_cb.bot = bot_mock
    lang_cb.data = "set_lang:my"
    lang_cb.message = MagicMock(spec=Message)
    lang_cb.message.edit_text = AsyncMock()
    lang_cb.answer = AsyncMock()

    with patch("app.handlers.start.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await handle_set_language(lang_cb)

    # Gate blocked user: renders membership gate prompt
    lang_cb.message.edit_text.assert_called_once()
    gate_kb = lang_cb.message.edit_text.call_args[1].get("reply_markup")
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
    verify_cb.message.edit_text = AsyncMock()
    verify_cb.answer = AsyncMock()

    with patch("app.handlers.start.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await handle_gate_verify(verify_cb)

    verify_cb.answer.assert_called_once()
    verify_cb.message.edit_text.assert_called_once()

    # Step 4: Strict state and ledger verification
    await db_session.refresh(user)
    assert user.channel_verified is True
    assert user.group_verified is True
    assert user.welcome_bonus_awarded is False  # MUST BE FALSE! ZERO welcome bonus!

    bal = await get_minute_balance(db_session, test_user_id)
    assert bal.welcome_seconds == 0  # ZERO welcome seconds!
    assert bal.monthly_free_seconds == 180  # Exactly 180 seconds
    assert bal.purchased_seconds == 0
    assert bal.total_usable_seconds == 180  # Exactly 180s (3.0 minutes, strictly NOT 480s / 8.0m)
    assert bal.total_usable_minutes == 3.0

    # Verify ledger events: exactly 1 MONTHLY_FREE credit event, ZERO WELCOME events
    events = (await db_session.execute(
        select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == test_user_id)
    )).scalars().all()
    assert len(events) == 1
    assert events[0].source == LedgerSource.MONTHLY_FREE.value
    assert events[0].direction == LedgerDirection.CREDIT.value
    assert events[0].seconds == 180

    # Verify no WelcomeBonusAward row exists
    award_rows = (await db_session.execute(
        select(WelcomeBonusAward).where(WelcomeBonusAward.user_id == test_user_id)
    )).scalars().all()
    assert len(award_rows) == 0

    # Step 5: Verify idempotency across repeated actions
    # Repeated /start
    start_msg_repeat = MagicMock(spec=Message)
    start_msg_repeat.from_user = TgUser(id=test_user_id, is_bot=False, first_name="OnboardingTester", username="onboard_test")
    start_msg_repeat.bot = bot_mock
    start_msg_repeat.answer = AsyncMock()

    with patch("app.handlers.start.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await handle_start(start_msg_repeat, cmd_mock)

    # Repeated gate:verify
    verify_cb_repeat = MagicMock(spec=CallbackQuery)
    verify_cb_repeat.from_user = TgUser(id=test_user_id, is_bot=False, first_name="OnboardingTester")
    verify_cb_repeat.bot = bot_mock
    verify_cb_repeat.data = "gate:verify"
    verify_cb_repeat.message = MagicMock(spec=Message)
    verify_cb_repeat.message.edit_text = AsyncMock()
    verify_cb_repeat.answer = AsyncMock()

    with patch("app.handlers.start.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await handle_gate_verify(verify_cb_repeat)

    # Repeated render_main_menu
    with patch("app.handlers.menu.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await render_main_menu(test_user_id, "my")

    # Final balance check: STILL strictly 180s, zero welcome bonus, still exactly 1 ledger event
    bal_final = await get_minute_balance(db_session, test_user_id)
    assert bal_final.welcome_seconds == 0
    assert bal_final.monthly_free_seconds == 180
    assert bal_final.total_usable_seconds == 180
    assert bal_final.total_usable_minutes == 3.0

    events_final = (await db_session.execute(
        select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == test_user_id)
    )).scalars().all()
    assert len(events_final) == 1
    assert events_final[0].source == LedgerSource.MONTHLY_FREE.value

