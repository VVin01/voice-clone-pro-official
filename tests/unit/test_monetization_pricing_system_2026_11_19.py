import asyncio
import io

import pytest
from PIL import Image
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.base import Base
from app.db.session import get_db_session
from app.models.enums import AccountCategory, JobStatus, JobType, PaymentStatus
from app.models.schema import (
    DatabaseMetadata,
    Job,
    JobCostTelemetry,
    MinuteBalance,
    PaymentRequest,
    PricingPlan,
    User,
    VoiceModel,
    WalletReservation,
    WalletTransaction,
    utc_now,
)
from app.services.billing import (
    BILLING_MODE_CREDIT_ONLY,
    BILLING_MODE_CREDIT_PLUS_VOICE,
    BILLING_MODE_SUBTITLE_ONLY,
    BILLING_MODE_VOICE_ONLY,
    InsufficientRecapCreditsError,
    InsufficientVoiceMinutesError,
    admin_refund_settled_job,
    estimate_job_requirements,
    get_wallet_snapshot,
    record_cost_telemetry,
    refund_or_release_job,
    release_job_reservation,
    reserve_for_job,
    settle_job_reservation,
)
from app.services.ledger import get_minute_balance, get_or_create_user
from app.services.payments import (
    PLAN_CATALOGUE,
    approve_payment_request,
    create_payment_request,
    get_active_pricing_plans,
    update_pricing_plan_config,
)
from app.webapp.api import api_router, get_current_user, require_admin, require_gate_verified


def _build_test_app(session_factory, current_user_holder):
    app = FastAPI()
    app.include_router(api_router)

    async def _override_db():
        async with session_factory() as s:
            yield s

    async def _override_user(db: AsyncSession = Depends(get_db_session)):
        return await db.get(User, current_user_holder["user_id"])

    async def _override_admin(db: AsyncSession = Depends(get_db_session)):
        return await db.get(User, current_user_holder["admin_id"])

    app.dependency_overrides[get_db_session] = _override_db
    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[require_gate_verified] = _override_user
    app.dependency_overrides[require_admin] = _override_admin
    return app


def _make_png_bytes(color=(12, 120, 210)) -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", (32, 32), color=color)
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_1_official_10_packages_and_api_vip_plans(db_session: AsyncSession):
    # Pricing v3 (2026-11-23): 10 plans across 3 categories (combo, voice, recap); topup retired.
    plans = await get_active_pricing_plans(db_session)
    assert len(plans) == 10
    by_code = {p.code: p for p in plans}

    # Combo Packages (4)
    assert by_code["pkg_starter"].minutes == 100 and by_code["pkg_starter"].recap_credits == 15 and by_code["pkg_starter"].price_mmk == 14900
    assert by_code["pkg_creator"].minutes == 200 and by_code["pkg_creator"].recap_credits == 30 and by_code["pkg_creator"].price_mmk == 28500
    assert by_code["pkg_pro"].minutes == 500 and by_code["pkg_pro"].recap_credits == 60 and by_code["pkg_pro"].price_mmk == 54900
    assert by_code["pkg_business"].minutes == 1000 and by_code["pkg_business"].recap_credits == 180 and by_code["pkg_business"].price_mmk == 114900

    # Voice Clone Minutes (3)
    assert by_code["voice_200"].minutes == 200 and by_code["voice_200"].price_mmk == 19800
    assert by_code["voice_500"].minutes == 500 and by_code["voice_500"].price_mmk == 39500
    assert by_code["voice_1000"].minutes == 1200 and by_code["voice_1000"].price_mmk == 79200

    # Recap Credits (3)
    assert by_code["recap_30"].recap_credits == 30 and by_code["recap_30"].price_mmk == 13500
    assert by_code["recap_60"].recap_credits == 60 and by_code["recap_60"].price_mmk == 25500
    assert by_code["recap_180"].recap_credits == 180 and by_code["recap_180"].price_mmk == 68400


