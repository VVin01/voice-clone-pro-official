import datetime
from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram.types import CallbackQuery, Message, User as TgUser
from sqlalchemy import select

from app.config import settings
from app.handlers.admin import (
    handle_admin_job_detail,
    handle_admin_job_retry_confirm,
    handle_admin_job_retry_prompt,
    handle_admin_queue,
    handle_admin_queue_failures,
    handle_admin_queue_view,
)
from app.models.enums import AccountCategory, JobStatus, UserStatus
from app.models.schema import AuditLog, Job, User, VoiceModel
from app.services.ledger import get_or_create_user


def create_mock_callback(user_id: int, data: str) -> CallbackQuery:
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=user_id, is_bot=False, first_name="BossAdmin", username="bossadmin")
    cb.data = data
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    return cb


@pytest.mark.asyncio
async def test_admin_queue_overview_and_recent_activity(db_session):
    """Queue monitor renders metrics, recent activity stream with user and model info, and category filter buttons."""
    settings.admin_ids_raw = "888111"
    admin_user, _ = await get_or_create_user(db_session, telegram_id=888111)
    admin_user.language_code = "my"

    u1, _ = await get_or_create_user(db_session, telegram_id=777001)
    u1.username = "singer_user"
    u1.first_name = "Singer"

    vm1 = VoiceModel(
        name="BurmeseZawgyiVoice",
        slug="burmese-zawgyi-voice",
        model_dir="/models/zawgyi",
        pth_path="/models/zawgyi.pth",
        status="ACTIVE",
    )
    db_session.add(vm1)
    await db_session.flush()

    j_success = Job(
        id="job-succ-101",
        user_id=u1.id,
        model_id=vm1.id,
        status=JobStatus.SUCCESS.value,
        edge_voice="my-MM-ThihaNeural",
        seconds_consumed=15,
        input_text="မင်္ဂလာပါ ခင်ဗျာ သီချင်းဆိုပြပါမည်",
        created_at=datetime.datetime(2026, 10, 15, 10, 0, tzinfo=datetime.timezone.utc),
    )
    j_running = Job(
        id="job-run-102",
        user_id=u1.id,
        model_id=vm1.id,
        status=JobStatus.RUNNING.value,
        edge_voice="my-MM-ThihaNeural",
        seconds_estimated=20,
        input_text="လက်ရှိ အသံပြောင်းလဲနေပါသည်",
        created_at=datetime.datetime(2026, 10, 15, 10, 5, tzinfo=datetime.timezone.utc),
    )
    j_failed = Job(
        id="job-fail-103",
        user_id=u1.id,
        model_id=vm1.id,
        status=JobStatus.FAILED.value,
        error_message="VoxCPM2 inference process timed out after 180s",
        input_text="ကျရှုံးသွားသော စာသားဖြစ်ပါသည်",
        created_at=datetime.datetime(2026, 10, 15, 10, 10, tzinfo=datetime.timezone.utc),
    )
    db_session.add_all([j_success, j_running, j_failed])
    await db_session.flush()

    cb = create_mock_callback(user_id=888111, data="admin:queue")
    await handle_admin_queue(cb, session=db_session)

    cb.message.edit_text.assert_called_once()
    text = cb.message.edit_text.call_args[0][0]
    reply_markup = cb.message.edit_text.call_args[1]["reply_markup"]

    # Verify counts
    assert "လုပ်ငန်းစဉ်စောင့်ကြည့်စနစ် (Queue Monitor)" in text
    assert "• ✅ အောင်မြင်: <b>1</b>" in text
    assert "• ⚙️ လုပ်ဆောင်နေသည်: <b>1</b>" in text
    assert "• ❌ မအောင်မြင်: <b>1</b>" in text

    # Verify recent activity stream
    assert "🕒 <b>လတ်တလော အသုံးပြုမှုများ (Recent Activity):</b>" in text
    assert "@singer_user" in text
    assert "BurmeseZawgyiVoice" in text
    assert "မင်္ဂလာပါ ခင်ဗျာ" in text

    # Verify category buttons in keyboard
    all_callbacks = [b.callback_data for row in reply_markup.inline_keyboard for b in row]
    assert "admin:queue_view:running" in all_callbacks
    assert "admin:queue_view:queued" in all_callbacks
    assert "admin:queue_view:success" in all_callbacks
    assert "admin:queue_failures" in all_callbacks
    assert "admin:queue" in all_callbacks
    assert "admin:dashboard" in all_callbacks


