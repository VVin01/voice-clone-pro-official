"""Unit tests for Part II — Pricing v3 + Buy Tab Rebuild + Voice Daily Cap Abolition (2026-11-23).

Pins:
1. Exact 10-plan v3 catalogue (codes, names, categories, grant minutes, display_minutes,
   recap_credits, price_mmk, validity_days, badge, is_topup=False, sort order).
2. Seed migration: 10 new v3 codes inserted once, legacy rows (including retired v2 codes)
   deactivated idempotently without deleting them, admin edits to v3 rows survive re-seed.
3. Legacy-code pending PaymentRequest still approves and grants its stamped amounts.
4. Validity-0 end-to-end:
   - pkg_business (validity_days=0) -> PaymentRequest.validity_days=0 -> WalletGrant.expires_at is None
   - pkg_starter (validity_days=30) -> PaymentRequest.validity_days=30 -> WalletGrant.expires_at == +30d
   - Mini App POST /api/vip/submit-slip stamps validity_days=0 for pkg_business & 90 for pkg_pro
   - update_pricing_plan_config preserves 0 -> 30 transition accurately
5. _format_plan_presentation output (validity_label MY/EN, bonus_200 badge MY/EN,
   voice_1000 display_minutes=1000 while grant minutes=1200, no hardcoded 10% savings).
6. Voice daily cap abolished: Settings().voice_daily_limit_minutes_paid == 0 and a PURCHASED
   user with heavy same-day voice usage is never rejected by the daily-minutes cap.
7. Buy Tab v8 & Profile Wallet move structure (3-button selector, no topup/subnav/explainer,
   collapsed Terms card with 7 items, Wallet & Orders lists inside #tab-profile, no 0-eating JS).
"""

from __future__ import annotations

import io
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.config.settings import Settings
from app.models.enums import AccountCategory, JobStatus, JobType, PaymentStatus
from app.models.schema import (
    Base,
    Job,
    MinuteBalance,
    PaymentRequest,
    PricingPlan,
    User,
    VoiceModel,
    WalletGrant,
    utc_now,
)
from app.services import payments as pay_svc
from app.services.queue import create_voice_clone_job
from app.webapp.api import _format_plan_presentation


ROOT = Path(__file__).resolve().parents[2]


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def _make_png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color=(12, 98, 210)).save(buf, format="PNG")
    return buf.getvalue()


# ── 1. Exact 10-plan v3 catalogue ─────────────────────────────────────────────


def test_pricing_v3_catalogue_exact_10_plans():
    expected = [
        ("pkg_starter", "Starter", "combo", 100, 15, 14900, 30, "", 100),
        ("pkg_creator", "Creator", "combo", 200, 30, 28500, 30, "", 200),
        ("pkg_pro", "Pro", "combo", 500, 60, 54900, 90, "", 500),
        ("pkg_business", "Business", "combo", 1000, 180, 114900, 0, "", 1000),
        ("voice_200", "200", "voice", 200, 0, 19800, 0, "", 200),
        ("voice_500", "500", "voice", 500, 0, 39500, 0, "", 500),
        ("voice_1000", "1000", "voice", 1200, 0, 79200, 0, "bonus_200", 1000),
        ("recap_30", "30", "recap", 0, 30, 13500, 0, "", 0),
        ("recap_60", "60", "recap", 0, 60, 25500, 0, "", 0),
        ("recap_180", "180", "recap", 0, 180, 68400, 0, "", 0),
    ]
    assert len(pay_svc.PLAN_CATALOGUE) == 10
    actual = [
        (
            p["code"],
            p["name"],
            p["category"],
            p["minutes"],
            p["recap_credits"],
            p["price_mmk"],
            p["validity_days"],
            p["badge"],
            p.get("display_minutes", p["minutes"]),
        )
        for p in pay_svc.PLAN_CATALOGUE
    ]
    assert actual == expected
    assert all(p["is_topup"] is False for p in pay_svc.PLAN_CATALOGUE)
    # All 22 v2 codes must live in LEGACY_PLAN_CATALOGUE (plus 7 pre-v2 = 29 total)
    v2_codes = {
        "voice_mini", "voice_starter", "voice_creator", "voice_pro", "voice_business",
        "recap_mini", "recap_starter", "recap_creator", "recap_pro", "recap_business",
        "combo_mini", "combo_starter", "combo_creator", "combo_pro", "combo_business",
        "topup_voice_10", "topup_voice_30", "topup_voice_60",
        "topup_recap_5", "topup_recap_15", "topup_recap_30", "topup_recap_60",
    }
    assert v2_codes.issubset(set(pay_svc.LEGACY_PLAN_CATALOGUE.keys()))
    assert not v2_codes.intersection({p["code"] for p in pay_svc.PLAN_CATALOGUE})