@pytest.mark.asyncio
async def test_2_combo_and_topup_purchase_credits_wallets_separately(db_session: AsyncSession):
    u, _ = await get_or_create_user(db_session, 900101, first_name="Buyer")
    u.recap_credits = 0
    u.recap_credit_seconds = 0
    bal = await get_minute_balance(db_session, u.id)
    bal.daily_free_seconds = 0
    bal.monthly_free_seconds = 0
    bal.welcome_seconds = 0
    bal.purchased_seconds = 0
    await db_session.flush()

    settings.admin_ids_raw = "999999"
    # Buy Combo Starter (15 Recap Credits + 100 Voice Minutes = 6000s, 14,900 MMK)
    pr = await create_payment_request(
        db_session, user_id=u.id, package_code="pkg_starter", receipt_file_id="slip_combo_1"
    )
    assert pr.amount_mmk == 14900
    assert pr.minutes == 100
    assert pr.recap_credits == 15

    ok, _, code = await approve_payment_request(db_session, pr.id, admin_id=999999)
    assert ok and code == "APPROVED"
    await db_session.flush()
    await db_session.refresh(u)
    await db_session.refresh(bal)

    assert u.recap_credits == pytest.approx(15.0, abs=1e-4)
    assert u.recap_credit_seconds == 900
    assert bal.purchased_seconds == 6000
    assert bal.clone_spendable_seconds == 6000

    # Buy Voice 200 mins (19,800 MMK) -> only voice wallet grows
    pr2 = await create_payment_request(
        db_session, user_id=u.id, package_code="voice_200", receipt_file_id="slip_voice_200"
    )
    await approve_payment_request(db_session, pr2.id, admin_id=999999)
    await db_session.refresh(u)
    await db_session.refresh(bal)
    assert bal.purchased_seconds == 18000  # 300m
    assert u.recap_credit_seconds == 900  # untouched

    # Buy Recap 30 Credits (13,500 MMK) -> only recap wallet grows
    pr3 = await create_payment_request(
        db_session, user_id=u.id, package_code="recap_30", receipt_file_id="slip_recap_30"
    )
    await approve_payment_request(db_session, pr3.id, admin_id=999999)
    await db_session.refresh(u)
    await db_session.refresh(bal)
    assert u.recap_credit_seconds == 2700  # 45 credits
    assert bal.purchased_seconds == 18000  # untouched

    txs = (
        await db_session.execute(
            select(WalletTransaction).where(WalletTransaction.user_id == u.id)
        )
    ).scalars().all()
    assert len(txs) >= 3
    assert any(t.tx_type == "PURCHASE" and t.wallet_kind == "COMBO" for t in txs)


