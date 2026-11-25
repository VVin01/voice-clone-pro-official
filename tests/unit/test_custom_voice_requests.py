import pytest
from unittest.mock import AsyncMock, MagicMock

from app.models.enums import AccountCategory, CustomVoiceRequestStatus
from app.services.custom_voice import (
    create_custom_voice_request,
    get_user_custom_voice_quota,
)
from app.services.ledger import get_or_create_user
# Bot cleanup 2026-11-03: the chat-side admin CVR workflow test
# retired with the admin:cvr:* screens. The service-level assign /
# reject tests below stay — services.custom_voice is unchanged.


@pytest.mark.asyncio
async def test_user_quota_free_vs_purchased(db_session):
    # Free user
    free_user, _ = await get_or_create_user(db_session, telegram_id=8801)
    quota_free = await get_user_custom_voice_quota(db_session, free_user.id)
    assert quota_free["is_purchased"] is False
    assert quota_free["is_admin"] is False
    assert quota_free["can_request"] is False
    assert quota_free["remaining_slots"] == 2

    # Purchased user
    purchased_user, _ = await get_or_create_user(db_session, telegram_id=8802)
    purchased_user.account_category = AccountCategory.PURCHASED.value
    await db_session.commit()

    quota_purchased = await get_user_custom_voice_quota(db_session, purchased_user.id)
    assert quota_purchased["is_purchased"] is True
    assert quota_purchased["can_request"] is True
    assert quota_purchased["max_allowed"] == 2
    assert quota_purchased["slots_used"] == 0
    assert quota_purchased["remaining_slots"] == 2


@pytest.mark.asyncio
async def test_free_user_cannot_create_request(db_session):
    free_user, _ = await get_or_create_user(db_session, telegram_id=8803)
    ok, req, err = await create_custom_voice_request(
        session=db_session,
        user_id=free_user.id,
        model_name="Nay Toe Voice",
        sample_file_id="audio_sample_001",
    )
    assert ok is False
    assert req is None
    assert err == "PURCHASED_TIER_REQUIRED"


@pytest.mark.asyncio
async def test_purchased_user_quota_enforcement(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=8804)
    user.account_category = AccountCategory.PURCHASED.value
    await db_session.commit()

    # 1. Create request 1
    ok1, req1, err1 = await create_custom_voice_request(
        session=db_session,
        user_id=user.id,
        model_name="Nay Toe",
        sample_file_id="sample_file_1",
        sample_file_name="nay_toe.mp3",
        sample_duration_seconds=60,
    )
    assert ok1 is True
    assert req1 is not None
    assert req1.status == CustomVoiceRequestStatus.PENDING.value
    assert req1.id.startswith("CVR-")

    q1 = await get_user_custom_voice_quota(db_session, user.id)
    assert q1["slots_used"] == 1
    assert q1["remaining_slots"] == 1
    assert q1["can_request"] is True

    # 2. Create request 2
    ok2, req2, err2 = await create_custom_voice_request(
        session=db_session,
        user_id=user.id,
        model_name="Rose",
        sample_file_id="sample_file_2",
        sample_file_name="rose.ogg",
        sample_duration_seconds=120,
    )
    assert ok2 is True
    assert req2 is not None

    q2 = await get_user_custom_voice_quota(db_session, user.id)
    assert q2["slots_used"] == 2
    assert q2["remaining_slots"] == 0
    assert q2["can_request"] is False

    # 3. Request 3 should be rejected due to quota limit
    ok3, req3, err3 = await create_custom_voice_request(
        session=db_session,
        user_id=user.id,
        model_name="Iron Man",
        sample_file_id="sample_file_3",
    )
    assert ok3 is False
    assert req3 is None
    assert err3 == "QUOTA_LIMIT_REACHED"


# Part B3 (2026-11-25): retired unmounted app/handlers/custom_voice.py and its DM handler tests.