# ── 2. Seed migration (fill-only + legacy deactivation + admin edit survives) ─


@pytest.mark.asyncio
async def test_seed_migration_deactivates_legacy_and_preserves_admin_edits(db_session: AsyncSession):
    # Pre-populate active v2 legacy rows as an existing production DB would have.
    legacy_v2_row = PricingPlan(
        code="combo_creator",
        name="Creator Combo",
        category="combo",
        sub_type="combo",
        minutes=45,
        recap_credits=70,
        price_mmk=27000,
        validity_days=30,
        is_topup=False,
        badge="best_value",
        sort_order=13,
        is_active=True,
    )
    legacy_topup_row = PricingPlan(
        code="topup_voice_10",
        name="Voice Top-up 10m",
        category="topup",
        sub_type="voice",
        minutes=10,
        recap_credits=0,
        price_mmk=2500,
        validity_days=30,
        is_topup=True,
        badge="",
        sort_order=16,
        is_active=True,
    )
    db_session.add_all([legacy_v2_row, legacy_topup_row])
    await db_session.commit()

    active = await pay_svc.get_active_pricing_plans(db_session)
    await db_session.commit()

    assert [p.code for p in active] == [p["code"] for p in pay_svc.PLAN_CATALOGUE]
    # Check exact validity_days preserved on seeded rows (0 must NOT become 30)
    by_code = {p.code: p for p in active}
    assert by_code["pkg_starter"].validity_days == 30
    assert by_code["pkg_creator"].validity_days == 30
    assert by_code["pkg_pro"].validity_days == 90
    assert by_code["pkg_business"].validity_days == 0
    assert by_code["voice_200"].validity_days == 0
    assert by_code["voice_1000"].validity_days == 0
    assert by_code["voice_1000"].minutes == 1200
    assert by_code["recap_180"].validity_days == 0

    # Legacy rows still exist (not deleted) but are deactivated
    leg1 = (await db_session.execute(select(PricingPlan).where(PricingPlan.code == "combo_creator"))).scalar_one()
    leg2 = (await db_session.execute(select(PricingPlan).where(PricingPlan.code == "topup_voice_10"))).scalar_one()
    assert leg1.is_active is False
    assert leg2.is_active is False

    # Admin edits a v3 plan's price and validity_days (0 -> 45)
    ok, updated, msg = await pay_svc.update_pricing_plan_config(
        db_session, "pkg_business", price_mmk=125000, validity_days=45, admin_id=1
    )
    assert ok is True and msg == "PLAN_UPDATED"
    assert updated.price_mmk == 125000
    assert updated.validity_days == 45
    await db_session.commit()

    # Re-seeding must NOT overwrite admin edits and must keep legacy rows inactive
    await pay_svc._ensure_pricing_plans_seeded(db_session)
    await db_session.commit()

    biz = (await db_session.execute(select(PricingPlan).where(PricingPlan.code == "pkg_business"))).scalar_one()
    assert biz.price_mmk == 125000
    assert biz.validity_days == 45

    # Admin list shows only the 10 v3 plans (legacy excluded)
    all_admin_plans = await pay_svc.get_all_pricing_plans(db_session)
    assert len(all_admin_plans) == 10
    assert "combo_creator" not in {p.code for p in all_admin_plans}


# ── 3. Legacy-code in-flight PaymentRequest still approves cleanly ────────────


