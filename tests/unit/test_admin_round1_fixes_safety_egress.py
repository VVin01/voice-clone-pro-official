"""Unit tests for Admin Console Round 1 — Bugs, Safety Confirmations, Yangon Formatting & Egress Diet (2026-11-25).

Pins all Round 1 acceptance invariants:
- Step 0: `yt-dlp==2026.11.23` in `requirements.txt` & `requirements.lock` (`bgutil-ytdlp-pot-provider==2.0.2` unchanged).
- B1: `POST /api/admin/edit-approve-slip` invokes `sync_admin_copies` and credits fractional promo recap credits;
      Bot DM `admin:pay_edit:` includes `+20% ပရိုမို` (`admin:pay_edit_promo20:`) pre-filling x1.2 minutes & 2-dp credits.
- B2: `get_admin_user_detail` expires & commits due `WalletGrant`s BEFORE `_admin_user_row` so header and grant card agree;
      `balance_seconds` equals `clone_spendable_seconds` (excluding TTS-only `referral_seconds`);
      `list_admin_users(sort="balance_desc")` sorts by effective spendable balance.
- B3: `get_admin_overview` returns live `_get_laptop_worker_status` (never hardcoded `"ONLINE (VPS Server)"`);
      `get_admin_analytics` buckets revenue on `COALESCE(reviewed_at, created_at)` in `Asia/Yangon` with zero `Job` blob loads.
- B4: `defer(Job.final_script)`, `defer(Job.recap_config)`, `defer(Job.input_text)` active on `/api/admin/queue`,
      `/api/admin/history`, and `/api/admin/users/{id}` `recent_jobs`.
- B5: `AuditLog` entries written for `retry_queued_job` (`job.admin_retry`), `cancel_queued_job` (`job.cancel`),
      `purge_admin_history` (`history.purge`), `admin_activate_local_bot_api` (`ops.local_bot_api_activate`), and
      `handle_moderation_ban_callback` (`group_mod.gai_ban` + `↩️ ပြန်ဖြည်မည်` `gmod_undo:unban:` button).
- B6 & B7: `pending-slips` returns ISO-8601 with UTC offset; `extend_admin_wallet_grant` formats customer DM date in
           `Asia/Yangon`; `POST /api/admin/orders/export-dm` sends Yangon CSV document to admin's Telegram DM and logs
           `orders.export_dm`, while UI no longer calls `window.open` for CSV export.
- F1–F5: Universal formatters (`admFmtYangon`, `admFmtSecs`, `admFmtCredits`, `admFmtMmk`), repaired slip bindings &
         preset chips, segmented-broadcast & `adjust-user` confirmation guards, zero `/api/admin/fleet/status` polls,
         adaptive queue polling, and dirty-only `saveAdminSettings` covering `#admp-runtime-card`.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select

from app.config import settings
from app.handlers import admin as admin_handlers
from app.handlers import group_admin as group_admin_handlers
from app.models.enums import AccountCategory, JobStatus, JobType, PaymentStatus
from app.models.schema import (
    AuditLog,
    BotSetting,
    GroupAiModerationStrike,
    Job,
    MinuteBalance,
    PaymentRequest,
    User,
    WalletGrant,
)
from app.services import ledger as ledger_mod
from app.services.billing import create_wallet_grant
from app.webapp import api as api_mod

PaymentReviewFSM = admin_handlers.PaymentReviewFSM

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
ADMIN_PRO_JS = (ROOT / "app/webapp/static/js/admin_pro.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")


def test_step0_ytdlp_pinned_and_pot_provider_unchanged():
    for fname in ("requirements.txt", "requirements.lock"):
        lines = [
            ln.strip()
            for ln in (ROOT / fname).read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        ytdlp_lines = [ln for ln in lines if ln.startswith("yt-dlp==")]
        pot_lines = [ln for ln in lines if ln.startswith("bgutil-ytdlp-pot-provider==")]
        assert ytdlp_lines == ["yt-dlp==2026.11.23"], f"Unexpected yt-dlp pin in {fname}: {ytdlp_lines}"
        assert pot_lines == ["bgutil-ytdlp-pot-provider==2.0.2"], f"Unexpected pot pin in {fname}: {pot_lines}"


@pytest.mark.asyncio
async def test_b1_edit_approve_slip_syncs_admin_copies_and_credits_fractional_promo(
    db_session, monkeypatch
):
    from app.services import payment_review as pr_mod

    monkeypatch.setattr(settings, "bot_token", "123456:TEST_TOKEN")
    monkeypatch.setattr(settings, "admin_ids_raw", "990101")

    fake_bot = SimpleNamespace(
        send_message=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )
    monkeypatch.setattr(api_mod, "create_telegram_bot", lambda token: fake_bot)
    sync_spy = AsyncMock(return_value=1)
    monkeypatch.setattr(pr_mod, "sync_admin_copies", sync_spy)

    admin = User(id=990101, first_name="BossAdmin", account_category=AccountCategory.PURCHASED.value)
    buyer = User(id=88010101, first_name="Ma Hla", account_category=AccountCategory.FREE.value, recap_credits=0.0)
    db_session.add_all([admin, buyer])
    await db_session.flush()

    slip = PaymentRequest(
        id="PAY-R1-B1",
        user_id=buyer.id,
        package_code="combo_starter",
        minutes=20,
        recap_credits=5,
        validity_days=30,
        amount_mmk=13500,
        status=PaymentStatus.PENDING.value,
    )
    db_session.add(slip)
    await db_session.commit()

    res = await api_mod.edit_and_approve_payment_slip(
        api_mod.EditApproveSlipRequest(
            request_id="PAY-R1-B1",
            corrected_minutes=24,
            corrected_recap_credits=6.25,
            validity_days=30,
            edit_note="+20% promo + bonus",
        ),
        admin=admin,
        db=db_session,
    )
    assert res["success"] is True
    assert res["minutes"] == 24
    assert res["recap_credits"] == 6.25
    assert sync_spy.await_count == 1
    assert fake_bot.send_message.await_count == 1

    await db_session.refresh(buyer)
    assert buyer.recap_credits == pytest.approx(6.25)
    bal = await db_session.get(MinuteBalance, buyer.id)
    assert bal is not None and bal.purchased_seconds == 24 * 60
    grant = (
        await db_session.execute(select(WalletGrant).where(WalletGrant.user_id == buyer.id))
    ).scalar_one()
    assert grant.remaining_recap_seconds == round(6.25 * 60)


@pytest.mark.asyncio
async def test_b1_bot_dm_pay_edit_renders_promo20_and_prefills_1_2x(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", "990102")

    kb = admin_handlers._pay_edit_cancel_kb("PAY-DM-20", include_promo=True)
    cb_datas = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "admin:pay_edit_promo20:PAY-DM-20" in cb_datas
    assert "admin:pay_edit_cancel:PAY-DM-20" in cb_datas

    admin = User(id=990102, first_name="Admin", account_category=AccountCategory.PURCHASED.value)
    buyer = User(id=88010201, first_name="Ko Tun", account_category=AccountCategory.FREE.value)
    db_session.add_all([admin, buyer])
    await db_session.flush()
    slip = PaymentRequest(
        id="PAY-DM-20",
        user_id=buyer.id,
        package_code="combo_starter",
        minutes=25,
        recap_credits=7,
        validity_days=30,
        amount_mmk=13500,
        status=PaymentStatus.PENDING.value,
        review_claimed_by=990102,
    )
    db_session.add(slip)
    await db_session.commit()

    fsm_data: dict = {}
    fsm_state = {"current": PaymentReviewFSM.waiting_for_corrected_minutes}

    class _FakeFSM:
        async def set_state(self, st):
            fsm_state["current"] = st

        async def update_data(self, **kw):
            fsm_data.update(kw)

        async def clear(self):
            fsm_data.clear()
            fsm_state["current"] = None

    sent_msgs = []
    cb = SimpleNamespace(
        data="admin:pay_edit_promo20:PAY-DM-20",
        from_user=SimpleNamespace(id=990102, full_name="Admin"),
        message=SimpleNamespace(
            edit_text=AsyncMock(side_effect=lambda text, **kw: sent_msgs.append((text, kw))),
            answer=AsyncMock(side_effect=lambda text, **kw: sent_msgs.append((text, kw))),
        ),
        bot=SimpleNamespace(
            send_message=AsyncMock(side_effect=lambda **kw: sent_msgs.append((kw.get("text", ""), kw)))
        ),
        answer=AsyncMock(),
    )

    await admin_handlers.handle_admin_pay_edit_promo20(cb, _FakeFSM(), session=db_session)
    assert fsm_state["current"] == PaymentReviewFSM.confirming_edit
    assert fsm_data["corrected_minutes"] == 30  # round(25 * 1.2)
    assert fsm_data["corrected_recap_credits"] == pytest.approx(8.4)  # round(7 * 1.2, 2)
    assert len(sent_msgs) == 1
    confirm_kb = sent_msgs[0][1]["reply_markup"]
    confirm_cbs = [btn.callback_data for row in confirm_kb.inline_keyboard for btn in row]
    assert "admin:pay_edit_confirm:PAY-DM-20" in confirm_cbs
    assert "admin:pay_edit_cancel:PAY-DM-20" in confirm_cbs


@pytest.mark.asyncio
async def test_b2_user_detail_expires_due_grants_before_header_projection(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", "990201")
    monkeypatch.setattr(settings, "free_daily_allowance_seconds", 0)

    admin = User(id=990201, first_name="Owner", account_category=AccountCategory.PURCHASED.value)
    target = User(
        id=88020101,
        first_name="ExpiredVip",
        account_category=AccountCategory.PURCHASED.value,
        recap_credits=5.0,
    )
    db_session.add_all([admin, target])
    await db_session.flush()
    db_session.add(MinuteBalance(user_id=target.id, purchased_seconds=600))
    grant = await create_wallet_grant(
        db_session,
        user_id=target.id,
        source_type="COMBO",
        voice_seconds=600,
        recap_seconds=300,
        package_code="combo_starter",
        validity_days=30,
    )
    # Force the grant past expiry while User/MinuteBalance still carry stale VIP state
    grant.expires_at = datetime.now(timezone.utc) - timedelta(hours=2)
    await db_session.commit()

    detail = await api_mod.get_admin_user_detail(target.id, admin=admin, db=db_session)
    assert detail["wallet_grants"][0]["status"] == "EXPIRED"
    assert detail["account_category"] == AccountCategory.FREE.value
    assert detail["balance_seconds"] == 0
    assert detail["recap_credits"] == 0.0
    assert detail["balance_breakdown"]["purchased_seconds"] == 0


@pytest.mark.asyncio
async def test_b2_balance_seconds_is_clone_spendable_and_users_sort_by_effective_balance(
    db_session, monkeypatch
):
    monkeypatch.setattr(ledger_mod, "get_current_daily_period", lambda: "2026-11-25")
    monkeypatch.setattr(settings, "free_daily_allowance_seconds", 180)
    monkeypatch.setattr(settings, "admin_ids_raw", "990202")

    admin = User(id=990202, first_name="Owner", account_category=AccountCategory.FREE.value)
    # User A: 0 purchased, 180 daily granted today + 120 welcome = 300 clone-spendable, plus 600 TTS-only referral_seconds
    user_a = User(id=88020201, first_name="BonusUser", account_category=AccountCategory.FREE.value)
    # User B: 120 purchased, 0 daily/bonus = 120 clone-spendable
    user_b = User(id=88020202, first_name="SmallPaid", account_category=AccountCategory.PURCHASED.value)
    db_session.add_all([admin, user_a, user_b])
    await db_session.flush()
    db_session.add_all(
        [
            MinuteBalance(
                user_id=user_a.id,
                purchased_seconds=0,
                daily_free_seconds=180,
                last_daily_grant_date="2026-11-25",
                welcome_seconds=120,
                referral_seconds=600,
            ),
            MinuteBalance(
                user_id=user_b.id,
                purchased_seconds=120,
                daily_free_seconds=0,
                referral_seconds=0,
            ),
        ]
    )
    await db_session.commit()

    res = await api_mod.list_admin_users(
        limit=10, offset=0, sort="balance_desc", admin=admin, db=db_session
    )
    rows = [r for r in res["users"] if r["id"] in (user_a.id, user_b.id)]
    assert [r["id"] for r in rows] == [user_a.id, user_b.id]

    row_a, row_b = rows[0], rows[1]
    assert row_a["balance_seconds"] == 300  # excludes 600s TTS-only referral_seconds
    assert row_a["referral_seconds"] == 600
    assert row_b["balance_seconds"] == 120
    assert "referral_seconds" not in row_b


@pytest.mark.asyncio
async def test_b3_overview_live_worker_status_and_analytics_yangon_coalesce_no_blobs(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "admin_ids_raw", "990301")

    admin = User(id=990301, first_name="Owner", account_category=AccountCategory.PURCHASED.value)
    buyer = User(id=88030101, first_name="Buyer", account_category=AccountCategory.PURCHASED.value)
    db_session.add_all([admin, buyer])
    await db_session.flush()
    recent_iso = (datetime.now(timezone.utc) - timedelta(seconds=25)).isoformat()
    hb = BotSetting(key="worker_laptop_heartbeat", value=recent_iso)

    # Slip created 3 days ago, approved at 2026-11-25 18:04 UTC == 2026-11-26 00:34 Asia/Yangon
    rev_utc = datetime(2026, 11, 25, 18, 4, 0, tzinfo=timezone.utc)
    slip = PaymentRequest(
        id="PAY-YGN-B3",
        user_id=buyer.id,
        package_code="combo_starter",
        minutes=20,
        recap_credits=30,
        amount_mmk=13500,
        status=PaymentStatus.APPROVED.value,
        created_at=rev_utc - timedelta(days=3),
        reviewed_at=rev_utc,
    )
    big_job = Job(
        id="rc_big_b3",
        user_id=buyer.id,
        job_type=JobType.RECAP.value,
        engine="recap",
        status=JobStatus.SUCCESS.value,
        input_text="X" * 4000,
        final_script="Y" * 20000,
        recap_config=json.dumps({"blob": "Z" * 10000}),
        created_at=rev_utc,
    )
    db_session.add_all([hb, slip, big_job])
    await db_session.commit()

    executed_sql: list[str] = []

    def _capture_sql(conn, cursor, statement, parameters, context, executemany):
        executed_sql.append(statement)

    sync_engine = db_session.bind.sync_engine
    event.listen(sync_engine, "before_cursor_execute", _capture_sql)
    try:
        ov = await api_mod.get_admin_overview(admin=admin, db=db_session)
        an = await api_mod.get_admin_analytics(days=30, admin=admin, db=db_session)
    finally:
        event.remove(sync_engine, "before_cursor_execute", _capture_sql)

    assert ov["worker_status"] != "ONLINE (VPS Server)"
    assert ov["worker"]["status_code"] == "ONLINE"
    assert ov["worker"]["is_online"] is True
    assert ov["worker"]["last_seen_ago"] != "Never"

    for sql in executed_sql:
        low = sql.lower()
        if "from jobs" in low:
            assert "final_script" not in low, f"Unexpected final_script select: {sql}"
            assert "recap_config" not in low, f"Unexpected recap_config select: {sql}"
            assert "jobs.input_text" not in low, f"Unexpected input_text select: {sql}"

    assert an["revenue"]["total_approved_mmk"] == 13500
    daily_dates = [d["date"] for d in an["revenue"]["daily"]]
    assert "2026-11-26" in daily_dates  # Yangon date for 2026-11-25 18:04 UTC


@pytest.mark.asyncio
async def test_b4_admin_queue_history_and_user_detail_defer_job_blobs(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", "990401")

    admin = User(id=990401, first_name="Owner", account_category=AccountCategory.PURCHASED.value)
    u = User(id=88040101, first_name="Maker", account_category=AccountCategory.FREE.value)
    db_session.add_all([admin, u])
    await db_session.flush()
    j_active = Job(
        id="vc_q_b4",
        user_id=u.id,
        job_type=JobType.VOICE_CLONE.value,
        engine="voxcpm",
        status=JobStatus.QUEUED.value,
        input_text="မင်္ဂလာပါ " * 80,
        final_script="SCRIPT_BLOB_" * 500,
        recap_config=json.dumps({"huge": "A" * 5000}),
    )
    j_done = Job(
        id="rc_h_b4",
        user_id=u.id,
        job_type=JobType.RECAP.value,
        engine="recap",
        status=JobStatus.SUCCESS.value,
        input_text="https://youtube.com/watch?v=abcdefghijk" + ("_pad" * 80),
        final_script="DONE_SCRIPT_" * 500,
        recap_config=json.dumps({"huge_done": "B" * 5000}),
    )
    db_session.add_all([j_active, j_done])
    await db_session.commit()

    executed_sql: list[str] = []

    def _capture_sql(conn, cursor, statement, parameters, context, executemany):
        executed_sql.append(statement)

    sync_engine = db_session.bind.sync_engine
    event.listen(sync_engine, "before_cursor_execute", _capture_sql)
    try:
        q_res = await api_mod.get_admin_queue(status="all", limit=50, admin=admin, db=db_session)
        h_res = await api_mod.get_admin_global_history(
            timeframe="24h", status="all", job_type="all", limit=50, user=admin, db=db_session
        )
        d_res = await api_mod.get_admin_user_detail(u.id, admin=admin, db=db_session)
    finally:
        event.remove(sync_engine, "before_cursor_execute", _capture_sql)

    assert q_res["counts"]["window_total"] >= 2
    assert h_res["total_count"] >= 2
    assert len(d_res["recent_jobs"]) == 2

    # All 3 admin job queries (queue, history, and user-detail recent_jobs) use ORDER BY;
    # exclude tts_service.tts_cap_consumed_seconds's un-ordered TTS quota helper query.
    job_selects = [
        s
        for s in executed_sql
        if "from jobs" in s.lower()
        and "order by" in s.lower()
        and s.lstrip().upper().startswith("SELECT")
    ]
    assert len(job_selects) == 3
    for sql in job_selects:
        low = sql.lower()
        assert "final_script" not in low, f"Job blob final_script leaked into query: {sql}"
        assert "recap_config" not in low, f"Job blob recap_config leaked into query: {sql}"
        # Raw jobs.input_text column must be deferred; only substr(jobs.input_text, ...) preview is permitted
        stripped = re.sub(r"substr\s*\(\s*jobs\.input_text[^)]*\)", "", low)
        assert "jobs.input_text" not in stripped, f"Un-deferred jobs.input_text leaked into query: {sql}"


@pytest.mark.asyncio
async def test_b5_audit_logs_for_retry_cancel_purge_local_bot_api_and_gai_ban_undo(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "admin_ids_raw", "990501")
    monkeypatch.setattr(settings, "bot_token", "123456:TEST_TOKEN")

    admin = User(id=990501, first_name="AdminBoss", username="boss", account_category=AccountCategory.PURCHASED.value)
    member = User(id=88050101, first_name="Member", account_category=AccountCategory.FREE.value)
    db_session.add_all([admin, member])
    await db_session.flush()
    j_fail = Job(
        id="vc_retry_b5",
        user_id=member.id,
        job_type=JobType.VOICE_CLONE.value,
        engine="voxcpm",
        status=JobStatus.FAILED.value,
        input_text="retry me",
    )
    j_run = Job(
        id="vc_cancel_b5",
        user_id=member.id,
        job_type=JobType.VOICE_CLONE.value,
        engine="voxcpm",
        status=JobStatus.QUEUED.value,
        input_text="cancel me",
    )
    j_purge = Job(
        id="vc_purge_b5",
        user_id=member.id,
        job_type=JobType.VOICE_CLONE.value,
        engine="voxcpm",
        status=JobStatus.FAILED.value,
        input_text="purge me",
    )
    strike = GroupAiModerationStrike(
        chat_id=-100555666,
        user_id=77788899,
        category="spam",
        message_id=42,
        action="mute_24h",
    )
    db_session.add_all([j_fail, j_run, j_purge, strike])
    await db_session.commit()
    await db_session.refresh(strike)

    # 1. Retry -> job.admin_retry
    await api_mod.retry_queued_job("vc_retry_b5", admin=admin, db=db_session)
    # 2. Cancel -> job.cancel (existing pin preserved)
    await api_mod.cancel_queued_job("vc_cancel_b5", admin=admin, db=db_session)
    # 3. Purge -> history.purge with mode + counts
    await api_mod.purge_admin_history(
        api_mod.AdminPurgeHistoryRequest(mode="single", job_id="vc_purge_b5"),
        user=admin,
        db=db_session,
    )
    # 4. Local Bot API activate -> ops.local_bot_api_activate
    monkeypatch.setattr(
        api_mod,
        "activate_local_bot_api",
        AsyncMock(return_value={"active": True, "mode": "local", "max_upload_mb": 2000}),
    )
    await api_mod.admin_activate_local_bot_api(
        api_mod._LocalBotApiActivateReq(url="http://127.0.0.1:8081"),
        admin=admin,
        db=db_session,
    )
    # 5. Bot Strike-3 gai_ban: -> group_mod.gai_ban + 1-tap undo button (gmod_undo:unban:)
    edited_calls = []
    fake_bot = SimpleNamespace(ban_chat_member=AsyncMock())
    cb = SimpleNamespace(
        data=f"gai_ban:{strike.id}:-100555666:77788899",
        from_user=SimpleNamespace(id=990501, username="boss"),
        bot=fake_bot,
        message=SimpleNamespace(
            html_text="🚨 Strike 3 alert",
            text="🚨 Strike 3 alert",
            edit_text=AsyncMock(side_effect=lambda txt, **kw: edited_calls.append((txt, kw))),
        ),
        answer=AsyncMock(),
    )
    await group_admin_handlers.handle_moderation_ban_callback(cb, bot=fake_bot, session=db_session)

    logs = (await db_session.execute(select(AuditLog))).scalars().all()
    by_action = {lg.action: lg for lg in logs}
    assert "job.admin_retry" in by_action
    assert "job.cancel" in by_action
    assert "history.purge" in by_action
    purge_meta = json.loads(by_action["history.purge"].details_json)
    assert purge_meta["mode"] == "single" and purge_meta["deleted_rows"] == 1 and "deleted_files" in purge_meta
    assert "ops.local_bot_api_activate" in by_action
    assert "group_mod.gai_ban" in by_action

    assert len(edited_calls) == 1
    undo_kb = edited_calls[0][1]["reply_markup"]
    undo_btns = [btn for row in undo_kb.inline_keyboard for btn in row]
    assert len(undo_btns) == 1
    assert undo_btns[0].text == "↩️ ပြန်ဖြည်မည်"
    assert undo_btns[0].callback_data == "gmod_undo:unban:-100555666:77788899"


@pytest.mark.asyncio
async def test_b6_b7_yangon_timestamps_and_orders_export_dm_delivery(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", "990601")
    monkeypatch.setattr(settings, "bot_token", "123456:TEST_TOKEN")

    sent_docs = []
    sent_dms = []
    fake_bot = SimpleNamespace(
        send_document=AsyncMock(side_effect=lambda **kw: sent_docs.append(kw)),
        send_message=AsyncMock(side_effect=lambda **kw: sent_dms.append(kw)),
        session=SimpleNamespace(close=AsyncMock()),
    )
    monkeypatch.setattr(api_mod, "create_telegram_bot", lambda token: fake_bot)

    admin = User(id=990601, first_name="Owner", account_category=AccountCategory.PURCHASED.value)
    buyer = User(id=88060101, first_name="Daw Mya", account_category=AccountCategory.PURCHASED.value)
    db_session.add_all([admin, buyer])
    await db_session.flush()

    # 2026-11-25 18:04 UTC -> 2026-11-26 00:34 Asia/Yangon
    ts_utc = datetime(2026, 11, 25, 18, 4, 0, tzinfo=timezone.utc)
    approved_order = PaymentRequest(
        id="PAY-CSV-1",
        user_id=buyer.id,
        package_code="combo_starter",
        minutes=20,
        recap_credits=30,
        amount_mmk=13500,
        status=PaymentStatus.APPROVED.value,
        created_at=ts_utc,
        reviewed_at=ts_utc,
    )
    pending_slip = PaymentRequest(
        id="PAY-PEND-ISO",
        user_id=buyer.id,
        package_code="combo_starter",
        minutes=20,
        recap_credits=30,
        amount_mmk=13500,
        status=PaymentStatus.PENDING.value,
        created_at=ts_utc,
    )
    db_session.add_all([approved_order, pending_slip])
    grant = await create_wallet_grant(
        db_session,
        user_id=buyer.id,
        source_type="ADMIN_GRANT",
        voice_seconds=600,
        package_code="promo",
        validity_days=10,
    )
    grant.expires_at = ts_utc
    await db_session.commit()

    # B6a: pending-slips returns raw ISO-8601 with UTC offset, never pre-formatted "... UTC"
    slips = await api_mod.get_pending_payment_slips(admin=admin, db=db_session)
    slip_row = next(s for s in slips if s["id"] == "PAY-PEND-ISO")
    assert "T" in slip_row["created_at"] and slip_row["created_at"].endswith("+00:00")
    assert "UTC" not in slip_row["created_at"]

    # B6b: grant-extend customer DM date is formatted in Asia/Yangon (+1 day from 2026-11-26 00:34 -> 2026-11-27 00:34)
    ext_res = await api_mod.extend_admin_wallet_grant(
        grant.id,
        api_mod.AdminExtendGrantRequest(add_days=1, reason="Yangon test", notify_user=True),
        admin=admin,
        db=db_session,
    )
    assert ext_res["success"] is True
    assert len(sent_dms) == 1
    assert "2026-11-27 00:34" in sent_dms[0]["text"]

    # B7: POST /api/admin/orders/export-dm sends Yangon CSV document to admin DM + writes AuditLog
    dm_res = await api_mod.export_admin_orders_csv_to_dm(
        api_mod.AdminOrdersExportDmRequest(status="APPROVED", search="", days=30),
        admin=admin,
        db=db_session,
    )
    assert dm_res["success"] is True and dm_res["count"] == 1
    assert len(sent_docs) == 1
    assert sent_docs[0]["chat_id"] == admin.id
    csv_text = sent_docs[0]["document"].data.decode("utf-8-sig")
    assert "PAY-CSV-1" in csv_text
    assert "2026-11-26 00:34" in csv_text  # Yangon timestamp, NOT 2026-11-25 18:04 UTC
    assert "18:04" not in csv_text

    export_log = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "orders.export_dm"))
    ).scalar_one_or_none()
    assert export_log is not None and export_log.admin_id == admin.id


def test_f3_universal_formatters_in_node_vm(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")

    fmt_start = APP_JS.index("function admFmtSecs(total)")
    fmt_end = APP_JS.index("function renderAdminBanZone", fmt_start)
    fmt_slice = APP_JS[fmt_start:fmt_end]

    ygn_start = APP_JS.index("const _ADM_MONTHS_SHORT =")
    ygn_end = APP_JS.index("function admStateLoading", ygn_start)
    ygn_slice = APP_JS[ygn_start:ygn_end]

    script = (
        "var window = {};\n"
        "var localStorage = { getItem: () => 'my', setItem: () => {}, removeItem: () => {} };\n"
        "var document = { getElementById: () => null };\n"
        "function escapeHtml(s) { return String(s ?? ''); }\n"
        + I18N_JS + "\n" + ygn_slice + "\n" + fmt_slice + "\n"
        "currentLang = 'my';\n"
        "const nowIso = new Date().toISOString();\n"
        "const out = {\n"
        "  ygnFixed: admFmtYangon('2025-11-25T15:04:00Z'),\n"
        "  ygnTodayMy: admFmtYangon(nowIso),\n"
        "  sec750: admFmtSecs(750),\n"
        "  sec300: admFmtSecs(300),\n"
        "  sec45: admFmtSecs(45),\n"
        "  sec0: admFmtSecs(0),\n"
        "  sec300Full: admFmtSecs(300, false),\n"
        "  sec0Full: admFmtSecs(0, false),\n"
        "  crInt: admFmtCredits(2.0),\n"
        "  crHalf: admFmtCredits(2.50),\n"
        "  crTrim: admFmtCredits(2.0167),\n"
        "  mmkMy: admFmtMmk(13500),\n"
        "};\n"
        "currentLang = 'en';\n"
        "out.mmkEn = admFmtMmk(13500);\n"
        "out.ygnTodayEn = admFmtYangon(nowIso);\n"
        "console.log(JSON.stringify(out));\n"
    )
    js_file = tmp_path / "_r1_fmt_test.js"
    js_file.write_text(script, encoding="utf-8")
    proc = subprocess.run([node, str(js_file)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    # 15:04 UTC + 06:30 Yangon = 21:34 (9:34 PM) on 25 Nov 2025
    assert "25 Nov 2025" in out["ygnFixed"] and "9:34 PM" in out["ygnFixed"]
    assert out["ygnTodayMy"].startswith("ယနေ့ · ")
    assert out["ygnTodayEn"].startswith("Today · ")

    assert out["sec750"] == "12m 30s"
    assert out["sec300"] == "5m"
    assert out["sec45"] == "45s"
    assert out["sec0"] == "0m"
    assert out["sec300Full"] == "5m 0s"
    assert out["sec0Full"] == "0m 0s"

    assert out["crInt"] == "2"
    assert out["crHalf"] == "2.5"
    assert out["crTrim"] == "2.02"

    assert out["mmkMy"] == "13,500 ကျပ်"
    assert out["mmkEn"] == "13,500 MMK"


def test_f1_to_f5_static_safety_confirmations_and_egress_guards():
    # F1: No dangling approveSlip( / rejectSlip( calls; receipt thumbnails use fetchAuthImageUrl; 4 promo chips present
    assert re.search(r"(?<![A-Za-z0-9_])approveSlip\s*\(", ADMIN_PRO_JS) is None
    assert re.search(r"(?<![A-Za-z0-9_])rejectSlip\s*\(", ADMIN_PRO_JS) is None
    assert "adminApproveSlip(" in ADMIN_PRO_JS and "adminRejectSlip(" in ADMIN_PRO_JS
    assert "fetchAuthImageUrl(" in ADMIN_PRO_JS
    assert '<img src="/api/receipts/' not in ADMIN_PRO_JS
    for chip in ("မူလအတိုင်း", "+10%", "+20% ပရိုမို", "+50%"):
        assert chip in ADMIN_PRO_JS, f"Missing Edit-Approve preset chip: {chip}"

    # B7/F1: Orders CSV button posts to /api/admin/orders/export-dm and never window.open()s /orders/export
    assert "/api/admin/orders/export-dm" in ADMIN_PRO_JS
    assert "window.open(" not in ADMIN_PRO_JS
    assert "/api/admin/orders/export" not in APP_JS

    # F2: Misleading fields & fake progress bars eliminated
    assert "balance_minutes" not in ADMIN_PRO_JS
    det_start = APP_JS.index("function renderAdminDetailExtras(u)")
    det_end = APP_JS.index("function renderAdminBanZone", det_start)
    det_body = APP_JS[det_start:det_end]
    assert "monthly_free_seconds" not in det_body
    assert "custom_voices_count" not in det_body

    q_start = APP_JS.index("function renderAdminQueueData(data)")
    q_end = APP_JS.index("window.loadAdminQueue = loadAdminQueue", q_start)
    q_body = APP_JS[q_start:q_end]
    assert "22%" not in q_body and "58%" not in q_body
    assert "|| '15'" not in q_body and '|| "15"' not in q_body

    # F3: Manual Key Health refresh passes true -> ?refresh=true
    assert 'onclick="loadAdminKeyHealth(true)"' in INDEX_HTML
    assert "?refresh=true" in APP_JS

    # F4: Segmented broadcast guard covers ALL audiences via /api/admin/broadcast/preview + askConfirm -> showStudioConfirm
    assert "/api/admin/broadcast/preview" in ADMIN_PRO_JS
    bc_idx = ADMIN_PRO_JS.index("window.adminSendBroadcast")
    bc_block = ADMIN_PRO_JS[bc_idx : bc_idx + 2200]
    assert "askConfirm(confirmMsg," in bc_block
    assert "loadBroadcastPreview()" in bc_block

    # F4: adjust-user default tier = မပြောင်းပါ (""), wrapped in showStudioConfirm with FREE-downgrade warning
    assert '<option value="" data-i18n="admin_tier_keep">' in INDEX_HTML
    adj_start = APP_JS.index("async function adminApplyAdjust()")
    adj_end = APP_JS.index("window.adminApplyAdjust", adj_start)
    adj_body = APP_JS[adj_start:adj_end]
    assert "showStudioConfirm(confirmAdjustMsg," in adj_body
    assert 'tier === "FREE"' in adj_body and "adm_confirm_adjust_free_warn" in adj_body
    assert "0m ဖြစ်သွားပါမည်" in I18N_JS

    # F4: Grant extend custom sheet includes default-checked notify_user checkbox
    assert "admp-ge-notify" in ADMIN_PRO_JS
    assert "သုံးစွဲသူထံ Telegram စာ ပို့ အသိပေးမည်" in ADMIN_PRO_JS
    assert "notify_user" in ADMIN_PRO_JS

    # F5: Zero /api/admin/fleet/status polls in frontend JS; adaptive queue polling; dirty-only settings w/ runtime keys
    assert "/api/admin/fleet/status" not in APP_JS
    assert "/api/admin/fleet/status" not in ADMIN_PRO_JS
    assert "scheduleAdminQueuePoll" in APP_JS
    assert "4000" in APP_JS and "15000" in APP_JS
    assert "visibilitychange" in APP_JS
    for rt_id in ("admp-rt-conc", "admp-rt-timeout", "admp-rt-profile", "admp-rt-fx"):
        assert rt_id in APP_JS, f"saveAdminSettings missing runtime input tracking for {rt_id}"
    assert "_isSettingsElDirty" in APP_JS and "_adminSettingsSavedSnapshot" in APP_JS