@pytest.mark.asyncio
async def test_admin_queue_view_categories(db_session):
    """Admin can filter queue by category (running, queued, success) and click individual jobs."""
    settings.admin_ids_raw = "888111"
    admin_user, _ = await get_or_create_user(db_session, telegram_id=888111)
    admin_user.language_code = "my"

    u, _ = await get_or_create_user(db_session, telegram_id=777002)
    u.username = "test_user_cat"

    j_run = Job(
        id="job-view-run-1",
        user_id=u.id,
        status=JobStatus.RUNNING.value,
        edge_voice="my-MM-ThihaNeural",
        seconds_estimated=25,
        input_text="Working on it right now",
        created_at=datetime.datetime(2026, 10, 15, 10, 20, tzinfo=datetime.timezone.utc),
    )
    db_session.add(j_run)
    await db_session.flush()

    # 1. Inspect running category view
    cb_run = create_mock_callback(user_id=888111, data="admin:queue_view:running")
    await handle_admin_queue_view(cb_run, session=db_session)

    cb_run.message.edit_text.assert_called_once()
    text = cb_run.message.edit_text.call_args[0][0]
    kb = cb_run.message.edit_text.call_args[1]["reply_markup"]

    assert "လက်ရှိလုပ်ဆောင်နေဆဲ အလုပ်များ (1)" in text
    assert "@test_user_cat" in text
    assert "Working on it right now" in text

    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"admin:job_detail:{j_run.id}" in cbs
    assert "admin:queue_view:running" in cbs
    assert "admin:queue" in cbs

    # 2. Inspect queued category view (empty)
    cb_queued = create_mock_callback(user_id=888111, data="admin:queue_view:queued")
    await handle_admin_queue_view(cb_queued, session=db_session)
    cb_queued.message.edit_text.assert_called_once()
    q_text = cb_queued.message.edit_text.call_args[0][0]
    assert "တန်းစီစောင့်ဆိုင်းနေသော အလုပ်များ (0)" in q_text
    assert "လတ်တလော အလုပ်မှတ်တမ်း မရှိသေးပါ" in q_text


@pytest.mark.asyncio
async def test_admin_job_detail_burmese_diagnostics_and_retry(db_session):
    """System failure provides clear Burmese diagnostics and interactive retry button."""
    settings.admin_ids_raw = "888111"
    admin_user, _ = await get_or_create_user(db_session, telegram_id=888111)
    admin_user.language_code = "my"

    target_user, _ = await get_or_create_user(db_session, telegram_id=777003)
    target_user.username = "artist_khant"
    target_user.first_name = "Khant"
    target_user.status = UserStatus.ACTIVE.value
    target_user.account_category = AccountCategory.PURCHASED.value
    target_user.balance_seconds = 600

    vm = VoiceModel(
        name="AungKhantStudio",
        slug="aung-khant-studio",
        model_dir="/models/aungkhant",
        pth_path="/models/aungkhant.pth",
        status="ACTIVE",
    )
    db_session.add(vm)
    await db_session.flush()

    sys_failed_job = Job(
        id="job-sys-fail-99",
        user_id=target_user.id,
        model_id=vm.id,
        status=JobStatus.FAILED.value,
        error_message="VoxCPM2 inference process timed out after 180s (GPU memory spike)",
        input_text="မင်္ဂလာပါ လူကြီးမင်းတို့ခင်ဗျာ",
        created_at=datetime.datetime(2026, 10, 15, 10, 30, tzinfo=datetime.timezone.utc),
    )
    db_session.add(sys_failed_job)
    await db_session.flush()

    cb = create_mock_callback(user_id=888111, data=f"admin:job_detail:{sys_failed_job.id}")
    await handle_admin_job_detail(cb, session=db_session)

    cb.message.edit_text.assert_called_once()
    text = cb.message.edit_text.call_args[0][0]
    kb = cb.message.edit_text.call_args[1]["reply_markup"]

    # Verify diagnostics & Burmese explanations
    assert "Job Failure Diagnostics" in text
    assert "AungKhantStudio" in text
    assert "Khant (@artist_khant)" in text
    assert "💎 PURCHASED" in text
    assert "ဆာဗာ / AI စနစ်ပိုင်းဆိုင်ရာ ယာယီချို့ယွင်းချက် (System Error)" in text
    assert "Retry ပြန်လည်လုပ်ဆောင်နိုင်ပါသည်" in text

    # Verify retry button is rendered
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"admin:job_retry_prompt:{sys_failed_job.id}" in cbs
    assert "admin:queue" in cbs
    assert "admin:queue_failures" in cbs


