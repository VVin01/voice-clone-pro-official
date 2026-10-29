from __future__ import annotations

from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import event
from app.config import settings
from app.db.base import Base

connect_args = {}
if settings.database_url.startswith("sqlite"):
    connect_args["check_same_thread"] = False
    connect_args["timeout"] = 15.0


def set_sqlite_pragma(dbapi_connection, connection_record=None) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=15000")
    cursor.close()


engine = create_async_engine(
    settings.database_url,
    echo=False,
    connect_args=connect_args,
    future=True,
)

if settings.database_url.startswith("sqlite"):
    event.listen(engine.sync_engine, "connect", set_sqlite_pragma)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


from app.models.schema import DatabaseMetadata, utc_now
from sqlalchemy import select


class DatabaseIdentityError(RuntimeError):
    """Raised when connecting to a database whose identity does not match expectations."""
    pass


async def verify_database_safety(session: AsyncSession) -> bool:
    """Verify that the connected database matches the expected identity and schema."""
    stmt = select(DatabaseMetadata).where(DatabaseMetadata.key == "db_identity")
    res = await session.execute(stmt)
    meta = res.scalar_one_or_none()
    if meta is None or meta.value != settings.expected_db_identity:
        actual_val = meta.value if meta else "NONE (uninitialized or legacy database)"
        raise DatabaseIdentityError(
            f"DATABASE IDENTITY SAFEGUARD ERROR: Expected database identity '{settings.expected_db_identity}', "
            f"but found '{actual_val}'. Aborting to prevent accidental operations on the wrong database!"
        )
    return True


async def init_db() -> None:
    """Create all tables and register database identity metadata."""
    if settings.storage_root:
        settings.storage_root.mkdir(parents=True, exist_ok=True)
    if settings.data_root:
        settings.data_root.mkdir(parents=True, exist_ok=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Ensure columns exist across SQLite and PostgreSQL databases
        def _add_missing_cols(connection):
            from sqlalchemy import text
            dialect_name = connection.dialect.name
            if dialect_name == "postgresql":
                # PostgreSQL safe IF NOT EXISTS column additions
                connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS recap_credits INTEGER DEFAULT 3"))
                connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS engine VARCHAR(20) DEFAULT 'voxcpm'"))
                connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS preset_voice VARCHAR(50)"))
                connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source_audio_path TEXT"))
                connection.execute(text("ALTER TABLE voice_models ADD COLUMN IF NOT EXISTS sample_audio_path TEXT"))
                connection.execute(text("ALTER TABLE payment_requests ADD COLUMN IF NOT EXISTS slip_url TEXT"))
            elif dialect_name == "sqlite":
                # SQLite PRAGMA check
                res_users = connection.execute(text("PRAGMA table_info(users)"))
                cols_users = [row[1] for row in res_users.fetchall()]
                if "recap_credits" not in cols_users:
                    connection.execute(text("ALTER TABLE users ADD COLUMN recap_credits INTEGER DEFAULT 3"))

                res = connection.execute(text("PRAGMA table_info(voice_models)"))
                cols = [row[1] for row in res.fetchall()]
                if "sample_audio_path" not in cols:
                    connection.execute(text("ALTER TABLE voice_models ADD COLUMN sample_audio_path TEXT"))

                res_jobs = connection.execute(text("PRAGMA table_info(jobs)"))
                cols_jobs = [row[1] for row in res_jobs.fetchall()]
                if "source_audio_path" not in cols_jobs:
                    connection.execute(text("ALTER TABLE jobs ADD COLUMN source_audio_path TEXT"))
                if "engine" not in cols_jobs:
                    connection.execute(text("ALTER TABLE jobs ADD COLUMN engine VARCHAR(20) DEFAULT 'voxcpm'"))
                if "preset_voice" not in cols_jobs:
                    connection.execute(text("ALTER TABLE jobs ADD COLUMN preset_voice VARCHAR(50)"))

                res_pay = connection.execute(text("PRAGMA table_info(payment_requests)"))
                cols_pay = [row[1] for row in res_pay.fetchall()]
                if "slip_url" not in cols_pay:
                    connection.execute(text("ALTER TABLE payment_requests ADD COLUMN slip_url TEXT"))

        await conn.run_sync(_add_missing_cols)

    # Initialize database identity metadata idempotently
    async with AsyncSessionLocal() as session:
        stmt = select(DatabaseMetadata).where(DatabaseMetadata.key == "db_identity")
        res = await session.execute(stmt)
        if res.scalar_one_or_none() is None:
            session.add(DatabaseMetadata(key="db_identity", value=settings.expected_db_identity))
            session.add(DatabaseMetadata(key="schema_version", value="2.0.0"))
            session.add(DatabaseMetadata(key="created_at", value=utc_now().isoformat()))
            await session.commit()

        from app.services.dynamic_settings import load_dynamic_settings
        await load_dynamic_settings(session)

        # Ensure VoxCPM Cloud AI model is active and available
        try:
            from app.models.schema import VoiceModel
            from app.models.enums import VoiceModelStatus, VoiceModelType
            stmt_v = select(VoiceModel).where(VoiceModel.slug == "voxcpm-cloud-ai")
            res_v = await session.execute(stmt_v)
            vox_m = res_v.scalar_one_or_none()
            if vox_m is None:
                session.add(VoiceModel(
                    name="🎙️ VoxCPM (Cloud AI)",
                    slug="voxcpm-cloud-ai",
                    type=VoiceModelType.SYSTEM.value,
                    status=VoiceModelStatus.ACTIVE.value,
                    allowed_for_free=True,
                    allowed_for_purchased=True,
                    is_default_free=True,
                    pth_path="voxcpm",
                    model_dir="cloud",
                ))
                await session.commit()
            elif vox_m.status != VoiceModelStatus.ACTIVE.value:
                vox_m.status = VoiceModelStatus.ACTIVE.value
                vox_m.allowed_for_free = True
                vox_m.allowed_for_purchased = True
                await session.commit()

            # Voice v2: no legacy trained-model system models are seeded. Legacy .pth rows
            # already in the database are left untouched, but new jobs for them are
            # rejected/mapped by queue.py and the worker. VoxCPM2 + reference audio
            # is the replacement path for those voices.
        except Exception as e_vox_init:
            import logging
            logging.getLogger(__name__).warning("Could not auto-register VoxCPM model: %s", e_vox_init)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