@pytest.mark.asyncio
async def test_legacy_pending_payment_approves_with_stamped_amounts(db_session: AsyncSession):
    user = User(id=7701, first_name="LegacyBuyer", account_category=AccountCategory.FREE.value)
    db_session.add(user)
    db_session.add(MinuteBalance(user_id=7701))
    await db_session.flush()

    await pay_svc._ensure_pricing_plans_seeded(db_session)
    pay = PaymentRequest(
        id="PAY-LEGACY-V2-001",
        user_id=7701,
        package_code="combo_creator",
        minutes=45,
        recap_credits=70,
        amount_mmk=27000,
        validity_days=30,
        status=PaymentStatus.PENDING.value,
    )
    db_session.add(pay)
    await db_session.commit()

    ok, approved, status_code = await pay_svc.approve_payment_request(db_session, "PAY-LEGACY-V2-001", admin_id=99)
    await db_session.commit()
    assert ok is True and status_code == "APPROVED"

    bal = (await db_session.execute(select(MinuteBalance).where(MinuteBalance.user_id == 7701))).scalar_one()
    assert bal.purchased_seconds == 45 * 60
    u_after = (await db_session.execute(select(User).where(User.id == 7701))).scalar_one()
    assert u_after.recap_credits == 73.0  # 3 default free credits + 70 stamped credits
    grant = (await db_session.execute(select(WalletGrant).where(WalletGrant.payment_request_id == "PAY-LEGACY-V2-001"))).scalar_one()
    assert grant.initial_voice_seconds == 45 * 60
    assert grant.initial_recap_seconds == 70 * 60
    assert grant.package_name == "Creator Combo"
    assert grant.expires_at is not None


# ── 4. Validity-0 end-to-end vs Validity-30 expiry ────────────────────────────


@pytest.mark.asyncio
async def test_validity_0_business_never_expires_and_starter_expires_30d(db_session: AsyncSession):
    for uid in (8801, 8802):
        db_session.add(User(id=uid, first_name=f"U{uid}", account_category=AccountCategory.FREE.value))
        db_session.add(MinuteBalance(user_id=uid))
    await db_session.flush()
    await pay_svc._ensure_pricing_plans_seeded(db_session)
    await db_session.commit()

    # Buying pkg_business (validity_days=0)
    pay_biz = await pay_svc.create_payment_request(db_session, user_id=8801, package_code="pkg_business")
    assert pay_biz.validity_days == 0
    ok_b, _, _ = await pay_svc.approve_payment_request(db_session, pay_biz.id, admin_id=1)
    assert ok_b is True
    await db_session.commit()

    grant_biz = (await db_session.execute(select(WalletGrant).where(WalletGrant.payment_request_id == pay_biz.id))).scalar_one()
    assert grant_biz.expires_at is None
    assert grant_biz.initial_voice_seconds == 1000 * 60
    assert grant_biz.initial_recap_seconds == 180 * 60

    # Buying pkg_starter (validity_days=30)
    pay_st = await pay_svc.create_payment_request(db_session, user_id=8802, package_code="pkg_starter")
    assert pay_st.validity_days == 30
    ok_s, _, _ = await pay_svc.approve_payment_request(db_session, pay_st.id, admin_id=1)
    assert ok_s is True
    await db_session.commit()

    grant_st = (await db_session.execute(select(WalletGrant).where(WalletGrant.payment_request_id == pay_st.id))).scalar_one()
    assert grant_st.expires_at is not None
    # SQLite stores naive timestamps; compare delta directly
    delta = grant_st.expires_at.replace(tzinfo=None) - grant_st.purchased_at.replace(tzinfo=None)
    assert abs(delta - timedelta(days=30)) < timedelta(seconds=5)


