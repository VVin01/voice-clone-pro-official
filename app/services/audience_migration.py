from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.schema import ImportedAudience, User, utc_now

logger = logging.getLogger(__name__)


@dataclass
class AudienceImportResult:
    total_source_records: int = 0
    valid_ids_count: int = 0
    invalid_ids_count: int = 0
    already_imported_count: int = 0
    newly_imported_count: int = 0
    batch_name: str = "legacy_migration_v1"
    is_success: bool = True
    error_message: str | None = None


def inspect_legacy_database_users(legacy_db_path: Path | str) -> list[int]:
    """Read-only extraction of Telegram user IDs from the legacy SQLite database.

    Enforces read-only URI mode to prevent any writes to the source database.
    Does not extract or return any balances, history, referrals, or personal info.
    """
    path = Path(legacy_db_path)
    if not path.is_file():
        raise FileNotFoundError(f"Legacy database file not found at: {path}")

    # Connect strictly in read-only mode using SQLite URI
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        cur = conn.cursor()
        # Verify table exists
        table_check = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()
        if not table_check:
            raise RuntimeError(f"Legacy database at {path} does not contain a 'users' table.")

        # Extract only user IDs
        rows = cur.execute("SELECT id FROM users ORDER BY id ASC").fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()


async def import_legacy_audience(
    session: AsyncSession,
    legacy_db_path: Path | str | None = None,
    batch_name: str = "legacy_migration_v1",
) -> AudienceImportResult:
    """Idempotently import Telegram IDs from the legacy database into imported_audience.

    Safely filters invalid IDs, skips duplicates, and preserves full database integrity.
    Does not touch old balances or grant unearned minutes to imported users.
    """
    db_path = legacy_db_path or (settings.data_root / "voice_clone_official.db")
    result = AudienceImportResult(batch_name=batch_name)

    try:
        raw_ids = inspect_legacy_database_users(db_path)
        result.total_source_records = len(raw_ids)

        # Existing imported IDs in target session
        existing_imported_stmt = select(ImportedAudience.telegram_id)
        res = await session.execute(existing_imported_stmt)
        existing_imported_ids = set(res.scalars().all())

        # Existing registered users in new system
        existing_users_stmt = select(User.id)
        res_users = await session.execute(existing_users_stmt)
        registered_new_ids = set(res_users.scalars().all())

        now = utc_now()
        for raw_id in raw_ids:
            # Validate ID format (must be positive integer)
            try:
                uid = int(raw_id)
                if uid <= 0:
                    result.invalid_ids_count += 1
                    continue
            except (ValueError, TypeError):
                result.invalid_ids_count += 1
                continue

            result.valid_ids_count += 1

            if uid in existing_imported_ids:
                result.already_imported_count += 1
                continue

            # Insert new record into imported_audience
            record = ImportedAudience(
                telegram_id=uid,
                source_batch=batch_name,
                imported_at=now,
                is_registered_in_new_system=(uid in registered_new_ids),
                broadcast_eligible=True,
                notes=f"Imported from {Path(db_path).name}",
            )
            session.add(record)
            existing_imported_ids.add(uid)
            result.newly_imported_count += 1

        await session.flush()
        result.is_success = True
        logger.info(
            "Audience import completed: total=%d, valid=%d, invalid=%d, existing=%d, new=%d (batch=%s)",
            result.total_source_records,
            result.valid_ids_count,
            result.invalid_ids_count,
            result.already_imported_count,
            result.newly_imported_count,
            batch_name,
        )
        return result

    except Exception as exc:
        logger.exception("Audience import failed: %s", exc)
        result.is_success = False
        result.error_message = str(exc)
        return result
