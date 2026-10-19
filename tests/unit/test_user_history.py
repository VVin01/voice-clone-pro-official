import datetime
from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram.types import CallbackQuery, Message, User as TgUser
from sqlalchemy import select

from app.handlers.menu import (
    handle_history_clear_confirm,
    handle_history_clear_prompt,
    handle_history_command,
    handle_history_jobs,
    handle_history_ledger,
    handle_menu_history,
)
from app.models.enums import AccountCategory, JobStatus, LedgerDirection, LedgerSource
from app.models.schema import Job, MinuteLedgerEvent, User, VoiceModel
from app.services.history import clear_user_history, get_user_history_cleared_at
from app.services.ledger import (
    admin_adjust_balance,
    award_welcome_bonus,
    consume_seconds,
    get_minute_balance,
    get_or_create_user,
)


def create_mock_callback(user_id: int, data: str) -> CallbackQuery:
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=user_id, is_bot=False, first_name="UThant", username="uthant_user")
    cb.data = data
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    return cb


@pytest.mark.asyncio
async def test_user_history_overview_rendering(db_session):
    """User history overview accurately displays minute balances, recent voice jobs, and ledger credits."""
    user, _ = await get_or_create_user(db_session, telegram_id=660001, username="uthant_user", first_name="U Thant")
    user.language_code = "my"
    await award_welcome_bonus(db_session, user.id)

    vm = VoiceModel(
        name="TharaphiVoice",
        slug="tharaphi-voice",
        model_dir="/models/tharaphi",
        pth_path="/models/tharaphi.pth",
        status="ACTIVE",
    )
    db_session.add(vm)
    await db_session.flush()

    # Create completed job
    j1 = Job(
        id="job-hist-1",
        user_id=user.id,
        model_id=vm.id,
        status=JobStatus.SUCCESS.value,
        edge_voice="my-MM-ThihaNeural",
        seconds_consumed=20,
        input_text="မင်္ဂလာပါ နမူနာ အသံမှတ်တမ်း ဖြစ်ပါသည်",
        created_at=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10),
    )
    db_session.add(j1)
    await db_session.flush()

    # Deduct consumed seconds to record ledger event
    await consume_seconds(db_session, user.id, 20, job_id=str(j1.id), note="Voice clone generation (TharaphiVoice, 20s)")

    cb = create_mock_callback(user_id=660001, data="menu:history")
    await handle_menu_history(cb, session=db_session)

    cb.message.edit_text.assert_called_once()
    text = cb.message.edit_text.call_args[0][0]
    kb = cb.message.edit_text.call_args[1]["reply_markup"]

    # Verify overview sections
    assert "အသုံးပြုမှုနှင့် မိနစ်မှတ်တမ်း (History)" in text
    assert "U Thant (@uthant_user)" in text
    assert "လက်ရှိလက်ကျန်" in text
    assert "သုံးစွဲပြီး မိနစ်စုစုပေါင်း" in text

    # Verify recent activity stream
    assert "TharaphiVoice" in text
    assert "20s" in text
    assert "Welcome Bonus" in text

    # Verify buttons
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "history:jobs" in cbs
    assert "history:ledger" in cbs
    assert "history:clear_prompt" in cbs
    assert "menu:history" in cbs
    assert "menu:help_settings" in cbs
    assert "menu:home" in cbs


@pytest.mark.asyncio
async def test_user_history_sub_views_jobs_and_ledger(db_session):
    """Sub-views display detailed lists of voice clone jobs and minute transactions."""
    user, _ = await get_or_create_user(db_session, telegram_id=660002)
    user.language_code = "my"

    vm = VoiceModel(
        name="KyawSwalVoice",
        slug="kyaw-swal-voice",
        model_dir="/models/kyawswal",
        pth_path="/models/kyawswal.pth",
        status="ACTIVE",
    )
    db_session.add(vm)
    await db_session.flush()

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    j = Job(
        id="job-kyaw-99",
        user_id=user.id,
        model_id=vm.id,
        status=JobStatus.SUCCESS.value,
        edge_voice="my-MM-ThihaNeural",
        seconds_consumed=35,
        input_text="သီချင်းစာသား နမူနာ အသံသွင်းခြင်း",
        created_at=now_utc - datetime.timedelta(minutes=15),
    )
    db_session.add(j)
    await db_session.flush()

    # Credit purchased minutes
    await admin_adjust_balance(
        session=db_session,
        admin_id=888111,
        user_id=user.id,
        seconds=1800,
        direction=LedgerDirection.CREDIT,
        source=LedgerSource.PURCHASE,
        reason="Topup 30 mins package",
    )

    # 1. Test history:jobs view
    cb_jobs = create_mock_callback(user_id=660002, data="history:jobs")
    await handle_history_jobs(cb_jobs, session=db_session)
    cb_jobs.message.edit_text.assert_called_once()
    jobs_text = cb_jobs.message.edit_text.call_args[0][0]
    jobs_kb = cb_jobs.message.edit_text.call_args[1]["reply_markup"]

    assert "အသံထုတ်လုပ်မှု မှတ်တမ်းများ" in jobs_text
    assert "KyawSwalVoice" in jobs_text
    assert "35s" in jobs_text
    assert "သီချင်းစာသား နမူနာ" in jobs_text

    jobs_cbs = [b.callback_data for row in jobs_kb.inline_keyboard for b in row]
    assert "history:ledger" in jobs_cbs
    assert "history:clear_prompt" in jobs_cbs
    assert "menu:history" in jobs_cbs

    # 2. Test history:ledger view
    cb_ledger = create_mock_callback(user_id=660002, data="history:ledger")
    await handle_history_ledger(cb_ledger, session=db_session)
    cb_ledger.message.edit_text.assert_called_once()
    ledger_text = cb_ledger.message.edit_text.call_args[0][0]

    assert "မိနစ် အဝင်/အထွက် မှတ်တမ်း" in ledger_text
    assert "+1800s" in ledger_text or "+1,800s" in ledger_text or "1800" in ledger_text
    assert "Topup 30 mins package" in ledger_text


