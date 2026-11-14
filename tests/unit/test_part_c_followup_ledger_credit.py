"""Part C follow-up tests:
1. RecapCreditLedgerEvent completeness (signup, payment approval, opening backfill).
2. MinuteBalance.consumed_seconds NET of refunds across refund_job_seconds,
   voice-job cancels, and /menu + Admin user detail views.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db import session as session_mod
from app.db.base import Base
from app.handlers.menu import (
    render_history_ledger_content,
    render_history_overview_content,
)
from app.models.enums import (
    AccountCategory,
    JobStatus,
    JobType,
    LedgerDirection,
    LedgerSource,
    PaymentStatus,
)
from app.models.schema import (
    Job,
    MinuteBalance,
    MinuteLedgerEvent,
    PaymentRequest,
    RecapCreditLedgerEvent,
    User,
)
from app.scripts.backfill_recap_credit_ledger import run_backfill
from app.services.ledger import (
    DEFAULT_SIGNUP_RECAP_CREDITS,
    backfill_recap_credit_opening_balances,
    consume_seconds,
    credit_purchased_minutes,
    draw_tts_referral_seconds,
    get_minute_balance,
    get_or_create_user,
    grant_monthly_free_allowance,
    refund_job_seconds,
    refund_tts_referral_seconds,
    settle_recap_voice_seconds,
)
from app.services.payment_review import apply_edited_approval, claim_payment_review
from app.services.payments import approve_payment_request, create_payment_request
from app.webapp import api as api_mod


async def _open_store(tmp_path, monkeypatch, db_name: str):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/{db_name}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", maker)
    monkeypatch.setattr(api_mod, "AsyncSessionLocal", maker)
    return engine, maker


async def _recap_ledger_sum(session: AsyncSession, user_id: int) -> int:
    rows = (
        await session.execute(
            select(RecapCreditLedgerEvent).where(
                RecapCreditLedgerEvent.user_id == user_id
            )
        )
    ).scalars().all()
    return sum(
        int(r.credits or 0)
        if r.direction == LedgerDirection.CREDIT.value
        else -int(r.credits or 0)
        for r in rows
    )


@pytest.mark.asyncio
async def test_signup_grant_writes_single_recap_credit_row(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch, "signup_grant.db")
    uid = 950001
    async with maker() as s:
        user, is_new = await get_or_create_user(s, uid, first_name="SignupUser")
        await s.commit()
        assert is_new is True
        assert user.recap_credits == DEFAULT_SIGNUP_RECAP_CREDITS

        events = (
            await s.execute(
                select(RecapCreditLedgerEvent).where(
                    RecapCreditLedgerEvent.user_id == uid
                )
            )
        ).scalars().all()
        assert len(events) == 1
        assert events[0].direction == LedgerDirection.CREDIT.value
        assert events[0].credits == DEFAULT_SIGNUP_RECAP_CREDITS
        assert events[0].reference_id == f"SIGNUP:{uid}"
        assert "signup" in (events[0].note or "").lower()
        assert int(user.recap_credits) == await _recap_ledger_sum(s, uid)

    # Re-calling get_or_create_user for the existing user writes no second row
    async with maker() as s:
        user2, is_new2 = await get_or_create_user(s, uid, first_name="SignupUser")
        await s.commit()
        assert is_new2 is False
        assert user2.recap_credits == DEFAULT_SIGNUP_RECAP_CREDITS
        events2 = (
            await s.execute(
                select(RecapCreditLedgerEvent).where(
                    RecapCreditLedgerEvent.user_id == uid
                )
            )
        ).scalars().all()
        assert len(events2) == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_miniapp_get_current_user_signup_writes_credit_row(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch, "miniapp_signup.db")
    uid = 950002
    monkeypatch.setattr(
        api_mod,
        "parse_and_verify_init_data",
        lambda _raw: {
            "id": uid,
            "first_name": "WebUser",
            "username": "webuser",
            "language_code": "en",
        },
    )

    async with maker() as s:
        user = await api_mod.get_current_user("signed-init", s)
        assert user.id == uid
        assert user.recap_credits == DEFAULT_SIGNUP_RECAP_CREDITS
        events = (
            await s.execute(
                select(RecapCreditLedgerEvent).where(
                    RecapCreditLedgerEvent.user_id == uid
                )
            )
        ).scalars().all()
        assert len(events) == 1
        assert events[0].direction == LedgerDirection.CREDIT.value
        assert events[0].credits == DEFAULT_SIGNUP_RECAP_CREDITS
        assert events[0].reference_id == f"SIGNUP:{uid}"
        assert int(user.recap_credits) == await _recap_ledger_sum(s, uid)

    # Second Mini App hit must not write a duplicate signup CREDIT
    async with maker() as s:
        _ = await api_mod.get_current_user("signed-init", s)
        events = (
            await s.execute(
                select(RecapCreditLedgerEvent).where(
                    RecapCreditLedgerEvent.user_id == uid
                )
            )
        ).scalars().all()
        assert len(events) == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_payment_approval_writes_single_recap_credit_row_and_blocks_double_approve(
    tmp_path, monkeypatch
):
    engine, maker = await _open_store(tmp_path, monkeypatch, "payment_approval.db")
    uid = 950010
    admin_id = 111

    async with maker() as s:
        await get_or_create_user(s, uid, first_name="Buyer")
        pay_recap = await create_payment_request(
            s, uid, package_code="recap_starter", receipt_file_id="rcpt_recap_only"
        )
        pay_combo = await create_payment_request(
            s, uid, package_code="combined_starter", receipt_file_id="rcpt_combo"
        )
        pay_voice = await create_payment_request(
            s, uid, package_code="MIN_10", receipt_file_id="rcpt_voice_only"
        )
        pay_edited = await create_payment_request(
            s, uid, package_code="recap_starter", receipt_file_id="rcpt_edited"
        )
        await s.commit()

    # 1) Recap-only approval: +5 credits, 1 CREDIT row referencing pay_recap.id + plan name
    async with maker() as s:
        ok, p1, code = await approve_payment_request(s, pay_recap.id, admin_id=admin_id)
        await s.commit()
        assert ok is True and code == "APPROVED" and p1 is not None

        # Double-approve attempt must be rejected and write zero extra rows
        ok2, _p2, code2 = await approve_payment_request(s, pay_recap.id, admin_id=admin_id)
        await s.commit()
        assert ok2 is False and code2 == f"ALREADY_{PaymentStatus.APPROVED.value}"

        rows_p1 = (
            await s.execute(
                select(RecapCreditLedgerEvent).where(
                    RecapCreditLedgerEvent.user_id == uid,
                    RecapCreditLedgerEvent.reference_id == pay_recap.id,
                )
            )
        ).scalars().all()
        assert len(rows_p1) == 1
        assert rows_p1[0].direction == LedgerDirection.CREDIT.value
        assert rows_p1[0].credits == 5
        assert "recap_starter" in (rows_p1[0].note or "")

    # 2) Combined plan approval: credits both minutes and 5 recap credits in same txn
    async with maker() as s:
        ok, _p, code = await approve_payment_request(s, pay_combo.id, admin_id=admin_id)
        await s.commit()
        assert ok is True and code == "APPROVED"
        rows_combo = (
            await s.execute(
                select(RecapCreditLedgerEvent).where(
                    RecapCreditLedgerEvent.reference_id == pay_combo.id
                )
            )
        ).scalars().all()
        assert len(rows_combo) == 1
        assert rows_combo[0].credits == 5
        assert "combined_starter" in (rows_combo[0].note or "")

    # 3) Voice-only plan writes 0 recap credit rows
    async with maker() as s:
        ok, _p, code = await approve_payment_request(s, pay_voice.id, admin_id=admin_id)
        await s.commit()
        assert ok is True and code == "APPROVED"
        rows_voice = (
            await s.execute(
                select(RecapCreditLedgerEvent).where(
                    RecapCreditLedgerEvent.reference_id == pay_voice.id
                )
            )
        ).scalars().all()
        assert rows_voice == []

    # 4) Edit-and-approve with corrected_recap_credits=8 writes exactly 8 credits
    async with maker() as s:
        claimed, _p, _c = await claim_payment_review(s, pay_edited.id, admin_id=admin_id)
        assert claimed is True
        ok, _p, code = await apply_edited_approval(
            s,
            pay_edited.id,
            admin_id=admin_id,
            corrected_minutes=1,
            corrected_recap_credits=8,
        )
        await s.commit()
        assert ok is True and code == "APPROVED"
        rows_ed = (
            await s.execute(
                select(RecapCreditLedgerEvent).where(
                    RecapCreditLedgerEvent.reference_id == pay_edited.id
                )
            )
        ).scalars().all()
        assert len(rows_ed) == 1
        assert rows_ed[0].credits == 8

        user = await s.get(User, uid)
        # 3 (signup) + 5 + 5 + 0 + 8 = 21
        assert user.recap_credits == 21
        assert int(user.recap_credits) == await _recap_ledger_sum(s, uid)

    await engine.dispose()


@pytest.mark.asyncio
async def test_backfill_opening_balances_derives_every_user_and_is_idempotent(
    tmp_path, monkeypatch, capsys
):
    engine, maker = await _open_store(tmp_path, monkeypatch, "backfill.db")

    async with maker() as s:
        # Legacy user A: 3 credits, 0 ledger rows -> shortfall 3
        s.add(User(id=951001, recap_credits=3, account_category=AccountCategory.FREE.value))
        # Legacy user B: 7 credits, 1 DEBIT of 1 -> shortfall 8 (e.g. 3 signup + 5 buy - 1 spend)
        s.add(User(id=951002, recap_credits=7, account_category=AccountCategory.PURCHASED.value))
        s.add(
            RecapCreditLedgerEvent(
                user_id=951002,
                source="RECAP",
                direction=LedgerDirection.DEBIT.value,
                credits=1,
                reference_id="rc_legacy_b",
                note="Legacy submit debit",
            )
        )
        # Legacy user C: 0 credits, 1 DEBIT of 3 -> shortfall 3
        s.add(User(id=951003, recap_credits=0, account_category=AccountCategory.FREE.value))
        s.add(
            RecapCreditLedgerEvent(
                user_id=951003,
                source="RECAP",
                direction=LedgerDirection.DEBIT.value,
                credits=3,
                reference_id="rc_legacy_c",
                note="Legacy submit debit",
            )
        )
        # Post-fix user D: created via get_or_create_user (already balanced: 3 == 3)
        await get_or_create_user(s, 951004)
        # Legacy user E: 0 credits, 0 ledger rows (already balanced: 0 == 0)
        s.add(User(id=951005, recap_credits=0, account_category=AccountCategory.FREE.value))
        await s.commit()

    # 1) Dry-run prints counts and writes NOTHING
    dry_summary = await run_backfill(maker, dry_run=True)
    captured_dry = capsys.readouterr().out
    assert "dry-run counts:" in captured_dry
    assert "needing_backfill=3" in captured_dry
    assert "total_shortfall_credits=14" in captured_dry
    assert dry_summary["users_scanned"] == 5
    assert dry_summary["users_already_balanced"] == 2
    assert dry_summary["users_needing_backfill"] == 3
    assert dry_summary["total_shortfall_credits"] == 14
    assert dry_summary["users_backfilled"] == 0
    assert dry_summary["credits_written"] == 0

    async with maker() as s:
        assert (
            await s.execute(
                select(RecapCreditLedgerEvent).where(
                    RecapCreditLedgerEvent.note == "opening balance backfill 2026-11"
                )
            )
        ).scalars().all() == []

    # 2) First apply run writes ONE opening CREDIT row per shortfall user
    apply_summary = await run_backfill(maker, dry_run=False)
    captured_apply = capsys.readouterr().out
    assert "dry-run counts:" in captured_apply
    assert "applied: users_backfilled=3 credits_written=14" in captured_apply
    assert apply_summary["users_backfilled"] == 3
    assert apply_summary["credits_written"] == 14

    async with maker() as s:
        backfill_rows = (
            await s.execute(
                select(RecapCreditLedgerEvent)
                .where(RecapCreditLedgerEvent.note == "opening balance backfill 2026-11")
                .order_by(RecapCreditLedgerEvent.user_id)
            )
        ).scalars().all()
        assert [(r.user_id, r.direction, r.credits) for r in backfill_rows] == [
            (951001, "CREDIT", 3),
            (951002, "CREDIT", 8),
            (951003, "CREDIT", 3),
        ]

        # Acceptance gate: for EVERY user, recap_credits == sum(CREDIT) - sum(DEBIT)
        all_users = (await s.execute(select(User).order_by(User.id))).scalars().all()
        for u in all_users:
            assert int(u.recap_credits) == await _recap_ledger_sum(s, u.id)

    # 3) Second run must write NOTHING (idempotent)
    second_summary = await run_backfill(maker, dry_run=False)
    assert second_summary["users_needing_backfill"] == 0
    assert second_summary["users_backfilled"] == 0
    assert second_summary["credits_written"] == 0

    async with maker() as s:
        backfill_rows_after = (
            await s.execute(
                select(RecapCreditLedgerEvent).where(
                    RecapCreditLedgerEvent.note == "opening balance backfill 2026-11"
                )
            )
        ).scalars().all()
        assert len(backfill_rows_after) == 3
        all_users = (await s.execute(select(User).order_by(User.id))).scalars().all()
        for u in all_users:
            assert int(u.recap_credits) == await _recap_ledger_sum(s, u.id)

    await engine.dispose()


@pytest.mark.asyncio
async def test_consumed_seconds_net_across_refund_partial_replay_and_floor(
    tmp_path, monkeypatch
):
    engine, maker = await _open_store(tmp_path, monkeypatch, "consumed_net.db")
    uid = 952001

    async with maker() as s:
        await get_or_create_user(s, uid, first_name="NetUser", language_code="en")
        await credit_purchased_minutes(s, uid, minutes=10, payment_id="PAY-NET-1")

        # Establish a non-zero pre-consume baseline (45s actually settled)
        await consume_seconds(s, uid, 45, job_id="job_settled")
        bal = await get_minute_balance(s, uid)
        assert bal.consumed_seconds == 45

        # 1) Consume -> fail -> refund restores consumed_seconds to pre-consume value (45)
        await consume_seconds(s, uid, 120, job_id="job_fail_1")
        bal = await get_minute_balance(s, uid)
        assert bal.consumed_seconds == 165

        await refund_job_seconds(s, uid, "job_fail_1")
        bal = await get_minute_balance(s, uid)
        assert bal.consumed_seconds == 45
        assert bal.purchased_seconds == 600 - 45

        # 2) Replayed refund refunds 0 and decrements 0
        await refund_job_seconds(s, uid, "job_fail_1")
        bal = await get_minute_balance(s, uid)
        assert bal.consumed_seconds == 45

        # 3) Partial refund via Option B settle under-run then failure refund
        await consume_seconds(s, uid, 100, job_id="rc_partial_1")
        bal = await get_minute_balance(s, uid)
        assert bal.consumed_seconds == 145
        await settle_recap_voice_seconds(s, uid, "rc_partial_1", actual_seconds=60)
        bal = await get_minute_balance(s, uid)
        assert bal.consumed_seconds == 105  # 45 + 60 kept
        await refund_job_seconds(s, uid, "rc_partial_1")
        bal = await get_minute_balance(s, uid)
        assert bal.consumed_seconds == 45  # remaining 60 refunded too

        # 4) Partial refund when a prior-month MONTHLY_FREE debit is non-refundable:
        # consume 180s monthly_free + 20s purchased = 200s
        await grant_monthly_free_allowance(s, uid)
        await consume_seconds(s, uid, 200, job_id="job_prior_month")
        bal = await get_minute_balance(s, uid)
        assert bal.consumed_seconds == 245
        # Age the MONTHLY_FREE debit row to the previous month
        mf_debit = (
            await s.execute(
                select(MinuteLedgerEvent).where(
                    MinuteLedgerEvent.user_id == uid,
                    MinuteLedgerEvent.reference_id == "job_prior_month",
                    MinuteLedgerEvent.source == LedgerSource.MONTHLY_FREE.value,
                )
            )
        ).scalar_one()
        mf_debit.created_at = datetime.now(timezone.utc) - timedelta(days=40)
        await s.flush()

        # Only the 20s PURCHASE debit is refunded in this call -> decrements by 20, not 200
        await refund_job_seconds(s, uid, "job_prior_month")
        bal = await get_minute_balance(s, uid)
        assert bal.consumed_seconds == 225

        # 5) Floor at 0: consumed_seconds never goes negative even if counter was smaller
        bal.consumed_seconds = 10
        await s.flush()
        await consume_seconds(s, uid, 50, job_id="job_floor")
        bal.consumed_seconds = 10  # simulate legacy/lower counter before refund
        await s.flush()
        await refund_job_seconds(s, uid, "job_floor")
        bal = await get_minute_balance(s, uid)
        assert bal.consumed_seconds == 0

        # 6) TTS referral draw & refund do NOT participate in consumed_seconds
        bal.referral_seconds = 90
        await s.flush()
        drawn = await draw_tts_referral_seconds(s, uid, "tts_net_1", 60)
        assert drawn == 60
        assert bal.consumed_seconds == 0
        returned = await refund_tts_referral_seconds(s, uid, "tts_net_1")
        assert returned == 60
        assert bal.consumed_seconds == 0
        await s.commit()

    await engine.dispose()


@pytest.mark.asyncio
async def test_voice_cancel_updates_consumed_seconds_in_menu_and_admin_detail(
    tmp_path, monkeypatch
):
    engine, maker = await _open_store(tmp_path, monkeypatch, "cancel_net_ui.db")
    uid = 952002
    admin_id = 1
    monkeypatch.setattr(settings, "admin_ids_raw", str(admin_id))

    async with maker() as s:
        user, _ = await get_or_create_user(s, uid, first_name="CancelUser", language_code="en")
        await credit_purchased_minutes(s, uid, minutes=5, payment_id="PAY-NET-2")
        await consume_seconds(s, uid, 120, job_id="vc_cancel_net")
        s.add(
            Job(
                id="vc_cancel_net",
                user_id=uid,
                chat_id=uid,
                status=JobStatus.QUEUED.value,
                job_type=JobType.VOICE_CLONE.value,
                engine="voxcpm",
                input_text="cancel test",
                seconds_estimated=120,
            )
        )
        await s.commit()

        # Before cancel: consumed_seconds == 120 (2.0m) across admin detail + menu
        detail_before = await api_mod.get_admin_user_detail(
            uid, admin=SimpleNamespace(id=admin_id), db=s
        )
        assert detail_before["balance_breakdown"]["consumed_seconds"] == 120
        overview_before, _ = await render_history_overview_content(s, uid)
        assert "2.0" in overview_before

        # User voice cancel routes through refund_job_seconds
        res = await api_mod.cancel_voice_job("vc_cancel_net", user=user, db=s)
        assert res["success"] is True and res["status"] == "CANCELLED"

        bal_after = await get_minute_balance(s, uid)
        assert bal_after.consumed_seconds == 0
        detail_after = await api_mod.get_admin_user_detail(
            uid, admin=SimpleNamespace(id=admin_id), db=s
        )
        assert detail_after["balance_breakdown"]["consumed_seconds"] == 0
        overview_after, _ = await render_history_overview_content(s, uid)
        ledger_after, _ = await render_history_ledger_content(s, uid)
        assert "0.0" in overview_after
        assert "0.0" in ledger_after

    await engine.dispose()
