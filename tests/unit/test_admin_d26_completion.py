from contextlib import asynccontextmanager
import datetime
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from aiogram.types import CallbackQuery, Chat, Message, User as TgUser
from sqlalchemy import select

from app.config import settings
from app.handlers.admin import (
    AdminAdjustFSM,
    AdminUserSearchFSM,
    handle_admin_adjust_cancel,
    handle_admin_pay_receipt,
    handle_admin_queue_failures,
    handle_admin_user_command,
    handle_admin_user_search_cancel,
    handle_admin_user_search_query,
    handle_admin_user_search_start,
    handle_admin_user_toggle_status,
    render_admin_model_detail,
    render_admin_user_detail,
)
from app.models.enums import AccountCategory, JobStatus, PaymentStatus, UserStatus
from app.models.schema import (
    AdjustmentIdempotencyRecord,
    AuditLog,
    Job,
    MinuteLedgerEvent,
    PaymentRequest,
    User,
    VoiceModel,
)
from app.services.ledger import get_minute_balance, get_or_create_user
from app.ui.keyboards import (
    admin_queue_kb,
    admin_user_card_kb,
    admin_users_list_kb,
)


class FakeFSMContext:
    """In-memory replica of aiogram FSMContext."""

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


def create_real_frozen_callback(user_id: int, data: str):
    """Instantiate a real frozen aiogram CallbackQuery with mocked methods."""
    user = TgUser(id=user_id, is_bot=False, first_name="BossAdmin")
    msg = MagicMock(spec=Message)
    msg.edit_text = AsyncMock()
    msg.answer = AsyncMock()
    msg.answer_photo = AsyncMock()

    cb = CallbackQuery(
        id="cb_test_id",
        from_user=user,
        chat_instance="chat_instance_test",
        message=msg,
        data=data,
    )
    cb.as_(AsyncMock())
    object.__setattr__(cb, "answer", AsyncMock())
    return cb


# ---------------------------------------------------------------------------
# Phase 2: Frozen Callback Fix Regression Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_admin_adjust_cancel_frozen_callback_regression(db_session):
    """Prove that handle_admin_adjust_cancel never mutates frozen CallbackQuery."""
    settings.admin_ids_raw = "888111"

    # Set up target user with initial balance
    target = User(id=77001, username="target_alice", account_category=AccountCategory.FREE.value)
    db_session.add(target)
    await db_session.flush()

    bal = await get_minute_balance(db_session, target.id)
    bal.purchased_seconds = 600  # 10m
    await db_session.flush()

    state = FakeFSMContext(
        initial_state=AdminAdjustFSM.confirming,
        initial_data={
            "target_user_id": target.id,
            "direction": "CREDIT",
            "minutes": 5,
            "seconds": 300,
            "token": "ADJ-TEST-CANCEL-TOKEN",
            "return_page": 2,
            "lang": "en",
        },
    )

    # Use a real aiogram frozen CallbackQuery
    cb = create_real_frozen_callback(user_id=888111, data="admin:adjust_cancel")

    # Verify that cb is indeed frozen
    with pytest.raises(Exception) as exc_info:
        cb.data = "illegal_mutation"
    assert "frozen" in str(exc_info.value).lower()

    # Invoke handle_admin_adjust_cancel
    await handle_admin_adjust_cancel(cb, state=state, session=db_session)

    # 1. Callback query data was NOT mutated
    assert cb.data == "admin:adjust_cancel"

    # 2. State was cleared
    assert state.current_state is None
    assert state.data == {}

    # 3. Callback was answered with alert
    cb.answer.assert_called_once_with("❌ Adjustment cancelled.", show_alert=True)

    # 4. User detail card was re-rendered for the target user with pagination context preserved
    cb.message.edit_text.assert_called_once()
    rendered_text = cb.message.edit_text.call_args[0][0]
    reply_markup = cb.message.edit_text.call_args[1]["reply_markup"]

    assert "User Detail Card" in rendered_text
    assert "77001" in rendered_text
    assert "@target_alice" in rendered_text

    # Verify back button points to return_page (2)
    nav_callbacks = [b.callback_data for row in reply_markup.inline_keyboard for b in row]
    assert "admin:users_page:2" in nav_callbacks

    # 5. User balance remains exactly untouched
    refreshed_bal = await get_minute_balance(db_session, target.id)
    assert refreshed_bal.purchased_seconds == 600

    # 6. No ledger events or idempotency records were created
    events = (await db_session.execute(
        select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == target.id)
    )).scalars().all()
    assert len(events) == 0

    idemp_records = (await db_session.execute(
        select(AdjustmentIdempotencyRecord)
    )).scalars().all()
    assert len(idemp_records) == 0


