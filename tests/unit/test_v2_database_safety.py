from __future__ import annotations

import pytest
from app.db.session import DatabaseIdentityError, verify_database_safety
from app.models.schema import DatabaseMetadata


@pytest.mark.asyncio
async def test_verify_database_safety_success(db_session):
    """Database with key db_identity='voice_clone_v2' passes safety verification."""
    is_safe = await verify_database_safety(db_session)
    assert is_safe is True


@pytest.mark.asyncio
async def test_verify_database_safety_mismatch_fails(db_session):
    """Database with mismatched identity raises DatabaseIdentityError to protect production data."""
    meta = await db_session.get(DatabaseMetadata, "db_identity")
    if meta:
        meta.value = "legacy_production_db"
        await db_session.flush()

    with pytest.raises(DatabaseIdentityError) as exc_info:
        await verify_database_safety(db_session)

    assert "DATABASE IDENTITY SAFEGUARD ERROR" in str(exc_info.value)
    assert "legacy_production_db" in str(exc_info.value)


@pytest.mark.asyncio
async def test_verify_database_safety_missing_metadata_fails(db_session):
    """Database lacking identity metadata raises DatabaseIdentityError."""
    meta = await db_session.get(DatabaseMetadata, "db_identity")
    if meta:
        await db_session.delete(meta)
        await db_session.flush()

    with pytest.raises(DatabaseIdentityError) as exc_info:
        await verify_database_safety(db_session)

    assert "DATABASE IDENTITY SAFEGUARD ERROR" in str(exc_info.value)
    assert "NONE (uninitialized or legacy database)" in str(exc_info.value)