@pytest.mark.asyncio
async def test_user_history_clear_flow_and_subsequent_activity(db_session):
    """Clearing history suppresses past records from user display while preserving accounting balances and capturing new activity."""
    user, _ = await get_or_create_user(db_session, telegram_id=660003)
    user.language_code = "my"
    await award_welcome_bonus(db_session, user.id)

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    old_job = Job(
        id="job-old-to-clear",
        user_id=user.id,
        status=JobStatus.SUCCESS.value,
        edge_voice="my-MM-ThihaNeural",
        seconds_consumed=15,
        input_text="ရှင်းလင်းခံရမည့် စာသားဟောင်း",
        created_at=now_utc - datetime.timedelta(hours=2),
    )
    db_session.add(old_job)
    await db_session.flush()

    initial_balance = await get_minute_balance(db_session, user.id)

    # 1. Prompt clear
    cb_prompt = create_mock_callback(user_id=660003, data="history:clear_prompt")
    await handle_history_clear_prompt(cb_prompt, session=db_session)

    cb_prompt.message.edit_text.assert_called_once()
    prompt_text = cb_prompt.message.edit_text.call_args[0][0]
    prompt_kb = cb_prompt.message.edit_text.call_args[1]["reply_markup"]

    assert "History မှတ်တမ်း ရှင်းလင်းရန် အတည်ပြုခြင်း" in prompt_text
    assert "လက်ကျန်မိနစ်များကို လုံးဝ (လုံးဝ) ထိခိုက်မှု မရှိပါ" in prompt_text
    prompt_cbs = [b.callback_data for row in prompt_kb.inline_keyboard for b in row]
    assert "history:clear_confirm" in prompt_cbs
    assert "menu:history" in prompt_cbs

    # 2. Confirm clear
    cb_confirm = create_mock_callback(user_id=660003, data="history:clear_confirm")
    await handle_history_clear_confirm(cb_confirm, session=db_session)

    # Toast feedback answered
    cb_confirm.answer.assert_called_once()
    assert "ရှင်းလင်းပြီးပါပြီ" in cb_confirm.answer.call_args[0][0]

    # Re-rendered history is clean
    cleared_text = cb_confirm.message.edit_text.call_args[0][0]
    assert "မှတ်တမ်း မရှိသေးပါ" in cleared_text
    assert "job-old-to-clear" not in cleared_text

    # Balance is 100% preserved
    post_clear_balance = await get_minute_balance(db_session, user.id)
    assert post_clear_balance.total_usable_seconds == initial_balance.total_usable_seconds

    # 3. Subsequent new job appears in history
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    new_job = Job(
        id="job-brand-new",
        user_id=user.id,
        status=JobStatus.SUCCESS.value,
        edge_voice="my-MM-ThihaNeural",
        seconds_consumed=25,
        input_text="ရှင်းပြီးမှ အသစ်ထပ်လုပ်သော အသံဖိုင်",
        created_at=now_utc + datetime.timedelta(seconds=5),
    )
    db_session.add(new_job)
    await db_session.flush()

    cb_new = create_mock_callback(user_id=660003, data="menu:history")
    await handle_menu_history(cb_new, session=db_session)
    new_hist_text = cb_new.message.edit_text.call_args[0][0]

    assert "ရှင်းပြီးမှ အသစ်ထပ်လုပ်သော" in new_hist_text
    assert "ရှင်းလင်းခံရမည့် စာသားဟောင်း" not in new_hist_text


@pytest.mark.asyncio
async def test_user_history_command_handler(db_session):
    """Command /history sends history overview message."""
    user, _ = await get_or_create_user(db_session, telegram_id=660004)
    user.language_code = "en"

    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=660004, is_bot=False, first_name="John")
    msg.answer = AsyncMock()

    await handle_history_command(msg, session=db_session)
    msg.answer.assert_called_once()
    text = msg.answer.call_args[0][0]
    assert "Usage & Minute History" in text
    assert "Usable Balance" in text
