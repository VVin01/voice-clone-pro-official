"""Batch X (2026-11-04): 24-hour retention + truthful admin user count.

Owner's direct orders from the Admin Console screenshot review:

1. RETENTION IS 24 HOURS, NOT 7 DAYS — "၇ ရက် ထိမ်းသိမ်းတာမျိုး
   မလုပ်တော့ဘူး — ၂၄ နာရီပဲ". The retention source of truth
   (settings.media_retention_days / api._media_retention_days) drops
   7 → 1 day; every consumer (record sweep, orphan sweep, purge
   "older_retention", history window, TTL fields) already derives
   from it, and every user-facing promise of the old window — bot
   locales and Mini App copy, my + en — now states 24 hours.

2. TRUTHFUL USER COUNT — the overview headline (230 at the time)
   counted every User row ever created, including accounts that only
   ever opened the bot. Following the Batch R pattern, the headline
   is now REAL users (≥1 job in any status, OR an approved purchase,
   OR a purchase/admin credit grant) with the split riding along:
   registered total · never used. total_users keeps its literal
   meaning for other consumers.

3. Consistency pass over the neighbouring cards (VIP / pending
   slips / queue) — pinned below; each already meant what its label
   says, so nothing else changed.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.enums import (
    AccountCategory,
    JobStatus,
    JobType,
    LedgerDirection,
    LedgerSource,
    PaymentStatus,
)
from app.models.schema import Job, MinuteLedgerEvent, PaymentRequest, User
from app.services.i18n import t
from app.services.ledger import get_or_create_user
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
ADMIN_ID = 999201
NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _admin_settings(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    yield


def _user(uid: int, **kw) -> User:
    return User(
        id=uid,
        first_name=kw.pop("first_name", "T"),
        username=None,
        account_category=kw.pop("account_category", AccountCategory.FREE.value),
        language_code="my",
        **kw,
    )


def _job(jid: str, uid: int, status: str, created_at, job_type=JobType.VOICE_CLONE.value) -> Job:
    return Job(
        id=jid,
        user_id=uid,
        status=status,
        job_type=job_type,
        engine="voxcpm" if job_type == JobType.VOICE_CLONE.value else "recap",
        created_at=created_at,
    )


def _pay(pid: str, uid: int, status: str) -> PaymentRequest:
    return PaymentRequest(
        id=pid,
        user_id=uid,
        package_code="VOICE_30",
        minutes=30,
        amount_mmk=20000,
        status=status,
    )


def _ledger(uid: int, source: str, direction: str) -> MinuteLedgerEvent:
    return MinuteLedgerEvent(
        user_id=uid, source=source, direction=direction, seconds=300
    )


async def _seed(db, *objs):
    for o in objs:
        db.add(o)
    await db.commit()


# ── 1a. Retention window: history + TTL fields ───────────────────────

@pytest.mark.asyncio
async def test_history_window_is_24_hours(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=771001, first_name="H")
    await _seed(
        db_session,
        _job("jx_hist_in", user.id, JobStatus.SUCCESS.value, NOW - timedelta(hours=23)),
        _job("jx_hist_out", user.id, JobStatus.SUCCESS.value, NOW - timedelta(hours=25)),
    )

    hist = await api_mod.get_user_history(user=user, db=db_session)

    ids = {h["id"] for h in hist["history"]}
    assert "jx_hist_in" in ids
    assert "jx_hist_out" not in ids
    assert hist["retention_days"] == 1
    assert hist["ttl_hours"] == 24


# ── 1b. Retention window: the purge boundary follows it ──────────────

@pytest.mark.asyncio
async def test_purge_older_retention_uses_24h_boundary(db_session):
    admin = _user(ADMIN_ID)
    owner, _ = await get_or_create_user(db_session, telegram_id=771002, first_name="P")
    await _seed(
        db_session,
        admin,
        _job("jx_purge_in", owner.id, JobStatus.SUCCESS.value, NOW - timedelta(hours=23)),
        _job("jx_purge_out", owner.id, JobStatus.SUCCESS.value, NOW - timedelta(hours=25)),
    )

    res = await api_mod.purge_admin_history(
        api_mod.AdminPurgeHistoryRequest(mode="older_retention"),
        user=admin,
        db=db_session,
    )

    assert res["purged_count"] == 1
    remaining = {j.id for j in (await db_session.execute(select(Job))).scalars().all()}
    assert remaining == {"jx_purge_in"}


# ── 1c. No user-facing surface still promises 7 days ─────────────────

def test_no_seven_day_retention_promise_remains():
    i18n_js = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
    # The only legitimate remaining 7-day strings in the dictionary
    # are the income reporting window and the admin history FILTER
    # chip — neither is a retention promise.
    allowed_keys = {"admin_income_7d", "adm_tf_7d"}
    for line in i18n_js.splitlines():
        if "၇ ရက်" in line or "7 day" in line.lower():
            m = re.match(r"\s*([a-z0-9_]+):", line)
            assert m and m.group(1) in allowed_keys, f"stale 7-day copy: {line.strip()}"

    index_html = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
    assert "၇ ရက်အထိ" not in index_html  # expiry notes
    assert "၇ ရက်ကျော်" not in index_html  # purge button fallback

    for lang in ("my", "en"):
        data = json.loads(
            (ROOT / f"locales/{lang}/messages.json").read_text(encoding="utf-8")
        )
        for key in ("help_settings_guide", "recap_retention_note", "admin_media_gone_desc"):
            assert "၇ ရက်" not in data[key], (lang, key)
            assert "7 day" not in data[key].lower(), (lang, key)
        # …and the rendered retention lines state the 24-hour window.
        note = t("recap_retention_note", lang, retention_hours=24)
        assert "24" in note and "{retention_hours}" not in note
        guide = t(
            "help_settings_guide",
            lang,
            free_min="3", free_sec=180, char_free="1,000",
            char_purchased="5,000", vip_recap_min=10,
            # Batch BX: the guide's daily figures are the voice/TTS
            # minute caps + the FREE recap length now.
            # Batch BZ: the FREE voice day is a pre-built clause now.
            voice_daily_free_clause=" (clause)", voice_daily_paid=30,
            tts_daily_free=15, tts_daily_paid=60, recap_free_min=2,
            retention_hours=24, referral_seconds=120, referral_cap_min=40,
        )
        assert "24" in guide and "{retention_hours}" not in guide


def test_overview_users_card_is_wired_to_real_users():
    app_js = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
    assert "stats.real_users" in app_js
    assert 'id="adm-total-users-split"' in index_html
    assert "admin_stat_users_split" in app_js


# ── 2. Real users: headline + split ──────────────────────────────────

@pytest.mark.asyncio
async def test_overview_real_users_headline_and_split(db_session):
    admin = _user(ADMIN_ID)                                   # bare row → never used
    idle = _user(772001)                                      # bare row → never used
    job_user = _user(772002)                                  # FAILED job → real
    buyer = _user(772003)                                     # APPROVED slip → real
    pending_buyer = _user(772004)                             # PENDING slip only → never used
    granted = _user(772005)                                   # ADJUST credit → real
    passive = _user(772006)                                   # WELCOME credit only → never used
    overlap = _user(772007)                                   # job + purchase → real, once
    await _seed(db_session, admin, idle, job_user, buyer, pending_buyer,
                granted, passive, overlap)
    await _seed(
        db_session,
        _job("jx_r_failed", job_user.id, JobStatus.FAILED.value, NOW),
        _job("jx_r_success", overlap.id, JobStatus.SUCCESS.value, NOW),
        _pay("pay_ok", buyer.id, PaymentStatus.APPROVED.value),
        _pay("pay_pending", pending_buyer.id, PaymentStatus.PENDING.value),
        _pay("pay_overlap", overlap.id, PaymentStatus.APPROVED.value),
        _ledger(granted.id, LedgerSource.ADJUST.value, LedgerDirection.CREDIT.value),
        _ledger(passive.id, LedgerSource.WELCOME.value, LedgerDirection.CREDIT.value),
    )

    data = await api_mod.get_admin_overview(admin, db_session)

    assert data["total_users"] == 8          # every registered row (unchanged meaning)
    assert data["registered_users"] == 8
    assert data["real_users"] == 4           # job_user, buyer, granted, overlap
    assert data["never_used_users"] == 4     # admin, idle, pending_buyer, passive
    assert data["real_users"] + data["never_used_users"] == data["total_users"]


@pytest.mark.asyncio
async def test_overview_real_users_zero_when_nobody_used_the_bot(db_session):
    admin = _user(ADMIN_ID)
    await _seed(db_session, admin, _user(772101), _user(772102))

    data = await api_mod.get_admin_overview(admin, db_session)

    assert data["real_users"] == 0
    assert data["never_used_users"] == 3
    assert data["total_users"] == 3


# ── 3. Consistency pins: the neighbouring cards mean what they say ───

@pytest.mark.asyncio
async def test_overview_vip_slips_queue_cards_match_their_labels(db_session):
    admin = _user(ADMIN_ID)
    vip = _user(772201, account_category=AccountCategory.PURCHASED.value)
    free = _user(772202)
    await _seed(db_session, admin, vip, free)
    await _seed(
        db_session,
        _pay("pay_p1", free.id, PaymentStatus.PENDING.value),
        _pay("pay_p2", vip.id, PaymentStatus.PENDING.value),
        _pay("pay_a1", vip.id, PaymentStatus.APPROVED.value),
        _job("jx_q1", free.id, JobStatus.QUEUED.value, NOW),
        _job("jx_q2", free.id, JobStatus.RUNNING.value, NOW),
        _job("jx_q3", vip.id, JobStatus.SUCCESS.value, NOW),
    )

    data = await api_mod.get_admin_overview(admin, db_session)

    # VIP card = accounts in the PURCHASED category (its tap target
    # filters the users list by exactly this category).
    assert data["vip_users"] == 1
    # Pending-slips card = payment requests awaiting review only.
    assert data["pending_slips"] == 2
    # Queue card = jobs waiting in QUEUED; a RUNNING job is being
    # processed, not queued, and must not inflate it.
    assert data["queued_jobs"] == 1
    assert data["running_jobs"] == 1
