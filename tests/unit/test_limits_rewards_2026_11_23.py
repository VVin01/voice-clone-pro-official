"""Part I — Limits & Rewards Update (2026-11-23) unit test suite.

Pins:
  (a) Daily free voice allowance (180s/UTC day, non-accumulating, idempotent within a day,
      expires at UTC day rollover, monthly grant retired, existing monthly_free_seconds untouched,
      admins exempt, purchased users unaffected, ledger note matches spec).
  (b) Paid Gemini pool taste (10 min/day = 600s delivered, steps to own key or Edge once spent,
      own key never limited, free taste unchanged at 2 min/day + 100 req budget, admin editable
      with (0, 1440) bounds).
  (c) Referral reward = 60s into Voice Clone bucket (referral_voice_seconds -> clone_spendable),
      uncapped (21st+ referrals still rewarded), already-earned past TTS referral_seconds untouched.
  (d) Character limit defaults (free=3000, purchased=15000, safety=15000), VoiceGenerateRequest
      accepts 15,000 chars, and MY/EN user-facing copy strings across Mini App & bot locales.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config.settings import Settings, settings
from app.models.enums import AccountCategory, JobType, LedgerDirection, LedgerSource
from app.models.schema import (
    DailyFreeAllowanceGrant,
    MinuteBalance,
    MinuteLedgerEvent,
    ReferralReward,
)
from app.services import gemini_key_service as gks
from app.services import ledger as ledger_mod
from app.services.billing import (
    BILLING_MODE_VOICE_ONLY,
    reserve_for_job,
    settle_job_reservation,
)
from app.services.ledger import (
    MAX_REFERRALS_REWARDED,
    REFERRAL_REWARD_SECONDS,
    confirm_referral_and_reward,
    consume_seconds,
    credit_purchased_minutes,
    get_minute_balance,
    get_or_create_user,
    get_referral_stats,
    grant_daily_free_allowance,
    grant_monthly_free_allowance,
)
from app.services.tts_service import tts_character_limit
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
MY_LOCALE = json.loads((ROOT / "locales/my/messages.json").read_text(encoding="utf-8"))
EN_LOCALE = json.loads((ROOT / "locales/en/messages.json").read_text(encoding="utf-8"))


# ── (a) Daily Free Voice Allowance (Non-Accumulating) ─────────────────────


@pytest.mark.asyncio
async def test_daily_allowance_grants_once_per_day_and_does_not_accumulate(db_session, monkeypatch):
    user, _ = await get_or_create_user(db_session, telegram_id=910001)
    monkeypatch.setattr(ledger_mod, "get_current_daily_period", lambda: "2026-11-23")

    granted1, bal1 = await grant_daily_free_allowance(db_session, user.id)
    assert granted1 is True
    assert bal1.daily_free_seconds == 180
    assert bal1.effective_daily_free_seconds == 180
    assert bal1.clone_spendable_seconds == 180
    assert bal1.last_daily_grant_date == "2026-11-23"

    # Idempotent within the same UTC day
    granted2, bal2 = await grant_daily_free_allowance(db_session, user.id)
    assert granted2 is False
    assert bal2.daily_free_seconds == 180

    # Ledger note matches spec verbatim
    events = (
        await db_session.execute(
            select(MinuteLedgerEvent).where(
                MinuteLedgerEvent.user_id == user.id,
                MinuteLedgerEvent.source == LedgerSource.DAILY_FREE.value,
                MinuteLedgerEvent.direction == LedgerDirection.CREDIT.value,
            )
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].note == "Daily Free Allowance 2026-11-23 (3 minutes)"

    # Consume 70s on Day 1 -> 110s remaining on Day 1
    bal_after = await consume_seconds(db_session, user.id, seconds_to_consume=70, job_id="lr-day1-job")
    assert bal_after.daily_free_seconds == 110
    assert bal_after.effective_daily_free_seconds == 110

    # Roll UTC clock to Day 2: leftover 110s from Day 1 MUST expire immediately
    monkeypatch.setattr(ledger_mod, "get_current_daily_period", lambda: "2026-11-24")
    assert bal_after.effective_daily_free_seconds == 0
    assert bal_after.clone_spendable_seconds == 0
    with pytest.raises(ValueError, match="Insufficient balance"):
        await consume_seconds(db_session, user.id, seconds_to_consume=10, job_id="lr-day2-pregrant")

    # Day 2 grant resets to exactly 180s (NEVER 110 + 180 = 290s or 360s)
    granted_d2, bal_d2 = await grant_daily_free_allowance(db_session, user.id)
    assert granted_d2 is True
    assert bal_d2.daily_free_seconds == 180
    assert bal_d2.effective_daily_free_seconds == 180
    assert bal_d2.clone_spendable_seconds == 180
    assert bal_d2.last_daily_grant_date == "2026-11-24"

    rows = (
        await db_session.execute(
            select(DailyFreeAllowanceGrant).where(DailyFreeAllowanceGrant.user_id == user.id)
        )
    ).scalars().all()
    assert sorted(r.day for r in rows) == ["2026-11-23", "2026-11-24"]


@pytest.mark.asyncio
async def test_monthly_grant_retired_and_existing_monthly_balance_untouched(db_session, monkeypatch):
    user, _ = await get_or_create_user(db_session, telegram_id=910002)
    bal = await get_minute_balance(db_session, user.id)
    bal.monthly_free_seconds = 120  # pre-existing already-granted monthly seconds
    await db_session.flush()

    # Retired monthly grant does NOT grant or modify wallet
    m_granted, bal_m = await grant_monthly_free_allowance(db_session, user.id)
    assert m_granted is False
    assert bal_m.monthly_free_seconds == 120

    # Daily grant adds today's 180s without wiping or modifying existing monthly_free_seconds
    monkeypatch.setattr(ledger_mod, "get_current_daily_period", lambda: "2026-11-23")
    d_granted, bal_d = await grant_daily_free_allowance(db_session, user.id)
    assert d_granted is True
    assert bal_d.monthly_free_seconds == 120  # untouched!
    assert bal_d.effective_daily_free_seconds == 180
    assert bal_d.clone_spendable_seconds == 300  # 180 daily + 120 existing monthly

    # Daily allowance is drawn BEFORE legacy monthly/wallet balance
    res = await reserve_for_job(
        db_session,
        user.id,
        "lr-draw-order-1",
        job_type=JobType.VOICE_CLONE.value,
        billing_mode=BILLING_MODE_VOICE_ONLY,
        reserve_voice_seconds=100,
    )
    assert res.reserved_voice_daily == 100
    assert res.reserved_voice_monthly == 0
    await settle_job_reservation(db_session, user.id, "lr-draw-order-1", actual_voice_seconds=100)
    bal_after = await get_minute_balance(db_session, user.id)
    assert bal_after.daily_free_seconds == 80
    assert bal_after.monthly_free_seconds == 120


@pytest.mark.asyncio
async def test_daily_allowance_exempts_admins_and_skips_purchased_users(db_session, monkeypatch):
    monkeypatch.setattr(ledger_mod, "get_current_daily_period", lambda: "2026-11-23")
    settings.admin_ids_raw = "910003"
    admin_u, _ = await get_or_create_user(db_session, telegram_id=910003)
    g_adm, b_adm = await grant_daily_free_allowance(db_session, admin_u.id)
    assert g_adm is False
    assert b_adm.daily_free_seconds == 0

    vip_u, _ = await get_or_create_user(db_session, telegram_id=910004)
    await credit_purchased_minutes(db_session, vip_u.id, minutes=10, payment_id="PAY-LR-VIP")
    assert vip_u.account_category == AccountCategory.PURCHASED.value
    g_vip, b_vip = await grant_daily_free_allowance(db_session, vip_u.id)
    assert g_vip is False
    assert b_vip.daily_free_seconds == 0
    assert b_vip.purchased_seconds == 600


# ── (b) Paid Gemini Pool Taste: 10 min/day ────────────────────────────────


@pytest.mark.asyncio
async def test_paid_gemini_pool_taste_600s_and_own_key_exempt(db_session, monkeypatch):
    monkeypatch.setattr(gks, "_utc_day", lambda: date(2026, 11, 23))

    paid_u, _ = await get_or_create_user(db_session, telegram_id=910010)
    paid_u.account_category = AccountCategory.PURCHASED.value
    await db_session.flush()

    # Initial paid route: 600s limit, pool allowed
    ctx0 = await gks.resolve_gemini_route(db_session, paid_u, is_admin=False)
    assert ctx0.taste_limit_seconds == 600.0
    assert ctx0.taste_remaining_seconds == 600.0
    assert ctx0.pool_allowed is True
    assert ctx0.taste_blocked is False

    # Debit 599s -> still allowed (1s remaining)
    await gks.debit_pool_usage(
        db_session, paid_u.id, tier=AccountCategory.PURCHASED.value, seconds=599.0, requests=5
    )
    ctx1 = await gks.resolve_gemini_route(db_session, paid_u, is_admin=False)
    assert ctx1.pool_allowed is True
    assert ctx1.taste_blocked is False
    assert ctx1.taste_remaining_seconds == pytest.approx(1.0)

    # Purchased pool requests MUST NOT pollute the global FREE daily budget
    assert await gks.free_budget_used_requests(db_session) == 0

    # Debit 1 more second -> hits 600s cap -> pool closed & taste_blocked=True
    await gks.debit_pool_usage(
        db_session, paid_u.id, tier=AccountCategory.PURCHASED.value, seconds=1.0, requests=1
    )
    ctx2 = await gks.resolve_gemini_route(db_session, paid_u, is_admin=False)
    assert ctx2.pool_allowed is False
    assert ctx2.taste_blocked is True
    assert ctx2.taste_remaining_seconds == 0.0

    # Adding an own key bypasses taste_blocked (own key is never limited!)
    paid_u.gemini_api_key = "AIzaSyTestUserOwnKey1234567890"
    paid_u.gemini_key_state = gks.KEY_STATE_OK
    await db_session.flush()
    ctx3 = await gks.resolve_gemini_route(db_session, paid_u, is_admin=False)
    assert ctx3.has_usable_own_key is True
    assert ctx3.taste_blocked is False

    # Free user taste remains 120s (2 min/day)
    free_u, _ = await get_or_create_user(db_session, telegram_id=910011)
    ctx_free = await gks.resolve_gemini_route(db_session, free_u, is_admin=False)
    assert ctx_free.taste_limit_seconds == 120.0
    assert ctx_free.pool_allowed is True

    # Admin remains exempt
    settings.admin_ids_raw = "910012"
    admin_u, _ = await get_or_create_user(db_session, telegram_id=910012)
    ctx_adm = await gks.resolve_gemini_route(db_session, admin_u, is_admin=True)
    assert ctx_adm.pool_allowed is True
    assert ctx_adm.taste_blocked is False
    assert ctx_adm.taste_remaining_seconds is None


def test_paid_gemini_taste_admin_settings_validation():
    cleaned = api_mod._validate_admin_settings_updates(
        {"paid_gemini_taste_minutes": 10, "free_daily_allowance_seconds": 180}
    )
    assert cleaned["paid_gemini_taste_minutes"] == 10
    assert cleaned["free_daily_allowance_seconds"] == 180

    with pytest.raises(HTTPException) as exc_lo:
        api_mod._validate_admin_settings_updates({"paid_gemini_taste_minutes": -1})
    assert exc_lo.value.status_code == 400

    with pytest.raises(HTTPException) as exc_hi:
        api_mod._validate_admin_settings_updates({"paid_gemini_taste_minutes": 1441})
    assert exc_hi.value.status_code == 400


# ── (c) Referral Reward: 60s Voice Clone, Uncapped ────────────────────────


@pytest.mark.asyncio
async def test_referral_reward_60s_voice_bucket_uncapped_past_tts_untouched(db_session):
    assert REFERRAL_REWARD_SECONDS == 60
    assert MAX_REFERRALS_REWARDED is None

    referrer, _ = await get_or_create_user(db_session, telegram_id=910020)
    bal = await get_minute_balance(db_session, referrer.id)
    # Pre-existing already-earned past TTS referral seconds (must NOT be converted or clawed back)
    bal.referral_seconds = 240
    await db_session.flush()

    # Invite 25 friends (> old 20-friend ceiling)
    for i in range(1, 26):
        uid = 920000 + i
        await get_or_create_user(db_session, telegram_id=uid, referral_code_arg=referrer.referral_code)
        was_pending, rewarded, idx = await confirm_referral_and_reward(db_session, uid)
        assert was_pending is True
        assert rewarded is True
        assert idx == i

    ref_bal = await get_minute_balance(db_session, referrer.id)
    # 25 * 60s = 1500s in Voice Clone bucket
    assert ref_bal.referral_voice_seconds == 1500
    assert ref_bal.clone_spendable_seconds == 1500
    # Past TTS referral balance untouched & NOT folded into clone_spendable_seconds
    assert ref_bal.referral_seconds == 240
    assert ref_bal.total_usable_seconds == 1740

    # Verify Voice Clone consumption actually draws referral_voice_seconds
    after_consume = await consume_seconds(db_session, referrer.id, seconds_to_consume=90, job_id="lr-ref-clone-1")
    assert after_consume.referral_voice_seconds == 1410
    assert after_consume.referral_seconds == 240  # TTS bucket still untouched!

    # Stats report uncapped semantics
    stats = await get_referral_stats(db_session, referrer.id)
    assert stats["rewarded_referrals"] == 25
    assert stats["earned_seconds"] == 1500
    assert stats["max_referrals"] is None
    assert stats["unlimited"] is True

    # Verify DB rows include reward_index 21..25
    db_indices = sorted(
        r.reward_index
        for r in (
            await db_session.execute(
                select(ReferralReward).where(ReferralReward.referrer_id == referrer.id)
            )
        ).scalars().all()
    )
    assert db_indices == list(range(1, 26))


# ── (d) Character Limit Defaults & Surviving Copy Strings (MY + EN) ───────


def test_character_limit_defaults_and_request_schema():
    # Verify Pydantic class defaults directly (independent of local .env)
    assert Settings.model_fields["free_daily_allowance_seconds"].default == 180
    assert Settings.model_fields["paid_gemini_taste_minutes"].default == 10
    assert Settings.model_fields["character_limit_free"].default == 3000
    assert Settings.model_fields["character_limit_purchased"].default == 15000
    assert Settings.model_fields["infrastructure_safety_character_limit"].default == 15000

    # VoiceGenerateRequest must accept 15,000-character VIP text
    req = api_mod.VoiceGenerateRequest(text="က" * 15000)
    assert len(req.text) == 15000

    # tts_character_limit uses 3000 (free) / 15000 (purchased)
    class _DummyUser:
        def __init__(self, cat):
            self.account_category = cat

    assert tts_character_limit(_DummyUser(AccountCategory.FREE), is_admin=False) == 3000
    assert tts_character_limit(_DummyUser(AccountCategory.PURCHASED), is_admin=False) == 15000


def test_part1_copy_strings_my_and_en():
    # Daily allowance copy (not monthly)
    assert 'home_free_voice_period: "180 စက္ကန့် / နေ့စဉ်"' in I18N_JS
    assert 'home_free_voice_period: "180s / Daily"' in I18N_JS
    assert "180 စက္ကန့် / လစဉ်" not in I18N_JS
    assert "180s / Monthly" not in I18N_JS
    assert "180 စက္ကန့် / နေ့စဉ်" in INDEX_HTML

    # Character limits in TTS hint & excess CTA (MY + EN)
    assert "3,000 (VIP 15,000)" in I18N_JS
    assert "3,000 characters per generation (VIP 15,000)" in I18N_JS
    assert "VIP 10 မိနစ်/နေ့" in I18N_JS
    assert "VIP 10 min/day" in I18N_JS
    assert "VIP (15,000 လုံး)" in I18N_JS
    assert "Upgrade to VIP (15,000 chars)" in I18N_JS

    # Referral terms in Mini App + Bot locales (MY + EN)
    assert "သူငယ်ချင်း ၁ ယောက် ဖိတ်တိုင်း Voice Clone ၁ မိနစ် · အကန့်အသတ် မရှိ" in I18N_JS
    assert "1 min of Voice Clone per friend invited · Unlimited" in I18N_JS
    assert "Voice Clone ၁ မိနစ်" in MY_LOCALE["referral_desc"]
    assert "အကန့်အသတ် မရှိ" in MY_LOCALE["referral_desc"]
    assert "TTS အသံ ၂ မိနစ်" not in MY_LOCALE["referral_desc"]
    assert "Voice Clone time" in EN_LOCALE["referral_desc"]
    assert "Unlimited" in EN_LOCALE["referral_desc"]
    assert "2 minutes of TTS" not in EN_LOCALE["referral_desc"]
    assert "15,000" in MY_LOCALE["text_too_long_vip_note"]
    assert "15,000" in EN_LOCALE["text_too_long_vip_note"]