@pytest.mark.asyncio
async def test_3_all_four_billing_modes_second_level_reserve_settle_release(db_session: AsyncSession):
    u, _ = await get_or_create_user(db_session, 900102, first_name="BillTester")
    u.recap_credits = 10.0
    u.recap_credit_seconds = 600  # 10.00 Credits
    bal = await get_minute_balance(db_session, u.id)
    bal.monthly_free_seconds = 0
    bal.welcome_seconds = 0
    bal.purchased_seconds = 300  # 5.00 Voice Minutes
    await db_session.flush()

    # A. Normal AI Voice Recap (gemini): est 180s output (3.0 Cr), actual output 150s (2.5 Cr), 0 voice
    await reserve_for_job(
        db_session, u.id, "job_norm",
        job_type="RECAP", billing_mode=BILLING_MODE_CREDIT_ONLY,
        reserve_recap_seconds=180, reserve_voice_seconds=0,
    )
    snap = await get_wallet_snapshot(db_session, u.id)
    assert snap["available_recap_credits"] == pytest.approx(7.0, abs=1e-2)
    assert snap["reserved_recap_credits"] == pytest.approx(3.0, abs=1e-2)
    assert snap["reserved_voice_seconds"] == 0

    res_a = await settle_job_reservation(
        db_session, u.id, "job_norm", actual_recap_seconds=150, actual_voice_seconds=999
    )
    assert res_a["settled_recap_seconds"] == 150  # 2.50 Cr
    assert res_a["refunded_recap_seconds"] == 30  # 0.50 Cr released!
    assert res_a["settled_voice_seconds"] == 0  # strictly ignored for credit_only!
    await db_session.refresh(u)
    await db_session.refresh(bal)
    assert u.recap_credit_seconds == 450  # 7.50 Cr remaining
    assert u.recap_credits == pytest.approx(7.5, abs=1e-4)
    assert u.reserved_recap_seconds == 0
    assert bal.purchased_seconds == 300  # untouched!

    # B. Custom Voice Clone Recap (voxcpm): output 120s (2.0 Cr) != voice audio 75s (1.25 Mins)
    await reserve_for_job(
        db_session, u.id, "job_vox",
        job_type="RECAP", billing_mode=BILLING_MODE_CREDIT_PLUS_VOICE,
        reserve_recap_seconds=150, reserve_voice_seconds=150,
    )
    assert bal.purchased_seconds == 300  # Non-destructive hold!
    assert bal.reserved_seconds == 150
    assert bal.clone_spendable_seconds == 150

    res_b = await settle_job_reservation(
        db_session, u.id, "job_vox", actual_recap_seconds=120, actual_voice_seconds=75
    )
    assert res_b["settled_recap_seconds"] == 120
    assert res_b["refunded_recap_seconds"] == 30
    assert res_b["settled_voice_seconds"] == 75
    assert res_b["refunded_voice_seconds"] == 75
    await db_session.refresh(u)
    await db_session.refresh(bal)
    assert u.recap_credit_seconds == 330  # 5.50 Cr remaining
    assert bal.purchased_seconds == 225  # 300 - 75
    assert bal.reserved_seconds == 0

    # C. Subtitle Only (none): actual output 90s (1.50 Cr), 0 voice
    await reserve_for_job(
        db_session, u.id, "job_sub",
        job_type="RECAP", billing_mode=BILLING_MODE_SUBTITLE_ONLY,
        reserve_recap_seconds=90, reserve_voice_seconds=0,
    )
    res_c = await settle_job_reservation(
        db_session, u.id, "job_sub", actual_recap_seconds=90, actual_voice_seconds=0
    )
    assert res_c["settled_recap_seconds"] == 90
    assert res_c["settled_voice_seconds"] == 0
    await db_session.refresh(u)
    await db_session.refresh(bal)
    assert u.recap_credit_seconds == 240  # 4.00 Cr left
    assert bal.purchased_seconds == 225  # untouched

    # D. Voice Clone Standalone: actual audio 45s -> 0 Recap Credits deducted
    await reserve_for_job(
        db_session, u.id, "job_vc",
        job_type="VOICE_CLONE", billing_mode=BILLING_MODE_VOICE_ONLY,
        reserve_recap_seconds=0, reserve_voice_seconds=60,
    )
    res_d = await settle_job_reservation(
        db_session, u.id, "job_vc", actual_recap_seconds=0, actual_voice_seconds=45
    )
    assert res_d["settled_voice_seconds"] == 45
    assert res_d["settled_recap_seconds"] == 0
    await db_session.refresh(u)
    await db_session.refresh(bal)
    assert u.recap_credit_seconds == 240
    assert bal.purchased_seconds == 180


