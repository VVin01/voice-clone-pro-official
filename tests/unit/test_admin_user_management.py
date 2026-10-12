from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from aiogram.types import CallbackQuery, Message, User as TgUser
from sqlalchemy import select

from app.config import settings
from app.handlers.admin import (
    AdminAdjustFSM,
    handle_admin_adjust_amount_input,
    handle_admin_adjust_cancel,
    handle_admin_adjust_confirm,
    handle_admin_adjust_reason_input,
    handle_admin_adjust_start,
    handle_admin_queue,
    handle_admin_queue_recover_disabled,
    handle_admin_user_detail,
    handle_admin_users,
    handle_admin_users_page,
)
from app.models.enums import AccountCategory, JobStatus, LedgerDirection, LedgerSource
from app.models.schema import (
    AdjustmentIdempotencyRecord,
    AuditLog,
    Job,
    MinuteBalance,
    MinuteLedgerEvent,
    User,
    utc_now,
)
from app.services.i18n import t
from app.ui.keyboards import admin_queue_kb
from app.services.ledger import (
    admin_adjust_balance,
    get_minute_balance,
    get_or_create_user,
    get_paginated_users,
    get_user_recent_ledger_events,
)


class FakeFSMContext:
    """In-memory FSMContext replica for reliable handler unit tests."""

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


@pytest.mark.asyncio
async def test_admin_users_pagination_and_navigation(db_session):
    settings.admin_ids_raw = "888111"

    # Pre-populate admin user first so get_or_create_user does not add another user during handler invocation
    admin = User(
        id=888111,
        username="boss_admin",
        first_name="BossAdmin",
        account_category=AccountCategory.FREE.value,
    )
    db_session.add(admin)

    # Create 11 additional users (12 users total)
    for i in range(1, 12):
        u = User(
            id=1000 + i,
            username=f"user_{i}",
            first_name=f"First{i}",
            account_category=AccountCategory.FREE.value if i % 2 == 0 else AccountCategory.PURCHASED.value,
        )
        db_session.add(u)
    await db_session.flush()

    # Page 1: 5 users
    users_p1, total_users, total_pages = await get_paginated_users(db_session, page=1, page_size=5)
    assert total_users == 12
    assert total_pages == 3
    assert len(users_p1) == 5

    # Page 2: 5 users
    users_p2, _, _ = await get_paginated_users(db_session, page=2, page_size=5)
    assert len(users_p2) == 5
    assert {u.id for u in users_p1}.isdisjoint({u.id for u in users_p2})

    # Page 3: 2 users
    users_p3, _, _ = await get_paginated_users(db_session, page=3, page_size=5)
    assert len(users_p3) == 2

    # Clamping beyond total pages
    users_p99, _, _ = await get_paginated_users(db_session, page=99, page_size=5)
    assert len(users_p99) == 2

    # Test callback handler
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    cb.data = "admin:users_page:2"
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    await handle_admin_users_page(cb, session=db_session)
    cb.message.edit_text.assert_called_once()
    rendered_text = cb.message.edit_text.call_args[0][0]
    reply_markup = cb.message.edit_text.call_args[1]["reply_markup"]

    assert "Page 2/3" in rendered_text
    assert "Total Registered Users: <b>12</b>" in rendered_text

    # Verify pagination buttons
    nav_callbacks = [b.callback_data for row in reply_markup.inline_keyboard for b in row]
    assert "admin:users_page:1" in nav_callbacks  # Prev
    assert "admin:users_page:3" in nav_callbacks  # Next


@pytest.mark.asyncio
async def test_admin_users_empty_db(db_session):
    settings.admin_ids_raw = "888111"

    users, total_users, total_pages = await get_paginated_users(db_session, page=1, page_size=5)
    assert users == []
    assert total_users == 0
    assert total_pages == 1

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    cb.data = "admin:users"
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    with patch("app.handlers.admin.get_paginated_users", return_value=([], 0, 1)):
        await handle_admin_users(cb, session=db_session)
        cb.message.edit_text.assert_called_once()
        rendered_text = cb.message.edit_text.call_args[0][0]
        assert "No registered users found" in rendered_text


