"""Batch AJ — adversarial weakness-hunt fixes: MONEY cluster.

Findings covered: H1 (slip replay double-credit), H6 (admin retry of a
refunded job must re-charge — voice ledger netting), H8 (recap refund
claim must be an atomic CAS), H11 (segmented top-up / refund amounts
must come from the durable ledger, not the crash-losable blob), M3
(payment review claim enforced in the service, not just the bot UI),
M4 (edit-and-approve: service-level bounds + recap-credits correction),
L3 (cross-month MONTHLY_FREE refund leak), L22 (allowance grant must
serialise on the user row).

Every test names its finding id. Written against the UNFIXED tree
first: each fails there for the reason stated in its docstring.
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import session as session_mod
from app.db.base import Base
from app.models.enums import LedgerDirection, LedgerSource, PaymentStatus
from app.models.schema import (
    Job,
    MinuteLedgerEvent,
    PaymentRequest,
    RecapCreditLedgerEvent,
    User,
    utc_now,
)
from app.services.ledger import (
    consume_seconds,
    get_minute_balance,
    get_or_create_user,
    refund_job_seconds,
)
from app.services.payment_review import apply_edited_approval, claim_payment_review
from app.services.payments import (
    approve_payment_request,
    create_payment_request,
    reject_payment_request,
)


# ── helpers ──────────────────────────────────────────────────────────


async def _open_store(tmp_path, monkeypatch, name="batch_aj_money.db"):
    """File-backed DB + the API helpers' own session factory pointed
    at it (the recap credit helpers open AsyncSessionLocal themselves)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/{name}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", maker)
    return engine, maker


def _recap_blob(**over):
    state = {
        "config": {"url": "https://example.com/m", "title": "AJ Movie"},
        "credit_deducted": True,
        "credits_charged": 2,
        "credit_refunded": False,
        "credits_refunded": 0,
        "is_vip": False,
        "phase": "full",
    }
    state.update(over)
    return json.dumps(state)


async def _make_job(maker, job_id, user_id, blob):
    async with maker() as s:
        s.add(Job(
            id=job_id, user_id=user_id, chat_id=user_id, status="FAILED",
            job_type="RECAP", engine="recap", recap_config=blob,
        ))
        await s.commit()


# ── H1: one bank slip = one credit, globally ─────────────────────────


@pytest.mark.asyncio
async def test_batch_aj_h1_fingerprint_stored_on_request(db_session):
    """The created request persists its slip fingerprint (tg:<file_id>)
    so later submissions can be matched even after the slip file is
    deleted at approval. Unfixed: no such attribute/value."""
    user, _ = await get_or_create_user(db_session, telegram_id=8101)
    payment = await create_payment_request(
        session=db_session, user_id=user.id, package_code="MIN_10",
        receipt_file_id="slip-file-001",
    )
    assert getattr(payment, "slip_fingerprint", None) == "tg:slip-file-001"


@pytest.mark.asyncio
async def test_batch_aj_h1_replay_after_approval_blocked_same_user(db_session):
    from app.services.payments import SlipAlreadyUsedError
    """Unfixed hole: the duplicate guard only matched PENDING rows, and
    approval deletes the slip — so re-sending the SAME slip after
    approval minted a second credit for one transfer."""
    user, _ = await get_or_create_user(db_session, telegram_id=8102)
    first = await create_payment_request(
        session=db_session, user_id=user.id, package_code="MIN_10",
        receipt_file_id="slip-file-002",
    )
    ok, _, _ = await approve_payment_request(db_session, first.id, admin_id=999)
    assert ok
    with pytest.raises(SlipAlreadyUsedError):
        await create_payment_request(
            session=db_session, user_id=user.id, package_code="MIN_10",
            receipt_file_id="slip-file-002",
        )


@pytest.mark.asyncio
async def test_batch_aj_h1_replay_after_approval_blocked_other_user(db_session):
    from app.services.payments import SlipAlreadyUsedError
    """The same slip forwarded to a second account must not credit
    that account either — the guard is global, not per-user."""
    user_a, _ = await get_or_create_user(db_session, telegram_id=8103)
    user_b, _ = await get_or_create_user(db_session, telegram_id=8104)
    first = await create_payment_request(
        session=db_session, user_id=user_a.id, package_code="MIN_10",
        receipt_file_id="slip-file-003",
    )
    ok, _, _ = await approve_payment_request(db_session, first.id, admin_id=999)
    assert ok
    with pytest.raises(SlipAlreadyUsedError):
        await create_payment_request(
            session=db_session, user_id=user_b.id, package_code="MIN_30",
            receipt_file_id="slip-file-003",
        )


