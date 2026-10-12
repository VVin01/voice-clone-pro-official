"""
Phase D14-A-Fix-2 — Focused tests for narrow IntegrityError discrimination
in admin_adjust_balance.

All tests use isolated temporary databases. Production DB is never touched.
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.enums import AccountCategory, LedgerDirection, LedgerSource
from app.models.schema import (
    AdjustmentIdempotencyRecord,
    AuditLog,
    MinuteBalance,
    MinuteLedgerEvent,
    User,
)
from app.services.ledger import admin_adjust_balance, get_minute_balance


# ---------------------------------------------------------------------------
# Isolated DB helpers
# ---------------------------------------------------------------------------

def _make_engine(db_url: str):
    engine = create_async_engine(db_url, echo=False)

    @event.listens_for(engine.sync_engine, "connect")
    def set_pragmas(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


async def _bootstrap(db_url: str):
    """Create schema and return (engine, session_factory)."""
    engine = _make_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    return engine, factory


async def _seed_user(factory, user_id: int):
    async with factory() as s:
        u = User(id=user_id, account_category=AccountCategory.FREE.value)
        s.add(u)
        await s.commit()


# ---------------------------------------------------------------------------
# Test 1: Sequential duplicate — exactly one adjustment applied
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sequential_duplicate_suppressed(tmp_path):
    """Sequential double-tap with the same key must apply the credit exactly once."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'seq_dup.db'}"
    engine, factory = await _bootstrap(db_url)
    await _seed_user(factory, user_id=1001)

    token = "SEQ-DUP-TOKEN-001"

    async with factory() as s:
        bal1 = await admin_adjust_balance(
            session=s,
            user_id=1001,
            admin_id=9000,
            seconds=300,
            source=LedgerSource.PURCHASE,
            direction=LedgerDirection.CREDIT,
            reason="First call",
            idempotency_key=token,
        )
        await s.commit()

    # Repeat with the same session (simulates handler retry)
    async with factory() as s:
        bal2 = await admin_adjust_balance(
            session=s,
            user_id=1001,
            admin_id=9000,
            seconds=300,
            source=LedgerSource.PURCHASE,
            direction=LedgerDirection.CREDIT,
            reason="Second call (replay)",
            idempotency_key=token,
        )
        await s.commit()

    # Verify final DB state
    async with factory() as s:
        final = await get_minute_balance(s, 1001)
        assert final.purchased_seconds == 300, "Expected exactly 300s — duplicate must be suppressed"

        events = (await s.execute(
            select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == 1001)
        )).scalars().all()
        assert len(events) == 1, f"Expected exactly 1 ledger event, got {len(events)}"

        recs = (await s.execute(
            select(AdjustmentIdempotencyRecord).where(
                AdjustmentIdempotencyRecord.idempotency_key == token
            )
        )).scalars().all()
        assert len(recs) == 1, f"Expected exactly 1 idempotency record, got {len(recs)}"

        audits = (await s.execute(
            select(AuditLog).where(AuditLog.target_id == "1001")
        )).scalars().all()
        assert len(audits) == 1, f"Expected exactly 1 audit log, got {len(audits)}"

    await engine.dispose()


