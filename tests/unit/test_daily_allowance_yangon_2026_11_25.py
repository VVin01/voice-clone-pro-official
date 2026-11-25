"""Unit tests for Part C — Daily Free Allowance Asia/Yangon Calendar Day (2026-11-25).

Pins:
1. `get_current_daily_period()` and `allowance_day_of()` boundaries under frozen UTC clocks:
   - `2026-11-25T17:29:59Z` (`23:59:59` Yangon) -> `"2026-11-25"`
   - `2026-11-25T17:30:00Z` (`00:00:00` Yangon, Nov 26) -> `"2026-11-26"`
   - `2026-11-25T00:00:00Z` (`06:30:00` Yangon) -> `"2026-11-25"`
2. Grant idempotency across the old UTC midnight boundary (same Yangon day, different
   UTC days -> ONE 180 s grant) and across the new Yangon midnight boundary (`17:00Z` vs
   `18:00Z` -> TWO daily grants on `"2026-11-25"` and `"2026-11-26"`).
3. `MinuteBalance.effective_daily_free_seconds` flips at Yangon midnight (`17:30Z`),
   not at UTC midnight (`00:00Z`).
4. Refund attribution (`refund_job_seconds` and `settle_recap_voice_seconds`):
   a `DAILY_FREE` debit created at `2026-11-25T22:30:00Z` (`05:00` Yangon, Nov 26)
   is attributed to `"2026-11-26"`.
5. Reservation release across the UTC -> Yangon switch: a reservation stamped with a
   pre-switch UTC day string releases 100% cleanly after the switch with zero stranded
   reserved seconds.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.models.enums import JobType, LedgerDirection, LedgerSource
from app.models.schema import DailyFreeAllowanceGrant, MinuteBalance, MinuteLedgerEvent
from app.services import billing as billing_mod
from app.services import ledger as ledger_mod


def _freeze_ledger_utc(monkeypatch, utc_dt: datetime) -> None:
    """Freeze `datetime.now(...)` inside `app.services.ledger` to `utc_dt`."""

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return utc_dt.replace(tzinfo=None)
            return utc_dt.astimezone(tz)

    monkeypatch.setattr(ledger_mod, "datetime", _FrozenDateTime)
    monkeypatch.setattr(ledger_mod, "utc_now", lambda: utc_dt)


def test_yangon_day_helper_exact_boundaries(monkeypatch):
    # 2026-11-25T17:29:59Z == 2026-11-25 23:59:59+06:30 -> "2026-11-25"
    dt_before_midnight = datetime(2026, 11, 25, 17, 29, 59, tzinfo=timezone.utc)
    _freeze_ledger_utc(monkeypatch, dt_before_midnight)
    assert ledger_mod.get_current_daily_period() == "2026-11-25"
    assert ledger_mod.allowance_day_of(dt_before_midnight) == "2026-11-25"
    # Naive UTC timestamp (as returned by SQLite) converts identically
    assert ledger_mod.allowance_day_of(dt_before_midnight.replace(tzinfo=None)) == "2026-11-25"

    # 2026-11-25T17:30:00Z == 2026-11-26 00:00:00+06:30 -> "2026-11-26"
    dt_at_midnight = datetime(2026, 11, 25, 17, 30, 0, tzinfo=timezone.utc)
    _freeze_ledger_utc(monkeypatch, dt_at_midnight)
    assert ledger_mod.get_current_daily_period() == "2026-11-26"
    assert ledger_mod.allowance_day_of(dt_at_midnight) == "2026-11-26"
    assert ledger_mod.allowance_day_of(dt_at_midnight.replace(tzinfo=None)) == "2026-11-26"

    # 2026-11-25T00:00:00Z == 2026-11-25 06:30:00+06:30 -> "2026-11-25"
    dt_utc_midnight = datetime(2026, 11, 25, 0, 0, 0, tzinfo=timezone.utc)
    _freeze_ledger_utc(monkeypatch, dt_utc_midnight)
    assert ledger_mod.get_current_daily_period() == "2026-11-25"
    assert ledger_mod.allowance_day_of(dt_utc_midnight) == "2026-11-25"


@pytest.mark.asyncio
async def test_grant_idempotency_across_old_utc_midnight_and_rollover_at_yangon_midnight(
    db_session, monkeypatch
):
    # User A: grant at 2026-11-25T23:00Z (05:30 Yangon Nov 26) and second attempt at
    # 2026-11-26T01:00Z (07:30 Yangon Nov 26) -> same Yangon day across different UTC days -> ONE grant!
    user_a, _ = await ledger_mod.get_or_create_user(db_session, telegram_id=610001)
    await db_session.commit()

    _freeze_ledger_utc(monkeypatch, datetime(2026, 11, 25, 23, 0, 0, tzinfo=timezone.utc))
    granted_1, bal_1 = await ledger_mod.grant_daily_free_allowance(db_session, user_a.id)
    await db_session.commit()
    assert granted_1 is True
    assert bal_1.last_daily_grant_date == "2026-11-26"
    assert bal_1.daily_free_seconds == 180

    _freeze_ledger_utc(monkeypatch, datetime(2026, 11, 26, 1, 0, 0, tzinfo=timezone.utc))
    granted_2, bal_2 = await ledger_mod.grant_daily_free_allowance(db_session, user_a.id)
    await db_session.commit()
    assert granted_2 is False
    assert bal_2.daily_free_seconds == 180
    cnt_a = (
        await db_session.execute(
            select(func.count(DailyFreeAllowanceGrant.id)).where(
                DailyFreeAllowanceGrant.user_id == user_a.id
            )
        )
    ).scalar_one()
    assert cnt_a == 1

    # User B: grant at 17:00Z (23:30 Yangon Nov 25) and another at 18:00Z (00:30 Yangon Nov 26)
    # -> different Yangon days within the same UTC date -> TWO grants on their respective days!
    user_b, _ = await ledger_mod.get_or_create_user(db_session, telegram_id=610002)
    await db_session.commit()

    _freeze_ledger_utc(monkeypatch, datetime(2026, 11, 25, 17, 0, 0, tzinfo=timezone.utc))
    g_b1, bal_b1 = await ledger_mod.grant_daily_free_allowance(db_session, user_b.id)
    await db_session.commit()
    assert g_b1 is True
    assert bal_b1.last_daily_grant_date == "2026-11-25"

    # Spend 100 s before Yangon midnight so non-accumulation is also verified
    await ledger_mod.consume_seconds(db_session, user_b.id, 100, job_id="job_b_nov25")
    await db_session.commit()
    assert bal_b1.daily_free_seconds == 80

    _freeze_ledger_utc(monkeypatch, datetime(2026, 11, 25, 18, 0, 0, tzinfo=timezone.utc))
    g_b2, bal_b2 = await ledger_mod.grant_daily_free_allowance(db_session, user_b.id)
    await db_session.commit()
    assert g_b2 is True
    assert bal_b2.last_daily_grant_date == "2026-11-26"
    assert bal_b2.daily_free_seconds == 180  # Non-accumulating fresh 180 s

    days_b = list(
        (
            await db_session.execute(
                select(DailyFreeAllowanceGrant.day)
                .where(DailyFreeAllowanceGrant.user_id == user_b.id)
                .order_by(DailyFreeAllowanceGrant.day)
            )
        ).scalars().all()
    )
    assert days_b == ["2026-11-25", "2026-11-26"]


def test_effective_daily_free_seconds_flips_at_yangon_midnight_not_utc_midnight(monkeypatch):
    # Grant issued on Yangon day "2026-11-25"
    bal = MinuteBalance(
        user_id=610003,
        daily_free_seconds=180,
        last_daily_grant_date="2026-11-25",
    )
    # At 17:29:59Z (23:59:59 Yangon Nov 25): still active
    _freeze_ledger_utc(monkeypatch, datetime(2026, 11, 25, 17, 29, 59, tzinfo=timezone.utc))
    assert bal.effective_daily_free_seconds == 180

    # At 17:30:00Z (00:00:00 Yangon Nov 26): immediately flips to 0
    _freeze_ledger_utc(monkeypatch, datetime(2026, 11, 25, 17, 30, 0, tzinfo=timezone.utc))
    assert bal.effective_daily_free_seconds == 0

    # Grant issued at 18:00Z Nov 25 for Yangon day "2026-11-26" stays 180 across 00:00Z UTC midnight!
    bal_nov26 = MinuteBalance(
        user_id=610004,
        daily_free_seconds=180,
        last_daily_grant_date="2026-11-26",
    )
    _freeze_ledger_utc(monkeypatch, datetime(2026, 11, 25, 23, 59, 59, tzinfo=timezone.utc))
    assert bal_nov26.effective_daily_free_seconds == 180
    _freeze_ledger_utc(monkeypatch, datetime(2026, 11, 26, 0, 0, 1, tzinfo=timezone.utc))
    assert bal_nov26.effective_daily_free_seconds == 180


@pytest.mark.asyncio
async def test_refund_and_recap_settle_attribute_early_morning_yangon_debits_correctly(
    db_session, monkeypatch
):
    """A DAILY_FREE debit created at 2026-11-25T22:30Z (= 05:00 Yangon Nov 26) belongs
    to Yangon day '2026-11-26' even when refunded after 00:00Z on 2026-11-26."""
    user, _ = await ledger_mod.get_or_create_user(db_session, telegram_id=610005)
    await db_session.commit()

    # Grant and debit at 2026-11-25T22:30Z (05:00 Yangon Nov 26; UTC date is still Nov 25!)
    t_debit = datetime(2026, 11, 25, 22, 30, 0, tzinfo=timezone.utc)
    _freeze_ledger_utc(monkeypatch, t_debit)
    await ledger_mod.grant_daily_free_allowance(db_session, user.id)
    await ledger_mod.consume_seconds(db_session, user.id, 120, job_id="job_early_yangon")
    # Stamp exact UTC created_at (naive and aware coverage) on the debit event
    debit_ev = (
        await db_session.execute(
            select(MinuteLedgerEvent).where(
                MinuteLedgerEvent.reference_id == "job_early_yangon",
                MinuteLedgerEvent.direction == LedgerDirection.DEBIT.value,
            )
        )
    ).scalar_one()
    debit_ev.created_at = t_debit
    await db_session.commit()

    # Refund at 2026-11-26T02:00Z (08:30 Yangon Nov 26; UTC date changed to Nov 26, Yangon day still Nov 26)
    _freeze_ledger_utc(monkeypatch, datetime(2026, 11, 26, 2, 0, 0, tzinfo=timezone.utc))
    bal_after = await ledger_mod.refund_job_seconds(db_session, user.id, "job_early_yangon")
    await db_session.commit()
    assert bal_after.daily_free_seconds == 180
    assert bal_after.consumed_seconds == 0

    # Same check for settle_recap_voice_seconds partial refund
    _freeze_ledger_utc(monkeypatch, t_debit)
    await ledger_mod.consume_seconds(db_session, user.id, 100, job_id="rc_early_yangon")
    rc_debit = (
        await db_session.execute(
            select(MinuteLedgerEvent).where(
                MinuteLedgerEvent.reference_id == "rc_early_yangon",
                MinuteLedgerEvent.direction == LedgerDirection.DEBIT.value,
            )
        )
    ).scalar_one()
    rc_debit.created_at = t_debit.replace(tzinfo=None)  # SQLite naive UTC shape
    await db_session.commit()

    _freeze_ledger_utc(monkeypatch, datetime(2026, 11, 26, 3, 0, 0, tzinfo=timezone.utc))
    summary = await ledger_mod.settle_recap_voice_seconds(
        db_session, user.id, "rc_early_yangon", actual_seconds=40
    )
    await db_session.commit()
    assert summary["refunded_seconds"] == 60
    bal_final = await ledger_mod.get_minute_balance(db_session, user.id)
    assert bal_final.daily_free_seconds == 140


@pytest.mark.asyncio
async def test_pre_switch_utc_reservation_releases_cleanly_after_yangon_switch(
    db_session, monkeypatch
):
    """A reservation stamped with a pre-switch UTC day string releases 100% after the switch
    (both before and after the new Yangon day's grant) with zero stranded reserved seconds."""
    user, _ = await ledger_mod.get_or_create_user(db_session, telegram_id=610006)
    bal = await ledger_mod.get_minute_balance(db_session, user.id)
    bal.welcome_seconds = 60  # Mix daily + persistent bucket to verify both release cleanly
    await db_session.commit()

    # Simulate pre-switch state on UTC day "2026-11-25"
    _freeze_ledger_utc(monkeypatch, datetime(2026, 11, 25, 16, 0, 0, tzinfo=timezone.utc))
    await ledger_mod.grant_daily_free_allowance(db_session, user.id)
    res = await billing_mod.reserve_for_job(
        db_session,
        user_id=user.id,
        job_id="job_preswitch_1",
        job_type=JobType.VOICE_CLONE.value,
        billing_mode=billing_mod.BILLING_MODE_VOICE_ONLY,
        reserve_recap_seconds=0,
        reserve_voice_seconds=210,  # 180s daily ("2026-11-25") + 30s welcome
    )
    await db_session.commit()
    assert res.reserved_voice_daily == 180
    assert res.reserved_voice_daily_day == "2026-11-25"
    assert res.reserved_voice_welcome == 30

    # Deploy switch + Yangon midnight rolls to "2026-11-26" (at 18:00Z Nov 25 = 00:30 Yangon Nov 26)
    _freeze_ledger_utc(monkeypatch, datetime(2026, 11, 25, 18, 0, 0, tzinfo=timezone.utc))
    assert ledger_mod.get_current_daily_period() == "2026-11-26"

    # Case A: Release BEFORE user receives the Nov 26 grant -> 0 reserved_seconds, welcome intact
    rel = await billing_mod.release_job_reservation(db_session, user.id, "job_preswitch_1")
    await db_session.commit()
    assert rel["released"] is True
    assert rel["released_voice_seconds"] == 210

    bal_after_a = await ledger_mod.get_minute_balance(db_session, user.id)
    assert bal_after_a.reserved_daily_free_seconds == 0
    assert bal_after_a.reserved_welcome_seconds == 0
    assert bal_after_a.reserved_seconds == 0
    assert bal_after_a.welcome_seconds == 60

    # Case B: Pre-switch reservation released AFTER user already got the new Yangon day's grant
    _freeze_ledger_utc(monkeypatch, datetime(2026, 11, 25, 16, 30, 0, tzinfo=timezone.utc))
    user2, _ = await ledger_mod.get_or_create_user(db_session, telegram_id=610007)
    bal2 = await ledger_mod.get_minute_balance(db_session, user2.id)
    bal2.welcome_seconds = 50
    await ledger_mod.grant_daily_free_allowance(db_session, user2.id)
    await billing_mod.reserve_for_job(
        db_session,
        user_id=user2.id,
        job_id="job_preswitch_2",
        job_type=JobType.VOICE_CLONE.value,
        billing_mode=billing_mod.BILLING_MODE_VOICE_ONLY,
        reserve_recap_seconds=0,
        reserve_voice_seconds=200,  # 180s daily ("2026-11-25") + 20s welcome
    )
    await db_session.commit()

    # Roll to Nov 26 Yangon, user opens app (gets Nov 26 grant) and starts a new 40s reservation
    _freeze_ledger_utc(monkeypatch, datetime(2026, 11, 25, 18, 15, 0, tzinfo=timezone.utc))
    await ledger_mod.grant_daily_free_allowance(db_session, user2.id)
    await billing_mod.reserve_for_job(
        db_session,
        user_id=user2.id,
        job_id="job_newday_active",
        job_type=JobType.VOICE_CLONE.value,
        billing_mode=billing_mod.BILLING_MODE_VOICE_ONLY,
        reserve_recap_seconds=0,
        reserve_voice_seconds=40,
    )
    await db_session.commit()

    # Releasing the old pre-switch job releases its 20s welcome hold without clobbering today's 40s daily hold
    await billing_mod.release_job_reservation(db_session, user2.id, "job_preswitch_2")
    await db_session.commit()
    bal_after_b = await ledger_mod.get_minute_balance(db_session, user2.id)
    assert bal_after_b.reserved_welcome_seconds == 0
    assert bal_after_b.reserved_daily_free_seconds == 40
    assert bal_after_b.clone_spendable_seconds == (180 + 50) - 40