@pytest.mark.asyncio
async def test_batch_aj_h1_pending_slip_of_another_user_blocked(db_session):
    from app.services.payments import SlipAlreadyUsedError
    """While user A's slip is still PENDING, user B submitting the
    identical slip is the same replay in flight — blocked too."""
    user_a, _ = await get_or_create_user(db_session, telegram_id=8105)
    user_b, _ = await get_or_create_user(db_session, telegram_id=8106)
    await create_payment_request(
        session=db_session, user_id=user_a.id, package_code="MIN_10",
        receipt_file_id="slip-file-004",
    )
    with pytest.raises(SlipAlreadyUsedError):
        await create_payment_request(
            session=db_session, user_id=user_b.id, package_code="MIN_10",
            receipt_file_id="slip-file-004",
        )


@pytest.mark.asyncio
async def test_batch_aj_h1_resubmit_after_rejection_allowed(db_session):
    """A REJECTED slip was judged unusable by an admin; the legitimate
    owner re-sending it (e.g. after a fix-up conversation) must not be
    locked out forever — rejection frees the fingerprint."""
    user, _ = await get_or_create_user(db_session, telegram_id=8107)
    first = await create_payment_request(
        session=db_session, user_id=user.id, package_code="MIN_10",
        receipt_file_id="slip-file-005",
    )
    ok, _, _ = await reject_payment_request(db_session, first.id, admin_id=999, reason="blurry")
    assert ok
    again = await create_payment_request(
        session=db_session, user_id=user.id, package_code="MIN_10",
        receipt_file_id="slip-file-005",
    )
    assert again.status == PaymentStatus.PENDING.value
    assert again.id != first.id


@pytest.mark.asyncio
async def test_batch_aj_h1_same_user_pending_still_returns_existing(db_session):
    """Pre-existing behaviour preserved: the same user's double-tap
    while PENDING returns the existing request instead of erroring."""
    user, _ = await get_or_create_user(db_session, telegram_id=8108)
    first = await create_payment_request(
        session=db_session, user_id=user.id, package_code="MIN_10",
        receipt_file_id="slip-file-006",
    )
    second = await create_payment_request(
        session=db_session, user_id=user.id, package_code="MIN_10",
        receipt_file_id="slip-file-006",
    )
    assert second.id == first.id


@pytest.mark.asyncio
async def test_batch_aj_h1_unique_index_rejects_duplicate_fingerprint(db_session):
    """Race backstop: the defensive partial unique index (created at
    startup by app.db.session) makes two live rows with one
    fingerprint impossible even if both service checks race."""
    from app.db.session import ensure_batch_aj_unique_indexes

    engine = db_session.bind
    await ensure_batch_aj_unique_indexes(engine)
    user, _ = await get_or_create_user(db_session, telegram_id=8109)
    await create_payment_request(
        session=db_session, user_id=user.id, package_code="MIN_10",
        receipt_file_id="slip-file-007",
    )
    await db_session.commit()
    rogue = PaymentRequest(
        id="PAY-ROGUE", user_id=user.id, package_code="MIN_10", minutes=10,
        amount_mmk=8000, status=PaymentStatus.PENDING.value,
        slip_fingerprint="tg:slip-file-007",
    )
    db_session.add(rogue)
    with pytest.raises(Exception):
        await db_session.flush()


# ── M3: the review claim is enforced by the service itself ───────────


@pytest.mark.asyncio
async def test_batch_aj_m3_approve_blocked_by_other_admins_claim(db_session):
    """Unfixed hole: the claim was only checked in the bot handlers;
    the Mini App approve path (approve_payment_request) never looked
    at it, so a second admin could approve over an in-progress edit."""
    user, _ = await get_or_create_user(db_session, telegram_id=8110)
    payment = await create_payment_request(
        session=db_session, user_id=user.id, package_code="MIN_10",
        receipt_file_id="slip-file-010",
    )
    ok, _, code = await claim_payment_review(db_session, payment.id, admin_id=111)
    assert ok and code == "CLAIMED"
    await db_session.commit()

    ok, _, code = await approve_payment_request(db_session, payment.id, admin_id=222)
    assert ok is False
    assert code == "CLAIMED_BY_OTHER"

    # The claim holder themselves can still approve.
    ok, approved, code = await approve_payment_request(db_session, payment.id, admin_id=111)
    assert ok is True
    assert approved.status == PaymentStatus.APPROVED.value