@pytest.mark.asyncio
async def test_admin_user_detail_card_and_html_escaping(db_session):
    settings.admin_ids_raw = "888111"

    # User with special characters in username & first name
    unsafe_uname = "rock<roll>&cool"
    unsafe_first = "John <Boss> & Co"
    u = User(
        id=7701,
        username=unsafe_uname,
        first_name=unsafe_first,
        language_code="my",
        account_category=AccountCategory.PURCHASED.value,
    )
    db_session.add(u)
    await db_session.flush()

    bal = await get_minute_balance(db_session, u.id)
    bal.purchased_seconds = 600   # 10m
    bal.welcome_seconds = 300     # 5m
    bal.referral_seconds = 60     # 1m
    bal.consumed_seconds = 120    # 2m
    await db_session.flush()

    # Add 4 ledger events with unsafe characters in note
    unsafe_note = "Bonus <tag> & note #1"
    for i in range(1, 5):
        ev = MinuteLedgerEvent(
            user_id=u.id,
            source=LedgerSource.ADJUST.value if i == 1 else LedgerSource.PURCHASE.value,
            direction=LedgerDirection.CREDIT.value,
            seconds=60 * i,
            note=f"{unsafe_note} ({i})",
        )
        db_session.add(ev)
    await db_session.flush()

    recent_events = await get_user_recent_ledger_events(db_session, u.id, limit=3)
    assert len(recent_events) == 3

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    cb.data = f"admin:user_detail:{u.id}:1"
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    await handle_admin_user_detail(cb, session=db_session)
    cb.message.edit_text.assert_called_once()
    rendered = cb.message.edit_text.call_args[0][0]
    parse_mode = cb.message.edit_text.call_args[1].get("parse_mode")
    assert parse_mode == "HTML"

    # Verify HTML escaping
    assert "@rock&lt;roll&gt;&amp;cool" in rendered
    assert "John &lt;Boss&gt; &amp; Co" in rendered
    assert "<roll>" not in rendered
    assert "<Boss>" not in rendered
    assert "Bonus &lt;tag&gt; &amp; note" in rendered
    assert "<tag>" not in rendered

    # Verify breakdown
    assert "Purchased: <b>10.0m</b> (600s)" in rendered
    assert "Welcome: <b>5.0m</b> (300s)" in rendered
    assert "Referral: <b>1.0m</b> (60s)" in rendered
    assert "Consumed: <b>2.0m</b> (120s)" in rendered
    assert "Total Usable: <b>16.0m</b> (960s)" in rendered
    assert "Account Status: <b>PURCHASED</b>" in rendered


@pytest.mark.asyncio
async def test_admin_user_detail_deleted_or_missing(db_session):
    settings.admin_ids_raw = "888111"

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    cb.data = "admin:user_detail:999999:1"
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    await handle_admin_user_detail(cb, session=db_session)
    cb.answer.assert_called_once_with("User not found", show_alert=True)


@pytest.mark.asyncio
async def test_admin_adjust_balance_credit_success_and_status_promotion(db_session):
    u = User(id=8801, account_category=AccountCategory.FREE.value)
    db_session.add(u)
    await db_session.flush()

    bal = await get_minute_balance(db_session, u.id)
    assert bal.purchased_seconds == 0
    assert u.account_category == AccountCategory.FREE.value

    updated_bal = await admin_adjust_balance(
        session=db_session,
        user_id=u.id,
        admin_id=888111,
        seconds=600,
        source=LedgerSource.PURCHASE,
        direction=LedgerDirection.CREDIT,
        reason="Goodwill compensation for downtime",
        idempotency_key="ADJ-TEST-CREDIT-1",
    )
    await db_session.flush()

    assert updated_bal.purchased_seconds == 600
    assert updated_bal.purchased_minutes == 10.0
    assert u.account_category == AccountCategory.PURCHASED.value

    # Verify MinuteLedgerEvent
    events = (await db_session.execute(
        select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == u.id)
    )).scalars().all()
    assert len(events) == 1
    assert events[0].source == LedgerSource.ADJUST.value
    assert events[0].direction == LedgerDirection.CREDIT.value
    assert events[0].seconds == 600
    assert events[0].reference_id == "ADJ-TEST-CREDIT-1"
    assert "Goodwill compensation for downtime" in events[0].note

    # Verify AuditLog
    logs = (await db_session.execute(
        select(AuditLog).where(AuditLog.target_id == str(u.id))
    )).scalars().all()
    assert len(logs) == 1
    assert logs[0].action == "balance.adjust.credit"
    assert logs[0].admin_id == 888111
    assert "ADJ-TEST-CREDIT-1" in logs[0].details_json