@pytest.mark.asyncio
async def test_handle_admin_adjust_cancel_without_target_user(db_session):
    """When target_user_id is not in FSM, cancel gracefully returns to paginated users."""
    settings.admin_ids_raw = "888111"

    state = FakeFSMContext(
        initial_state=AdminAdjustFSM.waiting_for_amount,
        initial_data={"return_page": 3},
    )

    cb = create_real_frozen_callback(user_id=888111, data="admin:adjust_cancel")

    await handle_admin_adjust_cancel(cb, state=state, session=db_session)

    assert state.current_state is None
    cb.answer.assert_called_once_with("❌ Adjustment cancelled.", show_alert=True)
    cb.message.edit_text.assert_called_once()
    assert "User Management" in cb.message.edit_text.call_args[0][0]


@pytest.mark.asyncio
async def test_handle_admin_adjust_cancel_unauthorized():
    """Unauthorized callers cannot execute adjustment cancel."""
    settings.admin_ids_raw = "888111"
    state = FakeFSMContext(initial_state=AdminAdjustFSM.confirming)

    cb = create_real_frozen_callback(user_id=999999, data="admin:adjust_cancel")

    await handle_admin_adjust_cancel(cb, state=state)

    cb.answer.assert_called_once_with("⛔ Access denied. Admin only.", show_alert=True)
    assert state.current_state == AdminAdjustFSM.confirming  # State preserved


# ---------------------------------------------------------------------------
# Phase 3: Functional Admin Operations Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_user_status_toggle_block_and_unblock(db_session):
    """Authorized admin can block and unblock a user with audit logging."""
    settings.admin_ids_raw = "888111"

    u = User(id=77002, username="bob_spammer", status=UserStatus.ACTIVE.value)
    db_session.add(u)
    await db_session.flush()

    # 1. Block user
    cb_block = create_real_frozen_callback(user_id=888111, data=f"admin:user_status:{u.id}:BLOCK:1")
    await handle_admin_user_toggle_status(cb_block, session=db_session)

    assert u.status == UserStatus.BLOCKED.value
    cb_block.answer.assert_called_once()
    assert "BLOCKED" in cb_block.answer.call_args[0][0]

    # Verify audit log
    audit_block = (await db_session.execute(
        select(AuditLog).where(AuditLog.target_id == str(u.id), AuditLog.action == "user.block")
    )).scalar_one_or_none()
    assert audit_block is not None
    assert audit_block.admin_id == 888111

    # 2. Unblock user
    cb_unblock = create_real_frozen_callback(user_id=888111, data=f"admin:user_status:{u.id}:UNBLOCK:1")
    await handle_admin_user_toggle_status(cb_unblock, session=db_session)

    assert u.status == UserStatus.ACTIVE.value
    cb_unblock.answer.assert_called_once()
    assert "ACTIVE" in cb_unblock.answer.call_args[0][0]

    # Verify audit log
    audit_unblock = (await db_session.execute(
        select(AuditLog).where(AuditLog.target_id == str(u.id), AuditLog.action == "user.unblock")
    )).scalar_one_or_none()
    assert audit_unblock is not None


