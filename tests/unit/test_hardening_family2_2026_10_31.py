"""Hardening batch — Family 2 (money integrity completion).

F5: recap_credits mutations are atomic conditional UPDATEs. The
    helpers (_charge_recap_credits / _refund_recap_credits), the recap
    submit deduction, the admin-adjust recap path and approve-slip
    crediting no longer read-modify-write. Sequential semantics are
    identical to before; the conditional WHERE clause is what closes
    the concurrent-submit race.
F6: voice submit pre-check — a job whose text cannot fit the balance
    even at a deliberately-fast 20 chars/sec speaking rate is rejected
    up front with the existing INSUFFICIENT_BALANCE mapping instead of
    burning a full GPU generation and failing at charge time.
Slip parity: the webapp slip resolves minutes/price DB-row-first,
    catalogue-fallback — exactly like the bot's create_payment_request.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.webapp.api as api_mod
from app.config import settings
from app.db.base import Base
from app.models.enums import AccountCategory
from app.models.schema import Job, PaymentRequest, PricingPlan, User, VoiceModel
from app.services.ledger import get_or_create_user
from app.services.payments import get_plan_by_code
from app.services.queue import create_voice_clone_job


# ── F5: atomic helpers against a real DB ──────────────────────────────

class _RealStore:
    """Points AsyncSessionLocal at a real file-backed SQLite DB (one
    short-lived engine per session — no cross-loop engine sharing)."""

    def __init__(self, db_file: str):
        self._db_file = db_file

    def make_session(self):
        engine = create_async_engine(f"sqlite+aiosqlite:///{self._db_file}", future=True)
        maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        return maker()

    def add_user(self, uid, credits):
        async def _seed():
            async with self.make_session() as s:
                s.add(User(id=uid, recap_credits=credits))
                await s.commit()

        asyncio.run(_seed())

    def credits(self, uid):
        async def _read():
            async with self.make_session() as s:
                user = await s.get(User, uid)
                return user.recap_credits if user else None

        return asyncio.run(_read())


@pytest.fixture
def real_store(monkeypatch, tmp_path):
    db_file = str(tmp_path / "f5.db")
    sync_engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    store = _RealStore(db_file)
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", store.make_session)
    monkeypatch.setattr(settings, "admin_ids_raw", "313131")
    return store


def test_f5_charge_is_all_or_nothing(real_store):
    real_store.add_user(7501, 2)
    assert asyncio.run(api_mod._charge_recap_credits(7501, 3)) is False
    assert real_store.credits(7501) == 2  # nothing partially deducted
    assert asyncio.run(api_mod._charge_recap_credits(7501, 2)) is True
    assert real_store.credits(7501) == 0
    # The exact race F5 closes, sequentially: a second charge against
    # the drained balance MUST fail instead of going negative / free.
    assert asyncio.run(api_mod._charge_recap_credits(7501, 1)) is False
    assert real_store.credits(7501) == 0


def test_f5_refund_adds_and_missing_user_fails(real_store):
    real_store.add_user(7502, 1)
    assert asyncio.run(api_mod._refund_recap_credits(7502, 3)) is True
    assert real_store.credits(7502) == 4
    assert asyncio.run(api_mod._refund_recap_credits(759999, 1)) is False
    assert asyncio.run(api_mod._charge_recap_credits(759999, 1)) is False


def test_f5_admin_semantics_unchanged(real_store):
    real_store.add_user(313131, 7)  # the pinned admin id
    assert asyncio.run(api_mod._charge_recap_credits(313131, 3)) is True
    assert real_store.credits(313131) == 7
    assert asyncio.run(api_mod._refund_recap_credits(313131, 3)) is False
    assert real_store.credits(313131) == 7


@pytest.mark.asyncio
async def test_f5_admin_adjust_recap_floor_and_add(db_session):
    admin = User(id=999111, username="adm", first_name="Admin", language_code="my")
    target = User(id=7503, username="tgt", first_name="Target", language_code="my", recap_credits=3)
    db_session.add_all([admin, target])
    await db_session.commit()

    # Negative adjust larger than the balance floors at 0 (established
    # semantics), applied atomically.
    res = await api_mod.adjust_user_status(
        api_mod.AdjustUserRequest(user_id=7503, add_recap_credits=-10, reason="clawback test"),
        admin, db_session,
    )
    assert res["success"] is True
    refreshed = await db_session.get(User, 7503)
    await db_session.refresh(refreshed)
    assert refreshed.recap_credits == 0

    res = await api_mod.adjust_user_status(
        api_mod.AdjustUserRequest(user_id=7503, add_recap_credits=5, reason="goodwill test"),
        admin, db_session,
    )
    assert res["success"] is True
    refreshed = await db_session.get(User, 7503)
    await db_session.refresh(refreshed)
    assert refreshed.recap_credits == 5


# ── F6: voice submit pre-check ────────────────────────────────────────

async def _purchased_user(db_session, uid):
    user, _ = await get_or_create_user(db_session, telegram_id=uid)
    user.account_category = AccountCategory.PURCHASED.value
    await db_session.commit()
    return user


async def _seed_voice_model(db_session):
    # create_voice_clone_job resolves the cloud model row; without it
    # the success path cannot complete (pre-existing behaviour).
    db_session.add(VoiceModel(
        name="VoxCPM Cloud", slug="voxcpm-cloud-ai", type="SYSTEM",
        owner_user_id=None, model_dir="cloud", pth_path="voxcpm",
        status="ACTIVE",
    ))
    await db_session.commit()


@pytest.mark.asyncio
async def test_f6_guaranteed_waste_job_rejected_upfront(db_session):
    # Purchased user, only the 180s monthly free allowance: 5000 chars
    # estimates to 250s even at the impossible 20 chars/sec — the job
    # was guaranteed to fail after generation. Rejected at submit.
    user = await _purchased_user(db_session, 7601)
    job, reason = await create_voice_clone_job(
        db_session, user_id=user.id, text="က" * 5000, preset_voice="komin"
    )
    assert job is None
    assert reason == "INSUFFICIENT_BALANCE"
    jobs = (await db_session.execute(select(Job))).scalars().all()
    assert jobs == []


@pytest.mark.asyncio
async def test_f6_fitting_job_still_enqueues(db_session):
    user = await _purchased_user(db_session, 7602)
    await _seed_voice_model(db_session)
    job, reason = await create_voice_clone_job(
        db_session, user_id=user.id, text="က" * 1000, preset_voice="komin"
    )
    assert job is not None
    assert reason == "QUEUED"


@pytest.mark.asyncio
async def test_f6_boundary_estimate_equal_to_balance_passes(db_session):
    # 3600 chars / 20 chars-per-sec == exactly the 180s allowance:
    # NOT greater, so the headroom rule lets it through (borderline
    # jobs are the post-generation charge's business, not the gate's).
    user = await _purchased_user(db_session, 7603)
    await _seed_voice_model(db_session)
    job, reason = await create_voice_clone_job(
        db_session, user_id=user.id, text="က" * 3600, preset_voice="komin"
    )
    assert job is not None, reason


# ── Slip parity: DB plan row wins over the catalogue ─────────────────

class _FakeUpload:
    def __init__(self, filename="slip.png", content=b"\x89PNG-fake-bytes"):
        self.filename = filename
        self._content = content
        self._sent = False

    async def read(self, size=-1):
        if self._sent:
            return b""
        self._sent = True
        return self._content


@pytest.mark.asyncio
async def test_slip_db_plan_price_wins_over_catalogue(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "bot_token", "")
    user, _ = await get_or_create_user(db_session, telegram_id=7701)
    await db_session.commit()

    cat = get_plan_by_code("MIN_10")
    assert cat["price_mmk"] == 8000
    # An admin price edit lives on the DB row: 8000 -> 9999.
    db_session.add(PricingPlan(
        code="MIN_10", minutes=10, price_mmk=9999, sort_order=0, is_active=True
    ))
    await db_session.commit()

    res = await api_mod.submit_payment_slip(
        package_code="MIN_10",
        amount_mmk=8000,
        slip_file=_FakeUpload(),
        user=user,
        db=db_session,
        _gate=user,
    )
    assert res["success"] is True
    req = (await db_session.execute(select(PaymentRequest))).scalar_one()
    # The webapp now agrees with the bot: the DB row's price/minutes.
    assert req.amount_mmk == 9999
    assert req.minutes == 10