@pytest.mark.asyncio
async def test_admin_adjust_balance_debit_success_and_status_demotion(db_session):
    u = User(id=8802, account_category=AccountCategory.PURCHASED.value)
    db_session.add(u)
    await db_session.flush()

    bal = await get_minute_balance(db_session, u.id)
    bal.purchased_seconds = 600  # 10m
    await db_session.flush()

    # Debit 600s: purchased balance reaches 0, demotes to FREE
    updated_bal = await admin_adjust_balance(
        session=db_session,
        user_id=u.id,
        admin_id=888111,
        seconds=600,
        source=LedgerSource.PURCHASE,
        direction=LedgerDirection.DEBIT,
        reason="Reversing erroneous credit",
        idempotency_key="ADJ-TEST-DEBIT-1",
    )
    await db_session.flush()

    assert updated_bal.purchased_seconds == 0
    assert u.account_category == AccountCategory.FREE.value


@pytest.mark.asyncio
async def test_admin_adjust_balance_insufficient_debit_rejected(db_session):
    u = User(id=8803, account_category=AccountCategory.PURCHASED.value)
    db_session.add(u)
    await db_session.flush()

    bal = await get_minute_balance(db_session, u.id)
    bal.purchased_seconds = 180  # 3m
    await db_session.flush()

    # Attempt to debit 600s (10m)
    with pytest.raises(ValueError, match="Insufficient purchased balance"):
        await admin_adjust_balance(
            session=db_session,
            user_id=u.id,
            admin_id=888111,
            seconds=600,
            source=LedgerSource.PURCHASE,
            direction=LedgerDirection.DEBIT,
            reason="Debit too large",
            idempotency_key="ADJ-OVER-DEBIT",
        )

    # Balance and account category remain intact
    assert bal.purchased_seconds == 180
    assert u.account_category == AccountCategory.PURCHASED.value


@pytest.mark.asyncio
async def test_admin_adjust_balance_validation_errors(db_session):
    u = User(id=8804, account_category=AccountCategory.FREE.value)
    db_session.add(u)
    await db_session.flush()

    # Empty reason
    with pytest.raises(ValueError, match="mandatory and cannot be empty"):
        await admin_adjust_balance(
            session=db_session,
            user_id=u.id,
            admin_id=888111,
            seconds=300,
            source=LedgerSource.PURCHASE,
            direction=LedgerDirection.CREDIT,
            reason="   ",
        )

    # Negative seconds
    with pytest.raises(ValueError, match="must be positive"):
        await admin_adjust_balance(
            session=db_session,
            user_id=u.id,
            admin_id=888111,
            seconds=-60,
            source=LedgerSource.PURCHASE,
            direction=LedgerDirection.CREDIT,
            reason="Valid reason",
        )

    # Missing user
    with pytest.raises(ValueError, match="User 999988 not found"):
        await admin_adjust_balance(
            session=db_session,
            user_id=999988,
            admin_id=888111,
            seconds=300,
            source=LedgerSource.PURCHASE,
            direction=LedgerDirection.CREDIT,
            reason="Valid reason",
        )


