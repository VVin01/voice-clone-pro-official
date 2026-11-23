from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import CallbackQuery, Message, User as TgUser

from app.config import settings
from app.models.enums import (
    AccountCategory,
    BroadcastCampaignStatus,
    BroadcastDeliveryStatus,
    UserStatus,
)
from app.models.schema import BroadcastCampaign, BroadcastDelivery, User
from app.services.broadcast import (
    BroadcastResult,
    create_or_get_broadcast_campaign,
    deliver_broadcast,
    get_broadcast_recipient_ids,
    mark_user_blocked,
    resume_broadcast_campaign,
)
from app.services.ledger import get_or_create_user
from app.ui.keyboards import (
    admin_broadcast_preview_kb,
    admin_broadcast_prompt_kb,
    admin_menu_kb,
    main_menu_kb,
)


class FakeFSMContext:
    """In-memory FSMContext replica for reliable unit testing."""
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
async def test_broadcast_keyboards_integration():
    """Verify admin dashboard includes broadcast button, user menu excludes it, and helper KBs format correctly."""
    # Admin landing keyboard (pulse menu) must include admin:broadcast
    adm_kb = admin_menu_kb(lang="en")
    adm_callbacks = [btn.callback_data for row in adm_kb.inline_keyboard for btn in row]
    assert "admin:broadcast" in adm_callbacks
    assert "Broadcast" in [btn.text for row in adm_kb.inline_keyboard for btn in row if btn.callback_data == "admin:broadcast"][0]

    # Normal user main menu keyboard must NEVER include admin:broadcast or admin:dashboard
    user_kb = main_menu_kb(is_admin=False, lang="en")
    user_callbacks = [btn.callback_data for row in user_kb.inline_keyboard for btn in row]
    assert "admin:broadcast" not in user_callbacks
    assert "admin:dashboard" not in user_callbacks

    # Helper keyboards
    prompt_kb = admin_broadcast_prompt_kb(lang="en")
    prompt_cbs = [btn.callback_data for row in prompt_kb.inline_keyboard for btn in row]
    assert prompt_cbs == ["admin:broadcast_cancel"]

    prev_kb = admin_broadcast_preview_kb(token="test123token", lang="en")
    prev_cbs = [btn.callback_data for row in prev_kb.inline_keyboard for btn in row]
    assert prev_cbs == ["admin:broadcast_send:test123token", "admin:broadcast_test:test123token", "admin:broadcast_cancel"]


@pytest.mark.asyncio
async def test_get_broadcast_recipient_ids_active_only(db_session):
    """Verify recipient queries select only ACTIVE users in ascending ID order."""
    u1 = User(id=3001, status=UserStatus.ACTIVE.value, account_category=AccountCategory.FREE.value)
    u2 = User(id=3002, status=UserStatus.BLOCKED.value, account_category=AccountCategory.FREE.value)
    u3 = User(id=3003, status=UserStatus.ACTIVE.value, account_category=AccountCategory.PURCHASED.value)
    db_session.add_all([u1, u2, u3])
    await db_session.flush()

    recipients = await get_broadcast_recipient_ids(db_session)
    assert 3001 in recipients
    assert 3003 in recipients
    assert 3002 not in recipients
    assert recipients == sorted(recipients)


@pytest.mark.asyncio
async def test_deliver_broadcast_success():
    """Verify plain text delivery loop to multiple recipients without errors."""
    bot = MagicMock()
    bot.send_message = AsyncMock()

    recipient_ids = [101, 102, 103]
    text = "Important system update: Maintenance complete."

    progress_calls = []
    async def track_progress(sent, failed, skipped):
        progress_calls.append((sent, failed, skipped))

    result = await deliver_broadcast(
        bot=bot,
        recipient_ids=recipient_ids,
        text=text,
        progress_callback=track_progress,
        delay_between_sends=0.0,
        progress_interval=1,
    )

    assert result.total == 3
    assert result.sent == 3
    assert result.failed == 0
    assert result.skipped == 0
    assert result.status == "COMPLETED"
    assert bot.send_message.call_count == 3
    # Verify plain text sending without parse_mode
    bot.send_message.assert_any_call(chat_id=101, text=text)
    bot.send_message.assert_any_call(chat_id=102, text=text)
    bot.send_message.assert_any_call(chat_id=103, text=text)
    assert len(progress_calls) >= 1