@pytest.mark.asyncio
async def test_admin_user_status_toggle_self_block_prevented(db_session):
    """Admin cannot block their own account."""
    settings.admin_ids_raw = "888111"

    admin_user = User(id=888111, username="admin_self", status=UserStatus.ACTIVE.value)
    db_session.add(admin_user)
    await db_session.flush()

    cb = create_real_frozen_callback(user_id=888111, data="admin:user_status:888111:BLOCK:1")
    await handle_admin_user_toggle_status(cb, session=db_session)

    assert admin_user.status == UserStatus.ACTIVE.value
    cb.answer.assert_called_once()
    assert "cannot block your own admin account" in cb.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_admin_user_search_workflow(db_session):
    """Admin user search handles FSM flow, ID lookup, username lookup, and commands."""
    settings.admin_ids_raw = "888111"

    admin_user = User(id=888111, username="admin_boss", language_code="en", status=UserStatus.ACTIVE.value)
    u = User(id=77003, username="search_target", first_name="TargetCharlie")
    db_session.add_all([admin_user, u])
    await db_session.flush()

    state = FakeFSMContext()

    # 1. Start search callback
    cb_start = create_real_frozen_callback(user_id=888111, data="admin:user_search")
    await handle_admin_user_search_start(cb_start, state=state, session=db_session)

    assert state.current_state == AdminUserSearchFSM.waiting_for_query
    cb_start.message.edit_text.assert_called_once()
    assert "Search User" in cb_start.message.edit_text.call_args[0][0]

    # 2. Cancel search
    cb_cancel = create_real_frozen_callback(user_id=888111, data="admin:user_search_cancel")
    await handle_admin_user_search_cancel(cb_cancel, state=state, session=db_session)

    assert state.current_state is None
    cb_cancel.message.edit_text.assert_called_once()
    assert "User Management" in cb_cancel.message.edit_text.call_args[0][0]

    # 3. Search by ID query
    state.current_state = AdminUserSearchFSM.waiting_for_query
    msg_id = MagicMock(spec=Message)
    msg_id.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    msg_id.text = "77003"
    msg_id.answer = AsyncMock()

    await handle_admin_user_search_query(msg_id, state=state, session=db_session)

    assert state.current_state is None
    msg_id.answer.assert_called_once()
    rendered_id = msg_id.answer.call_args[0][0]
    assert "User Detail Card" in rendered_id
    assert "77003" in rendered_id

    # 4. Search by username with @
    state.current_state = AdminUserSearchFSM.waiting_for_query
    msg_uname = MagicMock(spec=Message)
    msg_uname.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    msg_uname.text = "@SEARCH_TARGET"  # case-insensitive
    msg_uname.answer = AsyncMock()

    await handle_admin_user_search_query(msg_uname, state=state, session=db_session)

    assert state.current_state is None
    msg_uname.answer.assert_called_once()
    rendered_uname = msg_uname.answer.call_args[0][0]
    assert "User Detail Card" in rendered_uname
    assert "search_target" in rendered_uname

    # 5. Search non-existent
    state.current_state = AdminUserSearchFSM.waiting_for_query
    msg_missing = MagicMock(spec=Message)
    msg_missing.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    msg_missing.text = "ghost_user_404"
    msg_missing.answer = AsyncMock()

    await handle_admin_user_search_query(msg_missing, state=state, session=db_session)

    msg_missing.answer.assert_called_once()
    not_found = msg_missing.answer.call_args[0][0]
    assert "No user found" in not_found

    # 6. Direct command /user 77003
    msg_cmd = MagicMock(spec=Message)
    msg_cmd.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    msg_cmd.text = "/user 77003"
    msg_cmd.answer = AsyncMock()

    await handle_admin_user_command(msg_cmd, session=db_session)

    msg_cmd.answer.assert_called_once()
    assert "User Detail Card" in msg_cmd.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_admin_pay_receipt_view(db_session):
    """Admin can view payment receipt slip file."""
    settings.admin_ids_raw = "888111"

    admin_user = User(id=888111, username="admin_boss", language_code="en", status=UserStatus.ACTIVE.value)
    target = User(id=77001, username="payer_alice")
    db_session.add_all([admin_user, target])
    await db_session.flush()

    # Payment with receipt
    p1 = PaymentRequest(
        id="PAY-SLIP-101",
        user_id=77001,
        package_code="MIN_30",
        minutes=30,
        amount_mmk=5000,
        receipt_file_id="AGBAA-TELEGRAM-FILE-ID-SLIP-123",
        status=PaymentStatus.PENDING.value,
    )
    # Payment without receipt
    p2 = PaymentRequest(
        id="PAY-NO-SLIP-102",
        user_id=77001,
        package_code="MIN_10",
        minutes=10,
        amount_mmk=2000,
        receipt_file_id=None,
        status=PaymentStatus.PENDING.value,
    )
    db_session.add_all([p1, p2])
    await db_session.flush()

    # 1. View receipt slip
    cb_with = create_real_frozen_callback(user_id=888111, data=f"admin:pay_receipt:{p1.id}")
    await handle_admin_pay_receipt(cb_with, session=db_session)

    cb_with.message.answer_photo.assert_called_once()
    assert cb_with.message.answer_photo.call_args[1]["photo"] == "AGBAA-TELEGRAM-FILE-ID-SLIP-123"

    # 2. Payment without receipt
    cb_without = create_real_frozen_callback(user_id=888111, data=f"admin:pay_receipt:{p2.id}")
    await handle_admin_pay_receipt(cb_without, session=db_session)

    cb_without.answer.assert_called_once()
    assert "not available" in cb_without.answer.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_admin_queue_failures_inspection(db_session):
    """Admin can inspect recent failed jobs with sanitized HTML error output."""
    settings.admin_ids_raw = "888111"

    u1 = User(id=77001, username="job_user_one")
    u2 = User(id=77002, username="job_user_two")
    db_session.add_all([u1, u2])
    await db_session.flush()

    # Insert 2 failed jobs
    unsafe_err = "VoxCPM2 crash: <script>alert(1)</script> & out of memory"
    j1 = Job(
        id=901,
        user_id=77001,
        status=JobStatus.FAILED.value,
        error_message=unsafe_err,
        created_at=datetime.datetime(2026, 10, 13, 10, 0, tzinfo=datetime.timezone.utc),
    )
    j2 = Job(
        id=902,
        user_id=77002,
        status=JobStatus.FAILED.value,
        error_message="Edge-TTS network timeout",
        created_at=datetime.datetime(2026, 10, 13, 10, 5, tzinfo=datetime.timezone.utc),
    )
    db_session.add_all([j1, j2])
    await db_session.flush()

    cb = create_real_frozen_callback(user_id=888111, data="admin:queue_failures")
    await handle_admin_queue_failures(cb, session=db_session)

    cb.message.edit_text.assert_called_once()
    text = cb.message.edit_text.call_args[0][0]
    parse_mode = cb.message.edit_text.call_args[1]["parse_mode"]
    assert parse_mode == "HTML"

    # Verify job numbers and HTML entity escaping
    assert "#901" in text
    assert "#902" in text
    assert "&lt;script&gt;" in text
    assert "<script>" not in text
    assert "Edge-TTS network timeout" in text


@pytest.mark.asyncio
async def test_admin_model_detail_render_helper(db_session):
    """render_admin_model_detail safely formats model card."""
    settings.admin_ids_raw = "888111"

    m = VoiceModel(
        id=55,
        name="TestModelCustom",
        slug="test-model-custom",
        model_dir="C:/models/test_model_custom",
        pth_path="C:/models/test.pth",
        index_path="C:/models/test.index",
        status="ACTIVE",
        type="SYSTEM",
        is_default_free=True,
    )
    db_session.add(m)
    await db_session.flush()

    cb = create_real_frozen_callback(user_id=888111, data="admin:model:55")
    await render_admin_model_detail(cb, model_id=m.id, session=db_session)

    cb.message.edit_text.assert_called_once()
    rendered = cb.message.edit_text.call_args[0][0]
    assert "TestModelCustom" in rendered
    assert "ACTIVE" in rendered
    assert "Yes ⭐" in rendered