@pytest.mark.asyncio
async def test_admin_adjust_balance_idempotency_duplicate_suppression(db_session):
    u = User(id=8805, account_category=AccountCategory.FREE.value)
    db_session.add(u)
    await db_session.flush()

    token = "IDEMPOTENT-NONCE-ABC"

    # First execution: +300s
    bal1 = await admin_adjust_balance(
        session=db_session,
        user_id=u.id,
        admin_id=888111,
        seconds=300,
        source=LedgerSource.PURCHASE,
        direction=LedgerDirection.CREDIT,
        reason="Test idempotency",
        idempotency_key=token,
    )
    assert bal1.purchased_seconds == 300

    # Second execution with same key: must NOT add 300s again!
    bal2 = await admin_adjust_balance(
        session=db_session,
        user_id=u.id,
        admin_id=888111,
        seconds=300,
        source=LedgerSource.PURCHASE,
        direction=LedgerDirection.CREDIT,
        reason="Test idempotency replay",
        idempotency_key=token,
    )
    assert bal2.purchased_seconds == 300  # Exactly 300, never 600!

    # Exactly one ledger event with that reference_id
    events = (await db_session.execute(
        select(MinuteLedgerEvent).where(
            MinuteLedgerEvent.user_id == u.id,
            MinuteLedgerEvent.reference_id == token,
        )
    )).scalars().all()
    assert len(events) == 1

    # Exactly one AdjustmentIdempotencyRecord with that idempotency_key
    records = (await db_session.execute(
        select(AdjustmentIdempotencyRecord).where(
            AdjustmentIdempotencyRecord.idempotency_key == token,
        )
    )).scalars().all()
    assert len(records) == 1
    assert records[0].user_id == u.id
    assert records[0].seconds == 300

    # Exactly one AuditLog with that idempotency_key
    audits = (await db_session.execute(
        select(AuditLog).where(
            AuditLog.target_id == str(u.id),
        )
    )).scalars().all()
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_admin_adjust_balance_concurrent_duplicate_multi_session(tmp_path):
    """Test concurrent duplicate adjustments across separate sessions on an isolated database."""
    import asyncio
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from app.db.base import Base

    db_file = tmp_path / "concurrent_test.db"
    test_url = f"sqlite+aiosqlite:///{db_file}"
    engine = create_async_engine(test_url, echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as setup_session:
        u = User(id=7777, account_category=AccountCategory.FREE.value)
        setup_session.add(u)
        await setup_session.commit()

    token = "CONCURRENT-NONCE-XYZ"

    async def execute_adjust(worker_id: int):
        async with session_factory() as session:
            try:
                bal = await admin_adjust_balance(
                    session=session,
                    user_id=7777,
                    admin_id=888111,
                    seconds=300,
                    source=LedgerSource.PURCHASE,
                    direction=LedgerDirection.CREDIT,
                    reason=f"Concurrent adjust from worker {worker_id}",
                    idempotency_key=token,
                )
                await session.commit()
                return "ok", bal.purchased_seconds
            except Exception:
                await session.rollback()
                return "conflict", 0

    results = await asyncio.gather(execute_adjust(1), execute_adjust(2))
    assert len(results) == 2

    # Verify database state
    async with session_factory() as verify_session:
        final_bal = await get_minute_balance(verify_session, 7777)
        assert final_bal.purchased_seconds == 300  # Exactly 300s, NEVER 600s!

        events = (await verify_session.execute(
            select(MinuteLedgerEvent).where(
                MinuteLedgerEvent.user_id == 7777,
                MinuteLedgerEvent.reference_id == token,
            )
        )).scalars().all()
        assert len(events) == 1

        idemp_recs = (await verify_session.execute(
            select(AdjustmentIdempotencyRecord).where(
                AdjustmentIdempotencyRecord.idempotency_key == token,
            )
        )).scalars().all()
        assert len(idemp_recs) == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_admin_adjust_balance_different_keys_allowed(db_session):
    u = User(id=8809, account_category=AccountCategory.FREE.value)
    db_session.add(u)
    await db_session.flush()

    bal1 = await admin_adjust_balance(
        session=db_session,
        user_id=u.id,
        admin_id=888111,
        seconds=300,
        source=LedgerSource.PURCHASE,
        direction=LedgerDirection.CREDIT,
        reason="Adjustment 1",
        idempotency_key="ADJ-DIFF-KEY-1",
    )
    assert bal1.purchased_seconds == 300

    bal2 = await admin_adjust_balance(
        session=db_session,
        user_id=u.id,
        admin_id=888111,
        seconds=300,
        source=LedgerSource.PURCHASE,
        direction=LedgerDirection.CREDIT,
        reason="Adjustment 2",
        idempotency_key="ADJ-DIFF-KEY-2",
    )
    assert bal2.purchased_seconds == 600  # Both legitimate adjustments applied

    events = (await db_session.execute(
        select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == u.id)
    )).scalars().all()
    assert len(events) == 2