@pytest.mark.asyncio
async def test_submit_payment_slip_stamps_validity_0_and_90(db_session: AsyncSession, tmp_path):
    """Mini App /api/vip/submit-slip must stamp plan validity_days=0 (not DB default 30)."""
    from starlette.datastructures import UploadFile
    from app.webapp.api import submit_payment_slip

    user = User(id=8803, first_name="MiniAppBuyer", account_category=AccountCategory.FREE.value)
    db_session.add(user)
    db_session.add(MinuteBalance(user_id=8803))
    await pay_svc._ensure_pricing_plans_seeded(db_session)
    await db_session.commit()

    orig_storage = settings.storage_root
    orig_bot_token = settings.bot_token
    settings.storage_root = tmp_path
    settings.bot_token = ""
    try:
        # 1) pkg_business -> validity_days = 0
        img1 = _make_png_bytes()
        upload1 = UploadFile(filename="slip1.png", file=io.BytesIO(img1))
        await submit_payment_slip(
            package_code="pkg_business",
            amount_mmk=114900,
            slip_file=upload1,
            payment_reference="REF-BIZ-001",
            user=user,
            db=db_session,
            _gate=user,
        )
        req_biz = (
            await db_session.execute(select(PaymentRequest).where(PaymentRequest.payment_reference == "REF-BIZ-001"))
        ).scalar_one()
        assert req_biz.validity_days == 0
        assert req_biz.minutes == 1000
        assert req_biz.recap_credits == 180

        # 2) pkg_pro -> validity_days = 90
        buf2 = io.BytesIO()
        Image.new("RGB", (8, 8), color=(99, 12, 55)).save(buf2, format="PNG")
        upload2 = UploadFile(filename="slip2.png", file=io.BytesIO(buf2.getvalue()))
        await submit_payment_slip(
            package_code="pkg_pro",
            amount_mmk=54900,
            slip_file=upload2,
            payment_reference="REF-PRO-001",
            user=user,
            db=db_session,
            _gate=user,
        )
        req_pro = (
            await db_session.execute(select(PaymentRequest).where(PaymentRequest.payment_reference == "REF-PRO-001"))
        ).scalar_one()
        assert req_pro.validity_days == 90
    finally:
        settings.storage_root = orig_storage
        settings.bot_token = orig_bot_token


# ── 5. _format_plan_presentation ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_format_plan_presentation_v3_fields(db_session: AsyncSession):
    plans = await pay_svc.get_active_pricing_plans(db_session)
    by_code = {p.code: p for p in plans}

    # voice_1000: display_minutes=1000 while grant minutes=1200, bonus_200 badge
    v1000_my = _format_plan_presentation(by_code["voice_1000"], is_my=True)
    v1000_en = _format_plan_presentation(by_code["voice_1000"], is_my=False)
    assert v1000_my["minutes"] == 1200
    assert v1000_my["display_minutes"] == 1000
    assert v1000_my["badge_key"] == "bonus_200"
    assert v1000_my["badge"] == "Bonus +200 မိနစ်"
    assert v1000_en["badge"] == "Bonus +200 min"
    assert v1000_my["validity_days"] == 0
    assert v1000_my["validity_label"] == "သက်တမ်းမကုန်"
    assert v1000_en["validity_label"] == "No Expiry"

    # voice_200: display_minutes defaults to minutes (200)
    v200_my = _format_plan_presentation(by_code["voice_200"], is_my=True)
    assert v200_my["display_minutes"] == 200
    assert v200_my["minutes"] == 200
    assert v200_my["badge_key"] == ""

    # pkg_pro (90d) & pkg_starter (30d) & pkg_business (0d)
    pro_my = _format_plan_presentation(by_code["pkg_pro"], is_my=True)
    pro_en = _format_plan_presentation(by_code["pkg_pro"], is_my=False)
    assert pro_my["validity_label"] == "သက်တမ်း 90 ရက်"
    assert pro_en["validity_label"] == "Valid 90 Days"
    # No stale hardcoded "10%" combo savings claim
    assert all("10%" not in f for f in pro_my["features"])
    assert all("10%" not in f for f in pro_en["features"])

    biz_en = _format_plan_presentation(by_code["pkg_business"], is_my=False)
    assert biz_en["validity_days"] == 0
    assert biz_en["validity_label"] == "No Expiry"


# ── 6. Voice daily cap abolished (default 0) ──────────────────────────────────


