import asyncio
import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.enums import AccountCategory, LedgerDirection, LedgerSource
from app.models.schema import (
    MinuteBalance,
    MinuteLedgerEvent,
    Referral,
    ReferralReward,
    User,
    WelcomeBonusAward,
)
from app.services.ledger import (
    award_welcome_bonus,
    confirm_referral_and_reward,
    get_minute_balance,
    get_or_create_user,
)


@pytest.mark.asyncio
async def test_register_new_user_success(db_session):
    """1. Registering a new user successfully."""
    user, is_new = await get_or_create_user(
        db_session,
        telegram_id=987654321,
        username="new_user",
        first_name="New",
        language_code="en",
    )
    assert is_new is True
    assert user.id == 987654321
    assert user.username == "new_user"
    assert user.first_name == "New"
    assert user.language_code == "en"
    assert user.referral_code == "ref_987654321"

    # Verify MinuteBalance created
    bal = await get_minute_balance(db_session, user.id)
    assert bal is not None
    assert bal.user_id == 987654321
    assert bal.total_usable_seconds == 0


@pytest.mark.asyncio
async def test_register_repeated_same_user_idempotent(db_session):
    """2. Calling registration repeatedly for the same Telegram user without creating duplicates."""
    target_id = 8529151995

    # First call
    user1, is_new1 = await get_or_create_user(
        db_session,
        telegram_id=target_id,
        username="original_user",
        first_name="Original",
    )
    assert is_new1 is True
    assert user1.id == target_id
    assert user1.referral_code == f"ref_{target_id}"

    # Repeated calls should return the same user with is_new=False
    for i in range(5):
        user_repeat, is_new_repeat = await get_or_create_user(
            db_session,
            telegram_id=target_id,
            username=f"updated_user_{i}",
            first_name="Updated",
        )
        assert is_new_repeat is False
        assert user_repeat.id == target_id
        assert user_repeat.username == f"updated_user_{i}"
        assert user_repeat.referral_code == f"ref_{target_id}"

    # Verify strictly 1 user row and 1 balance row exist in DB
    users = (await db_session.execute(select(User).where(User.id == target_id))).scalars().all()
    assert len(users) == 1

    balances = (await db_session.execute(select(MinuteBalance).where(MinuteBalance.user_id == target_id))).scalars().all()
    assert len(balances) == 1


@pytest.mark.asyncio
async def test_referral_code_collision_with_another_user(db_session):
    """3. Handling a referral-code collision with another user."""
    # Pre-existing user with code ref_5555555555
    existing_user = User(
        id=1111111111,
        username="first_user",
        referral_code="ref_5555555555",
        account_category=AccountCategory.FREE.value,
    )
    db_session.add(existing_user)
    bal1 = MinuteBalance(user_id=1111111111)
    db_session.add(bal1)
    await db_session.commit()

    # Now user 5555555555 registers. Their default code ref_5555555555 collides with existing_user!
    new_user, is_new = await get_or_create_user(
        db_session,
        telegram_id=5555555555,
        username="second_user",
    )
    await db_session.commit()

    assert is_new is True
    assert new_user.id == 5555555555
    # The new user must receive a unique alternative code (e.g. ref_5555555555_<suffix>)
    assert new_user.referral_code != "ref_5555555555"
    assert new_user.referral_code.startswith("ref_5555555555_")
    assert len(new_user.referral_code) <= 32

    # Existing user's referral code was NOT overwritten or mutated
    check_first = await db_session.get(User, 1111111111)
    assert check_first.referral_code == "ref_5555555555"

    # Both users exist independently in DB
    all_users = (await db_session.execute(select(User).where(User.id.in_([1111111111, 5555555555])))).scalars().all()
    assert len(all_users) == 2