@pytest.mark.asyncio
async def test_4_failure_cancel_partial_and_concurrency_protection(db_session: AsyncSession):
    u, _ = await get_or_create_user(db_session, 900103, first_name="ConcUser")
    u.recap_credits = 2.0
    u.recap_credit_seconds = 120  # Exactly 2.00 Credits
    bal = await get_minute_balance(db_session, u.id)
    bal.monthly_free_seconds = 0
    bal.welcome_seconds = 0
    bal.purchased_seconds = 120  # Exactly 2.00 Minutes
    await db_session.flush()

    # Reserve Job 1 for 90s recap + 90s voice -> succeeds, leaves 30s each
    await reserve_for_job(
        db_session, u.id, "job_c1",
        job_type="RECAP", billing_mode=BILLING_MODE_CREDIT_PLUS_VOICE,
        reserve_recap_seconds=90, reserve_voice_seconds=90,
    )
    # Concurrent Job 2 wanting 60s recap MUST raise InsufficientRecapCreditsError!
    with pytest.raises(InsufficientRecapCreditsError):
        await reserve_for_job(
            db_session, u.id, "job_c2",
            job_type="RECAP", billing_mode=BILLING_MODE_CREDIT_ONLY,
            reserve_recap_seconds=60, reserve_voice_seconds=0,
        )

    # Cancel Job 1 twice -> full release, idempotent second call
    rel1 = await release_job_reservation(db_session, u.id, "job_c1", reason="USER_CANCELLED")
    assert rel1["released"] is True and rel1["released_recap_seconds"] == 90 and rel1["released_voice_seconds"] == 90
    rel2 = await release_job_reservation(db_session, u.id, "job_c1", reason="USER_CANCELLED")
    assert rel2["released"] is False
    await db_session.refresh(u)
    await db_session.refresh(bal)
    assert u.recap_credit_seconds == 120 and bal.clone_spendable_seconds == 120

    # Partial segmented delivery: reserve 120s, deliver 60s partial -> settles 60s, refunds 60s
    await reserve_for_job(
        db_session, u.id, "job_partial",
        job_type="RECAP", billing_mode=BILLING_MODE_CREDIT_ONLY,
        reserve_recap_seconds=120, reserve_voice_seconds=0,
    )
    res_p = await settle_job_reservation(
        db_session, u.id, "job_partial", actual_recap_seconds=60, partial_delivered=True
    )
    assert res_p["settled_recap_seconds"] == 60
    assert res_p["refunded_recap_seconds"] == 60
    await db_session.refresh(u)
    assert u.recap_credit_seconds == 60  # 1.00 Cr remaining


