"""Hardening batch — tail items (hunt #4 B3/B4, hunt #3 A5).

B3  pricing bounds live in the SERVICE now: every caller (webapp,
    bot typed input, bot preset callbacks) is backstopped by the
    same 100 – 10,000,000 MMK range.
B4  AdjustUserRequest carries sanity ceilings, so a huge adjust
    value is a clean 422 instead of an Integer-overflow 500.
A5  gemini_recap resolves its primary model through the same
    precedence as the recap planner (dynamic setting →
    GEMINI_PRIMARY_MODEL → GEMINI_TEXT_MODEL → legacy GEMINI_MODEL)
    instead of reading only the never-set GEMINI_MODEL.
"""
from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

import app.webapp.api as api_mod
from app.config import settings
from app.models.schema import PricingPlan
from app.services.gemini_recap import _resolve_primary_model
from app.services.payments import get_active_pricing_plans, update_pricing_plan_price


# ── B3 ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_b3_service_rejects_out_of_range_prices(db_session):
    await get_active_pricing_plans(db_session)  # seeds MIN_10 @ 8000
    for bad in (0, 99, -5, 10_000_001, 10**12):
        ok, plan, msg = await update_pricing_plan_price(db_session, "MIN_10", bad, admin_id=1)
        assert ok is False and plan is None and msg == "PRICE_OUT_OF_RANGE", bad
    row = (await db_session.execute(
        PricingPlan.__table__.select().where(PricingPlan.code == "MIN_10")
    )).fetchone()
    assert row.price_mmk == 8000  # untouched by every rejected write


@pytest.mark.asyncio
async def test_b3_service_accepts_boundary_prices(db_session):
    await get_active_pricing_plans(db_session)
    for good in (100, 10_000_000):
        ok, plan, msg = await update_pricing_plan_price(db_session, "MIN_10", good, admin_id=1)
        assert ok is True and plan.price_mmk == good


# ── B4 ────────────────────────────────────────────────────────────────

def test_b4_adjust_request_ceilings():
    ok = api_mod.AdjustUserRequest(
        user_id=1, add_minutes=100_000, add_seconds=-6_000_000, add_recap_credits=100_000
    )
    assert ok.add_minutes == 100_000
    for kwargs in (
        {"add_minutes": 100_001},
        {"add_minutes": -(10**15)},
        {"add_seconds": 6_000_001},
        {"add_recap_credits": 10**12},
    ):
        with pytest.raises(ValidationError):
            api_mod.AdjustUserRequest(user_id=1, **kwargs)


# ── A5 ────────────────────────────────────────────────────────────────

def test_a5_resolution_precedence(monkeypatch):
    for var in ("GEMINI_PRIMARY_MODEL", "GEMINI_TEXT_MODEL", "GEMINI_MODEL"):
        monkeypatch.delenv(var, raising=False)
    original = settings.gemini_primary_model
    try:
        # Dynamic setting wins.
        monkeypatch.setattr(settings, "gemini_primary_model", "gemini-3.6-flash")
        monkeypatch.setenv("GEMINI_MODEL", "gemini-9-legacy")
        assert _resolve_primary_model() == "gemini-3.6-flash"

        # Then the planner's env names, in planner order.
        monkeypatch.setattr(settings, "gemini_primary_model", "")
        monkeypatch.setenv("GEMINI_PRIMARY_MODEL", "gemini-primary-env")
        assert _resolve_primary_model() == "gemini-primary-env"
        monkeypatch.delenv("GEMINI_PRIMARY_MODEL")
        monkeypatch.setenv("GEMINI_TEXT_MODEL", "gemini-text-env")
        assert _resolve_primary_model() == "gemini-text-env"
        monkeypatch.delenv("GEMINI_TEXT_MODEL")

        # Legacy name still honoured last; retired generations rejected.
        assert _resolve_primary_model() == "gemini-9-legacy"
        monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
        assert _resolve_primary_model() == ""
    finally:
        settings.gemini_primary_model = original