@pytest.mark.asyncio
async def test_admin_adjust_fsm_workflow_complete(db_session):
    settings.admin_ids_raw = "888111"

    u = User(id=8806, username="adjust_target", account_category=AccountCategory.FREE.value)
    db_session.add(u)
    await db_session.flush()

    state = FakeFSMContext()

    # Step 0: Start adjustment callback
    cb_start = MagicMock(spec=CallbackQuery)
    cb_start.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    cb_start.data = f"admin:adjust_start:{u.id}:CREDIT:1"
    cb_start.message = MagicMock(spec=Message)
    cb_start.message.edit_text = AsyncMock()
    cb_start.answer = AsyncMock()

    await handle_admin_adjust_start(cb_start, state=state, session=db_session)
    assert state.current_state == AdminAdjustFSM.waiting_for_amount
    cb_start.message.edit_text.assert_called_once()
    assert "Step 1/2" in cb_start.message.edit_text.call_args[0][0]

    # Step 1: Input amount
    msg_amount = MagicMock(spec=Message)
    msg_amount.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    msg_amount.text = "10"
    msg_amount.answer = AsyncMock()

    await handle_admin_adjust_amount_input(msg_amount, state=state, session=db_session)
    assert state.current_state == AdminAdjustFSM.waiting_for_reason
    assert state.data["minutes"] == 10
    assert state.data["seconds"] == 600
    msg_amount.answer.assert_called_once()
    assert "Step 2/2" in msg_amount.answer.call_args[0][0]

    # Step 2: Input reason
    msg_reason = MagicMock(spec=Message)
    msg_reason.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    unsafe_reason = "Customer compensation <test> & verified"
    msg_reason.text = unsafe_reason
    msg_reason.answer = AsyncMock()

    await handle_admin_adjust_reason_input(msg_reason, state=state, session=db_session)
    assert state.current_state == AdminAdjustFSM.confirming
    generated_token = state.data["token"]
    assert generated_token.startswith("ADJ-")
    msg_reason.answer.assert_called_once()
    preview_call = msg_reason.answer.call_args[0][0]
    assert "Confirm Minute Adjustment" in preview_call
    assert "Customer compensation &lt;test&gt; &amp; verified" in preview_call
    assert "<test>" not in preview_call

    # Step 3: Confirm callback
    cb_confirm = MagicMock(spec=CallbackQuery)
    cb_confirm.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    cb_confirm.data = f"admin:adjust_confirm:{generated_token}"
    cb_confirm.message = MagicMock(spec=Message)
    cb_confirm.message.edit_text = AsyncMock()
    cb_confirm.answer = AsyncMock()

    await handle_admin_adjust_confirm(cb_confirm, state=state, session=db_session)
    assert state.current_state is None  # State cleared!
    cb_confirm.message.edit_text.assert_called_once()
    success_text = cb_confirm.message.edit_text.call_args[0][0]
    assert "Minute Adjustment Applied" in success_text
    assert "CREDIT 10m" in success_text

    # Verify balance was actually updated in database
    bal = await get_minute_balance(db_session, u.id)
    assert bal.purchased_seconds == 600
    assert u.account_category == AccountCategory.PURCHASED.value