@pytest.mark.asyncio
async def test_batch_aj_m3_reject_blocked_by_other_admins_claim(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=8111)
    payment = await create_payment_request(
        session=db_session, user_id=user.id, package_code="MIN_10",
        receipt_file_id="slip-file-011",
    )
    ok, _, _ = await claim_payment_review(db_session, payment.id, admin_id=111)
    assert ok
    await db_session.commit()
    ok, _, code = await reject_payment_request(db_session, payment.id, admin_id=222, reason="x")
    assert ok is False
    assert code == "CLAIMED_BY_OTHER"


@pytest.mark.asyncio
async def test_batch_aj_m3_stale_claim_does_not_block(db_session):
    """A claim older than the 60-minute TTL is abandoned work — it
    must not block another admin forever."""
    user, _ = await get_or_create_user(db_session, telegram_id=8112)
    payment = await create_payment_request(
        session=db_session, user_id=user.id, package_code="MIN_10",
        receipt_file_id="slip-file-012",
    )
    ok, claimed, _ = await claim_payment_review(db_session, payment.id, admin_id=111)
    assert ok
    claimed.review_claimed_at = utc_now() - timedelta(minutes=61)
    await db_session.commit()
    ok, _, code = await approve_payment_request(db_session, payment.id, admin_id=222)
    assert ok is True


# ── M4: edit-and-approve bounds + recap-credits correction ───────────


@pytest.mark.asyncio
async def test_batch_aj_m4_service_level_minutes_bound(db_session):
    """Unfixed: apply_edited_approval accepted corrected_minutes up to
    whatever the caller passed (the 100k cap lived only in the bot
    FSM), so a non-FSM caller could credit absurd minutes."""
    user, _ = await get_or_create_user(db_session, telegram_id=8113)
    payment = await create_payment_request(
        session=db_session, user_id=user.id, package_code="MIN_10",
        receipt_file_id="slip-file-013",
    )
    ok, _, _ = await claim_payment_review(db_session, payment.id, admin_id=111)
    assert ok
    await db_session.commit()
    ok, _, _ = await apply_edited_approval(db_session, payment.id, 111, 100_001)
    assert ok is False
    row = await db_session.get(PaymentRequest, payment.id)
    assert row.status == PaymentStatus.PENDING.value


@pytest.mark.asyncio
async def test_batch_aj_m4_edited_recap_credits_replace_catalogue(db_session):
    """Unfixed: edit-and-approve corrected minutes only; the plan's
    recap credits rode along at full catalogue value even when the
    admin knew the transfer covered fewer."""
    user, _ = await get_or_create_user(db_session, telegram_id=8114)
    before = int(user.recap_credits or 0)
    payment = await create_payment_request(
        session=db_session, user_id=user.id, package_code="combined_starter",
        receipt_file_id="slip-file-014",
    )
    ok, _, _ = await claim_payment_review(db_session, payment.id, admin_id=111)
    assert ok
    await db_session.commit()
    ok, approved, _ = await apply_edited_approval(
        db_session, payment.id, 111, 45, corrected_recap_credits=2,
    )
    assert ok is True
    await db_session.refresh(user)
    assert int(user.recap_credits or 0) == before + 2  # not the catalogue 5
    bal = await get_minute_balance(db_session, user.id)
    assert bal.purchased_seconds == 45 * 60
    assert approved.edited_recap_credits == 2


@pytest.mark.asyncio
async def test_batch_aj_m4_edited_recap_credits_bounded(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=8115)
    payment = await create_payment_request(
        session=db_session, user_id=user.id, package_code="combined_starter",
        receipt_file_id="slip-file-015",
    )
    ok, _, _ = await claim_payment_review(db_session, payment.id, admin_id=111)
    assert ok
    await db_session.commit()
    ok, _, _ = await apply_edited_approval(
        db_session, payment.id, 111, 30, corrected_recap_credits=1001,
    )
    assert ok is False


# ── H6 (voice side): consume/refund are net-aware ────────────────────


async def _fund_purchased(db_session, user_id, seconds=3600):
    bal = await get_minute_balance(db_session, user_id)
    bal.purchased_seconds = seconds
    await db_session.flush()
    return bal


@pytest.mark.asyncio
async def test_batch_aj_h6_consume_after_refund_charges_again(db_session):
    """Unfixed hole: consume_seconds skipped whenever ANY debit row
    existed for the job, so an admin retry of a refunded job ran —
    and completed — without ever being billed again."""
    user, _ = await get_or_create_user(db_session, telegram_id=8116)
    await _fund_purchased(db_session, user.id)
    await consume_seconds(db_session, user.id, 300, job_id="job-h6-a")
    bal = await get_minute_balance(db_session, user.id)
    assert bal.purchased_seconds == 3300
    await refund_job_seconds(db_session, user.id, "job-h6-a")
    bal = await get_minute_balance(db_session, user.id)
    assert bal.purchased_seconds == 3600
    # The retry bills again.
    await consume_seconds(db_session, user.id, 300, job_id="job-h6-a")
    bal = await get_minute_balance(db_session, user.id)
    assert bal.purchased_seconds == 3300