# ---------------------------------------------------------------------------
# Test 2: Concurrent duplicate — exactly one adjustment applied
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_duplicate_suppressed(tmp_path):
    """Two sessions racing with the same idempotency key must result in exactly one credit."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'concurrent_fix.db'}"
    engine, factory = await _bootstrap(db_url)
    await _seed_user(factory, user_id=2001)

    token = "CONCURRENT-FIX-TOKEN-002"

    async def attempt(worker_id: int):
        async with factory() as s:
            try:
                bal = await admin_adjust_balance(
                    session=s,
                    user_id=2001,
                    admin_id=9000,
                    seconds=300,
                    source=LedgerSource.PURCHASE,
                    direction=LedgerDirection.CREDIT,
                    reason=f"Worker {worker_id} concurrent attempt",
                    idempotency_key=token,
                )
                await s.commit()
                return "ok", bal.purchased_seconds
            except Exception as e:
                await s.rollback()
                return "err", str(e)

    results = await asyncio.gather(attempt(1), attempt(2))
    outcomes = [r[0] for r in results]

    # At least one must succeed, none may double-apply
    assert "ok" in outcomes, f"Expected at least one success, got: {results}"

    async with factory() as s:
        final = await get_minute_balance(s, 2001)
        assert final.purchased_seconds == 300, (
            f"Expected exactly 300s after concurrent race, got {final.purchased_seconds}s"
        )

        events = (await s.execute(
            select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == 2001)
        )).scalars().all()
        assert len(events) == 1, f"Expected exactly 1 ledger event, got {len(events)}"

        recs = (await s.execute(
            select(AdjustmentIdempotencyRecord).where(
                AdjustmentIdempotencyRecord.idempotency_key == token
            )
        )).scalars().all()
        assert len(recs) == 1, f"Expected exactly 1 idempotency record, got {len(recs)}"

    await engine.dispose()


# ---------------------------------------------------------------------------
# Test 3: Non-idempotency IntegrityError is propagated, no partial mutation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_idempotency_integrity_error_propagated(tmp_path):
    """A FK or CHECK violation must NOT be silently swallowed as a duplicate.
    The original IntegrityError must propagate and leave zero balance/ledger/audit mutations.
    """
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'integrity_reraise.db'}"
    engine, factory = await _bootstrap(db_url)

    # Do NOT seed user 3001 — calling adjust for a nonexistent user will trigger
    # a FK violation when the AdjustmentIdempotencyRecord references users.id.
    # The pre-check at the start of admin_adjust_balance raises ValueError("User not found")
    # before reaching the savepoint; so to test the IntegrityError reraise path we need
    # to directly exercise the except block.
    #
    # Strategy: seed a valid user, then monkeypatch the AdjustmentIdempotencyRecord insert
    # to raise an IntegrityError that is NOT a PK collision on the idempotency key.
    # We do this by inserting a record with a deliberately bad user_id FK after the fact,
    # triggering the error inside the savepoint, while the idempotency key itself never
    # gets committed (so the fresh SELECT confirms: key not found → re-raise).

    await _seed_user(factory, user_id=3001)

    token = "NON-IDEMP-FK-TOKEN-003"

    original_add = None

    call_count = [0]

    async with factory() as s:
        # Patch session.flush inside begin_nested to raise an IntegrityError
        # on first call (simulating a CHECK or FK failure, not a PK collision).
        original_flush = s.flush

        async def patched_flush(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # Simulate a constraint error that is NOT an idempotency PK collision.
                # We raise IntegrityError directly — the idempotency record was never
                # actually committed so the fresh SELECT will return None → re-raise.
                raise IntegrityError(
                    "UNIQUE constraint failed: some_other_table.col",
                    params={},
                    orig=Exception("simulated non-idempotency constraint violation"),
                )
            return await original_flush(*args, **kwargs)

        s.flush = patched_flush

        with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
            await admin_adjust_balance(
                session=s,
                user_id=3001,
                admin_id=9000,
                seconds=300,
                source=LedgerSource.PURCHASE,
                direction=LedgerDirection.CREDIT,
                reason="Test non-idempotency error propagation",
                idempotency_key=token,
            )

    # Confirm nothing was committed to the DB
    async with factory() as s:
        final = await get_minute_balance(s, 3001)
        assert final.purchased_seconds == 0, (
            f"Balance must remain 0 after propagated IntegrityError, got {final.purchased_seconds}s"
        )

        events = (await s.execute(
            select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == 3001)
        )).scalars().all()
        assert len(events) == 0, f"No ledger events expected, got {len(events)}"

        recs = (await s.execute(
            select(AdjustmentIdempotencyRecord).where(
                AdjustmentIdempotencyRecord.idempotency_key == token
            )
        )).scalars().all()
        assert len(recs) == 0, f"No idempotency record expected, got {len(recs)}"

        audits = (await s.execute(
            select(AuditLog).where(AuditLog.target_id == "3001")
        )).scalars().all()
        assert len(audits) == 0, f"No audit log expected, got {len(audits)}"

    await engine.dispose()


# ---------------------------------------------------------------------------
# Test 4: Distinct idempotency keys allow separate legitimate adjustments
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_distinct_keys_allow_separate_adjustments(tmp_path):
    """Two different idempotency keys must both be applied independently."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'distinct_keys.db'}"
    engine, factory = await _bootstrap(db_url)
    await _seed_user(factory, user_id=4001)

    async with factory() as s:
        bal1 = await admin_adjust_balance(
            session=s,
            user_id=4001,
            admin_id=9000,
            seconds=300,
            source=LedgerSource.PURCHASE,
            direction=LedgerDirection.CREDIT,
            reason="First distinct credit",
            idempotency_key="DISTINCT-KEY-A",
        )
        await s.commit()

    async with factory() as s:
        bal2 = await admin_adjust_balance(
            session=s,
            user_id=4001,
            admin_id=9000,
            seconds=600,
            source=LedgerSource.PURCHASE,
            direction=LedgerDirection.CREDIT,
            reason="Second distinct credit",
            idempotency_key="DISTINCT-KEY-B",
        )
        await s.commit()

    async with factory() as s:
        final = await get_minute_balance(s, 4001)
        assert final.purchased_seconds == 900, (
            f"Expected 900s (300+600), got {final.purchased_seconds}s"
        )

        events = (await s.execute(
            select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == 4001)
        )).scalars().all()
        assert len(events) == 2, f"Expected 2 ledger events, got {len(events)}"

        recs = (await s.execute(
            select(AdjustmentIdempotencyRecord)
        )).scalars().all()
        assert len(recs) == 2, f"Expected 2 idempotency records, got {len(recs)}"

        audits = (await s.execute(
            select(AuditLog).where(AuditLog.target_id == "4001")
        )).scalars().all()
        assert len(audits) == 2, f"Expected 2 audit logs, got {len(audits)}"

    await engine.dispose()


