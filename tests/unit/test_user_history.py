"""User history service unit tests.

Bot-chat /history menus were moved to the Mini App History view in Round 13 Part K,
while `app.services.history` remains live and backs personal history clearing/querying.
"""
import datetime
import pytest

from app.models.enums import JobStatus, LedgerDirection, LedgerSource
from app.models.schema import Job, VoiceModel
from app.services.history import (
    clear_user_history,
    get_models_map_for_jobs,
    get_user_history_cleared_at,
    get_user_history_jobs,
    get_user_history_ledger_events,
)
from app.services.ledger import (
    admin_adjust_balance,
    consume_seconds,
    get_minute_balance,
    get_or_create_user,
    grant_daily_free_allowance,
)


@pytest.mark.asyncio
async def test_user_history_jobs_and_models_map(db_session):
    """History service fetches user jobs and resolves referenced voice models."""
    user, _ = await get_or_create_user(db_session, telegram_id=660001, username="uthant_user", first_name="U Thant")
    await grant_daily_free_allowance(db_session, user.id)

    vm = VoiceModel(
        name="TharaphiVoice",
        slug="tharaphi-voice",
        model_dir="/models/tharaphi",
        pth_path="/models/tharaphi.pth",
        status="ACTIVE",
    )
    db_session.add(vm)
    await db_session.flush()

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
    await consume_seconds(db_session, user.id, 20, job_id=str(j1.id), note="Voice clone generation (TharaphiVoice, 20s)")

    jobs = await get_user_history_jobs(db_session, user.id)
    assert [j.id for j in jobs] == ["job-hist-1"]
    assert jobs[0].seconds_consumed == 20
    models_map = await get_models_map_for_jobs(db_session, jobs)
    assert models_map[vm.id].name == "TharaphiVoice"


@pytest.mark.asyncio
async def test_user_history_ledger_events_returned(db_session):
    """History service returns minute ledger events for the user."""
    user, _ = await get_or_create_user(db_session, telegram_id=660002)
    await admin_adjust_balance(
        session=db_session,
        admin_id=888111,
        user_id=user.id,
        seconds=1800,
        direction=LedgerDirection.CREDIT,
        source=LedgerSource.PURCHASE,
        reason="Purchase 30 mins package",
    )

    events = await get_user_history_ledger_events(db_session, user.id)
    assert any(ev.seconds == 1800 and "Purchase 30 mins package" in (ev.note or "") for ev in events)


@pytest.mark.asyncio
async def test_user_history_clear_suppresses_past_preserves_balance_and_keeps_new(db_session):
    """Clearing history suppresses past records while preserving accounting balances and capturing new activity."""
    user, _ = await get_or_create_user(db_session, telegram_id=660003)
    await grant_daily_free_allowance(db_session, user.id)

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
    assert await get_user_history_cleared_at(db_session, user.id) is None

    cleared_at = await clear_user_history(db_session, user.id)
    assert cleared_at is not None
    assert await get_user_history_cleared_at(db_session, user.id) is not None

    # Past jobs are filtered out after clear
    assert await get_user_history_jobs(db_session, user.id) == []

    # Accounting balance is 100% preserved
    post_clear_balance = await get_minute_balance(db_session, user.id)
    assert post_clear_balance.total_usable_seconds == initial_balance.total_usable_seconds

    # Subsequent new job appears in history
    new_job = Job(
        id="job-brand-new",
        user_id=user.id,
        status=JobStatus.SUCCESS.value,
        edge_voice="my-MM-ThihaNeural",
        seconds_consumed=25,
        input_text="ရှင်းပြီးမှ အသစ်ထပ်လုပ်သော အသံဖိုင်",
        created_at=cleared_at + datetime.timedelta(seconds=5),
    )
    db_session.add(new_job)
    await db_session.flush()

    jobs_after = await get_user_history_jobs(db_session, user.id)
    assert [j.id for j in jobs_after] == ["job-brand-new"]