@pytest.mark.asyncio
async def test_batch_aj_h6_second_refund_refunds_only_new_delta(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=8117)
    await _fund_purchased(db_session, user.id)
    await consume_seconds(db_session, user.id, 300, job_id="job-h6-b")
    await refund_job_seconds(db_session, user.id, "job-h6-b")
    await consume_seconds(db_session, user.id, 300, job_id="job-h6-b")
    await refund_job_seconds(db_session, user.id, "job-h6-b")
    bal = await get_minute_balance(db_session, user.id)
    assert bal.purchased_seconds == 3600
    # A third refund is a no-op (net is zero), never a mint.
    await refund_job_seconds(db_session, user.id, "job-h6-b")
    bal = await get_minute_balance(db_session, user.id)
    assert bal.purchased_seconds == 3600


@pytest.mark.asyncio
async def test_batch_aj_h6_consume_still_idempotent_without_refund(db_session):
    """The original protection must survive the netting: a duplicate
    consume with no intervening refund still charges exactly once."""
    user, _ = await get_or_create_user(db_session, telegram_id=8118)
    await _fund_purchased(db_session, user.id)
    await consume_seconds(db_session, user.id, 300, job_id="job-h6-c")
    await consume_seconds(db_session, user.id, 300, job_id="job-h6-c")
    bal = await get_minute_balance(db_session, user.id)
    assert bal.purchased_seconds == 3300


# ── L3: last month's free seconds must not refund into this month ────


@pytest.mark.asyncio
async def test_batch_aj_l3_cross_month_free_debit_not_refunded(db_session):
    """Unfixed: refund_job_seconds credited MONTHLY_FREE debits back
    with no period check — a job charged against LAST month's free
    bucket, refunded this month, minted free seconds on top of this
    month's fresh allowance."""
    user, _ = await get_or_create_user(db_session, telegram_id=8119)
    bal = await get_minute_balance(db_session, user.id)
    bal.monthly_free_seconds = 0
    bal.purchased_seconds = 100
    await db_session.flush()
    old = utc_now() - timedelta(days=40)
    db_session.add(MinuteLedgerEvent(
        user_id=user.id, source=LedgerSource.MONTHLY_FREE.value,
        direction=LedgerDirection.DEBIT.value, seconds=120,
        reference_id="job-l3", note="last month debit", created_at=old,
    ))
    db_session.add(MinuteLedgerEvent(
        user_id=user.id, source=LedgerSource.PURCHASE.value,
        direction=LedgerDirection.DEBIT.value, seconds=60,
        reference_id="job-l3", note="purchased debit",
    ))
    await db_session.flush()
    await refund_job_seconds(db_session, user.id, "job-l3")
    bal = await get_minute_balance(db_session, user.id)
    assert bal.purchased_seconds == 160  # purchased part refunded
    assert bal.monthly_free_seconds == 0  # last month's free part NOT resurrected


# ── H8: the persisted recap refund claim is an atomic CAS ────────────


@pytest.mark.asyncio
async def test_batch_aj_h8_concurrent_claims_exactly_one_wins(tmp_path, monkeypatch):
    """Unfixed hole: the claim was read-modify-write on the
    recap_config blob — two concurrent claimers (cancel racing the
    failure tail across a restart boundary) could both read
    credit_refunded=False and both proceed to refund."""
    from app.webapp import api as api_mod

    engine, maker = await _open_store(tmp_path, monkeypatch)
    async with maker() as s:
        user, _ = await get_or_create_user(s, 8120)
        await s.commit()
        uid = user.id
    # Three independent jobs: on the unfixed read-modify-write claim,
    # at least one pair of concurrent claimers both reading
    # credit_refunded=False before either commits is enough to fail.
    for n in range(3):
        job_id = f"rc_aj_h8_{n}"
        await _make_job(maker, job_id, uid, _recap_blob())
        results = await asyncio.gather(
            api_mod._claim_persisted_recap_refund(job_id, 2),
            api_mod._claim_persisted_recap_refund(job_id, 2),
        )
        states = sorted(r[0] for r in results)
        assert states == ["already", "claimed"], (job_id, results)
    await engine.dispose()