@pytest.mark.asyncio
async def test_admin_job_detail_policy_error_ineligible(db_session):
    """User/policy failure explains that retry is not available due to policy constraint."""
    settings.admin_ids_raw = "888111"
    admin_user, _ = await get_or_create_user(db_session, telegram_id=888111)

    target_user, _ = await get_or_create_user(db_session, telegram_id=777004)
    target_user.status = UserStatus.ACTIVE.value

    policy_failed_job = Job(
        id="job-policy-fail-88",
        user_id=target_user.id,
        status=JobStatus.FAILED.value,
        error_message="INSUFFICIENT_BALANCE: Required 60s, available 10s",
        input_text="လက်ကျန်မလုံလောက်သော စာသား",
        created_at=datetime.datetime(2026, 10, 15, 10, 35, tzinfo=datetime.timezone.utc),
    )
    db_session.add(policy_failed_job)
    await db_session.flush()

    cb = create_mock_callback(user_id=888111, data=f"admin:job_detail:{policy_failed_job.id}")
    await handle_admin_job_detail(cb, session=db_session)

    cb.message.edit_text.assert_called_once()
    text = cb.message.edit_text.call_args[0][0]
    kb = cb.message.edit_text.call_args[1]["reply_markup"]

    assert "POLICY_USER_ERROR" in text
    assert "Retry ပြုလုပ်၍ မရပါ" in text

    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"admin:job_retry_prompt:{policy_failed_job.id}" not in cbs


@pytest.mark.asyncio
async def test_admin_job_retry_prompt_and_confirmation_flow(db_session):
    """Admin retry prompt asks for confirmation in Burmese and resets job to QUEUED on confirm."""
    settings.admin_ids_raw = "888111"
    admin_user, _ = await get_or_create_user(db_session, telegram_id=888111)

    target_user, _ = await get_or_create_user(db_session, telegram_id=777005)
    target_user.status = UserStatus.ACTIVE.value
    target_user.username = "target_retry_user"

    job_to_retry = Job(
        id="job-retry-flow-77",
        user_id=target_user.id,
        status=JobStatus.FAILED.value,
        error_message="FFmpeg process killed: timeout",
        input_text="အသံပြန်စမ်းမည့် စာသား",
        created_at=datetime.datetime(2026, 10, 15, 10, 40, tzinfo=datetime.timezone.utc),
    )
    db_session.add(job_to_retry)
    await db_session.flush()

    # 1. Prompt step
    cb_prompt = create_mock_callback(user_id=888111, data=f"admin:job_retry_prompt:{job_to_retry.id}")
    await handle_admin_job_retry_prompt(cb_prompt, session=db_session)

    cb_prompt.message.edit_text.assert_called_once()
    prompt_text = cb_prompt.message.edit_text.call_args[0][0]
    prompt_kb = cb_prompt.message.edit_text.call_args[1]["reply_markup"]

    assert "Confirm Job Re-Queue & Retry" in prompt_text
    assert "@target_retry_user" in prompt_text
    assert "Job အခြေအနေသည် <b>FAILED</b> မှ <b>QUEUED</b> သို့ ပြောင်းလဲသွားပါမည်" in prompt_text

    prompt_cbs = [b.callback_data for row in prompt_kb.inline_keyboard for b in row]
    assert f"admin:job_retry_confirm:{job_to_retry.id}" in prompt_cbs
    assert f"admin:job_detail:{job_to_retry.id}" in prompt_cbs

    # 2. Confirm step
    cb_confirm = create_mock_callback(user_id=888111, data=f"admin:job_retry_confirm:{job_to_retry.id}")
    await handle_admin_job_retry_confirm(cb_confirm, session=db_session)

    # Job is reset to QUEUED and errors cleared
    await db_session.refresh(job_to_retry)
    assert job_to_retry.status == JobStatus.QUEUED.value
    assert job_to_retry.error_message is None
    assert job_to_retry.started_at is None
    assert job_to_retry.finished_at is None

    # Audit log recorded
    audit_entry = (await db_session.execute(
        select(AuditLog).where(AuditLog.target_id == str(job_to_retry.id))
    )).scalar_one_or_none()
    assert audit_entry is not None
    assert audit_entry.action == "queue.job_retry"

    # Notification callback answered with re-queued confirmation
    assert any("re-queued" in str(c).lower() for c in cb_confirm.answer.call_args_list)
