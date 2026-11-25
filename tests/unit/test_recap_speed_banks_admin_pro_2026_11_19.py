"""Regression & integration suite for 2026-11-19 upgrades:
1. Recap Speed Optimizations + ASS blur_boxes fix
2. Dynamic Multi-Bank / Payment Accounts (Admin + Mini App VIP + Bot Invoice + QR)
3. Complete Admin Power-Tools (Edit-Approve, Order Search/CSV, Expiring Watchlist &
   Grant Extend/Revoke, Targeted Broadcast Preview/History, Users Filter/Ledger,
   Audit Search, Create Promo Plan, Runtime Settings, Ops Queues, Lazy admin_pro.js)
"""
from __future__ import annotations

import json
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.enums import AccountCategory, PaymentStatus
from app.models.schema import Base, MinuteBalance, PaymentRequest, User, WalletGrant, utc_now

ROOT = Path(__file__).resolve().parents[2]


@pytest_asyncio.fixture
async def db_session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/admin_pro.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


# ── 1. Recap Speed Optimizations & ASS Blur-Box Fix ──────────────────────────

def test_probe_duration_wav_fast_path_skips_ffprobe(tmp_path, monkeypatch):
    from app.services.recap_engine import core

    wav = tmp_path / "seg.wav"
    with wave.open(str(wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 32000)  # exactly 2.0s

    def _boom(*a, **kw):
        raise AssertionError("ffprobe must NOT be spawned for standard WAV")

    monkeypatch.setattr(core, "_run_media", _boom)
    assert abs(core.probe_duration("ffprobe", wav) - 2.0) < 0.001


def test_render_video_part_intermediate_uses_ultrafast_no_faststart(tmp_path, monkeypatch):
    from app.services.recap_engine import core

    captured = {}

    def _fake_run(cmd, timeout=None, what=""):
        captured["cmd"] = list(cmd)
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr(core, "_run_media", _fake_run)
    src = tmp_path / "src.mp4"
    src.write_bytes(b"0" * 64)
    out = tmp_path / "part.mp4"
    core.render_video_part(
        "ffmpeg", src, out, {"video_start": 0.0, "video_end": 2.0}, 2.0,
        "libx264", render_quality="max", intermediate=True,
    )
    cmd = captured["cmd"]
    assert "-preset" in cmd and cmd[cmd.index("-preset") + 1] == "ultrafast"
    assert "-crf" in cmd and cmd[cmd.index("-crf") + 1] == "16"
    assert "+faststart" not in cmd


def test_mux_audio_embeds_soft_subtitles_in_single_pass(tmp_path, monkeypatch):
    from app.services.recap_engine import core

    captured = {}

    def _fake_run(cmd, timeout=None, what=""):
        captured["cmd"] = list(cmd)
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr(core, "_run_media", _fake_run)
    v = tmp_path / "v.mp4"
    a = tmp_path / "a.wav"
    s = tmp_path / "sub.srt"
    v.write_bytes(b"v")
    a.write_bytes(b"a")
    s.write_text("1\n00:00:00,000 --> 00:00:02,000\nမင်္ဂလာပါ\n", encoding="utf-8")
    embedded = core.mux_audio("ffmpeg", v, a, tmp_path / "out.mp4", soft_srt_path=s)
    assert embedded is True
    cmd = captured["cmd"]
    assert "-c:s" in cmd and "mov_text" in cmd
    assert "language=mya" in cmd


def test_recap_speed_source_invariants_and_downloader_fragments():
    core_src = (ROOT / "app/services/recap_engine/core.py").read_text(encoding="utf-8")
    pipe_src = (ROOT / "app/services/recap_engine/recap_pipeline.py").read_text(encoding="utf-8")
    dl_src = (ROOT / "app/services/downloader.py").read_text(encoding="utf-8")
    # 1. Strong protection folded into overlay graph & skipped post-mux when baked
    assert 'if bool(overlays.get("protection_strong")):' in core_src
    assert 'and not result.get("protection_strong_baked")' in pipe_src
    # 2. Timeline/ASS skips 10s probe encode
    assert "if timeline_spec is not None or ass_burn_file is not None:" in core_src
    # 3. ASS blur_boxes typo fixed
    assert 'boxes=self.overlays.get("blur_boxes") or self.overlays.get("boxes")' in core_src
    # 4. PNG prewarm gated to png/timeline modes only
    assert '_srm_pre in ("png", "timeline")' in core_src
    # 5. Concurrent fragment downloads enabled
    assert "'concurrent_fragment_downloads': 4" in dl_src


# ── 2. Multi-Bank / Payment Accounts (Full Stack) ────────────────────────────

@pytest.mark.asyncio
async def test_multi_bank_accounts_crud_sync_and_bot_html(db_session, tmp_path, monkeypatch):
    from app.services.payments import (
        format_extra_payment_accounts_html,
        get_payment_account_info,
        list_payment_accounts,
        save_payment_accounts,
    )
    from app.webapp import api

    monkeypatch.setattr(settings, "payment_kpay_number", "09111111111")
    monkeypatch.setattr(settings, "payment_kpay_name", "U KPay")
    monkeypatch.setattr(settings, "payment_wave_number", "09222222222")
    monkeypatch.setattr(settings, "payment_wave_name", "U Wave")
    monkeypatch.setattr(settings, "storage_root", tmp_path)

    # Default synthesizes KPay + Wave from legacy
    initial = await list_payment_accounts(db_session, include_inactive=True)
    assert len(initial) == 2
    assert format_extra_payment_accounts_html(initial, "my") == ""

    # Admin adds AYA Bank & CB Pay + updates KBZPay
    admin = User(id=990001, first_name="Boss", account_category=AccountCategory.PURCHASED.value)
    db_session.add(admin)
    await db_session.commit()

    res = await api.upsert_admin_payment_accounts(
        api.AdminPaymentAccountsBulkPayload(
            account=api.AdminPaymentAccountPayload(
                provider="AYA Bank",
                account_type="bank",
                number="0012-3456-7890",
                name="Daw Mya",
                bank_branch="Yangon Main",
                note="Put ID in remark",
                color="#B91C1C",
            )
        ),
        admin=admin,
        db=db_session,
    )
    assert res["success"] is True
    assert len(res["accounts"]) == 3
    aya = next(a for a in res["accounts"] if a["provider"] == "AYA Bank")

    # Telegram Bot invoice extra HTML renders the 3rd bank cleanly
    html_extra = format_extra_payment_accounts_html(res["accounts"], "my")
    assert "AYA Bank" in html_extra and "0012-3456-7890" in html_extra and "Yangon Main" in html_extra

    # Bidirectional legacy sync when primary KBZPay is edited via payment-accounts
    kpay_acc = next(a for a in res["accounts"] if a["id"] == "kpay_primary")
    kpay_acc["number"] = "09999888777"
    await save_payment_accounts(db_session, res["accounts"], admin_id=admin.id)
    await db_session.commit()
    legacy_after = await get_payment_account_info(db_session)
    assert legacy_after["kpay_number"] == "09999888777"

    # VIP plans endpoint returns all 3 active accounts dynamically
    vip_data = await api.get_vip_plans(user=admin, db=db_session)
    assert len(vip_data["payment_accounts"]) == 3
    assert any(a["provider"] == "AYA Bank" for a in vip_data["payment_accounts"])

    # Delete AYA Bank works; cannot delete last active account
    del_res = await api.delete_admin_payment_account(aya["id"], admin=admin, db=db_session)
    assert del_res["success"] is True and len(del_res["accounts"]) == 2


# ── 3. Admin Power-Tools Backend Endpoints ───────────────────────────────────

@pytest.mark.asyncio
async def test_admin_power_tools_endpoints_end_to_end(db_session, monkeypatch):
    from app.services.billing import create_wallet_grant
    from app.webapp import api

    monkeypatch.setattr(settings, "bot_token", "")

    admin = User(id=990010, first_name="Admin", account_category=AccountCategory.PURCHASED.value)
    buyer = User(
        id=88001001, first_name="Ko Aye", username="koaye",
        account_category=AccountCategory.PURCHASED.value,
        recap_credits=20.0,
    )
    db_session.add_all([admin, buyer])
    await db_session.flush()
    db_session.add(MinuteBalance(user_id=buyer.id, purchased_seconds=600))
    await db_session.flush()

    # Pending slip with payment_reference
    pr = PaymentRequest(
        id="PAY-PRO-1", user_id=buyer.id, package_code="combo_starter",
        minutes=20, recap_credits=30, validity_days=30,
        amount_mmk=13500, payment_reference="KBZ-TXN-5566",
        status=PaymentStatus.PENDING.value,
    )
    db_session.add(pr)
    await db_session.commit()

    # 1. Enriched pending slips + Edit-and-Approve from Mini App
    slips = await api.get_pending_payment_slips(admin=admin, db=db_session)
    assert len(slips) == 1
    assert slips[0]["payment_reference"] == "KBZ-TXN-5566"
    assert slips[0]["recap_credits"] == 30 and slips[0]["validity_days"] == 30

    ea = await api.edit_and_approve_payment_slip(
        api.EditApproveSlipRequest(
            request_id="PAY-PRO-1", corrected_minutes=25, corrected_recap_credits=35, validity_days=45
        ),
        admin=admin,
        db=db_session,
    )
    assert ea["success"] is True and ea["minutes"] == 25 and ea["recap_credits"] == 35 and ea["validity_days"] == 45

    # 2. Orders search + CSV export
    ord_search = await api.get_admin_orders(status_filter="APPROVED", search="KBZ-TXN-5566", days=30, limit=50, admin=admin, db=db_session)
    assert ord_search["count"] == 1
    csv_resp = await api.export_admin_orders_csv(status_filter="APPROVED", search="KBZ-TXN-5566", days=30, admin=admin, db=db_session)
    csv_txt = csv_resp.body.decode("utf-8-sig")
    assert "PAY-PRO-1" in csv_txt and "KBZ-TXN-5566" in csv_txt

    # 3. Expiring watchlist + Grant Extend + Grant Revoke
    soon_grant = await create_wallet_grant(
        db_session, user_id=buyer.id, source_type="ADMIN_GRANT",
        recap_seconds=600, package_code="promo_test", validity_days=3,
    )
    await db_session.commit()
    watch = await api.get_admin_expiring_grants(days=7, limit=50, admin=admin, db=db_session)
    assert any(g["grant_id"] == soon_grant.id for g in watch["grants"])

    ext = await api.extend_admin_wallet_grant(
        soon_grant.id, api.AdminExtendGrantRequest(add_days=15, reason="VIP bonus", notify_user=False),
        admin=admin, db=db_session,
    )
    assert ext["success"] is True

    rev = await api.revoke_admin_wallet_grant(
        soon_grant.id, api.AdminRevokeGrantRequest(reason="Test revoke"),
        admin=admin, db=db_session,
    )
    assert rev["success"] is True and rev["revoked_recap_credits"] == 10.0

    # 4. Create Custom/Promo Plan
    cp = await api.create_admin_pricing_plan(
        api.AdminPricingCreateRequest(
            code="promo_thingyan", name="Thingyan Combo", category="combo",
            minutes=30, recap_credits=40, price_mmk=15000, validity_days=30, badge="Promo",
        ),
        admin=admin,
        db=db_session,
    )
    assert cp["success"] is True and cp["code"] == "promo_thingyan"

    # 5. Broadcast preview & history + Audit filter + Users filter + Runtime settings + Ops queues
    bc_prev = await api.preview_admin_broadcast(audience="vip", admin=admin, db=db_session)
    assert bc_prev["deliverable"] >= 1
    bc_hist = await api.get_admin_broadcast_history(limit=10, admin=admin, db=db_session)
    assert "campaigns" in bc_hist

    aud_grant = await api.get_admin_audit_log(limit=20, offset=0, action_prefix="grant.", q="", admin=admin, db=db_session)
    assert len(aud_grant) >= 2

    u_detail = await api.get_admin_user_detail(buyer.id, admin=admin, db=db_session)
    assert "wallet_transactions_recent" in u_detail and len(u_detail["wallet_transactions_recent"]) >= 1

    # Test users sort by balance + filter
    u_sorted = await api.list_admin_users(limit=20, offset=0, admin=admin, db=db_session, category="", search="", filter="", sort="balance")
    assert u_sorted["total"] >= 2 and u_sorted["users"][0]["id"] == buyer.id

    # Test revoking a VOICE_MINUTE grant deducts from MinuteBalance cleanly
    v_grant = await create_wallet_grant(
        db_session, user_id=buyer.id, source_type="ADMIN_GRANT",
        voice_seconds=300, package_code="voice_test", validity_days=7,
    )
    await db_session.commit()
    v_rev = await api.revoke_admin_wallet_grant(
        v_grant.id, api.AdminRevokeGrantRequest(reason="Voice revoke test"),
        admin=admin, db=db_session,
    )
    assert v_rev["success"] is True and v_rev["revoked_voice_minutes"] == 5.0

    st_res = await api.update_admin_settings(
        api.AdminSettingsPayload(recap_worker_concurrency=3, ai_prompt_profile="v2", usd_to_mmk_rate=4800.0),
        admin=admin,
        db=db_session,
    )
    assert st_res["success"] is True
    st_get = await api.get_admin_settings(admin=admin, db=db_session)
    assert st_get["recap_worker_concurrency"] == 3 and st_get["usd_to_mmk_rate"] == 4800.0

    ops = await api.get_admin_ops_queues(admin=admin, db=db_session)
    assert "custom_voice_pending_count" in ops and "group_ai_escalations_pending_count" in ops


def test_frontend_admin_pro_and_bundle_budget():
    html = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
    admin_pro = (ROOT / "app/webapp/static/js/admin_pro.js").read_text(encoding="utf-8")
    assert 'id="extra-payment-accounts"' in html
    assert '<option value="TTS">' in html
    assert html.count("?v=20261125_admin_r1") == 5
    assert "/static/js/admin_pro.js" in app_js
    for marker in ("admp-banks-card", "admp-orders-bar", "admp-expiring-card", "admp-bc-aud", "admp-audit-bar", "admp-runtime-card"):
        assert marker in admin_pro
    lf_sum = sum(
        len((ROOT / f).read_bytes().replace(b"\r\n", b"\n"))
        for f in ("app/webapp/static/css/style.css", "app/webapp/static/js/app.js", "app/webapp/static/js/i18n.js")
    )
    assert lf_sum <= 1_650_000, f"Bundle budget exceeded: {lf_sum}"


def test_group_ai_senior_human_admin_training_and_sheet_sync():
    from app.services import group_ai_admin as gai
    from app.services import private_ai as pai

    sheet = gai.build_fact_sheet()
    # Built-in default sheet remains compact (~9.7k chars, well inside LLM budget)
    assert len(sheet) < 12000, f"Fact sheet unexpectedly bloated: {len(sheet)}"
    # Up-to-date features synced into Knowledge Sheet
    assert "Edge Neural အသံ ၈ မျိုး" in sheet
    assert "Timeline PNG" in sheet and "ASS Fast Burn" in sheet and "CapCut Karaoke" in sheet
    assert "Logo (Size & Opacity" in sheet and "9:16" in sheet and "16:9" in sheet and "1:1" in sheet
    assert "Bank / Wallet / QR" in sheet
    assert "+7 ရက် Grace" in sheet
    # Senior Human-Admin EQ & consultative rules in SYSTEM_INSTRUCTIONS & Private AI
    assert "Senior Admin" in gai.SYSTEM_INSTRUCTIONS
    assert "Consultative Advisor" in gai.SYSTEM_INSTRUCTIONS
    assert "Anti-Scam" in gai.SYSTEM_INSTRUCTIONS
    assert "Senior Admin" in pai.PRIVATE_SYSTEM_INSTRUCTIONS
