"""Batch BE — AO arming package (owner decision 2026-11-06 07:28).

Three parts:
- BE-1: a tier-priced recap whose render quality is Max pays ONE
  extra credit on top of the duration band (owner-approved
  2026-11-06 00:28). The surcharge is computed once at submit from
  the AU-clamped request, pinned on the registry entry + persisted
  state exactly like the band, and included in the quote, both
  balance pre-checks, both worker top-up targets and every refund.
  With the tiered switch OFF it does not exist — charges, response
  shape and labels are the legacy ones, byte-identical.
- BE-2: the Admin settings surface gains the tiered-pricing toggle
  (AO shipped the setting + API dark with no UI). It rides the same
  audited dynamic-settings write path as its neighbours.
- BE-3: user-facing recap price facts are aligned with the armed
  truth (band rule + Max +1): the bot help guide (my + en) states
  the band rule, and the Group AI fact sheet follows the live
  switch — legacy wording while off, band wording once armed, so
  the AI never quotes a price that is not in force.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

USER_ID = 616161
ADMIN_ID = 313131


class _LedgerHandle:
    """Real-SQLite stand-in for the credit helpers' session factory
    (mirrors tests/unit/test_batch_ao_2026_11_05.py)."""

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

    db_file = str(tmp_path / "batch_be_credits.db")
    sync_engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    handle = _LedgerHandle(db_file)
    monkeypatch.setattr(
        "app.db.session.AsyncSessionLocal", handle.make_session
    )
    monkeypatch.setattr(settings_obj, "admin_ids_raw", str(ADMIN_ID))
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


# ── Surcharge helpers ────────────────────────────────────────────────

@pytest.mark.parametrize(
    "quality,expected",
    [
        ("max", 1),
        ("MAX", 1),
        (" max ", 1),
        ("standard", 0),
        ("high", 0),
        ("", 0),
        (None, 0),
    ],
)
def test_max_quality_surcharge_math(quality, expected):
    from app.webapp.api import _recap_max_quality_surcharge

    assert _recap_max_quality_surcharge(quality) == expected


@pytest.mark.parametrize(
    "entry,expected",
    [
        ({"max_quality_surcharge": 1}, 1),
        ({"max_quality_surcharge": 0}, 0),
        ({"max_quality_surcharge": None}, 0),
        ({}, 0),  # predates the stamp — no surcharge read here
        ({"max_quality_surcharge": 5}, 1),  # clamped to a boolean-ish 1
        ({"max_quality_surcharge": "x"}, 0),
    ],
)
def test_entry_max_quality_surcharge_stamp(entry, expected):
    from app.webapp.api import _entry_max_quality_surcharge

    assert _entry_max_quality_surcharge(entry) == expected


# ── Segmented top-up: band + surcharge when tiered ───────────────────

def test_segmented_tiered_max_charges_band_plus_one(ledger):
    from app.webapp.api import (
        _refund_recap_job_charge,
        _top_up_recap_segment_charge,
    )

    ledger.add_user(credits=19)
    # 25 min → band 9; Max quality → +1. Split made only 3 parts.
    entry = {
        "credit_deducted": True,
        "credits_charged": 1,
        "tiered_pricing": True,
        "max_quality_surcharge": 1,
        "source_duration_seconds": 1500.0,
    }
    total = asyncio.run(_top_up_recap_segment_charge(USER_ID, entry, 3))
    assert total == 10
    assert entry["credits_charged"] == 10
    assert ledger.credits() == 10  # 20 − 10 in total

    # Idempotent re-run, then a failure refunds band + surcharge.
    assert asyncio.run(_top_up_recap_segment_charge(USER_ID, entry, 3)) == 10
    assert ledger.credits() == 10
    assert asyncio.run(_refund_recap_job_charge(USER_ID, entry)) is True
    assert ledger.credits() == 20
    assert entry["credits_refunded"] == 10


def test_segmented_tiered_standard_has_no_surcharge(ledger):
    from app.webapp.api import _top_up_recap_segment_charge

    ledger.add_user(credits=19)
    entry = {
        "credit_deducted": True,
        "credits_charged": 1,
        "tiered_pricing": True,
        "max_quality_surcharge": 0,
        "source_duration_seconds": 1500.0,
    }
    assert asyncio.run(_top_up_recap_segment_charge(USER_ID, entry, 3)) == 9
    assert ledger.credits() == 11


def test_segmented_surcharge_pinned_by_stamp_not_live_switch(ledger, monkeypatch):
    from app.webapp.api import _top_up_recap_segment_charge

    ledger.add_user(credits=30)
    # Submitted tiered+Max (stamps on), then the switch flipped OFF:
    # the job still pays band + surcharge — its pinned price.
    entry = {
        "credit_deducted": True,
        "credits_charged": 1,
        "tiered_pricing": True,
        "max_quality_surcharge": 1,
        "source_duration_seconds": 1500.0,
    }
    assert asyncio.run(_top_up_recap_segment_charge(USER_ID, entry, 3)) == 10

    # Submitted under the old rate (stamp False) with a stray
    # surcharge value present: the legacy per-part formula rules and
    # the surcharge is never consulted.
    legacy_entry = {
        "credit_deducted": True,
        "credits_charged": 1,
        "tiered_pricing": False,
        "max_quality_surcharge": 1,
        "source_duration_seconds": 1500.0,
    }
    assert asyncio.run(
        _top_up_recap_segment_charge(USER_ID, legacy_entry, 3)
    ) == 3


# ── Single-job band top-up: surcharge reaches the charge helper ──────

def test_single_job_band_one_max_tops_up_by_surcharge(ledger):
    from app.webapp.api import (
        _apply_recap_band_top_up,
        _refund_recap_job_charge,
    )

    ledger.add_user(credits=4)
    # 100-second source: band 1 — a plain band-1 job never tops up,
    # but with Max the total is 2, so exactly the surcharge moves.
    entry = {
        "credit_deducted": True,
        "credits_charged": 1,
        "tiered_pricing": True,
        "max_quality_surcharge": 1,
        "source_duration_seconds": 100.0,
    }
    total = asyncio.run(
        _apply_recap_band_top_up(USER_ID, entry, is_admin_user=False)
    )
    assert total == 2
    assert entry["credits_charged"] == 2
    assert ledger.credits() == 3  # 5 − 2 in total

    assert asyncio.run(_refund_recap_job_charge(USER_ID, entry)) is True
    assert ledger.credits() == 5
    assert entry["credits_refunded"] == 2


def test_single_job_band_plus_surcharge_and_insufficient(ledger):
    from app.webapp.api import (
        _RecapInsufficientCreditsError,
        _apply_recap_band_top_up,
    )

    ledger.add_user(credits=5)
    # 5:24 → band 2, Max → total 3.
    entry = {
        "credit_deducted": True,
        "credits_charged": 1,
        "tiered_pricing": True,
        "max_quality_surcharge": 1,
        "source_duration_seconds": 324.0,
    }
    total = asyncio.run(
        _apply_recap_band_top_up(USER_ID, entry, is_admin_user=False)
    )
    assert total == 3
    assert ledger.credits() == 3  # seeded 5 (post-submit) − 2 top-up

    # A balance that covers the band but NOT band + surcharge fails
    # clean, naming the full total, charging nothing extra.
    ledger.add_user(uid=USER_ID + 5, credits=1)
    poor_entry = {
        "credit_deducted": True,
        "credits_charged": 1,
        "tiered_pricing": True,
        "max_quality_surcharge": 1,
        "source_duration_seconds": 324.0,
    }
    with pytest.raises(_RecapInsufficientCreditsError) as excinfo:
        asyncio.run(
            _apply_recap_band_top_up(USER_ID + 5, poor_entry, is_admin_user=False)
        )
    assert excinfo.value.total_credits == 3
    assert ledger.credits(USER_ID + 5) == 1


def test_single_job_surcharge_inert_when_not_tiered(ledger):
    from app.webapp.api import _apply_recap_band_top_up

    ledger.add_user(credits=4)
    # No tier stamp + switch off: even a stamped surcharge is inert —
    # the flag-off world is byte-identical.
    entry = {
        "credit_deducted": True,
        "credits_charged": 1,
        "max_quality_surcharge": 1,
        "source_duration_seconds": 324.0,
    }
    total = asyncio.run(
        _apply_recap_band_top_up(USER_ID, entry, is_admin_user=False)
    )
    assert total == 1
    assert ledger.credits() == 4

    # Admins are never charged, surcharge or not.
    ledger.add_user(uid=ADMIN_ID, credits=7)
    admin_entry = {
        "credit_deducted": True,
        "credits_charged": 1,
        "tiered_pricing": True,
        "max_quality_surcharge": 1,
        "source_duration_seconds": 1500.0,
    }
    assert asyncio.run(
        _apply_recap_band_top_up(ADMIN_ID, admin_entry, is_admin_user=True)
    ) == 1
    assert ledger.credits(ADMIN_ID) == 7


# ── Submit path (create_recap_job) ───────────────────────────────────

def _submit(ledger, monkeypatch, *, uid=USER_ID, credits=5,
            category="PURCHASED", **req_over):
    """Drive create_recap_job against the ledger DB with a fake
    runner (mirrors tests/unit/test_batch_ao_2026_11_05.py)."""
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
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return ("ok", resp)

    return asyncio.run(_main())


def test_submit_on_max_quotes_band_plus_one_and_stamps(ledger, monkeypatch):
    from app.webapp import api as api_mod
    from app.webapp.api import (
        _apply_recap_band_top_up,
        _refund_recap_job_charge,
    )

    _tiered_on(monkeypatch)
    result = _submit(
        ledger, monkeypatch, credits=5, duration_seconds=324,
        render_quality="max",
    )
    assert result[0] == "ok"
    resp = result[1]
    # 5:24 → band 2, Max → quote 3; the submit still takes 1 credit.
    assert resp["credits_total"] == 3
    assert resp["credits_charged"] == 1
    assert ledger.credits() == 4

    entry = api_mod.RECAP_JOBS_REGISTRY[resp["recap_id"]]
    assert entry["tiered_pricing"] is True
    assert entry["max_quality_surcharge"] == 1

    # The worker charges exactly the quote; a failure refunds it all.
    charged = asyncio.run(
        _apply_recap_band_top_up(USER_ID, entry, is_admin_user=False)
    )
    assert charged == resp["credits_total"]
    assert ledger.credits() == 2  # 5 − 3 in total
    assert asyncio.run(_refund_recap_job_charge(USER_ID, entry)) is True
    assert ledger.credits() == 5
    assert entry["credits_refunded"] == 3

    # The persisted blob carries the pinned surcharge for restarts.
    import json

    async def _blob():
        from app.models.schema import Job

        async with ledger.make_session() as s:
            job = await s.get(Job, resp["recap_id"])
            return json.loads(job.recap_config)

    state = asyncio.run(_blob())
    assert state["max_quality_surcharge"] == 1
    assert state["tiered_pricing"] is True


def test_submit_on_standard_and_high_have_no_surcharge(ledger, monkeypatch):
    from app.webapp import api as api_mod

    _tiered_on(monkeypatch)
    for quality in ("standard", "high"):
        uid = USER_ID + (10 if quality == "high" else 0)
        result = _submit(
            ledger, monkeypatch, uid=uid, credits=5,
            duration_seconds=324, render_quality=quality,
        )
        assert result[0] == "ok"
        resp = result[1]
        assert resp["credits_total"] == 2  # band only
        entry = api_mod.RECAP_JOBS_REGISTRY[resp["recap_id"]]
        assert entry["max_quality_surcharge"] == 0


def test_submit_on_max_blocks_when_balance_covers_band_only(ledger, monkeypatch):
    _tiered_on(monkeypatch)
    # Band is 2 and the user holds exactly 2 — Max makes the total 3,
    # so the submit is refused before any credit moves.
    result = _submit(
        ledger, monkeypatch, credits=2, duration_seconds=324,
        render_quality="max",
    )
    assert result[0] == "error"
    assert result[1] == 403
    assert "3 Credits" in result[2]
    assert ledger.credits() == 2


def test_submit_on_segmented_max_quote_includes_surcharge(ledger, monkeypatch):
    _tiered_on(monkeypatch)
    # 25 minutes → band 9; Max → 10. A balance of 9 covered the old
    # band and must now be refused; 10 is accepted with quote 10.
    result = _submit(
        ledger, monkeypatch, credits=9, duration_seconds=1500,
        split_video=True, split_minutes=10, render_quality="max",
    )
    assert result[0] == "error"
    assert result[1] == 403
    assert "10 Credits" in result[2]
    assert ledger.credits() == 9

    result = _submit(
        ledger, monkeypatch, uid=USER_ID + 2, credits=10,
        duration_seconds=1500, split_video=True, split_minutes=10,
        render_quality="max",
    )
    assert result[0] == "ok"
    resp = result[1]
    assert resp["estimated_parts"] == 3
    assert resp["credits_total"] == 10
    assert resp["credits_charged"] == 1


def test_submit_off_max_is_legacy_byte_identical(ledger, monkeypatch):
    from app.webapp import api as api_mod

    # Switch OFF + Max: no tiered keys anywhere, one credit charged,
    # surcharge stamp is 0 — the pre-AO world exactly.
    result = _submit(
        ledger, monkeypatch, credits=5, duration_seconds=324,
        render_quality="max",
    )
    assert result[0] == "ok"
    resp = result[1]
    assert "credits_total" not in resp
    assert resp["credits_charged"] == 1
    assert ledger.credits() == 4
    entry = api_mod.RECAP_JOBS_REGISTRY[resp["recap_id"]]
    assert entry["tiered_pricing"] is False
    assert entry["max_quality_surcharge"] == 0


def test_submit_on_free_user_max_is_clamped_before_pricing(ledger, monkeypatch):
    from app.webapp import api as api_mod

    _tiered_on(monkeypatch)
    # A FREE user asking for Max is AU-clamped to Standard at
    # submit, so no surcharge can accrue: 100s → band 1, quote 1.
    result = _submit(
        ledger, monkeypatch, credits=3, category="FREE",
        duration_seconds=100, render_quality="max",
    )
    assert result[0] == "ok"
    resp = result[1]
    assert resp["credits_total"] == 1
    assert resp["credits_charged"] == 1
    entry = api_mod.RECAP_JOBS_REGISTRY[resp["recap_id"]]
    assert entry["max_quality_surcharge"] == 0
    assert ledger.credits() == 2


# ── BE-2: admin toggle rides the audited write path ──────────────────

def test_admin_settings_toggle_round_trip(ledger):
    from sqlalchemy import select

    from app.models.schema import AuditLog, BotSetting, User
    from app.webapp import api as api_mod
    from app.webapp.api import _tiered_recap_pricing_enabled

    assert _tiered_recap_pricing_enabled() is False

    async def _main():
        async with ledger.make_session() as s:
            s.add(
                User(id=ADMIN_ID, recap_credits=0, account_category="PURCHASED")
            )
            await s.commit()
            admin = await s.get(User, ADMIN_ID)
            payload = api_mod.AdminSettingsPayload(
                tiered_recap_pricing_enabled=True
            )
            resp = await api_mod.update_admin_settings(
                payload, admin=admin, db=s
            )
            assert resp["success"] is True
            row = (
                await s.execute(
                    select(BotSetting).where(
                        BotSetting.key == "tiered_recap_pricing_enabled"
                    )
                )
            ).scalar_one()
            assert row.value == "true"
            audit = (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "settings.update",
                        AuditLog.target_id == "tiered_recap_pricing_enabled",
                    )
                )
            ).scalar_one()
            assert audit.admin_id == ADMIN_ID

    asyncio.run(_main())
    # The live reader the pricing paths consult honours the flip.
    assert _tiered_recap_pricing_enabled() is True


# ── BE-3: price-fact alignment ───────────────────────────────────────

def test_fact_sheet_follows_the_switch(ledger, monkeypatch):
    from app.services.group_ai_admin import build_fact_sheet

    off = build_fact_sheet()
    assert "၁ ကြိမ်ကို Recap Credit 1 ခု" in off
    assert "အပိုင်း ၁ ပိုင်းကို Credit 1 ခု စီ ကျပါတယ်" in off
    assert "Max အရည်အသွေး ရွေးရင်" not in off

    _tiered_on(monkeypatch)
    on = build_fact_sheet()
    assert "၃ မိနစ် တစ်ပိုင်းလျှင်" in on
    assert "Recap Credit 1 ခု (ဥပမာ ၁၀ မိနစ် = 4 Credits)" in on
    assert "+1 Credit ထပ်ကျပါတယ်" in on
    assert "အပိုင်း ၁ ပိုင်းကို Credit 1 ခု စီ ကျပါတယ်" not in on


def test_help_guide_states_band_rule_both_locales():
    import json

    my = json.loads(
        (REPO_ROOT / "locales/my/messages.json").read_text(encoding="utf-8")
    )
    en = json.loads(
        (REPO_ROOT / "locales/en/messages.json").read_text(encoding="utf-8")
    )
    my_guide = my["help_settings_guide"]
    assert "၃ မိနစ် တစ်ပိုင်းလျှင် 1 Credit" in my_guide
    assert "Max အရည်အသွေး ရွေးချယ်လျှင် +1 Credit" in my_guide
    assert "အပိုင်းတစ်ခုလျှင် 1 Credit ကျသင့်ပါသည်" not in my_guide

    en_guide = en["help_settings_guide"]
    assert "1 credit per started 3 minutes" in en_guide
    assert "+1 credit when you choose Max render quality" in en_guide
    assert "1 credit per part" not in en_guide


# ── Wiring contracts (API / Mini App / i18n / token) ─────────────────

def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_batch_be_wiring_contracts():
    api_py = _read("app/webapp/api.py")
    app_js = _read("app/webapp/static/js/app.js")
    i18n_js = _read("app/webapp/static/js/i18n.js")
    html = _read("app/webapp/static/index.html")

    # BE-1: helpers + both top-up targets + submit pin + blob pin +
    # execution-entry derivation for unstamped entries.
    assert "def _recap_max_quality_surcharge(" in api_py
    assert "def _entry_max_quality_surcharge(" in api_py
    assert api_py.count("+ _entry_max_quality_surcharge(entry)") == 2
    assert '"max_quality_surcharge": submit_max_surcharge' in api_py
    assert "max_quality_surcharge=submit_max_surcharge" in api_py
    assert "submit_max_surcharge = (" in api_py
    assert 'entry["max_quality_surcharge"] = (' in api_py
    assert "_tiered_recap_pricing_enabled()" in api_py

    # BE-1 quote: the tiered branch adds the surcharge from the
    # selected quality and shows the breakdown in BOTH surfaces
    # (cost hint + Step-4 line); quality changes repaint the quote.
    assert 'state.recapRenderQuality === "max"' in app_js
    assert 'tf("recap_tiered_cost_estimate_max"' in app_js
    assert 'tf("recap_step4_quote_breakdown"' in app_js
    assert 'id="recap-step4-quote-breakdown"' in html
    for key in (
        "recap_tiered_cost_estimate_max:",
        "recap_step4_quote_breakdown:",
    ):
        assert i18n_js.count(key) == 2, key
    assert "{duration}" in i18n_js and "{total}" in i18n_js

    # BE-2: toggle element + load + save wiring + i18n pair, riding
    # the existing payload field and GET exposure.
    assert 'id="adm-set-tiered-pricing"' in html
    assert 'data-i18n="settings_tiered_pricing"' in html
    # Batch BY: the load paint goes through the dirty guard helper.
    assert "_admPaintChecked(elTieredPricing, !!s.tiered_recap_pricing_enabled)" in app_js
    assert "tiered_recap_pricing_enabled: tieredPricing" in app_js
    assert '"tiered_recap_pricing_enabled": bool(getattr(settings,' in api_py
    for key in ("settings_tiered_pricing:", "settings_tiered_pricing_hint:"):
        assert i18n_js.count(key) == 2, key


def test_be_cache_token_is_r79():
    html = _read("app/webapp/static/index.html")
    assert html.count("?v=20261104_r109") == 4
    assert "20261104_r78" not in html
