"""Batch AO — tiered recap pricing (owner decision 2026-11-05).

Credits = ceil(total source duration / 180): one credit per started
3-minute band, replacing the per-part formula for segmented jobs and
adding a band top-up for single jobs — but ONLY when the
``tiered_recap_pricing_enabled`` dynamic setting is on. The build
ships dark: with the switch off, every charge, quote and label is the
legacy behaviour (pinned here against the same helpers the legacy
per-part suite pins). A job's pricing mode is pinned at submit, so
the quote the user saw is the total the worker charges; refunds
return exactly the charged total on every path, including the Batch
AJ H11 restart path (stale entry, ledger carries the truth).

Free lane unchanged: a free source (≤2 min) prices as band 1, which
the historical credit gate already required. Admins are never
charged. Pack sizes, prices and balances are untouched — this batch
changes only what a credit covers, behind the switch.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

USER_ID = 515151
ADMIN_ID = 313131


class _LedgerHandle:
    """Real-SQLite stand-in for the credit helpers' session factory
    (mirrors tests/unit/test_recap_split_per_part_credit.py)."""

    def __init__(self, db_file: str):
        self._db_file = db_file

    def make_session(self):
        from sqlalchemy.ext.asyncio import (
            AsyncSession,
            async_sessionmaker,
            create_async_engine,
        )

        engine = create_async_engine(
            f"sqlite+aiosqlite:///{self._db_file}", future=True
        )
        maker = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )
        return maker()

    def add_user(self, uid=USER_ID, credits=0, category="PURCHASED"):
        from app.models.schema import User

        async def _seed():
            async with self.make_session() as s:
                s.add(
                    User(
                        id=uid,
                        recap_credits=credits,
                        account_category=category,
                    )
                )
                await s.commit()

        asyncio.run(_seed())
        return uid

    def credits(self, uid=USER_ID) -> int:
        from app.models.schema import User

        async def _read():
            async with self.make_session() as s:
                user = await s.get(User, uid)
                return user.recap_credits if user else None

        return asyncio.run(_read())


@pytest.fixture()
def ledger(monkeypatch, tmp_path):
    from sqlalchemy import create_engine

    import app.models.schema  # noqa: F401 — register all models on Base
    from app.config import settings as settings_obj
    from app.db.base import Base
    from app.webapp import api as api_mod

    db_file = str(tmp_path / "batch_ao_credits.db")
    sync_engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    handle = _LedgerHandle(db_file)
    monkeypatch.setattr(
        "app.db.session.AsyncSessionLocal", handle.make_session
    )
    monkeypatch.setattr(settings_obj, "admin_ids_raw", str(ADMIN_ID))
    # The switch starts OFF in every test; tiered tests flip it (or
    # stamp the entry) explicitly.
    monkeypatch.setattr(
        settings_obj, "tiered_recap_pricing_enabled", False
    )
    yield handle
    api_mod.RECAP_JOBS_REGISTRY.clear()


def _tiered_on(monkeypatch):
    from app.config import settings as settings_obj

    monkeypatch.setattr(
        settings_obj, "tiered_recap_pricing_enabled", True
    )


# ── Band math ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "duration,expected",
    [
        (180, 1),      # exactly one band
        (181, 2),      # one second into the next band
        (540, 3),      # exactly three bands
        (541, 4),
        (600, 4),      # the 10-minute cap prices as 4
        (179, 1),
        (324, 2),      # the quote example: 5:24 → 2 credits
        (1500, 9),     # 25 minutes → 9 (segmented totals use the total)
        (1, 1),
        (0, 1),        # unknown duration: defensive single band
        (None, 1),
        (-30, 1),
    ],
)
def test_band_math_boundaries(duration, expected):
    from app.webapp.api import _recap_band_credits

    assert _recap_band_credits(duration) == expected


def test_switch_default_off_and_registered():
    from app.config import settings as settings_obj
    from app.services.dynamic_settings import SETTING_TYPES

    assert settings_obj.tiered_recap_pricing_enabled is False
    assert SETTING_TYPES["tiered_recap_pricing_enabled"] is bool


# ── Segmented top-up: band replaces per-part when tiered ─────────────

def test_segmented_tiered_charges_band_not_parts(ledger):
    from app.webapp.api import _top_up_recap_segment_charge

    ledger.add_user(credits=19)
    # Submit took 1 (balance 20 → 19). The job's TOTAL duration is
    # 25 min → band 9, even though the split produced only 3 parts.
    entry = {
        "credit_deducted": True,
        "credits_charged": 1,
        "tiered_pricing": True,
        "source_duration_seconds": 1500.0,
    }
    total = asyncio.run(_top_up_recap_segment_charge(USER_ID, entry, 3))
    assert total == 9
    assert entry["credits_charged"] == 9
    assert entry["parts_count"] == 3  # processing split is unchanged
    assert ledger.credits() == 11  # 20 − 9 in total

    # Idempotent: a re-run for the same job charges nothing more.
    assert asyncio.run(_top_up_recap_segment_charge(USER_ID, entry, 3)) == 9
    assert ledger.credits() == 11


def test_segmented_switch_off_charges_legacy_per_part(ledger):
    from app.webapp.api import _top_up_recap_segment_charge

    ledger.add_user(credits=19)
    # Same job shape, but the entry carries no tier stamp and the
    # switch is off: the legacy per-part formula applies exactly.
    entry = {
        "credit_deducted": True,
        "credits_charged": 1,
        "source_duration_seconds": 1500.0,
    }
    total = asyncio.run(_top_up_recap_segment_charge(USER_ID, entry, 3))
    assert total == 3
    assert entry["credits_charged"] == 3
    assert ledger.credits() == 17  # 20 − 3


def test_pricing_mode_is_pinned_by_the_entry_stamp(ledger, monkeypatch):
    from app.webapp.api import _top_up_recap_segment_charge

    ledger.add_user(credits=30)
    # Switch ON globally, but this job was submitted under the old
    # rate (stamp False): it must still price per part.
    _tiered_on(monkeypatch)
    legacy_entry = {
        "credit_deducted": True,
        "credits_charged": 1,
        "tiered_pricing": False,
        "source_duration_seconds": 1500.0,
    }
    assert asyncio.run(
        _top_up_recap_segment_charge(USER_ID, legacy_entry, 3)
    ) == 3

    # Switch OFF globally, but this job was submitted tiered
    # (stamp True): it must still price by band.
    monkeypatch.setattr(
        "app.config.settings.tiered_recap_pricing_enabled", False
    )
    tiered_entry = {
        "credit_deducted": True,
        "credits_charged": 1,
        "tiered_pricing": True,
        "source_duration_seconds": 1500.0,
    }
    assert asyncio.run(
        _top_up_recap_segment_charge(USER_ID, tiered_entry, 3)
    ) == 9


def test_segmented_tiered_insufficient_balance_fails_clean(ledger):
    from app.webapp.api import (
        _RecapInsufficientCreditsError,
        _refund_recap_job_charge,
        _top_up_recap_segment_charge,
    )

    ledger.add_user(credits=1)
    # Balance was 2; submit took 1. Band total is 9 — cannot cover.
    entry = {
        "credit_deducted": True,
        "credits_charged": 1,
        "tiered_pricing": True,
        "source_duration_seconds": 1500.0,
    }
    with pytest.raises(_RecapInsufficientCreditsError) as excinfo:
        asyncio.run(_top_up_recap_segment_charge(USER_ID, entry, 3))
    assert excinfo.value.total_credits == 9
    assert "9" in str(excinfo.value)
    # Nothing extra was charged; the failure path refunds the submit
    # credit and the user is exactly whole.
    assert ledger.credits() == 1
    assert asyncio.run(_refund_recap_job_charge(USER_ID, entry)) is True
    assert ledger.credits() == 2
    assert entry["credits_refunded"] == 1


# ── Single-job band top-up (_apply_recap_band_top_up) ────────────────

def test_single_job_band_top_up_and_refund_symmetry(ledger):
    from app.webapp.api import (
        _apply_recap_band_top_up,
        _refund_recap_job_charge,
    )

    ledger.add_user(credits=4)
    # 5:24 source: submit took 1 (5 → 4), band total is 2.
    entry = {
        "credit_deducted": True,
        "credits_charged": 1,
        "tiered_pricing": True,
        "source_duration_seconds": 324.0,
    }
    total = asyncio.run(
        _apply_recap_band_top_up(USER_ID, entry, is_admin_user=False)
    )
    assert total == 2
    assert entry["credits_charged"] == 2
    assert ledger.credits() == 3  # 5 − 2 in total

    # Failure afterwards refunds the FULL band amount, exactly once.
    assert asyncio.run(_refund_recap_job_charge(USER_ID, entry)) is True
    assert ledger.credits() == 5
    assert entry["credits_refunded"] == 2
    assert asyncio.run(_refund_recap_job_charge(USER_ID, entry)) is False
    assert ledger.credits() == 5


def test_single_job_band_top_up_inert_when_off(ledger):
    from app.webapp.api import _apply_recap_band_top_up

    ledger.add_user(credits=4)
    entry = {
        "credit_deducted": True,
        "credits_charged": 1,
        "source_duration_seconds": 324.0,
    }
    total = asyncio.run(
        _apply_recap_band_top_up(USER_ID, entry, is_admin_user=False)
    )
    assert total == 1
    assert entry["credits_charged"] == 1
    assert ledger.credits() == 4  # nothing moved


def test_single_job_band_top_up_free_lane_and_admin(ledger):
    from app.webapp.api import _apply_recap_band_top_up

    # Free lane: a ≤2-minute source prices as band 1 — the top-up is
    # a no-op even with tiered pricing stamped on the job.
    ledger.add_user(credits=2, category="FREE")
    free_entry = {
        "credit_deducted": True,
        "credits_charged": 1,
        "tiered_pricing": True,
        "source_duration_seconds": 100.0,
    }
    assert asyncio.run(
        _apply_recap_band_top_up(USER_ID, free_entry, is_admin_user=False)
    ) == 1
    assert ledger.credits() == 2

    # Admins are never charged, whatever the duration.
    ledger.add_user(uid=ADMIN_ID, credits=7)
    admin_entry = {
        "credit_deducted": True,
        "credits_charged": 1,
        "tiered_pricing": True,
        "source_duration_seconds": 1500.0,
    }
    assert asyncio.run(
        _apply_recap_band_top_up(ADMIN_ID, admin_entry, is_admin_user=True)
    ) == 1
    assert ledger.credits(ADMIN_ID) == 7


def test_band_top_up_falls_back_to_worker_probed_duration(ledger):
    from app.webapp.api import _apply_recap_band_top_up

    ledger.add_user(credits=10)
    # No submit-resolved duration (probe failed at submit), but the
    # worker probed the downloaded source: band from that duration.
    entry = {
        "credit_deducted": True,
        "credits_charged": 1,
        "tiered_pricing": True,
        "source_duration_seconds": 0.0,
        "probed_duration_seconds": 600.0,
    }
    total = asyncio.run(
        _apply_recap_band_top_up(USER_ID, entry, is_admin_user=False)
    )
    assert total == 4  # ceil(600/180)
    assert ledger.credits() == 7  # 11 − 4 in total


# ── H11 restart path: stale entry, ledger carries the band ───────────

def test_band_refund_on_h11_restart_path(ledger):
    """A restart between the band top-up and its blob persist rebuilds
    the entry with the stale submit-time total (1). The refund must
    still return the full band amount the LEDGER recorded — never the
    stale 1 — and exactly once."""
    from app.services.ledger import record_recap_credit_event
    from app.webapp.api import (
        _apply_recap_band_top_up,
        _refund_recap_job_charge,
    )

    job_id = "rc_ao_h11_test"
    # The job really charged 9 in total (submit 1 + top-up 8), the
    # balance already reflects it (20 → 11); only the entry is stale.
    ledger.add_user(credits=11)

    async def _seed_ledger():
        async with ledger.make_session() as s:
            await record_recap_credit_event(
                s, user_id=USER_ID, direction="DEBIT", credits=1,
                reference_id=job_id, note="submit",
            )
            await record_recap_credit_event(
                s, user_id=USER_ID, direction="DEBIT", credits=8,
                reference_id=job_id, note="band top-up",
            )
            await s.commit()

    asyncio.run(_seed_ledger())

    entry = {
        "id": job_id,
        "credit_deducted": True,
        "credits_charged": 1,  # stale — the lost persist
        "tiered_pricing": True,
        "source_duration_seconds": 1500.0,
    }
    # A worker re-run reconciles against the ledger first: no double
    # charge, and the entry is healed to the true total.
    total = asyncio.run(
        _apply_recap_band_top_up(USER_ID, entry, is_admin_user=False)
    )
    assert total == 9
    assert entry["credits_charged"] == 9
    assert ledger.credits() == 11

    # The failure refund returns the full band amount, exactly once.
    assert asyncio.run(_refund_recap_job_charge(USER_ID, entry)) is True
    assert ledger.credits() == 20
    assert entry["credits_refunded"] == 9
    assert asyncio.run(_refund_recap_job_charge(USER_ID, entry)) is False
    assert ledger.credits() == 20


# ── Submit path (create_recap_job) ───────────────────────────────────

def _submit(ledger, monkeypatch, *, uid=USER_ID, credits=5,
            category="PURCHASED", **req_over):
    """Drive create_recap_job against the ledger DB with a fake
    runner, returning (response, user_balance_after)."""
    from fastapi import HTTPException

    from app.models.schema import User
    from app.webapp import api as api_mod

    calls = []

    async def _fake_runner(job_id, req, user_id, chat_id, **kwargs):
        calls.append(job_id)

    monkeypatch.setattr(
        api_mod, "_run_recap_background_job", _fake_runner
    )

    base = {
        "url": "",
        "title": "Test Movie",
        "voice_slug": "komin",
        "duration_seconds": 60,
    }
    base.update(req_over)

    async def _main():
        async with ledger.make_session() as s:
            user = await s.get(User, uid)
            if user is None:
                s.add(
                    User(
                        id=uid,
                        recap_credits=credits,
                        account_category=category,
                    )
                )
                await s.commit()
                user = await s.get(User, uid)
            req = api_mod.RecapConfigRequest(**base)
            try:
                resp = await api_mod.create_recap_job(
                    req, user=user, db=s, _gate=user
                )
            except HTTPException as exc:
                return ("error", exc.status_code, exc.detail)
            # Let the fake runner task run before the loop closes.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return ("ok", resp)

    # The registry entry stays in place: several tests inspect the
    # exact entry create_recap_job built (the fixture clears the
    # registry between tests).
    return asyncio.run(_main())


def test_submit_off_has_no_credits_total_and_charges_one(ledger, monkeypatch):
    result = _submit(ledger, monkeypatch, credits=5, duration_seconds=324)
    assert result[0] == "ok"
    resp = result[1]
    # Legacy response shape: the tiered quote key does not exist.
    assert "credits_total" not in resp
    assert resp["credits_charged"] == 1
    assert resp["remaining_credits"] == 4
    assert ledger.credits() == 4


def test_submit_on_quotes_band_and_stamps_entry(ledger, monkeypatch):
    from app.webapp import api as api_mod

    _tiered_on(monkeypatch)
    result = _submit(ledger, monkeypatch, credits=5, duration_seconds=324)
    assert result[0] == "ok"
    resp = result[1]
    # The quote is the band total; the submit itself still takes the
    # single submit credit (the worker tops up to the quote).
    assert resp["credits_total"] == 2
    assert resp["credits_charged"] == 1
    assert ledger.credits() == 4
    entry = api_mod.RECAP_JOBS_REGISTRY[resp["recap_id"]]
    assert entry["tiered_pricing"] is True
    assert entry["source_duration_seconds"] == 324.0


def test_submit_on_blocks_when_balance_cannot_cover_band(ledger, monkeypatch):
    _tiered_on(monkeypatch)
    # 5:24 → band 2, but the user holds exactly 1 credit: blocked at
    # submit with the tiered message, before any credit moves.
    result = _submit(ledger, monkeypatch, credits=1, duration_seconds=324)
    assert result[0] == "error"
    assert result[1] == 403
    assert "2 Credits" in result[2]
    assert ledger.credits() == 1


def test_submit_off_allows_what_tiered_blocks(ledger, monkeypatch):
    result = _submit(ledger, monkeypatch, credits=1, duration_seconds=324)
    assert result[0] == "ok"
    assert result[1]["credits_charged"] == 1
    assert ledger.credits() == 0


def test_submit_on_segmented_precheck_uses_band_not_parts(
    ledger, monkeypatch
):
    _tiered_on(monkeypatch)
    # 25 minutes, split size 10 → the part ESTIMATE is 3, but the
    # band total is 9. A balance of 5 covers the old per-part price
    # and must now be refused; a balance of 10 is accepted with the
    # quote set to the band, not the estimate.
    result = _submit(
        ledger, monkeypatch, credits=5, duration_seconds=1500,
        split_video=True, split_minutes=10,
    )
    assert result[0] == "error"
    assert result[1] == 403
    assert "9 Credits" in result[2]
    assert ledger.credits() == 5

    result = _submit(
        ledger, monkeypatch, uid=USER_ID + 2, credits=10,
        duration_seconds=1500, split_video=True, split_minutes=10,
    )
    assert result[0] == "ok"
    resp = result[1]
    assert resp["estimated_parts"] == 3
    assert resp["credits_total"] == 9
    assert resp["credits_charged"] == 1


def test_submit_off_segmented_precheck_uses_parts(ledger, monkeypatch):
    result = _submit(
        ledger, monkeypatch, credits=5, duration_seconds=1500,
        split_video=True, split_minutes=10,
    )
    assert result[0] == "ok"
    resp = result[1]
    assert resp["estimated_parts"] == 3
    assert "credits_total" not in resp


def test_submit_on_free_lane_unchanged(ledger, monkeypatch):
    _tiered_on(monkeypatch)
    # Free user, 100-second source (inside the 2-minute free cap),
    # the 3 signup credits: band is 1, charge is 1, quote is 1.
    result = _submit(
        ledger, monkeypatch, credits=3, category="FREE",
        duration_seconds=100,
    )
    assert result[0] == "ok"
    resp = result[1]
    assert resp["credits_total"] == 1
    assert resp["credits_charged"] == 1
    assert ledger.credits() == 2


def test_submit_quote_equals_worker_charge(ledger, monkeypatch):
    """The end-to-end guarantee: the total quoted in the submit
    response is the total the worker's band top-up charges, and a
    later failure refunds exactly that total."""
    from app.webapp import api as api_mod
    from app.webapp.api import (
        _apply_recap_band_top_up,
        _refund_recap_job_charge,
    )

    _tiered_on(monkeypatch)
    result = _submit(ledger, monkeypatch, credits=5, duration_seconds=324)
    assert result[0] == "ok"
    resp = result[1]
    quoted = resp["credits_total"]
    assert quoted == 2
    assert ledger.credits() == 4  # submit credit only, so far

    entry = api_mod.RECAP_JOBS_REGISTRY[resp["recap_id"]]
    charged = asyncio.run(
        _apply_recap_band_top_up(USER_ID, entry, is_admin_user=False)
    )
    assert charged == quoted
    assert ledger.credits() == 3  # 5 − quoted in total

    assert asyncio.run(_refund_recap_job_charge(USER_ID, entry)) is True
    assert ledger.credits() == 5
    assert entry["credits_refunded"] == quoted


# ── Messages ─────────────────────────────────────────────────────────

def test_tiered_message_names_band_total():
    from app.webapp.api import _tiered_credits_message

    msg = _tiered_credits_message(9, balance=5)
    assert "9 Credits" in msg
    assert "(5c)" in msg
    plain = _tiered_credits_message(2)
    assert "2 Credits" in plain


# ── Wiring contracts (settings / API / Mini App) ─────────────────────

def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_batch_ao_wiring_contracts():
    api_py = _read("app/webapp/api.py")
    app_js = _read("app/webapp/static/js/app.js")
    i18n_js = _read("app/webapp/static/js/i18n.js")
    html = _read("app/webapp/static/index.html")
    settings_py = _read("app/config/settings.py")
    dyn_py = _read("app/services/dynamic_settings.py")

    # Switch: setting + dynamic registration + admin surface + /me.
    assert "tiered_recap_pricing_enabled" in settings_py
    assert '"tiered_recap_pricing_enabled": bool' in dyn_py
    assert api_py.count("tiered_recap_pricing_enabled") >= 4
    assert '"tiered_pricing_enabled": _tiered_recap_pricing_enabled()' in api_py

    # Charging: band helpers + both worker top-up paths wired.
    assert "def _recap_band_credits(" in api_py
    assert "_top_up_recap_charge_to_total(" in api_py
    assert "_apply_recap_band_top_up(" in api_py
    assert "_top_up_recap_segment_charge(user_id, entry, total_parts)" in api_py
    # The quote rides the submit response only when armed.
    assert '"credits_total"' in api_py

    # Mini App: CTA/hint quote branch + confirmation prefers the
    # server total; the i18n key exists in BOTH locales with both
    # placeholders.
    assert "tiered_pricing_enabled" in app_js
    assert "formatRecapQuoteDuration(" in app_js
    assert 'tf("recap_tiered_cost_estimate"' in app_js
    assert "data.credits_total" in app_js
    assert i18n_js.count("recap_tiered_cost_estimate:") == 2
    assert "{duration}" in i18n_js and "{credits}" in i18n_js

    # Cache token bumped for the static changes.
    assert "20261104_r119" in html
    assert "20261104_r64" not in html