@pytest.mark.asyncio
async def test_voice_daily_limit_paid_default_zero_never_blocks(db_session: AsyncSession):
    fresh = Settings(_env_file=None)
    assert fresh.voice_daily_limit_minutes_paid == 0
    assert settings.voice_daily_limit_minutes_paid == 0
    # TTS daily caps untouched
    assert fresh.tts_daily_limit_minutes_free == 15
    assert fresh.tts_daily_limit_minutes_paid == 60

    vm = VoiceModel(
        name="VoxCPM Cloud",
        slug="voxcpm-cloud-ai",
        model_dir="voxcpm",
        pth_path="voxcpm",
    )
    user = User(id=8810, first_name="HeavyPaidUser", account_category=AccountCategory.PURCHASED.value)
    bal = MinuteBalance(user_id=8810, purchased_seconds=20000)
    db_session.add_all([vm, user, bal])
    await db_session.flush()

    # Seed 120 minutes of already-completed Voice Clone jobs today (well above old 30-min cap)
    db_session.add(
        Job(
            id="JOB-HEAVY-TODAY-1",
            user_id=8810,
            model_id=vm.id,
            job_type=JobType.VOICE_CLONE.value,
            status=JobStatus.SUCCESS.value,
            input_text="previous heavy work",
            seconds_consumed=7200,
            created_at=utc_now(),
        )
    )
    await db_session.commit()

    job, err = await create_voice_clone_job(
        db_session,
        user_id=8810,
        model_id=vm.id,
        text="မင်္ဂလာပါ ဒီနေ့ အသံထုတ်စမ်းသပ်မှု ဖြစ်ပါတယ်။",
    )
    assert err == "QUEUED"
    assert job is not None


# ── 7. Buy Tab v8 + Profile Wallet Move + No 0-eating JS ──────────────────────


def test_buy_tab_v8_and_profile_wallet_move_markup_and_js():
    html = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
    i18n_js = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
    admin_js = (ROOT / "app/webapp/static/js/admin.js").read_text(encoding="utf-8")

    # Isolate #tab-vip and #tab-profile blocks
    vip_start = html.index('<section id="tab-vip"')
    vip_end = html.index("</section>", vip_start)
    vip_html = html[vip_start:vip_end]

    prof_start = html.index('<section id="tab-profile"')
    prof_end = html.index("</section>", prof_start)
    prof_html = html[prof_start:prof_end]

    # Part D: deleted elements absent from #tab-vip
    assert "pane-header" not in vip_html
    assert "plan-how-card" not in vip_html
    assert "vip-subbtn-plans" not in vip_html
    assert "vip-subbtn-wallet" not in vip_html
    assert "plan-panel-topup" not in vip_html
    assert "vip-grid-topup" not in vip_html
    assert "vcp-vip-trust-note" not in vip_html
    assert "plan-free-note" not in vip_html

    # Part D: 3-button selector Package (default) / Voice / Recap + collapsed Terms card
    assert 'plan-tabs-3' in vip_html
    assert 'data-plan-cat="combo"' in vip_html
    assert 'data-plan-cat="voice"' in vip_html
    assert 'data-plan-cat="recap"' in vip_html
    assert ">Package<" in vip_html and ">Voice<" in vip_html and ">Recap<" in vip_html
    assert '<details class="card glass vcp-card buy-terms-card mt-3" id="buy-terms-card">' in vip_html
    assert "buy_terms_1" in vip_html and "buy_terms_7" in vip_html

    # Part E: Wallet & Orders moved into #tab-profile with IDs preserved, switchVipSubView gone
    for elem_id in ("vip-grants-list", "vip-orders-list", "vip-tx-list"):
        assert elem_id not in vip_html
        assert elem_id in prof_html
    assert "switchVipSubView" not in app_js
    assert "switchVipSubView" not in html

    # Part C JS audit: no `validity_days || 30` in admin.js
    assert "validity_days || 30" not in admin_js

    # Part D: verbatim MY Terms strings & Buy pill strings present in i18n.js
    assert 'buy_validity_no_expiry: "♾️ သက်တမ်း မကုန်"' in i18n_js
    assert 'buy_badge_bonus_200: "Bonus +200 မိနစ်"' in i18n_js
    assert 'buy_terms_1: "1 Recap Credit = ဗီဒီယို ၁ မိနစ် (Final Output)"' in i18n_js
    assert 'buy_terms_3: "Voice Clone တစ်ခါထုတ် စာလုံး — Free: 3,000 အထိ · ဝယ်ယူထားသူများ: 15,000 အထိ"' in i18n_js
    assert 'buy_terms_7: "တစ်ချိန်တည်း အလုပ် ၁ ခုသာ လုပ်နိုင်သည်"' in i18n_js
    # No stale hardcoded legacy prices in i18n.js
    assert "plan_voice_price" not in i18n_js
    assert "vip_shared_benefits_combo" not in i18n_js
