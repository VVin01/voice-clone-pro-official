import asyncio
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import event, select, text

from sqlalchemy.pool import StaticPool
from app.db.base import Base
from app.db.session import connect_args as app_connect_args, set_sqlite_pragma
from app.models.schema import MinuteLedgerEvent, ReferralReward, User
from app.services.ledger import (
    award_welcome_bonus,
    confirm_referral_and_reward,
    get_minute_balance,
    get_or_create_user,
)


@pytest.mark.asyncio
async def test_concurrent_welcome_bonus_awarding():
    """Simulate 10 concurrent requests to award welcome bonus on the same account."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        echo=False,
    )
    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with session_maker() as s:
        user, _ = await get_or_create_user(s, telegram_id=9001)
        await s.commit()

    async def run_award():
        async with session_maker() as s:
            try:
                res, _ = await award_welcome_bonus(s, 9001)
                await s.commit()
                return res
            except Exception:
                await s.rollback()
                return False

    results = await asyncio.gather(*(run_award() for _ in range(10)))
    awarded_count = sum(1 for r in results if r is True)
    assert awarded_count == 1, f"Expected exactly 1 award, got {awarded_count}"

    async with session_maker() as s:
        bal = await get_minute_balance(s, 9001)
        assert bal.welcome_seconds == 300
        events = (await s.execute(
            select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == 9001)
        )).scalars().all()
        assert len(events) == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_referral_rewarding(tmp_path):
    """Simulate 30 concurrent referral confirmations to verify that exactly 20 are rewarded."""
    db_file = tmp_path / "test_conc_ref.db"
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

    async with session_maker() as s:
        referrer, _ = await get_or_create_user(s, telegram_id=8000)
        # Create 30 pending referrals
        for i in range(1, 31):
            ref_user, _ = await get_or_create_user(s, telegram_id=8000 + i, referral_code_arg=referrer.referral_code)
        await s.commit()

    sem = asyncio.Semaphore(3)
    async def run_confirm(user_id: int):
        async with sem:
            for attempt in range(15):
                async with session_maker() as s:
                    try:
                        res = await confirm_referral_and_reward(s, user_id)
                        await s.commit()
                        return res
                    except Exception:
                        await s.rollback()
                        await asyncio.sleep(0.01 * (attempt + 1))
            return (False, False, 0)

    # Run in gather
    results = await asyncio.gather(*(run_confirm(8000 + i) for i in range(1, 31)))
    async with session_maker() as s:
        bal = await get_minute_balance(s, 8000)
        assert bal.referral_seconds == 600  # Exactly 20 * 30s = 600s
        rewards = (await s.execute(
            select(ReferralReward).where(ReferralReward.referrer_id == 8000)
        )).scalars().all()
        assert len(rewards) == 20

    await engine.dispose()


@pytest.mark.asyncio
async def test_set_sqlite_pragma_busy_timeout(tmp_path):
    """Verify set_sqlite_pragma configures busy_timeout=15000, foreign_keys=ON, and WAL mode."""
    db_file = tmp_path / "test_pragma.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False)
    event.listen(engine.sync_engine, "connect", set_sqlite_pragma)

    async with engine.connect() as conn:
        busy_timeout = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
        foreign_keys = (await conn.execute(text("PRAGMA foreign_keys"))).scalar()
        journal_mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()

    assert busy_timeout == 15000
    assert foreign_keys == 1
    assert str(journal_mode).lower() == "wal"

    await engine.dispose()


def test_app_connect_args_timeout():
    """Verify connect_args in app.db.session includes check_same_thread and 15s timeout for SQLite."""
    from app.config import settings

    if settings.database_url.startswith("sqlite"):
        assert app_connect_args.get("check_same_thread") is False
        assert app_connect_args.get("timeout") == 15.0


@pytest.mark.asyncio
async def test_sqlite_busy_timeout_contention_resolution(tmp_path):
    """Verify that concurrent writes resolve cleanly without immediate lock errors under busy_timeout."""
    db_file = tmp_path / "test_contention.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_file}",
        connect_args={"timeout": 15.0},
        echo=False,
    )
    event.listen(engine.sync_engine, "connect", set_sqlite_pragma)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    started_tx1 = asyncio.Event()

    async def tx1():
        async with session_maker() as s1:
            await get_or_create_user(s1, telegram_id=7001)
            started_tx1.set()
            await asyncio.sleep(0.3)
            await s1.commit()

    async def tx2():
        await started_tx1.wait()
        async with session_maker() as s2:
            await get_or_create_user(s2, telegram_id=7002)
            await s2.commit()

    await asyncio.gather(tx1(), tx2())

    async with session_maker() as s:
        u1 = (await s.execute(select(User).where(User.id == 7001))).scalar_one_or_none()
        u2 = (await s.execute(select(User).where(User.id == 7002))).scalar_one_or_none()
        assert u1 is not None
        assert u2 is not None

    await engine.dispose()
