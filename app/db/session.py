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
else:
    from app.db.diagnostics import install_postgres_session_listener

    install_postgres_session_listener(
        engine.sync_engine,
        engine_kind="async",
        lock_timeout_ms=10000,
        idle_in_tx_timeout_ms=60000,
    )

from app.db.diagnostics import register_engine_watchdog as _register_engine_watchdog

_register_engine_watchdog(engine.sync_engine, engine_kind="async")

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
        (
            "uq_pr_payment_reference",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_pr_payment_reference "
            "ON payment_requests (payment_reference) "
            "WHERE payment_reference IS NOT NULL",
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
                connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS recap_credits DOUBLE PRECISION DEFAULT 3.0"))
                connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS recap_credit_seconds BIGINT"))
                connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS reserved_recap_seconds BIGINT DEFAULT 0"))
                connection.execute(text(
                    "DO $$ BEGIN "
                    "IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='recap_credits' AND data_type IN ('integer','bigint')) THEN "
                    "ALTER TABLE users ALTER COLUMN recap_credits TYPE DOUBLE PRECISION USING recap_credits::double precision; "
                    "END IF; END $$;"
                ))
                connection.execute(text("UPDATE users SET recap_credit_seconds = CAST(ROUND(COALESCE(recap_credits, 3) * 60) AS BIGINT) WHERE recap_credit_seconds IS NULL"))
                connection.execute(text("ALTER TABLE minute_balances ADD COLUMN IF NOT EXISTS daily_free_seconds BIGINT DEFAULT 0"))
                connection.execute(text("ALTER TABLE minute_balances ADD COLUMN IF NOT EXISTS last_daily_grant_date VARCHAR(10)"))
                connection.execute(text("ALTER TABLE minute_balances ADD COLUMN IF NOT EXISTS referral_voice_seconds BIGINT DEFAULT 0"))
                connection.execute(text("ALTER TABLE minute_balances ADD COLUMN IF NOT EXISTS reserved_daily_free_seconds BIGINT DEFAULT 0"))
                connection.execute(text("ALTER TABLE minute_balances ADD COLUMN IF NOT EXISTS reserved_monthly_free_seconds BIGINT DEFAULT 0"))
                connection.execute(text("ALTER TABLE minute_balances ADD COLUMN IF NOT EXISTS reserved_welcome_seconds BIGINT DEFAULT 0"))
                connection.execute(text("ALTER TABLE minute_balances ADD COLUMN IF NOT EXISTS reserved_referral_voice_seconds BIGINT DEFAULT 0"))
                connection.execute(text("ALTER TABLE minute_balances ADD COLUMN IF NOT EXISTS reserved_purchased_seconds BIGINT DEFAULT 0"))
                connection.execute(text("ALTER TABLE wallet_reservations ADD COLUMN IF NOT EXISTS reserved_voice_daily BIGINT DEFAULT 0"))
                connection.execute(text("ALTER TABLE wallet_reservations ADD COLUMN IF NOT EXISTS reserved_voice_daily_day VARCHAR(10)"))
                connection.execute(text("ALTER TABLE wallet_reservations ADD COLUMN IF NOT EXISTS reserved_voice_referral BIGINT DEFAULT 0"))
                connection.execute(text("ALTER TABLE minute_ledger_events DROP CONSTRAINT IF EXISTS chk_ledger_source"))
                connection.execute(text(
                    "ALTER TABLE minute_ledger_events ADD CONSTRAINT chk_ledger_source CHECK "
                    "(source IN ('DAILY_FREE', 'MONTHLY_FREE', 'WELCOME', 'REFERRAL', 'PURCHASE', 'ADJUST', 'CONSUME'))"
                ))
                connection.execute(text("ALTER TABLE referral_rewards DROP CONSTRAINT IF EXISTS chk_referral_reward_index_range"))
                connection.execute(text(
                    "ALTER TABLE referral_rewards ADD CONSTRAINT chk_referral_reward_index_range CHECK (reward_index >= 1)"
                ))
                connection.execute(text("ALTER TABLE pricing_plans ADD COLUMN IF NOT EXISTS name VARCHAR(80) DEFAULT ''"))
                connection.execute(text("ALTER TABLE pricing_plans ADD COLUMN IF NOT EXISTS category VARCHAR(20) DEFAULT 'voice'"))
                connection.execute(text("ALTER TABLE pricing_plans ADD COLUMN IF NOT EXISTS sub_type VARCHAR(20) DEFAULT 'voice'"))
                connection.execute(text("ALTER TABLE pricing_plans ADD COLUMN IF NOT EXISTS recap_credits INTEGER DEFAULT 0"))
                connection.execute(text("ALTER TABLE pricing_plans ADD COLUMN IF NOT EXISTS is_topup BOOLEAN DEFAULT FALSE"))
                connection.execute(text("ALTER TABLE pricing_plans ADD COLUMN IF NOT EXISTS badge VARCHAR(30) DEFAULT ''"))
                connection.execute(text("ALTER TABLE pricing_plans ADD COLUMN IF NOT EXISTS validity_days INTEGER DEFAULT 30"))
                connection.execute(text("ALTER TABLE pricing_plans ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE"))
                connection.execute(text("ALTER TABLE wallet_transactions DROP CONSTRAINT IF EXISTS chk_wallet_tx_type"))
                connection.execute(text(
                    "ALTER TABLE wallet_transactions ADD CONSTRAINT chk_wallet_tx_type CHECK "
                    "(tx_type IN ('PURCHASE', 'RESERVE', 'DEDUCT', 'RELEASE', 'REFUND', 'ADMIN_ADJUST', 'BONUS', 'EXPIRE'))"
                ))
                connection.execute(text("ALTER TABLE recap_credit_ledger_events ADD COLUMN IF NOT EXISTS seconds BIGINT DEFAULT 0"))
                connection.execute(text(
                    "DO $$ BEGIN "
                    "IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recap_credit_ledger_events' AND column_name='credits' AND data_type IN ('integer','bigint')) THEN "
                    "ALTER TABLE recap_credit_ledger_events ALTER COLUMN credits TYPE DOUBLE PRECISION USING credits::double precision; "
                    "END IF; END $$;"
                ))
                connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS bot_blocked BOOLEAN DEFAULT FALSE"))
                connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_test_account BOOLEAN DEFAULT FALSE"))
                connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS gemini_api_key TEXT"))
                connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS gemini_key_state VARCHAR(10) DEFAULT ''"))
                connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS engine VARCHAR(20) DEFAULT 'voxcpm'"))
                connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS preset_voice VARCHAR(50)"))
                connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source_audio_path TEXT"))
                connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS recap_config TEXT"))
                connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS output_duration_seconds INTEGER DEFAULT 0"))
                connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS voice_audio_seconds INTEGER DEFAULT 0"))
                connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS recap_credits_charged DOUBLE PRECISION DEFAULT 0"))
                connection.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS output_size_bytes BIGINT DEFAULT 0"))
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
                connection.execute(text("ALTER TABLE payment_requests ADD COLUMN IF NOT EXISTS recap_credits INTEGER DEFAULT 0"))
                connection.execute(text("ALTER TABLE payment_requests ADD COLUMN IF NOT EXISTS validity_days INTEGER DEFAULT 30"))
                connection.execute(text("ALTER TABLE payment_requests ADD COLUMN IF NOT EXISTS payment_reference VARCHAR(128)"))
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
                    connection.execute(text("ALTER TABLE users ADD COLUMN recap_credits REAL DEFAULT 3.0"))
                if "recap_credit_seconds" not in cols_users:
                    connection.execute(text("ALTER TABLE users ADD COLUMN recap_credit_seconds BIGINT"))
                    connection.execute(text("UPDATE users SET recap_credit_seconds = CAST(ROUND(COALESCE(recap_credits, 3) * 60) AS INTEGER) WHERE recap_credit_seconds IS NULL"))
                if "reserved_recap_seconds" not in cols_users:
                    connection.execute(text("ALTER TABLE users ADD COLUMN reserved_recap_seconds BIGINT DEFAULT 0"))
                if "bot_blocked" not in cols_users:
                    connection.execute(text("ALTER TABLE users ADD COLUMN bot_blocked BOOLEAN DEFAULT 0"))
                if "is_test_account" not in cols_users:
                    connection.execute(text("ALTER TABLE users ADD COLUMN is_test_account BOOLEAN DEFAULT 0"))
                if "gemini_api_key" not in cols_users:
                    connection.execute(text("ALTER TABLE users ADD COLUMN gemini_api_key TEXT"))
                if "gemini_key_state" not in cols_users:
                    connection.execute(text("ALTER TABLE users ADD COLUMN gemini_key_state VARCHAR(10) DEFAULT ''"))

                res_mb = connection.execute(text("PRAGMA table_info(minute_balances)"))
                cols_mb = [row[1] for row in res_mb.fetchall()]
                for _mb_col in (
                    "daily_free_seconds",
                    "referral_voice_seconds",
                    "reserved_daily_free_seconds",
                    "reserved_monthly_free_seconds",
                    "reserved_welcome_seconds",
                    "reserved_referral_voice_seconds",
                    "reserved_purchased_seconds",
                ):
                    if _mb_col not in cols_mb:
                        connection.execute(text(f"ALTER TABLE minute_balances ADD COLUMN {_mb_col} BIGINT DEFAULT 0"))
                if "last_daily_grant_date" not in cols_mb:
                    connection.execute(text("ALTER TABLE minute_balances ADD COLUMN last_daily_grant_date VARCHAR(10)"))

                res_wr = connection.execute(text("PRAGMA table_info(wallet_reservations)"))
                cols_wr = [row[1] for row in res_wr.fetchall()]
                for _wr_col in ("reserved_voice_daily", "reserved_voice_referral"):
                    if _wr_col not in cols_wr:
                        connection.execute(text(f"ALTER TABLE wallet_reservations ADD COLUMN {_wr_col} BIGINT DEFAULT 0"))
                if "reserved_voice_daily_day" not in cols_wr:
                    connection.execute(text("ALTER TABLE wallet_reservations ADD COLUMN reserved_voice_daily_day VARCHAR(10)"))

                res_pp = connection.execute(text("PRAGMA table_info(pricing_plans)"))
                cols_pp = [row[1] for row in res_pp.fetchall()]
                if "name" not in cols_pp:
                    connection.execute(text("ALTER TABLE pricing_plans ADD COLUMN name VARCHAR(80) DEFAULT ''"))
                if "category" not in cols_pp:
                    connection.execute(text("ALTER TABLE pricing_plans ADD COLUMN category VARCHAR(20) DEFAULT 'voice'"))
                if "sub_type" not in cols_pp:
                    connection.execute(text("ALTER TABLE pricing_plans ADD COLUMN sub_type VARCHAR(20) DEFAULT 'voice'"))
                if "recap_credits" not in cols_pp:
                    connection.execute(text("ALTER TABLE pricing_plans ADD COLUMN recap_credits INTEGER DEFAULT 0"))
                if "is_topup" not in cols_pp:
                    connection.execute(text("ALTER TABLE pricing_plans ADD COLUMN is_topup BOOLEAN DEFAULT 0"))
                if "badge" not in cols_pp:
                    connection.execute(text("ALTER TABLE pricing_plans ADD COLUMN badge VARCHAR(30) DEFAULT ''"))
                if "validity_days" not in cols_pp:
                    connection.execute(text("ALTER TABLE pricing_plans ADD COLUMN validity_days INTEGER DEFAULT 30"))
                if "updated_at" not in cols_pp:
                    connection.execute(text("ALTER TABLE pricing_plans ADD COLUMN updated_at TIMESTAMP"))

                res_rcle = connection.execute(text("PRAGMA table_info(recap_credit_ledger_events)"))
                cols_rcle = [row[1] for row in res_rcle.fetchall()]
                if "seconds" not in cols_rcle:
                    connection.execute(text("ALTER TABLE recap_credit_ledger_events ADD COLUMN seconds BIGINT DEFAULT 0"))

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
                if "output_duration_seconds" not in cols_jobs:
                    connection.execute(text("ALTER TABLE jobs ADD COLUMN output_duration_seconds INTEGER DEFAULT 0"))
                if "voice_audio_seconds" not in cols_jobs:
                    connection.execute(text("ALTER TABLE jobs ADD COLUMN voice_audio_seconds INTEGER DEFAULT 0"))
                if "recap_credits_charged" not in cols_jobs:
                    connection.execute(text("ALTER TABLE jobs ADD COLUMN recap_credits_charged REAL DEFAULT 0"))
                if "output_size_bytes" not in cols_jobs:
                    connection.execute(text("ALTER TABLE jobs ADD COLUMN output_size_bytes BIGINT DEFAULT 0"))

                res_pay = connection.execute(text("PRAGMA table_info(payment_requests)"))
                cols_pay = [row[1] for row in res_pay.fetchall()]
                if "slip_url" not in cols_pay:
                    connection.execute(text("ALTER TABLE payment_requests ADD COLUMN slip_url TEXT"))
                if "recap_credits" not in cols_pay:
                    connection.execute(text("ALTER TABLE payment_requests ADD COLUMN recap_credits INTEGER DEFAULT 0"))
                if "validity_days" not in cols_pay:
                    connection.execute(text("ALTER TABLE payment_requests ADD COLUMN validity_days INTEGER DEFAULT 30"))
                if "payment_reference" not in cols_pay:
                    connection.execute(text("ALTER TABLE payment_requests ADD COLUMN payment_reference VARCHAR(128)"))
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

                # Migrate pre-existing SQLite wallet_transactions CHECK constraint if EXPIRE is missing.
                wt_ddl = connection.execute(
                    text("SELECT sql FROM sqlite_master WHERE type='table' AND name='wallet_transactions'")
                ).scalar_one_or_none()
                if wt_ddl and "chk_wallet_tx_type" in wt_ddl and "'EXPIRE'" not in wt_ddl:
                    import re as _re2
                    upgraded_wt = _re2.sub(
                        r"CONSTRAINT\s+chk_wallet_tx_type\s+CHECK\s*\(\s*tx_type\s+IN\s*\([^)]*\)\s*\)",
                        "CONSTRAINT chk_wallet_tx_type CHECK (tx_type IN ('PURCHASE', 'RESERVE', 'DEDUCT', 'RELEASE', 'REFUND', 'ADMIN_ADJUST', 'BONUS', 'EXPIRE'))",
                        wt_ddl,
                        flags=_re2.IGNORECASE,
                    )
                    if upgraded_wt != wt_ddl:
                        tmp_wt = _re2.sub(
                            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:[\"`\[]?wallet_transactions[\"`\]]?)",
                            "CREATE TABLE wallet_transactions__new_chk",
                            upgraded_wt,
                            count=1,
                            flags=_re2.IGNORECASE,
                        )
                        connection.execute(text("PRAGMA foreign_keys=OFF"))
                        try:
                            connection.execute(text("DROP TABLE IF EXISTS wallet_transactions__new_chk"))
                            connection.execute(text(tmp_wt))
                            connection.execute(text("INSERT INTO wallet_transactions__new_chk SELECT * FROM wallet_transactions"))
                            connection.execute(text("DROP TABLE wallet_transactions"))
                            connection.execute(text("ALTER TABLE wallet_transactions__new_chk RENAME TO wallet_transactions"))
                            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_wt_user_created ON wallet_transactions (user_id, created_at)"))
                        finally:
                            connection.execute(text("PRAGMA foreign_keys=ON"))

                # Migrate pre-existing SQLite minute_ledger_events CHECK constraint if DAILY_FREE is missing.
                mle_ddl = connection.execute(
                    text("SELECT sql FROM sqlite_master WHERE type='table' AND name='minute_ledger_events'")
                ).scalar_one_or_none()
                if mle_ddl and "chk_ledger_source" in mle_ddl and "'DAILY_FREE'" not in mle_ddl:
                    import re as _re3
                    upgraded_mle = _re3.sub(
                        r"CONSTRAINT\s+chk_ledger_source\s+CHECK\s*\(\s*source\s+IN\s*\([^)]*\)\s*\)",
                        "CONSTRAINT chk_ledger_source CHECK (source IN ('DAILY_FREE', 'MONTHLY_FREE', 'WELCOME', 'REFERRAL', 'PURCHASE', 'ADJUST', 'CONSUME'))",
                        mle_ddl,
                        flags=_re3.IGNORECASE,
                    )
                    if upgraded_mle != mle_ddl:
                        tmp_mle = _re3.sub(
                            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:[\"`\[]?minute_ledger_events[\"`\]]?)",
                            "CREATE TABLE minute_ledger_events__new_chk",
                            upgraded_mle,
                            count=1,
                            flags=_re3.IGNORECASE,
                        )
                        connection.execute(text("PRAGMA foreign_keys=OFF"))
                        try:
                            connection.execute(text("DROP TABLE IF EXISTS minute_ledger_events__new_chk"))
                            connection.execute(text(tmp_mle))
                            connection.execute(text("INSERT INTO minute_ledger_events__new_chk SELECT * FROM minute_ledger_events"))
                            connection.execute(text("DROP TABLE minute_ledger_events"))
                            connection.execute(text("ALTER TABLE minute_ledger_events__new_chk RENAME TO minute_ledger_events"))
                            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_mle_user_ref_dir ON minute_ledger_events (user_id, reference_id, direction)"))
                        finally:
                            connection.execute(text("PRAGMA foreign_keys=ON"))

                # Migrate pre-existing SQLite referral_rewards CHECK constraint to drop the 20-friend ceiling.
                rr_ddl = connection.execute(
                    text("SELECT sql FROM sqlite_master WHERE type='table' AND name='referral_rewards'")
                ).scalar_one_or_none()
                if rr_ddl and "reward_index <= 20" in rr_ddl:
                    import re as _re4
                    upgraded_rr = _re4.sub(
                        r"CONSTRAINT\s+chk_referral_reward_index_range\s+CHECK\s*\([^)]*\)",
                        "CONSTRAINT chk_referral_reward_index_range CHECK (reward_index >= 1)",
                        rr_ddl,
                        flags=_re4.IGNORECASE,
                    )
                    if upgraded_rr != rr_ddl:
                        tmp_rr = _re4.sub(
                            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:[\"`\[]?referral_rewards[\"`\]]?)",
                            "CREATE TABLE referral_rewards__new_chk",
                            upgraded_rr,
                            count=1,
                            flags=_re4.IGNORECASE,
                        )
                        connection.execute(text("PRAGMA foreign_keys=OFF"))
                        try:
                            connection.execute(text("DROP TABLE IF EXISTS referral_rewards__new_chk"))
                            connection.execute(text(tmp_rr))
                            connection.execute(text("INSERT INTO referral_rewards__new_chk SELECT * FROM referral_rewards"))
                            connection.execute(text("DROP TABLE referral_rewards"))
                            connection.execute(text("ALTER TABLE referral_rewards__new_chk RENAME TO referral_rewards"))
                            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_referral_rewards_referrer_id ON referral_rewards (referrer_id)"))
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

        try:
            from app.services.payments import _ensure_pricing_plans_seeded
            await _ensure_pricing_plans_seeded(session)
            await session.commit()
        except Exception as e_plan_seed:
            import logging
            logging.getLogger(__name__).warning("Pricing plan seed skipped: %s", e_plan_seed)

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

    # 30-day expiration transition: idempotently grandfather pre-existing paid balances
    # into LEGACY_GRANDFATHER grants (expires_at=NULL) so existing customers keep their balances.
    try:
        from app.services.billing import backfill_legacy_wallet_grants

        async with AsyncSessionLocal() as grant_backfill_session:
            g_summary = await backfill_legacy_wallet_grants(grant_backfill_session)
            if g_summary.get("users_grandfathered", 0) > 0:
                await grant_backfill_session.commit()
                import logging

                logging.getLogger(__name__).info(
                    "Legacy wallet grant grandfather backfill applied: %s", g_summary
                )
    except Exception as e_grant_backfill:
        import logging

        logging.getLogger(__name__).warning(
            "Legacy wallet grant grandfather backfill skipped: %s", e_grant_backfill
        )


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            try:
                await session.close()
            except Exception:
                pass
