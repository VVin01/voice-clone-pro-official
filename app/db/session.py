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


# Explicit pool sizing (audit fix 2026-11-03): the SQLAlchemy
# defaults (pool_size=5, max_overflow=10) exhausted in production —
# "QueuePool limit of size 5 overflow 10 reached" timeouts turned
# status polls into 30s hangs and 500s. pool_pre_ping discards
# connections the server already closed (Supabase/PgBouncer idle
# drops) instead of failing the first query that reuses them, and
# pool_recycle bounds connection age. In-memory SQLite uses a
# singleton StaticPool that rejects these arguments, hence the
# carve-out; file SQLite and Postgres both take the QueuePool path.
_pool_kwargs: dict = {}
if ":memory:" not in settings.database_url:
    _pool_kwargs = {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_timeout": settings.db_pool_timeout,
        "pool_recycle": settings.db_pool_recycle,
        "pool_pre_ping": True,
    }

engine = create_async_engine(
    settings.database_url,
    echo=False,
    connect_args=connect_args,
    future=True,
    **_pool_kwargs,
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


async def ensure_batch_aj_unique_indexes(engine) -> None:
    """Batch AJ: create the two money-safety unique indexes.

    Deliberately NOT declared in the models' ``__table_args__``:
    ``create_all`` would then try to build them against a production
    table that may already contain duplicates, and a failure there
    breaks boot. Instead each index is created here, in its own
    transaction, with IF NOT EXISTS, and ANY failure (including
    "existing rows violate it") is logged and skipped — the service-
    level checks (payments.create_payment_request's fingerprint
    guard, the recap ledger REFUND existence check) remain the
    primary enforcement; these indexes are the race backstop for
    when they do get created.

    * ``uq_pr_slip_fingerprint_live``: one live (PENDING) or APPROVED
      payment request per slip fingerprint — H1's one-slip-one-credit
      rule, across users. REJECTED rows are excluded so a rejected
      slip can legitimately be resubmitted.
    * ``uq_recap_ledger_refund_ref``: one recap-credit REFUND event
      per job reference — H8's second door on the refund race.
    """
    import logging

    from sqlalchemy import text

    log = logging.getLogger(__name__)
    statements = (
        (
            "uq_pr_slip_fingerprint_live",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_pr_slip_fingerprint_live "
            "ON payment_requests (slip_fingerprint) "
            "WHERE slip_fingerprint IS NOT NULL AND status NOT IN ('REJECTED', 'CANCELLED')",
        ),
        (
            "uq_recap_ledger_refund_ref",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_recap_ledger_refund_ref "
            "ON recap_credit_ledger_events (reference_id) "
            "WHERE substr(reference_id, 1, 7) = 'REFUND:' AND direction = 'CREDIT'",
        ),
    )
    for name, ddl in statements:
        try:
            async with engine.begin() as conn:
                if name == "uq_pr_slip_fingerprint_live":
                    dialect_name = conn.dialect.name
                    if dialect_name == "sqlite":
                        old_idx = (await conn.execute(text(
                            "SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_pr_slip_fingerprint_live'"
                        ))).scalar_one_or_none()
                        if old_idx and "CANCELLED" not in str(old_idx):
                            await conn.execute(text("DROP INDEX IF EXISTS uq_pr_slip_fingerprint_live"))
                    elif dialect_name == "postgresql":
                        old_pg = (await conn.execute(text(
                            "SELECT indexdef FROM pg_indexes WHERE indexname='uq_pr_slip_fingerprint_live'"
                        ))).scalar_one_or_none()
                        if old_pg and "CANCELLED" not in str(old_pg):
                            await conn.execute(text("DROP INDEX IF EXISTS uq_pr_slip_fingerprint_live"))
                await conn.execute(text(ddl))
        except Exception as exc:
            log.warning(
                "Batch AJ: could not create unique index %s (%s); "
                "service-level guards remain in force.",
                name, exc,
            )


async def ensure_hot_path_composite_indexes(engine) -> None:
    """Create composite indexes idempotently across SQLite and PostgreSQL."""
    import logging

    from sqlalchemy import text

    log = logging.getLogger(__name__)
    index_ddls = (
        "CREATE INDEX IF NOT EXISTS ix_jobs_status_created_at ON jobs (status, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_jobs_status_started_at ON jobs (status, started_at)",
        "CREATE INDEX IF NOT EXISTS ix_jobs_user_created_at ON jobs (user_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_mle_user_ref_dir ON minute_ledger_events (user_id, reference_id, direction)",
        "CREATE INDEX IF NOT EXISTS ix_rcle_ref_dir ON recap_credit_ledger_events (reference_id, direction)",
        "CREATE INDEX IF NOT EXISTS ix_pr_status_created_at ON payment_requests (status, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_audit_logs_created_at ON audit_logs (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_bd_campaign_status ON broadcast_deliveries (campaign_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_gae_status_created_at ON group_ai_escalations (status, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_pam_created_at ON private_ai_messages (created_at)",
    )
    for ddl in index_ddls:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(ddl))
        except Exception as exc:
            log.debug("Composite index creation skipped (%s): %s", ddl, exc)


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
                connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS bot_blocked BOOLEAN DEFAULT FALSE"))
                connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_test_account BOOLEAN DEFAULT FALSE"))
                connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS gemini_api_key TEXT"))
                connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS gemini_key_state VARCHAR(10) DEFAULT ''"))
                connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS engine VARCHAR(20) DEFAULT 'voxcpm'"))
                connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS preset_voice VARCHAR(50)"))
                connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source_audio_path TEXT"))
                connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS recap_config TEXT"))
                # Batch DP: park-and-ask state column + the widened
                # job-status constraint. Existing rows only ever hold
                # the old status set (a subset of the new one), so
                # dropping and re-adding the constraint validates
                # cleanly. Both statements are idempotent: the DROP
                # uses IF EXISTS and the re-ADD is a no-op in effect
                # when the constraint already carries the new set.
                connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS voice_choice_state TEXT"))
                connection.execute(text("ALTER TABLE jobs DROP CONSTRAINT IF EXISTS chk_job_status"))
                connection.execute(text(
                    "ALTER TABLE jobs ADD CONSTRAINT chk_job_status CHECK "
                    "(status IN ('PENDING', 'QUEUED', 'RUNNING', 'WAITING_CHOICE', 'SUCCESS', 'FAILED', 'CANCELLED'))"
                ))
                connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS reference_trimmed BOOLEAN DEFAULT FALSE"))
                connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tts_fallback_used BOOLEAN DEFAULT FALSE"))
                connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tts_route VARCHAR(24)"))
                connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS gemini_key_broken BOOLEAN DEFAULT FALSE"))
                connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS recap_ref_audio_path TEXT"))
                connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS final_script TEXT"))
                connection.execute(text("ALTER TABLE voice_models ADD COLUMN IF NOT EXISTS sample_audio_path TEXT"))
                connection.execute(text("ALTER TABLE payment_requests ADD COLUMN IF NOT EXISTS slip_url TEXT"))
                connection.execute(text("ALTER TABLE payment_requests ADD COLUMN IF NOT EXISTS admin_notify_copies TEXT"))
                connection.execute(text("ALTER TABLE payment_requests ADD COLUMN IF NOT EXISTS review_claimed_by BIGINT"))
                connection.execute(text("ALTER TABLE payment_requests ADD COLUMN IF NOT EXISTS review_claimed_at TIMESTAMP WITH TIME ZONE"))
                connection.execute(text("ALTER TABLE payment_requests ADD COLUMN IF NOT EXISTS original_minutes INTEGER"))
                connection.execute(text("ALTER TABLE payment_requests ADD COLUMN IF NOT EXISTS edited_by BIGINT"))
                connection.execute(text("ALTER TABLE payment_requests ADD COLUMN IF NOT EXISTS slip_fingerprint VARCHAR(128)"))
                connection.execute(text("ALTER TABLE payment_requests ADD COLUMN IF NOT EXISTS edited_recap_credits INTEGER"))
            elif dialect_name == "sqlite":
                # SQLite PRAGMA check
                res_users = connection.execute(text("PRAGMA table_info(users)"))
                cols_users = [row[1] for row in res_users.fetchall()]
                if "recap_credits" not in cols_users:
                    connection.execute(text("ALTER TABLE users ADD COLUMN recap_credits INTEGER DEFAULT 3"))
                if "bot_blocked" not in cols_users:
                    connection.execute(text("ALTER TABLE users ADD COLUMN bot_blocked BOOLEAN DEFAULT 0"))
                if "is_test_account" not in cols_users:
                    connection.execute(text("ALTER TABLE users ADD COLUMN is_test_account BOOLEAN DEFAULT 0"))
                if "gemini_api_key" not in cols_users:
                    connection.execute(text("ALTER TABLE users ADD COLUMN gemini_api_key TEXT"))
                if "gemini_key_state" not in cols_users:
                    connection.execute(text("ALTER TABLE users ADD COLUMN gemini_key_state VARCHAR(10) DEFAULT ''"))

                res = connection.execute(text("PRAGMA table_info(voice_models)"))
                cols = [row[1] for row in res.fetchall()]
                if "sample_audio_path" not in cols:
                    connection.execute(text("ALTER TABLE voice_models ADD COLUMN sample_audio_path TEXT"))

                res_jobs = connection.execute(text("PRAGMA table_info(jobs)"))
                cols_jobs = [row[1] for row in res_jobs.fetchall()]
                if "source_audio_path" not in cols_jobs:
                    connection.execute(text("ALTER TABLE jobs ADD COLUMN source_audio_path TEXT"))
                if "recap_config" not in cols_jobs:
                    connection.execute(text("ALTER TABLE jobs ADD COLUMN recap_config TEXT"))
                if "voice_choice_state" not in cols_jobs:
                    connection.execute(text("ALTER TABLE jobs ADD COLUMN voice_choice_state TEXT"))
                if "reference_trimmed" not in cols_jobs:
                    connection.execute(text("ALTER TABLE jobs ADD COLUMN reference_trimmed BOOLEAN DEFAULT 0"))
                if "tts_fallback_used" not in cols_jobs:
                    connection.execute(text("ALTER TABLE jobs ADD COLUMN tts_fallback_used BOOLEAN DEFAULT 0"))
                if "tts_route" not in cols_jobs:
                    connection.execute(text("ALTER TABLE jobs ADD COLUMN tts_route VARCHAR(24)"))
                if "gemini_key_broken" not in cols_jobs:
                    connection.execute(text("ALTER TABLE jobs ADD COLUMN gemini_key_broken BOOLEAN DEFAULT 0"))
                if "recap_ref_audio_path" not in cols_jobs:
                    connection.execute(text("ALTER TABLE jobs ADD COLUMN recap_ref_audio_path TEXT"))
                if "final_script" not in cols_jobs:
                    connection.execute(text("ALTER TABLE jobs ADD COLUMN final_script TEXT"))
                if "engine" not in cols_jobs:
                    connection.execute(text("ALTER TABLE jobs ADD COLUMN engine VARCHAR(20) DEFAULT 'voxcpm'"))
                if "preset_voice" not in cols_jobs:
                    connection.execute(text("ALTER TABLE jobs ADD COLUMN preset_voice VARCHAR(50)"))

                res_pay = connection.execute(text("PRAGMA table_info(payment_requests)"))
                cols_pay = [row[1] for row in res_pay.fetchall()]
                if "slip_url" not in cols_pay:
                    connection.execute(text("ALTER TABLE payment_requests ADD COLUMN slip_url TEXT"))
                if "admin_notify_copies" not in cols_pay:
                    connection.execute(text("ALTER TABLE payment_requests ADD COLUMN admin_notify_copies TEXT"))
                if "review_claimed_by" not in cols_pay:
                    connection.execute(text("ALTER TABLE payment_requests ADD COLUMN review_claimed_by BIGINT"))
                if "review_claimed_at" not in cols_pay:
                    connection.execute(text("ALTER TABLE payment_requests ADD COLUMN review_claimed_at TIMESTAMP"))
                if "original_minutes" not in cols_pay:
                    connection.execute(text("ALTER TABLE payment_requests ADD COLUMN original_minutes INTEGER"))
                if "edited_by" not in cols_pay:
                    connection.execute(text("ALTER TABLE payment_requests ADD COLUMN edited_by BIGINT"))
                if "slip_fingerprint" not in cols_pay:
                    connection.execute(text("ALTER TABLE payment_requests ADD COLUMN slip_fingerprint VARCHAR(128)"))
                if "edited_recap_credits" not in cols_pay:
                    connection.execute(text("ALTER TABLE payment_requests ADD COLUMN edited_recap_credits INTEGER"))

                # Migrate pre-existing SQLite jobs CHECK constraint if WAITING_CHOICE is missing.
                jobs_ddl = connection.execute(
                    text("SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'")
                ).scalar_one_or_none()
                if jobs_ddl and "chk_job_status" in jobs_ddl and "WAITING_CHOICE" not in jobs_ddl:
                    import re as _re

                    upgraded_ddl = _re.sub(
                        r"CONSTRAINT\s+chk_job_status\s+CHECK\s*\(\s*status\s+IN\s*\([^)]*\)\s*\)",
                        "CONSTRAINT chk_job_status CHECK (status IN ('PENDING', 'QUEUED', 'RUNNING', 'WAITING_CHOICE', 'SUCCESS', 'FAILED', 'CANCELLED'))",
                        jobs_ddl,
                        flags=_re.IGNORECASE,
                    )
                    if upgraded_ddl == jobs_ddl:
                        upgraded_ddl = _re.sub(
                            r"CHECK\s*\(\s*status\s+IN\s*\([^)]*\)\s*\)",
                            "CHECK (status IN ('PENDING', 'QUEUED', 'RUNNING', 'WAITING_CHOICE', 'SUCCESS', 'FAILED', 'CANCELLED'))",
                            jobs_ddl,
                            flags=_re.IGNORECASE,
                        )
                    if upgraded_ddl != jobs_ddl:
                        tmp_ddl = _re.sub(
                            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:[\"`\[]?jobs[\"`\]]?)",
                            "CREATE TABLE jobs__new_chk",
                            upgraded_ddl,
                            count=1,
                            flags=_re.IGNORECASE,
                        )
                        connection.execute(text("PRAGMA foreign_keys=OFF"))
                        try:
                            connection.execute(text("DROP TABLE IF EXISTS jobs__new_chk"))
                            connection.execute(text(tmp_ddl))
                            connection.execute(text("INSERT INTO jobs__new_chk SELECT * FROM jobs"))
                            connection.execute(text("DROP TABLE jobs"))
                            connection.execute(text("ALTER TABLE jobs__new_chk RENAME TO jobs"))
                            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_user_id ON jobs (user_id)"))
                        finally:
                            connection.execute(text("PRAGMA foreign_keys=ON"))

        await conn.run_sync(_add_missing_cols)

    # Batch AJ: defensive unique indexes (see the helper). Runs AFTER
    # the column additions above so slip_fingerprint exists.
    await ensure_batch_aj_unique_indexes(engine)
    await ensure_hot_path_composite_indexes(engine)

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

    # Part C follow-up: idempotently backfill opening CREDIT rows on
    # recap_credit_ledger_events for any pre-existing user whose
    # User.recap_credits is not yet derivable from ledger events.
    try:
        from app.services.ledger import backfill_recap_credit_opening_balances

        async with AsyncSessionLocal() as backfill_session:
            summary = await backfill_recap_credit_opening_balances(
                backfill_session, dry_run=False
            )
            if summary["users_backfilled"] > 0:
                await backfill_session.commit()
                import logging

                logging.getLogger(__name__).info(
                    "Recap credit opening balance backfill applied: %s", summary
                )
    except Exception as e_recap_backfill:
        import logging

        logging.getLogger(__name__).warning(
            "Recap credit opening balance backfill skipped: %s", e_recap_backfill
        )


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