@pytest.mark.asyncio
async def test_admin_adjust_fsm_validation_failures(db_session):
    settings.admin_ids_raw = "888111"

    u = User(id=8807, account_category=AccountCategory.PURCHASED.value)
    db_session.add(u)
    await db_session.flush()

    bal = await get_minute_balance(db_session, u.id)
    bal.purchased_seconds = 120  # 2m
    await db_session.flush()

    state = FakeFSMContext(
        initial_state=AdminAdjustFSM.waiting_for_amount,
        initial_data={"target_user_id": u.id, "direction": "DEBIT", "lang": "en"},
    )

    # 1. Non-digit input
    msg_bad1 = MagicMock(spec=Message)
    msg_bad1.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    msg_bad1.text = "abc"
    msg_bad1.answer = AsyncMock()
    await handle_admin_adjust_amount_input(msg_bad1, state=state, session=db_session)
    assert state.current_state == AdminAdjustFSM.waiting_for_amount  # Remains in state
    assert "positive whole number" in msg_bad1.answer.call_args[0][0]

    # 2. Insufficient debit
    msg_bad2 = MagicMock(spec=Message)
    msg_bad2.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    msg_bad2.text = "10"  # 10m > 2m
    msg_bad2.answer = AsyncMock()
    await handle_admin_adjust_amount_input(msg_bad2, state=state, session=db_session)
    assert state.current_state == AdminAdjustFSM.waiting_for_amount
    assert "Cannot debit <b>10m</b>" in msg_bad2.answer.call_args[0][0]

    # 3. Reason too short
    state.current_state = AdminAdjustFSM.waiting_for_reason
    state.data["minutes"] = 1
    state.data["seconds"] = 60
    msg_bad3 = MagicMock(spec=Message)
    msg_bad3.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    msg_bad3.text = "  "
    msg_bad3.answer = AsyncMock()
    await handle_admin_adjust_reason_input(msg_bad3, state=state, session=db_session)
    assert state.current_state == AdminAdjustFSM.waiting_for_reason
    assert "Reason is mandatory" in msg_bad3.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_admin_adjust_fsm_cancel_and_expired_token(db_session):
    settings.admin_ids_raw = "888111"

    u = User(id=8808, account_category=AccountCategory.FREE.value)
    db_session.add(u)
    await db_session.flush()

    state = FakeFSMContext(
        initial_state=AdminAdjustFSM.confirming,
        initial_data={"target_user_id": u.id, "token": "REAL-TOKEN-1", "return_page": 1},
    )

    # 1. Expired/mismatched token
    cb_bad = MagicMock(spec=CallbackQuery)
    cb_bad.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    cb_bad.data = "admin:adjust_confirm:WRONG-TOKEN"
    cb_bad.answer = AsyncMock()
    await handle_admin_adjust_confirm(cb_bad, state=state, session=db_session)
    cb_bad.answer.assert_called_once_with(
        "⚠️ Adjustment confirmation is invalid or has expired.", show_alert=True
    )
    assert state.current_state == AdminAdjustFSM.confirming  # State not cleared

    # 2. Cancel callback
    cb_cancel = MagicMock(spec=CallbackQuery)
    cb_cancel.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    cb_cancel.data = "admin:adjust_cancel"
    cb_cancel.message = MagicMock(spec=Message)
    cb_cancel.message.edit_text = AsyncMock()
    cb_cancel.answer = AsyncMock()

    await handle_admin_adjust_cancel(cb_cancel, state=state, session=db_session)
    assert state.current_state is None  # State cleared
    cb_cancel.answer.assert_called_once_with("❌ Adjustment cancelled.", show_alert=True)


