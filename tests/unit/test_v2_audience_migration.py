from __future__ import annotations

import pytest
import sqlite3
from sqlalchemy import select
from app.models.schema import ImportedAudience
from app.services.audience_migration import (
    import_legacy_audience,
    inspect_legacy_database_users,
)
from app.services.ledger import get_or_create_user


@pytest.fixture
def fake_legacy_db(tmp_path):
    """Create a temporary legacy SQLite database with users and legacy schema."""
    db_file = tmp_path / "legacy_test.db"
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            status TEXT DEFAULT 'ACTIVE',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.executemany(
        "INSERT INTO users (id, username, first_name, status) VALUES (?, ?, ?, ?)",
        [
            (11111, "alice", "Alice", "ACTIVE"),
            (22222, "bob", "Bob", "ACTIVE"),
            (33333, "charlie", "Charlie", "BLOCKED"),
            (44444, None, "Dave", "ACTIVE"),
            (-999, "invalid", "Invalid", "ACTIVE"),  # Non-positive ID
        ],
    )
    conn.commit()
    conn.close()
    return db_file


@pytest.mark.asyncio
async def test_inspect_legacy_database_users_readonly(fake_legacy_db):
    """Test read-only inspection of legacy DB without altering files."""
    mtime_before = fake_legacy_db.stat().st_mtime
    uids = inspect_legacy_database_users(fake_legacy_db)

    assert len(uids) == 5
    assert 11111 in uids
    assert 22222 in uids
    assert 33333 in uids
    assert 44444 in uids
    assert -999 in uids
    assert fake_legacy_db.stat().st_mtime == mtime_before


@pytest.mark.asyncio
async def test_import_legacy_audience_idempotency(db_session, fake_legacy_db):
    """Test importing legacy audience into ImportedAudience table idempotently."""
    # First import run
    res1 = await import_legacy_audience(db_session, legacy_db_path=fake_legacy_db, batch_name="test_batch_1")
    assert res1.total_source_records == 5
    assert res1.valid_ids_count == 4
    assert res1.newly_imported_count == 4
    assert res1.already_imported_count == 0
    assert res1.invalid_ids_count == 1

    # Verify rows in database
    stmt = select(ImportedAudience).order_by(ImportedAudience.telegram_id.asc())
    rows = (await db_session.execute(stmt)).scalars().all()
    assert len(rows) == 4
    assert [r.telegram_id for r in rows] == [11111, 22222, 33333, 44444]
    assert all(r.is_registered_in_new_system is False for r in rows)
    assert all(r.broadcast_eligible is True for r in rows)

    # Second import run (must be completely idempotent)
    res2 = await import_legacy_audience(db_session, legacy_db_path=fake_legacy_db, batch_name="test_batch_1")
    assert res2.total_source_records == 5
    assert res2.valid_ids_count == 4
    assert res2.newly_imported_count == 0
    assert res2.already_imported_count == 4

    # Count remained exactly 4
    rows_after = (await db_session.execute(stmt)).scalars().all()
    assert len(rows_after) == 4


@pytest.mark.asyncio
async def test_audience_registration_sync_on_interaction(db_session, fake_legacy_db):
    """When an imported user opens/registers with the new bot, is_registered_in_new_system becomes True."""
    await import_legacy_audience(db_session, legacy_db_path=fake_legacy_db)

    # User 11111 has not registered yet
    row_before = await db_session.get(ImportedAudience, 11111)
    assert row_before is not None
    assert row_before.is_registered_in_new_system is False

    # User 11111 triggers get_or_create_user (/start)
    user, is_new = await get_or_create_user(db_session, telegram_id=11111, username="alice_new")
    assert is_new is True

    # ImportedAudience row is now updated
    row_after = await db_session.get(ImportedAudience, 11111)
    assert row_after.is_registered_in_new_system is True