@pytest.mark.asyncio
async def test_concurrent_registration_attempts(tmp_path):
    """4. Concurrent registration attempts for the same new user ID."""
    db_file = tmp_path / "test_conc_reg.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False)

    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    target_user_id = 8529151995

    sem = asyncio.Semaphore(5)

    async def register_attempt(worker_id: int):
        async with sem:
            for attempt in range(10):
                async with session_maker() as s:
                    try:
                        u, is_new = await get_or_create_user(
                            s,
                            telegram_id=target_user_id,
                            username=f"worker_{worker_id}",
                        )
                        await s.commit()
                        return is_new
                    except Exception:
                        await s.rollback()
                        await asyncio.sleep(0.01 * (attempt + 1))
            return None

    # Run 10 concurrent registration requests for the same target user ID
    results = await asyncio.gather(*(register_attempt(i) for i in range(10)))

    # Exactly 1 request should have is_new=True, and the other 9 should have is_new=False
    new_count = sum(1 for r in results if r is True)
    existing_count = sum(1 for r in results if r is False)

    assert new_count == 1, f"Expected exactly 1 new user registration, got {new_count}"
    assert existing_count == 9, f"Expected 9 existing user retrievals, got {existing_count}"

    # Verify exactly 1 user row and 1 MinuteBalance row exist in the database
    async with session_maker() as s:
        users = (await s.execute(select(User).where(User.id == target_user_id))).scalars().all()
        assert len(users) == 1
        assert users[0].referral_code == f"ref_{target_user_id}"

        balances = (await s.execute(select(MinuteBalance).where(MinuteBalance.user_id == target_user_id))).scalars().all()
        assert len(balances) == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_transaction_rollback_and_recovery_after_failed_insert(db_session):
    """5. Correct transaction rollback and recovery after a failed insert inside session."""
    target_id = 7788990011

    # Simulate an error condition or collision by intentionally inserting a user with the target referral code
    blocker = User(
        id=12345,
        username="blocker",
        referral_code=f"ref_{target_id}",
        account_category=AccountCategory.FREE.value,
    )
    db_session.add(blocker)
    db_session.add(MinuteBalance(user_id=12345))
    await db_session.commit()

    # get_or_create_user must catch the collision, roll back the savepoint, generate a unique alternate, and succeed
    user, is_new = await get_or_create_user(db_session, telegram_id=target_id, username="recovered_user")
    await db_session.commit()

    assert is_new is True
    assert user.id == target_id
    assert user.referral_code != f"ref_{target_id}"
    assert user.referral_code.startswith(f"ref_{target_id}_")

    # Session must be completely healthy and usable for subsequent queries
    queried_user = await db_session.get(User, target_id)
    assert queried_user is not None
    assert queried_user.username == "recovered_user"


@pytest.mark.asyncio
async def test_no_duplicate_welcome_bonuses_or_referral_rewards(db_session):
    """6. No duplicate minute-ledger entries, welcome bonuses, or referral rewards."""
    referrer_id = 1122334455
    referred_id = 9988776655

    # 1. Register referrer
    referrer, _ = await get_or_create_user(db_session, telegram_id=referrer_id)
    await db_session.commit()

    # Award welcome bonus to referrer
    awarded, bal = await award_welcome_bonus(db_session, referrer_id)
    assert awarded is True
    assert bal.welcome_seconds == 300
    await db_session.commit()

    # Attempt second welcome bonus
    awarded2, bal2 = await award_welcome_bonus(db_session, referrer_id)
    assert awarded2 is False
    assert bal2.welcome_seconds == 300
    await db_session.commit()

    # 2. Register referred user with referrer's referral code
    referred_user, is_new = await get_or_create_user(
        db_session,
        telegram_id=referred_id,
        referral_code_arg=referrer.referral_code,
    )
    await db_session.commit()
    assert is_new is True

    # Repeated get_or_create_user for referred user with the same referral code
    repeat_user, repeat_is_new = await get_or_create_user(
        db_session,
        telegram_id=referred_id,
        referral_code_arg=referrer.referral_code,
    )
    await db_session.commit()
    assert repeat_is_new is False

    # Confirm and reward referral first time
    was_pending, rewarded, idx = await confirm_referral_and_reward(db_session, referred_id)
    assert was_pending is True
    assert rewarded is True
    assert idx == 1
    await db_session.commit()

    # Attempt second confirmation of the same referral
    was_pending2, rewarded2, idx2 = await confirm_referral_and_reward(db_session, referred_id)
    assert was_pending2 is False
    assert rewarded2 is False
    await db_session.commit()

    # Verify MinuteLedgerEvent counts for referrer:
    # Exactly 1 WELCOME credit (+300s) and 1 REFERRAL credit (+30s)
    events = (await db_session.execute(
        select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == referrer_id)
    )).scalars().all()
    assert len(events) == 2
    sources = [e.source for e in events]
    assert sources.count(LedgerSource.WELCOME.value) == 1
    assert sources.count(LedgerSource.REFERRAL.value) == 1

    # Verify WelcomeBonusAward record count
    welcome_awards = (await db_session.execute(
        select(WelcomeBonusAward).where(WelcomeBonusAward.user_id == referrer_id)
    )).scalars().all()
    assert len(welcome_awards) == 1

    # Verify ReferralReward record count
    rewards = (await db_session.execute(
        select(ReferralReward).where(ReferralReward.referrer_id == referrer_id)
    )).scalars().all()
    assert len(rewards) == 1