@pytest.mark.asyncio
async def test_admin_queue_recovery_disabled_and_uninvocable(db_session):
    settings.admin_ids_raw = "888111"

    # 1. Verify keyboard does not contain recovery buttons
    kb = admin_queue_kb(lang="en")
    button_callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "admin:queue_recover_prompt" not in button_callbacks
    assert "admin:queue_recover_confirm" not in button_callbacks
    assert "admin:queue" in button_callbacks  # Refresh Queue present
    assert "admin:dashboard" in button_callbacks  # Back button present

    # 2. Verify disabled handler safely rejects legacy/direct callback for prompt
    cb_prompt = MagicMock(spec=CallbackQuery)
    cb_prompt.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    cb_prompt.data = "admin:queue_recover_prompt"
    cb_prompt.answer = AsyncMock()

    await handle_admin_queue_recover_disabled(cb_prompt)
    cb_prompt.answer.assert_called_once()
    assert "Interactive queue recovery is disabled for safety" in cb_prompt.answer.call_args[0][0]

    # 3. Verify disabled handler safely rejects legacy/direct callback for confirm
    cb_confirm = MagicMock(spec=CallbackQuery)
    cb_confirm.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    cb_confirm.data = "admin:queue_recover_confirm"
    cb_confirm.answer = AsyncMock()

    await handle_admin_queue_recover_disabled(cb_confirm)
    cb_confirm.answer.assert_called_once()
    assert "Interactive queue recovery is disabled for safety" in cb_confirm.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_admin_adjust_confirm_handler_explicit_rollback():
    settings.admin_ids_raw = "888111"

    mock_session = AsyncMock()
    mock_session.commit = AsyncMock(side_effect=Exception("Database lock failure"))
    mock_session.rollback = AsyncMock()

    state = FakeFSMContext(
        initial_state=AdminAdjustFSM.confirming,
        initial_data={
            "token": "ROLLBACK-TOKEN",
            "target_user_id": 9999,
            "direction": "CREDIT",
            "minutes": 5,
            "seconds": 300,
            "reason": "Test rollback",
            "return_page": 1,
            "lang": "en",
        },
    )

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    cb.data = "admin:adjust_confirm:ROLLBACK-TOKEN"
    cb.answer = AsyncMock()

    with patch("app.handlers.admin.admin_adjust_balance", return_value=MagicMock(purchased_minutes=5.0, purchased_seconds=300, total_usable_minutes=5.0)):
        await handle_admin_adjust_confirm(cb, state=state, session=mock_session)

    # Verify rollback was called explicitly
    mock_session.rollback.assert_awaited_once()
    cb.answer.assert_called_once()
    assert "Adjustment failed: Database lock failure" in cb.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_unauthorized_admin_user_management(db_session):
    settings.admin_ids_raw = "888111"

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=999999, is_bot=False, first_name="Intruder")
    cb.data = "admin:users"
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    await handle_admin_users(cb, session=db_session)
    cb.message.edit_text.assert_not_called()
    cb.answer.assert_called_once_with("⛔ Access denied. Admin only.", show_alert=True)


@pytest.mark.asyncio
async def test_localization_parity_and_terminology():
    # Verify terminology in en and my messages.json
    en_desc = t("buy_minutes_desc", "en")
    assert "PURCHASED" in en_desc

    long_err_en = t("tts_text_too_long", "en", max_chars=500, curr_chars=600)
    assert "account type" in long_err_en
    assert "tier" not in long_err_en

    admin_tts_en = t("tts_admin_enter_text", "en", max_chars=5000)
    assert "User account" in admin_tts_en
    assert "tier" not in admin_tts_en

    pay_approve_en = t("payment_approved_user_notify", "en", minutes=10)
    assert "Account Status: <b>PURCHASED</b>" in pay_approve_en
    assert "tier" not in pay_approve_en.lower()

    pricing_title_en = t("admin_pricing_title", "en")
    assert "Active Minute Packages" in pricing_title_en

    pricing_title_my = t("admin_pricing_title", "my")
    assert "မိနစ်ပက်ကေ့ဂျ်များ" in pricing_title_my
