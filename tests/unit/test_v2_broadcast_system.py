from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import pytest
from sqlalchemy import select

from app.models.enums import BroadcastRecipientGroup, UserStatus
from app.models.schema import BroadcastCampaign, BroadcastDelivery, ImportedAudience, User
from app.services.broadcast import (
    create_or_get_broadcast_campaign,
    deliver_broadcast,
    get_broadcast_recipient_ids,
    mark_user_blocked,
)
from app.services.ledger import get_or_create_user


@pytest.mark.asyncio
async def test_broadcast_recipient_groups_filtering(db_session):
    """Verify distinct recipient ID lists for IMPORTED_ALL, IMPORTED_UNREGISTERED, and REGISTERED_ALL."""
    # Seed Registered users
    u1, _ = await get_or_create_user(db_session, telegram_id=101)
    u2, _ = await get_or_create_user(db_session, telegram_id=102)
    u3, _ = await get_or_create_user(db_session, telegram_id=103)
    u3.status = UserStatus.BLOCKED.value
    await db_session.flush()

    # Seed Imported audience
    imp1 = ImportedAudience(telegram_id=101, is_registered_in_new_system=True, broadcast_eligible=True)
    imp2 = ImportedAudience(telegram_id=201, is_registered_in_new_system=False, broadcast_eligible=True)
    imp3 = ImportedAudience(telegram_id=202, is_registered_in_new_system=False, broadcast_eligible=True)
    imp4 = ImportedAudience(telegram_id=203, is_registered_in_new_system=False, broadcast_eligible=False)  # Ineligible
    db_session.add_all([imp1, imp2, imp3, imp4])
    await db_session.flush()

    # 1. REGISTERED_ALL: Only active registered users (101, 102; 103 blocked)
    reg_ids = await get_broadcast_recipient_ids(db_session, BroadcastRecipientGroup.REGISTERED_ALL)
    assert set(reg_ids) == {101, 102}

    # 2. IMPORTED_ALL: All broadcast eligible imported users (101, 201, 202; 203 ineligible)
    all_imp_ids = await get_broadcast_recipient_ids(db_session, BroadcastRecipientGroup.IMPORTED_ALL)
    assert set(all_imp_ids) == {101, 201, 202}

    # 3. IMPORTED_UNREGISTERED: Only unregistered and eligible imported users (201, 202; 101 registered)
    unreg_ids = await get_broadcast_recipient_ids(db_session, BroadcastRecipientGroup.IMPORTED_UNREGISTERED)
    assert set(unreg_ids) == {201, 202}


@pytest.mark.asyncio
async def test_deliver_broadcast_mocked_network(db_session):
    """Verify broadcast delivery tracks durable campaign state using a mocked bot (NEVER live network)."""
    recipients = [701, 702, 703]
    campaign = await create_or_get_broadcast_campaign(
        session=db_session,
        admin_id=999999999,
        token="tok_test_123",
        text="Special Announcement",
        recipient_ids=recipients,
    )
    assert campaign.total_recipients == 3
    assert campaign.status == "PENDING"

    # Mock Bot that accepts messages
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    result = await deliver_broadcast(
        bot=mock_bot,
        recipient_ids=recipients,
        text="Special Announcement",
        session=db_session,
        campaign_id=campaign.id,
        delay_between_sends=0.001,
    )

    assert result.total == 3
    assert result.sent == 3
    assert result.failed == 0
    assert mock_bot.send_message.call_count == 3

    # Check updated campaign status in DB
    refreshed_campaign = await db_session.get(BroadcastCampaign, campaign.id)
    assert refreshed_campaign.status == "COMPLETED"
    assert refreshed_campaign.sent_count == 3


@pytest.mark.asyncio
async def test_mark_user_blocked_updates_imported_audience(db_session):
    """Marking a user blocked flags both User and ImportedAudience."""
    u, _ = await get_or_create_user(db_session, telegram_id=801)
    imp = ImportedAudience(telegram_id=801, broadcast_eligible=True)
    db_session.add(imp)
    await db_session.commit()

    await mark_user_blocked(db_session, 801)

    refreshed_user = await db_session.get(User, 801)
    refreshed_imp = await db_session.get(ImportedAudience, 801)
    assert refreshed_user.status == UserStatus.BLOCKED.value
    assert refreshed_imp.broadcast_eligible is False
