import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import event

from app.db.base import Base
from app.services.i18n import load_locales

# In local dev environments without libraqm (e.g. Windows), allow subtitle tests to proceed
try:
    from PIL import features
    import app.services.recap_engine.png_subtitles as _png_subs
    if not features.check("raqm"):
        _png_subs._RAQM_OK = True
except Exception:
    pass


@pytest_asyncio.fixture
async def db_session():
    """Create an isolated, in-memory SQLite database for testing."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        future=True,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    from app.config import settings
    from app.models.schema import DatabaseMetadata, utc_now

    async with session_maker() as session:
        session.add(DatabaseMetadata(key="db_identity", value=settings.expected_db_identity))
        session.add(DatabaseMetadata(key="schema_version", value="2.0.0"))
        session.add(DatabaseMetadata(key="created_at", value=utc_now().isoformat()))
        await session.commit()
        yield session

    await engine.dispose()


@pytest.fixture(autouse=True)
def setup_locales():
    load_locales()


@pytest.fixture(autouse=True)
def restore_settings():
    from app.config import settings
    orig_admin_ids = settings.admin_ids_raw
    orig_bot_token = settings.bot_token
    orig_welcome_bonus_seconds = settings.welcome_bonus_seconds
    orig_bot_mode = getattr(settings, "bot_mode", "polling")
    orig_webhook_secret = getattr(settings, "telegram_webhook_secret", "")
    orig_webhook_path = getattr(settings, "bot_webhook_path", "/telegram/webhook")
    orig_public_base_url = getattr(settings, "public_base_url", "")
    # Gate IDs too: several gate tests assign them directly on the
    # settings object, and without a restore the "configured" state
    # leaks into later test files (order-dependent failures once entry
    # points consult is_gate_configured()).
    orig_required_channel_id = settings.required_channel_id
    orig_required_group_id = settings.required_group_id
    orig_char_limit = settings.character_limit_free
    orig_purchased_limit = settings.character_limit_purchased
    orig_safety_limit = settings.infrastructure_safety_character_limit
    orig_free_daily_allowance = settings.free_daily_allowance_seconds
    orig_paid_gemini_taste = settings.paid_gemini_taste_minutes
    orig_free_gemini_taste = settings.free_gemini_taste_minutes
    orig_voice_daily_paid = settings.voice_daily_limit_minutes_paid
    orig_recap_on_queue = getattr(settings, "recap_on_queue", False)
    orig_prompt_profile = getattr(settings, "ai_prompt_profile", "v2")
    settings.required_channel_id = ""
    settings.required_group_id = ""
    settings.character_limit_free = 3000
    settings.character_limit_purchased = 15000
    settings.infrastructure_safety_character_limit = 15000
    settings.free_daily_allowance_seconds = 180
    settings.paid_gemini_taste_minutes = 10
    settings.free_gemini_taste_minutes = 2
    settings.voice_daily_limit_minutes_paid = 0
    settings.recap_on_queue = False
    settings.ai_prompt_profile = "v2"
    yield
    settings.admin_ids_raw = orig_admin_ids
    settings.bot_token = orig_bot_token
    settings.welcome_bonus_seconds = orig_welcome_bonus_seconds
    settings.required_channel_id = orig_required_channel_id
    settings.required_group_id = orig_required_group_id
    settings.character_limit_free = orig_char_limit
    settings.character_limit_purchased = orig_purchased_limit
    settings.infrastructure_safety_character_limit = orig_safety_limit
    settings.free_daily_allowance_seconds = orig_free_daily_allowance
    settings.paid_gemini_taste_minutes = orig_paid_gemini_taste
    settings.free_gemini_taste_minutes = orig_free_gemini_taste
    settings.voice_daily_limit_minutes_paid = orig_voice_daily_paid
    settings.recap_on_queue = orig_recap_on_queue
    settings.ai_prompt_profile = orig_prompt_profile
    settings.bot_mode = orig_bot_mode
    settings.telegram_webhook_secret = orig_webhook_secret
    settings.bot_webhook_path = orig_webhook_path
    settings.public_base_url = orig_public_base_url
    try:
        from app.webapp.telegram_webhook import attach_bot, recent_update_ids
        attach_bot(None, None)
        recent_update_ids.clear()
    except Exception:
        pass