# ---------------------------------------------------------------------------
# Test 5: Success path does not render before commit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_success_before_commit(tmp_path):
    """admin_adjust_balance must not mutate the DB until the caller calls commit().
    If the caller rolls back instead, the balance must remain unchanged.
    """
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'no_success_before_commit.db'}"
    engine, factory = await _bootstrap(db_url)
    await _seed_user(factory, user_id=5001)

    async with factory() as s:
        await admin_adjust_balance(
            session=s,
            user_id=5001,
            admin_id=9000,
            seconds=300,
            source=LedgerSource.PURCHASE,
            direction=LedgerDirection.CREDIT,
            reason="Must be rolled back",
            idempotency_key="ROLLBACK-BEFORE-COMMIT-KEY",
        )
        # Explicitly roll back — simulating an error in the handler after adjust but before commit
        await s.rollback()

    async with factory() as s:
        final = await get_minute_balance(s, 5001)
        assert final.purchased_seconds == 0, (
            f"Balance must be 0 after rollback, got {final.purchased_seconds}s"
        )

        events = (await s.execute(
            select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == 5001)
        )).scalars().all()
        assert len(events) == 0, f"No events expected after rollback, got {len(events)}"

        recs = (await s.execute(
            select(AdjustmentIdempotencyRecord)
        )).scalars().all()
        assert len(recs) == 0, f"No idempotency records expected after rollback, got {len(recs)}"

    await engine.dispose()


# ---------------------------------------------------------------------------
# Test 6: IntegrityError reraise does not expose confirmed-duplicate path
#         when idempotency_key is None (no key provided)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_integrity_error_without_key_always_reraised(tmp_path):
    """When no idempotency_key is provided and an IntegrityError occurs,
    it must always be re-raised (there is no idempotency record to confirm).
    """
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'no_key_reraise.db'}"
    engine, factory = await _bootstrap(db_url)
    await _seed_user(factory, user_id=6001)

    call_count = [0]

    async with factory() as s:
        original_flush = s.flush

        async def patched_flush(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise IntegrityError(
                    "CHECK constraint failed: some_table",
                    params={},
                    orig=Exception("simulated check constraint"),
                )
            return await original_flush(*args, **kwargs)

        s.flush = patched_flush

        with pytest.raises(IntegrityError):
            await admin_adjust_balance(
                session=s,
                user_id=6001,
                admin_id=9000,
                seconds=300,
                source=LedgerSource.PURCHASE,
                direction=LedgerDirection.CREDIT,
                reason="No-key integrity error test",
                idempotency_key=None,   # <-- no key provided
            )

    await engine.dispose()
