"""Batch DC — a re-charged recap job must stay refundable.

Production evidence (2026-11-09, paid user 9117134716, DB-verified):
four recap jobs failed (Batch DB incident) and were charged 9
credits and refunded 9 — correct. An admin queue retry then
re-charged two of them (3 + 2 credits, "Admin queue retry
re-charge (Batch AJ H6)"); both retries failed again, but NO
refund landed: ``_refund_recap_credits`` refused whenever ANY
REFUND row existed for the job, so the first refund permanently
blocked refunds of later re-charges. Balance 6 → 1; 5 credits
restored by a manual correction (CORRECTION:retry-recharge-2026-11-09).

The fix: the guard is the OUTSTANDING amount (ledger DEBIT total
minus refund-family total), the refund is capped at it, and later
refunds write ``REFUND:<job_id>#<n>`` rows because the partial
unique index ``uq_recap_ledger_refund_ref`` allows only one CREDIT
row per exact reference. Jobs with no ledger DEBIT rows (legacy)
keep the old exists-check.
"""
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import session as session_mod
from app.db.base import Base
from app.models.schema import RecapCreditLedgerEvent, User
from app.services.ledger import (
    get_or_create_user,
    recap_debit_total_for_job,
    recap_refund_total_for_job,
    record_recap_credit_event,
)
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
API_SRC = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")


async def _open_store(tmp_path, monkeypatch, name="batch_dc.db"):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/{name}", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", maker)
    return engine, maker


async def _make_user(maker, telegram_id=777001, recap_credits=0):
    async with maker() as session:
        user, _created = await get_or_create_user(session, telegram_id)
        user.recap_credits = recap_credits
        await session.commit()
        return user.id


async def _balance(maker, user_id):
    async with maker() as session:
        user = await session.get(User, user_id)
        return int(user.recap_credits or 0)


async def _charge(maker, user_id, job_id, credits, note="Recap job submit charge"):
    """Mirror a real charge: balance down + DEBIT ledger row, one commit."""
    async with maker() as session:
        user = await session.get(User, user_id)
        user.recap_credits = int(user.recap_credits or 0) - credits
        await record_recap_credit_event(
            session, user_id=user_id, direction="DEBIT", credits=credits,
            reference_id=job_id, note=note,
        )
        await session.commit()


async def _credit_rows(maker, user_id, job_id):
    async with maker() as session:
        rows = (await session.execute(
            select(RecapCreditLedgerEvent).where(
                RecapCreditLedgerEvent.user_id == user_id,
                RecapCreditLedgerEvent.direction == "CREDIT",
            )
        )).scalars().all()
        return sorted(
            (int(r.credits), r.reference_id) for r in rows
            if r.reference_id and job_id in r.reference_id
        )


# ── DC-1: the incident itself — retry re-charge must refund ─────────

@pytest.mark.asyncio
async def test_dc_retry_recharge_is_refundable(tmp_path, monkeypatch):
    _engine, maker = await _open_store(tmp_path, monkeypatch)
    uid = await _make_user(maker, recap_credits=6)
    job = "rc_dc_retry"
    await _charge(maker, uid, job, 2)
    assert await _balance(maker, uid) == 4
    assert await api_mod._refund_recap_credits(uid, 2, job_id=job) is True
    assert await _balance(maker, uid) == 6
    # Admin queue retry re-charges the fully-refunded job (AJ H6).
    await _charge(maker, uid, job, 2, note="Admin queue retry re-charge (Batch AJ H6)")
    assert await _balance(maker, uid) == 4
    # The retry fails too — under the old exists-guard this refund
    # was refused and the 2 credits were lost.
    assert await api_mod._refund_recap_credits(uid, 2, job_id=job) is True
    assert await _balance(maker, uid) == 6
    # Nothing left outstanding: a third attempt is a true double refund.
    assert await api_mod._refund_recap_credits(uid, 2, job_id=job) is False
    assert await _balance(maker, uid) == 6
    async with maker() as session:
        assert await recap_debit_total_for_job(session, job) == 4
        assert await recap_refund_total_for_job(session, job) == 4
    # Two refund rows, distinct references (unique-index safe).
    assert await _credit_rows(maker, uid, job) == [
        (2, "REFUND:rc_dc_retry"),
        (2, "REFUND:rc_dc_retry#2"),
    ]


# ── DC-2: refund is capped at the outstanding amount ────────────────

@pytest.mark.asyncio
async def test_dc_refund_capped_at_outstanding(tmp_path, monkeypatch):
    _engine, maker = await _open_store(tmp_path, monkeypatch)
    uid = await _make_user(maker, recap_credits=6)
    job = "rc_dc_cap"
    await _charge(maker, uid, job, 3)
    assert await api_mod._refund_recap_credits(uid, 3, job_id=job) is True
    await _charge(maker, uid, job, 1, note="Admin queue retry re-charge (Batch AJ H6)")
    assert await _balance(maker, uid) == 5
    # Caller asks for the job's full historical charge (3) — only the
    # outstanding 1 may come back.
    assert await api_mod._refund_recap_credits(uid, 3, job_id=job) is True
    assert await _balance(maker, uid) == 6
    async with maker() as session:
        assert await recap_refund_total_for_job(session, job) == 4


# ── DC-3: a plain double refund stays blocked ───────────────────────

@pytest.mark.asyncio
async def test_dc_plain_double_refund_still_blocked(tmp_path, monkeypatch):
    _engine, maker = await _open_store(tmp_path, monkeypatch)
    uid = await _make_user(maker, recap_credits=6)
    job = "rc_dc_plain"
    await _charge(maker, uid, job, 2)
    assert await api_mod._refund_recap_credits(uid, 2, job_id=job) is True
    assert await api_mod._refund_recap_credits(uid, 2, job_id=job) is False
    assert await _balance(maker, uid) == 6
    assert await _credit_rows(maker, uid, job) == [(2, "REFUND:rc_dc_plain")]


# ── DC-4: legacy jobs (no ledger DEBIT rows) keep the old path ──────

@pytest.mark.asyncio
async def test_dc_legacy_job_without_debit_rows(tmp_path, monkeypatch):
    _engine, maker = await _open_store(tmp_path, monkeypatch)
    uid = await _make_user(maker, recap_credits=4)
    job = "rc_dc_legacy"
    # Charge predates the ledger: balance was taken, no DEBIT row.
    assert await api_mod._refund_recap_credits(uid, 2, job_id=job) is True
    assert await _balance(maker, uid) == 6
    assert await api_mod._refund_recap_credits(uid, 2, job_id=job) is False
    assert await _balance(maker, uid) == 6


# ── DC-5: source pin — the guard is outstanding-based ───────────────

def test_dc_refund_guard_is_outstanding_based():
    start = API_SRC.index("async def _refund_recap_credits")
    end = API_SRC.index("async def _charge_recap_credits(", start)
    block = API_SRC[start:end]
    assert "recap_refund_family" in block
    assert "outstanding" in block
    assert "if job_id and await recap_refund_event_exists" not in block