@pytest.mark.asyncio
async def test_deliver_broadcast_retry_after_handling():
    """Verify bounded retry when TelegramRetryAfter is raised."""
    bot = MagicMock()
    # User 201 raises retry once then succeeds; User 202 always raises retry (fails)
    retry_exc = TelegramRetryAfter(method=MagicMock(), message="Too many requests", retry_after=0.01)

    call_counts = {201: 0, 202: 0}

    async def mock_send(chat_id, text):
        call_counts[chat_id] += 1
        if chat_id == 201:
            if call_counts[chat_id] == 1:
                raise retry_exc
            return True
        elif chat_id == 202:
            raise retry_exc

    bot.send_message = AsyncMock(side_effect=mock_send)

    result = await deliver_broadcast(
        bot=bot,
        recipient_ids=[201, 202],
        text="Test Rate Limiting",
        delay_between_sends=0.0,
        max_retries=1,
    )

    assert result.total == 2
    assert result.sent == 1
    assert result.failed == 1
    assert result.skipped == 0


@pytest.mark.asyncio
async def test_deliver_broadcast_forbidden_and_bad_request_skipped():
    """Verify TelegramForbiddenError and TelegramBadRequest are isolated as skipped without aborting."""
    bot = MagicMock()
    forbidden_exc = TelegramForbiddenError(method=MagicMock(), message="Forbidden: bot was blocked by the user")
    bad_req_exc = TelegramBadRequest(method=MagicMock(), message="Bad Request: chat not found")

    async def mock_send(chat_id, text):
        if chat_id == 401:
            raise forbidden_exc
        elif chat_id == 402:
            raise bad_req_exc
        elif chat_id == 403:
            return True

    bot.send_message = AsyncMock(side_effect=mock_send)

    result = await deliver_broadcast(
        bot=bot,
        recipient_ids=[401, 402, 403],
        text="Resilience Test",
        delay_between_sends=0.0,
    )

    assert result.total == 3
    assert result.sent == 1
    assert result.failed == 0
    assert result.skipped == 2


@pytest.mark.asyncio
async def test_deliver_broadcast_generic_exception_isolated():
    """Verify unexpected exceptions are counted as failed and do not crash the campaign."""
    bot = MagicMock()

    async def mock_send(chat_id, text):
        if chat_id == 501:
            raise RuntimeError("Unexpected socket drop")
        return True

    bot.send_message = AsyncMock(side_effect=mock_send)

    result = await deliver_broadcast(
        bot=bot,
        recipient_ids=[501, 502],
        text="Exception Safety Test",
        delay_between_sends=0.0,
    )

    assert result.total == 2
    assert result.sent == 1
    assert result.failed == 1
    assert result.skipped == 0


# Bot-chat broadcast FSM handlers (handle_admin_broadcast_*) were retired in Round 13 Part K
# in favour of the Mini App Admin tab broadcast flow; durable broadcast service tests remain below.


# ---------------------------------------------------------------------------
# Phase D12-Fix: Broadcast Reliability Hardening Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deliver_broadcast_full_retry_after_honored():
    """Verify TelegramRetryAfter honors full requested delay (e.g. 17.5s) without capping at 5.0s."""
    bot = MagicMock()
    retry_exc = TelegramRetryAfter(method=MagicMock(), message="Flood wait", retry_after=17.5)

    calls = 0
    async def mock_send(chat_id, text):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise retry_exc
        return True

    bot.send_message = AsyncMock(side_effect=mock_send)
    sleep_calls = []

    async def mock_sleep(seconds):
        sleep_calls.append(seconds)

    result = await deliver_broadcast(
        bot=bot,
        recipient_ids=[7001],
        text="Testing Full Retry After",
        delay_between_sends=0.0,
        max_retries=2,
        sleep_func=mock_sleep,
    )

    assert result.total == 1
    assert result.sent == 1
    assert result.failed == 0
    # Must sleep the exact 17.5 seconds requested by Telegram (NOT capped at 5.0)
    assert 17.5 in sleep_calls
    assert any(s >= 17.5 for s in sleep_calls)