@pytest.mark.asyncio
async def test_5_admin_pricing_edit_no_retro_change_refund_and_analytics_endpoints():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    settings.admin_ids_raw = "777777"
    async with session_factory() as s:
        s.add(DatabaseMetadata(key="db_identity", value=settings.expected_db_identity))
        s.add(DatabaseMetadata(key="schema_version", value="2.0.0"))
        s.add(DatabaseMetadata(key="created_at", value=utc_now().isoformat()))
        admin_u, _ = await get_or_create_user(s, 777777, first_name="Admin")
        buyer_u, _ = await get_or_create_user(s, 888888, first_name="Customer", username="cust")
        buyer_u.recap_credits = 5.0
        buyer_u.recap_credit_seconds = 300
        bal = await get_minute_balance(s, buyer_u.id)
        bal.purchased_seconds = 600  # existing 10m balance before price edits
        bal.monthly_free_seconds = 0
        await get_active_pricing_plans(s)

        # Create a settled job for admin refund & analytics test
        j1 = Job(
            id="rc_test_done",
            user_id=buyer_u.id,
            status=JobStatus.SUCCESS.value,
            job_type=JobType.RECAP.value,
            engine="recap",
            output_duration_seconds=120,
            voice_audio_seconds=60,
            recap_credits_charged=2.0,
            output_size_bytes=5_000_000,
        )
        s.add(j1)
        await reserve_for_job(
            s, buyer_u.id, "rc_test_done",
            job_type="RECAP", billing_mode=BILLING_MODE_CREDIT_PLUS_VOICE,
            reserve_recap_seconds=120, reserve_voice_seconds=90,
        )
        await settle_job_reservation(
            s, buyer_u.id, "rc_test_done", actual_recap_seconds=120, actual_voice_seconds=60
        )
        await record_cost_telemetry(
            s, job_id="rc_test_done", user_id=buyer_u.id,
            provider="gemini_text", model_name="gemini-2.5-flash",
            requests_count=2, input_tokens=10000, output_tokens=2000,
        )
        await record_cost_telemetry(
            s, job_id="rc_test_done", user_id=buyer_u.id,
            provider="voxcpm", model_name="VoxCPM",
            audio_seconds=60.0, runtime_seconds=36.0,
        )
        await record_cost_telemetry(
            s, job_id="rc_test_done", user_id=buyer_u.id,
            provider="bandwidth", bytes_transferred=5_000_000,
        )
        await s.commit()

    holder = {"user_id": 888888, "admin_id": 777777}
    app = _build_test_app(session_factory, holder)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # A. VIP Plans endpoint returns all 10 Pricing v3 plans across categories
        r_plans = await client.get("/api/vip/plans")
        assert r_plans.status_code == 200
        pdata = r_plans.json()
        assert len(pdata["plans"]) == 10
        assert [c["id"] for c in pdata["categories"]] == ["voice", "recap", "combo", "topup"]

        # B. Pre-job estimators
        r_est_r = await client.post("/api/recap/estimate", json={"duration_seconds": 150, "tts_engine": "gemini"})
        assert r_est_r.status_code == 200
        er = r_est_r.json()
        assert er["estimated_recap_credits"] == 2.5
        assert er["estimated_voice_seconds"] == 0
        assert er["billing_mode"] == "credit_only"

        r_est_none = await client.post("/api/recap/estimate", json={"duration_seconds": 90, "tts_engine": "none"})
        assert r_est_none.json()["billing_mode"] == "subtitle_only"
        assert r_est_none.json()["estimated_recap_credits"] == 1.5

        r_est_v = await client.post("/api/voices/estimate", json={"text": "မင်္ဂလာပါ " * 20})
        assert r_est_v.status_code == 200 and r_est_v.json()["estimated_voice_seconds"] > 0

        # C. Slip upload + Duplicate slip/payment_reference protection
        png1 = _make_png_bytes((10, 20, 30))
        r_slip = await client.post(
            "/api/vip/submit-slip",
            data={"package_code": "recap_30", "amount_mmk": "999", "payment_reference": "KPAY-TXN-7711"},
            files={"slip_file": ("slip1.png", png1, "image/png")},
        )
        assert r_slip.status_code == 200
        # Client amount 999 ignored; plan price 13,500 MMK snapshotted!
        slip_id = r_slip.json()["request_id"]

        # Duplicate payment_reference blocked!
        png2 = _make_png_bytes((50, 60, 70))
        r_dup_ref = await client.post(
            "/api/vip/submit-slip",
            data={"package_code": "recap_30", "amount_mmk": "13500", "payment_reference": "KPAY-TXN-7711"},
            files={"slip_file": ("slip2.png", png2, "image/png")},
        )
        assert r_dup_ref.status_code == 409

        # Approve slip via admin endpoint
        r_app = await client.post("/api/admin/approve-slip", json={"request_id": slip_id})
        assert r_app.status_code == 200 and r_app.json()["success"] is True

        # D. Admin edits package price, credits, minutes, active status -> NEVER alters existing balance
        async with session_factory() as s_chk:
            u_before = await s_chk.get(User, 888888)
            bal_before = await get_minute_balance(s_chk, 888888)
            cr_sec_before = u_before.recap_credit_seconds
            v_sec_before = bal_before.purchased_seconds

        r_edit = await client.post(
            "/api/admin/pricing/voice_200",
            json={"price_mmk": 21000, "minutes": 210, "is_active": False},
        )
        assert r_edit.status_code == 200
        assert r_edit.json()["price_mmk"] == 21000
        assert r_edit.json()["is_active"] is False

        # Existing wallet untouched!
        async with session_factory() as s_chk2:
            u_after = await s_chk2.get(User, 888888)
            bal_after = await get_minute_balance(s_chk2, 888888)
            assert u_after.recap_credit_seconds == cr_sec_before
            assert bal_after.purchased_seconds == v_sec_before

        # Disabled package blocks new checkout
        png3 = _make_png_bytes((90, 99, 105))
        r_dis = await client.post(
            "/api/vip/submit-slip",
            data={"package_code": "voice_200", "amount_mmk": "21000"},
            files={"slip_file": ("slip3.png", png3, "image/png")},
        )
        assert r_dis.status_code == 400

        # E. Admin Decimal Adjust (+2.5 credits) & Idempotent Job Refund
        r_adj = await client.post(
            "/api/admin/adjust-user",
            json={"user_id": 888888, "add_recap_credits": 2.5, "reason": "Promo bonus decimal"},
        )
        assert r_adj.status_code == 200
        assert r_adj.json()["recap_credits_applied"] == pytest.approx(2.5, abs=1e-4)

        r_ref = await client.post("/api/admin/refunds/job/rc_test_done", json={"reason": "Customer QA issue"})
        assert r_ref.status_code == 200
        assert r_ref.json()["refunded_recap_credits"] == 2.0
        assert r_ref.json()["refunded_voice_minutes"] == 1.0
        # Double refund blocked cleanly
        r_ref2 = await client.post("/api/admin/refunds/job/rc_test_done", json={"reason": "Duplicate"})
        assert r_ref2.status_code == 400

        # F. Wallet Summary, Transactions, Orders & Admin Analytics (100% real DB)
        r_sum = await client.get("/api/wallet/summary")
        assert r_sum.status_code == 200 and "available_recap_credits" in r_sum.json()
        r_txs = await client.get("/api/wallet/transactions")
        assert r_txs.status_code == 200 and r_txs.json()["count"] >= 3
        r_ord = await client.get("/api/vip/orders")
        assert r_ord.status_code == 200 and r_ord.json()["count"] == 1
        r_ana = await client.get("/api/admin/analytics?days=30")
        assert r_ana.status_code == 200
        ana = r_ana.json()
        assert ana["revenue"]["total_approved_mmk"] == 13500
        assert ana["revenue"]["top_package"]["package_code"] == "recap_30"
        assert ana["costs"]["gemini_text_usd"] > 0
        assert ana["costs"]["telemetry_summary"]["gemini_input_tokens"] == 10000

    await engine.dispose()


