import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import event

from app.db.base import Base
from app.services.i18n import load_locales


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
    yield
    settings.admin_ids_raw = orig_admin_ids
    settings.bot_token = orig_bot_token
    settings.welcome_bonus_seconds = orig_welcome_bonus_seconds