@pytest.mark.asyncio
async def test_deliver_broadcast_retry_budget_exhaustion():
    """Verify bounded retry when retries are exhausted records clean failure without premature send."""
    bot = MagicMock()
    retry_exc = TelegramRetryAfter(method=MagicMock(), message="Persistent rate limit", retry_after=3.0)
    bot.send_message = AsyncMock(side_effect=retry_exc)

    sleep_calls = []
    async def mock_sleep(seconds):
        sleep_calls.append(seconds)

    result = await deliver_broadcast(
        bot=bot,
        recipient_ids=[7002],
        text="Exhaustion Test",
        delay_between_sends=0.0,
        max_retries=2,
        sleep_func=mock_sleep,
    )

    assert result.total == 1
    assert result.sent == 0
    assert result.failed == 1
    # 2 retries = 2 sleeps of 3.0s
    assert sleep_calls.count(3.0) == 2


@pytest.mark.asyncio
async def test_deliver_broadcast_conservative_send_interval():
    """Verify default send interval is 0.1s and sleeps between consecutive recipient dispatches."""
    bot = MagicMock()
    bot.send_message = AsyncMock()

    sleep_calls = []
    async def mock_sleep(seconds):
        sleep_calls.append(seconds)

    result = await deliver_broadcast(
        bot=bot,
        recipient_ids=[7101, 7102, 7103],
        text="Cadence Test",
        sleep_func=mock_sleep,
    )

    assert result.total == 3
    assert result.sent == 3
    # 3 recipients -> 2 inter-message sleeps of default 0.1s
    assert sleep_calls.count(0.1) == 2


@pytest.mark.asyncio
async def test_deliver_broadcast_marks_user_blocked_on_forbidden(db_session):
    """Verify definitive TelegramForbiddenError flags the user bot_blocked.

    Since hunt B1 the flag is send-suppression ONLY: status must stay
    ACTIVE (status=BLOCKED is reserved for admin bans).
    """
    user, _ = await get_or_create_user(db_session, telegram_id=9101, language_code="en")
    assert user.status == UserStatus.ACTIVE.value
    await db_session.flush()

    bot = MagicMock()
    forbidden_exc = TelegramForbiddenError(method=MagicMock(), message="Forbidden: bot was blocked by the user")
    bot.send_message = AsyncMock(side_effect=forbidden_exc)

    result = await deliver_broadcast(
        bot=bot,
        recipient_ids=[9101],
        text="Blocked Test",
        delay_between_sends=0.0,
        session=db_session,
    )

    assert result.total == 1
    assert result.skipped == 1
    assert result.sent == 0

    # User must be flagged bot_blocked while status stays ACTIVE
    updated_user = await db_session.get(User, 9101)
    assert updated_user.bot_blocked is True
    assert updated_user.status == UserStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_deliver_broadcast_transient_error_preserves_user_active(db_session):
    """Verify transient network errors do NOT mark user as BLOCKED."""
    user, _ = await get_or_create_user(db_session, telegram_id=9102, language_code="en")
    assert user.status == UserStatus.ACTIVE.value
    await db_session.flush()

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=RuntimeError("Connection reset by peer"))

    result = await deliver_broadcast(
        bot=bot,
        recipient_ids=[9102],
        text="Transient Error Test",
        delay_between_sends=0.0,
        session=db_session,
    )

    assert result.total == 1
    assert result.failed == 1
    assert result.skipped == 0

    # User in database must remain ACTIVE
    updated_user = await db_session.get(User, 9102)
    assert updated_user.status == UserStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_durable_campaign_creation_and_idempotency(db_session):
    """Verify create_or_get_broadcast_campaign creates persistent records and is idempotent."""
    admin_id = 888001
    token = "token_durable_1"
    target_ids = [9301, 9302, 9303]

    # Create campaign
    c1 = await create_or_get_broadcast_campaign(
        session=db_session,
        admin_id=admin_id,
        token=token,
        text="Durable Broadcast",
        recipient_ids=target_ids,
    )

    assert c1.id is not None
    assert c1.status == BroadcastCampaignStatus.PENDING.value
    assert c1.total_recipients == 3

    # Check deliveries created
    deliveries = (await db_session.execute(
        BroadcastCampaign.__table__.select().where(BroadcastCampaign.id == c1.id)
    )).fetchall()
    assert len(deliveries) == 1

    # Idempotent call with same token
    c2 = await create_or_get_broadcast_campaign(
        session=db_session,
        admin_id=admin_id,
        token=token,
        text="Durable Broadcast",
        recipient_ids=target_ids,
    )
    assert c2.id == c1.id