@pytest.mark.asyncio
async def test_6_http_recap_generate_and_cancel_routes(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from app.db import session as session_mod
    from app.services.downloader import MediaDownloader
    from app.webapp import api as api_mod

    db_file = str(tmp_path / "monetization_http.db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(api_mod, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(settings, "admin_ids_raw", "777777")

    async with session_factory() as s:
        s.add(DatabaseMetadata(key="db_identity", value=settings.expected_db_identity))
        s.add(DatabaseMetadata(key="schema_version", value="2.0.0"))
        s.add(DatabaseMetadata(key="created_at", value=utc_now().isoformat()))
        u, _ = await get_or_create_user(s, 888999, first_name="HttpBuyer")
        u.recap_credits = 3.0
        u.recap_credit_seconds = 180  # 3.00 Credits
        bal = await get_minute_balance(s, u.id)
        bal.daily_free_seconds = 0
        bal.monthly_free_seconds = 0
        bal.welcome_seconds = 0
        bal.purchased_seconds = 60  # 1.00 Voice Minute
        await s.commit()

    # Stub background worker dispatch & probe/disk/URL-verify guards (hermetic — no network)
    monkeypatch.setattr(api_mod, "_require_disk_headroom", lambda: None)
    monkeypatch.setattr(api_mod, "_run_recap_background_job", AsyncMock())
    monkeypatch.setattr(api_mod, "_execute_recap_background_job", AsyncMock())
    monkeypatch.setattr(api_mod, "_recap_submit_eta_range", AsyncMock(return_value=[30, 90]))
    monkeypatch.setattr(
        MediaDownloader,
        "get_info",
        lambda self, url: {
            "title": "Sub Only Test",
            "duration": 120,
            "webpage_url": url,
            "extractor": "youtube",
            "formats": [{"format_id": "18", "ext": "mp4"}],
        },
    )

    holder = {"user_id": 888999, "admin_id": 777777}
    app = _build_test_app(session_factory, holder)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Subtitle-only (`none` engine) consumes Recap Credits ONLY (120s = 2.0 Cr), 0 Voice Minutes
        r_sub = await client.post(
            "/api/recap/generate",
            json={
                "url": "https://youtu.be/dummy12345",
                "title": "Sub Only Test",
                "duration_seconds": 120,
                "tts_engine": "none",
                "ratio": "9:16",
            },
        )
        assert r_sub.status_code == 200, r_sub.text
        job_sub_id = r_sub.json()["recap_id"]

        # Verify held balance (3.00 - 2.00 = 1.00 Cr available, 1.00 min voice untouched)
        r_w = await client.get("/api/wallet/summary")
        assert r_w.status_code == 200
        assert r_w.json()["available_recap_credits"] == pytest.approx(1.0, abs=1e-2)
        assert r_w.json()["reserved_recap_credits"] == pytest.approx(2.0, abs=1e-2)
        assert r_w.json()["available_voice_seconds"] == 60
        assert r_w.json()["reserved_voice_seconds"] == 0

        # 2. Default max_concurrent=1 -> concurrent submit returns 429; with slot allowance=3, insufficient credits returns 403
        monkeypatch.setattr(settings, "max_concurrent_jobs_per_user", 1)
        r_429 = await client.post(
            "/api/recap/generate",
            json={
                "url": "https://youtu.be/dummy99999",
                "title": "Low Cr Test",
                "duration_seconds": 120,
                "tts_engine": "gemini",
                "ratio": "9:16",
            },
        )
        assert r_429.status_code == 429

        monkeypatch.setattr(settings, "max_concurrent_jobs_per_user", 3)
        r_low = await client.post(
            "/api/recap/generate",
            json={
                "url": "https://youtu.be/dummy99999",
                "title": "Low Cr Test",
                "duration_seconds": 120,
                "tts_engine": "gemini",
                "ratio": "9:16",
            },
        )
        assert r_low.status_code == 403

        # 3. Cancel Recap job -> full release (back to 3.00 Cr available)
        r_can = await client.post(f"/api/recap/{job_sub_id}/cancel")
        assert r_can.status_code == 200, r_can.text
        r_w2 = await client.get("/api/wallet/summary")
        assert r_w2.json()["available_recap_credits"] == pytest.approx(3.0, abs=1e-2)
        assert r_w2.json()["reserved_recap_credits"] == 0.0

        # 4. Standalone Voice Job cancel via /api/jobs/{job_id}/cancel -> releases voice hold
        async with session_factory() as s2:
            vj = Job(
                id="vc_cancel_http_1",
                user_id=888999,
                status=JobStatus.QUEUED.value,
                job_type=JobType.VOICE_CLONE.value,
                engine="voxcpm",
                seconds_estimated=45,
            )
            s2.add(vj)
            await reserve_for_job(
                s2, 888999, "vc_cancel_http_1",
                job_type="VOICE_CLONE", billing_mode=BILLING_MODE_VOICE_ONLY,
                reserve_recap_seconds=0, reserve_voice_seconds=45,
            )
            await s2.commit()

        r_vc = await client.post("/api/jobs/vc_cancel_http_1/cancel")
        assert r_vc.status_code == 200, r_vc.text
        r_w3 = await client.get("/api/wallet/summary")
        assert r_w3.json()["available_voice_seconds"] == 60
        assert r_w3.json()["reserved_voice_seconds"] == 0

    await engine.dispose()