@pytest.mark.asyncio
async def test_batch_aj_h8_sequential_claim_still_reports_already(tmp_path, monkeypatch):
    from app.webapp import api as api_mod

    engine, maker = await _open_store(tmp_path, monkeypatch)
    async with maker() as s:
        user, _ = await get_or_create_user(s, 8121)
        await s.commit()
        uid = user.id
    await _make_job(maker, "rc_aj_h8b", uid, _recap_blob())
    first = await api_mod._claim_persisted_recap_refund("rc_aj_h8b", 2)
    second = await api_mod._claim_persisted_recap_refund("rc_aj_h8b", 2)
    assert first[0] == "claimed"
    assert second[0] == "already"
    await engine.dispose()


# ── H11: top-up / refund amounts derive from the durable ledger ─────


@pytest.mark.asyncio
async def test_batch_aj_h11_topup_uses_ledger_total_not_stale_entry(tmp_path, monkeypatch):
    """Unfixed hole: the top-up computed `already` from the in-memory
    entry / persisted blob only. After a restart between the submit
    charge commit and the blob persist, the blob said 1 while the
    ledger had durably charged 3 — the top-up charged the difference
    a second time."""
    from app.webapp import api as api_mod

    engine, maker = await _open_store(tmp_path, monkeypatch)
    async with maker() as s:
        user, _ = await get_or_create_user(s, 8122)
        user.recap_credits = 10
        await s.commit()
        uid = user.id
    # Durable truth: 3 credits were charged for this job (submit 1 +
    # top-up 2), each with its DEBIT ledger event.
    assert await api_mod._charge_recap_credits(uid, 1, job_id="rc_aj_h11")
    assert await api_mod._charge_recap_credits(uid, 2, job_id="rc_aj_h11")
    # The entry a restart would rebuild from the stale blob says 1.
    entry = {"id": "rc_aj_h11", "credit_deducted": True, "credits_charged": 1}
    total = await api_mod._top_up_recap_segment_charge(uid, entry, 3)
    assert total == 3
    async with maker() as s:
        user = await s.get(User, uid)
        assert int(user.recap_credits) == 7  # 10 - 3, NOT 10 - 3 - 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_batch_aj_h11_refund_uses_ledger_total(tmp_path, monkeypatch):
    """Mirror image: the refund must return what the LEDGER says was
    charged (3), even when the entry/blob only remembers 1."""
    from app.webapp import api as api_mod

    engine, maker = await _open_store(tmp_path, monkeypatch)
    async with maker() as s:
        user, _ = await get_or_create_user(s, 8123)
        user.recap_credits = 10
        await s.commit()
        uid = user.id
    assert await api_mod._charge_recap_credits(uid, 1, job_id="rc_aj_h11b")
    assert await api_mod._charge_recap_credits(uid, 2, job_id="rc_aj_h11b")
    entry = {"id": "rc_aj_h11b", "credit_deducted": True, "credits_charged": 1}
    refunded = await api_mod._refund_recap_job_charge(uid, entry)
    assert refunded is True
    async with maker() as s:
        user = await s.get(User, uid)
        assert int(user.recap_credits) == 10  # all 3 back, not just 1
    await engine.dispose()


# ── L22: allowance grant serialises on the user row ──────────────────


@pytest.mark.asyncio
async def test_batch_aj_l22_grant_does_not_resurrect_consumed_seconds(db_session):
    """Behavioural pin for the grant/consume contract (Limits round replaced
    monthly allowance with daily free allowance). Grant for today, consume
    part of it, grant again: the second grant is a same-day no-op and
    the consumed seconds stay consumed."""
    from app.services.ledger import grant_daily_free_allowance

    user, _ = await get_or_create_user(db_session, telegram_id=8124)
    await grant_daily_free_allowance(db_session, user.id)
    bal = await get_minute_balance(db_session, user.id)
    granted = bal.daily_free_seconds
    assert granted > 0
    await consume_seconds(db_session, user.id, 60, job_id="job-l22")
    await grant_daily_free_allowance(db_session, user.id)
    bal = await get_minute_balance(db_session, user.id)
    assert bal.daily_free_seconds == granted - 60


def test_batch_aj_l22_grant_locks_user_row():
    """Source pin: grant_daily_free_allowance must load the User row
    WITH FOR UPDATE (mirroring award_welcome_bonus / consume_seconds)
    so a concurrent consume cannot be overwritten by the grant's
    stale absolute bucket write."""
    import inspect

    from app.services import ledger

    src = inspect.getsource(ledger.grant_daily_free_allowance)
    assert "with_for_update" in src