@pytest.mark.asyncio
async def test_durable_campaign_tracking_and_resume(db_session):
    """Verify persistent per-recipient delivery tracking and safe resume without duplicate sends."""
    admin_id = 888002
    token = "token_resume_test"
    target_ids = [9401, 9402, 9403, 9404]

    campaign = await create_or_get_broadcast_campaign(
        session=db_session,
        admin_id=admin_id,
        token=token,
        text="Resume Campaign Test",
        recipient_ids=target_ids,
    )

    # First run: deliver to 9401 and 9402, then simulate crash on 9403
    sent_in_first_run = []
    async def mock_first_run_send(chat_id, text):
        if chat_id in (9401, 9402):
            sent_in_first_run.append(chat_id)
            return True
        elif chat_id == 9403:
            raise RuntimeError("Process interrupted mid-campaign (simulated crash)")
        return True

    bot_crash = MagicMock()
    bot_crash.send_message = AsyncMock(side_effect=mock_first_run_send)

    await deliver_broadcast(
        bot=bot_crash,
        recipient_ids=target_ids,
        text=campaign.message_text,
        delay_between_sends=0.0,
        session=db_session,
        campaign_id=campaign.id,
    )

    assert sent_in_first_run == [9401, 9402]

    # Verify database state after interruption
    d1 = (await db_session.execute(
        BroadcastDelivery.__table__.select().where(
            BroadcastDelivery.campaign_id == campaign.id,
            BroadcastDelivery.user_id == 9401,
        )
    )).fetchone()
    assert d1.status == BroadcastDeliveryStatus.SENT.value

    # Reset 9403 to PENDING to simulate clean resume of uncompleted deliveries
    from sqlalchemy import update
    await db_session.execute(
        update(BroadcastDelivery).where(
            BroadcastDelivery.campaign_id == campaign.id,
            BroadcastDelivery.user_id.in_([9403, 9404]),
        ).values(status=BroadcastDeliveryStatus.PENDING.value)
    )
    await db_session.commit()

    # Second run: Resume campaign
    sent_in_resume = []
    async def mock_resume_send(chat_id, text):
        sent_in_resume.append(chat_id)
        return True

    bot_resume = MagicMock()
    bot_resume.send_message = AsyncMock(side_effect=mock_resume_send)

    resumed_result = await resume_broadcast_campaign(
        bot=bot_resume,
        session=db_session,
        campaign_id=campaign.id,
        delay_between_sends=0.0,
    )

    # Exactly 9403 and 9404 must be sent; 9401 and 9402 must NOT be sent again
    assert sent_in_resume == [9403, 9404]
    assert 9401 not in sent_in_resume
    assert 9402 not in sent_in_resume
    assert resumed_result.total == 4


@pytest.mark.asyncio
async def test_durable_campaign_already_completed_no_duplicate_execution(db_session):
    """Verify an already completed campaign returns immediately without re-sending."""
    admin_id = 888003
    token = "token_completed_test"
    target_ids = [9501]

    campaign = await create_or_get_broadcast_campaign(
        session=db_session,
        admin_id=admin_id,
        token=token,
        text="Completed Test",
        recipient_ids=target_ids,
    )
    campaign.status = BroadcastCampaignStatus.COMPLETED.value
    await db_session.commit()

    bot = MagicMock()
    bot.send_message = AsyncMock()

    result = await deliver_broadcast(
        bot=bot,
        recipient_ids=target_ids,
        text="Completed Test",
        session=db_session,
        campaign_id=campaign.id,
    )

    assert result.status == BroadcastCampaignStatus.COMPLETED.value
    bot.send_message.assert_not_called()
