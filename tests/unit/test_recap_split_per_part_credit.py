"""Tests for per-part credit charging on segmented recaps.

Owner decision 2026-10-30 (supersedes the 7th release's "1 credit for
the whole segmented job"): a >10-minute recap that is split into parts
costs **1 credit PER PART**. The exact charge lands in the worker —
after segmentation, before any part renders — as one top-up over the
submit-time single credit. Submit shows an estimate (same target math
the segmenter uses) and pre-checks the balance. A job that cannot be
charged fails cleanly and the normal failure path refunds whatever was
charged, leaving the user exactly whole. Non-segmented recaps still
cost exactly 1 credit; admins are never charged.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

USER_ID = 424242
ADMIN_ID = 313131


class _LedgerHandle:
    """Real-SQLite stand-in for the credit helpers' session factory.

    Since hardening F5 the helpers mutate balances with conditional
    SQL UPDATEs, so a get()/commit()-only dict fake can no longer
    stand in for a session. This handle points AsyncSessionLocal at a
    real (file-backed) SQLite database — one short-lived engine per
    session, so the many asyncio.run() calls in these tests never
    share an engine across event loops — while every behavioural
    assertion in the tests below is unchanged.
    """

    def __init__(self, db_file: str):
        self._db_file = db_file

    def make_session(self):
        from sqlalchemy.ext.asyncio import (
            AsyncSession,
            async_sessionmaker,
            create_async_engine,
        )

        engine = create_async_engine(
            f"sqlite+aiosqlite:///{self._db_file}", future=True
        )
        maker = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )
        return maker()

    def add_user(self, uid=USER_ID, credits=0):
        from app.models.schema import User

        async def _seed():
            async with self.make_session() as s:
                s.add(User(id=uid, recap_credits=credits))
                await s.commit()

        asyncio.run(_seed())
        return uid

    def credits(self, uid=USER_ID) -> int:
        from app.models.schema import User

        async def _read():
            async with self.make_session() as s:
                user = await s.get(User, uid)
                return user.recap_credits if user else None

        return asyncio.run(_read())


@pytest.fixture()
def ledger(monkeypatch, tmp_path):
    """Patch the DB session factory the credit helpers open, and pin
    the admin id set so is_admin is deterministic."""
    from sqlalchemy import create_engine

    import app.models.schema  # noqa: F401 — register all models on Base
    from app.config import settings as settings_obj
    from app.db.base import Base

    db_file = str(tmp_path / "per_part_credits.db")
    sync_engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    handle = _LedgerHandle(db_file)
    monkeypatch.setattr(
        "app.db.session.AsyncSessionLocal", handle.make_session
    )
    monkeypatch.setattr(settings_obj, "admin_ids_raw", str(ADMIN_ID))
    return handle


def _add_user(store, uid=USER_ID, credits=0):
    return store.add_user(uid=uid, credits=credits)


# ── (a) Segmented job: exactly N credits, charged once ────────────────

def test_segmented_job_charges_exactly_n_credits_once(ledger):
    from app.webapp.api import _top_up_recap_segment_charge

    _add_user(ledger, credits=4)
    # Submit already took the normal single credit (balance 5 → 4) and
    # recorded it on the registry entry, exactly like create_recap_job.
    entry = {"credit_deducted": True, "credits_charged": 1}

    total = asyncio.run(_top_up_recap_segment_charge(USER_ID, entry, 3))
    assert total == 3
    assert entry["credits_charged"] == 3
    assert entry["parts_count"] == 3
    assert ledger.credits() == 2  # 5 − 3 in total, not a credit more

    # Idempotent: a second top-up for the same total charges nothing.
    assert asyncio.run(_top_up_recap_segment_charge(USER_ID, entry, 3)) == 3
    assert ledger.credits() == 2


# ── (b) Balance < N: clean failure before rendering, user left whole ──

def test_segmented_job_insufficient_balance_fails_clean_and_refunds(ledger):
    from app.webapp.api import (
        _RecapInsufficientCreditsError,
        _refund_recap_job_charge,
        _top_up_recap_segment_charge,
    )

    _add_user(ledger, credits=1)
    # Original balance was 2; submit took 1 → balance 1, but the job
    # needs 3 parts = 3 credits in total.
    entry = {"credit_deducted": True, "credits_charged": 1}

    with pytest.raises(_RecapInsufficientCreditsError) as excinfo:
        asyncio.run(_top_up_recap_segment_charge(USER_ID, entry, 3))
    assert excinfo.value.total_parts == 3
    assert "3" in str(excinfo.value)  # the message names the total
    # Nothing extra was charged by the failed top-up.
    assert ledger.credits() == 1
    assert entry["credits_charged"] == 1

    # The normal failure path refunds the job's charge → user is whole.
    assert asyncio.run(_refund_recap_job_charge(USER_ID, entry)) is True
    assert ledger.credits() == 2
    assert entry["credits_refunded"] == 1
    # A second failure handler for the same job is a no-op.
    assert asyncio.run(_refund_recap_job_charge(USER_ID, entry)) is False
    assert ledger.credits() == 2


def test_failed_segmented_job_refunds_the_full_n(ledger):
    from app.webapp.api import _refund_recap_job_charge

    _add_user(ledger, credits=2)
    # Fully charged segmented job (5 → 2), then a part render failed:
    # the refund is the job's FULL charge, like a normal failed recap.
    entry = {"credit_deducted": True, "credits_charged": 3, "parts_count": 3}
    assert asyncio.run(_refund_recap_job_charge(USER_ID, entry)) is True
    assert ledger.credits() == 5
    assert entry["credits_refunded"] == 3


# ── (c) Normal recap: still exactly 1 credit ──────────────────────────

def test_normal_job_refund_is_exactly_one_credit(ledger):
    from app.webapp.api import _refund_recap_job_charge

    _add_user(ledger, credits=7)
    # Legacy-shaped entry (no credits_charged key): the historical
    # default is the single submit credit.
    entry = {"credit_deducted": True}
    assert asyncio.run(_refund_recap_job_charge(USER_ID, entry)) is True
    assert ledger.credits() == 8
    assert entry["credits_refunded"] == 1


def test_charge_helper_is_all_or_nothing(ledger):
    from app.webapp.api import _charge_recap_credits

    _add_user(ledger, credits=2)
    assert asyncio.run(_charge_recap_credits(USER_ID, 3)) is False
    assert ledger.credits() == 2  # nothing partially deducted
    assert asyncio.run(_charge_recap_credits(USER_ID, 2)) is True
    assert ledger.credits() == 0


def test_admin_is_never_charged(ledger):
    """Admin recap behaviour is unchanged: the charge helper is a
    no-op success and the refund helper adds nothing — an admin job
    costs 0 credits today and still does."""
    from app.webapp.api import _charge_recap_credits, _refund_recap_credits

    _add_user(ledger, uid=ADMIN_ID, credits=7)
    assert asyncio.run(_charge_recap_credits(ADMIN_ID, 3)) is True
    assert ledger.credits(ADMIN_ID) == 7
    assert asyncio.run(_refund_recap_credits(ADMIN_ID, 3)) is False
    assert ledger.credits(ADMIN_ID) == 7


# ── Submit pre-check ──────────────────────────────────────────────────

def test_segment_balance_precheck():
    from app.webapp.api import _recap_segment_balance_ok

    assert _recap_segment_balance_ok(is_admin=False, balance=5, estimated_parts=5) is True
    assert _recap_segment_balance_ok(is_admin=False, balance=4, estimated_parts=5) is False
    assert _recap_segment_balance_ok(is_admin=False, balance=0, estimated_parts=2) is False
    # Admins are exempt from charging entirely.
    assert _recap_segment_balance_ok(is_admin=True, balance=0, estimated_parts=9) is True


# ── (d) Estimate matches the segmenter's target math ─────────────────

@pytest.mark.parametrize(
    "duration,split_minutes,cap,expected",
    [
        (754.0, 3, 600, 5),    # the owner's 12:34 audit case @3 min
        (754.0, 10, 600, 2),   # split size clamped to cap−30s = 570 s
        (720.0, 3, 600, 4),
        (1250.0, 5, 600, 5),
        (1150.0, 10, 600, 2),  # tiny tail joins the previous part
        (1850.0, 3, 600, 11),
    ],
)
def test_estimate_matches_segmenter_target_math(duration, split_minutes, cap, expected):
    from app.services.recap_engine.video_splitter import plan_parts
    from app.webapp.api import (
        _recap_estimate_part_count,
        _recap_segment_target_seconds,
    )

    target = _recap_segment_target_seconds(split_minutes, cap)
    planned = len(plan_parts(duration, target, smart=False, levels=None))
    assert planned == expected
    assert _recap_estimate_part_count(duration, split_minutes, cap) == planned


def test_segment_target_matches_worker_expression():
    from app.webapp.api import _recap_segment_target_seconds

    # The worker passes min(split_minutes*60, cap−30) to the segmenter;
    # the shared helper must be that exact expression.
    assert _recap_segment_target_seconds(3, 600) == 180.0
    assert _recap_segment_target_seconds(5, 600) == 300.0
    assert _recap_segment_target_seconds(10, 600) == 570.0


# ── Wiring contracts (submit / worker / status / Mini App) ───────────

def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_per_part_wiring_contracts():
    api_py = _read("app/webapp/api.py")
    # Submit: estimate + balance pre-check before deduction/queueing.
    assert "_recap_estimate_part_count(" in api_py
    assert "_recap_segment_balance_ok(" in api_py
    # Worker: exact charge after segmentation, via the shared target.
    assert "_top_up_recap_segment_charge(user_id, entry, total_parts)" in api_py
    assert "_recap_segment_target_seconds(" in api_py
    assert "split_source_into_segments" in api_py
    # Refunds route through the job-charge helper (full amount).
    assert api_py.count("_refund_recap_job_charge(user_id, entry)") == 2
    # Status + history expose parts count and credits used.
    assert '"credits_charged"' in api_py
    assert '"parts_count"' in api_py


def test_miniapp_split_cost_hint_contracts():
    app_js = _read("app/webapp/static/js/app.js")
    html = _read("app/webapp/static/index.html")
    i18n_js = _read("app/webapp/static/js/i18n.js")
    # Estimate + hint logic in the client, mirroring the server math.
    assert "function estimateRecapSegmentParts(" in app_js
    assert "function updateRecapSplitCostHint()" in app_js
    assert 'tf("recap_split_cost_estimate"' in app_js
    assert 'id="recap-split-cost-hint"' in html
    # Result + history show parts count and credits used.
    assert 'tf("recap_segmented_cost"' in app_js
    assert "data.parts_count" in app_js
    # i18n keys exist in BOTH language dicts.
    for key in (
        "recap_split_cost_estimate",
        "recap_split_cost_low_balance",
        "recap_segmented_cost",
    ):
        assert i18n_js.count(f"{key}:") == 2, key
        assert "{parts}" in i18n_js and "{credits}" in i18n_js
